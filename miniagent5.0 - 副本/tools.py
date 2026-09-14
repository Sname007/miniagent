import asyncio
import base64
import os
import shutil

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600
MAX_OUTPUT_CHARS = 30000

OK = "ok"
ERROR = "error"
TIMEOUT = "timeout"
STOPPED = "stopped"

NAME = "bash"
SCHEMA = {
    "type": "function",
    "name": NAME,
    "description": (
        "在持久 PowerShell 会话中执行命令，返回标准输出（stdout）和标准错误（stderr）。\n"
        "- 会话状态跨调用保留：当前目录、变量、函数、后台任务都会延续到下一次调用\n"
        "- 命令中的所有路径必须使用绝对路径\n"
        f"- workdir：先切换工作目录，必须为绝对路径；不传则沿用上一次的目录（初始为 {PROJECT_ROOT}）\n"
        f"- timeout：单条命令超时（秒），默认 {DEFAULT_TIMEOUT}，最大 {MAX_TIMEOUT}；超时会重启 shell，状态丢失\n"
        "- 不要执行需要交互输入的命令（会一直阻塞到超时）\n"
        "- 非零退出码以 [exit code: N] 标注，输出过长会被截断"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的 PowerShell 命令，其中所有路径必须为绝对路径"},
            "timeout": {"type": "integer", "description": f"超时时间（秒），默认 {DEFAULT_TIMEOUT}，最大 {MAX_TIMEOUT}"},
            "workdir": {"type": "string", "description": f"命令执行的工作目录（绝对路径），切换后会保留给后续调用，初始为 {PROJECT_ROOT}"},
        },
        "required": ["command"],
    },
}

READY = "___MINIAGENT_READY___"
END = "___MINIAGENT_END___"

BOOTSTRAP = "\n".join([
    "$ErrorActionPreference = 'Continue'",
    "[Console]::OutputEncoding = [Text.Encoding]::UTF8",
    "Write-Output '" + READY + "'",
    "while ($true) {",
    "  $line = [Console]::In.ReadLine()",
    "  if ($null -eq $line) { break }",
    "  $src = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($line))",
    "  $sb = [ScriptBlock]::Create($src)",
    "  $global:LASTEXITCODE = 0",
    "  $ok = $true",
    "  try { $out = . $sb 2>&1; $ok = $? }",
    "  catch { $out = $_; $ok = $false }",
    "  $text = (@($out) | ForEach-Object { if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message } else { $_ } } | Out-String).TrimEnd()",
    "  if ($text) { [Console]::Out.WriteLine($text) }",
    "  if ($ok) { $code = 0 + $LASTEXITCODE } else { $code = 1 }",
    "  [Console]::Out.WriteLine('" + END + "' + $code)",
    "  [Console]::Out.Flush()",
    "}",
])


class _ShellExited(Exception):
    pass


class _Shell:
    def __init__(self, exe):
        self._exe = exe
        self.proc = None
        self.buffer = b""
        self.dead = False

    async def start(self):
        encoded = base64.b64encode(BOOTSTRAP.encode("utf-16-le")).decode("ascii")
        self.proc = await asyncio.create_subprocess_exec(
            self._exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=PROJECT_ROOT,
        )
        try:
            await self._read_until(READY, 20)
        except (_ShellExited, asyncio.TimeoutError) as exc:
            self.dead = True
            raise RuntimeError(f"PowerShell 启动失败: {exc}") from exc

    async def execute(self, script, limit, cancel_event):
        line = base64.b64encode(script.encode("utf-8")).decode("ascii") + "\n"
        try:
            self.proc.stdin.write(line.encode("ascii"))
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            self.dead = True
            raise _ShellExited(str(exc)) from exc

        reader = asyncio.ensure_future(self._read_until(END, None))
        stop = asyncio.ensure_future(cancel_event.wait()) if cancel_event is not None else None
        waiters = {reader} | ({stop} if stop is not None else set())
        try:
            done, _ = await asyncio.wait(waiters, timeout=limit, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if stop is not None and not stop.done():
                stop.cancel()
            if reader not in done:
                reader.cancel()

        if reader in done:
            text, code = reader.result()
            return text, code, OK
        self.dead = True
        if stop is not None and stop.done() and not stop.cancelled():
            return "", 0, STOPPED
        return "", 0, TIMEOUT

    async def _read_until(self, marker, timeout):
        marker_bytes = marker.encode("ascii")
        while marker_bytes not in self.buffer:
            chunk = await asyncio.wait_for(self.proc.stdout.read(4096), timeout)
            if not chunk:
                self.dead = True
                raise _ShellExited("shell 已退出")
            self.buffer += chunk
        index = self.buffer.index(marker_bytes)
        head = self.buffer[:index]
        rest = self.buffer[index + len(marker_bytes):]
        if marker == END:
            digits = b""
            while rest[:1].isdigit():
                digits += rest[:1]
                rest = rest[1:]
            if rest.startswith(b"\r\n"):
                rest = rest[2:]
            elif rest.startswith(b"\n"):
                rest = rest[1:]
            code = int(digits or b"0")
        else:
            code = 0
            if rest.startswith(b"\r\n"):
                rest = rest[2:]
            elif rest.startswith(b"\n"):
                rest = rest[1:]
        self.buffer = rest
        return _decode(head), code

    async def terminate(self):
        self.dead = True
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        if os.name == "nt":
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/PID", str(proc.pid), "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await asyncio.wait_for(killer.wait(), 5)
            except (OSError, asyncio.TimeoutError):
                pass
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass


class _Session:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.shell = None


class Shells:
    def __init__(self):
        self._sessions = {}

    async def run(self, session_id, command=None, timeout=DEFAULT_TIMEOUT, workdir=None,
                  cancel_event=None, **kwargs):
        if not command or not str(command).strip():
            return "command 参数不能为空", ERROR
        if workdir:
            if not os.path.isabs(workdir):
                return f"workdir 必须是绝对路径: {workdir}", ERROR
            if not os.path.isdir(workdir):
                return f"工作目录不存在: {workdir}", ERROR

        exe = shutil.which("pwsh") or shutil.which("powershell")
        if not exe:
            return "未找到 PowerShell（pwsh 或 powershell.exe），无法执行命令", ERROR
        try:
            limit = max(1, min(int(timeout), MAX_TIMEOUT))
        except (TypeError, ValueError):
            limit = DEFAULT_TIMEOUT

        script = str(command)
        if workdir:
            script = f"Set-Location -LiteralPath '{os.path.abspath(workdir)}'\n{script}"

        session = self._sessions.setdefault(str(session_id), _Session())
        async with session.lock:
            if session.shell is None or session.shell.dead:
                session.shell = _Shell(exe)
                try:
                    await session.shell.start()
                except Exception as exc:
                    session.shell = None
                    return str(exc), ERROR
            shell = session.shell
            try:
                text, code, status = await shell.execute(script, limit, cancel_event)
            except _ShellExited as exc:
                await self._drop(session)
                return f"shell 已退出（{exc}），下一次调用会新建 shell", ERROR
            if status != OK or shell.dead:
                await self._drop(session)

        if status == STOPPED:
            return "命令被中断（用户停止生成），shell 已重启：当前目录与变量已重置", STOPPED
        if status == TIMEOUT:
            return f"命令执行超时（{limit}秒），shell 已重启：当前目录与变量已重置", TIMEOUT

        out = text.rstrip()
        if code:
            out = f"{out}\n[exit code: {code}]" if out else f"[exit code: {code}]"
        if not out:
            out = "命令执行成功（无输出）"
        if len(out) > MAX_OUTPUT_CHARS:
            out = out[:MAX_OUTPUT_CHARS] + f"\n...（输出过长已截断，共 {len(out)} 字符）"
        return out, OK

    async def close(self, session_id):
        session = self._sessions.pop(str(session_id), None)
        if session is not None and session.shell is not None:
            await session.shell.terminate()
            session.shell = None

    async def close_all(self):
        for session in list(self._sessions.values()):
            if session.shell is not None:
                await session.shell.terminate()
                session.shell = None
        self._sessions.clear()

    @staticmethod
    async def _drop(session):
        if session.shell is not None:
            await session.shell.terminate()
            session.shell = None


def _decode(data):
    if not data:
        return ""
    encodings = ["utf-8", "oem", "mbcs"] if os.name == "nt" else ["utf-8"]
    candidates = []
    for encoding in encodings:
        try:
            candidates.append(data.decode(encoding).lstrip("\ufeff"))
        except (UnicodeDecodeError, LookupError):
            continue
    if not candidates:
        return data.decode("utf-8", errors="replace").lstrip("\ufeff")
    for text in candidates:
        if any("\u4e00" <= ch <= "\u9fff" for ch in text):
            return text
    return candidates[0]

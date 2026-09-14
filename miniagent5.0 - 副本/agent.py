import asyncio
import json

from openai import AsyncOpenAI

import config
import sessions
import tools

SYSTEM_PROMPT = "你是一个功能强大的AI Agent，可以通过 bash 工具执行 PowerShell 命令来完成任务。"


class _Cancelled(Exception):
    pass


def _get(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _error_text(exc):
    for attr in ("message", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, str) and value:
            return value
    return str(exc) or exc.__class__.__name__


def build_input(messages):
    answered = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
    declared = {c.get("id") for m in messages if m.get("role") == "assistant"
                for c in (m.get("tool_calls") or []) if c.get("id")}

    items = []
    for msg in messages:
        role = msg.get("role")
        if role == "user":
            content = msg.get("content")
            if isinstance(content, str) and content:
                items.append({"role": "user", "content": content})

        elif role == "assistant":
            turn = []
            content = msg.get("content")
            if isinstance(content, str) and content:
                turn.append({"type": "message", "role": "assistant",
                             "content": [{"type": "output_text", "text": content}]})
            for call in msg.get("tool_calls") or []:
                call_id = call.get("id")
                if call_id and call_id in answered:
                    function = call.get("function") or {}
                    turn.append({"type": "function_call", "call_id": call_id,
                                 "name": function.get("name") or "",
                                 "arguments": function.get("arguments") or "{}"})
            reasoning = msg.get("reasoning_content")
            if turn and isinstance(reasoning, str) and reasoning:
                items.append({"type": "reasoning",
                              "content": [{"type": "reasoning_text", "text": reasoning}]})
            items.extend(turn)

        elif role == "tool":
            call_id = msg.get("tool_call_id")
            if call_id and call_id in declared:
                content = msg.get("content")
                items.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
                })
    return items


class StreamCollector:
    def __init__(self):
        self.text = ""
        self.reasoning = ""
        self.calls = {}
        self.usage = None
        self.status = "in_progress"
        self.incomplete_reason = None
        self.error = None
        self.terminal = False
        self.response_id = None

    def handle(self, event):
        kind = _get(event, "type", "") or ""

        if kind in ("response.created", "response.in_progress"):
            self.response_id = _get(_get(event, "response"), "id") or self.response_id

        elif kind == "response.reasoning_text.delta":
            delta = _get(event, "delta") or ""
            self.reasoning += delta
            return [{"type": "thinking", "content": delta}] if delta else []

        elif kind == "response.output_text.delta":
            delta = _get(event, "delta") or ""
            self.text += delta
            return [{"type": "delta", "content": delta}] if delta else []

        elif kind == "response.output_item.added":
            return self._item(_get(event, "item"), _get(event, "output_index", 0), final=False)

        elif kind == "response.output_item.done":
            return self._item(_get(event, "item"), _get(event, "output_index", 0), final=True)

        elif kind == "response.function_call_arguments.delta":
            call = self._call(_get(event, "item_id"), _get(event, "output_index", 0))
            call["arguments"] += _get(event, "delta") or ""

        elif kind == "response.function_call_arguments.done":
            call = self._call(_get(event, "item_id"), _get(event, "output_index", 0))
            arguments = _get(event, "arguments")
            if isinstance(arguments, str):
                call["arguments"] = arguments

        elif kind == "response.completed":
            self._finish(event, "completed")

        elif kind == "response.incomplete":
            self._finish(event, "incomplete")
            self.incomplete_reason = _get(_get(_get(event, "response"), "incomplete_details"), "reason")

        elif kind == "response.failed":
            self._finish(event, "failed")
            error = _get(_get(event, "response"), "error")
            self.error = _get(error, "message") or _get(error, "code") or "生成失败"

        elif kind == "error":
            self.terminal = True
            self.status = "failed"
            self.error = _get(event, "message") or _get(event, "code") or "Responses API 流式错误"

        return []

    def valid_calls(self):
        return [call for call in self.calls.values() if call["call_id"] and call["name"]]

    def assistant_message(self, include_calls=True):
        calls = []
        if include_calls:
            for call in self.valid_calls():
                calls.append({"id": call["call_id"], "type": "function",
                              "function": {"name": call["name"], "arguments": call["arguments"] or "{}"}})
        if not (self.text or self.reasoning or calls):
            return None
        message = {"role": "assistant", "content": self.text}
        if self.reasoning:
            message["reasoning_content"] = self.reasoning
        if calls:
            message["tool_calls"] = calls
        return message

    def _finish(self, event, status):
        response = _get(event, "response")
        self.terminal = True
        self.status = status
        self.response_id = _get(response, "id") or self.response_id
        self.usage = _usage(_get(response, "usage")) or self.usage

    def _call(self, item_id, output_index):
        key = item_id or f"index:{output_index}"
        if key not in self.calls:
            self.calls[key] = {"call_id": "", "name": "", "arguments": "", "started": False}
        return self.calls[key]

    def _item(self, item, output_index, final):
        kind = _get(item, "type", "")

        if kind == "function_call":
            call = self._call(_get(item, "id"), output_index)
            call["call_id"] = _get(item, "call_id") or call["call_id"]
            call["name"] = _get(item, "name") or call["name"]
            if final:
                arguments = _get(item, "arguments")
                if isinstance(arguments, str) and arguments:
                    call["arguments"] = arguments
            if call["started"] or not (call["call_id"] and call["name"]):
                return []
            call["started"] = True
            return [{"type": "tool_start", "id": call["call_id"], "name": call["name"]}]

        if not final:
            return []
        if kind == "message":
            self.text, event = self._complete(self.text, _message_text(item), "delta")
        elif kind == "reasoning":
            self.reasoning, event = self._complete(self.reasoning, _reasoning_text(item), "thinking")
        else:
            event = None
        return [event] if event else []

    @staticmethod
    def _complete(current, full, event_type):
        if not full or full == current:
            return current, None
        piece = full[len(current):] if full.startswith(current) else full
        return full, {"type": event_type, "content": piece}


def _content_text(item, types):
    content = _get(item, "content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(_get(part, "text") or "" for part in content if _get(part, "type", "") in types)


def _message_text(item):
    return _content_text(item, ("output_text", "text"))


def _reasoning_text(item):
    return _content_text(item, ("reasoning_text", "text"))


def _usage(usage):
    if not usage:
        return None
    input_tokens = _get(usage, "input_tokens", 0) or 0
    output_tokens = _get(usage, "output_tokens", 0) or 0
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": _get(usage, "total_tokens") or (input_tokens + output_tokens),
        "cached_tokens": _get(_get(usage, "input_tokens_details"), "cached_tokens", 0) or 0,
        "reasoning_tokens": _get(_get(usage, "output_tokens_details"), "reasoning_tokens", 0) or 0,
    }


async def _stream_events(stream, cancel_event):
    try:
        if cancel_event is None:
            async for event in stream:
                yield event
            return

        iterator = stream.__aiter__()
        stop = asyncio.ensure_future(cancel_event.wait())
        try:
            while True:
                nxt = asyncio.ensure_future(iterator.__anext__())
                done, _ = await asyncio.wait({nxt, stop}, return_when=asyncio.FIRST_COMPLETED)
                if stop in done:
                    nxt.cancel()
                    try:
                        await nxt
                    except (asyncio.CancelledError, StopAsyncIteration):
                        pass
                    raise _Cancelled()
                try:
                    yield nxt.result()
                except StopAsyncIteration:
                    return
        finally:
            stop.cancel()
    finally:
        try:
            await stream.close()
        except Exception:
            pass


def _unfinished_tool_events(calls, note):
    for call in calls:
        if call["started"]:
            yield {"type": "tool_end", "id": call["call_id"], "name": call["name"],
                   "args": call["arguments"], "result": note, "status": tools.STOPPED}


class Agent:
    def __init__(self):
        self.api_key = config.API_KEY
        self.base_url = config.BASE_URL
        self.model = config.MODEL_NAME
        self.effort = config.REASONING_EFFORT
        self.shells = tools.Shells()
        self._new_client()

    async def close(self):
        await self.shells.close_all()

    def _new_client(self):
        self.client = AsyncOpenAI(api_key=self.api_key or "EMPTY",
                                  base_url=self.base_url,
                                  timeout=config.API_TIMEOUT)

    def update_config(self, api_key=None, base_url=None, model=None, effort=None):
        if api_key:
            self.api_key = api_key
        if base_url:
            self.base_url = base_url
        if model:
            self.model = model
        if effort:
            self.effort = effort
        self._new_client()

    def _params(self, messages):
        params = {
            "model": self.model,
            "instructions": SYSTEM_PROMPT,
            "input": build_input(messages),
            "tools": [tools.SCHEMA],
            "stream": True,
        }
        if self.effort:
            params["reasoning"] = {"effort": self.effort}
        return params

    async def run(self, session_id, message, cancel_event=None):
        history = await sessions.load_history(session_id)
        history.append({"role": "user", "content": message})
        await sessions.save_history(session_id, history)

        answer = ""
        while True:
            if cancel_event is not None and cancel_event.is_set():
                yield {"type": "stopped", "full_response": answer}
                return

            try:
                stream = await self.client.responses.create(**self._params(history))
            except Exception as exc:
                yield {"type": "error", "message": f"请求失败: {_error_text(exc)}"}
                return

            collector = StreamCollector()
            cancelled = False
            stream_error = None
            try:
                async for event in _stream_events(stream, cancel_event):
                    for item in collector.handle(event):
                        yield item
            except _Cancelled:
                cancelled = True
            except Exception as exc:
                stream_error = _error_text(exc)

            answer += collector.text
            if collector.usage:
                yield {"type": "usage", "response_id": collector.response_id, **collector.usage}

            failure = stream_error or collector.error
            if not failure and collector.status == "incomplete" and collector.incomplete_reason not in (None, "max_output_tokens"):
                failure = f"生成被中断: {collector.incomplete_reason}"
            if not failure and not collector.terminal and not (collector.text or collector.reasoning or collector.valid_calls()):
                failure = "响应流意外结束（未收到 response.completed / incomplete / failed）"

            if cancelled or failure:
                partial = collector.assistant_message(include_calls=False)
                if partial:
                    history.append(partial)
                    await sessions.save_history(session_id, history)
                note = "已停止生成，工具未执行" if cancelled else "生成中断，工具未执行"
                for event in _unfinished_tool_events(collector.valid_calls(), note):
                    yield event
                if cancelled:
                    yield {"type": "stopped", "full_response": answer}
                else:
                    yield {"type": "error", "message": failure}
                return

            calls = collector.valid_calls()
            if not calls:
                assistant = collector.assistant_message()
                if assistant:
                    history.append(assistant)
                    await sessions.save_history(session_id, history)
                done = {"type": "done", "full_response": answer}
                if collector.status == "incomplete":
                    done["truncated"] = True
                yield done
                return

            assistant = collector.assistant_message()
            if assistant:
                history.append(assistant)
            interrupted = False
            pending = []
            for index, call in enumerate(calls):
                if cancel_event is not None and cancel_event.is_set():
                    interrupted, pending = True, calls[index:]
                else:
                    result, status = await self._run_tool(session_id, call, cancel_event)
                    yield {"type": "tool_end", "id": call["call_id"], "name": call["name"],
                           "args": call["arguments"], "result": result, "status": status}
                    history.append({"role": "tool", "tool_call_id": call["call_id"],
                                    "content": result, "status": status})
                    if status != tools.STOPPED:
                        continue
                    interrupted, pending = True, calls[index + 1:]
                note = "已停止生成，工具未执行"
                for call in pending:
                    if not call["started"]:
                        continue
                    history.append({"role": "tool", "tool_call_id": call["call_id"],
                                    "content": note, "status": tools.STOPPED})
                    yield {"type": "tool_end", "id": call["call_id"], "name": call["name"],
                           "args": call["arguments"], "result": note, "status": tools.STOPPED}
                break
            await sessions.save_history(session_id, history)
            if interrupted:
                yield {"type": "stopped", "full_response": answer}
                return

    async def _run_tool(self, session_id, call, cancel_event=None):
        raw = call["arguments"] or ""
        try:
            args = json.loads(raw) if raw.strip() else {}
            if not isinstance(args, dict):
                raise ValueError("工具参数必须是 JSON 对象")
        except (json.JSONDecodeError, ValueError) as exc:
            return f"[参数JSON解析失败] {exc}\n原始参数: {raw}", tools.ERROR
        if call["name"] != tools.NAME:
            return f"未知工具: {call['name']}", tools.ERROR
        try:
            result, status = await self.shells.run(session_id, cancel_event=cancel_event, **args)
        except Exception as exc:
            return f"工具执行异常: {exc}", tools.ERROR
        return (result if isinstance(result, str) else str(result)), status

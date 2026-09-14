import asyncio
import json
import os
import re
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openai import AsyncOpenAI

import config
import sessions
from agent import Agent

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(BASE_DIR, "ui")
CONFIG_PY = os.path.join(BASE_DIR, "config.py")
TEST_TIMEOUT = 15.0

agent = Agent()
active_runs = {}
runs_lock = asyncio.Lock()
index_html = ""


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global index_html
    with open(os.path.join(UI_DIR, "index.html"), "r", encoding="utf-8") as f:
        index_html = f.read()
    yield
    await agent.close()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=UI_DIR), name="static")


def usage_metrics(usage, max_tokens=None):
    max_tokens = config.MAX_TOKENS if max_tokens is None else max_tokens
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    cached_tokens = int(usage.get("cached_tokens") or 0)
    context_tokens = input_tokens + output_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": int(usage.get("total_tokens") or context_tokens),
        "cached_tokens": cached_tokens,
        "reasoning_tokens": int(usage.get("reasoning_tokens") or 0),
        "context_tokens": context_tokens,
        "max_tokens": max_tokens,
        "percentage": round(context_tokens / max_tokens * 100, 2) if max_tokens > 0 else 0.0,
        "cache_hit_rate": round(cached_tokens / input_tokens * 100, 1) if input_tokens > 0 else 0.0,
    }


def _enrich(event):
    if event.get("type") != "usage":
        return event
    enriched = dict(event)
    enriched.update(usage_metrics(event))
    enriched["source"] = "server"
    return enriched


def _write_config_field(key, value):
    literal = str(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else json.dumps(value, ensure_ascii=False)
    with open(CONFIG_PY, "r", encoding="utf-8") as f:
        content = f.read()
    pattern = rf"^{key}\s*=.*$"
    if re.search(pattern, content, re.MULTILINE):
        content = re.sub(pattern, lambda _m: f"{key} = {literal}", content, count=1, flags=re.MULTILINE)
    else:
        content = content.rstrip("\n") + f"\n{key} = {literal}\n"
    compile(content, CONFIG_PY, "exec")
    with open(CONFIG_PY, "w", encoding="utf-8") as f:
        f.write(content)


@app.get("/api/config")
async def get_config():
    return {
        "api_key": agent.api_key,
        "base_url": agent.base_url,
        "model_id": agent.model,
        "reasoning_effort": agent.effort,
        "max_tokens": config.MAX_TOKENS,
    }


@app.post("/api/config")
async def update_config(request: Request):
    async with runs_lock:
        if active_runs:
            return {"status": "error", "message": "有会话正在运行中，请结束后再修改设置"}
    body = await request.json()

    if "max_tokens" in body:
        try:
            max_tokens = int(body["max_tokens"])
        except (TypeError, ValueError):
            return {"status": "error", "message": "Max Tokens 必须是正整数"}
        if max_tokens <= 0:
            return {"status": "error", "message": "Max Tokens 必须是正整数"}
    else:
        max_tokens = None

    fields = {"api_key": "API_KEY", "base_url": "BASE_URL",
              "model_id": "MODEL_NAME", "reasoning_effort": "REASONING_EFFORT"}
    try:
        for body_key, config_key in fields.items():
            if body_key in body:
                await asyncio.to_thread(_write_config_field, config_key, str(body[body_key]))
        if max_tokens is not None:
            await asyncio.to_thread(_write_config_field, "MAX_TOKENS", max_tokens)
    except (OSError, SyntaxError, ValueError) as exc:
        return {"status": "error", "message": f"写入配置失败: {exc}"}

    agent.update_config(
        api_key=body.get("api_key"), base_url=body.get("base_url"),
        model=body.get("model_id"), effort=body.get("reasoning_effort"),
    )
    if max_tokens is not None:
        config.MAX_TOKENS = max_tokens
    return {"status": "ok"}


@app.post("/api/test")
async def test_api(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    base_url = (body.get("base_url") or agent.base_url or "").strip()
    api_key = (body.get("api_key") or agent.api_key or "").strip()
    model = (body.get("model_id") or agent.model or "").strip()
    if not base_url or not model:
        return {"status": "error", "message": "请先填写 Base URL 和 Model ID"}

    client = AsyncOpenAI(api_key=api_key or "EMPTY", base_url=base_url,
                         timeout=TEST_TIMEOUT, max_retries=0)
    stream = None
    started = time.perf_counter()
    try:
        stream = await client.responses.create(model=model, input="ping",
                                               max_output_tokens=16,
                                               reasoning={"effort": "none"}, stream=True)
        async for _event in stream:
            break
    except Exception as exc:
        message = getattr(exc, "message", None) or str(exc) or exc.__class__.__name__
        return {"status": "error", "message": str(message)[:300]}
    finally:
        for resource in (stream, client):
            try:
                await resource.close()
            except Exception:
                pass
    return {"status": "ok", "latency_ms": int((time.perf_counter() - started) * 1000)}


@app.get("/api/sessions")
async def get_sessions():
    return {"sessions": await sessions.list_sessions()}


@app.post("/api/sessions")
async def create_session():
    return {"session_id": await sessions.create_session()}


@app.get("/api/history/{session_id}")
async def get_history(session_id: str):
    return {"history": await sessions.load_history(session_id)}


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    async with runs_lock:
        entry = active_runs.get(session_id)
    if entry is not None:
        entry["cancel"].set()
        await entry["done"].wait()
        async with runs_lock:
            if session_id in active_runs:
                return JSONResponse(status_code=409,
                                    content={"status": "error", "message": "该会话仍在运行中，请稍后再删除"})
    await sessions.delete_session(session_id)
    await agent.shells.close(session_id)
    return {"status": "ok"}


@app.post("/api/sessions/{session_id}/clear")
async def clear_session(session_id: str):
    async with runs_lock:
        if session_id in active_runs:
            return JSONResponse(status_code=409,
                                content={"status": "error", "message": "该会话正在生成中，无法清空"})
    await sessions.clear_session(session_id)
    await agent.shells.close(session_id)
    return {"status": "ok"}


@app.get("/api/context/{session_id}")
async def get_context_info(session_id: str):
    usage = await sessions.load_usage(session_id)
    if usage:
        metrics = usage_metrics(usage)
        metrics.update({
            "source": "server",
            "token_count": metrics["context_tokens"],
            "model": usage.get("model"),
            "updated_at": usage.get("updated_at"),
            "response_id": usage.get("response_id"),
        })
        return metrics

    messages = await sessions.load_history(session_id)
    token_count = await asyncio.to_thread(sessions.count_tokens, messages)
    return {
        "source": "local",
        "token_count": token_count,
        "context_tokens": token_count,
        "max_tokens": config.MAX_TOKENS,
        "percentage": round(token_count / config.MAX_TOKENS * 100, 2) if config.MAX_TOKENS > 0 else 0,
        "model": agent.model,
        "updated_at": None,
    }


@app.post("/api/chat/{session_id}")
async def chat(session_id: str, request: Request):
    body = await request.json()
    message = (body.get("message") or "").strip()
    if not message:
        return {"status": "error", "message": "消息不能为空"}

    async with runs_lock:
        if session_id in active_runs:
            return JSONResponse(status_code=409,
                                content={"status": "error", "message": "该会话正在生成中，请先停止或等待完成"})
        cancel_event = asyncio.Event()
        active_runs[session_id] = {"cancel": cancel_event, "done": asyncio.Event()}

    async def event_stream():
        disconnected = False
        try:
            async for event in agent.run(session_id, message, cancel_event=cancel_event):
                if not disconnected:
                    try:
                        if await request.is_disconnected():
                            disconnected = True
                            cancel_event.set()
                    except Exception:
                        pass
                enriched = _enrich(event)
                if enriched.get("type") == "usage":
                    await sessions.save_usage(session_id, {**enriched, "model": agent.model,
                                                           "updated_at": time.time()})
                if not disconnected:
                    yield f"data: {json.dumps(enriched)}\n\n"
        except Exception as exc:
            if not disconnected:
                yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
        finally:
            async with runs_lock:
                entry = active_runs.pop(session_id, None)
            if entry:
                entry["done"].set()

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/chat/{session_id}/stop")
async def stop_chat(session_id: str):
    async with runs_lock:
        entry = active_runs.get(session_id)
    if entry is None:
        return {"status": "not_running"}
    entry["cancel"].set()
    return {"status": "stopping"}


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(index_html)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="info")

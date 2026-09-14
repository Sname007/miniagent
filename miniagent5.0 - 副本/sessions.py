import asyncio
import json
import os
import re
import uuid

from deepseek_tokenizer import ds_token

from config import HISTORY_DATA_DIR

USAGE_SUFFIX = ".usage.json"
_UNSAFE_ID = re.compile(r"[^A-Za-z0-9_-]")


def _name(session_id):
    return _UNSAFE_ID.sub("", str(session_id))[:64]


def _history_path(session_id):
    return os.path.join(HISTORY_DATA_DIR, f"{_name(session_id)}.json")


def _usage_path(session_id):
    return os.path.join(HISTORY_DATA_DIR, f"{_name(session_id)}{USAGE_SUFFIX}")


def _read_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return default


def _write_json(path, data):
    os.makedirs(HISTORY_DATA_DIR, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read_history(session_id):
    data = _read_json(_history_path(session_id), [])
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _scan_sessions():
    if not os.path.isdir(HISTORY_DATA_DIR):
        return []
    names = [n for n in os.listdir(HISTORY_DATA_DIR)
             if n.endswith(".json") and not n.endswith(USAGE_SUFFIX)]
    names.sort(key=lambda n: os.path.getmtime(os.path.join(HISTORY_DATA_DIR, n)), reverse=True)
    sessions = []
    for name in names:
        session_id = name[: -len(".json")]
        preview = "空会话"
        for msg in reversed(_read_history(session_id)):
            if msg.get("role") == "user" and isinstance(msg.get("content"), str) and msg["content"]:
                text = msg["content"]
                preview = text[:30] + "..." if len(text) > 30 else text
                break
        sessions.append({"id": session_id, "preview": preview})
    return sessions


def count_tokens(messages):
    parts = []
    for msg in messages:
        for key, value in msg.items():
            if key in ("role", "tool_call_id"):
                continue
            parts.append(json.dumps(value, ensure_ascii=False) if key == "tool_calls" else str(value))
    return len(ds_token.encode("\n".join(parts)))


async def list_sessions():
    return await asyncio.to_thread(_scan_sessions)


async def create_session():
    session_id = str(uuid.uuid4())
    await asyncio.to_thread(_write_json, _history_path(session_id), [])
    return session_id


async def load_history(session_id):
    return await asyncio.to_thread(_read_history, session_id)


async def save_history(session_id, messages):
    await asyncio.to_thread(_write_json, _history_path(session_id), messages)


async def clear_session(session_id):
    await asyncio.to_thread(_write_json, _history_path(session_id), [])
    await asyncio.to_thread(_remove, _usage_path(session_id))


async def delete_session(session_id):
    await asyncio.to_thread(_remove, _history_path(session_id))
    await asyncio.to_thread(_remove, _usage_path(session_id))


async def load_usage(session_id):
    data = await asyncio.to_thread(_read_json, _usage_path(session_id), None)
    return data if isinstance(data, dict) and data else None


async def save_usage(session_id, usage):
    await asyncio.to_thread(_write_json, _usage_path(session_id), usage)


def _remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass

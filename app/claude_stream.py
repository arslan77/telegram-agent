"""Translate one Claude Code stream-json line into board events."""

from __future__ import annotations

import json


def adapt_claude(line: str, acc: dict) -> list[dict]:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return []
    if not isinstance(event, dict):
        return []
    session_id = event.get("session_id") if isinstance(event.get("session_id"), str) else ""
    kind = event.get("type")
    if kind == "stream_event":
        return _text_delta(event.get("event"), session_id)
    if kind == "assistant":
        return _assistant(event.get("message"), session_id, acc)
    if kind == "user":
        return _tool_results(event.get("message"), session_id, acc)
    if kind == "result":
        return [event]
    if session_id:
        return [{"type": "system", "session_id": session_id}]
    return []


def _text_delta(inner: object, session_id: str) -> list[dict]:
    if not isinstance(inner, dict) or inner.get("type") != "content_block_delta":
        return []
    delta = inner.get("delta")
    if not isinstance(delta, dict) or delta.get("type") != "text_delta":
        return []
    text = delta.get("text")
    if not isinstance(text, str) or not text:
        return []
    return [_assistant_text(text, session_id, streaming=True)]


def _assistant(message: object, session_id: str, acc: dict) -> list[dict]:
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    events: list[dict] = []
    texts: list[str] = []
    open_tools = acc.setdefault("open_tools", {})
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            tool_id = block.get("id")
            tool = _cursor_tool(str(block.get("name") or ""), block.get("input"))
            if isinstance(tool_id, str) and tool_id:
                open_tools[tool_id] = tool
            events.append(_tool_event("started", tool, session_id))
        elif block.get("type") == "text" and isinstance(block.get("text"), str) and block["text"]:
            texts.append(block["text"])
    if texts:
        events.append(_assistant_text("".join(texts), session_id, streaming=False))
    return events


def _tool_results(message: object, session_id: str, acc: dict) -> list[dict]:
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    open_tools = acc.get("open_tools")
    if not isinstance(open_tools, dict):
        return []
    events: list[dict] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        tool = open_tools.get(block.get("tool_use_id"))
        if not isinstance(tool, dict) or not tool:
            continue
        name, body = next(iter(tool.items()))
        finished = dict(body) if isinstance(body, dict) else {}
        finished["result"] = {"success": {"stdout": _result_text(block.get("content"))}}
        events.append(_tool_event("completed", {name: finished}, session_id))
    return events


def _cursor_tool(name: str, raw_input: object) -> dict:
    args = dict(raw_input) if isinstance(raw_input, dict) else {}
    if not isinstance(args.get("path"), str):
        for key in ("file_path", "filePath"):
            value = args.get(key)
            if isinstance(value, str) and value:
                args["path"] = value
                break
    label = {
        "Read": "read",
        "Write": "write",
        "Edit": "edit",
        "Bash": "bash",
        "Grep": "grep",
        "Glob": "glob",
    }.get(name, name[:1].lower() + name[1:] if name else "tool")
    return {f"{label}ToolCall": {"args": args}}


def _assistant_text(text: str, session_id: str, *, streaming: bool) -> dict:
    event: dict = {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    if streaming:
        event["timestamp_ms"] = 1
    if session_id:
        event["session_id"] = session_id
    return event


def _tool_event(subtype: str, tool_call: dict, session_id: str) -> dict:
    event: dict = {"type": "tool_call", "subtype": subtype, "tool_call": tool_call}
    if session_id:
        event["session_id"] = session_id
    return event


def _result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)

"""Cursor stream-json events, including thinking deltas."""

from __future__ import annotations

import json

from cursor_telegram.activity import format_activity
from cursor_telegram.board import LiveBoard


def assistant_text(event: dict, *, raw: bool = False) -> str:
    message = event.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    text = "".join(parts)
    # Streamed words arrive as their own events, usually with a leading space.
    # Trimming each one glues the sentence together.
    return text if raw else text.strip()

def consume_event(line: str, acc: dict, board: LiveBoard | None = None) -> None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return
    if not isinstance(event, dict):
        return
    session_id = event.get("session_id")
    if isinstance(session_id, str) and session_id:
        acc["session_id"] = session_id
    kind = event.get("type")
    if kind == "assistant":
        has_ts = "timestamp_ms" in event
        has_call = "model_call_id" in event
        if has_ts and has_call:
            return
        streaming = has_ts and not has_call
        text = assistant_text(event, raw=streaming)
        if not text:
            return
        if board is not None:
            if isinstance(session_id, str) and session_id:
                board.set_session(session_id)
            if has_ts:
                board.stream(text)
            else:
                board.complete(text)
            acc["last"] = board.transcript.render()
        elif not has_ts:
            acc["last"] = text
    elif kind == "thinking":
        text = event.get("text") if isinstance(event.get("text"), str) else ""
        if board is not None and (text or event.get("subtype") == "completed"):
            if isinstance(session_id, str) and session_id:
                board.set_session(session_id)
            board.add_thinking(text, done=event.get("subtype") == "completed")
    elif kind == "tool_call" and event.get("subtype") in {"started", "completed"}:
        done = event.get("subtype") == "completed"
        acc["tool"] = format_activity(event.get("tool_call"), done=done)
        if done:
            acc["pending_tool"] = event.get("tool_call")
        if board is not None:
            if isinstance(session_id, str) and session_id:
                board.set_session(session_id)
            board.show_tool(event.get("tool_call"), done=done)
    elif kind == "result":
        result = event.get("result")
        if isinstance(result, str):
            acc["result"] = result
        acc["is_error"] = bool(event.get("is_error"))
        if isinstance(event.get("duration_ms"), int):
            acc["duration_ms"] = event["duration_ms"]

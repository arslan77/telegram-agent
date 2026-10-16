#!/usr/bin/env python3
"""Telegram bridge to the local Cursor CLI on this machine.

Only a private chat whose sender shares a phone number on TELEGRAM_ALLOWED_PHONES
can run the agent. Everyone else gets no work done. Runs use `agent -p` on this
host. A leading & is rejected so the chat cannot be handed to a cloud agent.
"""

from __future__ import annotations

import base64
import html
import json
import logging
import os
import queue
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

log = logging.getLogger("cursor-telegram")

TELEGRAM_LIMIT = 3900
RICH_LIMIT = 30000
RUN_TIMEOUT_SEC = 45 * 60
STATE_VERSION = 1
MAX_DOWNLOAD = 20 * 1024 * 1024
MAX_UPLOAD = 45 * 1024 * 1024
FILE_PAGE = 6
MAX_QUEUE = 20
REACTION_QUEUED = "👀"
REACTION_WORKING = "⚡"
REACTION_DONE = "👍"
REACTION_CANCELLED = "😐"
REACTION_ERROR = "👎"
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
MEDIA_EXT = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif",
    ".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg", ".opus",
    ".mp4", ".mov", ".webm",
}


class ConfigError(Exception):
    """Bad token, allowlist, or file permissions. The process exits 78."""


def normalize_phone(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def parse_allowlist(raw: str) -> set[str]:
    found: set[str] = set()
    for part in raw.replace(";", ",").split(","):
        digits = normalize_phone(part)
        if not digits:
            continue
        if len(digits) < 8 or len(digits) > 15:
            raise ConfigError(f"allowlist phone has {len(digits)} digits; use the full international number")
        found.add(digits)
    if not found:
        raise ConfigError("TELEGRAM_ALLOWED_PHONES is empty")
    return found


def mask_phone(digits: str) -> str:
    if len(digits) <= 4:
        return "••••"
    return digits[:3] + "•" * (len(digits) - 5) + digits[-2:]


def parse_command(text: str) -> tuple[str | None, str]:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None, stripped
    head, _, rest = stripped.partition(" ")
    return head.split("@", 1)[0].lower(), rest.strip()


def is_cloud_handoff(text: str) -> bool:
    return text.lstrip().startswith("&")


def split_text(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    body = text.strip() or "Finished, with no text."
    parts: list[str] = []
    while body:
        if len(body) <= limit:
            parts.append(body)
            break
        cut = body.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(body[:cut].rstrip())
        body = body[cut:].lstrip("\n")
    return parts


def redact(text: str, secret: str) -> str:
    if secret and secret in text:
        return text.replace(secret, "***")
    return text


def resolve_workspace(raw: str, home: Path) -> Path:
    home_real = home.resolve()
    text = raw.strip()
    if not text or text == "~":
        path = home_real
    elif text.startswith("~/"):
        path = home_real / text[2:]
    else:
        path = Path(text)
        if not path.is_absolute():
            path = home_real / path
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError("That folder does not exist.") from exc
    if not resolved.is_dir():
        raise ValueError("That path is not a folder.")
    if os.path.commonpath([str(resolved), str(home_real)]) != str(home_real):
        raise ValueError("That folder is outside your home directory.")
    return resolved


def contact_phone(message: dict) -> str | None:
    """Return the sender's own phone digits, or None if this is not their contact."""
    contact = message.get("contact")
    sender = message.get("from") or {}
    if not isinstance(contact, dict) or sender.get("is_bot"):
        return None
    if contact.get("user_id") != sender.get("id"):
        return None
    phone = normalize_phone(str(contact.get("phone_number") or ""))
    if len(phone) < 8 or len(phone) > 15:
        return None
    return phone


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


def cursor_model() -> str:
    return os.environ.get("CURSOR_MODEL", "").strip() or "auto"


def _tool_body(tool_call: object) -> tuple[str, dict]:
    if not isinstance(tool_call, dict) or not tool_call:
        return "", {}
    name, body = next(iter(tool_call.items()))
    return str(name).removesuffix("ToolCall"), body if isinstance(body, dict) else {}


def _tool_parts(tool_call: object) -> tuple[str, dict]:
    label, body = _tool_body(tool_call)
    if not label:
        return "", {}
    raw = body.get("args", body.get("arguments"))
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = None
    return label, raw if isinstance(raw, dict) else {}


def _success_result(tool_call: object) -> dict:
    _, body = _tool_body(tool_call)
    result = body.get("result")
    if not isinstance(result, dict):
        return {}
    success = result.get("success")
    return success if isinstance(success, dict) else result


def _arg_text(args: dict, *keys: str) -> str:
    for key in keys:
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def tool_label(tool_call: object) -> str:
    label, args = _tool_parts(tool_call)
    if not label:
        return "Working"
    path = args.get("path") or args.get("target_file")
    command = args.get("command")
    if isinstance(path, str) and path:
        return f"{label} {path}"[:240]
    if isinstance(command, list):
        command = " ".join(str(part) for part in command)
    if isinstance(command, str) and command:
        first = " ".join(command.split())
        return f"{label} {first}"[:240]
    return label[:240]


_TOOL_VERBS = {
    "read": "Reading",
    "write": "Writing",
    "edit": "Editing",
    "strreplace": "Editing",
    "delete": "Deleting",
    "shell": "Running",
    "bash": "Running",
    "grep": "Searching",
    "glob": "Finding",
    "ls": "Listing",
}


def tool_phrase(tool_call: object) -> str:
    raw = tool_label(tool_call)
    if raw == "Working":
        return "Working"
    kind, _, rest = raw.partition(" ")
    verb = _TOOL_VERBS.get(kind.lower(), kind[:1].upper() + kind[1:] if kind else "Working")
    if not rest:
        return verb
    if verb == "Running":
        return f"Running {rest}"[:220]
    short = rest.rstrip("/").rsplit("/", 1)[-1] or rest
    return f"{verb} {short}"[:220]


def _clip_rows(prefix: str, text: str, limit: int) -> list[str]:
    if not text:
        return []
    rows = text.splitlines() or [text]
    out: list[str] = []
    for line in rows[:limit]:
        clean = line.replace("`", "'")
        if len(clean) > 80:
            clean = clean[:79] + "…"
        out.append(prefix + clean)
    if len(rows) > limit:
        out.append(f"… +{len(rows) - limit} lines")
    return out


def tool_snippet(tool_call: object) -> str:
    """Short red/green preview for an edit. File bodies stay out of the chat."""
    label, args = _tool_parts(tool_call)
    if label.lower() not in {"edit", "strreplace"}:
        return ""
    old = _arg_text(args, "old_string", "old_str", "oldText", "old_text")
    new = _arg_text(args, "new_string", "new_str", "newText", "new_text")
    return "\n".join(_clip_rows("🔴 -", old, 4) + _clip_rows("🟢 +", new, 4))


def tool_entry(tool_call: object) -> str:
    phrase = tool_phrase(tool_call)
    snippet = tool_snippet(tool_call)
    if not snippet:
        return phrase
    return f"{phrase}\n\n```\n{snippet}\n```"


def _clip_cell(text: str, limit: int = 80) -> str:
    clean = text.replace("`", "'")
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1] + "…"


def _command_text(args: dict) -> str:
    command = args.get("command")
    if isinstance(command, list):
        command = " ".join(str(part) for part in command)
    if not isinstance(command, str):
        return ""
    return " ".join(command.split())


def format_activity(tool_call: object, *, done: bool) -> str:
    """One line for the tool that is running, or the tool that just finished."""
    label, args = _tool_parts(tool_call)
    kind = label.lower()
    mark = "✅" if done else "🟡"
    path = args.get("path") or args.get("target_file") or ""
    short = str(path).rstrip("/").rsplit("/", 1)[-1] if isinstance(path, str) and path else ""
    if kind in {"read"}:
        verb = "read" if done else "reading"
        return f"{mark} {verb} {short}" if short else f"{mark} {verb} file…"
    if kind in {"write"}:
        verb = "wrote" if done else "writing"
        return f"{mark} {verb} {short}" if short else f"{mark} {verb} file…"
    if kind in {"edit", "strreplace"}:
        verb = "edited" if done else "editing"
        return f"{mark} {verb} {short}" if short else f"{mark} {verb} file…"
    if kind in {"delete"}:
        verb = "deleted" if done else "deleting"
        return f"{mark} {verb} {short}" if short else f"{mark} {verb} file…"
    if kind in {"shell", "bash"}:
        command = _command_text(args)
        if len(command) > 60:
            command = command[:59] + "…"
        return f"{mark} shell: {command}" if command else f"{mark} shell…"
    if kind in {"grep", "rg"}:
        pattern = _arg_text(args, "pattern", "query", "regex")
        if len(pattern) > 50:
            pattern = pattern[:49] + "…"
        return f"{mark} grep: {pattern}" if pattern else f"{mark} grep…"
    if kind in {"glob", "ls"}:
        return f"{mark} {'listed' if done else 'listing'} {short}" if short else f"{mark} listing files…"
    if not label:
        return f"{mark} working…"
    return f"{mark} {label}"


def _limit_rows(lines: list[str], limit: int = 12) -> str:
    if len(lines) <= limit:
        return "\n".join(lines)
    extra = len(lines) - limit
    return "\n".join(lines[:limit] + [f"… +{extra} lines"])


def _is_unified_diff(text: str) -> bool:
    plus = sum(1 for line in text.splitlines() if line.startswith("+") and not line.startswith("+++"))
    minus = sum(1 for line in text.splitlines() if line.startswith("-") and not line.startswith("---"))
    return plus + minus >= 2


def colorize_diff(text: str) -> str:
    rows: list[str] = []
    for line in text.splitlines():
        shown = _clip_cell(line)
        if line.startswith(("+++", "---", "@@")):
            rows.append(f"🔷 {shown}")
        elif line.startswith("+"):
            rows.append(f"🟢 {shown}")
        elif line.startswith("-"):
            rows.append(f"🔴 {shown}")
        else:
            rows.append(f"   {shown}")
    return _limit_rows(rows)


def shell_output_snippet(tool_call: object) -> str:
    data = _success_result(tool_call)
    stdout = data.get("stdout") if isinstance(data.get("stdout"), str) else ""
    stderr = data.get("stderr") if isinstance(data.get("stderr"), str) else ""
    code = data.get("exitCode", data.get("exit_code"))
    rows: list[str] = []
    if code not in (None, 0, "0"):
        rows.append(f"🔴 exit {code}")
    rows.extend(f"🔴 {_clip_cell(line)}" for line in stderr.splitlines() if line.strip())
    rows.extend(f"🟢 {_clip_cell(line)}" for line in stdout.splitlines())
    return _limit_rows(rows)


def _result_text(tool_call: object) -> str:
    data = _success_result(tool_call)
    for key in ("stdout", "output", "content", "text", "interleavedOutput"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value
    matches = data.get("matches")
    if isinstance(matches, list):
        lines = [item for item in matches if isinstance(item, str)]
        if lines:
            return "\n".join(lines)
    return ""


def format_snippet(tool_call: object) -> str:
    """Short preview of the latest tool. Edits, shell output, grep, and diffs."""
    label, args = _tool_parts(tool_call)
    kind = label.lower()
    if kind in {"edit", "strreplace"}:
        return tool_snippet(tool_call)
    if kind in {"shell", "bash"}:
        stdout = _success_result(tool_call).get("stdout")
        if isinstance(stdout, str) and _is_unified_diff(stdout):
            return colorize_diff(stdout)
        output = shell_output_snippet(tool_call)
        if output:
            return output
        return _command_text(args)[:500]
    if kind in {"grep", "rg"}:
        text = _result_text(tool_call)
        if not text:
            return ""
        return _limit_rows([f"🟢 {_clip_cell(line)}" for line in text.splitlines()])
    text = _result_text(tool_call)
    if text and _is_unified_diff(text):
        return colorize_diff(text)
    return ""


_LIVE_BODY = 2200


def finalize_live(body: str) -> str:
    """Keep the streaming preview inside one Telegram message and close an open fence."""
    text = body.strip()
    if not text:
        return ""
    if text.count("```") % 2 == 1:
        text = text.rstrip() + "\n```"
    if len(text) > _LIVE_BODY:
        text = "…\n" + text[-(_LIVE_BODY - 2) :]
        if text.count("```") % 2 == 1:
            text = "```\n" + text
    return text


def build_run_html(
    *,
    model: str,
    project: str,
    session: str,
    elapsed: int | None,
    body: str,
    activity: str,
    snippet: str,
    phase: str,
    tools: list[str] | None = None,
) -> str:
    """Live run, or the finished answer that replaces it."""
    running = phase == "running"
    parts = [
        message_header(model, project=project, session=session, elapsed=elapsed, running=running)
    ]
    if running:
        preview = finalize_live(body)
        if preview:
            parts.append(markdown_to_telegram_html(preview))
        if activity:
            parts.append(f"<i>{escape_html(activity)}</i>")
        if snippet:
            parts.append(f"<pre><code>{escape_html(snippet)}</code></pre>")
        if len(parts) == 1:
            parts.append("<i>working…</i>")
    else:
        icon = {"cancelled": "⛔", "error": "🔴"}.get(phase, "✅")
        parts.append(icon)
        if body.strip():
            parts.append(markdown_to_telegram_html(body.strip()))
        else:
            parts.append("<i>(no output)</i>")
    steps = [entry.splitlines()[0].strip() for entry in (tools or []) if entry.strip()][-30:]
    if steps:
        parts.append(escape_html("\n".join(f"• {step}" for step in steps)))
    return "\n".join(parts)


def escape_rich_text(text: str) -> str:
    return "".join(f"\\{ch}" if ch in "*_`[]" else ch for ch in text)


def close_rich_fence(text: str) -> str:
    if text.count("```") % 2 == 1:
        return text.rstrip() + "\n```"
    return text


def rich_tool_details(tools: list[str] | None) -> str:
    shown = [entry for entry in (tools or []) if entry.strip()][-30:]
    if not shown:
        return ""
    lines: list[str] = []
    for entry in shown:
        first, _, rest = entry.partition("\n")
        lines.append(f"- {first.strip()}")
        extra = rest.strip()
        if extra:
            lines.append(close_rich_fence(extra))
    return "<details><summary>What Cursor did</summary>\n\n" + "\n\n".join(lines) + "\n\n</details>"


def rich_cell(text: str) -> str:
    return escape_rich_text(text).replace("|", "\\|").replace("\n", " ")


def rich_status_table(model: str, project: str, session: str, elapsed: int | None, *, running: bool) -> str:
    name = (model or "auto").strip() or "auto"
    clock = ""
    if elapsed is not None:
        icon = "⏳" if running else "⌛"
        clock = f"{icon} {max(0, elapsed)}s"
    row = " | ".join(
        rich_cell(part)
        for part in (session or "new", project or "workspace", name, clock or "—")
    )
    return "\n".join(
        [
            "| Session | Folder | Model | Time |",
            "|:---|:---|:---|---:|",
            f"| {row} |",
        ]
    )


def rich_action_buttons() -> str:
    return "\n".join(
        [
            '<tg-button-row align="left">',
            '<tg-button type="callback_data" style="primary" data="diff">Diff</tg-button>',
            '<tg-button type="callback_data" data="status">Status</tg-button>',
            '<tg-button type="callback_data" style="danger" data="end">End</tg-button>',
            "</tg-button-row>",
        ]
    )


def build_run_rich_markdown(
    *,
    model: str,
    project: str,
    session: str,
    elapsed: int | None,
    body: str,
    activity: str,
    snippet: str,
    phase: str,
    tools: list[str] | None = None,
    draft: bool = False,
) -> str:
    """One rich-markdown document for the live run or the finished answer."""
    running = phase == "running"
    parts = [rich_status_table(model, project, session, elapsed, running=running)]
    preview = body.strip()
    if running:
        if preview:
            parts.append(close_rich_fence(preview))
        if draft and not preview:
            think = activity.strip() or "working…"
            parts.append(f"<tg-thinking>{escape_html(think)}</tg-thinking>")
        elif activity:
            parts.append(f"> *{escape_rich_text(activity)}*")
        if snippet:
            safe_snippet = snippet.replace("```", "'''")
            parts.append(f"```\n{safe_snippet}\n```")
        if len(parts) == 1:
            parts.append("<tg-thinking>working…</tg-thinking>" if draft else "*working…*")
    else:
        parts.append({"cancelled": "⛔", "error": "🔴"}.get(phase, "✅"))
        if preview:
            parts.append(close_rich_fence(preview))
        else:
            parts.append("*(no output)*")
    details = rich_tool_details(tools)
    if details:
        parts.append(details)
    if not running:
        parts.append(rich_action_buttons())
    text = "\n\n".join(parts)
    if len(text) <= RICH_LIMIT:
        return text
    clipped = text[: RICH_LIMIT - 2].rstrip() + "\n…"
    return close_rich_fence(clipped)


def rich_unavailable(detail: str) -> bool:
    lowered = detail.lower()
    return "sendrichmessage failed: 404" in lowered or "unknown method" in lowered


def build_run_plain(
    *,
    model: str,
    project: str,
    session: str,
    elapsed: int | None,
    body: str,
    activity: str,
    snippet: str,
    phase: str,
    tools: list[str] | None = None,
) -> str:
    title = " ".join(part for part in (f"[{session}]" if session else "", project, model) if part)
    if elapsed is not None:
        title += f" · {max(0, elapsed)}s"
    lines = [title]
    if phase != "running":
        lines.append({"cancelled": "⛔", "error": "🔴"}.get(phase, "✅"))
    if body.strip():
        lines.append(body.strip())
    if phase == "running" and activity:
        lines.append(activity)
    if phase == "running" and snippet:
        lines.append(snippet)
    steps = [entry.splitlines()[0].strip() for entry in (tools or []) if entry.strip()][-30:]
    if steps:
        lines.append("\n".join(f"• {step}" for step in steps))
    return "\n\n".join(lines)[:4096]


RUN_KEYBOARD = {
    "inline_keyboard": [[
        {"text": "Diff", "callback_data": "diff", "style": "primary"},
        {"text": "Status", "callback_data": "status"},
        {"text": "End", "callback_data": "end", "style": "danger"},
    ]]
}
ALLOWED_UPDATES = ["message", "callback_query", "stopped_message_generation"]


_FILE_TOOL = re.compile(r"^(Reading|Writing|Editing|Deleting|Searching|Finding|Listing) (\S+)$")
_FENCE = re.compile(r"```([A-Za-z0-9_+-]*)\n?(.*?)```", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_HEADING = re.compile(r"^#{1,6}\s+(.+)$", re.M)
_CHECKBOX = re.compile(r"^(\s*)[-*]\s+\[([ xX])\]\s+(.+)$", re.M)
_BULLET = re.compile(r"^(\s*)[-*]\s+(.+)$", re.M)
_URL = re.compile(r"https?://[^\s<&*\"']+")


def escape_html(text: str) -> str:
    return html.escape(text, quote=False)


def linkify(escaped: str) -> str:
    def replace(match: re.Match[str]) -> str:
        url = match.group(0)
        tail = ""
        while url and url[-1] in ".,);:!?*":
            tail = url[-1] + tail
            url = url[:-1]
        if not url:
            return match.group(0)
        href = html.escape(url, quote=True)
        return f'<a href="{href}">{url}</a>{tail}'

    return _URL.sub(replace, escaped)


def message_header(
    model: str,
    *,
    project: str = "",
    session: str = "",
    elapsed: int | None = None,
    running: bool = True,
) -> str:
    name = (model or "auto").strip() or "auto"
    bits: list[str] = []
    if session:
        bits.append(f"[{escape_html(session)}]")
    if project:
        bits.append(escape_html(project))
    if bits:
        head = f"<b>{' '.join(bits)}</b> · <code>{escape_html(name)}</code>"
    else:
        head = f"<b>{escape_html(name)}</b>"
    if elapsed is not None:
        if running:
            icon = "⏳" if (max(0, elapsed) // 5) % 2 == 0 else "⌛"
        else:
            icon = "⌛"
        head += f" · {icon} {max(0, elapsed)}s"
    return f"<blockquote>{head}</blockquote>"


def markdown_to_telegram_html(text: str) -> str:
    """Render agent markdown as Telegram HTML. Unclosed markers stay as typed."""
    code_blocks: list[tuple[str, str]] = []
    inline_codes: list[str] = []
    links: list[tuple[str, str]] = []
    headings: list[str] = []

    def save_fence(match: re.Match[str]) -> str:
        body = match.group(2)
        if body.endswith("\n"):
            body = body[:-1]
        code_blocks.append((match.group(1).strip(), body))
        return f"\x00CODE{len(code_blocks) - 1}\x00"

    def save_inline(match: re.Match[str]) -> str:
        inline_codes.append(match.group(1))
        return f"\x00INL{len(inline_codes) - 1}\x00"

    def save_link(match: re.Match[str]) -> str:
        links.append((match.group(1), match.group(2)))
        return f"\x00LNK{len(links) - 1}\x00"

    def save_heading(match: re.Match[str]) -> str:
        headings.append(match.group(1).strip())
        return f"\x00HED{len(headings) - 1}\x00"

    text = _FENCE.sub(save_fence, text)
    text = _INLINE_CODE.sub(save_inline, text)
    text = _MD_LINK.sub(save_link, text)
    text = _HEADING.sub(save_heading, text)
    text = _CHECKBOX.sub(
        lambda match: f"{match.group(1)}• {'☑' if match.group(2).lower() == 'x' else '☐'} {match.group(3)}",
        text,
    )
    text = _BULLET.sub(lambda match: f"{match.group(1)}• {match.group(2)}", text)
    text = linkify(escape_html(text))
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<i>\1</i>", text)
    for index, (lang, body) in enumerate(code_blocks):
        klass = f' class="language-{html.escape(lang, quote=True)}"' if lang else ""
        block = f"<pre><code{klass}>{escape_html(body)}</code></pre>"
        text = text.replace(f"\x00CODE{index}\x00", block)
    for index, code in enumerate(inline_codes):
        text = text.replace(f"\x00INL{index}\x00", f"<code>{escape_html(code)}</code>")
    for index, (label, url) in enumerate(links):
        href = html.escape(url, quote=True)
        text = text.replace(f"\x00LNK{index}\x00", f'<a href="{href}">{escape_html(label)}</a>')
    for index, title in enumerate(headings):
        text = text.replace(f"\x00HED{index}\x00", f"<b>{escape_html(title)}</b>")
    return text


def format_prose(text: str) -> str:
    return markdown_to_telegram_html(text)


def format_paragraph(para: str) -> str:
    if para.startswith("Running "):
        command = para[len("Running ") :]
        if command and not command.endswith("."):
            return f'<pre><code class="language-bash">{escape_html(command)}</code></pre>'
    file_tool = _FILE_TOOL.match(para)
    if file_tool:
        return f"{escape_html(file_tool.group(1))} <code>{escape_html(file_tool.group(2))}</code>"
    return format_prose(para)


def format_bubble(text: str) -> str:
    return "\n\n".join(format_paragraph(para) for para in text.split("\n\n") if para)


def html_rejected(detail: str) -> bool:
    return "can't parse" in detail or "parse entities" in detail


class Transcript:
    """Append-only run log. Tool lines are committed; assistant words stream into the tail."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.tail = ""
        self.streamed = False

    def add_tool(self, phrase: str) -> bool:
        self.commit()
        if not phrase or (self.parts and self.parts[-1] == phrase):
            return False
        self.parts.append(phrase)
        return True

    def add_delta(self, text: str) -> bool:
        if not text:
            return False
        if text.startswith(self.tail):
            if text == self.tail:
                return False
            self.tail = text
        elif self.tail.endswith(text):
            return False
        else:
            self.tail += text
        self.streamed = True
        return True

    def add_complete(self, text: str) -> bool:
        chunk = text.strip()
        if not chunk:
            return False
        if self.streamed:
            self.streamed = False
            return False
        self.commit()
        if self.parts and self.parts[-1] == chunk:
            return False
        self.parts.append(chunk)
        return True

    def commit(self) -> None:
        chunk = self.tail.strip()
        self.tail = ""
        self.streamed = False
        if chunk and (not self.parts or self.parts[-1] != chunk):
            self.parts.append(chunk)

    def render(self) -> str:
        chunks = [part for part in self.parts if part]
        if self.tail:
            chunks.append(self.tail.rstrip())
        return "\n\n".join(chunks).strip()


def chunk_html(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split Telegram HTML on a closed tag or a blank line."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        window = remaining[:limit]
        split_at = -1
        for marker in ("</pre>", "</blockquote>", "\n\n"):
            pos = window.rfind(marker)
            if pos > split_at:
                split_at = pos + len(marker) if marker.startswith("</") else pos
        if split_at <= 0:
            split_at = window.rfind("\n")
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    return [chunk for chunk in chunks if chunk]


def split_live(text: str, limit: int) -> tuple[str, str]:
    """Split text so the first piece fits in one Telegram message."""
    if len(text) <= limit:
        return text, ""
    cut = text.rfind("\n", 0, limit)
    if cut < limit // 3:
        cut = limit
    if text[:cut].count("```") % 2 == 1:
        fence = text.rfind("```", 0, cut)
        if fence > limit // 3:
            cut = fence
    return text[:cut].rstrip(), text[cut:].lstrip("\n")


def message_ids_for_clear(row: dict, command_id: int | None) -> list[int]:
    found: list[int] = []
    seen: set[int] = set()
    for value in row.get("message_ids") or []:
        if isinstance(value, int) and value not in seen:
            seen.add(value)
            found.append(value)
    if isinstance(command_id, int) and command_id not in seen:
        found.append(command_id)
    return found


def forget_messages(row: dict) -> dict:
    kept = dict(row)
    kept["message_ids"] = []
    return kept


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


def safe_filename(name: str) -> str:
    base = Path(name).name
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in base).strip("._")
    return (cleaned or "file")[:80]


def media_kind(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".webp"}:
        return "photo"
    if ext == ".gif":
        return "animation"
    if ext in {".mp4", ".mov", ".webm"}:
        return "video"
    if ext in {".ogg", ".opus"}:
        return "voice"
    if ext in {".mp3", ".m4a", ".wav", ".flac", ".aac"}:
        return "audio"
    return "document"


def is_media(path: Path) -> bool:
    return path.suffix.lower() in MEDIA_EXT


def blocked_file(path: Path) -> bool:
    name = path.name
    if name.startswith(".env") or name in {"id_rsa", "credentials.json"}:
        return True
    return any(part in SKIP_DIRS for part in path.parts)


def inbound_prompt(path: Path, caption: str) -> str:
    kind = media_kind(path)
    if kind == "photo":
        note = f"The user sent an image. Read this file and look at it before answering:\n{path}"
    elif kind == "animation":
        note = f"The user sent an animation. Read this file:\n{path}"
    elif kind in {"audio", "voice"}:
        note = f"The user sent an audio file:\n{path}"
    elif kind == "video":
        note = f"The user sent a video file:\n{path}"
    else:
        note = f"The user sent a file:\n{path}"
    text = caption.strip()
    if text:
        return f"{note}\n\n{text}"
    if kind == "photo":
        return note + "\n\nDescribe what you see."
    return note + "\n\nLook at it and respond."


def attachment_from_message(message: dict) -> dict | None:
    caption = message.get("caption") if isinstance(message.get("caption"), str) else ""
    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        sizes = [item for item in photos if isinstance(item, dict) and isinstance(item.get("file_id"), str)]
        if sizes:
            best = max(sizes, key=lambda item: item.get("file_size") or 0)
            return {
                "file_id": best["file_id"],
                "name": "photo.jpg",
                "size": best.get("file_size") or 0,
                "caption": caption,
            }
    for key, default in (
        ("document", "file"),
        ("audio", "audio.mp3"),
        ("voice", "voice.ogg"),
        ("video", "video.mp4"),
        ("video_note", "video.mp4"),
        ("animation", "animation.mp4"),
    ):
        body = message.get(key)
        if isinstance(body, dict) and isinstance(body.get("file_id"), str):
            name = body.get("file_name") if isinstance(body.get("file_name"), str) and body.get("file_name") else default
            return {
                "file_id": body["file_id"],
                "name": name,
                "size": body.get("file_size") or 0,
                "caption": caption,
            }
    return None


def inside_workspace(workspace: Path, raw: str) -> Path | None:
    text = raw.strip()
    if not text or "\x00" in text:
        return None
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    try:
        resolved = candidate.resolve()
        root = workspace.resolve()
    except OSError:
        return None
    if resolved != root and root not in resolved.parents:
        return None
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        return None
    if blocked_file(relative):
        return None
    return resolved


def output_paths(tool_call: object, workspace: Path) -> list[Path]:
    """Media files a finished tool wrote. Reads and searches are ignored."""
    label, args = _tool_parts(tool_call)
    if label.lower() in {"read", "grep", "rg", "glob", "ls"}:
        return []
    found: list[Path] = []
    seen: set[Path] = set()

    def add(raw: object) -> None:
        if not isinstance(raw, str):
            return
        path = inside_workspace(workspace, raw)
        if path is None or not path.is_file() or not is_media(path) or path in seen:
            return
        seen.add(path)
        found.append(path)

    for key in ("path", "file_path", "filePath", "filename", "target_file"):
        add(args.get(key))
    success = _success_result(tool_call)
    for key in ("file_path", "filePath", "path", "image_path"):
        add(success.get(key))
    image = success.get("image_data") or success.get("imageData")
    if isinstance(image, str) and image.strip():
        try:
            raw_bytes = base64.b64decode(image, validate=False)
        except (ValueError, TypeError):
            raw_bytes = b""
        if raw_bytes and len(raw_bytes) <= MAX_UPLOAD:
            folder = workspace / ".cursor-telegram" / "outbound"
            folder.mkdir(parents=True, exist_ok=True)
            dest = folder / f"{secrets.token_hex(4)}.png"
            dest.write_bytes(raw_bytes)
            found.append(dest.resolve())
    return found


def media_index(root: Path) -> dict[str, float]:
    index: dict[str, float] = {}
    if not root.is_dir():
        return index
    base = root.resolve()
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            if not is_media(path) or blocked_file(path):
                continue
            try:
                resolved = path.resolve()
                resolved.relative_to(base)
                index[str(resolved)] = resolved.stat().st_mtime
            except (OSError, ValueError):
                continue
    return index


def changed_media(before: dict[str, float], root: Path) -> list[Path]:
    current = media_index(root)
    found: list[Path] = []
    for key, mtime in current.items():
        previous = before.get(key)
        if previous is None or mtime > previous + 0.001:
            found.append(Path(key))
    return found


def list_sendable(root: Path, query: str = "") -> list[Path]:
    needle = query.strip().lower()
    found: list[tuple[float, Path]] = []
    if not root.is_dir():
        return []
    base = root.resolve()
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            if blocked_file(path):
                continue
            try:
                resolved = path.resolve()
                relative = resolved.relative_to(base)
                size = resolved.stat().st_size
                mtime = resolved.stat().st_mtime
            except (OSError, ValueError):
                continue
            if size > MAX_UPLOAD or size <= 0:
                continue
            if needle and needle not in str(relative).lower():
                continue
            found.append((mtime, resolved))
            if len(found) >= 2000:
                break
        if len(found) >= 2000:
            break
    found.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in found]


def file_page(paths: list[Path], page: int, root: Path) -> tuple[list[Path], str]:
    start = max(0, page) * FILE_PAGE
    chunk = paths[start : start + FILE_PAGE]
    if not paths:
        return [], "No matching files."
    if not chunk:
        return [], "No more files."
    lines = [f"Files {start + 1}–{start + len(chunk)} of {len(paths)}"]
    for path in chunk:
        try:
            rel = path.resolve().relative_to(root.resolve())
        except ValueError:
            rel = Path(path.name)
        lines.append(str(rel))
    return chunk, "\n".join(lines)


class Telegram:
    def __init__(self, token: str) -> None:
        self.token = token
        self._lock = threading.Lock()

    def call(self, method: str, payload: dict, timeout: float) -> dict:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        data = json.dumps(payload).encode()
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._lock:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    body = response.read()
        except urllib.error.HTTPError as exc:
            detail = redact(exc.read().decode("utf-8", "replace"), self.token)
            if exc.code == 401:
                raise ConfigError("Telegram rejected the bot token") from None
            if exc.code == 429:
                retry = 5
                try:
                    retry = int(json.loads(detail).get("parameters", {}).get("retry_after", 5))
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
                time.sleep(min(retry, 30))
                return self.call(method, payload, timeout)
            raise RuntimeError(redact(f"telegram {method} failed: {exc.code} {detail[:300]}", self.token)) from None
        except urllib.error.URLError as exc:
            raise RuntimeError(redact(f"telegram {method} unreachable: {exc.reason}", self.token)) from None
        parsed = json.loads(body)
        if not parsed.get("ok"):
            raise RuntimeError(redact(f"telegram {method} not ok", self.token))
        return parsed

    def send_one(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict | None = None,
        html: bool = False,
        reply_to: int | None = None,
    ) -> int | None:
        payload: dict = {"chat_id": chat_id, "text": text[:4096]}
        if html:
            payload["parse_mode"] = "HTML"
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if isinstance(reply_to, int):
            payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        result = self.call("sendMessage", payload, timeout=30)
        message_id = result.get("result", {}).get("message_id")
        return message_id if isinstance(message_id, int) else None

    def react(self, chat_id: int, message_id: int | None, emoji: str) -> None:
        if not isinstance(message_id, int) or not emoji:
            return
        try:
            self.call(
                "setMessageReaction",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reaction": [{"type": "emoji", "emoji": emoji}],
                },
                timeout=15,
            )
        except RuntimeError as exc:
            log.info("reaction %s failed: %s", emoji, exc)

    def send(self, chat_id: int, text: str, reply_markup: dict | None = None) -> int | None:
        first_id = None
        for index, part in enumerate(split_text(text)):
            markup = reply_markup if index == 0 else None
            message_id = self.send_one(chat_id, part, markup)
            if first_id is None:
                first_id = message_id
        return first_id

    def download(self, file_id: str, dest: Path, limit: int = MAX_DOWNLOAD) -> None:
        info = self.call("getFile", {"file_id": file_id}, timeout=30)
        result = info.get("result") if isinstance(info.get("result"), dict) else {}
        remote = result.get("file_path")
        size = result.get("file_size")
        if not isinstance(remote, str) or not remote or remote.startswith("/"):
            raise RuntimeError("Telegram did not return the file")
        if isinstance(size, int) and size > limit:
            raise RuntimeError("File is larger than 20 MB")
        url = f"https://api.telegram.org/file/bot{self.token}/{remote}"
        request = urllib.request.Request(url, method="GET")
        dest.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                with dest.open("wb") as handle:
                    while True:
                        chunk = response.read(1024 * 64)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > limit:
                            raise RuntimeError("File is larger than 20 MB")
                        handle.write(chunk)
        except Exception:
            dest.unlink(missing_ok=True)
            raise
        if written == 0:
            dest.unlink(missing_ok=True)
            raise RuntimeError("Telegram returned an empty file")

    def send_file(self, method: str, field: str, chat_id: int, path: Path, caption: str = "") -> int | None:
        boundary = "----cursor" + secrets.token_hex(8)
        filename = safe_filename(path.name)
        chunks: list[bytes] = []

        def field_part(name: str, value: str) -> None:
            chunks.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
            )

        field_part("chat_id", str(chat_id))
        if caption:
            field_part("caption", caption[:1024])
        chunks.append(
            (
                f"--{boundary}\r\n"
                f"Content-Disposition: form-data; name=\"{field}\"; filename=\"{filename}\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode()
            + path.read_bytes()
            + b"\r\n"
        )
        chunks.append(f"--{boundary}--\r\n".encode())
        body = b"".join(chunks)
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with self._lock:
                with urllib.request.urlopen(request, timeout=120) as response:
                    parsed = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = redact(exc.read().decode("utf-8", "replace"), self.token)
            raise RuntimeError(redact(f"telegram {method} failed: {exc.code} {detail[:300]}", self.token)) from None
        except urllib.error.URLError as exc:
            raise RuntimeError(redact(f"telegram {method} unreachable: {exc.reason}", self.token)) from None
        if not parsed.get("ok"):
            raise RuntimeError(redact(f"telegram {method} not ok", self.token))
        message_id = parsed.get("result", {}).get("message_id")
        return message_id if isinstance(message_id, int) else None

    def edit(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        html: bool = False,
        reply_markup: dict | None = None,
    ) -> None:
        payload: dict = {"chat_id": chat_id, "message_id": message_id, "text": text[:4096]}
        if html:
            payload["parse_mode"] = "HTML"
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self.call("editMessageText", payload, timeout=30)

    def send_rich(
        self,
        chat_id: int,
        markdown: str,
        reply_markup: dict | None = None,
        reply_to: int | None = None,
    ) -> int | None:
        payload: dict = {
            "chat_id": chat_id,
            "rich_message": {"markdown": markdown[:32768]},
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if isinstance(reply_to, int):
            payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        result = self.call("sendRichMessage", payload, timeout=30)
        message_id = result.get("result", {}).get("message_id")
        return message_id if isinstance(message_id, int) else None

    def send_draft(self, chat_id: int, draft_id: int, markdown: str) -> None:
        self.call(
            "sendRichMessageDraft",
            {
                "chat_id": chat_id,
                "draft_id": draft_id,
                "can_stop": True,
                "rich_message": {"markdown": markdown[:32768]},
            },
            timeout=30,
        )

    def edit_rich(
        self,
        chat_id: int,
        message_id: int,
        markdown: str,
        reply_markup: dict | None = None,
    ) -> None:
        payload: dict = {
            "chat_id": chat_id,
            "message_id": message_id,
            "rich_message": {"markdown": markdown[:32768]},
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self.call("editMessageText", payload, timeout=30)

    def delete_many(self, chat_id: int, message_ids: list[int]) -> None:
        for start in range(0, len(message_ids), 100):
            chunk = message_ids[start : start + 100]
            if not chunk:
                continue
            try:
                self.call("deleteMessages", {"chat_id": chat_id, "message_ids": chunk}, timeout=30)
            except RuntimeError as exc:
                log.warning("delete batch failed: %s", exc)
                for message_id in chunk:
                    try:
                        self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id}, timeout=15)
                    except RuntimeError as single:
                        log.info("left message %s in place: %s", message_id, single)


class LiveBoard:
    """One Telegram message for the run: header, streamed answer, one tool line, then the final reply."""

    def __init__(
        self,
        telegram: Telegram,
        chat_id: int,
        on_message,
        model: str = "auto",
        project: str = "",
        reply_to: int | None = None,
    ) -> None:
        self.telegram = telegram
        self.chat_id = chat_id
        self.on_message = on_message
        self.model = (model or "auto").strip() or "auto"
        self.project = project or "workspace"
        self.reply_to = reply_to if isinstance(reply_to, int) else None
        self.session_label = "new"
        self.transcript = Transcript()
        self.started = time.time()
        self.activity = ""
        self.snippet = ""
        self.phase = "running"
        self.final_body: str | None = None
        self.message_id: int | None = None
        self.visible = ""
        self.last_at = 0.0
        self.dirty = False
        self.timer_slot = -1
        self.extras_sent = False
        self.use_rich = True
        self.use_draft = True
        self.rich_dead = False
        self.draft_id = secrets.randbelow(2_000_000_000) + 1
        self.tool_log: list[str] = []

    def set_session(self, session_id: str) -> None:
        short = session_id.strip()[:8]
        if not short or short == self.session_label:
            return
        self.session_label = short
        self.publish(force=True)

    def note(self, phrase: str) -> None:
        if not phrase or phrase == self.activity:
            return
        self.activity = phrase if phrase[:1] in "🟡✅⛔🔴" else f"🟡 {phrase}"
        self.snippet = ""
        self.publish(force=True)

    def show_tool(self, tool_call: object, *, done: bool) -> None:
        line = format_activity(tool_call, done=done)
        snippet = format_snippet(tool_call)
        if line == self.activity and snippet == self.snippet:
            return
        self.activity = line
        self.snippet = snippet
        if done:
            entry = line
            if snippet:
                safe_snippet = snippet.replace("```", "'''")
                entry = f"{line}\n\n```\n{safe_snippet}\n```"
            if entry and (not self.tool_log or self.tool_log[-1] != entry):
                self.tool_log.append(entry)
            if len(self.tool_log) > 30:
                del self.tool_log[:-30]
        self.publish(force=True)

    def stream(self, text: str) -> None:
        if self.transcript.add_delta(text):
            self.publish(force=False)

    def complete(self, text: str) -> None:
        if self.transcript.add_complete(text):
            self.publish(force=False)

    def finish(self, text: str, phase: str) -> None:
        self.final_body = (text or self.transcript.render()).strip()
        self.phase = phase
        self.activity = ""
        self.snippet = ""
        if not self.use_rich and not self.rich_dead:
            self.use_rich = True
        self.publish(force=True)

    def publish(self, force: bool = False) -> None:
        now = time.time()
        slot = int(now - self.started) // 5
        if not force and slot == self.timer_slot and now - self.last_at < 0.55:
            self.dirty = True
            return
        self.timer_slot = slot
        self._present()
        self.dirty = False
        self.last_at = time.time()

    def tick(self) -> None:
        slot = int(time.time() - self.started) // 5
        if slot != self.timer_slot:
            self.dirty = True
        if self.dirty:
            self.publish(force=True)

    def flush(self) -> None:
        self.publish(force=True)

    def _view_args(self) -> dict:
        elapsed = max(0, int(time.time() - self.started))
        body = self.final_body if self.final_body is not None else self.transcript.render()
        return {
            "model": self.model,
            "project": self.project,
            "session": self.session_label,
            "elapsed": elapsed,
            "body": body,
            "activity": self.activity if self.phase == "running" else "",
            "snippet": self.snippet if self.phase == "running" else "",
            "phase": self.phase,
            "tools": list(self.tool_log),
        }

    def _present(self) -> None:
        fields = self._view_args()
        if self.phase == "running" and self.use_draft and self._present_draft(fields):
            return
        if self.use_rich and self._present_rich(fields):
            return
        formatted = build_run_html(**fields)
        markup = None if self.phase == "running" else RUN_KEYBOARD
        if self.phase == "running" or len(formatted) <= TELEGRAM_LIMIT:
            show = formatted if len(formatted) <= 4096 else formatted[:TELEGRAM_LIMIT]
            self._send_piece(show, html=True, markup=markup, plain_fields=fields)
            return
        parts = chunk_html(formatted)
        self._send_piece(parts[0], html=True, markup=None, plain_fields=fields)
        if self.extras_sent:
            return
        self.extras_sent = True
        anchor = self.message_id
        anchor_visible = self.visible
        for index, part in enumerate(parts[1:], start=1):
            self.message_id = None
            self.visible = ""
            self._send_piece(
                part,
                html=True,
                markup=markup if index == len(parts) - 1 else None,
                plain_fields=fields,
            )
        self.message_id = anchor
        self.visible = anchor_visible

    def _present_draft(self, fields: dict) -> bool:
        markdown = build_run_rich_markdown(**fields, draft=True)
        marker = "draft\n" + markdown
        if marker == self.visible:
            return True
        try:
            self.telegram.send_draft(self.chat_id, self.draft_id, markdown)
        except RuntimeError as exc:
            log.info("rich draft failed, using a saved message: %s", exc)
            self.use_draft = False
            return False
        self.visible = marker
        return True

    def _present_rich(self, fields: dict) -> bool:
        markdown = build_run_rich_markdown(**fields)
        markup = None
        marker = "rich\n" + markdown
        if marker == self.visible and self.message_id is not None:
            return True
        if self.message_id is None:
            try:
                message_id = self.telegram.send_rich(
                    self.chat_id,
                    markdown,
                    reply_markup=markup,
                    reply_to=self.reply_to,
                )
            except RuntimeError as exc:
                detail = str(exc)
                log.info("rich send failed, using html: %s", detail)
                if rich_unavailable(detail):
                    self.rich_dead = True
                self.use_rich = False
                return False
            if message_id is None:
                self.use_rich = False
                return False
            self.message_id = message_id
            self.visible = marker
            self.on_message(message_id)
            return True
        try:
            self.telegram.edit_rich(self.chat_id, self.message_id, markdown, reply_markup=markup)
        except RuntimeError as exc:
            detail = str(exc)
            if "message is not modified" in detail:
                self.visible = marker
                return True
            log.info("rich edit failed, using html: %s", detail)
            self.use_rich = False
            return False
        self.visible = marker
        return True

    def _send_piece(
        self,
        show: str,
        *,
        html: bool,
        markup: dict | None,
        plain_fields: dict | None = None,
        replaced: bool = False,
    ) -> None:
        marker = show + ("|buttons" if markup else "")
        if marker == self.visible and self.message_id is not None:
            return
        if self.message_id is None:
            try:
                message_id = self.telegram.send_one(
                    self.chat_id,
                    show,
                    reply_markup=markup,
                    html=html,
                    reply_to=self.reply_to,
                )
            except RuntimeError as exc:
                if html and plain_fields is not None and html_rejected(str(exc)):
                    self._send_piece(build_run_plain(**plain_fields)[:4096], html=False, markup=markup)
                    return
                log.warning("send failed: %s", exc)
                return
            self.message_id = message_id
            self.visible = marker
            if message_id is not None:
                self.on_message(message_id)
            return
        try:
            self.telegram.edit(self.chat_id, self.message_id, show, html=html, reply_markup=markup)
        except RuntimeError as exc:
            detail = str(exc)
            if "message is not modified" in detail:
                self.visible = marker
                return
            if html and plain_fields is not None and html_rejected(detail):
                self._send_piece(
                    build_run_plain(**plain_fields)[:4096],
                    html=False,
                    markup=markup,
                    replaced=replaced,
                )
                return
            if not replaced and ("message to edit not found" in detail or "MESSAGE_ID_INVALID" in detail):
                log.info("live message gone, starting another")
                self.message_id = None
                self.visible = ""
                self._send_piece(show, html=html, markup=markup, plain_fields=plain_fields, replaced=True)
                return
            log.warning("edit failed: %s", exc)
            return
        self.visible = marker


class State:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.data = {
            "version": STATE_VERSION,
            "offset": 0,
            "initialized": False,
            "verified": {},
            "chats": {},
        }
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            log.warning("state file unreadable, starting fresh")
            return
        if isinstance(loaded, dict):
            self.data.update(loaded)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)


class Runner:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.proc: subprocess.Popen | None = None
        self.cancel = threading.Event()

    def busy(self) -> bool:
        with self.lock:
            return self.thread is not None and self.thread.is_alive()

    def start(self, target) -> bool:
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                return False
            self.cancel.clear()
            self.proc = None
            self.thread = threading.Thread(target=target, name="cursor-run", daemon=True)
            self.thread.start()
            return True

    def attach(self, proc: subprocess.Popen) -> None:
        with self.lock:
            self.proc = proc
            should_stop = self.cancel.is_set()
        if should_stop:
            stop_process(proc)

    def stop(self) -> None:
        self.cancel.set()
        with self.lock:
            proc = self.proc
        if proc is not None:
            stop_process(proc)

    def reset_cancel(self) -> None:
        with self.lock:
            self.cancel.clear()
            self.proc = None


def stop_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + 5
    while time.time() < deadline and proc.poll() is None:
        time.sleep(0.1)
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def find_agent() -> str:
    override = os.environ.get("CURSOR_AGENT", "").strip()
    if override:
        return override
    local = Path.home() / ".local" / "bin" / "agent"
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    raise ConfigError("Cursor CLI is not installed at ~/.local/bin/agent")


def default_workspace(home: Path) -> Path:
    configured = os.environ.get("CURSOR_WORKSPACE", "").strip()
    if configured:
        try:
            return resolve_workspace(configured, home)
        except ValueError as exc:
            log.warning("CURSOR_WORKSPACE ignored: %s", exc)
    lab = home / "alienware-lab"
    if lab.is_dir():
        return lab.resolve()
    return home.resolve()


HELP = "\n".join(
    [
        "Cursor is running on this machine under your account.",
        "",
        "/status",
        "/new — start a fresh chat",
        "/cancel — stop the current run. Queued messages still run",
        "/mode agent — edit files and run commands",
        "/mode ask — read only",
        "/mode plan — plan only",
        "/workspace — show or set the folder (inside your home directory)",
        "/clear — delete chat messages here. The Cursor session stays",
        "/files — newest files in the folder. Tap one to get it",
        "/files find name — search, including photos and audio",
        "",
        "Send a photo, file, video, or audio message and Cursor receives it.",
        "Any other text message is sent to that local session.",
        "A message sent while Cursor is busy waits its turn.",
        "👀 queued  ·  ⚡ working  ·  👍 done",
    ]
)

SHARE = {
    "keyboard": [[{"text": "Share my phone number", "request_contact": True}]],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}
REMOVE_KEYBOARD = {"remove_keyboard": True}


class Prompt:
    def __init__(self, chat_id: int, user_id: int, text: str, message_id: int | None) -> None:
        self.chat_id = chat_id
        self.user_id = user_id
        self.text = text
        self.message_id = message_id


class Bridge:
    def __init__(self, telegram: Telegram, state: State, allow: set[str], agent: str, home: Path) -> None:
        self.telegram = telegram
        self.state = state
        self.allow = allow
        self.agent = agent
        self.home = home
        self.runner = Runner()
        self.pending: deque[Prompt] = deque()
        self.queue_lock = threading.Lock()
        self.active_draft_id: int | None = None
        self.drop_session = False
        self.default_workspace = default_workspace(home)

    def queued(self) -> int:
        with self.queue_lock:
            return len(self.pending)

    def chat(self, user_id: int) -> dict:
        key = str(user_id)
        chats = self.state.data.setdefault("chats", {})
        row = chats.get(key)
        if not isinstance(row, dict):
            row = {}
            chats[key] = row
        row.setdefault("workspace", str(self.default_workspace))
        row.setdefault("mode", "agent")
        row.setdefault("session_id", "")
        if not isinstance(row.get("message_ids"), list):
            row["message_ids"] = []
        return row

    def remember(self, user_id: int, message_id: int | None) -> None:
        if not isinstance(message_id, int):
            return
        with self.state.lock:
            row = self.chat(user_id)
            ids = row["message_ids"]
            if message_id not in ids:
                ids.append(message_id)
            if len(ids) > 800:
                del ids[:-800]
            self.state.save()

    def say(self, user_id: int, chat_id: int, text: str, reply_markup: dict | None = None) -> int | None:
        first = None
        for index, part in enumerate(split_text(text)):
            markup = reply_markup if index == 0 else None
            message_id = self.telegram.send_one(chat_id, part, markup)
            self.remember(user_id, message_id)
            if first is None:
                first = message_id
        return first

    def clear_chat(self, chat_id: int, user_id: int, command_id: int | None) -> None:
        with self.state.lock:
            ids = message_ids_for_clear(self.chat(user_id), command_id)
        self.telegram.delete_many(chat_id, ids)
        with self.state.lock:
            self.chat(user_id)["message_ids"] = []
            self.state.save()
        self.say(user_id, chat_id, "Chat cleared. The Cursor session is unchanged.")

    def verified_phone(self, user_id: int) -> str | None:
        phone = self.state.data.get("verified", {}).get(str(user_id))
        if not isinstance(phone, str):
            return None
        if phone not in self.allow:
            self.state.data.get("verified", {}).pop(str(user_id), None)
            self.state.save()
            return None
        return phone

    def run(self) -> None:
        self.telegram.call("deleteWebhook", {"drop_pending_updates": False}, timeout=30)
        self.telegram.call(
            "setMyCommands",
            {
                "commands": [
                    {"command": "status", "description": "Account, folder, and chat"},
                    {"command": "new", "description": "Start a fresh chat"},
                    {"command": "cancel", "description": "Stop the current run"},
                    {"command": "mode", "description": "agent, ask, or plan"},
                    {"command": "workspace", "description": "Show or set the folder"},
                    {"command": "clear", "description": "Delete chat messages, keep the Cursor session"},
                    {"command": "files", "description": "List or search files, including audio"},
                    {"command": "help", "description": "What this bot can do"},
                ]
            },
            timeout=30,
        )
        if not self.state.data.get("initialized"):
            self.drain()
        log.info("bridge up, %d allowlisted numbers", len(self.allow))
        while True:
            self.pump()
            timeout = 2 if self.runner.busy() or self.queued() else 25
            try:
                updates = self.telegram.call(
                    "getUpdates",
                    {
                        "offset": self.state.data.get("offset", 0),
                        "timeout": timeout,
                        "allowed_updates": ALLOWED_UPDATES,
                    },
                    timeout=timeout + 15,
                )
            except ConfigError:
                raise
            except RuntimeError as exc:
                log.warning("%s", exc)
                time.sleep(5)
                continue
            for update in updates.get("result") or []:
                self.accept(update)

    def drain(self) -> None:
        skipped = 0
        while True:
            updates = self.telegram.call(
                "getUpdates",
                {"offset": self.state.data.get("offset", 0), "timeout": 0, "allowed_updates": ALLOWED_UPDATES},
                timeout=30,
            )
            batch = updates.get("result") or []
            if not batch:
                break
            skipped += len(batch)
            last = max(int(update.get("update_id", 0)) for update in batch)
            self.state.data["offset"] = last + 1
            if len(batch) < 100:
                break
        self.state.data["initialized"] = True
        self.state.save()
        log.info("skipped %d pending updates from before startup", skipped)

    def on_callback(self, query: dict) -> None:
        query_id = query.get("id")
        if isinstance(query_id, str):
            try:
                self.telegram.call("answerCallbackQuery", {"callback_query_id": query_id}, timeout=15)
            except RuntimeError as exc:
                log.warning("callback answer failed: %s", exc)
        sender = query.get("from") or {}
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        user_id = sender.get("id")
        chat_id = chat.get("id")
        data = query.get("data") if isinstance(query.get("data"), str) else ""
        if not isinstance(user_id, int) or not isinstance(chat_id, int):
            return
        if chat.get("type") not in {None, "private"}:
            return
        if self.verified_phone(user_id) is None:
            return
        if data == "status":
            self.say(user_id, chat_id, self.status_text(user_id))
        elif data == "end":
            if self.runner.busy():
                self.drop_session = True
            self.chat(user_id)["session_id"] = ""
            self.state.save()
            self.say(user_id, chat_id, "Next message starts a fresh chat.")
        elif data == "diff":
            self.send_diff(user_id, chat_id)
        elif data.startswith("f:"):
            self.send_saved_file(user_id, chat_id, data[2:])
        elif data.startswith("fp:"):
            page_text = data[3:]
            page = int(page_text) if page_text.isdigit() else 0
            row = self.chat(user_id)
            self.show_files(chat_id, user_id, str(row.get("file_query") or ""), page)

    def send_diff(self, user_id: int, chat_id: int) -> None:
        raw = str(self.chat(user_id).get("workspace") or "")
        try:
            folder = resolve_workspace(raw, self.home)
        except ValueError as exc:
            self.say(user_id, chat_id, str(exc))
            return
        excludes = [".", ":(exclude).env", ":(exclude)*.env"]
        try:
            stat = subprocess.run(
                ["git", "-C", str(folder), "diff", "--stat", "--", *excludes],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except OSError:
            self.say(user_id, chat_id, "git is not available on this machine.")
            return
        if stat.returncode != 0:
            self.say(user_id, chat_id, "This folder is not a git repository.")
            return
        diff = subprocess.run(
            ["git", "-C", str(folder), "diff", "--", *excludes],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        summary = (stat.stdout or "").strip()
        changes = (diff.stdout or "").strip()
        if not summary and not changes:
            self.say(user_id, chat_id, "No uncommitted changes.")
            return
        lines = f"{summary}\n\n{changes}".splitlines()
        if len(lines) > 80:
            text = "\n".join(lines[:80]) + f"\n… +{len(lines) - 80} lines"
        else:
            text = "\n".join(lines)
        shown = f"<pre><code>{escape_html(text[:3500])}</code></pre>"
        try:
            message_id = self.telegram.send_one(chat_id, shown, html=True)
        except RuntimeError:
            message_id = self.telegram.send_one(chat_id, text[:3500])
        if message_id is not None:
            self.remember(user_id, message_id)

    def accept(self, update: dict) -> None:
        update_id = int(update.get("update_id", 0))
        self.state.data["offset"] = update_id + 1
        self.state.save()
        stopped = update.get("stopped_message_generation")
        if isinstance(stopped, dict):
            self.on_generation_stopped(stopped)
            return
        query = update.get("callback_query")
        if isinstance(query, dict):
            self.on_callback(query)
            return
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat") or {}
        if chat.get("type") != "private":
            return
        sender = message.get("from") or {}
        if sender.get("is_bot"):
            return
        user_id = sender.get("id")
        chat_id = chat.get("id")
        if not isinstance(user_id, int) or not isinstance(chat_id, int):
            return
        log.info("update %s user %s", update_id, user_id)
        incoming = message.get("message_id")
        if isinstance(incoming, int):
            self.remember(user_id, incoming)
        if "contact" in message:
            self.on_contact(chat_id, user_id, message)
            return
        if self.verified_phone(user_id) is None:
            self.say(
                user_id,
                chat_id,
                "Share your phone number with the button. Only numbers on the allowlist can use this bot.",
                SHARE,
            )
            return
        attachment = attachment_from_message(message)
        if attachment is not None:
            self.on_file(chat_id, user_id, attachment, incoming if isinstance(incoming, int) else None)
            return
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            self.say(user_id, chat_id, "Send a text message, photo, file, or audio.")
            return
        self.on_text(chat_id, user_id, text, incoming if isinstance(incoming, int) else None)

    def on_contact(self, chat_id: int, user_id: int, message: dict) -> None:
        phone = contact_phone(message)
        if phone is None:
            self.say(user_id, chat_id, "Use the button to share your own number.", SHARE)
            return
        if phone not in self.allow:
            log.info("rejected user %s phone %s", user_id, mask_phone(phone))
            self.say(
                user_id,
                chat_id,
                f"{mask_phone(phone)} is not on the allowlist.",
                SHARE,
            )
            return
        self.state.data.setdefault("verified", {})[str(user_id)] = phone
        self.state.save()
        log.info("verified user %s phone %s", user_id, mask_phone(phone))
        self.say(
            user_id,
            chat_id,
            "Number accepted. Messages now go to Cursor on this machine.\n\n" + HELP,
            REMOVE_KEYBOARD,
        )

    def on_file(self, chat_id: int, user_id: int, attachment: dict, message_id: int | None = None) -> None:
        size = attachment.get("size") or 0
        if isinstance(size, int) and size > MAX_DOWNLOAD:
            self.say(user_id, chat_id, "Telegram only lets the bot download files up to 20 MB.")
            return
        try:
            folder = resolve_workspace(str(self.chat(user_id).get("workspace") or ""), self.home)
        except ValueError as exc:
            self.say(user_id, chat_id, str(exc))
            return
        dest = folder / ".cursor-telegram" / "inbound" / f"{int(time.time())}-{safe_filename(str(attachment.get('name') or 'file'))}"
        try:
            self.telegram.download(str(attachment["file_id"]), dest)
        except RuntimeError as exc:
            log.warning("download failed: %s", exc)
            self.say(user_id, chat_id, "Could not download that file.")
            return
        prompt = inbound_prompt(dest, str(attachment.get("caption") or ""))
        self.submit(chat_id, user_id, prompt, message_id)

    def show_files(self, chat_id: int, user_id: int, query: str, page: int) -> None:
        try:
            folder = resolve_workspace(str(self.chat(user_id).get("workspace") or ""), self.home)
        except ValueError as exc:
            self.say(user_id, chat_id, str(exc))
            return
        row = self.chat(user_id)
        row["file_query"] = query
        paths = list_sendable(folder, query)
        chunk, text = file_page(paths, page, folder)
        if not chunk:
            self.say(user_id, chat_id, text)
            return
        rows = []
        for path in chunk:
            token = self._file_token(user_id, path)
            label = path.name
            if media_kind(path) in {"audio", "voice"}:
                label = "🎧 " + label
            elif media_kind(path) in {"photo", "animation"}:
                label = "🖼 " + label
            rows.append([{"text": label[:40], "callback_data": f"f:{token}"}])
        if (page + 1) * FILE_PAGE < len(paths):
            rows.append([{"text": "More", "callback_data": f"fp:{page + 1}"}])
        self.say(user_id, chat_id, text, {"inline_keyboard": rows})

    def _file_token(self, user_id: int, path: Path) -> str:
        token = secrets.token_hex(4)
        bucket = self.state.data.setdefault("file_tokens", {})
        if isinstance(bucket, dict) and len(bucket) > 300:
            for key in list(bucket)[:80]:
                bucket.pop(key, None)
        if not isinstance(bucket, dict):
            bucket = {}
            self.state.data["file_tokens"] = bucket
        bucket[token] = {"user": user_id, "path": str(path)}
        self.state.save()
        return token

    def send_saved_file(self, user_id: int, chat_id: int, token: str) -> None:
        bucket = self.state.data.get("file_tokens")
        row = bucket.get(token) if isinstance(bucket, dict) else None
        if not isinstance(row, dict) or row.get("user") != user_id or not isinstance(row.get("path"), str):
            self.say(user_id, chat_id, "That file link expired. Send /files again.")
            return
        path = Path(row["path"])
        try:
            folder = resolve_workspace(str(self.chat(user_id).get("workspace") or ""), self.home)
            allowed = inside_workspace(folder, str(path))
        except ValueError as exc:
            self.say(user_id, chat_id, str(exc))
            return
        if allowed is None or not allowed.is_file():
            self.say(user_id, chat_id, "That file is no longer available.")
            return
        if not self.deliver_file(chat_id, user_id, allowed):
            self.say(user_id, chat_id, f"Could not send {allowed.name}.")

    def deliver_file(self, chat_id: int, user_id: int, path: Path) -> bool:
        try:
            if not path.is_file() or path.stat().st_size > MAX_UPLOAD or path.stat().st_size <= 0:
                return False
        except OSError:
            return False
        if blocked_file(path):
            return False
        kind = media_kind(path)
        method, field = {
            "photo": ("sendPhoto", "photo"),
            "animation": ("sendAnimation", "animation"),
            "video": ("sendVideo", "video"),
            "audio": ("sendAudio", "audio"),
            "voice": ("sendVoice", "voice"),
        }.get(kind, ("sendDocument", "document"))
        try:
            message_id = self.telegram.send_file(method, field, chat_id, path, caption=path.name)
        except RuntimeError as exc:
            log.warning("send file failed: %s", exc)
            if kind == "document":
                return False
            try:
                message_id = self.telegram.send_file("sendDocument", "document", chat_id, path, caption=path.name)
            except RuntimeError as second:
                log.warning("send document failed: %s", second)
                return False
        if message_id is not None:
            self.remember(user_id, message_id)
        return message_id is not None

    def on_text(self, chat_id: int, user_id: int, text: str, message_id: int | None = None) -> None:
        command, rest = parse_command(text)
        if command in {"/start", "/help"}:
            self.say(user_id, chat_id, HELP)
            return
        if command == "/clear":
            self.clear_chat(chat_id, user_id, message_id)
            return
        if command == "/cancel":
            if self.runner.busy():
                self.runner.stop()
                waiting = self.queued()
                if waiting == 1:
                    note = "Stopping the current run. 1 message is still queued."
                elif waiting:
                    note = f"Stopping the current run. {waiting} messages are still queued."
                else:
                    note = "Stopping the current run."
                self.say(user_id, chat_id, note)
            else:
                self.say(user_id, chat_id, "Nothing is running.")
            return
        if command == "/status":
            self.say(user_id, chat_id, self.status_text(user_id))
            return
        if command == "/new":
            if self.runner.busy():
                self.drop_session = True
            self.chat(user_id)["session_id"] = ""
            self.state.save()
            self.say(user_id, chat_id, "Next message starts a fresh chat.")
            return
        if command == "/mode":
            self.set_mode(chat_id, user_id, rest)
            return
        if command == "/workspace":
            self.set_workspace(chat_id, user_id, rest)
            return
        if command == "/files":
            query = rest[5:].strip() if rest.lower().startswith("find ") else rest.strip()
            self.show_files(chat_id, user_id, query, 0)
            return
        if command is not None:
            self.say(user_id, chat_id, "Unknown command.\n\n" + HELP)
            return
        if is_cloud_handoff(text):
            self.say(user_id, chat_id, "Cloud handoff is off. This bot only runs Cursor on this machine.")
            return
        self.submit(chat_id, user_id, text, message_id)

    def submit(self, chat_id: int, user_id: int, text: str, message_id: int | None) -> None:
        prompt = Prompt(chat_id, user_id, text, message_id)
        with self.queue_lock:
            if self.runner.busy():
                if len(self.pending) >= MAX_QUEUE:
                    full = True
                    queued = False
                else:
                    self.pending.append(prompt)
                    full = False
                    queued = True
            else:
                full = False
                queued = False
        if full:
            self.say(user_id, chat_id, f"Queue is full ({MAX_QUEUE}). Wait for a run to finish.")
            return
        if queued:
            self.telegram.react(chat_id, message_id, REACTION_QUEUED)
            return
        self._launch(prompt)

    def pump(self) -> None:
        if self.runner.busy():
            return
        with self.queue_lock:
            if not self.pending:
                return
            prompt = self.pending.popleft()
        self._launch(prompt)

    def _launch(self, prompt: Prompt) -> None:
        self.telegram.react(prompt.chat_id, prompt.message_id, REACTION_WORKING)
        if self.runner.start(lambda: self._drain(prompt)):
            return
        with self.queue_lock:
            self.pending.appendleft(prompt)
        self.telegram.react(prompt.chat_id, prompt.message_id, REACTION_QUEUED)

    def _drain(self, prompt: Prompt | None) -> None:
        while prompt is not None:
            self.telegram.react(prompt.chat_id, prompt.message_id, REACTION_WORKING)
            phase = "error"
            try:
                phase = self.execute(prompt.chat_id, prompt.user_id, prompt.text, prompt.message_id)
            except ConfigError as exc:
                log.error("%s", exc)
                os._exit(78)
            except Exception:
                log.exception("run failed")
                try:
                    self.say(prompt.user_id, prompt.chat_id, "The bridge hit an error before Cursor finished.")
                except Exception:
                    log.exception("could not report the error")
                phase = "error"
            emoji = {
                "done": REACTION_DONE,
                "cancelled": REACTION_CANCELLED,
                "error": REACTION_ERROR,
            }.get(phase, REACTION_ERROR)
            self.telegram.react(prompt.chat_id, prompt.message_id, emoji)
            with self.queue_lock:
                prompt = self.pending.popleft() if self.pending else None
            if prompt is not None:
                self.runner.reset_cancel()

    def set_mode(self, chat_id: int, user_id: int, rest: str) -> None:
        row = self.chat(user_id)
        if not rest:
            self.say(user_id, chat_id, f"Mode is {row['mode']}.")
            return
        mode = rest.split()[0].lower()
        if mode not in {"agent", "ask", "plan"}:
            self.say(user_id, chat_id, "Use /mode agent, /mode ask, or /mode plan.")
            return
        row["mode"] = mode
        self.state.save()
        self.say(user_id, chat_id, f"Mode is {mode}.")

    def set_workspace(self, chat_id: int, user_id: int, rest: str) -> None:
        row = self.chat(user_id)
        if not rest:
            self.say(user_id, chat_id, f"Folder is {row['workspace']}")
            return
        try:
            path = resolve_workspace(rest, self.home)
        except ValueError as exc:
            self.say(user_id, chat_id, str(exc))
            return
        if self.runner.busy():
            self.drop_session = True
        row["workspace"] = str(path)
        row["session_id"] = ""
        self.state.save()
        self.say(user_id, chat_id, f"Folder is {path}. The next message starts a fresh chat there.")

    def status_text(self, user_id: int) -> str:
        row = self.chat(user_id)
        phone = self.verified_phone(user_id) or ""
        account = self.account_line()
        session = row.get("session_id") or "none"
        running = "running" if self.runner.busy() else "idle"
        waiting = self.queued()
        run_line = f"Run {running}"
        if waiting == 1:
            run_line += ", 1 queued"
        elif waiting:
            run_line += f", {waiting} queued"
        return "\n".join(
            [
                account,
                f"Phone {mask_phone(phone)}",
                f"Folder {row.get('workspace')}",
                f"Mode {row.get('mode')}",
                f"Model {cursor_model()}",
                f"Chat {session}",
                run_line,
            ]
        )

    def account_line(self) -> str:
        try:
            completed = subprocess.run(
                [self.agent, "status", "--format", "json"],
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
                env=self.agent_env(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"Cursor status unavailable ({exc.__class__.__name__})"
        if completed.returncode != 0:
            return "Cursor CLI is installed and not signed in. Run scripts/login.sh from the Mac."
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            line = completed.stdout.strip().splitlines()
            return line[0][:200] if line else "Cursor is signed in."
        if isinstance(payload, dict):
            for key in ("email", "userEmail", "account"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    return f"Cursor account {value}"
            user = payload.get("user")
            if isinstance(user, dict):
                for key in ("email", "name"):
                    value = user.get(key)
                    if isinstance(value, str) and value:
                        return f"Cursor account {value}"
        return "Cursor is signed in."

    def agent_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.pop("CURSOR_API_KEY", None)
        env["NO_OPEN_BROWSER"] = "1"
        local_bin = str(Path.home() / ".local" / "bin")
        env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")
        return env

    def execute(self, chat_id: int, user_id: int, prompt: str, message_id: int | None = None) -> str:
        row = self.chat(user_id)
        workspace = str(row.get("workspace") or self.default_workspace)
        mode = str(row.get("mode") or "agent")
        session_id = str(row.get("session_id") or "")
        try:
            resolve_workspace(workspace, self.home)
        except ValueError as exc:
            self.say(user_id, chat_id, str(exc))
            return "error"
        board = LiveBoard(
            self.telegram,
            chat_id,
            lambda sent_id: self.remember(user_id, sent_id),
            model=cursor_model(),
            project=Path(workspace).name,
            reply_to=message_id,
        )
        self.active_draft_id = board.draft_id
        try:
            return self._execute_board(chat_id, user_id, prompt, workspace, mode, session_id, board)
        finally:
            if self.active_draft_id == board.draft_id:
                self.active_draft_id = None

    def on_generation_stopped(self, stopped: dict) -> None:
        chat = stopped.get("chat") or {}
        chat_id = chat.get("id")
        if chat.get("type") not in {None, "private"} or not isinstance(chat_id, int):
            return
        if self.verified_phone(chat_id) is None:
            return
        if stopped.get("draft_id") != self.active_draft_id or not self.runner.busy():
            return
        log.info("draft %s stopped", self.active_draft_id)
        self.runner.stop()

    def _execute_board(
        self,
        chat_id: int,
        user_id: int,
        prompt: str,
        workspace: str,
        mode: str,
        session_id: str,
        board: LiveBoard,
    ) -> str:
        board.publish(force=True)
        sent: set[str] = set()

        def on_output(path: Path) -> None:
            key = str(path)
            if key in sent:
                return
            if self.deliver_file(chat_id, user_id, path):
                sent.add(key)

        outcome = self.spawn(workspace, mode, session_id, prompt, board, on_output)
        if outcome.get("resume_failed") and session_id:
            board.note("Starting a fresh chat")
            outcome = self.spawn(workspace, mode, "", prompt, board, on_output)
        new_session = outcome.get("session_id") or ""
        if self.drop_session:
            self.drop_session = False
            self.chat(user_id)["session_id"] = ""
            self.state.save()
        elif isinstance(new_session, str) and new_session:
            self.chat(user_id)["session_id"] = new_session
            self.state.save()
        board.flush()
        return board.phase if board.phase in {"done", "cancelled", "error"} else "done"

    def spawn(
        self,
        workspace: str,
        mode: str,
        session_id: str,
        prompt: str,
        board: LiveBoard,
        on_output=None,
    ) -> dict:
        cmd = [
            self.agent,
            "-p",
            "--force",
            "--trust",
            "--approve-mcps",
            "--sandbox",
            "disabled",
            "--output-format",
            "stream-json",
            "--stream-partial-output",
            "--workspace",
            workspace,
        ]
        if mode in {"ask", "plan"}:
            cmd.extend(["--mode", mode])
        model = os.environ.get("CURSOR_MODEL", "").strip()
        if model:
            cmd.extend(["--model", model])
        if session_id:
            cmd.extend(["--resume", session_id])
        cmd.append(prompt)
        acc = {"last": "", "result": "", "tool": "", "session_id": "", "is_error": False, "duration_ms": 0}
        root = Path(workspace)
        before_media = media_index(root)
        stderr_chunks: list[bytes] = []
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                cwd=workspace,
                env=self.agent_env(),
                start_new_session=True,
            )
        except OSError as exc:
            board.finish(f"Could not start Cursor: {exc}", "error")
            return {"text": board.transcript.render(), "session_id": "", "cancelled": False, "resume_failed": False}
        self.runner.attach(proc)
        lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._read_stdout, args=(proc, lines), daemon=True).start()
        stderr_thread = threading.Thread(target=self._read_stderr, args=(proc, stderr_chunks), daemon=True)
        stderr_thread.start()
        deadline = time.time() + RUN_TIMEOUT_SEC
        timed_out = False
        while True:
            if self.runner.cancel.is_set():
                stop_process(proc)
                break
            if time.time() > deadline:
                timed_out = True
                stop_process(proc)
                break
            try:
                line = lines.get(timeout=0.4)
            except queue.Empty:
                board.tick()
                continue
            if line is None:
                break
            consume_event(line, acc, board)
            tool_call = acc.pop("pending_tool", None)
            if on_output is not None and tool_call is not None:
                for path in output_paths(tool_call, root):
                    on_output(path)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            stop_process(proc)
            proc.wait(timeout=5)
        stderr_thread.join(timeout=2)
        stderr = b"".join(stderr_chunks).decode("utf-8", "replace")[-1500:]
        cancelled = self.runner.cancel.is_set() or timed_out
        produced = (board.transcript.render() or str(acc.get("result") or "")).strip()
        auth_failed = _auth_failed(stderr)
        resume_failed = bool(session_id) and not produced and not cancelled and not auth_failed and proc.returncode not in (0, None)
        if on_output is not None:
            for path in changed_media(before_media, root):
                on_output(path)
        if resume_failed:
            return {"text": "", "session_id": "", "cancelled": False, "resume_failed": True}
        if auth_failed:
            board.finish(
                "Cursor on this machine is not signed in. From the Mac, run experiments/cursor-telegram/scripts/login.sh and open the URL it prints.",
                "error",
            )
        elif timed_out:
            board.finish(produced or "Stopped because the run exceeded 45 minutes.", "cancelled")
        elif cancelled:
            board.finish(produced or "Stopped.", "cancelled")
        elif acc.get("is_error"):
            board.finish(produced or "Cursor reported an error.", "error")
        elif not produced:
            detail = redact(stderr.strip(), self.telegram.token) or f"Cursor exited {proc.returncode}."
            board.finish(detail[:1500], "error")
        else:
            board.finish(produced, "done")
        board.flush()
        return {
            "text": board.transcript.render(),
            "session_id": "" if auth_failed else acc.get("session_id") or "",
            "cancelled": cancelled and not timed_out,
            "resume_failed": False,
        }

    @staticmethod
    def _read_stdout(proc: subprocess.Popen, lines: queue.Queue) -> None:
        if proc.stdout is None:
            lines.put(None)
            return
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    @staticmethod
    def _read_stderr(proc: subprocess.Popen, chunks: list[bytes]) -> None:
        if proc.stderr is None:
            return
        while True:
            data = proc.stderr.read(1024)
            if not data:
                return
            chunks.append(data)
            extra = sum(len(part) for part in chunks) - 8000
            while extra > 0 and chunks:
                drop = chunks.pop(0)
                extra -= len(drop)


def _auth_failed(stderr: str) -> bool:
    lowered = stderr.lower()
    markers = (
        "not authenticated",
        "please log in",
        "agent login",
        "unauthorized",
        "not logged in",
        "authentication required",
        "login required",
    )
    return any(marker in lowered for marker in markers)


def load_config() -> tuple[str, set[str]]:
    env_path = Path(os.environ.get("CURSOR_TELEGRAM_ENV", Path.home() / ".config" / "cursor-telegram" / "env"))
    if env_path.exists():
        mode = env_path.stat().st_mode
        if mode & 0o077:
            raise ConfigError(f"{env_path} is readable by someone other than your user")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    phones = os.environ.get("TELEGRAM_ALLOWED_PHONES", "").strip()
    if not token:
        raise ConfigError("TELEGRAM_BOT_TOKEN is missing")
    return token, parse_allowlist(phones)


def main() -> None:
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token, allow = load_config()
    home = Path.home()
    state_path = Path(os.environ.get("CURSOR_TELEGRAM_STATE", home / ".local" / "share" / "cursor-telegram" / "state.json"))
    bridge = Bridge(Telegram(token), State(state_path), allow, find_agent(), home)

    def on_stop(signum, _frame) -> None:
        log.info("signal %s", signum)
        bridge.runner.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)
    bridge.run()


if __name__ == "__main__":
    try:
        main()
    except ConfigError as exc:
        log.error("%s", exc)
        sys.exit(78)

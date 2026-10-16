"""One-line descriptions of Cursor tool calls."""

from __future__ import annotations

import json


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

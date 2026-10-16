"""Telegram rich messages for a run."""

from __future__ import annotations

from app.constants import RICH_LIMIT
from app.html_text import escape_html, thinking_preview

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


def safe_thinking_text(text: str, limit: int = 3500) -> str:
    shown = text.strip().replace("</details>", "").replace("<tg-", "<tg ")
    if not shown:
        return ""
    if len(shown) > limit:
        shown = "…\n" + shown[-limit:]
    return close_rich_fence(shown)


def rich_thinking_block(text: str, *, expanded: bool) -> str:
    """Thinking stays under the heading. It folds once the answer starts."""
    shown = safe_thinking_text(text)
    if not shown:
        return ""
    if expanded:
        lines = ["> **Thinking**"]
        for line in shown.splitlines():
            lines.append(f"> {line}" if line else ">")
        return "\n".join(lines)
    return "<details><summary>Thinking</summary>\n\n" + shown + "\n\n</details>"


def rich_status_heading(model: str, project: str, session: str, elapsed: int | None, *, running: bool) -> str:
    name = (model or "auto").strip() or "auto"
    bits: list[str] = []
    if session:
        bits.append(f"[{escape_rich_text(session)}]")
    if project:
        bits.append(escape_rich_text(project))
    title = " ".join(bits)
    clock = ""
    if elapsed is not None:
        icon = "⏳" if running else "⌛"
        clock = f" · {icon} {max(0, elapsed)}s"
    if title:
        return f"> **{title}** · `{name.replace('`', "'")}`{clock}"
    return f"> **{escape_rich_text(name)}**{clock}"


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
    thinking: str = "",
    draft: bool = False,
    continued: bool = False,
    buttons: bool | None = None,
) -> str:
    """One rich-markdown document for the live run or the finished answer."""
    running = phase == "running"
    if buttons is None:
        buttons = not running
    if continued:
        parts = ["> Continued"]
    else:
        parts = [rich_status_heading(model, project, session, elapsed, running=running)]
    preview = body.strip()
    thought = rich_thinking_block(thinking, expanded=running and not preview)
    if thought:
        parts.append(thought)
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
    if buttons:
        parts.append(rich_action_buttons())
    return "\n\n".join(parts)


def largest_rich_prefix(body: str, render, limit: int = RICH_LIMIT) -> int:
    """Largest prefix of body whose rendered message fits in one rich message."""
    if not body:
        return 0
    if len(render(body)) <= limit:
        return len(body)
    lo, hi, best = 0, len(body), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if mid and len(render(body[:mid])) <= limit:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if best <= 0:
        return 0
    for marker in ("\n\n", "\n"):
        cut = body.rfind(marker, 0, best)
        if cut > best // 2:
            return cut
    return best


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
    thinking: str = "",
) -> str:
    title = " ".join(part for part in (f"[{session}]" if session else "", project, model) if part)
    if elapsed is not None:
        title += f" · {max(0, elapsed)}s"
    lines = [title]
    if phase != "running":
        lines.append({"cancelled": "⛔", "error": "🔴"}.get(phase, "✅"))
    if body.strip():
        lines.append(body.strip())
    thought = thinking_preview(thinking) if phase == "running" else ""
    if thought and not body.strip():
        lines.append(thought)
    if phase == "running" and activity:
        lines.append(activity)
    if phase == "running" and snippet:
        lines.append(snippet)
    steps = [entry.splitlines()[0].strip() for entry in (tools or []) if entry.strip()][-30:]
    if steps:
        lines.append("\n".join(f"• {step}" for step in steps))
    return "\n\n".join(lines)[:4096]

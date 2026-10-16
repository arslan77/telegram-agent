"""HTML and rich-message text for a run."""

from __future__ import annotations

import html
import re

from app.constants import RICH_LIMIT


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


def thinking_preview(text: str, limit: int = 160) -> str:
    line = " ".join(text.split())
    if len(line) <= limit:
        return line
    return "…" + line[-(limit - 1) :]


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
    thinking: str = "",
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
        thought = thinking_preview(thinking)
        if thought and not preview:
            parts.append(f"<i>{escape_html(thought)}</i>")
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

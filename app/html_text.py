"""Telegram HTML for a run. Rich messages live in app.rich."""

from __future__ import annotations

import html
import re

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

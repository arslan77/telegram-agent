"""The answer text accumulated during a run."""

from __future__ import annotations

from cursor_telegram.constants import TELEGRAM_LIMIT


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

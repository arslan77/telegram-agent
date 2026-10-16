"""Shared fixtures. Each test module imports only what it exercises."""

from __future__ import annotations

from pathlib import Path

from app.bridge import Bridge
from app.state import State


class FakeTelegram:
    def __init__(self) -> None:
        self.reactions: list[tuple[int | None, str]] = []
        self.sent: list[str] = []

    def react(self, chat_id: int, message_id: int | None, emoji: str) -> None:
        self.reactions.append((message_id, emoji))

    def send_one(self, chat_id: int, text: str, reply_markup=None, html: bool = False, reply_to=None) -> int:
        self.sent.append(text)
        return len(self.sent)

    def call(self, method: str, payload: dict, timeout: float = 15) -> dict:
        return {"ok": True, "result": []}


def make_bridge(root: Path, phones: set[str] | None = None) -> tuple[Bridge, FakeTelegram]:
    telegram = FakeTelegram()
    bridge = Bridge(telegram, State(root / "state.json"), phones or set(), "/bin/true", root)
    return bridge, telegram

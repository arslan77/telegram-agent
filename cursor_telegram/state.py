"""Persistent chat, allowlist, and offset state."""

from __future__ import annotations

from pathlib import Path
import json
import os
import threading

from cursor_telegram.constants import STATE_VERSION, log


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

"""Process entry for the user service."""

from __future__ import annotations

from pathlib import Path
import logging
import os
import signal

from cursor_telegram.access import find_agent, load_config
from cursor_telegram.bridge import Bridge
from cursor_telegram.client import Telegram
from cursor_telegram.constants import log
from cursor_telegram.state import State


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

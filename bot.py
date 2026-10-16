#!/usr/bin/env python3
"""Telegram bridge to the local Cursor CLI on this machine.

Only a private chat whose sender shares a phone number on TELEGRAM_ALLOWED_PHONES
can run the agent. Everyone else gets no work done. Runs use `agent -p` on this
host. A leading & is rejected so the chat cannot be handed to a cloud agent.

The bridge itself lives in the app package. This file is the process
entry the user service starts.
"""

from __future__ import annotations

import logging
import sys

from app.access import ConfigError
from app.cli import main

if __name__ == "__main__":
    try:
        main()
    except ConfigError as exc:
        logging.getLogger("cursor-telegram").error("%s", exc)
        sys.exit(78)

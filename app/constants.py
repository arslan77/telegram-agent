"""Shared limits, reactions, and keyboards."""

from __future__ import annotations

import logging


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

RUN_KEYBOARD = {
    "inline_keyboard": [[
        {"text": "Diff", "callback_data": "diff", "style": "primary"},
        {"text": "Status", "callback_data": "status"},
        {"text": "End", "callback_data": "end", "style": "danger"},
    ]]
}
ALLOWED_UPDATES = ["message", "callback_query", "stopped_message_generation"]

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
        "/model — show or set the Cursor model for the next message",
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

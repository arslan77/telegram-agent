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
# Never offered or sent from Telegram: the bot's own token, SSH/cloud keys, saved state.
SECRET_DIRS = {".ssh", ".gnupg", ".aws", ".config", ".kube", ".docker"}
SECRET_NAMES = {"id_rsa", "id_ed25519", "id_ecdsa", "credentials.json", "state.json", "env", ".netrc", ".npmrc", ".pypirc"}
SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
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
        "/cli cursor — use Cursor for the next message",
        "/cli claude — use Claude Code for the next message",
        "/new — start a fresh chat",
        "/cancel — stop the current run. Queued messages still run",
        "/mode agent — edit files and run commands",
        "/mode ask — read only",
        "/mode plan — plan only",
        "/effort — set Claude Code effort (buttons), or /effort high",
        "/model — pick the model for the current CLI (buttons), or /model name",
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

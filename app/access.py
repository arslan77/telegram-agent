"""Who may talk to the bot, and which folder a run uses."""

from __future__ import annotations

from pathlib import Path
import os

from app.constants import TELEGRAM_LIMIT, log


class ConfigError(Exception):
    """Bad token, allowlist, or file permissions. The process exits 78."""


def normalize_phone(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def parse_allowlist(raw: str) -> set[str]:
    found: set[str] = set()
    for part in raw.replace(";", ",").split(","):
        digits = normalize_phone(part)
        if not digits:
            continue
        if len(digits) < 8 or len(digits) > 15:
            raise ConfigError(f"allowlist phone has {len(digits)} digits; use the full international number")
        found.add(digits)
    if not found:
        raise ConfigError("TELEGRAM_ALLOWED_PHONES is empty")
    return found


def mask_phone(digits: str) -> str:
    if len(digits) <= 4:
        return "••••"
    return digits[:3] + "•" * (len(digits) - 5) + digits[-2:]


def parse_command(text: str) -> tuple[str | None, str]:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None, stripped
    head, _, rest = stripped.partition(" ")
    return head.split("@", 1)[0].lower(), rest.strip()


def is_cloud_handoff(text: str) -> bool:
    return text.lstrip().startswith("&")


def split_text(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    body = text.strip() or "Finished, with no text."
    parts: list[str] = []
    while body:
        if len(body) <= limit:
            parts.append(body)
            break
        cut = body.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(body[:cut].rstrip())
        body = body[cut:].lstrip("\n")
    return parts


def redact(text: str, secret: str) -> str:
    if secret and secret in text:
        return text.replace(secret, "***")
    return text


def resolve_workspace(raw: str, home: Path) -> Path:
    home_real = home.resolve()
    text = raw.strip()
    if not text or text == "~":
        path = home_real
    elif text.startswith("~/"):
        path = home_real / text[2:]
    else:
        path = Path(text)
        if not path.is_absolute():
            path = home_real / path
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError("That folder does not exist.") from exc
    if not resolved.is_dir():
        raise ValueError("That path is not a folder.")
    if os.path.commonpath([str(resolved), str(home_real)]) != str(home_real):
        raise ValueError("That folder is outside your home directory.")
    return resolved


def contact_phone(message: dict) -> str | None:
    """Return the sender's own phone digits, or None if this is not their contact."""
    contact = message.get("contact")
    sender = message.get("from") or {}
    if not isinstance(contact, dict) or sender.get("is_bot"):
        return None
    if contact.get("user_id") != sender.get("id"):
        return None
    phone = normalize_phone(str(contact.get("phone_number") or ""))
    if len(phone) < 8 or len(phone) > 15:
        return None
    return phone

def cursor_model() -> str:
    return os.environ.get("CURSOR_MODEL", "").strip() or "auto"


def normalize_model(raw: str) -> str:
    name = raw.strip()
    if not name or any(ch.isspace() for ch in name) or len(name) > 80:
        raise ValueError("Use /model name, for example /model grok-4.7-high. /model auto uses the CLI default.")
    if not all(ch.isalnum() or ch in "._-" for ch in name):
        raise ValueError("Use /model name, for example /model grok-4.7-high. /model auto uses the CLI default.")
    return name


EFFORT_LEVELS = ["default", "low", "medium", "high", "xhigh", "max"]
CLAUDE_MODELS = ["default", "fable", "opus", "sonnet", "haiku"]


def claude_model() -> str:
    return os.environ.get("CLAUDE_MODEL", "").strip() or "default"


def claude_model_args(name: str) -> list[str]:
    chosen = (name or "").strip()
    if not chosen or chosen == "default":
        return []
    return ["--model", chosen]


def effort_args(level: str) -> list[str]:
    chosen = (level or "").strip()
    return ["--effort", chosen] if chosen in EFFORT_LEVELS[1:] else []


def parse_model_list(text: str) -> list[tuple[str, str]]:
    """Read `id - Label` lines from `agent --list-models`."""
    found: list[tuple[str, str]] = []
    for line in text.splitlines():
        ident, sep, label = line.partition(" - ")
        ident = ident.strip()
        if sep and ident and " " not in ident:
            found.append((ident, "".join(ch for ch in label if ch.isprintable()).strip()))
    return found


def model_family(ident: str) -> str:
    return ident.removeprefix("cursor-").split("-")[0]


def model_args(name: str) -> list[str]:
    chosen = (name or "").strip()
    if not chosen or chosen == "auto":
        return []
    return ["--model", chosen]

def find_claude() -> str:
    override = os.environ.get("CLAUDE_CLI", "").strip()
    if override:
        return override
    local = Path.home() / ".local" / "bin" / "claude"
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    raise ConfigError("Claude Code is not installed at ~/.local/bin/claude")


def find_agent() -> str:
    override = os.environ.get("CURSOR_AGENT", "").strip()
    if override:
        return override
    local = Path.home() / ".local" / "bin" / "agent"
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    raise ConfigError("Cursor CLI is not installed at ~/.local/bin/agent")


def default_workspace(home: Path) -> Path:
    configured = os.environ.get("CURSOR_WORKSPACE", "").strip()
    if configured:
        try:
            return resolve_workspace(configured, home)
        except ValueError as exc:
            log.warning("CURSOR_WORKSPACE ignored: %s", exc)
    return home.resolve()

def load_config() -> tuple[str, set[str]]:
    env_path = Path(os.environ.get("CURSOR_TELEGRAM_ENV", Path.home() / ".config" / "cursor-telegram" / "env"))
    if env_path.exists():
        mode = env_path.stat().st_mode
        if mode & 0o077:
            raise ConfigError(f"{env_path} is readable by someone other than your user")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    phones = os.environ.get("TELEGRAM_ALLOWED_PHONES", "").strip()
    if not token:
        raise ConfigError("TELEGRAM_BOT_TOKEN is missing")
    return token, parse_allowlist(phones)

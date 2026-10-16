#!/usr/bin/env bash
# Install the local Cursor CLI if needed, write the private env file, and start the user service.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="${HOME}/.local/bin:${PATH}"

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${PATH}"
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "uv is not on PATH. Install it from https://docs.astral.sh/uv/ and run this again." >&2
  exit 1
fi

uv sync --project "$ROOT"

if [[ ! -x "${HOME}/.local/bin/agent" ]]; then
  curl -fsSL https://cursor.com/install | bash
fi
if ! command -v agent >/dev/null 2>&1; then
  echo "Cursor CLI is not on PATH. Open a new shell, or add ~/.local/bin to PATH, then run this again." >&2
  exit 1
fi
agent --version

install -d -m 700 \
  "${HOME}/.config/cursor-telegram" \
  "${HOME}/.local/share/cursor-telegram" \
  "${HOME}/.config/systemd/user"

if [[ -f "${ROOT}/.env" ]]; then
  uv run --project "$ROOT" python - "$ROOT/.env" << 'PY'
import re
import sys
from pathlib import Path

source = Path(sys.argv[1])
values: dict[str, str] = {}
for raw in source.read_text().splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    values[key.strip()] = value.strip().strip("'").strip('"')

token = values.get("TELEGRAM_BOT_TOKEN", "").strip()
phones = values.get("TELEGRAM_ALLOWED_PHONES", "").strip()
if not token or not phones:
    sys.exit("Set both TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_PHONES in .env")

numbers = []
for part in re.split(r"[\s,;]+", phones):
    number = "".join(ch for ch in part if ch.isdigit())
    if not number:
        continue
    if not 8 <= len(number) <= 15:
        sys.exit(f"A phone number has {len(number)} digits. Use the full international number.")
    numbers.append(number)
if not numbers:
    sys.exit("TELEGRAM_ALLOWED_PHONES has no numbers")

lines = {
    "TELEGRAM_BOT_TOKEN": token,
    "TELEGRAM_ALLOWED_PHONES": ",".join(numbers),
}
workspace = values.get("CURSOR_WORKSPACE", "").strip()
model = values.get("CURSOR_MODEL", "").strip()
if workspace:
    lines["CURSOR_WORKSPACE"] = workspace
if model:
    lines["CURSOR_MODEL"] = model

safe = re.compile(r"[A-Za-z0-9_./:+@,-]+")
for key, value in lines.items():
    if "\n" in value or "\r" in value or not safe.fullmatch(value):
        sys.exit(f"{key} contains a character the env file will not store")

dest = Path.home() / ".config" / "cursor-telegram" / "env"
dest.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
dest.write_text("".join(f"{key}={value}\n" for key, value in lines.items()))
dest.chmod(0o600)
print(f"TELEGRAM_ENV=written numbers={len(numbers)}")
PY
elif [[ ! -f "${HOME}/.config/cursor-telegram/env" ]]; then
  echo "Copy .env.example to .env, fill in the token and phone numbers, then run this again." >&2
  exit 1
fi

UV="$(command -v uv)"
sed \
  -e "s|@ROOT@|${ROOT}|g" \
  -e "s|@UV@|${UV}|g" \
  "${ROOT}/systemd/cursor-telegram.service" \
  > "${HOME}/.config/systemd/user/cursor-telegram.service"

systemctl --user daemon-reload
loginctl enable-linger "${USER}" >/dev/null 2>&1 || true
systemctl --user enable --now cursor-telegram.service
systemctl --user restart cursor-telegram.service
sleep 1
systemctl --user --no-pager --lines=20 status cursor-telegram.service || true
echo "Bridge is installed from ${ROOT}"

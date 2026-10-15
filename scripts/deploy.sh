#!/usr/bin/env bash
# Install the Cursor CLI on the Alienware and the Telegram bridge that drives it.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
for env_file in "$ROOT/.env" "$ROOT/../../.env"; do
  if [[ -f "$env_file" ]]; then
    # shellcheck disable=SC1090
    set -a && source "$env_file" && set +a
    break
  fi
done
HOST="${SSH_HOST:-192.168.1.121}"
USER_NAME="${SSH_USER:-arslanarshad}"
REMOTE="${USER_NAME}@${HOST}"
DEST="/home/${USER_NAME}/alienware-lab/experiments/cursor-telegram"

rsync -az \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude 'state.json' \
  "$ROOT/" "$REMOTE:$DEST/"

ssh -o BatchMode=yes "$REMOTE" "bash -s -- '$DEST'" << 'EOF'
set -euo pipefail
DEST="$1"
install -d -m 700 \
  "$HOME/.config/cursor-telegram" \
  "$HOME/.local/share/cursor-telegram" \
  "$HOME/.config/systemd/user"
if [[ ! -x "$HOME/.local/bin/agent" ]]; then
  curl -fsSL https://cursor.com/install | bash
fi
export PATH="$HOME/.local/bin:$PATH"
agent --version
cp "$DEST/systemd/cursor-telegram.service" "$HOME/.config/systemd/user/cursor-telegram.service"
systemctl --user daemon-reload
loginctl enable-linger "$USER" >/dev/null 2>&1 || sudo loginctl enable-linger "$USER"
rm -f /tmp/cursor-agent-status.json /tmp/cursor-agent-status.err
if timeout 30 agent status --format json >/tmp/cursor-agent-status.json 2>/tmp/cursor-agent-status.err; then
  echo "CURSOR_AUTH=signed-in"
  python3 - << 'PY'
import json
from pathlib import Path
raw = Path("/tmp/cursor-agent-status.json").read_text()
try:
    payload = json.loads(raw)
except json.JSONDecodeError:
    print("CURSOR_ACCOUNT=signed-in")
else:
    found = ""
    if isinstance(payload, dict):
        for key in ("email", "userEmail"):
            if isinstance(payload.get(key), str):
                found = payload[key]
                break
        user = payload.get("user")
        if not found and isinstance(user, dict):
            for key in ("email", "name"):
                if isinstance(user.get(key), str):
                    found = user[key]
                    break
    print("CURSOR_ACCOUNT=" + (found or "signed-in"))
PY
else
  echo "CURSOR_AUTH=signed-out"
  python3 - << 'PY'
from pathlib import Path
err = Path("/tmp/cursor-agent-status.err").read_text(errors="replace").strip().splitlines()
print("CURSOR_STATUS_ERR=" + (err[-1][:200] if err else "no status output"))
PY
fi
rm -f /tmp/cursor-agent-status.json /tmp/cursor-agent-status.err
EOF

python3 - "$REMOTE" "$USER_NAME" << 'PY'
import os, re, subprocess, sys, tempfile

remote, user = sys.argv[1], sys.argv[2]
token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
phones = os.environ.get("TELEGRAM_ALLOWED_PHONES", "").strip()
workspace = os.environ.get("CURSOR_WORKSPACE", "").strip() or f"/home/{user}/alienware-lab"
model = os.environ.get("CURSOR_MODEL", "").strip()

if not token and not phones:
    print("TELEGRAM_ENV=missing")
    sys.exit(0)
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
    "CURSOR_WORKSPACE": workspace,
}
if model:
    lines["CURSOR_MODEL"] = model
safe = re.compile(r"[A-Za-z0-9_./:+@,-]+")
for key, value in lines.items():
    if "\n" in value or "\r" in value or not safe.fullmatch(value):
        sys.exit(f"{key} contains a character the env file will not store")

fd, name = tempfile.mkstemp(prefix="cursor-telegram-", text=True)
os.chmod(name, 0o600)
try:
    with os.fdopen(fd, "w") as handle:
        for key, value in lines.items():
            handle.write(f"{key}={value}\n")
    subprocess.run(
        ["ssh", "-o", "BatchMode=yes", remote, "install -d -m 700 ~/.config/cursor-telegram"],
        check=True,
    )
    subprocess.run(["scp", "-q", name, f"{remote}:.config/cursor-telegram/env"], check=True)
    subprocess.run(
        ["ssh", "-o", "BatchMode=yes", remote, "chmod 600 ~/.config/cursor-telegram/env"],
        check=True,
    )
finally:
    os.remove(name)
print(f"TELEGRAM_ENV=written numbers={len(numbers)}")
PY

ssh -o BatchMode=yes "$REMOTE" "bash -s" << 'EOF'
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
if [[ -f "$HOME/.config/cursor-telegram/env" ]]; then
  systemctl --user enable --now cursor-telegram.service
  systemctl --user restart cursor-telegram.service
  sleep 1
  systemctl --user --no-pager --lines=30 status cursor-telegram.service || true
else
  echo "TELEGRAM_SERVICE=waiting-for-env"
fi
if ! timeout 20 agent status --format json >/dev/null 2>&1; then
  mkdir -p "$HOME/.local/share/cursor-telegram"
  if ! pgrep -u "$USER" -f "$HOME/.local/bin/agent login" >/dev/null 2>&1; then
    nohup env NO_OPEN_BROWSER=1 "$HOME/.local/bin/agent" login \
      >"$HOME/.local/share/cursor-telegram/login.out" 2>&1 &
    sleep 3
  fi
  python3 - << 'PY'
from pathlib import Path
import re
text = Path.home().joinpath(".local/share/cursor-telegram/login.out").read_text(errors="replace")
urls = re.findall(r"https://\S+", text)
print("LOGIN_URL=" + (urls[0] if urls else ""))
if not urls:
    tail = " | ".join(line.strip() for line in text.splitlines()[-8:] if line.strip())
    print("LOGIN_LOG=" + tail[:500])
PY
else
  echo "LOGIN_URL="
fi
EOF

echo "Deployed to ${REMOTE}:${DEST}"

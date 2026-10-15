#!/usr/bin/env bash
# Sign the Alienware Cursor CLI into your account. Open the URL it prints.
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

ssh -o BatchMode=yes "$REMOTE" "bash -s" << 'EOF'
python3 - << 'PY'
import os, signal
from pathlib import Path
target = str(Path.home() / ".local/bin/agent") + " login"
skip = {os.getpid(), os.getppid()}
proc = Path("/proc")
if proc.is_dir():
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) in skip:
            continue
        try:
            cmd = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace")
        except OSError:
            continue
        if target in cmd:
            os.kill(int(entry.name), signal.SIGTERM)
PY
EOF

echo "Approve the login in your browser. Leave this command running until it says you are signed in."
ssh -t -o BatchMode=yes "$REMOTE" 'exec env NO_OPEN_BROWSER=1 "$HOME/.local/bin/agent" login'

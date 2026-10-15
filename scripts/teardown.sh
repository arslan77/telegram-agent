#!/usr/bin/env bash
# Stop the Telegram bridge.
#   ./teardown.sh           stop the service
#   ./teardown.sh --purge   also delete the host folder, allowlist, and bot state
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
PURGE=0
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

ssh -o BatchMode=yes "$REMOTE" "bash -s" << EOF
set -euo pipefail
systemctl --user disable --now cursor-telegram.service 2>/dev/null || true
pkill -u "\$USER" -f "\$HOME/.local/bin/agent login" >/dev/null 2>&1 || true
if [[ "$PURGE" == 1 ]]; then
  rm -rf "$DEST" \
    "\$HOME/.config/cursor-telegram" \
    "\$HOME/.local/share/cursor-telegram"
  rm -f "\$HOME/.config/systemd/user/cursor-telegram.service"
  systemctl --user daemon-reload
fi
EOF

if [[ "$PURGE" == 1 ]]; then
  echo "Bridge removed. The Cursor CLI and its login are still on the Alienware."
else
  echo "Bridge stopped."
fi

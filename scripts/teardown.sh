#!/usr/bin/env bash
# Stop the Telegram bridge on this computer.
#   ./scripts/teardown.sh           stop the service
#   ./scripts/teardown.sh --purge   also delete the allowlist, token, and bot state
set -euo pipefail
PURGE=0
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

systemctl --user disable --now cursor-telegram.service 2>/dev/null || true
if [[ "$PURGE" == 1 ]]; then
  rm -rf \
    "${HOME}/.config/cursor-telegram" \
    "${HOME}/.local/share/cursor-telegram"
  rm -f "${HOME}/.config/systemd/user/cursor-telegram.service"
  systemctl --user daemon-reload
  echo "Bridge stopped. The token, allowlist, and bot state are removed. The Cursor CLI login is unchanged."
else
  echo "Bridge stopped."
fi

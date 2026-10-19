#!/usr/bin/env bash
# Print the address of the setup page. It listens on 127.0.0.1 of the machine running the bot.
set -euo pipefail
port="${CURSOR_TELEGRAM_UI_PORT:-8787}"
keyfile="${HOME}/.config/cursor-telegram/ui-key"
if [ ! -s "$keyfile" ]; then
  echo "No key yet. Start the bot once: systemctl --user restart cursor-telegram.service" >&2
  exit 1
fi
key="$(cat "$keyfile")"
echo "On this machine, open:"
echo "  http://127.0.0.1:${port}/?key=${key}"
echo
echo "From another computer, tunnel first, then open the same address there:"
echo "  ssh -N -L ${port}:127.0.0.1:${port} ${USER}@$(hostname -f 2>/dev/null || hostname)"

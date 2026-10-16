#!/usr/bin/env bash
# Sign the local Cursor CLI into your Cursor account. Open the URL it prints.
set -euo pipefail
export PATH="${HOME}/.local/bin:${PATH}"
if ! command -v agent >/dev/null 2>&1; then
  echo "Cursor CLI is not installed. Run scripts/install.sh first." >&2
  exit 1
fi
echo "Approve the login in your browser. Leave this command running until it says you are signed in."
exec env NO_OPEN_BROWSER=1 agent login

#!/usr/bin/env bash
# Update to the latest version from GitHub and restart the service.
# Keeps the token, the allowlist (~/.config/cursor-telegram/env) and the bot state.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "There are local changes in $ROOT. Commit or stash them, then run this again." >&2
  exit 1
fi

before="$(git rev-parse --short HEAD)"
git pull --ff-only
after="$(git rev-parse --short HEAD)"
if [[ "$before" == "$after" ]]; then
  echo "Already at the latest version ($after). Restarting anyway."
else
  echo "Updated $before -> $after:"
  git log --oneline "${before}..${after}"
fi

# install.sh is safe to re-run: it syncs Python, re-renders the unit, and restarts.
# It leaves the env file alone when there is no .env in the checkout.
exec "$ROOT/scripts/install.sh"

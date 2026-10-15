# Cursor over Telegram

The Cursor CLI runs on the Alienware, signed in with your Cursor account. A Telegram bot on the same machine sends each message into a local `agent -p` session. Tool calls (files, shell, git) happen here. A message that starts with `&` is rejected, so the chat cannot be handed to a Cursor cloud agent.

Telegram is the remote control because the bot only makes outbound calls to `api.telegram.org`. The Alienware does not open a port. The desktop app is not installed.

The default folder is `~/alienware-lab`. `/workspace` can move it anywhere inside your home directory.

## Who can use it

`TELEGRAM_ALLOWED_PHONES` in the Mac `.env` is the list. Use full international numbers, comma-separated.

The bot answers in a private chat only. The sender has to tap **Share my phone number**. Telegram attaches that contact to their account; a typed number or someone else's contact is ignored. When the number matches the list, that Telegram user stays allowed until the number is removed and the service is restarted. Anyone else gets a refusal and no Cursor session.

The allowlisted person can run commands as `arslanarshad`. That is the remote control. The token and the phone list live in `~/.config/cursor-telegram/env` on the Alienware, mode `600`. They are not in git.

## Deploy

1. In Telegram, open `@BotFather`, send `/newbot`, and copy the token.
2. Add the token and your number to `.env` in this directory (or the Alienware checkout `.env` when this folder lives inside that repo):

```bash
TELEGRAM_BOT_TOKEN=123456:replace-me
TELEGRAM_ALLOWED_PHONES=+971501234567
```

3. Install and start it:

```bash
./experiments/cursor-telegram/scripts/deploy.sh
./experiments/cursor-telegram/scripts/login.sh
```

`login.sh` prints a URL. Open it on this Mac and approve the Alienware. Leave the command running until it finishes.

4. Open the bot, tap **Share my phone number**, then send a task.

`/status` shows the account, folder, and chat. `/new` starts a fresh chat. `/cancel` stops the current run. `/mode ask` is read-only, `/mode plan` plans, `/mode agent` edits and runs commands.

Changing the allowlist means updating `.env` and running `deploy.sh` again.

## Stop

```bash
./experiments/cursor-telegram/scripts/teardown.sh
./experiments/cursor-telegram/scripts/teardown.sh --purge
```

`--purge` removes the bridge, the allowlist, and the bot state. It leaves the Cursor CLI and its login in place.

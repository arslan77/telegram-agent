# Telegram Agent

A private Telegram bot that runs a coding agent on your own computer. You message the bot. [Cursor](https://cursor.com/docs/cli/overview) or [Claude Code](https://docs.claude.com/en/docs/claude-code/overview) edits files, runs commands, and answers in that same chat. The bot only makes outbound calls to Telegram, so the computer does not open a port.

![A Telegram message runs Cursor or Claude Code on your computer, and the reply stays in the same chat](docs/architecture.svg)

## What you get

- **Two agents, one chat.** `/cli cursor` or `/cli claude` picks the tool for the next message. Each tool keeps its own session, so switching never loses the other conversation.
- **A live reply.** One Telegram message is edited as the work happens: what the agent is doing, the tools it ran, then the answer.
- **Pick models with buttons.** `/model` shows tappable buttons for the active tool. `/effort` sets how hard Claude Code thinks.
- **A setup page** for switching tools, choosing models, and signing each one in, without typing commands.
- **Files in both directions.** Send a photo or voice note and the agent can read it. Files the agent makes come back in the chat. `/files` lists the folder.
- **A queue that survives restarts.** A message sent while the agent is busy waits its turn and continues after a restart.
- **Locked to you.** Only allowlisted phone numbers get through, in private chat only.

![The /model buttons: Cursor model families, the models in one family, and the Claude Code aliases](docs/model-picker.svg)

The default folder is your home directory. `/workspace` can move it anywhere inside your home directory. A message that starts with `&` is rejected, so a chat cannot be handed to a Cursor cloud agent.

## Dependencies

| | |
|---|---|
| Cursor CLI | The `agent` command, signed in to a Cursor account. Required, since Cursor is the default. |
| Claude Code | The `claude` command at `~/.local/bin/claude`, signed in. Optional. Needed only for `/cli claude`. |
| Telegram bot | A token from [@BotFather](https://t.me/BotFather), and the phone numbers allowed to use it. |
| git and uv | `git` to clone and upgrade. uv installs Python and runs the bot. The bot itself uses the Python standard library only, with no third-party packages. |
| Linux systemd | Optional. `scripts/install.sh` keeps the bot running as a user service. You can also run it in the foreground. |

## Install

Linux with systemd, in about five minutes. On macOS, or without systemd, skip `install.sh` and see [Run in the foreground](#run-in-the-foreground).

```bash
git clone https://github.com/arslan77/telegram-agent.git
cd telegram-agent
cp .env.example .env    # then fill in TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_PHONES
./scripts/install.sh
./scripts/login.sh      # signs Cursor in. Open the link it prints.
```

Then open your bot in Telegram, tap **Share my phone number**, and send a task.

What each step needs:

1. **A bot token.** In Telegram, open [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the token into `.env`.

2. **Your phone number.** `TELEGRAM_ALLOWED_PHONES` takes full international numbers, comma-separated. Only these numbers can use the bot. Optional settings in `.env`: `CURSOR_WORKSPACE` (a folder inside your home directory), `CURSOR_MODEL` and `CLAUDE_MODEL` (the starting model for each tool).

3. **`install.sh`** installs [uv](https://docs.astral.sh/uv/) and the Cursor CLI if they are missing, writes `~/.config/cursor-telegram/env` with mode `600`, installs a systemd user service, and starts it. The token and phone list never enter the repo. It is safe to run again.

4. **`login.sh`** runs the Cursor CLI's own sign-in. Leave it running until it says you are signed in. To use Claude Code too, install it and run `claude auth login`, or sign in later from the [setup page](#setup-page).

Python 3.11 or newer is required. uv installs it for you.

## Upgrade

```bash
./scripts/upgrade.sh
```

This pulls the latest version from GitHub, re-syncs Python, and restarts the service. Your token, allowlist and bot state are kept. A message in progress is interrupted and continues after the restart. If you changed files in the checkout, the script stops and asks you to commit or stash them first.

## Setup page

![The setup page: tool switch, sign-in with the vendor link, models, effort, and mode](docs/setup-page.png)

The bot serves a small page on `127.0.0.1:8787` of the machine it runs on. It switches between Cursor and Claude Code, sets the model, Claude effort and mode, and signs each tool in. The Sign in button starts the vendor's own login, shows the link it prints, and has a box for a code if the login asks for one. Passwords are never typed into the page.

```bash
./scripts/ui.sh
```

This prints the address with its key and the SSH tunnel command for opening it from another computer. The key lives in `~/.config/cursor-telegram/ui-key`. The page refuses other host names, needs the key on every request, and never listens on the network. Set `CURSOR_TELEGRAM_UI=off` to disable it or `CURSOR_TELEGRAM_UI_PORT` to move it. Changes apply to the next message. Model, effort and mode are written to every existing chat.

## Commands

| | |
|---|---|
| `/status` | Account, folder, tool, model, effort, and chat. |
| `/cli cursor`, `/cli claude` | Choose the tool for the next message. A run already in progress finishes on the tool it started with. |
| `/model` | Buttons for the active tool. `/model name` sets one directly. `/model auto` (Cursor) or `/model default` (Claude) uses the tool's default. |
| `/effort` | Claude Code effort: low, medium, high, xhigh, max, or default. |
| `/mode agent`, `ask`, `plan` | Edit and run, read-only, or plan only. |
| `/workspace` | Show or set the folder. |
| `/new` | Start a fresh chat for the active tool. |
| `/cancel` | Stop the current run. Queued messages still run. |
| `/clear` | Delete the chat messages in Telegram. The session stays. |
| `/files` | Newest files in the folder, including photos and audio. Tap one to get it. |

Changing the allowlist means updating `.env` and running `scripts/install.sh` again.

## Security

The bot answers in a private chat only. Telegram attaches the shared contact to the sender's account. A typed number or someone else's contact is ignored. When the number is on the list, that Telegram user stays allowed until the number is removed and the service is restarted. Anyone else gets a refusal and no agent session.

The bot runs as the user who starts it. In `agent` mode the agent has that user's full power on purpose: Claude Code runs with `bypassPermissions` and Cursor with `--force` and no sandbox. The chosen folder is only where it starts, not a boundary, and on a host with passwordless sudo that includes root.

The bot's own guardrails are narrower:

- Only allowlisted phones can reach it.
- `/files` never offers keys, `.ssh`, `.config`, or the bot's state.
- The Telegram token and phone list are removed from the agent's environment.
- The setup page listens on `127.0.0.1` only and needs its key.

Use `/mode ask` or `/mode plan` when you want the agent to stop editing.

## Run in the foreground

Any OS with the Cursor CLI, including macOS. No systemd needed. Fill in `.env`, then:

```bash
uv sync
set -a && source .env && set +a
uv run python bot.py
```

Ctrl-C stops it. On a machine where `install.sh` already ran, source `~/.config/cursor-telegram/env` instead of `.env`.

## Tests

```bash
uv run python -m unittest discover -s tests -t .
```

## Stop

```bash
./scripts/teardown.sh
./scripts/teardown.sh --purge
```

`--purge` removes the service, the token, the allowlist, and the bot state. It leaves the Cursor and Claude Code CLIs and their logins in place.

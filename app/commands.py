"""Telegram commands and button actions.

Add a command with @command. Add a button with @callback or @callback_prefix.
The bridge dispatches to this table, so a new action does not edit the update loop.
"""

from __future__ import annotations

from collections.abc import Callable

from app.access import is_cloud_handoff, parse_command, resolve_workspace
from app.constants import HELP

CommandHandler = Callable[..., None]
CallbackHandler = Callable[..., None]

COMMANDS: dict[str, CommandHandler] = {}
CALLBACKS: dict[str, CallbackHandler] = {}
CALLBACK_PREFIXES: list[tuple[str, CallbackHandler]] = []


def command(*names: str):
    def decorate(fn: CommandHandler) -> CommandHandler:
        for name in names:
            COMMANDS[name] = fn
        return fn

    return decorate


def callback(*names: str):
    def decorate(fn: CallbackHandler) -> CallbackHandler:
        for name in names:
            CALLBACKS[name] = fn
        return fn

    return decorate


def callback_prefix(*prefixes: str):
    def decorate(fn: CallbackHandler) -> CallbackHandler:
        for prefix in prefixes:
            CALLBACK_PREFIXES.append((prefix, fn))
        return fn

    return decorate


def dispatch_text(bridge, chat_id: int, user_id: int, text: str, message_id: int | None) -> bool:
    """Handle a command or a rejected cloud handoff. Return False when text is a prompt."""
    name, rest = parse_command(text)
    if name is None:
        if is_cloud_handoff(text):
            bridge.say(user_id, chat_id, "Cloud handoff is off. This bot only runs Cursor on this machine.")
            return True
        return False
    handler = COMMANDS.get(name)
    if handler is None:
        bridge.say(user_id, chat_id, "Unknown command.\n\n" + HELP)
        return True
    handler(bridge, chat_id, user_id, rest, message_id)
    return True


def dispatch_callback(bridge, user_id: int, chat_id: int, data: str) -> None:
    handler = CALLBACKS.get(data)
    if handler is not None:
        handler(bridge, user_id, chat_id, data)
        return
    for prefix, prefixed in CALLBACK_PREFIXES:
        if data.startswith(prefix):
            prefixed(bridge, user_id, chat_id, data[len(prefix) :])
            return


@command("/start", "/help")
def help_command(bridge, chat_id: int, user_id: int, rest: str, message_id: int | None) -> None:
    bridge.say(user_id, chat_id, HELP)


@command("/clear")
def clear_command(bridge, chat_id: int, user_id: int, rest: str, message_id: int | None) -> None:
    bridge.clear_chat(chat_id, user_id, message_id)


@command("/cancel")
def cancel_command(bridge, chat_id: int, user_id: int, rest: str, message_id: int | None) -> None:
    if bridge.runner.busy():
        bridge.runner.stop()
        waiting = bridge.queued()
        if waiting == 1:
            note = "Stopping the current run. 1 message is still queued."
        elif waiting:
            note = f"Stopping the current run. {waiting} messages are still queued."
        else:
            note = "Stopping the current run."
        bridge.say(user_id, chat_id, note)
        return
    bridge.say(user_id, chat_id, "Nothing is running.")


@command("/status")
def status_command(bridge, chat_id: int, user_id: int, rest: str, message_id: int | None) -> None:
    bridge.say(user_id, chat_id, bridge.status_text(user_id))


@command("/new")
def new_command(bridge, chat_id: int, user_id: int, rest: str, message_id: int | None) -> None:
    if bridge.runner.busy():
        bridge.drop_session = True
    bridge.chat(user_id)["session_id"] = ""
    bridge.state.save()
    bridge.say(user_id, chat_id, "Next message starts a fresh chat.")


@command("/mode")
def mode_command(bridge, chat_id: int, user_id: int, rest: str, message_id: int | None) -> None:
    row = bridge.chat(user_id)
    if not rest:
        bridge.say(user_id, chat_id, f"Mode is {row['mode']}.")
        return
    mode = rest.split()[0].lower()
    if mode not in {"agent", "ask", "plan"}:
        bridge.say(user_id, chat_id, "Use /mode agent, /mode ask, or /mode plan.")
        return
    row["mode"] = mode
    bridge.state.save()
    bridge.say(user_id, chat_id, f"Mode is {mode}.")


@command("/workspace")
def workspace_command(bridge, chat_id: int, user_id: int, rest: str, message_id: int | None) -> None:
    row = bridge.chat(user_id)
    if not rest:
        bridge.say(user_id, chat_id, f"Folder is {row['workspace']}")
        return
    try:
        path = resolve_workspace(rest, bridge.home)
    except ValueError as exc:
        bridge.say(user_id, chat_id, str(exc))
        return
    if bridge.runner.busy():
        bridge.drop_session = True
    row["workspace"] = str(path)
    row["session_id"] = ""
    bridge.state.save()
    bridge.say(user_id, chat_id, f"Folder is {path}. The next message starts a fresh chat there.")


@command("/files")
def files_command(bridge, chat_id: int, user_id: int, rest: str, message_id: int | None) -> None:
    query = rest[5:].strip() if rest.lower().startswith("find ") else rest.strip()
    bridge.show_files(chat_id, user_id, query, 0)


@callback("status")
def status_button(bridge, user_id: int, chat_id: int, data: str) -> None:
    bridge.say(user_id, chat_id, bridge.status_text(user_id))


@callback("end")
def end_button(bridge, user_id: int, chat_id: int, data: str) -> None:
    if bridge.runner.busy():
        bridge.drop_session = True
    bridge.chat(user_id)["session_id"] = ""
    bridge.state.save()
    bridge.say(user_id, chat_id, "Next message starts a fresh chat.")


@callback("diff")
def diff_button(bridge, user_id: int, chat_id: int, data: str) -> None:
    bridge.send_diff(user_id, chat_id)


@callback_prefix("f:")
def file_button(bridge, user_id: int, chat_id: int, token: str) -> None:
    bridge.send_saved_file(user_id, chat_id, token)


@callback_prefix("fp:")
def file_page_button(bridge, user_id: int, chat_id: int, page_text: str) -> None:
    page = int(page_text) if page_text.isdigit() else 0
    row = bridge.chat(user_id)
    bridge.show_files(chat_id, user_id, str(row.get("file_query") or ""), page)

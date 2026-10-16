"""Queue, commands, and the loop that turns Telegram into Cursor runs."""

from __future__ import annotations

from pathlib import Path
from collections import deque
import json
import os
import queue
import secrets
import subprocess
import threading
import time

from app.access import ConfigError, contact_phone, cursor_model, default_workspace, is_cloud_handoff, mask_phone, parse_command, redact, resolve_workspace, split_text
from app.board import LiveBoard
from app.chatlog import message_ids_for_clear
from app.client import Telegram
from app.constants import ALLOWED_UPDATES, FILE_PAGE, HELP, MAX_DOWNLOAD, MAX_QUEUE, MAX_UPLOAD, REACTION_CANCELLED, REACTION_DONE, REACTION_ERROR, REACTION_QUEUED, REACTION_WORKING, REMOVE_KEYBOARD, RUN_TIMEOUT_SEC, SHARE, log
from app.events import consume_event
from app.files import attachment_from_message, blocked_file, changed_media, file_page, inbound_prompt, inside_workspace, list_sendable, media_index, media_kind, output_paths, safe_filename
from app.render import escape_html
from app.runner import Runner, stop_process
from app.state import State


class Prompt:
    def __init__(self, chat_id: int, user_id: int, text: str, message_id: int | None) -> None:
        self.chat_id = chat_id
        self.user_id = user_id
        self.text = text
        self.message_id = message_id


class Bridge:
    def __init__(self, telegram: Telegram, state: State, allow: set[str], agent: str, home: Path) -> None:
        self.telegram = telegram
        self.state = state
        self.allow = allow
        self.agent = agent
        self.home = home
        self.runner = Runner()
        self.pending: deque[Prompt] = deque()
        self.queue_lock = threading.Lock()
        self.active_draft_id: int | None = None
        self.drop_session = False
        self.default_workspace = default_workspace(home)

    def queued(self) -> int:
        with self.queue_lock:
            return len(self.pending)

    def chat(self, user_id: int) -> dict:
        key = str(user_id)
        chats = self.state.data.setdefault("chats", {})
        row = chats.get(key)
        if not isinstance(row, dict):
            row = {}
            chats[key] = row
        row.setdefault("workspace", str(self.default_workspace))
        row.setdefault("mode", "agent")
        row.setdefault("session_id", "")
        if not isinstance(row.get("message_ids"), list):
            row["message_ids"] = []
        return row

    def remember(self, user_id: int, message_id: int | None) -> None:
        if not isinstance(message_id, int):
            return
        with self.state.lock:
            row = self.chat(user_id)
            ids = row["message_ids"]
            if message_id not in ids:
                ids.append(message_id)
            if len(ids) > 800:
                del ids[:-800]
            self.state.save()

    def say(self, user_id: int, chat_id: int, text: str, reply_markup: dict | None = None) -> int | None:
        first = None
        for index, part in enumerate(split_text(text)):
            markup = reply_markup if index == 0 else None
            message_id = self.telegram.send_one(chat_id, part, markup)
            self.remember(user_id, message_id)
            if first is None:
                first = message_id
        return first

    def clear_chat(self, chat_id: int, user_id: int, command_id: int | None) -> None:
        with self.state.lock:
            ids = message_ids_for_clear(self.chat(user_id), command_id)
        self.telegram.delete_many(chat_id, ids)
        with self.state.lock:
            self.chat(user_id)["message_ids"] = []
            self.state.save()
        self.say(user_id, chat_id, "Chat cleared. The Cursor session is unchanged.")

    def verified_phone(self, user_id: int) -> str | None:
        phone = self.state.data.get("verified", {}).get(str(user_id))
        if not isinstance(phone, str):
            return None
        if phone not in self.allow:
            self.state.data.get("verified", {}).pop(str(user_id), None)
            self.state.save()
            return None
        return phone

    def run(self) -> None:
        self.telegram.call("deleteWebhook", {"drop_pending_updates": False}, timeout=30)
        self.telegram.call(
            "setMyCommands",
            {
                "commands": [
                    {"command": "status", "description": "Account, folder, and chat"},
                    {"command": "new", "description": "Start a fresh chat"},
                    {"command": "cancel", "description": "Stop the current run"},
                    {"command": "mode", "description": "agent, ask, or plan"},
                    {"command": "workspace", "description": "Show or set the folder"},
                    {"command": "clear", "description": "Delete chat messages, keep the Cursor session"},
                    {"command": "files", "description": "List or search files, including audio"},
                    {"command": "help", "description": "What this bot can do"},
                ]
            },
            timeout=30,
        )
        if not self.state.data.get("initialized"):
            self.drain()
        log.info("bridge up, %d allowlisted numbers", len(self.allow))
        while True:
            self.pump()
            timeout = 2 if self.runner.busy() or self.queued() else 25
            try:
                updates = self.telegram.call(
                    "getUpdates",
                    {
                        "offset": self.state.data.get("offset", 0),
                        "timeout": timeout,
                        "allowed_updates": ALLOWED_UPDATES,
                    },
                    timeout=timeout + 15,
                )
            except ConfigError:
                raise
            except RuntimeError as exc:
                log.warning("%s", exc)
                time.sleep(5)
                continue
            for update in updates.get("result") or []:
                self.accept(update)

    def drain(self) -> None:
        skipped = 0
        while True:
            updates = self.telegram.call(
                "getUpdates",
                {"offset": self.state.data.get("offset", 0), "timeout": 0, "allowed_updates": ALLOWED_UPDATES},
                timeout=30,
            )
            batch = updates.get("result") or []
            if not batch:
                break
            skipped += len(batch)
            last = max(int(update.get("update_id", 0)) for update in batch)
            self.state.data["offset"] = last + 1
            if len(batch) < 100:
                break
        self.state.data["initialized"] = True
        self.state.save()
        log.info("skipped %d pending updates from before startup", skipped)

    def on_callback(self, query: dict) -> None:
        query_id = query.get("id")
        if isinstance(query_id, str):
            try:
                self.telegram.call("answerCallbackQuery", {"callback_query_id": query_id}, timeout=15)
            except RuntimeError as exc:
                log.warning("callback answer failed: %s", exc)
        sender = query.get("from") or {}
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        user_id = sender.get("id")
        chat_id = chat.get("id")
        data = query.get("data") if isinstance(query.get("data"), str) else ""
        if not isinstance(user_id, int) or not isinstance(chat_id, int):
            return
        if chat.get("type") not in {None, "private"}:
            return
        if self.verified_phone(user_id) is None:
            return
        if data == "status":
            self.say(user_id, chat_id, self.status_text(user_id))
        elif data == "end":
            if self.runner.busy():
                self.drop_session = True
            self.chat(user_id)["session_id"] = ""
            self.state.save()
            self.say(user_id, chat_id, "Next message starts a fresh chat.")
        elif data == "diff":
            self.send_diff(user_id, chat_id)
        elif data.startswith("f:"):
            self.send_saved_file(user_id, chat_id, data[2:])
        elif data.startswith("fp:"):
            page_text = data[3:]
            page = int(page_text) if page_text.isdigit() else 0
            row = self.chat(user_id)
            self.show_files(chat_id, user_id, str(row.get("file_query") or ""), page)

    def send_diff(self, user_id: int, chat_id: int) -> None:
        raw = str(self.chat(user_id).get("workspace") or "")
        try:
            folder = resolve_workspace(raw, self.home)
        except ValueError as exc:
            self.say(user_id, chat_id, str(exc))
            return
        excludes = [".", ":(exclude).env", ":(exclude)*.env"]
        try:
            stat = subprocess.run(
                ["git", "-C", str(folder), "diff", "--stat", "--", *excludes],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except OSError:
            self.say(user_id, chat_id, "git is not available on this machine.")
            return
        if stat.returncode != 0:
            self.say(user_id, chat_id, "This folder is not a git repository.")
            return
        diff = subprocess.run(
            ["git", "-C", str(folder), "diff", "--", *excludes],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        summary = (stat.stdout or "").strip()
        changes = (diff.stdout or "").strip()
        if not summary and not changes:
            self.say(user_id, chat_id, "No uncommitted changes.")
            return
        lines = f"{summary}\n\n{changes}".splitlines()
        if len(lines) > 80:
            text = "\n".join(lines[:80]) + f"\n… +{len(lines) - 80} lines"
        else:
            text = "\n".join(lines)
        shown = f"<pre><code>{escape_html(text[:3500])}</code></pre>"
        try:
            message_id = self.telegram.send_one(chat_id, shown, html=True)
        except RuntimeError:
            message_id = self.telegram.send_one(chat_id, text[:3500])
        if message_id is not None:
            self.remember(user_id, message_id)

    def accept(self, update: dict) -> None:
        update_id = int(update.get("update_id", 0))
        self.state.data["offset"] = update_id + 1
        self.state.save()
        stopped = update.get("stopped_message_generation")
        if isinstance(stopped, dict):
            self.on_generation_stopped(stopped)
            return
        query = update.get("callback_query")
        if isinstance(query, dict):
            self.on_callback(query)
            return
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat") or {}
        if chat.get("type") != "private":
            return
        sender = message.get("from") or {}
        if sender.get("is_bot"):
            return
        user_id = sender.get("id")
        chat_id = chat.get("id")
        if not isinstance(user_id, int) or not isinstance(chat_id, int):
            return
        log.info("update %s user %s", update_id, user_id)
        incoming = message.get("message_id")
        if isinstance(incoming, int):
            self.remember(user_id, incoming)
        if "contact" in message:
            self.on_contact(chat_id, user_id, message)
            return
        if self.verified_phone(user_id) is None:
            self.say(
                user_id,
                chat_id,
                "Share your phone number with the button. Only numbers on the allowlist can use this bot.",
                SHARE,
            )
            return
        attachment = attachment_from_message(message)
        if attachment is not None:
            self.on_file(chat_id, user_id, attachment, incoming if isinstance(incoming, int) else None)
            return
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            self.say(user_id, chat_id, "Send a text message, photo, file, or audio.")
            return
        self.on_text(chat_id, user_id, text, incoming if isinstance(incoming, int) else None)

    def on_contact(self, chat_id: int, user_id: int, message: dict) -> None:
        phone = contact_phone(message)
        if phone is None:
            self.say(user_id, chat_id, "Use the button to share your own number.", SHARE)
            return
        if phone not in self.allow:
            log.info("rejected user %s phone %s", user_id, mask_phone(phone))
            self.say(
                user_id,
                chat_id,
                f"{mask_phone(phone)} is not on the allowlist.",
                SHARE,
            )
            return
        self.state.data.setdefault("verified", {})[str(user_id)] = phone
        self.state.save()
        log.info("verified user %s phone %s", user_id, mask_phone(phone))
        self.say(
            user_id,
            chat_id,
            "Number accepted. Messages now go to Cursor on this machine.\n\n" + HELP,
            REMOVE_KEYBOARD,
        )

    def on_file(self, chat_id: int, user_id: int, attachment: dict, message_id: int | None = None) -> None:
        size = attachment.get("size") or 0
        if isinstance(size, int) and size > MAX_DOWNLOAD:
            self.say(user_id, chat_id, "Telegram only lets the bot download files up to 20 MB.")
            return
        try:
            folder = resolve_workspace(str(self.chat(user_id).get("workspace") or ""), self.home)
        except ValueError as exc:
            self.say(user_id, chat_id, str(exc))
            return
        dest = folder / ".cursor-telegram" / "inbound" / f"{int(time.time())}-{safe_filename(str(attachment.get('name') or 'file'))}"
        try:
            self.telegram.download(str(attachment["file_id"]), dest)
        except RuntimeError as exc:
            log.warning("download failed: %s", exc)
            self.say(user_id, chat_id, "Could not download that file.")
            return
        prompt = inbound_prompt(dest, str(attachment.get("caption") or ""))
        self.submit(chat_id, user_id, prompt, message_id)

    def show_files(self, chat_id: int, user_id: int, query: str, page: int) -> None:
        try:
            folder = resolve_workspace(str(self.chat(user_id).get("workspace") or ""), self.home)
        except ValueError as exc:
            self.say(user_id, chat_id, str(exc))
            return
        row = self.chat(user_id)
        row["file_query"] = query
        paths = list_sendable(folder, query)
        chunk, text = file_page(paths, page, folder)
        if not chunk:
            self.say(user_id, chat_id, text)
            return
        rows = []
        for path in chunk:
            token = self._file_token(user_id, path)
            label = path.name
            if media_kind(path) in {"audio", "voice"}:
                label = "🎧 " + label
            elif media_kind(path) in {"photo", "animation"}:
                label = "🖼 " + label
            rows.append([{"text": label[:40], "callback_data": f"f:{token}"}])
        if (page + 1) * FILE_PAGE < len(paths):
            rows.append([{"text": "More", "callback_data": f"fp:{page + 1}"}])
        self.say(user_id, chat_id, text, {"inline_keyboard": rows})

    def _file_token(self, user_id: int, path: Path) -> str:
        token = secrets.token_hex(4)
        bucket = self.state.data.setdefault("file_tokens", {})
        if isinstance(bucket, dict) and len(bucket) > 300:
            for key in list(bucket)[:80]:
                bucket.pop(key, None)
        if not isinstance(bucket, dict):
            bucket = {}
            self.state.data["file_tokens"] = bucket
        bucket[token] = {"user": user_id, "path": str(path)}
        self.state.save()
        return token

    def send_saved_file(self, user_id: int, chat_id: int, token: str) -> None:
        bucket = self.state.data.get("file_tokens")
        row = bucket.get(token) if isinstance(bucket, dict) else None
        if not isinstance(row, dict) or row.get("user") != user_id or not isinstance(row.get("path"), str):
            self.say(user_id, chat_id, "That file link expired. Send /files again.")
            return
        path = Path(row["path"])
        try:
            folder = resolve_workspace(str(self.chat(user_id).get("workspace") or ""), self.home)
            allowed = inside_workspace(folder, str(path))
        except ValueError as exc:
            self.say(user_id, chat_id, str(exc))
            return
        if allowed is None or not allowed.is_file():
            self.say(user_id, chat_id, "That file is no longer available.")
            return
        if not self.deliver_file(chat_id, user_id, allowed):
            self.say(user_id, chat_id, f"Could not send {allowed.name}.")

    def deliver_file(self, chat_id: int, user_id: int, path: Path) -> bool:
        try:
            if not path.is_file() or path.stat().st_size > MAX_UPLOAD or path.stat().st_size <= 0:
                return False
        except OSError:
            return False
        if blocked_file(path):
            return False
        kind = media_kind(path)
        method, field = {
            "photo": ("sendPhoto", "photo"),
            "animation": ("sendAnimation", "animation"),
            "video": ("sendVideo", "video"),
            "audio": ("sendAudio", "audio"),
            "voice": ("sendVoice", "voice"),
        }.get(kind, ("sendDocument", "document"))
        try:
            message_id = self.telegram.send_file(method, field, chat_id, path, caption=path.name)
        except RuntimeError as exc:
            log.warning("send file failed: %s", exc)
            if kind == "document":
                return False
            try:
                message_id = self.telegram.send_file("sendDocument", "document", chat_id, path, caption=path.name)
            except RuntimeError as second:
                log.warning("send document failed: %s", second)
                return False
        if message_id is not None:
            self.remember(user_id, message_id)
        return message_id is not None

    def on_text(self, chat_id: int, user_id: int, text: str, message_id: int | None = None) -> None:
        command, rest = parse_command(text)
        if command in {"/start", "/help"}:
            self.say(user_id, chat_id, HELP)
            return
        if command == "/clear":
            self.clear_chat(chat_id, user_id, message_id)
            return
        if command == "/cancel":
            if self.runner.busy():
                self.runner.stop()
                waiting = self.queued()
                if waiting == 1:
                    note = "Stopping the current run. 1 message is still queued."
                elif waiting:
                    note = f"Stopping the current run. {waiting} messages are still queued."
                else:
                    note = "Stopping the current run."
                self.say(user_id, chat_id, note)
            else:
                self.say(user_id, chat_id, "Nothing is running.")
            return
        if command == "/status":
            self.say(user_id, chat_id, self.status_text(user_id))
            return
        if command == "/new":
            if self.runner.busy():
                self.drop_session = True
            self.chat(user_id)["session_id"] = ""
            self.state.save()
            self.say(user_id, chat_id, "Next message starts a fresh chat.")
            return
        if command == "/mode":
            self.set_mode(chat_id, user_id, rest)
            return
        if command == "/workspace":
            self.set_workspace(chat_id, user_id, rest)
            return
        if command == "/files":
            query = rest[5:].strip() if rest.lower().startswith("find ") else rest.strip()
            self.show_files(chat_id, user_id, query, 0)
            return
        if command is not None:
            self.say(user_id, chat_id, "Unknown command.\n\n" + HELP)
            return
        if is_cloud_handoff(text):
            self.say(user_id, chat_id, "Cloud handoff is off. This bot only runs Cursor on this machine.")
            return
        self.submit(chat_id, user_id, text, message_id)

    def submit(self, chat_id: int, user_id: int, text: str, message_id: int | None) -> None:
        prompt = Prompt(chat_id, user_id, text, message_id)
        with self.queue_lock:
            if self.runner.busy():
                if len(self.pending) >= MAX_QUEUE:
                    full = True
                    queued = False
                else:
                    self.pending.append(prompt)
                    full = False
                    queued = True
            else:
                full = False
                queued = False
        if full:
            self.say(user_id, chat_id, f"Queue is full ({MAX_QUEUE}). Wait for a run to finish.")
            return
        if queued:
            self.telegram.react(chat_id, message_id, REACTION_QUEUED)
            return
        self._launch(prompt)

    def pump(self) -> None:
        if self.runner.busy():
            return
        with self.queue_lock:
            if not self.pending:
                return
            prompt = self.pending.popleft()
        self._launch(prompt)

    def _launch(self, prompt: Prompt) -> None:
        self.telegram.react(prompt.chat_id, prompt.message_id, REACTION_WORKING)
        if self.runner.start(lambda: self._drain(prompt)):
            return
        with self.queue_lock:
            self.pending.appendleft(prompt)
        self.telegram.react(prompt.chat_id, prompt.message_id, REACTION_QUEUED)

    def _drain(self, prompt: Prompt | None) -> None:
        while prompt is not None:
            self.telegram.react(prompt.chat_id, prompt.message_id, REACTION_WORKING)
            phase = "error"
            try:
                phase = self.execute(prompt.chat_id, prompt.user_id, prompt.text, prompt.message_id)
            except ConfigError as exc:
                log.error("%s", exc)
                os._exit(78)
            except Exception:
                log.exception("run failed")
                try:
                    self.say(prompt.user_id, prompt.chat_id, "The bridge hit an error before Cursor finished.")
                except Exception:
                    log.exception("could not report the error")
                phase = "error"
            emoji = {
                "done": REACTION_DONE,
                "cancelled": REACTION_CANCELLED,
                "error": REACTION_ERROR,
            }.get(phase, REACTION_ERROR)
            self.telegram.react(prompt.chat_id, prompt.message_id, emoji)
            with self.queue_lock:
                prompt = self.pending.popleft() if self.pending else None
            if prompt is not None:
                self.runner.reset_cancel()

    def set_mode(self, chat_id: int, user_id: int, rest: str) -> None:
        row = self.chat(user_id)
        if not rest:
            self.say(user_id, chat_id, f"Mode is {row['mode']}.")
            return
        mode = rest.split()[0].lower()
        if mode not in {"agent", "ask", "plan"}:
            self.say(user_id, chat_id, "Use /mode agent, /mode ask, or /mode plan.")
            return
        row["mode"] = mode
        self.state.save()
        self.say(user_id, chat_id, f"Mode is {mode}.")

    def set_workspace(self, chat_id: int, user_id: int, rest: str) -> None:
        row = self.chat(user_id)
        if not rest:
            self.say(user_id, chat_id, f"Folder is {row['workspace']}")
            return
        try:
            path = resolve_workspace(rest, self.home)
        except ValueError as exc:
            self.say(user_id, chat_id, str(exc))
            return
        if self.runner.busy():
            self.drop_session = True
        row["workspace"] = str(path)
        row["session_id"] = ""
        self.state.save()
        self.say(user_id, chat_id, f"Folder is {path}. The next message starts a fresh chat there.")

    def status_text(self, user_id: int) -> str:
        row = self.chat(user_id)
        phone = self.verified_phone(user_id) or ""
        account = self.account_line()
        session = row.get("session_id") or "none"
        running = "running" if self.runner.busy() else "idle"
        waiting = self.queued()
        run_line = f"Run {running}"
        if waiting == 1:
            run_line += ", 1 queued"
        elif waiting:
            run_line += f", {waiting} queued"
        return "\n".join(
            [
                account,
                f"Phone {mask_phone(phone)}",
                f"Folder {row.get('workspace')}",
                f"Mode {row.get('mode')}",
                f"Model {cursor_model()}",
                f"Chat {session}",
                run_line,
            ]
        )

    def account_line(self) -> str:
        try:
            completed = subprocess.run(
                [self.agent, "status", "--format", "json"],
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
                env=self.agent_env(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"Cursor status unavailable ({exc.__class__.__name__})"
        if completed.returncode != 0:
            return "Cursor CLI is installed and not signed in. Run scripts/login.sh from the Mac."
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            line = completed.stdout.strip().splitlines()
            return line[0][:200] if line else "Cursor is signed in."
        if isinstance(payload, dict):
            for key in ("email", "userEmail", "account"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    return f"Cursor account {value}"
            user = payload.get("user")
            if isinstance(user, dict):
                for key in ("email", "name"):
                    value = user.get(key)
                    if isinstance(value, str) and value:
                        return f"Cursor account {value}"
        return "Cursor is signed in."

    def agent_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.pop("CURSOR_API_KEY", None)
        env["NO_OPEN_BROWSER"] = "1"
        local_bin = str(Path.home() / ".local" / "bin")
        env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")
        return env

    def execute(self, chat_id: int, user_id: int, prompt: str, message_id: int | None = None) -> str:
        row = self.chat(user_id)
        workspace = str(row.get("workspace") or self.default_workspace)
        mode = str(row.get("mode") or "agent")
        session_id = str(row.get("session_id") or "")
        try:
            resolve_workspace(workspace, self.home)
        except ValueError as exc:
            self.say(user_id, chat_id, str(exc))
            return "error"
        board = LiveBoard(
            self.telegram,
            chat_id,
            lambda sent_id: self.remember(user_id, sent_id),
            model=cursor_model(),
            project=Path(workspace).name,
            reply_to=message_id,
        )
        self.active_draft_id = board.draft_id
        try:
            return self._execute_board(chat_id, user_id, prompt, workspace, mode, session_id, board)
        finally:
            if self.active_draft_id == board.draft_id:
                self.active_draft_id = None

    def on_generation_stopped(self, stopped: dict) -> None:
        chat = stopped.get("chat") or {}
        chat_id = chat.get("id")
        if chat.get("type") not in {None, "private"} or not isinstance(chat_id, int):
            return
        if self.verified_phone(chat_id) is None:
            return
        if stopped.get("draft_id") != self.active_draft_id or not self.runner.busy():
            return
        log.info("draft %s stopped", self.active_draft_id)
        self.runner.stop()

    def _execute_board(
        self,
        chat_id: int,
        user_id: int,
        prompt: str,
        workspace: str,
        mode: str,
        session_id: str,
        board: LiveBoard,
    ) -> str:
        board.publish(force=True)
        sent: set[str] = set()

        def on_output(path: Path) -> None:
            key = str(path)
            if key in sent:
                return
            if self.deliver_file(chat_id, user_id, path):
                sent.add(key)

        outcome = self.spawn(workspace, mode, session_id, prompt, board, on_output)
        if outcome.get("resume_failed") and session_id:
            board.note("Starting a fresh chat")
            outcome = self.spawn(workspace, mode, "", prompt, board, on_output)
        new_session = outcome.get("session_id") or ""
        if self.drop_session:
            self.drop_session = False
            self.chat(user_id)["session_id"] = ""
            self.state.save()
        elif isinstance(new_session, str) and new_session:
            self.chat(user_id)["session_id"] = new_session
            self.state.save()
        board.flush()
        return board.phase if board.phase in {"done", "cancelled", "error"} else "done"

    def spawn(
        self,
        workspace: str,
        mode: str,
        session_id: str,
        prompt: str,
        board: LiveBoard,
        on_output=None,
    ) -> dict:
        cmd = [
            self.agent,
            "-p",
            "--force",
            "--trust",
            "--approve-mcps",
            "--sandbox",
            "disabled",
            "--output-format",
            "stream-json",
            "--stream-partial-output",
            "--workspace",
            workspace,
        ]
        if mode in {"ask", "plan"}:
            cmd.extend(["--mode", mode])
        model = os.environ.get("CURSOR_MODEL", "").strip()
        if model:
            cmd.extend(["--model", model])
        if session_id:
            cmd.extend(["--resume", session_id])
        cmd.append(prompt)
        acc = {"last": "", "result": "", "tool": "", "session_id": "", "is_error": False, "duration_ms": 0}
        root = Path(workspace)
        before_media = media_index(root)
        stderr_chunks: list[bytes] = []
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                cwd=workspace,
                env=self.agent_env(),
                start_new_session=True,
            )
        except OSError as exc:
            board.finish(f"Could not start Cursor: {exc}", "error")
            return {"text": board.transcript.render(), "session_id": "", "cancelled": False, "resume_failed": False}
        self.runner.attach(proc)
        lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._read_stdout, args=(proc, lines), daemon=True).start()
        stderr_thread = threading.Thread(target=self._read_stderr, args=(proc, stderr_chunks), daemon=True)
        stderr_thread.start()
        deadline = time.time() + RUN_TIMEOUT_SEC
        timed_out = False
        while True:
            if self.runner.cancel.is_set():
                stop_process(proc)
                break
            if time.time() > deadline:
                timed_out = True
                stop_process(proc)
                break
            try:
                line = lines.get(timeout=0.4)
            except queue.Empty:
                board.tick()
                continue
            if line is None:
                break
            consume_event(line, acc, board)
            tool_call = acc.pop("pending_tool", None)
            if on_output is not None and tool_call is not None:
                for path in output_paths(tool_call, root):
                    on_output(path)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            stop_process(proc)
            proc.wait(timeout=5)
        stderr_thread.join(timeout=2)
        stderr = b"".join(stderr_chunks).decode("utf-8", "replace")[-1500:]
        cancelled = self.runner.cancel.is_set() or timed_out
        produced = (board.transcript.render() or str(acc.get("result") or "")).strip()
        auth_failed = _auth_failed(stderr)
        resume_failed = bool(session_id) and not produced and not cancelled and not auth_failed and proc.returncode not in (0, None)
        if on_output is not None:
            for path in changed_media(before_media, root):
                on_output(path)
        if resume_failed:
            return {"text": "", "session_id": "", "cancelled": False, "resume_failed": True}
        if auth_failed:
            board.finish(
                "Cursor on this machine is not signed in. From the Mac, run experiments/cursor-telegram/scripts/login.sh and open the URL it prints.",
                "error",
            )
        elif timed_out:
            board.finish(produced or "Stopped because the run exceeded 45 minutes.", "cancelled")
        elif cancelled:
            board.finish(produced or "Stopped.", "cancelled")
        elif acc.get("is_error"):
            board.finish(produced or "Cursor reported an error.", "error")
        elif not produced:
            detail = redact(stderr.strip(), self.telegram.token) or f"Cursor exited {proc.returncode}."
            board.finish(detail[:1500], "error")
        else:
            board.finish(produced, "done")
        board.flush()
        return {
            "text": board.transcript.render(),
            "session_id": "" if auth_failed else acc.get("session_id") or "",
            "cancelled": cancelled and not timed_out,
            "resume_failed": False,
        }

    @staticmethod
    def _read_stdout(proc: subprocess.Popen, lines: queue.Queue) -> None:
        if proc.stdout is None:
            lines.put(None)
            return
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    @staticmethod
    def _read_stderr(proc: subprocess.Popen, chunks: list[bytes]) -> None:
        if proc.stderr is None:
            return
        while True:
            data = proc.stderr.read(1024)
            if not data:
                return
            chunks.append(data)
            extra = sum(len(part) for part in chunks) - 8000
            while extra > 0 and chunks:
                drop = chunks.pop(0)
                extra -= len(drop)


def _auth_failed(stderr: str) -> bool:
    lowered = stderr.lower()
    markers = (
        "not authenticated",
        "please log in",
        "agent login",
        "unauthorized",
        "not logged in",
        "authentication required",
        "login required",
    )
    return any(marker in lowered for marker in markers)

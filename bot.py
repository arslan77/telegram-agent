#!/usr/bin/env python3
"""Telegram bridge to the local Cursor CLI on this machine.

Only a private chat whose sender shares a phone number on TELEGRAM_ALLOWED_PHONES
can run the agent. Everyone else gets no work done. Runs use `agent -p` on this
host. A leading & is rejected so the chat cannot be handed to a cloud agent.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("cursor-telegram")

TELEGRAM_LIMIT = 3900
RUN_TIMEOUT_SEC = 45 * 60
STATE_VERSION = 1


class ConfigError(Exception):
    """Bad token, allowlist, or file permissions. The process exits 78."""


def normalize_phone(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def parse_allowlist(raw: str) -> set[str]:
    found: set[str] = set()
    for part in raw.replace(";", ",").split(","):
        digits = normalize_phone(part)
        if not digits:
            continue
        if len(digits) < 8 or len(digits) > 15:
            raise ConfigError(f"allowlist phone has {len(digits)} digits; use the full international number")
        found.add(digits)
    if not found:
        raise ConfigError("TELEGRAM_ALLOWED_PHONES is empty")
    return found


def mask_phone(digits: str) -> str:
    if len(digits) <= 4:
        return "••••"
    return digits[:3] + "•" * (len(digits) - 5) + digits[-2:]


def parse_command(text: str) -> tuple[str | None, str]:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None, stripped
    head, _, rest = stripped.partition(" ")
    return head.split("@", 1)[0].lower(), rest.strip()


def is_cloud_handoff(text: str) -> bool:
    return text.lstrip().startswith("&")


def split_text(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    body = text.strip() or "Finished, with no text."
    parts: list[str] = []
    while body:
        if len(body) <= limit:
            parts.append(body)
            break
        cut = body.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(body[:cut].rstrip())
        body = body[cut:].lstrip("\n")
    return parts


def redact(text: str, secret: str) -> str:
    if secret and secret in text:
        return text.replace(secret, "***")
    return text


def resolve_workspace(raw: str, home: Path) -> Path:
    home_real = home.resolve()
    text = raw.strip()
    if not text or text == "~":
        path = home_real
    elif text.startswith("~/"):
        path = home_real / text[2:]
    else:
        path = Path(text)
        if not path.is_absolute():
            path = home_real / path
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError("That folder does not exist.") from exc
    if not resolved.is_dir():
        raise ValueError("That path is not a folder.")
    if os.path.commonpath([str(resolved), str(home_real)]) != str(home_real):
        raise ValueError("That folder is outside your home directory.")
    return resolved


def contact_phone(message: dict) -> str | None:
    """Return the sender's own phone digits, or None if this is not their contact."""
    contact = message.get("contact")
    sender = message.get("from") or {}
    if not isinstance(contact, dict) or sender.get("is_bot"):
        return None
    if contact.get("user_id") != sender.get("id"):
        return None
    phone = normalize_phone(str(contact.get("phone_number") or ""))
    if len(phone) < 8 or len(phone) > 15:
        return None
    return phone


def assistant_text(event: dict) -> str:
    message = event.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts).strip()


def tool_label(tool_call: object) -> str:
    if not isinstance(tool_call, dict) or not tool_call:
        return "Working"
    name, body = next(iter(tool_call.items()))
    label = str(name).removesuffix("ToolCall")
    args: dict = {}
    if isinstance(body, dict):
        raw = body.get("args", body.get("arguments"))
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = None
        if isinstance(raw, dict):
            args = raw
    path = args.get("path") or args.get("target_file")
    command = args.get("command")
    if isinstance(path, str) and path:
        return f"{label} {path}"[:240]
    if isinstance(command, str) and command:
        first = " ".join(command.split())
        return f"{label} {first}"[:240]
    return label[:240]


def consume_event(line: str, acc: dict) -> None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return
    if not isinstance(event, dict):
        return
    session_id = event.get("session_id")
    if isinstance(session_id, str) and session_id:
        acc["session_id"] = session_id
    kind = event.get("type")
    if kind == "assistant":
        if "timestamp_ms" in event:
            return
        text = assistant_text(event)
        if text:
            acc["last"] = text
    elif kind == "tool_call" and event.get("subtype") == "started":
        acc["tool"] = tool_label(event.get("tool_call"))
    elif kind == "result":
        result = event.get("result")
        if isinstance(result, str):
            acc["result"] = result
        acc["is_error"] = bool(event.get("is_error"))
        if isinstance(event.get("duration_ms"), int):
            acc["duration_ms"] = event["duration_ms"]


class Telegram:
    def __init__(self, token: str) -> None:
        self.token = token
        self._lock = threading.Lock()

    def call(self, method: str, payload: dict, timeout: float) -> dict:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        data = json.dumps(payload).encode()
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._lock:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    body = response.read()
        except urllib.error.HTTPError as exc:
            detail = redact(exc.read().decode("utf-8", "replace"), self.token)
            if exc.code == 401:
                raise ConfigError("Telegram rejected the bot token") from None
            if exc.code == 429:
                retry = 5
                try:
                    retry = int(json.loads(detail).get("parameters", {}).get("retry_after", 5))
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
                time.sleep(min(retry, 30))
                return self.call(method, payload, timeout)
            raise RuntimeError(redact(f"telegram {method} failed: {exc.code} {detail[:300]}", self.token)) from None
        except urllib.error.URLError as exc:
            raise RuntimeError(redact(f"telegram {method} unreachable: {exc.reason}", self.token)) from None
        parsed = json.loads(body)
        if not parsed.get("ok"):
            raise RuntimeError(redact(f"telegram {method} not ok", self.token))
        return parsed

    def send(self, chat_id: int, text: str, reply_markup: dict | None = None) -> int | None:
        first_id = None
        for part in split_text(text):
            payload: dict = {"chat_id": chat_id, "text": part}
            if reply_markup is not None and first_id is None:
                payload["reply_markup"] = reply_markup
            result = self.call("sendMessage", payload, timeout=30)
            message_id = result.get("result", {}).get("message_id")
            if first_id is None:
                first_id = message_id
            reply_markup = None
        return first_id

    def edit(self, chat_id: int, message_id: int, text: str) -> None:
        self.call(
            "editMessageText",
            {"chat_id": chat_id, "message_id": message_id, "text": text[:TELEGRAM_LIMIT]},
            timeout=30,
        )


class State:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.data = {
            "version": STATE_VERSION,
            "offset": 0,
            "initialized": False,
            "verified": {},
            "chats": {},
        }
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            log.warning("state file unreadable, starting fresh")
            return
        if isinstance(loaded, dict):
            self.data.update(loaded)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)


class Runner:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.proc: subprocess.Popen | None = None
        self.cancel = threading.Event()

    def busy(self) -> bool:
        with self.lock:
            return self.thread is not None and self.thread.is_alive()

    def start(self, target) -> bool:
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                return False
            self.cancel.clear()
            self.proc = None
            self.thread = threading.Thread(target=target, name="cursor-run", daemon=True)
            self.thread.start()
            return True

    def attach(self, proc: subprocess.Popen) -> None:
        with self.lock:
            self.proc = proc
            should_stop = self.cancel.is_set()
        if should_stop:
            stop_process(proc)

    def stop(self) -> None:
        self.cancel.set()
        with self.lock:
            proc = self.proc
        if proc is not None:
            stop_process(proc)


def stop_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + 5
    while time.time() < deadline and proc.poll() is None:
        time.sleep(0.1)
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def find_agent() -> str:
    override = os.environ.get("CURSOR_AGENT", "").strip()
    if override:
        return override
    local = Path.home() / ".local" / "bin" / "agent"
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    raise ConfigError("Cursor CLI is not installed at ~/.local/bin/agent")


def default_workspace(home: Path) -> Path:
    configured = os.environ.get("CURSOR_WORKSPACE", "").strip()
    if configured:
        try:
            return resolve_workspace(configured, home)
        except ValueError as exc:
            log.warning("CURSOR_WORKSPACE ignored: %s", exc)
    lab = home / "alienware-lab"
    if lab.is_dir():
        return lab.resolve()
    return home.resolve()


HELP = "\n".join(
    [
        "Cursor is running on this machine under your account.",
        "",
        "/status",
        "/new — start a fresh chat",
        "/cancel — stop the current run",
        "/mode agent — edit files and run commands",
        "/mode ask — read only",
        "/mode plan — plan only",
        "/workspace — show or set the folder (inside your home directory)",
        "",
        "Any other message is sent to that local session.",
    ]
)

SHARE = {
    "keyboard": [[{"text": "Share my phone number", "request_contact": True}]],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}
REMOVE_KEYBOARD = {"remove_keyboard": True}


class Bridge:
    def __init__(self, telegram: Telegram, state: State, allow: set[str], agent: str, home: Path) -> None:
        self.telegram = telegram
        self.state = state
        self.allow = allow
        self.agent = agent
        self.home = home
        self.runner = Runner()
        self.default_workspace = default_workspace(home)

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
        return row

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
                    {"command": "help", "description": "What this bot can do"},
                ]
            },
            timeout=30,
        )
        if not self.state.data.get("initialized"):
            self.drain()
        log.info("bridge up, %d allowlisted numbers", len(self.allow))
        while True:
            timeout = 2 if self.runner.busy() else 25
            try:
                updates = self.telegram.call(
                    "getUpdates",
                    {
                        "offset": self.state.data.get("offset", 0),
                        "timeout": timeout,
                        "allowed_updates": ["message"],
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
                {"offset": self.state.data.get("offset", 0), "timeout": 0, "allowed_updates": ["message"]},
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

    def accept(self, update: dict) -> None:
        update_id = int(update.get("update_id", 0))
        self.state.data["offset"] = update_id + 1
        self.state.save()
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
        if "contact" in message:
            self.on_contact(chat_id, user_id, message)
            return
        if self.verified_phone(user_id) is None:
            self.telegram.send(
                chat_id,
                "Share your phone number with the button. Only numbers on the allowlist can use this bot.",
                SHARE,
            )
            return
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            self.telegram.send(chat_id, "Send a text message.")
            return
        self.on_text(chat_id, user_id, text)

    def on_contact(self, chat_id: int, user_id: int, message: dict) -> None:
        phone = contact_phone(message)
        if phone is None:
            self.telegram.send(chat_id, "Use the button to share your own number.", SHARE)
            return
        if phone not in self.allow:
            log.info("rejected user %s phone %s", user_id, mask_phone(phone))
            self.telegram.send(
                chat_id,
                f"{mask_phone(phone)} is not on the allowlist.",
                SHARE,
            )
            return
        self.state.data.setdefault("verified", {})[str(user_id)] = phone
        self.state.save()
        log.info("verified user %s phone %s", user_id, mask_phone(phone))
        self.telegram.send(
            chat_id,
            "Number accepted. Messages now go to Cursor on this machine.\n\n" + HELP,
            REMOVE_KEYBOARD,
        )

    def on_text(self, chat_id: int, user_id: int, text: str) -> None:
        command, rest = parse_command(text)
        if command in {"/start", "/help"}:
            self.telegram.send(chat_id, HELP)
            return
        if command == "/cancel":
            if self.runner.busy():
                self.runner.stop()
                self.telegram.send(chat_id, "Stopping the current run.")
            else:
                self.telegram.send(chat_id, "Nothing is running.")
            return
        if command == "/status":
            self.telegram.send(chat_id, self.status_text(user_id))
            return
        if command in {"/new", "/mode", "/workspace"} and self.runner.busy():
            self.telegram.send(chat_id, "A run is in progress. /cancel to stop it.")
            return
        if command == "/new":
            self.chat(user_id)["session_id"] = ""
            self.state.save()
            self.telegram.send(chat_id, "Next message starts a fresh chat.")
            return
        if command == "/mode":
            self.set_mode(chat_id, user_id, rest)
            return
        if command == "/workspace":
            self.set_workspace(chat_id, user_id, rest)
            return
        if command is not None:
            self.telegram.send(chat_id, "Unknown command.\n\n" + HELP)
            return
        if is_cloud_handoff(text):
            self.telegram.send(chat_id, "Cloud handoff is off. This bot only runs Cursor on this machine.")
            return
        if not self.runner.start(lambda: self._guarded(chat_id, user_id, text)):
            self.telegram.send(chat_id, "A run is already in progress. /cancel to stop it.")

    def _guarded(self, chat_id: int, user_id: int, text: str) -> None:
        try:
            self.execute(chat_id, user_id, text)
        except ConfigError as exc:
            log.error("%s", exc)
            os._exit(78)
        except Exception:
            log.exception("run failed")
            try:
                self.telegram.send(chat_id, "The bridge hit an error before Cursor finished.")
            except Exception:
                log.exception("could not report the error")

    def set_mode(self, chat_id: int, user_id: int, rest: str) -> None:
        row = self.chat(user_id)
        if not rest:
            self.telegram.send(chat_id, f"Mode is {row['mode']}.")
            return
        mode = rest.split()[0].lower()
        if mode not in {"agent", "ask", "plan"}:
            self.telegram.send(chat_id, "Use /mode agent, /mode ask, or /mode plan.")
            return
        row["mode"] = mode
        self.state.save()
        self.telegram.send(chat_id, f"Mode is {mode}.")

    def set_workspace(self, chat_id: int, user_id: int, rest: str) -> None:
        row = self.chat(user_id)
        if not rest:
            self.telegram.send(chat_id, f"Folder is {row['workspace']}")
            return
        try:
            path = resolve_workspace(rest, self.home)
        except ValueError as exc:
            self.telegram.send(chat_id, str(exc))
            return
        row["workspace"] = str(path)
        row["session_id"] = ""
        self.state.save()
        self.telegram.send(chat_id, f"Folder is {path}. The next message starts a fresh chat there.")

    def status_text(self, user_id: int) -> str:
        row = self.chat(user_id)
        phone = self.verified_phone(user_id) or ""
        account = self.account_line()
        session = row.get("session_id") or "none"
        running = "running" if self.runner.busy() else "idle"
        return "\n".join(
            [
                account,
                f"Phone {mask_phone(phone)}",
                f"Folder {row.get('workspace')}",
                f"Mode {row.get('mode')}",
                f"Chat {session}",
                f"Run {running}",
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

    def execute(self, chat_id: int, user_id: int, prompt: str) -> None:
        row = self.chat(user_id)
        workspace = str(row.get("workspace") or self.default_workspace)
        mode = str(row.get("mode") or "agent")
        session_id = str(row.get("session_id") or "")
        try:
            resolve_workspace(workspace, self.home)
        except ValueError as exc:
            self.telegram.send(chat_id, str(exc))
            return
        status_id = self.telegram.send(chat_id, f"Working in {workspace}")
        outcome = self.spawn(workspace, mode, session_id, prompt, chat_id, status_id)
        if outcome.get("resume_failed") and session_id:
            if status_id is not None:
                self.safe_edit(chat_id, status_id, "Previous chat could not be resumed. Starting a fresh one.")
            outcome = self.spawn(workspace, mode, "", prompt, chat_id, status_id)
        new_session = outcome.get("session_id") or ""
        if isinstance(new_session, str) and new_session:
            self.chat(user_id)["session_id"] = new_session
            self.state.save()
        reply = outcome.get("text") or "Finished, with no text."
        if outcome.get("cancelled"):
            reply = "Stopped."
        parts = split_text(reply)
        if status_id is not None:
            self.safe_edit(chat_id, status_id, parts[0])
            for part in parts[1:]:
                self.telegram.send(chat_id, part)
        else:
            self.telegram.send(chat_id, reply)

    def safe_edit(self, chat_id: int, message_id: int, text: str) -> None:
        try:
            self.telegram.edit(chat_id, message_id, text)
        except RuntimeError as exc:
            log.warning("edit failed: %s", exc)
            self.telegram.send(chat_id, text)

    def spawn(
        self,
        workspace: str,
        mode: str,
        session_id: str,
        prompt: str,
        chat_id: int,
        status_id: int | None,
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
            return {"text": f"Could not start Cursor: {exc}"}
        self.runner.attach(proc)
        lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._read_stdout, args=(proc, lines), daemon=True).start()
        stderr_thread = threading.Thread(target=self._read_stderr, args=(proc, stderr_chunks), daemon=True)
        stderr_thread.start()
        deadline = time.time() + RUN_TIMEOUT_SEC
        last_edit = 0.0
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
                line = lines.get(timeout=1)
            except queue.Empty:
                continue
            if line is None:
                break
            consume_event(line, acc)
            tool = acc.get("tool") or ""
            now = time.time()
            if tool and status_id is not None and now - last_edit > 2:
                self.safe_edit(chat_id, status_id, tool)
                last_edit = now
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            stop_process(proc)
            proc.wait(timeout=5)
        stderr_thread.join(timeout=2)
        stderr = b"".join(stderr_chunks).decode("utf-8", "replace")[-1500:]
        cancelled = self.runner.cancel.is_set() or timed_out
        produced = (acc.get("last") or acc.get("result") or "").strip()
        auth_failed = _auth_failed(stderr)
        resume_failed = bool(session_id) and not produced and not cancelled and not auth_failed and proc.returncode not in (0, None)
        if resume_failed:
            return {"text": "", "session_id": "", "cancelled": False, "resume_failed": True}
        text = produced
        if auth_failed:
            text = "Cursor on this machine is not signed in. From the Mac, run experiments/cursor-telegram/scripts/login.sh and open the URL it prints."
        elif timed_out:
            text = (text + "\n\nStopped because the run exceeded 45 minutes.").strip()
        elif cancelled:
            text = "Stopped."
        elif not text:
            detail = redact(stderr.strip(), self.telegram.token) or f"Cursor exited {proc.returncode}."
            text = detail[:1500]
        if acc.get("duration_ms") and text and not cancelled and not auth_failed:
            seconds = max(1, int(acc["duration_ms"]) // 1000)
            text = f"{text}\n\n— {seconds}s"
        return {
            "text": text,
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


def load_config() -> tuple[str, set[str]]:
    env_path = Path(os.environ.get("CURSOR_TELEGRAM_ENV", Path.home() / ".config" / "cursor-telegram" / "env"))
    if env_path.exists():
        mode = env_path.stat().st_mode
        if mode & 0o077:
            raise ConfigError(f"{env_path} is readable by someone other than your user")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    phones = os.environ.get("TELEGRAM_ALLOWED_PHONES", "").strip()
    if not token:
        raise ConfigError("TELEGRAM_BOT_TOKEN is missing")
    return token, parse_allowlist(phones)


def main() -> None:
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token, allow = load_config()
    home = Path.home()
    state_path = Path(os.environ.get("CURSOR_TELEGRAM_STATE", home / ".local" / "share" / "cursor-telegram" / "state.json"))
    bridge = Bridge(Telegram(token), State(state_path), allow, find_agent(), home)

    def on_stop(signum, _frame) -> None:
        log.info("signal %s", signum)
        bridge.runner.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)
    bridge.run()


if __name__ == "__main__":
    try:
        main()
    except ConfigError as exc:
        log.error("%s", exc)
        sys.exit(78)

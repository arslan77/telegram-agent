"""Telegram Bot API calls."""

from __future__ import annotations

from pathlib import Path
import contextlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.request

from app.access import ConfigError, redact, split_text
from app.constants import MAX_DOWNLOAD, log
from app.files import safe_filename


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
            with (contextlib.nullcontext() if method == "getUpdates" else self._lock):
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

    def send_one(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict | None = None,
        html: bool = False,
        reply_to: int | None = None,
    ) -> int | None:
        payload: dict = {"chat_id": chat_id, "text": text[:4096]}
        if html:
            payload["parse_mode"] = "HTML"
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if isinstance(reply_to, int):
            payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        result = self.call("sendMessage", payload, timeout=30)
        message_id = result.get("result", {}).get("message_id")
        return message_id if isinstance(message_id, int) else None

    def react(self, chat_id: int, message_id: int | None, emoji: str) -> None:
        if not isinstance(message_id, int) or not emoji:
            return
        try:
            self.call(
                "setMessageReaction",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reaction": [{"type": "emoji", "emoji": emoji}],
                },
                timeout=15,
            )
        except RuntimeError as exc:
            log.info("reaction %s failed: %s", emoji, exc)

    def send(self, chat_id: int, text: str, reply_markup: dict | None = None) -> int | None:
        first_id = None
        for index, part in enumerate(split_text(text)):
            markup = reply_markup if index == 0 else None
            message_id = self.send_one(chat_id, part, markup)
            if first_id is None:
                first_id = message_id
        return first_id

    def download(self, file_id: str, dest: Path, limit: int = MAX_DOWNLOAD) -> None:
        info = self.call("getFile", {"file_id": file_id}, timeout=30)
        result = info.get("result") if isinstance(info.get("result"), dict) else {}
        remote = result.get("file_path")
        size = result.get("file_size")
        if not isinstance(remote, str) or not remote or remote.startswith("/"):
            raise RuntimeError("Telegram did not return the file")
        if isinstance(size, int) and size > limit:
            raise RuntimeError("File is larger than 20 MB")
        url = f"https://api.telegram.org/file/bot{self.token}/{remote}"
        request = urllib.request.Request(url, method="GET")
        dest.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                with dest.open("wb") as handle:
                    while True:
                        chunk = response.read(1024 * 64)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > limit:
                            raise RuntimeError("File is larger than 20 MB")
                        handle.write(chunk)
        except RuntimeError:
            dest.unlink(missing_ok=True)
            raise
        except Exception as exc:
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"telegram download unreachable: {exc}") from None
        if written == 0:
            dest.unlink(missing_ok=True)
            raise RuntimeError("Telegram returned an empty file")

    def send_file(self, method: str, field: str, chat_id: int, path: Path, caption: str = "") -> int | None:
        boundary = "----cursor" + secrets.token_hex(8)
        filename = safe_filename(path.name)
        chunks: list[bytes] = []

        def field_part(name: str, value: str) -> None:
            chunks.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
            )

        field_part("chat_id", str(chat_id))
        if caption:
            field_part("caption", caption[:1024])
        chunks.append(
            (
                f"--{boundary}\r\n"
                f"Content-Disposition: form-data; name=\"{field}\"; filename=\"{filename}\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode()
            + path.read_bytes()
            + b"\r\n"
        )
        chunks.append(f"--{boundary}--\r\n".encode())
        body = b"".join(chunks)
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with self._lock:
                with urllib.request.urlopen(request, timeout=120) as response:
                    parsed = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = redact(exc.read().decode("utf-8", "replace"), self.token)
            raise RuntimeError(redact(f"telegram {method} failed: {exc.code} {detail[:300]}", self.token)) from None
        except urllib.error.URLError as exc:
            raise RuntimeError(redact(f"telegram {method} unreachable: {exc.reason}", self.token)) from None
        if not parsed.get("ok"):
            raise RuntimeError(redact(f"telegram {method} not ok", self.token))
        message_id = parsed.get("result", {}).get("message_id")
        return message_id if isinstance(message_id, int) else None

    def edit(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        html: bool = False,
        reply_markup: dict | None = None,
    ) -> None:
        payload: dict = {"chat_id": chat_id, "message_id": message_id, "text": text[:4096]}
        if html:
            payload["parse_mode"] = "HTML"
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self.call("editMessageText", payload, timeout=30)

    def send_rich(
        self,
        chat_id: int,
        markdown: str,
        reply_markup: dict | None = None,
        reply_to: int | None = None,
    ) -> int | None:
        payload: dict = {
            "chat_id": chat_id,
            "rich_message": {"markdown": markdown[:32768]},
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if isinstance(reply_to, int):
            payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        result = self.call("sendRichMessage", payload, timeout=30)
        message_id = result.get("result", {}).get("message_id")
        return message_id if isinstance(message_id, int) else None

    def send_draft(self, chat_id: int, draft_id: int, markdown: str) -> None:
        self.call(
            "sendRichMessageDraft",
            {
                "chat_id": chat_id,
                "draft_id": draft_id,
                "can_stop": True,
                "rich_message": {"markdown": markdown[:32768]},
            },
            timeout=30,
        )

    def edit_rich(
        self,
        chat_id: int,
        message_id: int,
        markdown: str,
        reply_markup: dict | None = None,
    ) -> None:
        payload: dict = {
            "chat_id": chat_id,
            "message_id": message_id,
            "rich_message": {"markdown": markdown[:32768]},
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self.call("editMessageText", payload, timeout=30)

    def delete_many(self, chat_id: int, message_ids: list[int]) -> None:
        for start in range(0, len(message_ids), 100):
            chunk = message_ids[start : start + 100]
            if not chunk:
                continue
            try:
                self.call("deleteMessages", {"chat_id": chat_id, "message_ids": chunk}, timeout=30)
            except RuntimeError as exc:
                log.warning("delete batch failed: %s", exc)
                for message_id in chunk:
                    try:
                        self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id}, timeout=15)
                    except RuntimeError as single:
                        log.info("left message %s in place: %s", message_id, single)

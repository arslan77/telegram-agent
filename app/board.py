"""The Telegram message that stays on screen during a run."""

from __future__ import annotations

import secrets
import time

from app.activity import format_activity, format_snippet
from app.client import Telegram
from app.constants import RICH_LIMIT, RUN_KEYBOARD, TELEGRAM_LIMIT, log
from app.html_text import build_run_html, html_rejected
from app.rich import build_run_plain, build_run_rich_markdown, largest_rich_prefix, rich_unavailable
from app.transcript import Transcript, chunk_html


class LiveBoard:
    """One Telegram message for the run: header, streamed answer, one tool line, then the final reply."""

    def __init__(
        self,
        telegram: Telegram,
        chat_id: int,
        on_message,
        model: str = "auto",
        project: str = "",
        reply_to: int | None = None,
        message_id: int | None = None,
    ) -> None:
        self.telegram = telegram
        self.chat_id = chat_id
        self.on_message = on_message
        self.model = (model or "auto").strip() or "auto"
        self.project = project or "workspace"
        self.reply_to = reply_to if isinstance(reply_to, int) else None
        self.session_label = "new"
        self.transcript = Transcript()
        self.started = time.time()
        self.activity = ""
        self.snippet = ""
        self.thinking = ""
        self.thinking_open = False
        self.phase = "running"
        self.final_body: str | None = None
        self.message_id = message_id if isinstance(message_id, int) else None
        self.visible = ""
        self.last_at = 0.0
        self.dirty = False
        self.timer_slot = -1
        self.extras_sent = False
        self.use_rich = True
        self.use_draft = False
        self.rich_dead = False
        self.frozen_ids: list[int] = []
        self.frozen_chars = 0
        self.draft_id = secrets.randbelow(2_000_000_000) + 1
        self.tool_log: list[str] = []

    def set_session(self, session_id: str) -> None:
        short = session_id.strip()[:8]
        if not short or short == self.session_label:
            return
        self.session_label = short
        self.publish(force=True)

    def note(self, phrase: str) -> None:
        if not phrase or phrase == self.activity:
            return
        self.activity = phrase if phrase[:1] in "🟡✅⛔🔴" else f"🟡 {phrase}"
        self.snippet = ""
        self.publish(force=True)

    def add_thinking(self, text: str, *, done: bool) -> None:
        if done:
            if not self.thinking_open and not text:
                return
            self.thinking_open = False
            self.publish(force=False)
            return
        if not text:
            return
        if not self.thinking_open and self.thinking and not self.thinking.endswith("\n\n"):
            self.thinking += "\n\n"
        self.thinking_open = True
        self.thinking += text
        if len(self.thinking) > 12000:
            self.thinking = self.thinking[-12000:]
        self.publish(force=False)

    def show_tool(self, tool_call: object, *, done: bool) -> None:
        line = format_activity(tool_call, done=done)
        snippet = format_snippet(tool_call)
        if line == self.activity and snippet == self.snippet:
            return
        self.activity = line
        self.snippet = snippet
        if done:
            entry = line
            if snippet:
                safe_snippet = snippet.replace("```", "'''")
                entry = f"{line}\n\n```\n{safe_snippet}\n```"
            if entry and (not self.tool_log or self.tool_log[-1] != entry):
                self.tool_log.append(entry)
            if len(self.tool_log) > 30:
                del self.tool_log[:-30]
        self.publish(force=True)

    def stream(self, text: str) -> None:
        if self.transcript.add_delta(text):
            self.publish(force=False)

    def complete(self, text: str) -> None:
        if self.transcript.add_complete(text):
            self.publish(force=False)

    def finish(self, text: str, phase: str) -> None:
        self.final_body = (text or self.transcript.render()).strip()
        self.phase = phase
        self.activity = ""
        self.snippet = ""
        if not self.use_rich and not self.rich_dead:
            self.use_rich = True
        self.publish(force=True)

    def publish(self, force: bool = False) -> None:
        now = time.time()
        slot = int(now - self.started) // 5
        if not force and slot == self.timer_slot and now - self.last_at < 0.55:
            self.dirty = True
            return
        self.timer_slot = slot
        self._present()
        self.dirty = False
        self.last_at = time.time()

    def tick(self) -> None:
        slot = int(time.time() - self.started) // 5
        if slot != self.timer_slot:
            self.dirty = True
        if self.dirty:
            self.publish(force=True)

    def flush(self) -> None:
        self.publish(force=True)

    def _view_args(self) -> dict:
        elapsed = max(0, int(time.time() - self.started))
        body = self.final_body if self.final_body is not None else self.transcript.render()
        return {
            "model": self.model,
            "project": self.project,
            "session": self.session_label,
            "elapsed": elapsed,
            "body": body,
            "activity": self.activity if self.phase == "running" else "",
            "snippet": self.snippet if self.phase == "running" else "",
            "phase": self.phase,
            "tools": list(self.tool_log),
            "thinking": self.thinking,
        }

    def _present(self) -> None:
        fields = self._view_args()
        if self.use_rich and self._present_rich(fields):
            return
        formatted = build_run_html(**fields)
        markup = None if self.phase == "running" else RUN_KEYBOARD
        if self.phase == "running" or len(formatted) <= TELEGRAM_LIMIT:
            show = formatted if len(formatted) <= 4096 else formatted[:TELEGRAM_LIMIT]
            self._send_piece(show, html=True, markup=markup, plain_fields=fields)
            return
        parts = chunk_html(formatted)
        self._send_piece(parts[0], html=True, markup=None, plain_fields=fields)
        if self.extras_sent:
            return
        self.extras_sent = True
        anchor = self.message_id
        anchor_visible = self.visible
        for index, part in enumerate(parts[1:], start=1):
            self.message_id = None
            self.visible = ""
            self._send_piece(
                part,
                html=True,
                markup=markup if index == len(parts) - 1 else None,
                plain_fields=fields,
            )
        self.message_id = anchor
        self.visible = anchor_visible

    def _rich_doc(self, fields: dict, body: str, *, continued: bool, buttons: bool) -> str:
        data = dict(fields)
        data["body"] = body
        return build_run_rich_markdown(**data, continued=continued, buttons=buttons)

    def _present_rich(self, fields: dict) -> bool:
        body = str(fields.get("body") or "")
        buttons = self.phase != "running"
        while True:
            continued = self.frozen_chars > 0
            rest = body[self.frozen_chars :]
            markdown = self._rich_doc(fields, rest, continued=continued, buttons=buttons)
            if len(markdown) <= RICH_LIMIT or not rest:
                return self._upsert_rich(markdown, reply=not self.frozen_ids)
            fit = largest_rich_prefix(
                rest,
                lambda prefix: self._rich_doc(fields, prefix, continued=continued, buttons=False),
            )
            if fit <= 0:
                return self._upsert_rich(markdown[:RICH_LIMIT], reply=not self.frozen_ids)
            chunk = self._rich_doc(fields, rest[:fit], continued=continued, buttons=False)
            if not self._upsert_rich(chunk, reply=not self.frozen_ids):
                return False
            if self.message_id is None:
                return False
            self.frozen_ids.append(self.message_id)
            self.frozen_chars += fit
            self.message_id = None
            self.visible = ""

    def _upsert_rich(self, markdown: str, *, reply: bool) -> bool:
        marker = "rich\n" + markdown
        if marker == self.visible and self.message_id is not None:
            return True
        if self.message_id is None:
            try:
                message_id = self.telegram.send_rich(
                    self.chat_id,
                    markdown,
                    reply_to=self.reply_to if reply else None,
                )
            except RuntimeError as exc:
                detail = str(exc)
                log.info("rich send failed, using html: %s", detail)
                if rich_unavailable(detail):
                    self.rich_dead = True
                self.use_rich = False
                return False
            if message_id is None:
                self.use_rich = False
                return False
            self.message_id = message_id
            self.visible = marker
            self.on_message(message_id)
            return True
        try:
            self.telegram.edit_rich(self.chat_id, self.message_id, markdown)
        except RuntimeError as exc:
            detail = str(exc)
            if "message is not modified" in detail:
                self.visible = marker
                return True
            log.info("rich edit failed, using html: %s", detail)
            self.use_rich = False
            return False
        self.visible = marker
        return True

    def _send_piece(
        self,
        show: str,
        *,
        html: bool,
        markup: dict | None,
        plain_fields: dict | None = None,
        replaced: bool = False,
    ) -> None:
        marker = show + ("|buttons" if markup else "")
        if marker == self.visible and self.message_id is not None:
            return
        if self.message_id is None:
            try:
                message_id = self.telegram.send_one(
                    self.chat_id,
                    show,
                    reply_markup=markup,
                    html=html,
                    reply_to=self.reply_to,
                )
            except RuntimeError as exc:
                if html and plain_fields is not None and html_rejected(str(exc)):
                    self._send_piece(build_run_plain(**plain_fields)[:4096], html=False, markup=markup)
                    return
                log.warning("send failed: %s", exc)
                return
            self.message_id = message_id
            self.visible = marker
            if message_id is not None:
                self.on_message(message_id)
            return
        try:
            self.telegram.edit(self.chat_id, self.message_id, show, html=html, reply_markup=markup)
        except RuntimeError as exc:
            detail = str(exc)
            if "message is not modified" in detail:
                self.visible = marker
                return
            if html and plain_fields is not None and html_rejected(detail):
                self._send_piece(
                    build_run_plain(**plain_fields)[:4096],
                    html=False,
                    markup=markup,
                    replaced=replaced,
                )
                return
            if not replaced and ("message to edit not found" in detail or "MESSAGE_ID_INVALID" in detail):
                log.info("live message gone, starting another")
                self.message_id = None
                self.visible = ""
                self._send_piece(show, html=html, markup=markup, plain_fields=plain_fields, replaced=True)
                return
            log.warning("edit failed: %s", exc)
            return
        self.visible = marker

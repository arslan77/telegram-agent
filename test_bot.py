import json
import tempfile
import threading
import unittest
from pathlib import Path

from bot import (
    Bridge,
    ConfigError,
    MAX_QUEUE,
    REACTION_CANCELLED,
    Prompt,
    REACTION_DONE,
    REACTION_QUEUED,
    REACTION_WORKING,
    State,
    Transcript,
    attachment_from_message,
    changed_media,
    consume_event,
    contact_phone,
    file_page,
    forget_messages,
    inbound_prompt,
    inside_workspace,
    list_sendable,
    output_paths,
    safe_filename,
    LiveBoard,
    build_run_html,
    build_run_rich_markdown,
    format_activity,
    format_bubble,
    format_snippet,
    is_cloud_handoff,
    message_header,
    mask_phone,
    message_ids_for_clear,
    parse_allowlist,
    parse_command,
    redact,
    resolve_workspace,
    split_live,
    split_text,
    tool_entry,
    tool_label,
    tool_phrase,
)


class AllowlistTests(unittest.TestCase):
    def test_accepts_international_numbers(self):
        self.assertEqual(parse_allowlist("+971 50 123 4567, 14155552671"), {"971501234567", "14155552671"})

    def test_rejects_short_numbers(self):
        with self.assertRaises(ConfigError):
            parse_allowlist("12345")

    def test_rejects_empty(self):
        with self.assertRaises(ConfigError):
            parse_allowlist(" , ")

    def test_mask_hides_the_middle(self):
        masked = mask_phone("971501234567")
        self.assertEqual(masked, "971" + ("•" * 7) + "67")
        self.assertNotIn("5012345", masked)


class ContactTests(unittest.TestCase):
    def test_accepts_the_sender_contact(self):
        message = {
            "from": {"id": 7, "is_bot": False},
            "contact": {"user_id": 7, "phone_number": "+971501234567"},
        }
        self.assertEqual(contact_phone(message), "971501234567")

    def test_rejects_a_forwarded_contact(self):
        message = {
            "from": {"id": 7, "is_bot": False},
            "contact": {"user_id": 8, "phone_number": "+971501234567"},
        }
        self.assertIsNone(contact_phone(message))

    def test_rejects_a_contact_with_no_user_id(self):
        message = {
            "from": {"id": 7, "is_bot": False},
            "contact": {"phone_number": "+971501234567"},
        }
        self.assertIsNone(contact_phone(message))


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        (self.home / "lab").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_allows_a_folder_inside_home(self):
        self.assertEqual(resolve_workspace("~/lab", self.home), (self.home / "lab").resolve())

    def test_rejects_a_path_outside_home(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        with self.assertRaises(ValueError):
            resolve_workspace(str(outside), self.home)

    def test_rejects_a_symlink_that_leaves_home(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (self.home / "link").symlink_to(outside)
        with self.assertRaises(ValueError):
            resolve_workspace("link", self.home)

    def test_rejects_a_missing_folder(self):
        with self.assertRaises(ValueError):
            resolve_workspace("~/missing", self.home)


class CommandTests(unittest.TestCase):
    def test_strips_the_bot_name(self):
        self.assertEqual(parse_command("/mode@alienware_bot ask"), ("/mode", "ask"))

    def test_plain_text_is_a_prompt(self):
        self.assertEqual(parse_command("fix the tests"), (None, "fix the tests"))

    def test_cloud_handoff_is_rejected(self):
        self.assertTrue(is_cloud_handoff("  & refactor this"))
        self.assertFalse(is_cloud_handoff("refactor this"))


class OutputTests(unittest.TestCase):
    def test_tool_label_omits_file_contents(self):
        label = tool_label({"writeToolCall": {"args": {"path": "a.py", "fileText": "SECRET TOKEN"}}})
        self.assertEqual(label, "write a.py")
        self.assertNotIn("SECRET", label)
        phrase = tool_phrase({"readToolCall": {"args": {"path": "/home/a/README.md", "fileText": "SECRET"}}})
        self.assertEqual(phrase, "Reading README.md")
        self.assertNotIn("SECRET", phrase)

    def test_transcript_appends_words_and_tools_once(self):
        log = Transcript()
        self.assertTrue(log.add_tool("Reading README.md"))
        self.assertFalse(log.add_tool("Reading README.md"))
        self.assertTrue(log.add_delta("Hello"))
        self.assertTrue(log.add_delta(" there"))
        self.assertEqual(log.render(), "Reading README.md\n\nHello there")
        self.assertFalse(log.add_complete("Hello there"))
        self.assertEqual(log.render(), "Reading README.md\n\nHello there")

    def test_streamed_words_keep_the_leading_space(self):
        board_parts: list[str] = []

        class Board:
            def __init__(self) -> None:
                self.transcript = Transcript()

            def stream(self, text: str) -> None:
                self.transcript.add_delta(text)
                board_parts.append(self.transcript.render())

            def complete(self, text: str) -> None:
                self.transcript.add_complete(text)

            def note(self, phrase: str) -> None:
                self.transcript.add_tool(phrase)

        board = Board()
        acc: dict = {}
        for word in ("I", " can't", " comply", "."):
            consume_event(
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp_ms": 1,
                        "message": {"content": [{"type": "text", "text": word}]},
                    }
                ),
                acc,
                board,
            )
        self.assertEqual(board.transcript.render(), "I can't comply.")

    def test_commands_render_as_telegram_code(self):
        shown = format_bubble(
            "I'll open the chat.\n\n"
            'Running curl -sS "https://example.com?q=1&x=2"\n\n'
            "Reading README.md\n\n"
            "The chat is public: **https://example.com/tunnel**"
        )
        self.assertIn('<pre><code class="language-bash">curl -sS "https://example.com?q=1&amp;x=2"</code></pre>', shown)
        self.assertIn("Reading <code>README.md</code>", shown)
        self.assertIn('<b><a href="https://example.com/tunnel">https://example.com/tunnel</a></b>', shown)
        self.assertIn("I'll open the chat.", shown)
        self.assertNotIn("**", shown)

    def test_markdown_renders_headings_lists_and_fences(self):
        shown = format_bubble(
            "# Plan\n\n- one\n- [x] two\n\nSee [docs](https://example.com/docs)\n\n```python\nprint(1)\n```"
        )
        self.assertIn("<b>Plan</b>", shown)
        self.assertIn("• one", shown)
        self.assertIn("• ☑ two", shown)
        self.assertIn('<a href="https://example.com/docs">docs</a>', shown)
        self.assertIn('<pre><code class="language-python">print(1)</code></pre>', shown)

    def test_edit_snippet_shows_removed_and_added_lines(self):
        call = {
            "strReplaceToolCall": {
                "args": {
                    "path": "bot.py",
                    "old_string": "alpha",
                    "new_string": "beta",
                    "fileText": "SECRET TOKEN",
                }
            }
        }
        entry = tool_entry(call)
        self.assertIn("Editing bot.py", entry)
        self.assertIn("🔴 -alpha", entry)
        self.assertIn("🟢 +beta", entry)
        self.assertNotIn("SECRET", entry)
        shown = format_bubble(entry)
        self.assertIn("Editing <code>bot.py</code>", shown)
        self.assertIn("<pre><code>🔴 -alpha\n🟢 +beta</code></pre>", shown)

    def test_header_names_the_model(self):
        self.assertEqual(
            message_header("grok-4.7-high"),
            "<blockquote><b>grok-4.7-high</b></blockquote>",
        )
        shown = message_header("grok-4.7-high", project="lab", session="abcd1234", elapsed=12)
        self.assertIn("[abcd1234]", shown)
        self.assertIn("lab", shown)
        self.assertIn("grok-4.7-high", shown)
        self.assertIn("⏳ 12s", shown)

    def test_live_view_keeps_one_status_line_then_the_final_answer(self):
        shell = {
            "shellToolCall": {
                "args": {"command": "echo hi"},
                "result": {"success": {"stdout": "hi\n", "stderr": "warn\n", "exitCode": 0}},
            }
        }
        self.assertEqual(format_activity(shell, done=False), "🟡 shell: echo hi")
        self.assertEqual(format_activity(shell, done=True), "✅ shell: echo hi")
        snippet = format_snippet(shell)
        self.assertIn("🟢 hi", snippet)
        self.assertIn("🔴 warn", snippet)
        diff = {
            "shellToolCall": {
                "args": {"command": "git diff"},
                "result": {"success": {"stdout": "@@ -1 +1 @@\n-old\n+new\n", "stderr": "", "exitCode": 0}},
            }
        }
        colored = format_snippet(diff)
        self.assertIn("🔷 @@ -1 +1 @@", colored)
        self.assertIn("🔴 -old", colored)
        self.assertIn("🟢 +new", colored)
        running = build_run_html(
            model="grok-4.7-high",
            project="lab",
            session="abcd1234",
            elapsed=3,
            body="Opening the tunnel.",
            activity="🟡 shell: echo hi",
            snippet="echo hi",
            phase="running",
        )
        self.assertIn("Opening the tunnel.", running)
        self.assertIn("🟡 shell: echo hi", running)
        self.assertIn("<pre><code>echo hi</code></pre>", running)
        done = build_run_html(
            model="grok-4.7-high",
            project="lab",
            session="abcd1234",
            elapsed=8,
            body="The tunnel is up.",
            activity="🟡 shell: echo hi",
            snippet="echo hi",
            phase="done",
        )
        self.assertIn("✅", done)
        self.assertIn("The tunnel is up.", done)
        self.assertNotIn("shell: echo hi", done)
        self.assertNotIn("<pre><code>echo hi</code></pre>", done)

    def test_split_live_starts_a_second_message(self):
        head, tail = split_live("one\ntwo\nthree\nfour", 10)
        self.assertLessEqual(len(head), 10)
        self.assertTrue(tail)
        self.assertNotIn(tail, head)

    def test_clear_forgets_messages_and_keeps_the_cursor_session(self):
        row = {"session_id": "chat-1", "workspace": "/home/a/lab", "mode": "agent", "message_ids": [4, 4, "nope", 9]}
        self.assertEqual(message_ids_for_clear(row, 11), [4, 9, 11])
        kept = forget_messages(row)
        self.assertEqual(kept["session_id"], "chat-1")
        self.assertEqual(kept["workspace"], "/home/a/lab")
        self.assertEqual(kept["message_ids"], [])
        self.assertEqual(row["message_ids"], [4, 4, "nope", 9])

    def test_split_and_redact(self):
        parts = split_text("a\n" + ("b" * 4000), limit=100)
        self.assertGreater(len(parts), 1)
        self.assertNotIn("super-secret", redact("token super-secret here", "super-secret"))


class FileTests(unittest.TestCase):
    def test_safe_filename_drops_directories(self):
        self.assertEqual(safe_filename("../../etc/passwd"), "passwd")
        self.assertNotIn("/", safe_filename("a/b.mp3"))

    def test_photo_uses_the_largest_size_and_caption(self):
        attachment = attachment_from_message(
            {
                "caption": "what is this",
                "photo": [
                    {"file_id": "small", "file_size": 10},
                    {"file_id": "large", "file_size": 99},
                ],
            }
        )
        self.assertIsNotNone(attachment)
        assert attachment is not None
        self.assertEqual(attachment["file_id"], "large")
        self.assertEqual(attachment["caption"], "what is this")

    def test_prompts_distinguish_images_and_audio(self):
        image = inbound_prompt(Path("/home/a/lab/.cursor-telegram/inbound/pic.jpg"), "")
        audio = inbound_prompt(Path("/home/a/lab/.cursor-telegram/inbound/voice.ogg"), "transcribe this")
        self.assertIn("Read this file and look at it", image)
        self.assertIn("audio file", audio)
        self.assertIn("transcribe this", audio)

    def test_output_paths_skip_reads_and_keep_new_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "pic.png"
            image.write_bytes(b"png")
            read = {"readToolCall": {"args": {"path": str(image)}}}
            self.assertEqual(output_paths(read, root), [])
            secret = root / ".env"
            secret.write_bytes(b"TOKEN=1")
            leaked = {"writeToolCall": {"args": {"path": str(secret)}}}
            self.assertEqual(output_paths(leaked, root), [])
            made = {
                "generateImageToolCall": {
                    "args": {"file_path": "pic.png"},
                    "result": {"success": {"file_path": "pic.png"}},
                }
            }
            self.assertEqual(output_paths(made, root), [image.resolve()])
            outside = inside_workspace(root, "../escape.png")
            self.assertIsNone(outside)

    def test_changed_media_finds_new_audio_and_list_skips_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_bytes(b"SECRET=1")
            (root / ".git").mkdir()
            (root / ".git" / "song.mp3").write_bytes(b"nope")
            before = {}
            note = root / "note.mp3"
            note.write_bytes(b"abc")
            found = changed_media(before, root)
            self.assertEqual(found, [note.resolve()])
            listed = list_sendable(root, "note")
            self.assertEqual(listed, [note.resolve()])
            self.assertEqual(list_sendable(root, ".env"), [])
            page, text = file_page(listed, 0, root)
            self.assertEqual(page, [note.resolve()])
            self.assertIn("note.mp3", text)


class FakeTelegram:
    def __init__(self) -> None:
        self.reactions: list[tuple[int | None, str]] = []
        self.sent: list[str] = []

    def react(self, chat_id: int, message_id: int | None, emoji: str) -> None:
        self.reactions.append((message_id, emoji))

    def send_one(self, chat_id: int, text: str, reply_markup=None, html: bool = False, reply_to=None) -> int:
        self.sent.append(text)
        return 1


class QueueTests(unittest.TestCase):
    def bridge(self, telegram: FakeTelegram, root: Path) -> Bridge:
        return Bridge(telegram, State(root / "state.json"), set(), "/bin/true", root)

    def test_second_message_waits_and_keeps_its_own_reaction(self):
        telegram = FakeTelegram()
        started = threading.Event()
        release = threading.Event()
        seen: list[tuple[str, bool]] = []
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self.bridge(telegram, Path(tmp))

            def execute(chat_id, user_id, text, message_id=None):
                seen.append((text, bridge.runner.cancel.is_set()))
                if text == "first":
                    started.set()
                    self.assertTrue(release.wait(2))
                    bridge.runner.stop()
                    return "cancelled"
                return "done"

            bridge.execute = execute
            bridge.submit(1, 2, "first", 10)
            self.assertTrue(started.wait(2))
            bridge.submit(1, 2, "second", 11)
            self.assertEqual(bridge.queued(), 1)
            self.assertIn((11, REACTION_QUEUED), telegram.reactions)
            self.assertIn((10, REACTION_WORKING), telegram.reactions)
            release.set()
            self.assertIsNotNone(bridge.runner.thread)
            bridge.runner.thread.join(3)
            self.assertEqual(seen, [("first", False), ("second", False)])
            self.assertEqual(bridge.queued(), 0)
            self.assertIn((10, REACTION_CANCELLED), telegram.reactions)
            self.assertIn((11, REACTION_WORKING), telegram.reactions)
            self.assertIn((11, REACTION_DONE), telegram.reactions)

    def test_queue_stops_at_the_limit(self):
        telegram = FakeTelegram()
        started = threading.Event()
        release = threading.Event()
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self.bridge(telegram, Path(tmp))

            def execute(chat_id, user_id, text, message_id=None):
                started.set()
                release.wait(2)
                return "error"

            bridge.execute = execute
            bridge.submit(1, 2, "running", 1)
            self.assertTrue(started.wait(2))
            for index in range(MAX_QUEUE):
                bridge.submit(1, 2, f"queued-{index}", 100 + index)
            bridge.submit(1, 2, "overflow", 999)
            self.assertEqual(bridge.queued(), MAX_QUEUE)
            self.assertTrue(any("Queue is full" in text for text in telegram.sent))
            release.set()
            bridge.runner.thread.join(3)

    def test_pump_starts_a_prompt_left_behind(self):
        telegram = FakeTelegram()
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self.bridge(telegram, Path(tmp))
            ran: list[str] = []

            def execute(chat_id, user_id, text, message_id=None):
                ran.append(text)
                return "done"

            bridge.execute = execute
            bridge.pending.append(Prompt(1, 2, "left", 7))
            bridge.pump()
            self.assertIsNotNone(bridge.runner.thread)
            bridge.runner.thread.join(3)
            self.assertEqual(ran, ["left"])
            self.assertIn((7, REACTION_WORKING), telegram.reactions)
            self.assertIn((7, REACTION_DONE), telegram.reactions)


class RichMessageTests(unittest.TestCase):
    def test_rich_markdown_keeps_structure(self):
        shown = build_run_rich_markdown(
            model="grok_4",
            project="lab_notes",
            session="abcd1234",
            elapsed=4,
            body="# Plan\n\n- one\n- [x] two\n\n```python\nprint(1)\n```\n\n| Item | State |\n|:-----|:-----:|\n| queue | done |",
            activity="",
            snippet="",
            phase="done",
        )
        self.assertIn("> **[abcd1234] lab\\_notes** · `grok_4` · ⌛ 4s", shown)
        self.assertIn("# Plan", shown)
        self.assertIn("- [x] two", shown)
        self.assertIn("```python\nprint(1)\n```", shown)
        self.assertIn("| queue | done |", shown)
        self.assertIn("✅", shown)
        self.assertNotIn("<b>", shown)

    def test_rich_markdown_keeps_answers_longer_than_the_html_cap(self):
        body = "word " * 1200
        shown = build_run_rich_markdown(
            model="auto",
            project="lab",
            session="abcd1234",
            elapsed=2,
            body=body,
            activity="",
            snippet="",
            phase="done",
        )
        self.assertGreater(len(shown), 4096)
        self.assertIn(body.strip(), shown)

    def test_rich_markdown_closes_a_live_fence_and_escapes_the_activity(self):
        shown = build_run_rich_markdown(
            model="auto",
            project="lab",
            session="abcd1234",
            elapsed=1,
            body="```python\nprint(1)",
            activity="🟡 reading file_name.py",
            snippet="print(1)",
            phase="running",
        )
        self.assertTrue(shown.count("```") % 2 == 0)
        self.assertIn("> *🟡 reading file\\_name.py*", shown)
        self.assertIn("print(1)", shown)

    def test_rejected_rich_message_uses_html(self):
        class Api:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def send_rich(self, chat_id, markdown, reply_markup=None, reply_to=None):
                self.calls.append("rich")
                raise RuntimeError("telegram sendRichMessage failed: 400 can't parse entities")

            def send_one(self, chat_id, text, reply_markup=None, html=False, reply_to=None):
                self.calls.append("html")
                self.html = html
                self.text = text
                return 15

            def edit(self, *args, **kwargs):
                self.calls.append("edit-html")

            def edit_rich(self, *args, **kwargs):
                self.calls.append("edit-rich")
                raise RuntimeError("telegram editMessageText failed: 400 can't parse entities")

        api = Api()
        board = LiveBoard(api, 1, lambda message_id: None, model="auto", project="lab")
        board.publish(force=True)
        self.assertEqual(api.calls, ["rich", "html"])
        self.assertTrue(api.html)
        self.assertIn("<blockquote>", api.text)
        self.assertFalse(board.use_rich)

        board.finish("Done.\n\n# Kept", "done")
        self.assertEqual(api.calls[:4], ["rich", "html", "edit-rich", "edit-html"])
        self.assertFalse(board.use_rich)

    def test_rich_edits_stick_when_telegram_accepts_them(self):
        class Api:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def send_rich(self, chat_id, markdown, reply_markup=None, reply_to=None):
                self.calls.append("rich")
                self.markdown = markdown
                return 21

            def edit_rich(self, chat_id, message_id, markdown, reply_markup=None):
                self.calls.append("edit-rich")
                self.markdown = markdown
                self.markup = reply_markup

            def send_one(self, *args, **kwargs):
                raise AssertionError("html fallback")

            def edit(self, *args, **kwargs):
                raise AssertionError("html edit")

        api = Api()
        board = LiveBoard(api, 1, lambda message_id: None, model="auto", project="lab")
        board.publish(force=True)
        board.finish("# Answer\n\n- done", "done")
        self.assertEqual(api.calls, ["rich", "edit-rich"])
        self.assertIn("# Answer", api.markdown)
        self.assertIn("- done", api.markdown)
        self.assertIsNotNone(api.markup)


if __name__ == "__main__":
    unittest.main()

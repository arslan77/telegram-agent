import tempfile
import threading
import unittest
from pathlib import Path

from app.commands import COMMANDS, dispatch_text
from tests.support import make_bridge


class CommandDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bridge, self.telegram = make_bridge(Path(self.tmp.name))
        self.bridge.account_line = lambda: "Cursor account test@example.com"

    def tearDown(self):
        self.tmp.cleanup()

    def test_help_sends_the_command_list(self):
        self.bridge.on_text(1, 2, "/help", 3)
        self.assertIn("/status", self.telegram.sent[-1])
        self.assertIn("/cancel", self.telegram.sent[-1])

    def test_an_unknown_command_explains_itself(self):
        self.bridge.on_text(1, 2, "/nope", 3)
        self.assertIn("Unknown command.", self.telegram.sent[-1])

    def test_plain_text_is_not_consumed_as_a_command(self):
        self.assertFalse(dispatch_text(self.bridge, 1, 2, "fix the tests", 3))
        self.assertEqual(self.telegram.sent, [])

    def test_a_cloud_handoff_is_refused_and_not_queued(self):
        self.bridge.on_text(1, 2, "& refactor this", 3)
        self.assertIn("Cloud handoff is off", self.telegram.sent[-1])
        self.assertEqual(self.bridge.queued(), 0)

    def test_mode_ask_is_saved(self):
        self.bridge.on_text(1, 2, "/mode ask", 3)
        self.assertEqual(self.bridge.chat(2)["mode"], "ask")
        self.assertIn("Mode is ask.", self.telegram.sent[-1])

    def test_an_unknown_mode_does_not_change_the_saved_mode(self):
        self.bridge.on_text(1, 2, "/mode yolo", 3)
        self.assertEqual(self.bridge.chat(2)["mode"], "agent")
        self.assertIn("/mode agent", self.telegram.sent[-1])

    def test_mode_without_an_argument_reports_the_current_mode(self):
        self.bridge.on_text(1, 2, "/mode", 3)
        self.assertIn("Mode is agent.", self.telegram.sent[-1])

    def test_workspace_without_an_argument_reports_the_folder(self):
        self.bridge.on_text(1, 2, "/workspace", 3)
        self.assertIn("Folder is", self.telegram.sent[-1])

    def test_workspace_rejects_a_missing_folder(self):
        self.bridge.on_text(1, 2, "/workspace ~/missing", 3)
        self.assertIn("does not exist", self.telegram.sent[-1])

    def test_new_clears_the_session(self):
        self.bridge.chat(2)["session_id"] = "chat-1"
        self.bridge.on_text(1, 2, "/new", 3)
        self.assertEqual(self.bridge.chat(2)["session_id"], "")
        self.assertIn("fresh chat", self.telegram.sent[-1])

    def test_cancel_when_nothing_is_running(self):
        self.bridge.on_text(1, 2, "/cancel", 3)
        self.assertEqual(self.telegram.sent[-1], "Nothing is running.")

    def test_status_names_the_idle_run(self):
        self.bridge.on_text(1, 2, "/status", 3)
        shown = self.telegram.sent[-1]
        self.assertIn("Run idle", shown)
        self.assertIn("Cursor account test@example.com", shown)

    def test_a_newly_registered_command_is_dispatched(self):
        def extra(bridge, chat_id, user_id, rest, message_id):
            bridge.say(user_id, chat_id, f"extra {rest}")

        COMMANDS["/extra"] = extra
        try:
            self.bridge.on_text(1, 2, "/extra please", 3)
        finally:
            COMMANDS.pop("/extra", None)
        self.assertEqual(self.telegram.sent[-1], "extra please")


class CallbackDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bridge, self.telegram = make_bridge(Path(self.tmp.name), phones={"971500000000"})
        self.bridge.state.data["verified"] = {"5": "971500000000"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_end_clears_the_session_for_a_verified_user(self):
        self.bridge.chat(5)["session_id"] = "chat-1"
        self.bridge.on_callback({"id": "q1", "from": {"id": 5}, "message": {"chat": {"id": 9, "type": "private"}}, "data": "end"})
        self.assertEqual(self.bridge.chat(5)["session_id"], "")
        self.assertIn("fresh chat", self.telegram.sent[-1])

    def test_an_unverified_user_gets_no_button_action(self):
        self.bridge.on_callback({"from": {"id": 8}, "message": {"chat": {"id": 9, "type": "private"}}, "data": "end"})
        self.assertEqual(self.telegram.sent, [])

    def test_a_file_page_button_uses_the_saved_query(self):
        seen: list[tuple[str, int]] = []

        def show_files(chat_id, user_id, query, page):
            seen.append((query, page))

        self.bridge.chat(5)["file_query"] = "notes"
        self.bridge.show_files = show_files
        self.bridge.on_callback({"from": {"id": 5}, "message": {"chat": {"id": 9, "type": "private"}}, "data": "fp:2"})
        self.assertEqual(seen, [("notes", 2)])

    def test_a_stop_for_a_different_draft_does_not_cancel_the_run(self):
        self.bridge.active_draft_id = 7
        started = threading.Event()

        def run() -> None:
            started.set()
            self.bridge.runner.cancel.wait(2)

        self.assertTrue(self.bridge.runner.start(run))
        self.assertTrue(started.wait(1))
        self.bridge.on_generation_stopped({"draft_id": 9, "chat": {"id": 5, "type": "private"}})
        self.assertFalse(self.bridge.runner.cancel.is_set())
        self.bridge.on_generation_stopped({"draft_id": 7, "chat": {"id": 5, "type": "private"}})
        self.assertTrue(self.bridge.runner.cancel.is_set())
        self.bridge.runner.thread.join(2)

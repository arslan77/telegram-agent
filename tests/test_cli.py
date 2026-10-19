import json
import unittest

from app.activity import format_activity
from app.claude_stream import adapt_claude
from app.events import consume_event
from app.launch import run_argv
from app.transcript import Transcript
from tests.support import make_bridge
from tests.test_commands import CommandDispatchTests


class CliCommandTests(CommandDispatchTests):
    def test_cli_without_an_argument_reports_cursor(self):
        self.bridge.on_text(1, 2, "/cli", 3)
        self.assertEqual(self.telegram.sent[-1], "CLI is cursor.")

    def test_cli_claude_is_saved_for_the_machine_and_keeps_both_sessions(self):
        self.bridge.chat(2)["session_id"] = "cursor-chat"
        self.bridge.chat(2)["claude_session_id"] = "claude-chat"
        self.bridge.on_text(1, 2, "/cli claude", 3)
        self.assertEqual(self.bridge.state.data["cli"], "claude")
        self.assertEqual(self.bridge.chat(2)["session_id"], "cursor-chat")
        self.assertEqual(self.bridge.chat(2)["claude_session_id"], "claude-chat")
        self.assertIn("The next message uses it.", self.telegram.sent[-1])
        self.bridge.on_text(1, 2, "/status", 3)
        self.assertIn("CLI claude", self.telegram.sent[-1])

    def test_an_unknown_cli_does_not_change_the_saved_tool(self):
        self.bridge.on_text(1, 2, "/cli gemini", 3)
        self.assertNotIn("cli", self.bridge.state.data)
        self.assertIn("/cli cursor", self.telegram.sent[-1])

    def test_new_on_claude_clears_only_the_claude_session(self):
        self.bridge.state.data["cli"] = "claude"
        self.bridge.chat(2)["session_id"] = "cursor-chat"
        self.bridge.chat(2)["claude_session_id"] = "claude-chat"
        self.bridge.on_text(1, 2, "/new", 3)
        self.assertEqual(self.bridge.chat(2)["session_id"], "cursor-chat")
        self.assertEqual(self.bridge.chat(2)["claude_session_id"], "")


class LaunchTests(unittest.TestCase):
    def test_claude_run_uses_the_claude_binary_and_its_own_session(self):
        cmd = run_argv(
            "claude",
            binary="/usr/bin/claude",
            workspace="/home/a/lab",
            mode="agent",
            model="default",
            session_id="claude-1",
            prompt="hello",
        )
        self.assertEqual(cmd[0], "/usr/bin/claude")
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "bypassPermissions")
        self.assertEqual(cmd[cmd.index("--resume") + 1], "claude-1")
        self.assertNotIn("--model", cmd)
        self.assertNotIn("--workspace", cmd)
        self.assertEqual(cmd[-1], "hello")

    def test_claude_model_alias_is_passed(self):
        cmd = run_argv(
            "claude", binary="c", workspace="/w", mode="agent", model="sonnet", session_id="", prompt="hi"
        )
        self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")

    def test_model_list_is_parsed_and_grouped(self):
        from app.access import model_family, parse_model_list

        found = parse_model_list("Available models\n\nauto - Auto (default)\ncursor-grok-4.5-high - Grok 4.5\n")
        self.assertEqual([i for i, _ in found], ["auto", "cursor-grok-4.5-high"])
        self.assertEqual(model_family("cursor-grok-4.5-high"), "grok")

    def test_claude_effort_is_passed_only_when_set(self):
        base = dict(binary="c", workspace="/w", mode="agent", model="default", session_id="", prompt="hi")
        self.assertNotIn("--effort", run_argv("claude", **base))
        cmd = run_argv("claude", effort="high", **base)
        self.assertEqual(cmd[cmd.index("--effort") + 1], "high")
        self.assertNotIn("--effort", run_argv("cursor", effort="high", **base))

    def test_secret_files_are_never_sendable(self):
        from pathlib import Path
        from app.files import blocked_file

        for name in ("/home/a/.ssh/deploy", "/home/a/.config/cursor-telegram/env", "/home/a/x/server.pem",
                     "/home/a/state.json", "/home/a/.aws/config"):
            self.assertTrue(blocked_file(Path(name)), name)
        self.assertFalse(blocked_file(Path("/home/a/lab/notes.md")))

    def test_agent_env_drops_the_bot_secrets(self):
        import os
        import tempfile
        from pathlib import Path
        from unittest import mock

        with tempfile.TemporaryDirectory() as root:
            bridge, _ = make_bridge(Path(root))
            with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_ALLOWED_PHONES": "1"}):
                env = bridge.agent_env()
        self.assertNotIn("TELEGRAM_BOT_TOKEN", env)
        self.assertNotIn("TELEGRAM_ALLOWED_PHONES", env)

    def test_claude_ask_does_not_bypass_permissions(self):
        cmd = run_argv(
            "claude",
            binary="/usr/bin/claude",
            workspace="/home/a/lab",
            mode="ask",
            model="auto",
            session_id="",
            prompt="look",
        )
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "dontAsk")
        self.assertNotIn("--resume", cmd)

    def test_cursor_run_still_passes_the_model_and_workspace(self):
        cmd = run_argv(
            "cursor",
            binary="/usr/bin/agent",
            workspace="/home/a/lab",
            mode="plan",
            model="grok-4.7-high",
            session_id="chat-1",
            prompt="plan this",
        )
        self.assertEqual(cmd[0], "/usr/bin/agent")
        self.assertEqual(cmd[cmd.index("--workspace") + 1], "/home/a/lab")
        self.assertEqual(cmd[cmd.index("--mode") + 1], "plan")
        self.assertEqual(cmd[cmd.index("--model") + 1], "grok-4.7-high")
        self.assertEqual(cmd[cmd.index("--resume") + 1], "chat-1")
        self.assertEqual(cmd[-1], "plan this")


class ClaudeStreamTests(unittest.TestCase):
    def setUp(self):
        self.acc: dict = {}
        self.board = _Board()

    def feed(self, event: dict) -> None:
        for adapted in adapt_claude(json.dumps(event), self.acc):
            consume_event(json.dumps(adapted), self.acc, self.board)

    def test_a_text_delta_streams_into_the_answer(self):
        self.feed(
            {
                "type": "stream_event",
                "session_id": "sess-1",
                "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hello"}},
            }
        )
        self.assertEqual(self.board.transcript.render(), "hello")
        self.assertEqual(self.acc["session_id"], "sess-1")

    def test_a_tool_call_shows_the_file_it_reads(self):
        self.feed(
            {
                "type": "assistant",
                "session_id": "sess-1",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "Read",
                            "input": {"file_path": "/home/a/lab/note.txt"},
                        }
                    ]
                },
            }
        )
        self.feed(
            {
                "type": "user",
                "session_id": "sess-1",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "tool-1", "content": "hello-from-note"}
                    ]
                },
            }
        )
        self.assertEqual(self.board.tools[0], format_activity(self.board.calls[0], done=False))
        self.assertIn("reading note.txt", self.board.tools[0])
        self.assertIn("read note.txt", self.board.tools[1])


class _Board:
    def __init__(self) -> None:
        self.transcript = Transcript()
        self.tools: list[str] = []
        self.calls: list[object] = []

    def set_session(self, session_id: str) -> None:
        return None

    def stream(self, text: str) -> None:
        self.transcript.add_delta(text)

    def complete(self, text: str) -> None:
        self.transcript.add_complete(text)

    def show_tool(self, tool_call: object, *, done: bool) -> None:
        self.calls.append(tool_call)
        self.tools.append(format_activity(tool_call, done=done))

    def add_thinking(self, text: str, *, done: bool) -> None:
        return None


if __name__ == "__main__":
    unittest.main()

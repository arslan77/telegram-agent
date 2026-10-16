import json
import unittest

from app.board import LiveBoard
from app.events import assistant_text, consume_event
from app.transcript import Transcript


class AssistantTextTests(unittest.TestCase):
    def test_joins_text_blocks_and_strips_a_finished_message(self):
        event = {"message": {"content": [{"type": "text", "text": "  hello "}, {"type": "text", "text": "there"}]}}
        self.assertEqual(assistant_text(event), "hello there")

    def test_a_streamed_chunk_keeps_its_leading_space(self):
        event = {"message": {"content": [{"type": "text", "text": " can't"}]}}
        self.assertEqual(assistant_text(event, raw=True), " can't")

    def test_a_missing_message_is_empty(self):
        self.assertEqual(assistant_text({}), "")


class ConsumeEventTests(unittest.TestCase):
    def test_invalid_json_is_ignored(self):
        acc: dict = {}
        consume_event("not json", acc, None)
        self.assertEqual(acc, {})

    def test_a_streamed_assistant_message_keeps_each_leading_space(self):
        class Board:
            def __init__(self) -> None:
                self.transcript = Transcript()

            def set_session(self, session_id: str) -> None:
                return None

            def stream(self, text: str) -> None:
                self.transcript.add_delta(text)

            def complete(self, text: str) -> None:
                self.transcript.add_complete(text)

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

    def test_an_assistant_event_with_a_model_call_id_is_not_the_answer(self):
        class Board:
            def __init__(self) -> None:
                self.calls: list[str] = []
                self.transcript = Transcript()

            def stream(self, text: str) -> None:
                self.calls.append(text)

            def complete(self, text: str) -> None:
                self.calls.append(text)

        board = Board()
        consume_event(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp_ms": 1,
                    "model_call_id": "m1",
                    "message": {"content": [{"type": "text", "text": "hidden"}]},
                }
            ),
            {},
            board,
        )
        self.assertEqual(board.calls, [])

    def test_a_finished_assistant_message_is_stored_when_there_is_no_board(self):
        acc: dict = {}
        consume_event(
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Done."}]}}),
            acc,
            None,
        )
        self.assertEqual(acc["last"], "Done.")

    def test_a_tool_event_records_the_activity_line(self):
        acc: dict = {}
        consume_event(
            json.dumps(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "tool_call": {"shellToolCall": {"args": {"command": "echo hi"}}},
                }
            ),
            acc,
            None,
        )
        self.assertEqual(acc["tool"], "🟡 shell: echo hi")

    def test_a_completed_tool_is_kept_for_a_later_send(self):
        acc: dict = {}
        tool = {"readToolCall": {"args": {"path": "README.md"}}}
        consume_event(json.dumps({"type": "tool_call", "subtype": "completed", "tool_call": tool}), acc, None)
        self.assertEqual(acc["pending_tool"], tool)
        self.assertEqual(acc["tool"], "✅ read README.md")

    def test_a_result_event_stores_the_text_and_the_error_flag(self):
        acc: dict = {}
        consume_event(json.dumps({"type": "result", "result": "ok", "is_error": False, "duration_ms": 12}), acc, None)
        self.assertEqual(acc["result"], "ok")
        self.assertFalse(acc["is_error"])
        self.assertEqual(acc["duration_ms"], 12)


class ThinkingEventTests(unittest.TestCase):
    def setUp(self):
        self.board = LiveBoard(None, 1, lambda message_id: None, model="auto", project="lab")
        self.board.publish = lambda force=False: None

    def test_deltas_join_into_one_thought_and_set_the_session(self):
        consume_event(json.dumps({"type": "thinking", "subtype": "delta", "text": "Look at ", "session_id": "abcd1234"}), {}, self.board)
        consume_event(json.dumps({"type": "thinking", "subtype": "delta", "text": "the heading.", "session_id": "abcd1234"}), {}, self.board)
        consume_event(json.dumps({"type": "thinking", "subtype": "completed", "session_id": "abcd1234"}), {}, self.board)
        self.assertEqual(self.board.thinking, "Look at the heading.")
        self.assertEqual(self.board.session_label, "abcd1234")
        self.assertFalse(self.board.thinking_open)

    def test_a_later_round_is_separated_from_the_first(self):
        self.board.add_thinking("First.", done=False)
        self.board.add_thinking("", done=True)
        self.board.add_thinking("Second.", done=False)
        self.assertEqual(self.board.thinking, "First.\n\nSecond.")

    def test_an_empty_delta_does_not_open_a_thought(self):
        self.board.add_thinking("", done=False)
        self.assertEqual(self.board.thinking, "")
        self.assertFalse(self.board.thinking_open)

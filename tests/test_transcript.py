import unittest

from app.chatlog import forget_messages, message_ids_for_clear
from app.transcript import Transcript, split_live


class TranscriptTests(unittest.TestCase):
    def test_a_tool_line_is_recorded_once(self):
        log = Transcript()
        self.assertTrue(log.add_tool("Reading README.md"))
        self.assertFalse(log.add_tool("Reading README.md"))
        self.assertEqual(log.render(), "Reading README.md")

    def test_deltas_append_and_a_matching_complete_is_ignored(self):
        log = Transcript()
        self.assertTrue(log.add_delta("Hello"))
        self.assertTrue(log.add_delta(" there"))
        self.assertEqual(log.render(), "Hello there")
        self.assertFalse(log.add_complete("Hello there"))
        self.assertEqual(log.render(), "Hello there")

    def test_an_empty_delta_does_not_change_the_transcript(self):
        log = Transcript()
        self.assertFalse(log.add_delta(""))
        self.assertEqual(log.render(), "")

    def test_a_new_complete_message_is_appended_after_the_stream_is_committed(self):
        log = Transcript()
        log.add_delta("Hello")
        log.add_complete("Hello")
        self.assertTrue(log.add_complete("Next paragraph"))
        self.assertEqual(log.render(), "Hello\n\nNext paragraph")


class SplitLiveTests(unittest.TestCase):
    def test_a_short_text_stays_in_one_piece(self):
        head, tail = split_live("hello", 10)
        self.assertEqual(head, "hello")
        self.assertEqual(tail, "")

    def test_a_long_text_starts_a_second_piece_on_a_line_break(self):
        head, tail = split_live("one\ntwo\nthree\nfour", 10)
        self.assertLessEqual(len(head), 10)
        self.assertTrue(tail)
        self.assertNotIn(tail.strip(), head)


class ChatlogTests(unittest.TestCase):
    def test_clear_collects_integer_ids_and_the_command_message(self):
        row = {"message_ids": [4, 4, "nope", 9]}
        self.assertEqual(message_ids_for_clear(row, 11), [4, 9, 11])

    def test_clear_does_not_repeat_the_command_id_when_it_is_already_stored(self):
        self.assertEqual(message_ids_for_clear({"message_ids": [11]}, 11), [11])

    def test_forget_clears_message_ids_and_leaves_the_session(self):
        row = {"session_id": "chat-1", "workspace": "/home/a/lab", "mode": "agent", "message_ids": [4, 9]}
        kept = forget_messages(row)
        self.assertEqual(kept["session_id"], "chat-1")
        self.assertEqual(kept["workspace"], "/home/a/lab")
        self.assertEqual(kept["message_ids"], [])
        self.assertEqual(row["message_ids"], [4, 9])

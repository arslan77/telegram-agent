import unittest

from app.board import LiveBoard
from app.constants import RICH_LIMIT


class RejectingApi:
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


class AcceptingApi:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def send_rich(self, chat_id, markdown, reply_markup=None, reply_to=None):
        self.calls.append("rich")
        self.markdown = markdown
        self.markup = reply_markup
        return 21

    def edit_rich(self, chat_id, message_id, markdown, reply_markup=None):
        self.calls.append("edit-rich")
        self.markdown = markdown
        self.markup = reply_markup

    def send_one(self, *args, **kwargs):
        raise AssertionError("html fallback")

    def edit(self, *args, **kwargs):
        raise AssertionError("html edit")


class SplittingApi:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.edits: list[tuple[int, str]] = []
        self.next_id = 1

    def send_rich(self, chat_id, markdown, reply_markup=None, reply_to=None):
        self.sent.append(markdown)
        message_id = self.next_id
        self.next_id += 1
        return message_id

    def edit_rich(self, chat_id, message_id, markdown, reply_markup=None):
        self.edits.append((message_id, markdown))

    def send_one(self, *args, **kwargs):
        raise AssertionError("html fallback")

    def edit(self, *args, **kwargs):
        raise AssertionError("html edit")


class RichFallbackTests(unittest.TestCase):
    def test_a_rejected_rich_message_is_sent_as_html(self):
        api = RejectingApi()
        board = LiveBoard(api, 1, lambda message_id: None, model="auto", project="lab")
        board.publish(force=True)
        self.assertEqual(api.calls, ["rich", "html"])
        self.assertTrue(api.html)
        self.assertIn("<blockquote>", api.text)
        self.assertFalse(board.use_rich)

    def test_a_rejected_finish_also_falls_back_to_html(self):
        api = RejectingApi()
        board = LiveBoard(api, 1, lambda message_id: None, model="auto", project="lab")
        board.publish(force=True)
        board.finish("Done.\n\n# Kept", "done")
        self.assertEqual(api.calls[:4], ["rich", "html", "edit-rich", "edit-html"])
        self.assertFalse(board.use_rich)


class RichEditTests(unittest.TestCase):
    def test_an_accepted_run_edits_the_same_rich_message(self):
        api = AcceptingApi()
        board = LiveBoard(api, 1, lambda message_id: None, model="auto", project="lab")
        board.publish(force=True)
        board.finish("# Answer\n\n- done", "done")
        self.assertEqual(api.calls, ["rich", "edit-rich"])
        self.assertIn("# Answer", api.markdown)
        self.assertIn("- done", api.markdown)
        self.assertNotIn("tg-thinking", api.markdown)
        self.assertIn("> **", api.markdown)
        self.assertIn('style="primary" data="diff"', api.markdown)
        self.assertIn('style="danger" data="end"', api.markdown)
        self.assertIsNone(api.markup)


class RichSplitTests(unittest.TestCase):
    def test_a_long_answer_stays_in_the_first_message_and_continues_in_a_second(self):
        api = SplittingApi()
        board = LiveBoard(api, 1, lambda message_id: None, model="auto", project="lab")
        board.final_body = "alpha\n\n" + ("word " * 8000)
        board.phase = "done"
        board.publish(force=True)
        self.assertGreaterEqual(len(api.sent), 2)
        first = api.sent[0]
        self.assertIn("alpha", first)
        self.assertLessEqual(len(first), RICH_LIMIT)
        board.final_body += "\n\nmore"
        board.publish(force=True)
        self.assertEqual(api.sent[0], first)
        self.assertTrue(api.edits)
        self.assertNotEqual(api.edits[-1][0], 1)

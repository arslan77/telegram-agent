import unittest

from app.constants import RICH_LIMIT
from app.rich import build_run_rich_markdown


def rich(**overrides) -> str:
    fields = {
        "model": "grok-4.7-high",
        "project": "lab",
        "session": "abcd1234",
        "elapsed": 2,
        "body": "",
        "activity": "",
        "snippet": "",
        "phase": "done",
    }
    fields.update(overrides)
    return build_run_rich_markdown(**fields)


class RichHeadingTests(unittest.TestCase):
    def test_the_heading_is_one_quote_and_not_a_table(self):
        shown = rich(model="grok_4", project="lab_notes", elapsed=4, body="Done.", phase="done")
        self.assertIn("> **[abcd1234] lab\\_notes** · `grok_4` · ⌛ 4s", shown)
        self.assertNotIn("| Session |", shown)

    def test_a_finished_answer_keeps_markdown_and_the_action_buttons(self):
        shown = rich(
            model="grok_4",
            project="lab_notes",
            elapsed=4,
            body="# Plan\n\n- one\n- [x] two\n\n```python\nprint(1)\n```\n\n| Item | State |\n|:-----|:-----:|\n| queue | done |",
            phase="done",
        )
        self.assertIn('style="danger" data="end"', shown)
        self.assertIn('style="primary" data="diff"', shown)
        self.assertIn("# Plan", shown)
        self.assertIn("- [x] two", shown)
        self.assertIn("```python\nprint(1)\n```", shown)
        self.assertIn("| queue | done |", shown)
        self.assertIn("✅", shown)
        self.assertNotIn("<b>", shown)

    def test_a_running_message_has_no_action_buttons(self):
        shown = rich(phase="running", body="Working on it.")
        self.assertNotIn("tg-button", shown)

    def test_a_continued_message_names_itself_instead_of_repeating_the_heading(self):
        shown = rich(body="more", continued=True)
        self.assertTrue(shown.startswith("> Continued"))
        self.assertNotIn("[abcd1234]", shown)


class RichBodyTests(unittest.TestCase):
    def test_an_answer_longer_than_the_html_cap_is_kept_whole(self):
        body = "word " * 1200
        shown = rich(body=body, elapsed=2)
        self.assertGreater(len(shown), 4096)
        self.assertLessEqual(len(shown), RICH_LIMIT)
        self.assertIn(body.strip(), shown)

    def test_a_live_code_fence_is_closed_and_the_activity_is_escaped(self):
        shown = rich(
            phase="running",
            elapsed=1,
            body="```python\nprint(1)",
            activity="🟡 reading file_name.py",
            snippet="print(1)",
        )
        self.assertEqual(shown.count("```") % 2, 0)
        self.assertIn("> *🟡 reading file\\_name.py*", shown)
        self.assertIn("print(1)", shown)
        self.assertNotIn("tg-thinking", shown)

    def test_a_draft_with_no_answer_uses_the_thinking_tag(self):
        shown = rich(phase="running", elapsed=1, activity="🟡 reading bot.py", draft=True)
        self.assertIn("<tg-thinking>🟡 reading bot.py</tg-thinking>", shown)

    def test_finished_steps_fold_and_a_saved_message_has_no_thinking_tag(self):
        shown = rich(phase="done", elapsed=1, body="Done.", tools=["✅ read bot.py\n\n```\nprint(1)\n```"])
        self.assertIn("<details><summary>What Cursor did</summary>", shown)
        self.assertIn("- ✅ read bot.py", shown)
        self.assertNotIn("tg-thinking", shown)


class RichThinkingTests(unittest.TestCase):
    def test_thinking_is_open_under_the_heading_before_the_answer(self):
        shown = rich(phase="running", elapsed=2, thinking="Check the heading first.")
        self.assertLess(shown.index("> **[abcd1234]"), shown.index("> **Thinking**"))
        self.assertLess(shown.index("> **Thinking**"), shown.index("Check the heading first."))
        self.assertNotIn("tg-thinking", shown)
        self.assertNotIn("<details>", shown)

    def test_thinking_folds_once_the_answer_starts(self):
        shown = rich(phase="running", elapsed=3, body="The heading stays.", thinking="Check the heading first.")
        self.assertIn("<details><summary>Thinking</summary>", shown)
        self.assertLess(shown.index("<details>"), shown.index("The heading stays."))

    def test_a_finished_answer_keeps_thinking_folded(self):
        shown = rich(phase="done", elapsed=4, body="The heading stays.", thinking="Check the heading first.")
        self.assertIn("<details><summary>Thinking</summary>", shown)
        self.assertNotIn("> **Thinking**", shown)

    def test_a_details_tag_inside_thinking_cannot_close_the_fold(self):
        shown = rich(phase="done", body="Answer.", thinking="before </details> after")
        self.assertEqual(shown.count("<details>"), shown.count("</details>"))
        self.assertIn("before", shown)
        self.assertIn("after", shown)

import unittest

from app.html_text import build_run_html, format_bubble, message_header


class MessageHeaderTests(unittest.TestCase):
    def test_a_model_alone_is_a_bold_quote(self):
        self.assertEqual(
            message_header("grok-4.7-high"),
            "<blockquote><b>grok-4.7-high</b></blockquote>",
        )

    def test_session_folder_and_model_share_one_quote(self):
        shown = message_header("grok-4.7-high", project="lab", session="abcd1234", elapsed=12)
        self.assertIn("[abcd1234]", shown)
        self.assertIn("lab", shown)
        self.assertIn("<code>grok-4.7-high</code>", shown)
        self.assertIn("⏳ 12s", shown)

    def test_the_clock_flips_on_the_odd_five_second_slot(self):
        shown = message_header("auto", elapsed=5, running=True)
        self.assertIn("⌛ 5s", shown)

    def test_a_finished_run_uses_the_still_hourglass(self):
        shown = message_header("auto", elapsed=12, running=False)
        self.assertIn("⌛ 12s", shown)


class FormatBubbleTests(unittest.TestCase):
    def test_a_running_line_becomes_a_bash_block_and_a_url_becomes_a_link(self):
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

    def test_headings_lists_links_and_fences_keep_their_structure(self):
        shown = format_bubble(
            "# Plan\n\n- one\n- [x] two\n\nSee [docs](https://example.com/docs)\n\n```python\nprint(1)\n```"
        )
        self.assertIn("<b>Plan</b>", shown)
        self.assertIn("• one", shown)
        self.assertIn("• ☑ two", shown)
        self.assertIn('<a href="https://example.com/docs">docs</a>', shown)
        self.assertIn('<pre><code class="language-python">print(1)</code></pre>', shown)

    def test_an_edit_snippet_is_a_code_block(self):
        shown = format_bubble("Editing bot.py\n\n```\n🔴 -alpha\n🟢 +beta\n```")
        self.assertIn("Editing <code>bot.py</code>", shown)
        self.assertIn("<pre><code>🔴 -alpha\n🟢 +beta</code></pre>", shown)


class BuildRunHtmlTests(unittest.TestCase):
    def _view(self, **overrides):
        fields = {
            "model": "grok-4.7-high",
            "project": "lab",
            "session": "abcd1234",
            "elapsed": 3,
            "body": "",
            "activity": "",
            "snippet": "",
            "phase": "running",
        }
        fields.update(overrides)
        return build_run_html(**fields)

    def test_a_running_view_shows_the_answer_the_activity_and_the_snippet(self):
        shown = self._view(body="Opening the tunnel.", activity="🟡 shell: echo hi", snippet="echo hi")
        self.assertIn("Opening the tunnel.", shown)
        self.assertIn("🟡 shell: echo hi", shown)
        self.assertIn("<pre><code>echo hi</code></pre>", shown)

    def test_an_empty_running_view_says_working(self):
        self.assertIn("<i>working…</i>", self._view())

    def test_thinking_shows_before_the_answer_starts(self):
        shown = self._view(thinking="Check the heading first.")
        self.assertIn("Check the heading first.", shown)
        self.assertNotIn("working…", shown)

    def test_a_finished_answer_drops_the_live_activity(self):
        shown = self._view(phase="done", elapsed=8, body="The tunnel is up.", activity="🟡 shell: echo hi", snippet="echo hi")
        self.assertIn("✅", shown)
        self.assertIn("The tunnel is up.", shown)
        self.assertNotIn("shell: echo hi", shown)
        self.assertNotIn("<pre><code>echo hi</code></pre>", shown)

    def test_a_cancelled_run_uses_the_stop_icon(self):
        self.assertIn("⛔", self._view(phase="cancelled", body="Stopped."))

    def test_an_error_with_no_body_says_there_was_no_output(self):
        shown = self._view(phase="error", body="  ")
        self.assertIn("🔴", shown)
        self.assertIn("(no output)", shown)

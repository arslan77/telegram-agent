import unittest

from app.activity import format_activity, format_snippet, tool_entry, tool_label, tool_phrase


def call(name: str, args: dict, result: dict | None = None) -> dict:
    body: dict = {"args": args}
    if result is not None:
        body["result"] = {"success": result}
    return {name: body}


class ToolLabelTests(unittest.TestCase):
    def test_a_write_label_names_the_file_and_drops_the_body(self):
        label = tool_label(call("writeToolCall", {"path": "a.py", "fileText": "SECRET TOKEN"}))
        self.assertEqual(label, "write a.py")
        self.assertNotIn("SECRET", label)

    def test_a_read_phrase_uses_the_file_name_only(self):
        phrase = tool_phrase(call("readToolCall", {"path": "/home/a/README.md", "fileText": "SECRET"}))
        self.assertEqual(phrase, "Reading README.md")
        self.assertNotIn("SECRET", phrase)

    def test_an_edit_entry_shows_removed_and_added_lines_only(self):
        entry = tool_entry(
            call(
                "strReplaceToolCall",
                {"path": "bot.py", "old_string": "alpha", "new_string": "beta", "fileText": "SECRET TOKEN"},
            )
        )
        self.assertIn("Editing bot.py", entry)
        self.assertIn("🔴 -alpha", entry)
        self.assertIn("🟢 +beta", entry)
        self.assertNotIn("SECRET", entry)


class FormatActivityTests(unittest.TestCase):
    def test_read_uses_the_running_and_finished_verbs(self):
        tool = call("readToolCall", {"path": "/home/a/bot.py"})
        self.assertEqual(format_activity(tool, done=False), "🟡 reading bot.py")
        self.assertEqual(format_activity(tool, done=True), "✅ read bot.py")

    def test_write_uses_the_running_and_finished_verbs(self):
        tool = call("writeToolCall", {"path": "note.md"})
        self.assertEqual(format_activity(tool, done=False), "🟡 writing note.md")
        self.assertEqual(format_activity(tool, done=True), "✅ wrote note.md")

    def test_edit_uses_the_running_and_finished_verbs(self):
        tool = call("editToolCall", {"path": "bot.py"})
        self.assertEqual(format_activity(tool, done=False), "🟡 editing bot.py")
        self.assertEqual(format_activity(tool, done=True), "✅ edited bot.py")

    def test_delete_uses_the_running_and_finished_verbs(self):
        tool = call("deleteToolCall", {"path": "old.py"})
        self.assertEqual(format_activity(tool, done=False), "🟡 deleting old.py")
        self.assertEqual(format_activity(tool, done=True), "✅ deleted old.py")

    def test_shell_keeps_a_short_command(self):
        tool = call("shellToolCall", {"command": "echo hi"})
        self.assertEqual(format_activity(tool, done=False), "🟡 shell: echo hi")
        self.assertEqual(format_activity(tool, done=True), "✅ shell: echo hi")

    def test_shell_clips_a_long_command(self):
        tool = call("shellToolCall", {"command": "x" * 80})
        shown = format_activity(tool, done=False)
        self.assertTrue(shown.startswith("🟡 shell: "))
        self.assertLessEqual(len(shown), len("🟡 shell: ") + 60)
        self.assertTrue(shown.endswith("…"))

    def test_grep_shows_the_pattern(self):
        tool = call("grepToolCall", {"pattern": "LiveBoard"})
        self.assertEqual(format_activity(tool, done=False), "🟡 grep: LiveBoard")

    def test_listing_uses_the_folder_name(self):
        tool = call("lsToolCall", {"path": "/home/a/lab"})
        self.assertEqual(format_activity(tool, done=False), "🟡 listing lab")
        self.assertEqual(format_activity(tool, done=True), "✅ listed lab")

    def test_an_empty_tool_is_just_working(self):
        self.assertEqual(format_activity({}, done=False), "🟡 working…")

    def test_an_unknown_tool_keeps_its_name(self):
        self.assertEqual(format_activity(call("webSearchToolCall", {}), done=False), "🟡 webSearch")


class FormatSnippetTests(unittest.TestCase):
    def test_shell_output_marks_stdout_and_stderr(self):
        snippet = format_snippet(
            call("shellToolCall", {"command": "echo hi"}, {"stdout": "hi\n", "stderr": "warn\n", "exitCode": 0})
        )
        self.assertIn("🟢 hi", snippet)
        self.assertIn("🔴 warn", snippet)

    def test_a_unified_diff_uses_hunk_and_line_marks(self):
        snippet = format_snippet(
            call(
                "shellToolCall",
                {"command": "git diff"},
                {"stdout": "@@ -1 +1 @@\n-old\n+new\n", "stderr": "", "exitCode": 0},
            )
        )
        self.assertIn("🔷 @@ -1 +1 @@", snippet)
        self.assertIn("🔴 -old", snippet)
        self.assertIn("🟢 +new", snippet)

    def test_a_failed_shell_shows_the_exit_code(self):
        snippet = format_snippet(call("shellToolCall", {"command": "false"}, {"stdout": "", "stderr": "", "exitCode": 1}))
        self.assertIn("🔴 exit 1", snippet)

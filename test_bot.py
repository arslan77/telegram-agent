import tempfile
import unittest
from pathlib import Path

from bot import (
    ConfigError,
    contact_phone,
    is_cloud_handoff,
    mask_phone,
    parse_allowlist,
    parse_command,
    redact,
    resolve_workspace,
    split_text,
    tool_label,
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

    def test_split_and_redact(self):
        parts = split_text("a\n" + ("b" * 4000), limit=100)
        self.assertGreater(len(parts), 1)
        self.assertNotIn("super-secret", redact("token super-secret here", "super-secret"))


if __name__ == "__main__":
    unittest.main()

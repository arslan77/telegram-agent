import os
import tempfile
import unittest
from pathlib import Path

from app.access import (
    ConfigError,
    contact_phone,
    cursor_model,
    default_workspace,
    is_cloud_handoff,
    mask_phone,
    parse_allowlist,
    parse_command,
    redact,
    resolve_workspace,
    split_text,
)


class ParseAllowlistTests(unittest.TestCase):
    def test_keeps_digits_from_spaced_international_numbers(self):
        self.assertEqual(parse_allowlist("+971 50 123 4567, 14155552671"), {"971501234567", "14155552671"})

    def test_accepts_a_semicolon_separator(self):
        self.assertEqual(parse_allowlist("971501234567;14155552671"), {"971501234567", "14155552671"})

    def test_collapses_the_same_number_written_twice(self):
        self.assertEqual(parse_allowlist("971501234567, +971501234567"), {"971501234567"})

    def test_accepts_the_shortest_and_longest_international_lengths(self):
        self.assertEqual(parse_allowlist("12345678, 123456789012345"), {"12345678", "123456789012345"})

    def test_rejects_a_number_shorter_than_eight_digits(self):
        with self.assertRaises(ConfigError):
            parse_allowlist("1234567")

    def test_rejects_a_number_longer_than_fifteen_digits(self):
        with self.assertRaises(ConfigError):
            parse_allowlist("1234567890123456")

    def test_rejects_an_empty_list(self):
        with self.assertRaises(ConfigError):
            parse_allowlist(" , ")


class MaskPhoneTests(unittest.TestCase):
    def test_hides_the_middle_and_keeps_the_ends(self):
        masked = mask_phone("971501234567")
        self.assertEqual(masked, "971" + ("•" * 7) + "67")
        self.assertNotIn("5012345", masked)

    def test_hides_a_very_short_value_completely(self):
        self.assertEqual(mask_phone("1234"), "••••")


class ContactPhoneTests(unittest.TestCase):
    def test_accepts_the_sender_own_contact(self):
        message = {
            "from": {"id": 7, "is_bot": False},
            "contact": {"user_id": 7, "phone_number": "+971501234567"},
        }
        self.assertEqual(contact_phone(message), "971501234567")

    def test_rejects_someone_elses_contact(self):
        message = {
            "from": {"id": 7, "is_bot": False},
            "contact": {"user_id": 8, "phone_number": "+971501234567"},
        }
        self.assertIsNone(contact_phone(message))

    def test_rejects_a_contact_with_no_telegram_user(self):
        message = {
            "from": {"id": 7, "is_bot": False},
            "contact": {"phone_number": "+971501234567"},
        }
        self.assertIsNone(contact_phone(message))

    def test_rejects_a_contact_sent_by_a_bot(self):
        message = {
            "from": {"id": 7, "is_bot": True},
            "contact": {"user_id": 7, "phone_number": "+971501234567"},
        }
        self.assertIsNone(contact_phone(message))

    def test_rejects_a_phone_that_is_too_short(self):
        message = {
            "from": {"id": 7, "is_bot": False},
            "contact": {"user_id": 7, "phone_number": "12345"},
        }
        self.assertIsNone(contact_phone(message))


class ParseCommandTests(unittest.TestCase):
    def test_strips_the_bot_name_and_lowercases_the_command(self):
        self.assertEqual(parse_command("  /Mode@my_bot ask  "), ("/mode", "ask"))

    def test_plain_text_is_a_prompt(self):
        self.assertEqual(parse_command("fix the tests"), (None, "fix the tests"))

    def test_a_command_without_an_argument_has_an_empty_rest(self):
        self.assertEqual(parse_command("/status"), ("/status", ""))

    def test_leading_ampersand_is_a_cloud_handoff(self):
        self.assertTrue(is_cloud_handoff("  & refactor this"))

    def test_an_ampersand_inside_the_sentence_is_a_prompt(self):
        self.assertFalse(is_cloud_handoff("refactor this & that"))


class WorkspacePathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        (self.home / "lab").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_tilde_slash_stays_inside_home(self):
        self.assertEqual(resolve_workspace("~/lab", self.home), (self.home / "lab").resolve())

    def test_a_relative_folder_is_resolved_from_home(self):
        self.assertEqual(resolve_workspace("lab", self.home), (self.home / "lab").resolve())

    def test_a_bare_tilde_is_home(self):
        self.assertEqual(resolve_workspace("~", self.home), self.home.resolve())

    def test_a_path_outside_home_is_rejected(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        with self.assertRaises(ValueError):
            resolve_workspace(str(outside), self.home)

    def test_a_symlink_that_leaves_home_is_rejected(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (self.home / "link").symlink_to(outside)
        with self.assertRaises(ValueError):
            resolve_workspace("link", self.home)

    def test_a_missing_folder_is_rejected(self):
        with self.assertRaises(ValueError):
            resolve_workspace("~/missing", self.home)

    def test_a_file_is_not_a_workspace(self):
        (self.home / "notes.txt").write_text("hi")
        with self.assertRaises(ValueError):
            resolve_workspace("notes.txt", self.home)

    def test_default_workspace_is_home_when_unset(self):
        previous = os.environ.pop("CURSOR_WORKSPACE", None)
        try:
            self.assertEqual(default_workspace(self.home), self.home.resolve())
        finally:
            if previous is not None:
                os.environ["CURSOR_WORKSPACE"] = previous

    def test_default_workspace_uses_cursor_workspace_when_set(self):
        previous = os.environ.get("CURSOR_WORKSPACE")
        os.environ["CURSOR_WORKSPACE"] = str(self.home / "lab")
        try:
            self.assertEqual(default_workspace(self.home), (self.home / "lab").resolve())
        finally:
            if previous is None:
                os.environ.pop("CURSOR_WORKSPACE", None)
            else:
                os.environ["CURSOR_WORKSPACE"] = previous

    def test_cursor_model_falls_back_to_auto(self):
        previous = os.environ.pop("CURSOR_MODEL", None)
        try:
            self.assertEqual(cursor_model(), "auto")
        finally:
            if previous is not None:
                os.environ["CURSOR_MODEL"] = previous


class TextLimitTests(unittest.TestCase):
    def test_split_text_breaks_on_a_newline_inside_the_limit(self):
        parts = split_text("a\n" + ("b" * 4000), limit=100)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part) <= 100 for part in parts))
        self.assertEqual("".join(parts).replace("\n", ""), "a" + ("b" * 4000))

    def test_empty_text_still_sends_a_finished_note(self):
        self.assertEqual(split_text("   "), ["Finished, with no text."])

    def test_redact_replaces_only_the_secret(self):
        self.assertEqual(redact("token super-secret here", "super-secret"), "token *** here")

    def test_redact_leaves_text_alone_when_there_is_no_secret(self):
        self.assertEqual(redact("hello", ""), "hello")

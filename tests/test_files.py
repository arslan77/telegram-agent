import tempfile
import unittest
from pathlib import Path

from app.files import (
    attachment_from_message,
    changed_media,
    file_page,
    inbound_prompt,
    inside_workspace,
    list_sendable,
    output_paths,
    safe_filename,
)


class SafeFilenameTests(unittest.TestCase):
    def test_directories_are_dropped(self):
        self.assertEqual(safe_filename("../../etc/passwd"), "passwd")

    def test_a_slash_cannot_remain_in_the_name(self):
        self.assertNotIn("/", safe_filename("a/b.mp3"))

    def test_an_empty_name_becomes_file(self):
        self.assertEqual(safe_filename("///"), "file")


class AttachmentTests(unittest.TestCase):
    def test_a_photo_uses_the_largest_size_and_keeps_the_caption(self):
        attachment = attachment_from_message(
            {
                "caption": "what is this",
                "photo": [
                    {"file_id": "small", "file_size": 10},
                    {"file_id": "large", "file_size": 99},
                ],
            }
        )
        self.assertEqual(attachment["file_id"], "large")
        self.assertEqual(attachment["name"], "photo.jpg")
        self.assertEqual(attachment["caption"], "what is this")

    def test_a_voice_note_gets_an_ogg_name(self):
        attachment = attachment_from_message({"voice": {"file_id": "v1", "file_size": 4}})
        self.assertEqual(attachment["name"], "voice.ogg")
        self.assertEqual(attachment["file_id"], "v1")

    def test_a_document_keeps_its_file_name(self):
        attachment = attachment_from_message({"document": {"file_id": "d1", "file_name": "notes.txt"}})
        self.assertEqual(attachment["name"], "notes.txt")

    def test_a_text_message_has_no_attachment(self):
        self.assertIsNone(attachment_from_message({"text": "hello"}))


class InboundPromptTests(unittest.TestCase):
    def test_an_image_asks_for_a_description_when_there_is_no_caption(self):
        image = inbound_prompt(Path("/home/a/lab/.cursor-telegram/inbound/pic.jpg"), "")
        self.assertIn("Read this file and look at it", image)
        self.assertIn("Describe what you see.", image)

    def test_audio_includes_the_caption(self):
        audio = inbound_prompt(Path("/home/a/lab/.cursor-telegram/inbound/voice.ogg"), "transcribe this")
        self.assertIn("audio file", audio)
        self.assertIn("transcribe this", audio)
        self.assertNotIn("Describe what you see.", audio)

    def test_a_plain_file_asks_for_a_look(self):
        note = inbound_prompt(Path("/home/a/lab/notes.txt"), "")
        self.assertIn("The user sent a file:", note)
        self.assertIn("Look at it and respond.", note)


class OutputPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_read_does_not_send_the_file_back(self):
        image = self.root / "pic.png"
        image.write_bytes(b"png")
        self.assertEqual(output_paths({"readToolCall": {"args": {"path": str(image)}}}, self.root), [])

    def test_a_secret_file_is_not_sent(self):
        secret = self.root / ".env"
        secret.write_bytes(b"TOKEN=1")
        self.assertEqual(output_paths({"writeToolCall": {"args": {"path": str(secret)}}}, self.root), [])

    def test_a_generated_image_inside_the_workspace_is_sent(self):
        image = self.root / "pic.png"
        image.write_bytes(b"png")
        made = {
            "generateImageToolCall": {
                "args": {"file_path": "pic.png"},
                "result": {"success": {"file_path": "pic.png"}},
            }
        }
        self.assertEqual(output_paths(made, self.root), [image.resolve()])

    def test_a_path_that_leaves_the_workspace_is_rejected(self):
        self.assertIsNone(inside_workspace(self.root, "../escape.png"))


class FileListTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_audio_is_found_and_secrets_are_skipped(self):
        (self.root / ".env").write_bytes(b"SECRET=1")
        (self.root / ".git").mkdir()
        (self.root / ".git" / "song.mp3").write_bytes(b"nope")
        note = self.root / "note.mp3"
        note.write_bytes(b"abc")
        self.assertEqual(changed_media({}, self.root), [note.resolve()])
        self.assertEqual(list_sendable(self.root, "note"), [note.resolve()])
        self.assertEqual(list_sendable(self.root, ".env"), [])

    def test_the_first_page_names_the_file(self):
        note = self.root / "note.mp3"
        note.write_bytes(b"abc")
        page, text = file_page([note.resolve()], 0, self.root)
        self.assertEqual(page, [note.resolve()])
        self.assertIn("note.mp3", text)

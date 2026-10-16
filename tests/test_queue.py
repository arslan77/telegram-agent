import tempfile
import threading
import unittest
from pathlib import Path

from app.bridge import Prompt
from app.constants import MAX_QUEUE, REACTION_CANCELLED, REACTION_DONE, REACTION_QUEUED, REACTION_WORKING
from tests.support import make_bridge


class QueueTests(unittest.TestCase):
    def test_an_idle_submit_starts_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, telegram = make_bridge(Path(tmp))
            ran: list[str] = []

            def execute(chat_id, user_id, text, message_id=None):
                ran.append(text)
                return "done"

            bridge.execute = execute
            bridge.submit(1, 2, "now", 10)
            bridge.runner.thread.join(3)
            self.assertEqual(ran, ["now"])
            self.assertEqual(bridge.queued(), 0)
            self.assertIn((10, REACTION_WORKING), telegram.reactions)
            self.assertIn((10, REACTION_DONE), telegram.reactions)
            self.assertNotIn((10, REACTION_QUEUED), telegram.reactions)

    def test_a_second_message_waits_and_is_not_cancelled_with_the_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, telegram = make_bridge(Path(tmp))
            started = threading.Event()
            release = threading.Event()
            seen: list[tuple[str, bool]] = []

            def execute(chat_id, user_id, text, message_id=None):
                seen.append((text, bridge.runner.cancel.is_set()))
                if text == "first":
                    started.set()
                    self.assertTrue(release.wait(2))
                    bridge.runner.stop()
                    return "cancelled"
                return "done"

            bridge.execute = execute
            bridge.submit(1, 2, "first", 10)
            self.assertTrue(started.wait(2))
            bridge.submit(1, 2, "second", 11)
            self.assertEqual(bridge.queued(), 1)
            self.assertIn((11, REACTION_QUEUED), telegram.reactions)
            self.assertIn((10, REACTION_WORKING), telegram.reactions)
            release.set()
            bridge.runner.thread.join(3)
            self.assertEqual(seen, [("first", False), ("second", False)])
            self.assertEqual(bridge.queued(), 0)
            self.assertIn((10, REACTION_CANCELLED), telegram.reactions)
            self.assertIn((11, REACTION_DONE), telegram.reactions)

    def test_the_queue_stops_at_the_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, telegram = make_bridge(Path(tmp))
            started = threading.Event()
            release = threading.Event()

            def execute(chat_id, user_id, text, message_id=None):
                started.set()
                release.wait(2)
                return "error"

            bridge.execute = execute
            bridge.submit(1, 2, "running", 1)
            self.assertTrue(started.wait(2))
            for index in range(MAX_QUEUE):
                bridge.submit(1, 2, f"queued-{index}", 100 + index)
            bridge.submit(1, 2, "overflow", 999)
            self.assertEqual(bridge.queued(), MAX_QUEUE)
            self.assertTrue(any("Queue is full" in text for text in telegram.sent))
            release.set()
            bridge.runner.thread.join(3)

    def test_pump_starts_a_prompt_left_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, telegram = make_bridge(Path(tmp))
            ran: list[str] = []

            def execute(chat_id, user_id, text, message_id=None):
                ran.append(text)
                return "done"

            bridge.execute = execute
            bridge.pending.append(Prompt(1, 2, "left", 7))
            bridge.pump()
            bridge.runner.thread.join(3)
            self.assertEqual(ran, ["left"])
            self.assertIn((7, REACTION_WORKING), telegram.reactions)
            self.assertIn((7, REACTION_DONE), telegram.reactions)

    def test_a_queued_message_is_still_there_after_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge, _telegram = make_bridge(root)
            started = threading.Event()
            release = threading.Event()

            def execute(chat_id, user_id, text, message_id=None):
                started.set()
                release.wait(2)
                return "done"

            bridge.execute = execute
            bridge.submit(1, 2, "first", 10)
            self.assertTrue(started.wait(2))
            bridge.submit(1, 2, "second", 11)
            jobs = bridge.state.data["jobs"]
            self.assertEqual([job["text"] for job in jobs], ["first", "second"])
            self.assertEqual([job["status"] for job in jobs], ["running", "queued"])
            restarted, _again = make_bridge(root)
            self.assertEqual([item.text for item in restarted.pending], ["first", "second"])
            self.assertTrue(restarted.pending[0].resumed)
            self.assertFalse(restarted.pending[1].resumed)
            release.set()
            bridge.runner.thread.join(3)

    def test_an_interrupted_run_continues_in_order_after_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge, _telegram = make_bridge(root)
            bridge.state.data["jobs"] = [
                {
                    "id": "job-running",
                    "chat_id": 1,
                    "user_id": 2,
                    "text": "finish the header",
                    "message_id": 11,
                    "board_message_id": 42,
                    "status": "running",
                },
                {
                    "id": "job-waiting",
                    "chat_id": 1,
                    "user_id": 2,
                    "text": "then the queue",
                    "message_id": 12,
                    "status": "queued",
                },
                {"status": "queued"},
            ]
            bridge.state.save()
            restarted, _again = make_bridge(root)
            self.assertEqual([item.text for item in restarted.pending], ["finish the header", "then the queue"])
            self.assertTrue(restarted.pending[0].resumed)
            self.assertEqual(restarted.pending[0].board_message_id, 42)
            ran: list[str] = []

            def execute(chat_id, user_id, text, message_id=None):
                ran.append(text)
                return "done"

            restarted.execute = execute
            restarted.pump()
            restarted.runner.thread.join(3)
            self.assertEqual(ran, ["finish the header", "then the queue"])
            self.assertEqual(restarted.state.data["jobs"], [])

    def test_a_finished_run_drops_its_saved_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, _telegram = make_bridge(Path(tmp))

            def execute(chat_id, user_id, text, message_id=None):
                return "done"

            bridge.execute = execute
            bridge.submit(1, 2, "now", 10)
            bridge.runner.thread.join(3)
            self.assertEqual(bridge.state.data.get("jobs"), [])

import json
import socket
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from app.webui import SetupPage
from tests.support import make_bridge


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class SetupPageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.bridge, _ = make_bridge(root)
        self.bridge.chat(7)
        self.page = SetupPage(self.bridge, free_port(), root / "ui-key")
        self.page.tool_status = lambda tool, fresh=False: {"installed": True, "signed_in": True, "account": "a@b"}
        self.page.start()
        self.base = f"http://127.0.0.1:{self.page.port}"
        self.cookie = {"Cookie": f"ui_key={self.page.key}"}

    def tearDown(self):
        self.page.stop()
        self.tmp.cleanup()

    def get(self, path, headers=None):
        request = urllib.request.Request(self.base + path, headers=headers or {})
        return urllib.request.urlopen(request)

    def post(self, path, body, headers=None):
        merged = {"Content-Type": "application/json", "X-Requested-With": "setup", **self.cookie, **(headers or {})}
        request = urllib.request.Request(self.base + path, json.dumps(body).encode(), merged, method="POST")
        return urllib.request.urlopen(request)

    def status(self, call):
        try:
            return call().status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_key_file_is_private(self):
        self.assertEqual((Path(self.tmp.name) / "ui-key").stat().st_mode & 0o777, 0o600)

    def test_no_key_is_refused(self):
        self.assertEqual(self.status(lambda: self.get("/api/state")), 401)

    def test_state_with_the_cookie(self):
        data = json.load(self.get("/api/state", self.cookie))
        self.assertEqual(data["cli"], "cursor")
        self.assertTrue(data["tools"]["claude"]["signed_in"])

    def test_wrong_host_is_refused(self):
        self.assertEqual(self.status(lambda: self.get("/api/state", {**self.cookie, "Host": "evil.example"})), 403)

    def test_post_needs_the_custom_header(self):
        request = urllib.request.Request(
            self.base + "/api/config", b'{"cli":"claude"}', {"Content-Type": "application/json", **self.cookie}, method="POST"
        )
        self.assertEqual(self.status(lambda: urllib.request.urlopen(request)), 403)
        self.assertEqual(self.bridge.current_cli(), "cursor")

    def test_config_changes_cli_and_every_chat(self):
        self.post("/api/config", {"cli": "claude", "claude_model": "sonnet", "effort": "high", "mode": "plan"})
        self.assertEqual(self.bridge.current_cli(), "claude")
        row = self.bridge.chat(7)
        self.assertEqual((row["claude_model"], row["effort"], row["mode"]), ("sonnet", "high", "plan"))

    def test_bad_config_changes_nothing(self):
        self.assertEqual(self.status(lambda: self.post("/api/config", {"cli": "claude", "mode": "root"})), 400)
        self.assertEqual(self.bridge.current_cli(), "cursor")

    def test_login_shows_the_link_and_takes_a_code(self):
        script = "import sys; print('Visit https://example.test/auth?x=1 now', flush=True); print('got', sys.stdin.readline().strip())"
        self.bridge.agent = sys.executable
        self.page.bridge.agent_env = lambda: {}
        # Run the stub through the same Login class the page uses.
        from app.webui import Login

        login = Login("cursor", [sys.executable, "-c", script], {})
        self.page.logins["cursor"] = login
        for _ in range(50):
            if login.snapshot()["url"]:
                break
            import time

            time.sleep(0.1)
        self.assertEqual(login.snapshot()["url"], "https://example.test/auth?x=1")
        self.post("/api/login/code", {"tool": "cursor", "code": "abc123"})
        login.proc.wait(timeout=5)
        import time

        time.sleep(0.2)
        snap = login.snapshot()
        self.assertIn("got abc123", snap["output"])
        self.assertTrue(snap["ok"])


if __name__ == "__main__":
    unittest.main()

import http.client
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402

INITIAL_STATE = dict(server.state)


def reset_state():
    with server.state_lock:
        server.state.clear()
        server.state.update(INITIAL_STATE)
        server.art_bytes = b""
        server.art_type = "image/jpeg"


class SceneTests(unittest.TestCase):
    def setUp(self):
        reset_state()
        self._config = server.config
        server.config = {"theater_id": 1, "theater_name": "", "quiet_for_cinema": 120}

    def tearDown(self):
        server.config = self._config
        reset_state()

    def test_no_title_is_cinema(self):
        server.set_state(loud_at=time.time())
        self.assertEqual(server.snapshot()["scene"], "cinema")

    def test_title_and_recent_sound_is_music(self):
        now = time.time()
        server.set_state(title="Song", key="a\nSong", loud_at=now - 30, recognized_at=now - 100)
        snap = server.snapshot()
        self.assertEqual(snap["scene"], "music")
        self.assertEqual(server.scene_now(), ("music", False))

    def test_quiet_past_window_is_cinema(self):
        server.set_state(title="Song", key="a\nSong", loud_at=time.time() - 121)
        self.assertEqual(server.snapshot()["scene"], "cinema")
        self.assertEqual(server.snapshot()["status"], "cinema")

    def test_window_follows_config(self):
        server.config["quiet_for_cinema"] = 10
        server.set_state(title="Song", key="a\nSong", loud_at=time.time() - 30)
        self.assertEqual(server.snapshot()["scene"], "cinema")

    def test_trailer_sound_forces_cinema(self):
        server.set_state(title="Song", key="a\nSong", loud_at=time.time(), trailer_sound=True)
        self.assertEqual(server.snapshot()["scene"], "cinema")
        self.assertEqual(server.scene_now(), ("cinema", True))

    def test_art_url_contract(self):
        self.assertEqual(server.snapshot()["art"], "")
        server.set_state(key="a\nSong")
        with server.state_lock:
            server.art_bytes = b"img"
        snap = server.snapshot()
        self.assertEqual(snap["art"], f"/art?v={int(snap['changed_at'])}")

    def test_stale_art_dropped(self):
        server.set_state(key="new")
        with server.state_lock:
            server.art_bytes = b""

        class Resp:
            headers = {"Content-Type": "image/png"}

            def read(self):
                return b"old-cover"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        orig = server.urllib.request.urlopen
        server.urllib.request.urlopen = lambda *a, **k: Resp()
        try:
            server.fetch_art("http://example/x.jpg", "old")
            self.assertEqual(server.art_bytes, b"")
            server.fetch_art("http://example/x.jpg", "new")
            self.assertEqual(server.art_bytes, b"old-cover")
        finally:
            server.urllib.request.urlopen = orig


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        reset_state()
        self.tmp = tempfile.TemporaryDirectory()
        self._config_path = server.CONFIG_PATH
        self._config = server.config
        self._refresh = server.refresh_cinema
        server.CONFIG_PATH = Path(self.tmp.name) / "config.json"
        server.config = {"theater_id": 1, "theater_name": "Old", "quiet_for_cinema": 120}
        server.refresh_cinema = lambda: None

    def tearDown(self):
        server.CONFIG_PATH = self._config_path
        server.config = self._config
        server.refresh_cinema = self._refresh
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def raw_get(self, path):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
            sock.sendall(f"GET {path} HTTP/1.0\r\nHost: x\r\n\r\n".encode())
            data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        return int(data.split(b" ", 2)[1])

    def post_config(self, payload, headers=None):
        hdrs = {"Content-Type": "application/json"}
        hdrs.update(headers or {})
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return self.request("POST", "/api/config", body, hdrs)

    def test_fonts_traversal_404(self):
        self.assertEqual(self.request("GET", "/fonts/../config.json")[0], 404)
        self.assertEqual(self.raw_get("/fonts/../config.json"), 404)
        self.assertEqual(self.raw_get("/fonts/../server.py"), 404)

    def test_fonts_served(self):
        fonts = sorted((server.ROOT / "fonts").glob("*.ttf"))
        if not fonts:
            self.skipTest("no fonts")
        self.assertEqual(self.request("GET", f"/fonts/{fonts[0].name}")[0], 200)

    def test_post_without_json_type_is_415(self):
        status, _ = self.request("POST", "/api/config", b'{"theater_id": 5}', {"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        self.assertEqual(self.request("POST", "/mute", b"", {})[0], 415)
        self.assertEqual(server.config["theater_id"], 1)

    def test_bad_origin_is_403(self):
        status, _ = self.post_config({"theater_id": 5}, {"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(server.config["theater_id"], 1)
        status, _ = self.request("POST", "/mute", b"{}", {"Content-Type": "application/json", "Origin": "null"})
        self.assertEqual(status, 403)

    def test_mute_ok(self):
        server.set_state(trailer_sound=True)
        status, _ = self.request(
            "POST", "/mute", b"{}", {"Content-Type": "application/json", "Origin": "http://localhost:8787"}
        )
        self.assertEqual(status, 200)
        self.assertFalse(server.state["trailer_sound"])

    def test_bad_values_are_400_and_unchanged(self):
        for payload in (
            {"theater_id": "abc", "theater_name": "New"},
            {"theater_id": -3},
            {"quiet_for_cinema": 99999, "theater_name": "New"},
            {"quiet_for_cinema": "x"},
            {"theater_name": 7},
            [1, 2],
            b"not json",
            b"\xff\xfe",
        ):
            status, _ = self.post_config(payload)
            self.assertEqual(status, 400, payload)
        self.assertEqual(server.config, {"theater_id": 1, "theater_name": "Old", "quiet_for_cinema": 120})
        self.assertFalse(server.CONFIG_PATH.exists())

    def test_bad_content_length_is_400(self):
        status, _ = self.request(
            "POST", "/api/config", b"{}", {"Content-Type": "application/json", "Content-Length": "nope"}
        )
        self.assertEqual(status, 400)

    def test_valid_post_updates_config(self):
        status, body = self.post_config(
            {"theater_id": "42", "theater_name": "New", "quiet_for_cinema": 60},
            {"Origin": "http://127.0.0.1:8787"},
        )
        self.assertEqual(status, 200)
        expected = {"theater_id": 42, "theater_name": "New", "quiet_for_cinema": 60}
        self.assertEqual(json.loads(body), expected)
        self.assertEqual(server.config, expected)
        self.assertEqual(json.loads(server.CONFIG_PATH.read_text()), expected)

    def test_bad_lat_is_400(self):
        self.assertEqual(self.request("GET", "/api/theaters?lat=abc&lon=1")[0], 400)

    def test_phase_removed(self):
        self.assertEqual(self.request("POST", "/phase", b"{}", {"Content-Type": "application/json"})[0], 404)


if __name__ == "__main__":
    unittest.main()

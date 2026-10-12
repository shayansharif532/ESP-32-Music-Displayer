"""HTTP routing of the companion API against a stub monitor: which request reaches which call, with which target app.

    companion\\.venv\\Scripts\\python.exe -m unittest discover -s companion/tests
"""
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from desk_media_companion.server import make_server  # noqa: E402


class StubMonitor:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def now_playing(self):
        return {"ok": True, "has_session": True, "app": "Spotify.exe"}

    def control(self, action, position_ms=None, app=""):
        self.calls.append(("control", action, position_ms, app))
        return {"ok": True, "action": action, "app": app}

    def volume(self, action, step=5, level=None, app=""):
        self.calls.append(("volume", action, step, level, app))
        return {"ok": True, "action": action, "volume": 50}

    def switch_source(self, direction, shown_app=""):
        self.calls.append(("source", direction, shown_app))
        return {"ok": True, "changed": True, "app": "chrome.exe", "now": {"app": "chrome.exe"}}

    def auto_source(self):
        self.calls.append(("auto",))
        return {"ok": True}

    def list_sources(self):
        return {"ok": True, "sources": []}

    def artwork(self, size=300, max_w=None):
        return None


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mon = StubMonitor()
        cls.server = make_server(cls.mon, 0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.mon.calls.clear()

    def req(self, method, path, headers=None):
        h = {"X-Desk-Client": "test"} if method == "POST" else {}
        h.update(headers or {})
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode() or "{}")

    def test_controls_carry_the_displayed_app(self):
        for action in ("playpause", "next", "previous", "play", "pause"):
            self.req("POST", f"/control/{action}?app=chrome.exe")
        self.req("POST", "/control/seek?position_ms=12345&app=Spotify.exe")
        self.assertEqual(self.mon.calls, [("control", "playpause", None, "chrome.exe"), ("control", "next", None, "chrome.exe"),
                                          ("control", "previous", None, "chrome.exe"), ("control", "play", None, "chrome.exe"),
                                          ("control", "pause", None, "chrome.exe"), ("control", "seek", 12345, "Spotify.exe")])

    def test_shuffle_and_repeat_routes_carry_the_app_and_answer_with_the_fresh_state(self):
        status, body = self.req("POST", "/control/shuffle?app=Spotify.exe")
        self.assertEqual((status, body["ok"], body["now"]["app"]), (200, True, "Spotify.exe"))
        for action in ("repeat", "repeat-track", "shuffle-off"):
            self.req("POST", f"/control/{action}?app=Spotify.exe")
        self.assertEqual(self.mon.calls, [("control", "shuffle", None, "Spotify.exe"), ("control", "repeat", None, "Spotify.exe"),
                                          ("control", "repeat-track", None, "Spotify.exe"), ("control", "shuffle-off", None, "Spotify.exe")])

    def test_controls_without_app_still_work(self):
        self.req("POST", "/control/next")
        self.assertEqual(self.mon.calls, [("control", "next", None, "")])

    def test_volume_carries_the_displayed_app(self):
        self.req("POST", "/control/volume-down?step=5&app=Spotify.exe")
        self.req("POST", "/control/volume-up?app=chrome.exe")
        self.req("POST", "/volume?level=30&app=chrome.exe")
        self.assertEqual(self.mon.calls, [("volume", "volume-down", 5, None, "Spotify.exe"), ("volume", "volume-up", 5, None, "chrome.exe"),
                                          ("volume", "volume-set", 5, 30, "chrome.exe")])

    def test_source_switching_is_relative_to_the_displayed_app(self):
        status, body = self.req("POST", "/control/source-next?from=Spotify.exe")
        self.assertEqual((status, body["changed"], body["now"]["app"]), (200, True, "chrome.exe"))
        self.req("POST", "/control/source-prev?from=chrome.exe")
        self.req("POST", "/control/source-next")
        self.req("POST", "/control/source-auto")
        self.assertEqual(self.mon.calls, [("source", 1, "Spotify.exe"), ("source", -1, "chrome.exe"), ("source", 1, ""), ("auto",)])

    def test_app_ids_with_special_characters_survive_url_encoding(self):
        self.req("POST", "/control/next?app=Microsoft.ZuneMusic_8wekyb3d8bbwe%21Microsoft.ZuneMusic")
        self.assertEqual(self.mon.calls[-1][3], "Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic")

    def test_sources_listing(self):
        self.assertEqual(self.req("GET", "/sources"), (200, {"ok": True, "sources": []}))

    def test_posts_need_the_client_header(self):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}/control/source-next", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(r, timeout=5)
        self.assertEqual(ctx.exception.code, 403)
        self.assertEqual(self.mon.calls, [])

    def test_unknown_control_is_404(self):
        self.assertEqual(self.req("POST", "/control/explode")[0], 404)


if __name__ == "__main__":
    unittest.main()

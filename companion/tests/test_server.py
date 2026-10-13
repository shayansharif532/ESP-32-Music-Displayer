"""HTTP routing of the companion API against a stub monitor: which request reaches which call, with which target app.

    companion\\.venv\\Scripts\\python.exe -m unittest discover -s companion/tests
"""
import json
import os
import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from desk_media_companion.server import make_server  # noqa: E402


class StubMonitor:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.art_calls: list[tuple] = []

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
        self.art_calls.append((size, max_w))
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

    def test_the_idle_item_is_named_by_the_display_in_source_requests(self):
        self.req("POST", "/control/source-next?from=idle")
        self.req("POST", "/control/source-prev?from=idle")
        self.assertEqual(self.mon.calls, [("source", 1, "idle"), ("source", -1, "idle")])

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


class Hardening(unittest.TestCase):
    """Bounded input: Content-Length parsing, body limits, parameter clamps, fixed error texts, the connection cap, no CORS.
    (Loopback client, so no signature is involved: that is test_pairing.py.)"""

    @classmethod
    def setUpClass(cls):
        cls.mon = StubMonitor()
        cls.server = make_server(cls.mon, 0, bind="127.0.0.1")
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.mon.calls.clear()
        self.mon.art_calls.clear()

    def raw(self, data: bytes) -> bytes:
        """Raw bytes in, everything the server answers until it closes the connection out (requests here say Connection: close)."""
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
            s.sendall(data)
            chunks = []
            try:
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
            except ConnectionError:
                pass
        return b"".join(chunks)

    def post(self, headers: str, body: bytes = b"", path: str = "/control/next") -> bytes:
        return self.raw((f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nX-Desk-Client: t\r\nConnection: close\r\n{headers}\r\n").encode()
                        + body)

    @staticmethod
    def status(answer: bytes) -> int:
        return int(answer.split(b" ", 2)[1])

    def get(self, path: str):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode() or "{}")

    def test_a_malformed_content_length_is_400_and_closes_the_connection(self):
        for value in ("abc", "-1", "+5", "1e3", "1_0", "", " ", "0x10", "99999999999", "5 5", "٣"):
            with self.subTest(value=value):
                answer = self.post(f"Content-Length: {value}\r\n")
                self.assertEqual(self.status(answer), 400, value)
                self.assertIn(b"connection: close", answer.lower())
        self.assertEqual(self.status(self.post("Content-Length: 3\r\nContent-Length: 3\r\n", b"abc")), 400)     # two of them
        self.assertEqual(self.mon.calls, [])

    def test_a_chunked_body_is_refused(self):
        answer = self.post("Transfer-Encoding: chunked\r\n", b"0\r\n\r\n")
        self.assertEqual(self.status(answer), 400)
        self.assertEqual(self.mon.calls, [])

    def test_bodies_over_4096_bytes_are_413_and_closed_before_they_are_read(self):
        for length in (4097, 5000, 10 ** 9 - 1):
            answer = self.post(f"Content-Length: {length}\r\n")                    # no body sent: the server must not wait for one
            self.assertEqual(self.status(answer), 413, length)
            self.assertIn(b"connection: close", answer.lower())
        self.assertEqual(self.mon.calls, [])

    def test_a_body_of_exactly_4096_bytes_is_still_accepted_and_ignored(self):
        answer = self.post("Content-Length: 4096\r\n", b"x" * 4096)
        self.assertEqual(self.status(answer), 200)
        self.assertEqual(self.mon.calls, [("control", "next", None, "")])

    def test_a_short_body_does_not_hang_or_crash_the_server(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
            s.sendall(f"POST /control/next HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nX-Desk-Client: t\r\nContent-Length: 100\r\n\r\nshort".encode())
            s.shutdown(socket.SHUT_WR)                                           # the client hangs up after 5 of 100 bytes
            self.assertEqual(s.recv(4096), b"")
        self.assertEqual(self.mon.calls, [])
        self.assertEqual(self.get("/health")[0], 200)

    def test_too_many_query_fields_are_400(self):
        self.assertEqual(self.get("/now-playing?" + "&".join(f"a{i}=1" for i in range(40)))[0], 400)
        self.assertEqual(self.get("/now-playing?" + "&".join(f"a{i}=1" for i in range(10)))[0], 200)

    def test_artwork_parameters_are_clamped(self):
        for query, expected in (("size=312&maxw=432", (312, 432)), ("size=100000&maxw=999999", (600, 800)), ("size=1&maxw=-5", (16, 16)),
                                ("size=abc&maxw=xyz", (300, None)), ("", (300, None)), ("size=-40", (16, None)), ("maxw=5000", (300, 800)),
                                ("size=" + "9" * 5000, (300, None)), ("size=%E0%A4%A&maxw=%00", (300, None))):
            self.mon.art_calls.clear()
            self.assertEqual(self.get("/artwork?" + query)[0], 404)                  # the stub has no artwork; only the clamped call matters
            self.assertEqual(self.mon.art_calls, [expected], query[:30])

    def test_volume_step_level_and_seek_position_are_clamped(self):
        for query, expected_step in (("step=9999", 50), ("step=0", 1), ("step=-7", 1), ("step=abc", 5), ("", 5), ("step=12", 12)):
            self.mon.calls.clear()
            self.post("", path="/control/volume-up?" + query)
            self.assertEqual(self.mon.calls, [("volume", "volume-up", expected_step, None, "")], query)
        for query, expected in (("level=500", 100), ("level=-3", 0), ("level=40", 40)):
            self.mon.calls.clear()
            self.post("", path="/volume?" + query)
            self.assertEqual(self.mon.calls, [("volume", "volume-set", 5, expected, "")], query)
        for path in ("/volume?level=x", "/volume"):
            self.assertEqual(self.status(self.post("", path=path)), 400)
        for query, expected in (("position_ms=-5", 0), ("position_ms=99999999999999", 2 ** 31 - 1), ("position_ms=1234", 1234)):
            self.mon.calls.clear()
            self.post("", path="/control/seek?" + query)
            self.assertEqual(self.mon.calls, [("control", "seek", expected, "")], query)
        for path in ("/control/seek?position_ms=abc", "/control/seek"):
            self.assertEqual(self.status(self.post("", path=path)), 400)

    def test_error_answers_never_echo_the_request(self):
        for answer in (self.raw(b"ZZMARKER /ZZMARKER HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"),       # unknown method
                       self.raw(b"GET /ZZMARKER HTTP/9.9\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"),             # bad version
                       self.raw(b"GET /ZZMARKER/ZZMARKER HTTP/1.1\r\nHost: ZZMARKER\r\nConnection: close\r\n\r\n"),     # bad Host
                       self.raw(b"GET /ZZMARKER HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"),             # unknown path
                       self.post("", path="/control/ZZMARKER"), self.post("Content-Length: ZZMARKER\r\n")):
            self.assertNotIn(b"ZZMARKER", answer)
            if answer.startswith(b"HTTP/"):              # (the stock parser answers a bad protocol version without a status line)
                self.assertGreaterEqual(self.status(answer), 400)
            body = answer.split(b"\r\n\r\n", 1)[-1]
            self.assertFalse(body and b"<html" in body.lower())                      # JSON, not the stock HTML error page
            if body:
                self.assertIs(json.loads(body)["ok"], False)

    def test_the_server_header_does_not_name_the_python_version(self):
        answer = self.raw(b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        self.assertIn(b"Server: DeskMediaCompanion/", answer)
        self.assertNotIn(b"Python", answer)

    def test_options_is_405_without_any_cors_header(self):
        answer = self.raw(b"OPTIONS /control/next HTTP/1.1\r\nHost: 127.0.0.1\r\nOrigin: http://evil.example\r\n"
                          b"Access-Control-Request-Method: POST\r\nConnection: close\r\n\r\n")
        self.assertEqual(self.status(answer), 405)
        self.assertNotIn(b"access-control", answer.lower())
        self.assertIn(b"Allow: GET, HEAD, POST", answer)
        answer = self.raw(b"GET /now-playing HTTP/1.1\r\nHost: 127.0.0.1\r\nOrigin: http://evil.example\r\nConnection: close\r\n\r\n")
        self.assertEqual(self.status(answer), 200)
        self.assertNotIn(b"access-control", answer.lower())

    def test_at_most_32_connections_are_served_at_once(self):
        held = []
        try:
            for _ in range(32):
                held.append(socket.create_connection(("127.0.0.1", self.port), timeout=5))
            self.wait_until(lambda: self.server.active_connections == 32)
            extra = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            held.append(extra)
            extra.sendall(f"GET /health HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n\r\n".encode())
            try:
                self.assertEqual(extra.recv(4096), b"")                              # closed at once, not served
            except ConnectionError:
                pass
            self.assertEqual(self.server.active_connections, 32)
        finally:
            for s in held:
                s.close()
        self.wait_until(lambda: self.server.active_connections == 0)               # the slots come back when the clients leave
        self.assertEqual(self.get("/health")[0], 200)

    def wait_until(self, predicate, timeout: float = 5.0) -> None:
        end = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > end:
                self.fail("condition not reached")
            time.sleep(0.01)


class IdleItemGuards(unittest.TestCase):
    """The synthetic idle item is a display choice, not a media session: nothing that controls media may reach an app through it."""

    def test_media_commands_and_volume_aimed_at_idle_are_refused_before_any_session_is_touched(self):
        from desk_media_companion.media import MediaMonitor
        mon = MediaMonitor()                     # never started: these refusals must not need the Windows media API at all
        for action in ("playpause", "next", "previous", "seek", "shuffle"):
            r = mon.control(action, 1000, "idle")
            self.assertFalse(r["ok"], action)
            self.assertIn("idle", r["error"])
        for action in ("volume-up", "volume-down", "volume-set", "get"):
            r = mon.volume(action, level=10, app="IDLE")
            self.assertFalse(r["ok"], action)
            self.assertIsNone(r["volume"])


class IdleRoutes(unittest.TestCase):
    """GET /idle, GET /idle/image and the idle fields of /now-playing, against a real IdleStore in a temporary folder."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        from desk_media_companion import idle as idle_mod
        cls.idle_mod = idle_mod
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = idle_mod.IdleStore(os.path.join(cls.tmp.name, "idle"))
        cls.server = make_server(StubMonitor(), 0, idle=cls.store)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def test_now_playing_carries_the_version_and_the_clock(self):
        status, _h, body = self.get("/now-playing")
        j = json.loads(body)
        self.assertEqual((status, j["idle_v"]), (200, self.store.version))
        self.assertAlmostEqual(j["clock_ms"], self.idle_mod.local_clock_ms(), delta=3000)

    def test_idle_config_and_picture(self):
        status, _h, body = self.get("/idle")
        cfg = json.loads(body)
        self.assertEqual((status, cfg["theme"], cfg["image"]), (200, "dark", None))
        self.assertEqual(self.get("/idle/image")[0], 404)
        buf = __import__("io").BytesIO()
        __import__("PIL.Image", fromlist=["Image"]).new("RGB", (300, 200), (10, 120, 200)).save(buf, "PNG")
        v = self.store.version
        self.store.update(theme="photo", text="Hello")
        self.store.set_picture(buf.getvalue(), "x.png", "blur")
        status, headers, body = self.get("/idle")
        cfg = json.loads(body)
        self.assertGreater(cfg["version"], v)
        self.assertEqual((cfg["theme"], cfg["text"], cfg["bg_mode"]), ("photo", "Hello", "blur"))
        status, headers, jpeg = self.get("/idle/image")
        self.assertEqual((status, headers["Content-Type"]), (200, "image/jpeg"))
        self.assertEqual((len(jpeg), int(headers["X-Idle-Crc"])), (cfg["image"]["size"], cfg["image"]["crc"]))
        self.assertEqual(json.loads(self.get("/now-playing")[2])["idle_v"], cfg["version"])

    def test_idle_cannot_be_changed_over_http(self):
        for method in ("POST", "PUT"):
            r = urllib.request.Request(f"http://127.0.0.1:{self.port}/idle", method=method, data=b"{}", headers={"X-Desk-Client": "test"})
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(r, timeout=5)
            self.assertIn(ctx.exception.code, (404, 501))


if __name__ == "__main__":
    unittest.main()

"""The transport-independent API (api.py): route behaviour, the authentication matrix for HTTP versus USB, the 426 / 429 gates, /hello,
and the USB session handshake (LinkSession) with a known-answer vector.

    companion\\.venv\\Scripts\\python.exe -m unittest discover -s companion/tests
"""
import hashlib
import hmac
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from desk_media_companion import api as api_mod  # noqa: E402
from desk_media_companion.api import Api, ClientCtx, LinkSession, Reply  # noqa: E402
from desk_media_companion.idle import IdleStore  # noqa: E402
from desk_media_companion.pairing import Pairing, PairingStore  # noqa: E402
from desk_media_companion.server import make_server  # noqa: E402

H = bytes.fromhex
CID = H("0102030405060708")
DEV = H("11" * 8)
OTHER_DEV = H("22" * 8)
K = H("a388cfc735774ba3e13991eadd90a4ffa2020becaf2d78a70541f26db7a09821")      # docs/protocol.md appendix
ND, NC = H("aa" * 16), H("bb" * 16)
# Known answers for the session proofs of docs/usb-protocol.md section 4 (K, cid, dev, nd, nc above), computed with plain hmac / hashlib.
PROOF_PC = "727d46e471798ea86f5c697581b3cb0c8091ecfbe45286afc30dfc5e3be4a448"      # HMAC-SHA256(K, "DMLINK-PC\n"  || cid || dev || nd || nc)
PROOF_DEV = "0ee7f37576dc0eb66571a7cbac24eaef2bb906c6384da6725f9c1d1fee1e1354"     # HMAC-SHA256(K, "DMLINK-DEV\n" || cid || dev || nd || nc)


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class StubMonitor:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.art_calls: list[tuple] = []
        self.debug: list[str] = []
        self.artwork_result = ("art1", b"\xff\xd8\xff\xe0" + bytes(range(256)) * 4)

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
        return self.artwork_result

    def request_debug(self, action):
        self.debug.append(action)

    def take_debug(self):
        return ""


def paired_pairing(directory: str, clock: FakeClock, *, paired: bool = True) -> Pairing:
    store = PairingStore(directory, cid=CID)
    if paired:
        store.set_device(DEV, K)
    return Pairing(store, name="TEST-PC", clock=clock)


def usb(authenticated: bool = True, *, dev: bytes = DEV, session: LinkSession | None = None, compatible: bool = True) -> ClientCtx:
    return ClientCtx("usb", authenticated, "usb:COM9", dev, compatible, session)


def http(authenticated: bool = True, if_none_match: str = "") -> ClientCtx:
    return ClientCtx("http", authenticated, "127.0.0.1", if_none_match=if_none_match)


def body_of(reply: Reply) -> dict:
    return json.loads(reply.body.decode("utf-8"))


class ApiCase(unittest.TestCase):
    dev_tools = False
    paired = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = FakeClock()
        self.pairing = paired_pairing(self.tmp.name, self.clock, paired=self.paired)
        self.mon = StubMonitor()
        self.api = Api(self.mon, None, self.pairing, self.dev_tools)

    def call(self, method, target, ctx=None, body=b"") -> Reply:
        return self.api.handle(method, target, body, ctx or usb())


class Routes(ApiCase):
    """Every route behaves the same over both transports (the callers are authenticated here)."""

    def test_the_same_requests_get_the_same_answers_over_http_and_usb(self):
        requests = [("GET", "/now-playing"), ("GET", "/sources"), ("GET", "/volume?app=chrome.exe"), ("GET", "/artwork?size=312&maxw=432"),
                    ("POST", "/control/playpause?app=chrome.exe"), ("POST", "/control/seek?position_ms=1234&app=Spotify.exe"),
                    ("POST", "/control/volume-up?step=10&app=Spotify.exe"), ("POST", "/volume?level=30&app=chrome.exe"),
                    ("POST", "/control/source-next?from=Spotify.exe"), ("POST", "/control/source-auto"),
                    ("POST", "/control/shuffle?app=Spotify.exe"), ("GET", "/nonexistent"), ("POST", "/control/explode"), ("POST", "/volume"),
                    ("GET", "/health"), ("GET", "/idle")]
        for method, target in requests:
            a, b = self.call(method, target, http()), self.call(method, target, usb())
            self.assertEqual((a.status, a.body, a.content_type, a.headers), (b.status, b.body, b.content_type, b.headers), (method, target))

    def test_controls_carry_the_displayed_app(self):
        for action in ("playpause", "next", "previous", "play", "pause"):
            self.call("POST", f"/control/{action}?app=chrome.exe")
        self.call("POST", "/control/seek?position_ms=12345&app=Spotify.exe")
        self.assertEqual(self.mon.calls, [("control", "playpause", None, "chrome.exe"), ("control", "next", None, "chrome.exe"),
                                          ("control", "previous", None, "chrome.exe"), ("control", "play", None, "chrome.exe"),
                                          ("control", "pause", None, "chrome.exe"), ("control", "seek", 12345, "Spotify.exe")])

    def test_shuffle_and_repeat_answer_with_the_fresh_state(self):
        r = self.call("POST", "/control/shuffle?app=Spotify.exe")
        self.assertEqual((r.status, body_of(r)["ok"], body_of(r)["now"]["app"]), (200, True, "Spotify.exe"))
        for action in ("repeat", "repeat-track", "shuffle-off"):
            self.call("POST", f"/control/{action}?app=Spotify.exe")
        self.assertEqual([c[1] for c in self.mon.calls], ["shuffle", "repeat", "repeat-track", "shuffle-off"])

    def test_volume_and_sources(self):
        self.call("POST", "/control/volume-down?step=5&app=Spotify.exe")
        self.call("POST", "/control/volume-up?app=chrome.exe")
        self.call("POST", "/volume?level=30&app=chrome.exe")
        self.call("POST", "/control/source-prev?from=idle")
        self.call("POST", "/control/source-next")
        self.assertEqual(self.mon.calls, [("volume", "volume-down", 5, None, "Spotify.exe"), ("volume", "volume-up", 5, None, "chrome.exe"),
                                          ("volume", "volume-set", 5, 30, "chrome.exe"), ("source", -1, "idle"), ("source", 1, "")])
        self.assertEqual(body_of(self.call("GET", "/sources")), {"ok": True, "sources": []})

    def test_parameters_are_clamped_and_required_ones_are_400(self):
        self.call("POST", "/control/volume-up?step=9999")
        self.call("POST", "/control/volume-up?step=abc")
        self.call("POST", "/volume?level=500")
        self.call("POST", "/control/seek?position_ms=99999999999999")
        self.assertEqual([c[2] for c in self.mon.calls[:2]], [50, 5])
        self.assertEqual(self.mon.calls[2][3], 100)
        self.assertEqual(self.mon.calls[3][2], 2 ** 31 - 1)
        self.assertEqual(self.call("POST", "/volume").status, 400)
        self.assertEqual(self.call("POST", "/control/seek").status, 400)
        for query, expected in (("size=100000&maxw=999999", (600, 800)), ("size=abc", (300, None)), ("", (300, None)), ("size=1&maxw=-5", (16, 16))):
            self.mon.art_calls.clear()
            self.call("GET", "/artwork?" + query)
            self.assertEqual(self.mon.art_calls, [expected], query)

    def test_artwork_headers_etag_and_304(self):
        r = self.call("GET", "/artwork?size=312&maxw=432", http())
        self.assertEqual((r.status, r.content_type, r.headers["X-Art-Id"], r.headers["ETag"]), (200, "image/jpeg", "art1", '"art1-312-432"'))
        r2 = self.call("GET", "/artwork?size=312&maxw=432", http(if_none_match='"art1-312-432"'))
        self.assertEqual((r2.status, r2.body, r2.headers), (304, b"", {"ETag": '"art1-312-432"'}))
        self.mon.artwork_result = None
        r3 = self.call("GET", "/artwork")
        self.assertEqual((r3.status, body_of(r3)), (404, {"ok": False, "error": "no artwork"}))

    def test_a_bad_target_is_400_and_errors_never_echo_the_request(self):
        r = self.call("GET", "/now-playing?" + "&".join(f"a{i}=1" for i in range(40)))
        self.assertEqual(r.status, 400)
        for method, target in (("GET", "/ZZMARKER"), ("POST", "/control/ZZMARKER"), ("POST", "/pair/ZZMARKER"), ("GET", "/link/ZZMARKER"),
                               ("POST", "/volume?level=ZZMARKER"), ("GET", "/pair/status?dev=ZZMARKER")):
            for ctx in (http(), usb(), usb(False)):
                self.assertNotIn(b"ZZMARKER", self.call(method, target, ctx).body, (method, target))

    def test_head_is_answered_like_get(self):
        r = self.call("HEAD", "/health", http())
        self.assertEqual((r.status, body_of(r)["service"]), (200, "desk-media-companion"))

    def test_idle_routes_with_a_real_store(self):
        with tempfile.TemporaryDirectory() as d:
            store = IdleStore(os.path.join(d, "idle"))
            api = Api(self.mon, store, self.pairing)
            now = body_of(api.handle("GET", "/now-playing", b"", usb()))
            self.assertEqual(now["idle_v"], store.version)
            self.assertIn("clock_ms", now)
            self.assertEqual(body_of(api.handle("GET", "/idle", b"", usb()))["theme"], "dark")
            self.assertEqual(api.handle("GET", "/idle/image", b"", usb()).status, 404)


class DevTools(ApiCase):
    dev_tools = True

    def test_debug_routes_work_over_both_transports_and_need_the_session(self):
        for ctx in (http(), usb()):
            r = self.call("POST", "/debug/pair_accept", ctx)
            self.assertEqual((r.status, body_of(r)), (200, {"ok": True, "debug": "pair_accept"}))
        self.assertEqual(self.mon.debug, ["pair_accept", "pair_accept"])
        self.assertEqual(self.call("POST", "/debug/not_an_action").status, 404)
        self.assertEqual(self.call("POST", "/debug/pair_accept", usb(False)).status, 401)
        self.assertEqual(self.mon.debug, ["pair_accept", "pair_accept"])

    def test_pending_debug_rides_on_now_playing_hello_and_pair_status(self):
        self.mon.take_debug = lambda: "pair_shot"
        self.assertEqual(body_of(self.call("GET", "/now-playing"))["debug"], "pair_shot")
        self.assertEqual(body_of(self.call("GET", "/hello", usb(False)))["debug"], "pair_shot")


class NoDevTools(ApiCase):
    def test_debug_routes_do_not_exist_without_the_flag(self):
        self.assertEqual(self.call("POST", "/debug/pair_accept").status, 404)
        self.assertEqual(self.mon.debug, [])


class AuthMatrix(ApiCase):
    PROTECTED = [("GET", "/now-playing"), ("GET", "/artwork?size=100"), ("GET", "/volume"), ("GET", "/sources"), ("GET", "/idle"),
                 ("GET", "/idle/image"), ("GET", "/nonexistent"), ("POST", "/control/next"), ("POST", "/control/seek?position_ms=5"),
                 ("POST", "/volume?level=5"), ("POST", "/control/source-auto"), ("POST", "/control/shuffle"), ("POST", "/debug/screenshot"),
                 ("POST", "/hello"), ("POST", "/health"), ("GET", "/control/next")]
    OPEN = [("GET", "/hello"), ("GET", "/health"), ("GET", "/pair/status?dev=" + "11" * 8), ("POST", "/pair/offer"), ("POST", "/pair/reveal"),
            ("POST", "/pair/confirm"), ("POST", "/pair/cancel"), ("POST", "/link/open"), ("POST", "/link/auth")]

    def test_usb_without_a_session_gets_401_auth_when_a_display_is_paired(self):
        for ctx in (usb(False), http(False)):
            for method, target in self.PROTECTED:
                r = self.call(method, target, ctx)
                self.assertEqual((r.status, body_of(r)), (401, {"ok": False, "error": "auth"}), (ctx.transport, method, target))
        self.assertEqual((self.mon.calls, self.mon.art_calls, self.mon.debug), ([], [], []))

    def test_without_any_pairing_the_answer_is_401_unpaired(self):
        self.pairing.forget()
        for method, target in self.PROTECTED:
            r = self.call(method, target, usb(False))
            self.assertEqual((r.status, body_of(r)), (401, {"ok": False, "error": "unpaired"}), (method, target))
        self.assertEqual(self.mon.calls, [])

    def test_open_routes_need_no_session(self):
        for method, target in self.OPEN:
            r = self.call(method, target, usb(False))
            self.assertNotIn(r.status, (401, 426, 429), (method, target))

    def test_with_a_session_the_protected_routes_are_served(self):
        for method, target in (("GET", "/now-playing"), ("GET", "/sources"), ("POST", "/control/next")):
            self.assertEqual(self.call(method, target, usb(True)).status, 200, (method, target))

    def test_hello_over_http_is_exactly_what_it_was(self):
        self.assertEqual(body_of(self.call("GET", "/hello", http(False))),
                         {"ok": True, "v": 2, "id": CID.hex(), "name": "TEST-PC", "paired": True, "pairing": False})

    def test_hello_over_usb_adds_the_transport_and_whether_the_paired_display_is_this_one(self):
        base = {"ok": True, "v": 2, "id": CID.hex(), "name": "TEST-PC", "paired": True, "pairing": False, "transport": "usb"}
        self.assertEqual(body_of(self.call("GET", "/hello", usb(False, dev=DEV))), dict(base, paired_with_you=True))
        self.assertEqual(body_of(self.call("GET", "/hello", usb(False, dev=OTHER_DEV)))["paired_with_you"], False)
        self.assertEqual(body_of(self.call("GET", "/hello", usb(False, dev=b"")))["paired_with_you"], False)
        self.pairing.forget()
        r = body_of(self.call("GET", "/hello", usb(False, dev=DEV)))
        self.assertEqual((r["paired"], r["paired_with_you"]), (False, False))
        self.pairing.open_window()
        self.assertTrue(body_of(self.call("GET", "/hello", usb(False)))["pairing"])

    def test_an_incompatible_link_answers_426_for_everything_except_hello(self):
        ctx = usb(True, compatible=False)
        for method, target in self.PROTECTED + self.OPEN[1:]:
            r = self.call(method, target, ctx)
            self.assertEqual((r.status, body_of(r)), (426, {"ok": False, "error": "update needed"}), (method, target))
        r = self.call("GET", "/hello", usb(False, compatible=False))
        self.assertEqual((r.status, body_of(r)["transport"]), (200, "usb"))
        self.assertEqual(self.mon.calls, [])

    def test_a_blocked_session_answers_429_on_protected_routes_but_not_on_the_open_ones(self):
        session = LinkSession(self.pairing, self.clock)
        for _ in range(10):
            session._limiter.fail("usb")
        ctx = usb(False, session=session)
        r = self.call("GET", "/now-playing", ctx)
        self.assertEqual((r.status, body_of(r)["error"]), (429, "too many failures"))
        self.assertEqual(self.call("GET", "/hello", ctx).status, 200)
        self.assertEqual(self.call("GET", "/health", ctx).status, 200)
        self.assertEqual(self.call("POST", "/pair/cancel", ctx, b"{}").status, 400)         # reached the pairing code: not blocked
        self.assertEqual(self.call("POST", "/link/open", ctx, b"{}").status, 429)
        self.assertEqual(self.call("GET", "/now-playing", usb(True, session=session)).status, 200)    # an established session is not affected

    def test_link_routes_exist_only_on_usb_with_a_session_and_only_as_post(self):
        session = LinkSession(self.pairing, self.clock)
        body = json.dumps({"dev": DEV.hex(), "nd": ND.hex()}).encode()
        self.assertEqual(self.call("POST", "/link/open", http(False), body).status, 404)
        self.assertEqual(self.call("POST", "/link/open", usb(False), body).status, 404)               # no session object: no such route
        self.assertEqual(self.call("GET", "/link/open", usb(False, session=session)).status, 404)
        self.assertEqual(self.call("POST", "/link/other", usb(False, session=session), body).status, 404)
        self.assertEqual(self.call("POST", "/link/open", usb(False, session=session), body).status, 200)


class PairingRoutes(ApiCase):
    paired = False

    def test_pairing_routes_work_over_usb_and_http(self):
        for ctx in (usb(False), http(False)):
            r = self.call("POST", "/pair/offer", ctx, b"{}")
            self.assertEqual((r.status, body_of(r)), (400, {"ok": False, "error": "bad_request"}))
            self.assertEqual(self.call("POST", "/pair/offer", ctx, b"not json").status, 400)
            self.assertEqual(self.call("GET", "/pair/offer", ctx).status, 404)
        self.assertEqual(self.call("GET", "/pair/status?dev=" + "11" * 8).status, 404)
        self.pairing.open_window()
        import desk_media_companion.x25519 as x
        priv = bytes(range(32))
        pub = x.x25519(priv, x.BASE_POINT)
        ne = H("cc" * 16)
        offer = {"dev": DEV.hex(), "pub": pub.hex(), "ce": hashlib.sha256(pub + ne).hexdigest()}
        r = self.call("POST", "/pair/offer", usb(False), json.dumps(offer).encode())
        self.assertEqual((r.status, body_of(r)["id"]), (200, CID.hex()))

    def test_without_a_pairing_object_the_pairing_routes_are_closed(self):
        api = Api(self.mon, None, None)
        self.assertEqual(api.handle("POST", "/pair/offer", b"{}", usb(False)).status, 403)
        self.assertEqual(body_of(api.handle("GET", "/hello", b"", usb(False)))["id"], "0" * 16)
        self.assertEqual(api.handle("GET", "/now-playing", b"", usb(False)).status, 401)


# ---- the session handshake -------------------------------------------------------------------------------------------------------------

class SessionCase(ApiCase):
    def setUp(self):
        super().setUp()
        self.session = LinkSession(self.pairing, self.clock, rand=lambda n: NC[:n])

    def post(self, route: str, obj, beacon_dev: bytes = DEV):
        raw = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        r = self.session.handle(route, raw, beacon_dev)
        return r.status, body_of(r)

    def open(self, dev: bytes = DEV, nd: bytes = ND):
        return self.post("/link/open", {"dev": dev.hex(), "nd": nd.hex()})

    def handshake(self) -> None:
        status, body = self.open()
        self.assertEqual(status, 200)
        self.assertEqual(self.post("/link/auth", {"dev": DEV.hex(), "proof": PROOF_DEV}), (200, {"ok": True}))
        self.assertTrue(self.session.authenticated)


class Handshake(SessionCase):
    def test_the_vector_of_the_spec_and_the_firmware(self):
        """docs/usb-protocol.md section 10 (nd = cc * 16, nc = dd * 16): the firmware's on-target self-test checks the same two proofs."""
        session = LinkSession(self.pairing, self.clock, rand=lambda n: bytes([0xDD]) * n)
        opened = session.handle("/link/open", json.dumps({"dev": DEV.hex(), "nd": "cc" * 16}).encode(), DEV)
        self.assertEqual(body_of(opened)["proof"], "6eb2d91074d29d26f2c752fd75c9577e41516d44a7acbb5ad032686d09cb56dd")
        auth = session.handle("/link/auth", json.dumps({"dev": DEV.hex(), "proof": "b9e783272a2baef27e9f868e45a9c64d8728c37b081aecb5cc3f7c0c92006c89"}).encode(), DEV)
        self.assertEqual(auth.status, 200)
        self.assertTrue(session.authenticated)

    def test_the_known_answer_vector(self):
        """The companion's proof and the one it accepts match the HMACs computed with plain hmac / hashlib."""
        status, body = self.open()
        self.assertEqual((status, body["ok"], body["nc"], body["proof"]), (200, True, NC.hex(), PROOF_PC))
        self.assertFalse(self.session.authenticated)
        self.assertEqual(self.post("/link/auth", {"dev": DEV.hex(), "proof": PROOF_DEV}), (200, {"ok": True}))
        self.assertTrue(self.session.authenticated)
        expect = hmac.new(K, b"DMLINK-PC\n" + CID + DEV + ND + NC, hashlib.sha256).hexdigest()
        self.assertEqual(expect, PROOF_PC)

    def test_nonces_are_fresh_per_open_and_a_recorded_session_is_useless(self):
        rand_values = iter([H("01" * 16), H("02" * 16)])
        session = LinkSession(self.pairing, self.clock, rand=lambda n: next(rand_values))
        r1 = session.handle("/link/open", json.dumps({"dev": DEV.hex(), "nd": ND.hex()}).encode(), DEV)
        r2 = session.handle("/link/open", json.dumps({"dev": DEV.hex(), "nd": ND.hex()}).encode(), DEV)
        self.assertNotEqual(body_of(r1)["nc"], body_of(r2)["nc"])
        self.assertNotEqual(body_of(r1)["proof"], body_of(r2)["proof"])
        replay = session.handle("/link/auth", json.dumps({"dev": DEV.hex(), "proof": PROOF_DEV}).encode(), DEV)     # DEV proof of the old nonces
        self.assertEqual((replay.status, body_of(replay)), (401, {"ok": False, "error": "auth"}))

    def test_the_proof_is_compared_in_constant_time(self):
        self.open()
        with mock.patch.object(api_mod.hmac, "compare_digest", wraps=hmac.compare_digest) as spy:
            self.post("/link/auth", {"dev": DEV.hex(), "proof": PROOF_DEV})
        self.assertGreaterEqual(spy.call_count, 3)               # dev, key and the proof itself

    def test_open_without_a_pairing_or_for_another_display_is_unpaired_and_never_counted(self):
        for _ in range(40):
            self.assertEqual(self.open(dev=OTHER_DEV), (401, {"ok": False, "error": "unpaired"}))
        self.assertEqual(self.session.blocked_for(), 0)
        self.assertEqual(self.post("/link/open", {"dev": DEV.hex(), "nd": ND.hex()}, beacon_dev=OTHER_DEV)[1]["error"], "unpaired")
        self.pairing.forget()
        self.assertEqual(self.open(), (401, {"ok": False, "error": "unpaired"}))
        self.assertEqual(self.post("/link/auth", {"dev": DEV.hex(), "proof": PROOF_DEV})[1]["error"], "unpaired")
        self.assertEqual(self.session.blocked_for(), 0)

    def test_a_wrong_proof_is_401_auth_and_burns_the_nonces(self):
        self.open()
        self.assertEqual(self.post("/link/auth", {"dev": DEV.hex(), "proof": "00" * 32}), (401, {"ok": False, "error": "auth"}))
        self.assertFalse(self.session.authenticated)
        self.assertTrue(self.session.auth_failed)
        self.assertEqual(self.post("/link/auth", {"dev": DEV.hex(), "proof": PROOF_DEV})[0], 401)   # the right proof, but the open is used up
        self.handshake()
        self.assertFalse(self.session.auth_failed)

    def test_auth_without_open_for_another_display_or_after_the_timeout_fails(self):
        self.assertEqual(self.post("/link/auth", {"dev": DEV.hex(), "proof": PROOF_DEV})[0], 401)
        self.open()
        self.assertEqual(self.post("/link/auth", {"dev": OTHER_DEV.hex(), "proof": PROOF_DEV})[0], 401)
        self.open()
        self.clock.advance(LinkSession.OPEN_TTL_S + 1)
        self.assertEqual(self.post("/link/auth", {"dev": DEV.hex(), "proof": PROOF_DEV})[0], 401)
        self.assertFalse(self.session.authenticated)
        self.open()
        self.clock.advance(5)
        self.assertEqual(self.post("/link/auth", {"dev": DEV.hex(), "proof": PROOF_DEV})[0], 200)

    def test_ten_failures_in_a_minute_block_for_a_minute(self):
        self.open()
        for i in range(10):
            self.assertEqual(self.post("/link/auth", {"dev": DEV.hex(), "proof": "00" * 32})[0], 401, i)
        self.assertEqual(self.post("/link/open", {"dev": DEV.hex(), "nd": ND.hex()}), (429, {"ok": False, "error": "too many failures"}))
        self.assertEqual(self.post("/link/auth", {"dev": DEV.hex(), "proof": PROOF_DEV})[0], 429)
        self.assertTrue(1 <= self.session.blocked_for() <= 60)
        self.clock.advance(30)
        self.assertEqual(self.open()[0], 429)
        self.clock.advance(31)
        self.handshake()

    def test_failures_older_than_a_minute_do_not_add_up(self):
        for _ in range(9):
            self.open()
            self.post("/link/auth", {"dev": DEV.hex(), "proof": "00" * 32})
        self.clock.advance(61)
        for _ in range(9):
            self.open()
            self.post("/link/auth", {"dev": DEV.hex(), "proof": "00" * 32})
        self.assertEqual(self.session.blocked_for(), 0)

    def test_malformed_bodies_are_400_and_count_as_failures(self):
        bad = [b"", b"not json", b"[]", b'"x"', b"5", b"null", b"{" * 500, b'{"dev": 5}', b'{"dev":"11","nd":"00"}',
               json.dumps({"dev": DEV.hex(), "nd": "zz" * 16}).encode(), json.dumps({"dev": DEV.hex(), "nd": "aa" * 17}).encode(),
               b'{"dev":"' + b"1" * 200 + b'"}']
        for raw in bad[:10]:
            status, body = self.post("/link/open", raw)
            self.assertEqual((status, body), (400, {"ok": False, "error": "bad_request"}), raw[:20])
        self.assertEqual(self.session.blocked_for(), 60)
        self.assertFalse(self.session.authenticated)

    def test_an_open_alone_does_not_revoke_the_session(self):
        """/link/open needs no proof: anybody on the cable could otherwise lock the display out. The session is replaced when /link/auth succeeds."""
        self.handshake()
        self.open()
        self.assertTrue(self.session.authenticated)
        self.assertEqual(self.post("/link/auth", {"dev": DEV.hex(), "proof": "00" * 32})[0], 401)      # a failed second handshake ...
        self.assertTrue(self.session.authenticated)                                                    # ... does not end the first session either

    def test_a_pairing_may_only_be_offered_by_the_display_that_beacons_on_this_cable(self):
        self.pairing.open_window()
        import desk_media_companion.x25519 as x
        pub = x.x25519(bytes(range(32)), x.BASE_POINT)
        offer = {"dev": OTHER_DEV.hex(), "pub": pub.hex(), "ce": hashlib.sha256(pub + H("cc" * 16)).hexdigest()}
        self.assertEqual(self.call("POST", "/pair/offer", usb(False, dev=DEV), json.dumps(offer).encode()).status, 400)
        offer["dev"] = DEV.hex()
        self.assertEqual(self.call("POST", "/pair/offer", usb(False, dev=DEV), json.dumps(offer).encode()).status, 200)

    def test_forgetting_the_pairing_ends_the_session_at_once(self):
        self.handshake()
        self.pairing.forget()
        self.assertFalse(self.session.authenticated)
        self.assertEqual(self.call("GET", "/now-playing", usb(self.session.authenticated, session=self.session)).status, 401)

    def test_a_replaced_pairing_ends_the_session(self):
        self.handshake()
        self.pairing._store.set_device(DEV, H("5a" * 32))           # re-paired: same display, new key
        self.assertFalse(self.session.authenticated)
        self.pairing._store.set_device(DEV, K)
        self.assertFalse(self.session.authenticated)                 # and it does not come back by itself

    def test_drop_ends_everything(self):
        self.handshake()
        self.session.drop()
        self.assertFalse(self.session.authenticated)
        self.open()
        self.session.drop()
        self.assertEqual(self.post("/link/auth", {"dev": DEV.hex(), "proof": PROOF_DEV})[0], 401)

    def test_secrets_never_reach_the_log_and_errors_never_echo(self):
        with self.assertLogs("api", "DEBUG") as logs:
            self.handshake()
            self.session.drop()
        text = "\n".join(logs.output)
        for secret in (K.hex(), ND.hex(), NC.hex(), PROOF_PC, PROOF_DEV, DEV.hex()):
            self.assertNotIn(secret, text)
        status, body = self.post("/link/open", b'{"dev":"ZZMARKER","nd":"ZZMARKER"}')
        self.assertNotIn("ZZMARKER", json.dumps(body))

    def test_without_a_pairing_object_everything_is_unpaired(self):
        session = LinkSession(None, self.clock)
        r = session.handle("/link/open", json.dumps({"dev": DEV.hex(), "nd": ND.hex()}).encode(), DEV)
        self.assertEqual((r.status, body_of(r)["error"]), (401, "unpaired"))
        self.assertFalse(session.authenticated)

    def test_the_session_is_safe_under_concurrent_use(self):
        errors = []

        def hammer(i):
            try:
                for _ in range(200):
                    self.post("/link/open", {"dev": DEV.hex(), "nd": ND.hex()})
                    self.session.authenticated
                    self.session.drop()
            except Exception as exc:                              # pragma: no cover - the assertion below reports it
                errors.append(exc)
        threads = [threading.Thread(target=hammer, args=(i,)) for i in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])


class SharedApiOverHttp(ApiCase):
    def test_make_server_uses_a_shared_api_and_keeps_the_lan_hello_unchanged(self):
        server = make_server(self.mon, 0, api=self.api, bind="127.0.0.1")
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/hello", timeout=5) as r:
            hello = json.loads(r.read().decode())
        self.assertEqual(hello, {"ok": True, "v": 2, "id": CID.hex(), "name": "TEST-PC", "paired": True, "pairing": False})
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/now-playing", timeout=5) as r:     # loopback: exempt
            self.assertEqual(json.loads(r.read().decode())["app"], "Spotify.exe")


if __name__ == "__main__":
    unittest.main()

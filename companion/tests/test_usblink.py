"""The USB link end to end: a simulated display (companion/tools/display_sim.py) on an in-memory cable <-> Link <-> Api with a fake media
monitor. No hardware and no real serial port is ever opened, listed or written (a physical board is attached to the development PC).

Most tests are LOCKSTEP: one thread, a fake clock, a few milliseconds per simulated minute. The `Threads` tests use real threads and real
time (a slow route must not stop the PING answers).

    companion\\.venv\\Scripts\\python.exe -m unittest discover -s companion/tests
"""
import json
import os
import random
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from desk_media_companion import __version__  # noqa: E402
from desk_media_companion import framing as fr  # noqa: E402
from desk_media_companion import usblink  # noqa: E402
from desk_media_companion.api import Api  # noqa: E402
from desk_media_companion.framing import (FLAG_FIRST, FLAG_SINGLE, TYPE_ACK, TYPE_BEACON, TYPE_LOG, TYPE_PING, TYPE_PONG,  # noqa: E402
                                          TYPE_REQUEST, TYPE_RESPONSE, FrameParser, encode_frame, encode_request)
from desk_media_companion.pairing import Pairing, PairingStore  # noqa: E402
from desk_media_companion.usblink import Link, LinkManager, PortInfo  # noqa: E402
from display_client import PairingFailed, ProofError  # noqa: E402
from display_sim import (DisplaySim, DuplexPipe, FakeClock, FakeMonitor, LinkError, ManagerRig, Pending, RealTimeDriver,  # noqa: E402
                         SimRig)


def frames_of(chunks: list[bytes]) -> list:
    """The frames a list of writes (`PipeEnd.written`) contained."""
    return FrameParser().feed(b"".join(chunks))


def raw_request(rig: SimRig, payload: bytes, msg_id: int = 40, flags: int = FLAG_SINGLE, wait: float = 2.0):
    """A REQUEST frame with exactly this payload (so malformed ones can be sent); the answer, or None if nothing came back in `wait` s."""
    frame = encode_frame(TYPE_REQUEST, payload, msg_id=msg_id, flags=flags)
    p = Pending(msg_id, "RAW", "", frame, 16384, rig.clock.t, attempts=99)           # attempts=99: the sim must not retry it
    rig.sim.pending[msg_id] = p
    rig.sim.send_frame(frame)
    try:
        rig.run_until(lambda: p.done, timeout=wait)
    except AssertionError:
        pass
    rig.sim.pending.pop(msg_id, None)
    return p.response


class RigCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def make(self, **kw) -> SimRig:
        return SimRig(self.tmp.name, **kw)

    def connected(self, **kw) -> SimRig:
        rig = self.make(paired=True, **kw)
        rig.connect()
        return rig


# ---- presence: candidate, BEACON, ACK ------------------------------------------------------------------------------------------------------

class BeaconAndAck(RigCase):
    def test_a_port_is_only_a_candidate_until_a_valid_beacon_and_gets_nothing_at_all(self):
        rig = self.make()
        rig.sim.beacons_enabled = False
        rng = random.Random(5)
        for data in (b"ESP-ROM:esp32s3-20210327\r\nBuild:Mar 27 2021\r\nrst:0x1 (POWERON)\r\n", encode_frame(TYPE_PING, b"x", msg_id=1),
                     encode_request(1, "GET", "/now-playing"), encode_frame(TYPE_LOG, b"hi"), encode_frame(0x7F), b"\xfa\xde" * 50,
                     bytes(rng.getrandbits(8) for _ in range(3000))):
            rig.pipe.pc.inject(data)
        rig.advance(5)
        self.assertEqual(rig.pipe.pc.written, [])                     # the companion has not written one byte
        self.assertIsNone(rig.link.display)
        self.assertFalse(rig.link.serving)
        self.assertEqual(rig.link.counters()["ignored_before_beacon"], 4)

    def test_a_valid_beacon_is_acked_with_the_companions_identity_and_compat(self):
        rig = self.make()
        rig.advance(0.5)
        ack_frame = frames_of(rig.pipe.pc.written)[0]
        self.assertEqual((ack_frame.type, ack_frame.msg_id, ack_frame.flags), (TYPE_ACK, 0, FLAG_SINGLE))
        self.assertTrue(ack_frame.payload.startswith(b'{"v":1,"api":2,"min_api":2,"app":"'))
        self.assertEqual(json.loads(ack_frame.payload), {"v": 1, "api": 2, "min_api": 2, "app": __version__, "name": "TEST-PC",
                                                         "id": rig.pairing.cid_hex, "compat": True})
        d = rig.link.display
        self.assertEqual((d.dev, d.fw, d.api, d.min_api, d.paired), (rig.sim.dev, "1.0.0", 2, 2, False))
        self.assertTrue(rig.link.serving and rig.sim.companion_present)

    def test_at_most_one_ack_per_200_ms(self):
        rig = self.make()
        rig.sim.beacons_enabled = False
        beacon = encode_frame(TYPE_BEACON, rig.sim.beacon_payload())
        acks = lambda: len([f for f in frames_of(rig.pipe.pc.written) if f.type == TYPE_ACK])  # noqa: E731
        for _ in range(6):                                            # six beacons in the same instant
            rig.pipe.pc.inject(beacon)
        rig.settle()
        self.assertEqual(acks(), 1)
        rig.clock.advance(0.1)
        rig.pipe.pc.inject(beacon)
        rig.settle()
        self.assertEqual(acks(), 1)                                   # 100 ms later: still suppressed
        rig.clock.advance(0.15)
        rig.pipe.pc.inject(beacon)
        rig.settle()
        self.assertEqual(acks(), 2)                                   # 250 ms after the first: answered
        self.assertGreaterEqual(rig.link.counters()["acks_suppressed"], 6)

    def test_invalid_beacons_do_not_make_a_display(self):
        good = {"v": 1, "api": 2, "min_api": 2, "dev": "0102030405060708", "boot": "01020304", "fw": "1.0.0", "paired": False}
        variants = [b"not json", b"[]", b"null", b"{}", b"\xff\xfe", b"{" * 300,
                    *[json.dumps(dict(good, **{k: v})).encode() for k, v in (
                        ("v", 2), ("v", True), ("v", "1"), ("api", "2"), ("api", 0), ("api", 256), ("api", True), ("min_api", 3), ("min_api", 0),
                        ("dev", "01020304"), ("dev", "zz" * 8), ("dev", 5), ("boot", "0102"), ("boot", "zzzzzzzz"), ("fw", 7), ("paired", "yes"))],
                    *[json.dumps({k: v for k, v in good.items() if k != drop}).encode() for drop in ("v", "api", "min_api", "dev", "boot")],
                    json.dumps(dict(good, fw="x" * 600)).encode()]
        rig = self.make()
        rig.sim.beacons_enabled = False
        for payload in variants:
            rig.pipe.pc.inject(encode_frame(TYPE_BEACON, payload))
            rig.clock.advance(0.3)
            rig.settle()
        rig.pipe.pc.inject(encode_frame(TYPE_BEACON, json.dumps(good).encode(), flags=FLAG_FIRST))     # a beacon is one frame
        rig.settle()
        self.assertEqual(rig.pipe.pc.written, [])
        self.assertIsNone(rig.link.display)
        self.assertEqual(rig.link.counters()["bad_beacons"], len(variants) + 1)
        rig.pipe.pc.inject(encode_frame(TYPE_BEACON, json.dumps(good).encode()))                      # the valid one is accepted
        rig.settle()
        self.assertEqual(rig.link.display.dev.hex(), "0102030405060708")

    def test_unknown_fields_are_ignored_and_the_firmware_text_is_cleaned(self):
        rig = self.make()
        rig.sim.beacon_override = json.dumps({"v": 1, "api": 2, "min_api": 2, "dev": "aa" * 8, "boot": "bb" * 4, "paired": True,
                                              "fw": "1.0.0\x1b[31m\n+dirty", "future": {"x": [1, 2]}}).encode()
        rig.advance(1)
        self.assertEqual(rig.link.display.fw, "1.0.0[31m+dirty")
        self.assertTrue(rig.link.display.paired)


class Compatibility(RigCase):
    def ack_compat(self, rig) -> bool:
        return rig.sim.ack["compat"]

    def test_the_version_overlap_decides(self):
        for api, min_api, compatible in ((2, 2, True), (2, 1, True), (5, 2, True), (7, 3, False), (3, 3, False), (1, 1, False), (1, 1, False)):
            with self.subTest(api=api, min_api=min_api):
                with tempfile.TemporaryDirectory() as d:
                    rig = SimRig(d, sim_kwargs={"api": api, "min_api": min_api})
                    rig.advance(1)
                    self.assertEqual((self.ack_compat(rig), rig.link.compatible), (compatible, compatible))

    def test_an_incompatible_display_gets_426_for_everything_but_hello_and_the_ack_says_so(self):
        rig = self.make(paired=True, sim_kwargs={"api": 1, "min_api": 1})                 # an old firmware: api 1 < min_api 2
        rig.run_until(lambda: rig.sim.companion_present)
        self.assertFalse(rig.sim.ack["compat"])
        hello = rig.sim.get("/hello")
        self.assertEqual((hello.status, hello.json()["transport"], hello.json()["paired_with_you"]), (200, "usb", True))
        for method, target in (("GET", "/now-playing"), ("GET", "/health"), ("POST", "/control/next"), ("POST", "/link/open"), ("POST", "/pair/offer")):
            r = rig.sim.request(method, target, b"{}" if method == "POST" and "/" in target[1:] and target != "/control/next" else b"")
            self.assertEqual((r.status, r.json()), (426, {"ok": False, "error": "update needed"}), (method, target))
        self.assertEqual(rig.monitor.calls, [])
        self.assertFalse(rig.link.authenticated)

    def test_a_display_that_needs_a_newer_companion(self):
        rig = self.make(paired=True, sim_kwargs={"api": 3, "min_api": 3})                 # the display needs api 3, the companion speaks 2
        rig.run_until(lambda: rig.sim.companion_present)
        self.assertFalse(rig.sim.ack["compat"])
        self.assertEqual(rig.sim.get("/now-playing").status, 426)

    def test_the_status_names_which_side_needs_the_update(self):
        with tempfile.TemporaryDirectory() as d:
            rig = ManagerRig(d)
            rig.add_display("COM9", api=1, min_api=1)
            rig.run_until(lambda: rig.state() == "incompatible")
            st = rig.manager.status()
            self.assertEqual((st["update_needed"], st["detail"], st["compatible"]), ("display", "update_display", False))
        with tempfile.TemporaryDirectory() as d:
            rig = ManagerRig(d)
            rig.add_display("COM9", api=3, min_api=3)
            rig.run_until(lambda: rig.state() == "incompatible")
            self.assertEqual(rig.manager.status()["update_needed"], "app")


# ---- keep-alive, PING, unknown frames, LOG --------------------------------------------------------------------------------------------------

class Housekeeping(RigCase):
    def test_a_display_that_goes_silent_for_8_seconds_closes_the_link(self):
        rig = self.connected()
        rig.sim.beacons_enabled = False
        rig.advance(7.5)
        self.assertFalse(rig.link.closed)
        rig.advance(1.0)
        self.assertTrue(rig.link.closed)
        self.assertEqual(rig.link.close_reason, "display silent")
        self.assertFalse(rig.pipe.pc.is_open)                                       # the port was closed
        self.assertFalse(rig.link.authenticated)

    def test_any_valid_frame_counts_as_a_sign_of_life(self):
        rig = self.connected()
        rig.sim.beacons_enabled = False
        for _ in range(8):                                                          # a LOG line every 5 s keeps it up for 40 s
            rig.advance(5)
            rig.sim.log("still here")
        self.assertFalse(rig.link.closed)

    def test_ping_is_answered_with_the_same_bytes(self):
        rig = self.connected()
        rig.sim.ping(b"hello", msg_id=7)
        rig.settle()
        self.assertEqual(rig.sim.pongs, [b"hello"])
        pong = [f for f in frames_of(rig.pipe.pc.written) if f.type == TYPE_PONG][0]
        self.assertEqual((pong.msg_id, pong.payload), (7, b"hello"))
        rig.sim.ping(b"", msg_id=8)
        rig.sim.ping(b"x" * 64, msg_id=9)
        rig.sim.ping(b"x" * 65, msg_id=10)                                          # over 64 bytes: not answered
        rig.settle()
        self.assertEqual(rig.sim.pongs, [b"hello", b"", b"x" * 64])
        self.assertEqual(rig.link.counters()["bad_pings"], 1)

    def test_send_debug_reaches_only_a_served_display_and_only_clean_action_names(self):
        rig = self.make()
        self.assertFalse(rig.link.send_debug("swipe_up"))                                  # a candidate: nothing is written
        rig.sim.beacons_enabled = False
        self.assertEqual(rig.pipe.pc.written, [])
        rig.sim.beacons_enabled = True
        rig.advance(1)
        self.assertTrue(rig.link.send_debug("swipe_up"))
        rig.settle()
        self.assertEqual(rig.sim.debug_actions, ["swipe_up"])
        frame = [f for f in frames_of(rig.pipe.pc.written) if f.type == fr.TYPE_DEBUG][0]
        self.assertEqual((frame.msg_id, frame.single, frame.payload), (0, True, b"swipe_up"))
        for bad in ("", "x" * 400, "tab\t", "café", "new\nline", "\x7f", "esc\x1b[0m"):
            self.assertFalse(rig.link.send_debug(bad), repr(bad))
        self.assertTrue(rig.link.send_debug("x" * 399))
        rig.settle()
        self.assertEqual(len(rig.sim.debug_actions), 2)
        rig.link.close("done")
        self.assertFalse(rig.link.send_debug("swipe_up"))

    def test_unknown_frame_types_are_ignored_counted_and_the_link_keeps_working(self):
        rig = self.connected()
        for ftype in (0x7F, 0x03, 0x12, TYPE_ACK, TYPE_RESPONSE):
            rig.sim.send_frame(encode_frame(ftype, b"future stuff", msg_id=3))
        rig.sim.send_frame(encode_frame(TYPE_PING, b"after"))
        rig.settle()
        self.assertEqual(rig.link.counters()["unknown_types"], 5)
        self.assertEqual(rig.sim.pongs, [b"after"])
        self.assertEqual(rig.sim.get("/now-playing").status, 200)

    def test_log_frames_reach_the_sink_as_clean_capped_lines(self):
        lines = []
        rig = self.connected(link_kwargs={"log_sink": lambda kind, line: lines.append((kind, line))})
        rig.sim.log("I (123) app: first\nsecond line\r\nthird\x1b[31mred\x07\x00 tab\there")
        rig.sim.log("x" * 1000)
        rig.sim.log("\n".join(f"line {i}" for i in range(100)))
        rig.sim.send_frame(encode_frame(TYPE_LOG, b"bad \xff\xfe utf8 \xc3"))
        rig.sim.log("%s %d %(name)s {0} {x.__class__} $(whoami) `id`")
        rig.settle()
        got = [line for kind, line in lines if kind == "log"]
        self.assertEqual(got[0], "I (123) app: first")
        self.assertEqual(got[1], "second line")
        self.assertEqual(got[2], "third[31mred tab here")                           # ESC, BEL and NUL gone, the tab is a space
        self.assertEqual(got[3], "x" * 397 + "...")                                 # capped at 400 characters
        self.assertEqual(got[4:68], [f"line {i}" for i in range(64)])               # at most 64 lines per frame
        self.assertIn("bad", got[68])
        self.assertEqual(got[69], "%s %d %(name)s {0} {x.__class__} $(whoami) `id`")    # delivered as data, never interpreted
        self.assertTrue(all(kind == "log" for kind, _ in lines))

    def test_the_default_sink_logs_display_text_as_data(self):
        rig = self.connected()
        with self.assertLogs("usb.display", "DEBUG") as logs:
            rig.sim.log("%s %(x)s {0}")
            rig.sim.log("W (12) app: a warning %s")
            rig.sim.log("E (13) app: an error")
            rig.sim.log('I (14) pc_media: now playing "Some Title"')
            rig.settle()
        self.assertEqual(logs.output, ["DEBUG:usb.display:%s %(x)s {0}", "INFO:usb.display:W (12) app: a warning %s",
                                       "WARNING:usb.display:E (13) app: an error", 'DEBUG:usb.display:I (14) pc_media: now playing "Some Title"'])

    def test_ordinary_display_lines_stay_out_of_the_normal_log(self):
        rig = self.connected()
        with self.assertLogs("usb.display", "DEBUG") as logs:
            rig.sim.log('I (14) pc_media: now playing "Some Title"')
            rig.settle()
        self.assertEqual([r.levelname for r in logs.records], ["DEBUG"])

    def test_log_floods_are_rate_limited(self):
        lines = []
        rig = self.connected(link_kwargs={"log_sink": lambda kind, line: lines.append(line)})
        for i in range(500):
            rig.sim.log(f"spam {i}")
        rig.settle()
        self.assertLess(len(lines), 260)
        self.assertGreater(rig.link.counters()["log_dropped"], 200)
        rig.advance(3)
        rig.sim.log("later")
        rig.settle()
        self.assertEqual(lines[-1], "later")

    def test_the_development_mode_has_no_line_budget(self):
        lines = []
        rig = self.connected(link_kwargs={"log_sink": lambda kind, line: lines.append(line), "log_unlimited": True})
        for i in range(500):                                          # a screenshot is ~2400 lines in a burst
            rig.sim.log(f"spam {i}")
        rig.settle()
        self.assertEqual(len(lines), 500)
        self.assertEqual(rig.link.counters().get("log_dropped", 0), 0)

    def test_text_between_frames_goes_to_the_sink_as_raw_lines(self):
        lines = []
        rig = self.make(link_kwargs={"log_sink": lambda kind, line: lines.append((kind, line))})
        rig.sim.noise("ESP-ROM:esp32s3-20210327\r\nBuild:Mar 27 2021\r\nrst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)\r\n")
        rig.sim.noise("partial")
        rig.advance(1)
        self.assertEqual(lines, [("raw", "ESP-ROM:esp32s3-20210327"), ("raw", "Build:Mar 27 2021"),
                                 ("raw", "rst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)")])

    def test_a_bug_in_a_frame_handler_is_counted_and_the_link_lives_on(self):
        rig = self.connected()
        with mock.patch.object(Link, "_on_ping", side_effect=RuntimeError("bug")), self.assertLogs("usb", "WARNING") as logs:
            rig.sim.ping(b"x")
            rig.settle()
        self.assertEqual(rig.link.counters()["handler_errors"], 1)
        self.assertEqual([line for line in logs.output if "bug" in line], [])                 # the type is logged, never the text
        self.assertEqual(rig.sim.get("/now-playing").status, 200)

    def test_a_broken_sink_does_not_break_the_link(self):
        def boom(kind, line):
            raise RuntimeError("sink")
        rig = self.connected(link_kwargs={"log_sink": boom})
        rig.sim.log("hello")
        rig.settle()
        self.assertEqual(rig.link.counters()["sink_errors"], 1)
        self.assertEqual(rig.sim.get("/now-playing").status, 200)


# ---- requests and responses ----------------------------------------------------------------------------------------------------------------

class Requests(RigCase):
    def test_routes_are_served_over_the_link_exactly_as_over_http(self):
        rig = self.connected()
        r = rig.sim.get("/now-playing")
        self.assertEqual((r.status, r.json()["title"]), (200, "Synthetic Song"))
        r = rig.sim.post("/control/next?app=Synthetic.exe")
        self.assertEqual((r.status, r.json()["action"]), (200, "next"))
        self.assertEqual(rig.monitor.calls, [("control", "next", None, "Synthetic.exe")])
        self.assertEqual(rig.sim.get("/nonexistent").status, 404)
        self.assertEqual(rig.sim.post("/control/seek").status, 400)
        self.assertEqual(rig.sim.get("/sources").json(), {"ok": True, "sources": []})

    def test_before_the_session_only_the_open_routes_answer(self):
        rig = self.make(paired=True)
        rig.run_until(lambda: rig.sim.companion_present)
        r = rig.sim.get("/now-playing")
        self.assertEqual((r.status, r.json()), (401, {"ok": False, "error": "auth"}))
        hello = rig.sim.get("/hello").json()
        self.assertEqual((hello["transport"], hello["paired_with_you"], hello["paired"], hello["id"]), ("usb", True, True, rig.pairing.cid_hex))
        self.assertEqual(rig.sim.get("/health").status, 200)
        self.assertEqual(rig.monitor.calls, [])
        with tempfile.TemporaryDirectory() as d:
            other = SimRig(d)                                                       # nothing paired at all
            other.advance(1)
            self.assertEqual(other.sim.get("/now-playing").json(), {"ok": False, "error": "unpaired"})
            self.assertEqual(other.sim.get("/hello").json()["paired_with_you"], False)

    def test_malformed_requests_get_400_and_nothing_is_echoed(self):
        rig = self.connected()
        cases = [b"GET /now-playing", b"PUT /x\n", b"get /x\n", b"GET  /x\n", b"GET /\xc3\xa9\n", b"GET x\n", b"GET \n", b"GET\n", b"\n",
                 b"GET /a" + b"b" * 512 + b"\n", b"GET /now-playing\nbody", b"POST /control/next\n{}", b"POST /volume?level=5\nx", b"GET /a b\n",
                 b"GET /ZZMARKER\x00\n", b"ZZMARKER /ZZMARKER\n", b"GET /now-playing HTTP/1.1\n"]
        for payload in cases:
            r = raw_request(rig, payload)
            self.assertIsNotNone(r, payload[:30])
            self.assertEqual((r.status, r.json()), (400, {"ok": False, "error": "bad request"}), payload[:30])
            self.assertNotIn(b"ZZMARKER", r.body)
        r = raw_request(rig, b"GET /now-playing\n", flags=FLAG_FIRST)                # a request is a single frame
        self.assertEqual(r.status, 400)
        r = raw_request(rig, b"POST /pair/offer\n" + b"x" * 1025)
        self.assertEqual((r.status, r.json()["error"]), (413, "body too large"))
        r = raw_request(rig, b"GET /" + b"a" * 511 + b"\n")                          # exactly 512 bytes of target is fine (and unknown)
        self.assertEqual(r.status, 404)
        r = raw_request(rig, b"GET /now-playing?" + "&".join(f"a{i}=1" for i in range(40)).encode() + b"\n")
        self.assertEqual(r.status, 400)
        r = raw_request(rig, b"POST /pair/offer\n{}")                                # a body where one belongs: reaches the pairing code
        self.assertEqual((r.status, r.json()["error"]), (400, "bad_request"))
        self.assertEqual(rig.monitor.calls, [])
        self.assertEqual(rig.sim.get("/now-playing").status, 200)                    # and the link is fine

    def test_a_request_with_id_zero_is_ignored(self):
        rig = self.connected()
        before = rig.link.counters()["responses"]
        rig.sim.send_frame(encode_request(0, "GET", "/now-playing"))
        rig.advance(1)
        self.assertEqual(rig.link.counters()["responses"], before)
        self.assertEqual(rig.link.counters()["bad_requests"], 1)

    def test_a_160_kb_cover_arrives_intact_over_a_randomly_splitting_pipe(self):
        rig = self.connected()
        rig.pipe.to_display.max_chunk = 97                                          # the companion's writes reach the display in odd pieces
        rig.pipe.to_pc.max_chunk = 13
        r = rig.sim.get("/artwork?size=312&maxw=432")
        self.assertEqual(r.status, 200)
        self.assertEqual(len(r.body), 160_000)
        self.assertEqual(r.body, rig.monitor.cover)
        chunks = [f for f in rig.sim.frames_rx if f.type == TYPE_RESPONSE and f.msg_id == 3]
        self.assertGreaterEqual(len(chunks), 40)
        self.assertTrue(all(len(f.payload) <= 4096 for f in chunks))
        self.assertEqual([f.seq for f in chunks], [i & 0xFF for i in range(len(chunks))])
        self.assertEqual(rig.sim.counts["response_abandoned"], 0)

    def test_a_cover_the_display_cannot_hold_is_dropped_by_the_display_not_truncated(self):
        monitor = FakeMonitor(cover_bytes=170_000)                                   # over the display's 160 KB cover limit
        rig = self.make(paired=True, monitor=monitor)
        rig.connect()
        with self.assertRaises(LinkError):
            rig.sim.get("/artwork?size=312")
        self.assertGreaterEqual(rig.sim.counts["response_too_big"], 1)

    def test_a_response_over_512_kb_becomes_a_500(self):
        monitor = FakeMonitor(cover_bytes=600_000)
        rig = self.make(paired=True, monitor=monitor)
        rig.connect()
        rig.sim.JSON_LIMIT = 1 << 20
        r = rig.sim.get("/artwork?size=312")
        self.assertEqual((r.status, r.json()["error"]), (500, "response too large"))

    def test_the_api_raising_is_a_500_with_a_fixed_text(self):
        rig = self.connected()
        rig.monitor.now_playing = lambda: (_ for _ in ()).throw(RuntimeError("ZZSECRET"))
        with self.assertLogs("usb", "WARNING") as logs:
            r = rig.sim.get("/now-playing")
        self.assertNotIn("ZZSECRET", "\n".join(logs.output))                                # the log names the exception type, not its text
        self.assertEqual((r.status, r.json()), (500, {"ok": False, "error": "internal error"}))
        self.assertNotIn(b"ZZSECRET", r.body)

    def test_the_hello_of_a_display_that_is_not_the_paired_one(self):
        rig = self.make(paired=True)
        rig.pairing._store.set_device(bytes(range(8)), bytes(32))                    # the PC is paired with some other display
        rig.run_until(lambda: rig.sim.companion_present)
        hello = rig.sim.get("/hello").json()
        self.assertEqual((hello["paired"], hello["paired_with_you"]), (True, False))
        r = rig.sim.post("/link/open", {"dev": rig.sim.dev.hex(), "nd": "00" * 16})
        self.assertEqual((r.status, r.json()["error"]), (401, "unpaired"))


class DuplicateRequests(RigCase):
    def test_a_lost_response_is_replayed_and_the_request_is_not_executed_again(self):
        rig = self.connected()
        rig.pipe.to_display.queue("drop")                                             # the answer to the next request never arrives
        r = rig.sim.post("/control/next?app=Synthetic.exe")
        self.assertEqual((r.status, r.json()["n"]), (200, 1))
        self.assertEqual(rig.monitor.control_count, 1)                                # the real side effect happened exactly once
        counters = rig.link.counters()
        self.assertEqual((counters["duplicates_replayed"], counters["requests"]), (1, 3))       # open, auth, next
        self.assertEqual(rig.sim.next_id, 4)

    def test_a_corrupted_response_is_replayed_too_and_a_big_one_in_full(self):
        rig = self.connected()
        rig.pipe.to_display.queue("pass", "corrupt")                                  # the second chunk of the cover is damaged
        r = rig.sim.get("/artwork?size=312&maxw=432")
        self.assertEqual(r.body, rig.monitor.cover)
        self.assertEqual(rig.monitor.art_calls, 1)                                    # replayed from memory: the monitor was asked once
        rig.pipe.to_display.queue("corrupt")
        r = rig.sim.post("/control/previous")
        self.assertEqual((r.status, rig.monitor.control_count), (200, 1))

    def test_the_same_id_with_different_bytes_is_a_new_request(self):
        rig = self.connected()
        rig.sim.wait(rig.sim.request_async("POST", "/control/next", msg_id=50))
        rig.sim.wait(rig.sim.request_async("POST", "/control/previous", msg_id=50))      # the counter wrapped: same ID, other bytes
        self.assertEqual([c[1] for c in rig.monitor.calls], ["next", "previous"])

    def test_the_same_id_and_bytes_after_10_seconds_is_executed_again(self):
        rig = self.connected()
        rig.sim.wait(rig.sim.request_async("POST", "/control/next", msg_id=60))
        rig.advance(9)
        rig.sim.wait(rig.sim.request_async("POST", "/control/next", msg_id=60))          # within 10 s: replayed
        self.assertEqual(rig.monitor.control_count, 1)
        rig.advance(2)                                                                  # the first answer was remembered at t, now t + 11 s
        rig.sim.wait(rig.sim.request_async("POST", "/control/next", msg_id=60))
        self.assertEqual(rig.monitor.control_count, 2)

    def test_only_the_last_eight_exchanges_are_remembered(self):
        rig = self.connected()
        for i in range(9):
            rig.sim.wait(rig.sim.request_async("POST", "/control/next", msg_id=100 + i))
        self.assertEqual(rig.monitor.control_count, 9)
        rig.sim.wait(rig.sim.request_async("POST", "/control/next", msg_id=108))          # the newest: replayed
        self.assertEqual(rig.monitor.control_count, 9)
        rig.sim.wait(rig.sim.request_async("POST", "/control/next", msg_id=100))          # the oldest was pushed out: executed
        self.assertEqual(rig.monitor.control_count, 10)

    def test_a_duplicate_frame_on_the_wire_is_executed_once(self):
        rig = self.connected()
        rig.pipe.to_pc.queue("duplicate")
        r = rig.sim.post("/control/next")
        self.assertEqual(r.status, 200)
        rig.settle()
        self.assertEqual(rig.monitor.control_count, 1)
        self.assertEqual(rig.link.counters()["duplicates_replayed"], 1)

    def test_a_remembered_private_answer_is_not_replayed_after_the_session_ended(self):
        """Duplicate suppression must not serve now-playing data to a link whose pairing was forgotten in the meantime."""
        rig = self.connected()
        first = rig.sim.wait(rig.sim.request_async("GET", "/now-playing", msg_id=500))
        self.assertEqual(first.status, 200)
        rig.pairing.forget()                                                               # the session ends at once
        again = rig.sim.wait(rig.sim.request_async("GET", "/now-playing", msg_id=500))     # same id, same bytes, well inside the memory window
        self.assertEqual(again.status, 401)
        self.assertEqual(rig.link.counters().get("duplicates_replayed", 0), 0)

    def test_a_display_reboot_forgets_the_memory_so_a_reused_id_is_a_new_request(self):
        rig = self.connected()
        r = rig.sim.post("/control/next")                                                # id 3 of the first life
        self.assertEqual(r.json()["n"], 1)
        rig.sim.reboot()
        rig.run_until(lambda: rig.sim.companion_present)
        self.assertFalse(rig.link.authenticated)                                          # the new boot ended the session
        self.assertEqual(rig.sim.open_session().status, 200)                              # ids 1 and 2 again
        r = rig.sim.post("/control/next")                                                # id 3 again, the same bytes as before the reboot
        self.assertEqual(r.json()["n"], 2)
        self.assertEqual(rig.monitor.control_count, 2)


# ---- damaged traffic ---------------------------------------------------------------------------------------------------------------------------

class FaultInjection(RigCase):
    def test_a_truncated_request_is_never_answered_and_the_retry_succeeds(self):
        rig = self.connected()
        rig.pipe.to_pc.queue("truncate")
        r = rig.sim.post("/control/next")
        self.assertEqual((r.status, rig.monitor.control_count), (200, 1))

    def test_a_corrupted_request_is_never_answered_and_the_retry_succeeds(self):
        rig = self.connected()
        for _ in range(5):
            rig.pipe.to_pc.queue("corrupt")
            r = rig.sim.post("/control/next")
            self.assertEqual(r.status, 200)
        self.assertEqual(rig.monitor.control_count, 5)                                    # each exactly once
        c = rig.link.counters()
        self.assertGreaterEqual(c["parser_bad_crc"] + c["parser_bad_hcrc"] + c["parser_noise_bytes"], 5)

    def test_garbage_and_a_flood_before_a_request_cost_nothing(self):
        rig = self.connected()
        rig.pipe.to_pc.queue("garbage", "flood", "garbage")
        for _ in range(3):
            self.assertEqual(rig.sim.get("/now-playing").status, 200)
        c = rig.link.counters()
        self.assertGreater(c["parser_noise_bytes"], 15_000)
        self.assertEqual(rig.monitor.control_count, 0)

    def test_a_split_pipe_and_split_frames_are_fine(self):
        rig = self.connected()
        rig.pipe.to_pc.max_chunk = 3
        rig.pipe.to_display.max_chunk = 5
        rig.pipe.to_pc.queue("split", "split")
        for _ in range(4):
            self.assertEqual(rig.sim.post("/control/next").status, 200)
        self.assertEqual(rig.monitor.control_count, 4)

    def test_an_oversize_len_a_newer_version_and_a_wedge_attempt_are_survived(self):
        rig = self.connected()
        head = bytearray(fr._HEAD_NO_CRC.pack(fr.MAGIC, 1, TYPE_LOG, 3, 0, 5, 5000))
        head += fr._U16.pack(fr.crc16_ccitt(bytes(head)))                                # a valid header claiming 5000 bytes
        rig.pipe.pc.inject(bytes(head) + b"\x00" * 100)
        rig.pipe.pc.inject(encode_frame(TYPE_LOG, b"from the future", ver=2))
        rig.pipe.pc.inject(b"\xfa\xde\x01\x10")                                           # the start of a frame that never finishes
        rig.settle()
        self.assertEqual(rig.sim.get("/now-playing").status, 200)
        c = rig.link.counters()
        self.assertEqual((c["parser_bad_len"], c["parser_unknown_ver"]), (1, 1))

    def test_a_slow_trickle_within_one_second_is_answered_and_a_slower_one_is_not(self):
        rig = self.connected()
        frame = encode_request(77, "GET", "/health")
        before = rig.link.counters()["responses"]
        for b in frame:                                                                   # 0.01 s per byte: well within a second
            rig.pipe.pc.inject(bytes([b]))
            rig.clock.advance(0.01)
            rig.step()
        self.assertEqual(rig.link.counters()["responses"], before + 1)
        frame = encode_request(78, "GET", "/health")
        for b in frame:                                                                   # 0.1 s per byte: far too slow
            rig.pipe.pc.inject(bytes([b]))
            rig.clock.advance(0.1)
            rig.step()
        rig.advance(2)
        self.assertEqual(rig.link.counters()["responses"], before + 1)                    # abandoned, never answered
        self.assertGreaterEqual(rig.link.counters()["parser_partial_timeouts"], 1)
        self.assertEqual(rig.sim.get("/now-playing").status, 200)                         # and nothing is wedged

    def test_a_cable_pulled_in_the_middle_of_a_response_closes_the_link(self):
        rig = self.connected()
        rig.pipe.pc.fail_after = (5, OSError("WriteFile failed (PermissionError(13, 'Access is denied.', None, 5))"))
        rig.sim.request_async("GET", "/artwork?size=312")
        rig.run_until(lambda: rig.link.closed, timeout=5)
        self.assertTrue(rig.link.close_reason.startswith("write failed"))
        self.assertFalse(rig.pipe.pc.is_open)

    def test_a_write_that_times_out_leaves_the_link_up_and_the_retry_gets_the_full_answer(self):
        rig = self.connected()
        rig.pipe.pc.fail_after = (3, TimeoutError("write timeout"))
        r = rig.sim.get("/artwork?size=312")
        self.assertEqual(r.body, rig.monitor.cover)
        self.assertFalse(rig.link.closed)
        self.assertEqual(rig.link.counters()["write_timeouts"], 1)
        self.assertEqual(rig.monitor.art_calls, 1)

    def test_a_read_error_closes_the_link_cleanly(self):
        rig = self.connected()
        rig.pipe.unplug()
        rig.step()
        self.assertTrue(rig.link.closed)
        self.assertTrue(rig.link.close_reason.startswith("read failed"))
        self.assertFalse(rig.link.pump(0))                                                # a closed link stays closed and quiet

    def test_a_garbage_flood_with_magics_keeps_the_buffer_bounded(self):
        rig = self.connected()
        for _ in range(30):
            rig.pipe.pc.inject(b"\xfa\xde" * 3000)
            rig.settle()
        self.assertLessEqual(len(rig.link._parser), fr.MAX_BUFFER)
        self.assertEqual(rig.sim.get("/now-playing").status, 200)


# ---- the session --------------------------------------------------------------------------------------------------------------------------------

class Session(RigCase):
    def test_the_handshake_and_a_wrong_key_on_either_side(self):
        rig = self.make(paired=True)
        rig.run_until(lambda: rig.sim.companion_present)
        self.assertFalse(rig.link.authenticated)
        r = rig.sim.open_session()
        self.assertEqual((r.status, r.json()), (200, {"ok": True}))
        self.assertTrue(rig.link.authenticated and rig.sim.authenticated)
        # a display holding another key notices that the COMPANION'S proof does not verify and refuses to go on
        rig.sim.key = bytes(32)
        with self.assertRaises(ProofError):
            rig.sim.open_session()
        self.assertTrue(rig.link.authenticated)                                           # an /link/open alone does not end a live session

    def test_a_bad_proof_is_401_and_ten_of_them_block_the_handshake_for_a_minute(self):
        rig = self.make(paired=True)
        rig.run_until(lambda: rig.sim.companion_present)
        for i in range(10):
            self.assertEqual(rig.sim.post("/link/open", {"dev": rig.sim.dev.hex(), "nd": "ab" * 16}).status, 200)
            r = rig.sim.post("/link/auth", {"dev": rig.sim.dev.hex(), "proof": "00" * 32})
            self.assertEqual((r.status, r.json()), (401, {"ok": False, "error": "auth"}), i)
        self.assertFalse(rig.link.authenticated)
        r = rig.sim.post("/link/open", {"dev": rig.sim.dev.hex(), "nd": "ab" * 16})
        self.assertEqual((r.status, r.json()["error"]), (429, "too many failures"))
        self.assertEqual(rig.sim.get("/now-playing").status, 429)                         # the session routes stay blocked ...
        self.assertEqual(rig.sim.get("/hello").status, 200)                               # ... the open ones answer
        rig.advance(61)
        self.assertEqual(rig.sim.open_session().status, 200)
        self.assertTrue(rig.link.authenticated)

    def test_forgetting_the_pairing_ends_the_session_at_once(self):
        rig = self.connected()
        self.assertEqual(rig.sim.get("/now-playing").status, 200)
        rig.pairing.forget()
        r = rig.sim.get("/now-playing")
        self.assertEqual((r.status, r.json()["error"]), (401, "unpaired"))
        self.assertFalse(rig.link.authenticated)
        self.assertEqual(rig.sim.post("/link/open", {"dev": rig.sim.dev.hex(), "nd": "00" * 16}).json()["error"], "unpaired")

    def test_re_pairing_with_another_key_ends_the_old_session(self):
        rig = self.connected()
        rig.pairing._store.set_device(rig.sim.dev, bytes(range(1, 33)))
        self.assertEqual(rig.sim.get("/now-playing").status, 401)

    def test_a_display_reboot_drops_the_session_even_though_the_port_stayed_open(self):
        rig = self.connected()
        rig.sim.reboot()
        rig.run_until(lambda: rig.sim.companion_present)
        self.assertFalse(rig.link.authenticated)
        self.assertEqual(rig.sim.get("/now-playing").status, 401)
        self.assertFalse(rig.link.closed)
        rig.sim.open_session()
        self.assertEqual(rig.sim.get("/now-playing").status, 200)
        self.assertEqual(rig.link.counters()["display_resets"], 1)

    def test_closing_the_link_ends_the_session(self):
        rig = self.connected()
        session = rig.link.session
        rig.link.close("test")
        self.assertFalse(session.authenticated)

    def test_first_time_pairing_over_the_link(self):
        rig = self.make()
        rig.advance(1)
        rig.pairing.open_window()
        codes = []

        def on_code(code):
            codes.append(code)
            return code == rig.pairing.snapshot()["code"]                                 # "the same code on both screens"
        attempt = rig.sim.pair(on_code=on_code, after_confirm=rig.pairing.pc_confirm)
        self.assertTrue(rig.pairing.paired and rig.sim.paired)
        self.assertEqual(rig.pairing.key(), rig.sim.key)
        self.assertEqual(rig.pairing.device_id, rig.sim.dev)                              # the PC's record names the display by its persistent dev
        self.assertEqual(attempt.dev, rig.sim.dev)
        self.assertTrue(rig.pairing.paired_with(rig.link.display.dev))
        rig.advance(2)                                                                    # the next beacon says "paired"
        self.assertTrue(rig.link.display.paired)
        self.assertEqual(rig.sim.open_session().status, 200)
        self.assertEqual(rig.sim.get("/now-playing").status, 200)

    def test_pairing_is_refused_without_a_window_and_a_cancel_ends_it(self):
        rig = self.make()
        rig.advance(1)
        with self.assertRaises(PairingFailed) as ctx:
            rig.sim.pair()
        self.assertEqual((ctx.exception.status, ctx.exception.error), (403, "pairing_closed"))
        rig.pairing.open_window()
        with self.assertRaises(PairingFailed) as ctx:
            rig.sim.pair(on_code=lambda code: False)                                       # the user taps Cancel on the display
        self.assertEqual(ctx.exception.error, "cancelled_by_user")
        self.assertFalse(rig.pairing.paired)

    def test_pairing_secrets_never_reach_the_log(self):
        rig = self.make()
        rig.advance(1)
        rig.pairing.open_window()
        with self.assertLogs(level="DEBUG") as logs:
            a = rig.sim.pair(after_confirm=rig.pairing.pc_confirm)
            rig.sim.open_session()
            rig.link.close("done")
        text = "\n".join(logs.output)
        for secret in (a.derived.key.hex(), a.derived.code, a.n_e.hex(), a.derived.proof_dev.hex(), a.derived.proof_pc.hex(), a.pub_e.hex()):
            self.assertNotIn(secret, text)


# ---- reconnecting -------------------------------------------------------------------------------------------------------------------------------

class Reconnects(RigCase):
    def test_unplug_and_replug_reconnects_without_pairing_again(self):
        rig = self.connected()
        key = rig.pairing.key()
        rig.pipe.unplug()
        rig.advance(1)
        self.assertTrue(rig.link.closed)
        rig.advance(7)
        self.assertFalse(rig.sim.companion_present)                                        # the display noticed: no ACK for 6 s
        rig.pipe.plug()
        rig.link = rig.new_link()                                                           # the manager opens the port again
        rig.connect()
        self.assertEqual(rig.sim.get("/now-playing").status, 200)
        self.assertEqual(rig.pairing.key(), key)
        self.assertEqual(rig.link.counters()["requests"], 3)                                # hello-less: open, auth, now-playing - no /pair/*

    def test_a_display_reboot_reconnects(self):
        rig = self.connected()
        rig.sim.reboot()
        rig.run_until(lambda: rig.sim.companion_present)
        rig.sim.open_session()
        self.assertEqual(rig.sim.get("/now-playing").status, 200)

    def test_a_companion_restart_is_survived_by_both_sides(self):
        rig = self.connected()
        rig.link.close("companion exits")                                                   # the process is gone; so is its port handle
        with self.assertRaises(LinkError):
            rig.sim.get("/now-playing")                                                     # times out (3 s, one retry, 3 s)
        self.assertFalse(rig.sim.companion_present)
        rig.link = rig.new_link()                                                           # the companion starts again and opens the port
        rig.run_until(lambda: rig.sim.companion_present)
        r = rig.sim.get("/now-playing")
        self.assertEqual((r.status, r.json()["error"]), (401, "auth"))                      # the new process has no session ...
        rig.sim.open_session()                                                              # ... the display opens one (the pairing is on disk)
        self.assertEqual(rig.sim.get("/now-playing").status, 200)

    def test_the_pairing_survives_a_restart_of_the_whole_companion(self):
        rig = self.connected()
        again = Pairing(PairingStore(self.tmp.name), name="TEST-PC")
        self.assertTrue(again.paired_with(rig.sim.dev))
        self.assertEqual(again.key(), rig.sim.key)


# ---- the manager ------------------------------------------------------------------------------------------------------------------------------

class ManagerBasics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rig = ManagerRig(self.tmp.name)

    def test_searching_when_nothing_is_plugged_in(self):
        self.rig.advance(5)
        st = self.rig.manager.status()
        self.assertEqual((st["state"], st["port"], st["dev"], st["ports"]), ("searching", "", "", []))
        self.assertEqual(self.rig.opens, [])

    def test_a_paired_display_connects_by_itself_and_the_status_describes_it(self):
        sim = self.rig.add_display("COM9", fw="1.2.3")
        self.rig.pair_instantly("COM9")
        self.rig.run_until(lambda: self.rig.state() == "probing" and self.rig.manager.status()["detail"] == "starting_session")
        sim.open_session()
        self.rig.run_until(lambda: self.rig.state() == "connected")
        st = self.rig.manager.status()
        self.assertEqual((st["port"], st["dev"], st["dev_short"], st["fw"], st["api"], st["min_api"]),
                         ("COM9", sim.dev.hex(), sim.dev.hex()[:8], "1.2.3", 2, 2))
        self.assertTrue(st["paired_with_you"] and st["display_paired"] and st["compatible"] and st["authenticated"])
        self.assertGreater(st["link"]["frames_rx"], 0)
        self.assertEqual(sim.get("/now-playing").status, 200)
        kinds = [(e["type"], e["status"]["state"]) for e in self.rig.events if e["type"] == "status"]
        self.assertEqual(kinds[0][1], "probing")
        self.assertEqual(kinds[-1], ("status", "connected"))

    def test_listeners_get_changes_only_and_a_broken_listener_does_not_stop_the_others(self):
        calls = []
        self.rig.manager.add_listener(lambda e: (_ for _ in ()).throw(RuntimeError("broken")))
        self.rig.manager.add_listener(calls.append)
        sim = self.rig.add_display("COM9")
        self.rig.pair_instantly("COM9")
        with self.assertLogs("usb", "WARNING"):                                             # the broken listener is reported, once per event
            self.rig.advance(4)
            n = len(calls)
            self.rig.advance(10)                                                            # nothing changes: no new events
            self.assertEqual(len(calls), n)
            self.assertTrue(all(e["type"] in ("status", "pair-request") for e in calls))
            sim.open_session()
            self.rig.run_until(lambda: self.rig.state() == "connected")
            self.rig.advance(1)                                                             # the link woke the scanner: it reports the change
        self.assertGreater(len(calls), n)
        self.assertEqual(calls[-1]["status"]["state"], "connected")

    def test_the_manager_forwards_a_debug_action_to_the_served_display(self):
        sim = self.rig.add_display("COM9")
        self.assertFalse(self.rig.manager.send_debug("key_up"))                              # nothing is connected yet
        self.rig.pair_instantly("COM9")
        self.rig.run_until(lambda: self.rig.manager.status()["detail"] == "starting_session")
        self.assertTrue(self.rig.manager.send_debug("key_up"))
        self.assertFalse(self.rig.manager.send_debug("not printable \x00"))
        self.rig.advance(0.5)
        self.assertEqual(sim.debug_actions, ["key_up"])

    def test_a_listener_can_be_removed(self):
        calls = []
        self.rig.manager.add_listener(calls.append)
        self.rig.manager.remove_listener(calls.append)
        self.rig.advance(3)
        self.assertEqual(calls, [])

    def test_unplug_then_replug_comes_back_by_itself(self):
        sim = self.rig.add_display("COM9")
        self.rig.pair_instantly("COM9")
        self.rig.run_until(lambda: self.rig.manager.status()["detail"] == "starting_session")
        sim.open_session()
        self.rig.run_until(lambda: self.rig.state() == "connected")
        self.rig.unplug("COM9")
        self.rig.run_until(lambda: self.rig.state() == "searching")
        self.assertEqual(self.rig.manager.status()["ports"], [])
        self.rig.advance(8)
        self.rig.replug("COM9")
        self.rig.run_until(lambda: self.rig.manager.status()["detail"] == "starting_session")
        sim.open_session()
        self.rig.run_until(lambda: self.rig.state() == "connected")
        self.assertEqual(sim.get("/now-playing").status, 200)

    def test_a_board_reboot_on_an_open_port_drops_the_session_and_comes_back(self):
        sim = self.rig.add_display("COM9")
        self.rig.pair_instantly("COM9")
        self.rig.run_until(lambda: self.rig.manager.status()["detail"] == "starting_session")
        sim.open_session()
        self.rig.run_until(lambda: self.rig.state() == "connected")
        sim.reboot()
        self.rig.run_until(lambda: self.rig.state() == "probing")
        sim.open_session()
        self.rig.run_until(lambda: self.rig.state() == "connected")

    def test_a_companion_restart_reconnects_the_paired_display_without_pairing(self):
        sim = self.rig.add_display("COM9")
        self.rig.pair_instantly("COM9")
        self.rig.run_until(lambda: self.rig.manager.status()["detail"] == "starting_session")
        sim.open_session()
        self.rig.run_until(lambda: self.rig.state() == "connected")
        self.rig.restart_companion()
        self.assertTrue(self.rig.pairing.paired)
        self.rig.run_until(lambda: self.rig.manager.status()["detail"] == "starting_session")
        sim.open_session()
        self.rig.run_until(lambda: self.rig.state() == "connected")
        self.assertEqual([e for e in self.rig.events if e["type"] == "pair-request"], [])

    def test_a_display_that_stops_talking_is_dropped_and_found_again(self):
        sim = self.rig.add_display("COM9")
        self.rig.pair_instantly("COM9")
        self.rig.run_until(lambda: self.rig.manager.status()["detail"] == "starting_session")
        sim.open_session()
        self.rig.run_until(lambda: self.rig.state() == "connected")
        sim.beacons_enabled = False
        self.rig.run_until(lambda: self.rig.state() == "probing", timeout=20)
        opens = len(self.rig.opens)
        sim.beacons_enabled = True
        self.rig.run_until(lambda: self.rig.manager.status()["detail"] == "starting_session", timeout=20)
        self.assertGreater(len(self.rig.opens), opens)
        sim.open_session()
        self.rig.run_until(lambda: self.rig.state() == "connected")

    def test_a_port_that_never_beacons_is_retried_with_a_growing_back_off_and_never_written_to(self):
        sim = self.rig.add_display("COM9")
        sim.beacons_enabled = False                                                         # some other device with the same VID / PID
        self.rig.advance(130)
        times = [t for _n, t in self.rig.open_times]
        gaps = [round(b - a, 1) for a, b in zip(times, times[1:])]
        self.assertGreaterEqual(len(times), 8)
        for got, want in zip(gaps[:4], (6.0, 7.8, 10.7, 13.0)):                              # 3 s probe + back-off 3, 4.8, 7.7, 10 s
            self.assertAlmostEqual(got, want, delta=0.35, msg=str(gaps))
        self.assertTrue(all(g <= 13.4 for g in gaps), gaps)                                  # capped
        self.assertEqual(self.rig.pipes["COM9"].pc.written if self.rig.pipes["COM9"].pc else [], [])
        self.assertEqual(self.rig.state(), "probing")
        sim.beacons_enabled = True                                                          # it was only rebooting
        self.rig.pair_instantly("COM9")
        self.rig.run_until(lambda: self.rig.manager.status()["detail"] == "starting_session", timeout=40)

    def test_only_ports_with_the_right_vid_and_pid_are_opened(self):
        self.rig.add_display("COM3", vid_pid=(0x0403, 0x6001))                              # some USB-serial adapter
        self.rig.add_display("COM4", vid_pid=(0x303A, 0x4001))
        self.rig.advance(10)
        self.assertEqual(self.rig.opens, [])
        self.rig.add_display("COM5")
        self.rig.advance(3)
        self.assertEqual(set(self.rig.opens), {"COM5"})

    def test_usb_port_restricts_the_scan_to_one_port(self):
        rig = ManagerRig(self.tmp.name, only_port="com10")
        rig.add_display("COM9")
        rig.add_display("COM10", vid_pid=(0x1234, 0x5678))                                   # development: any VID / PID, but only this port
        rig.advance(5)
        self.assertEqual(set(rig.opens), {"COM10"})
        self.assertEqual(rig.manager.status()["ports"], ["COM10"])

    def test_a_failing_port_listing_is_survived(self):
        sim = self.rig.add_display("COM9")
        self.rig.pair_instantly("COM9")
        self.rig.run_until(lambda: self.rig.manager.status()["detail"] == "starting_session")
        sim.open_session()
        self.rig.run_until(lambda: self.rig.state() == "connected")
        good = self.rig._list
        self.rig.manager._lister = mock.Mock(side_effect=OSError("registry"))
        with self.assertLogs("usb", "WARNING"):                                             # reported once, not on every scan
            self.rig.advance(6)
        self.assertEqual(self.rig.state(), "connected")                                     # nothing was torn down because of a hiccup
        self.rig.manager._lister = good
        self.rig.advance(4)
        self.assertEqual(self.rig.state(), "connected")

    def test_stop_closes_everything(self):
        self.rig.add_display("COM9")
        self.rig.advance(2)
        link = self.rig.manager._recs["COM9"].link
        self.rig.manager.stop()
        self.assertTrue(link.closed)
        self.assertEqual(link.close_reason, "stopped")


class ManagerPorts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rig = ManagerRig(self.tmp.name)

    def test_a_port_held_by_another_program_is_reported_busy_and_retried_with_back_off(self):
        self.rig.add_display("COM9")
        self.rig.busy["COM9"] = PermissionError(13, "Access is denied.", None, 5)
        self.rig.advance(60)
        st = self.rig.manager.status()
        self.assertEqual((st["state"], st["detail"], st["port"]), ("busy", "port_in_use", "COM9"))
        times = [t for _n, t in self.rig.open_times]
        gaps = [round(b - a, 1) for a, b in zip(times, times[1:])]
        for got, want in zip(gaps[:5], (2.0, 3.2, 5.1, 8.2, 10.0)):
            self.assertAlmostEqual(got, want, delta=0.25, msg=str(gaps))
        self.assertLessEqual(len(times), 10)                                                # polite: nowhere near one try per scan
        del self.rig.busy["COM9"]                                                           # the other program let go
        self.rig.pair_instantly("COM9")
        self.rig.run_until(lambda: self.rig.state() == "probing" and self.rig.manager.status()["detail"] == "starting_session", timeout=30)

    def test_pyserial_style_access_denied_messages_count_as_busy_other_errors_do_not(self):
        for exc, kind in ((PermissionError(13, "Access is denied."), "busy"),
                          (OSError("could not open port 'COM9': PermissionError(13, 'Access is denied.', None, 5)"), "busy"),
                          (OSError("could not open port 'COM9': FileNotFoundError(2, 'The system cannot find the file specified.', None, 2)"), "gone"),
                          (FileNotFoundError(2, "x"), "gone"), (OSError("something else"), "error"), (ValueError("odd"), "error"),
                          (OSError(5, "io", None, 5), "busy")):
            self.assertEqual(usblink.classify_open_error(exc), kind, repr(exc))
        self.rig.add_display("COM9")
        self.rig.busy["COM9"] = OSError("could not open port 'COM9': FileNotFoundError(2, 'x', None, 2)")
        self.rig.advance(10)
        self.assertNotEqual(self.rig.state(), "busy")
        self.assertGreaterEqual(len(self.rig.opens), 4)                                     # a vanished port is simply tried again each scan
        self.rig.busy["COM9"] = ValueError("a driver bug")                                  # any exception at all is survived
        self.rig.advance(6)
        self.assertEqual(self.rig.manager.status()["ports"], ["COM9"])

    def test_two_displays_on_the_cable_one_is_served_the_other_only_reported(self):
        a = self.rig.add_display("COM9")
        b = self.rig.add_display("COM10")
        self.rig.pair_instantly("COM9")
        self.rig.run_until(lambda: self.rig.manager.status()["detail"] == "starting_session")
        a.open_session()
        self.rig.run_until(lambda: self.rig.state() == "connected")
        st = self.rig.manager.status()
        self.assertEqual((st["port"], st["dev"]), ("COM9", a.dev.hex()))
        self.assertEqual(st["others"], [{"port": "COM10", "dev": b.dev.hex(), "dev_short": b.dev.hex()[:8]}])
        self.rig.advance(12)
        self.assertFalse(b.companion_present)                                               # never ACKed ...
        with self.assertRaises(LinkError):
            b.get("/hello")                                                                   # ... never answered
        self.assertEqual(self.rig.pipes["COM10"].pc.written, [])                            # not one byte written to it
        self.assertEqual(a.get("/now-playing").status, 200)
        self.assertEqual([e for e in self.rig.events if e["type"] == "pair-request"], [])   # the second display does not start a pairing
        self.rig.unplug("COM9")                                                              # the first goes away: the second is served
        self.rig.run_until(lambda: b.companion_present, timeout=30)
        self.rig.run_until(lambda: self.rig.manager.status()["port"] == "COM10")
        self.assertEqual(b.get("/hello").json()["paired_with_you"], False)

    def test_a_display_that_vanishes_while_the_companion_is_writing(self):
        sim = self.rig.add_display("COM9")
        self.rig.pair_instantly("COM9")
        self.rig.run_until(lambda: self.rig.manager.status()["detail"] == "starting_session")
        sim.open_session()
        self.rig.run_until(lambda: self.rig.state() == "connected")
        pc = self.rig.pipes["COM9"].pc
        pc.fail_after = (7, OSError("WriteFile failed"))
        sim.request_async("GET", "/artwork?size=100")
        self.rig.run_until(lambda: self.rig.manager._recs["COM9"].link is None or self.rig.manager._recs["COM9"].link.closed, timeout=10)
        self.rig.run_until(lambda: self.rig.manager._recs["COM9"].link is not None and not self.rig.manager._recs["COM9"].link.closed, timeout=10)
        sim.pending.clear()
        self.rig.run_until(lambda: self.rig.manager.status()["detail"] == "starting_session", timeout=20)
        sim.open_session()
        self.rig.run_until(lambda: self.rig.state() == "connected")


# ---- the pairing policy -------------------------------------------------------------------------------------------------------------------------

class PairingPolicy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rig = ManagerRig(self.tmp.name)

    def requests(self):
        return [e for e in self.rig.events if e["type"] == "pair-request"]

    def test_an_unpaired_display_with_no_paired_one_opens_the_window_by_itself_and_the_pairing_completes(self):
        sim = self.rig.add_display("COM9")
        self.rig.run_until(lambda: self.rig.state() == "pairing")
        self.assertTrue(self.rig.pairing.window_open)
        self.assertEqual(self.requests(), [{"type": "pair-request", "dev": sim.dev.hex()[:8], "port": "COM9", "seconds": 120}])
        st = self.rig.manager.status()
        self.assertEqual((st["detail"], st["paired_with_you"], st["display_paired"]), ("not_paired", False, False))
        codes = []
        sim.pair(on_code=lambda c: codes.append(c) or c == self.rig.pairing.snapshot()["code"], after_confirm=self.rig.pairing.pc_confirm)
        self.assertEqual(len(codes), 1)
        self.assertTrue(self.rig.pairing.paired_with(sim.dev))
        sim.open_session()
        self.rig.run_until(lambda: self.rig.state() == "connected")
        self.assertEqual(len(self.requests()), 1)                                           # one offer, ever

    def test_the_window_is_not_offered_again_for_the_same_appearance_but_for_a_new_one(self):
        sim = self.rig.add_display("COM9")
        self.rig.run_until(lambda: len(self.requests()) == 1)
        self.rig.advance(200)                                                                 # the 120 s window ran out unused
        self.assertFalse(self.rig.pairing.window_open)
        self.assertEqual(len(self.requests()), 1)                                             # not nagging
        self.assertEqual(self.rig.state(), "unpaired")
        sim.reboot()                                                                          # a new appearance: replug / restart of the display
        self.rig.run_until(lambda: len(self.requests()) == 2, timeout=30)

    def test_a_cancelled_attempt_is_not_offered_again_for_ten_minutes(self):
        sim = self.rig.add_display("COM9")
        self.rig.run_until(lambda: len(self.requests()) == 1)
        self.rig.pairing.pc_cancel()
        self.rig.advance(3)
        sim.reboot()
        self.rig.advance(300)
        self.assertEqual(len(self.requests()), 1)
        self.assertFalse(self.rig.pairing.window_open)
        self.rig.advance(310)                                                                 # ten minutes are over
        sim.reboot()
        self.rig.run_until(lambda: len(self.requests()) == 2, timeout=30)

    def test_a_cancel_on_the_display_counts_too(self):
        sim = self.rig.add_display("COM9")
        self.rig.run_until(lambda: len(self.requests()) == 1)
        with self.assertRaises(PairingFailed):
            sim.pair(on_code=lambda code: False)
        self.rig.advance(3)
        sim.reboot()
        self.rig.advance(120)
        self.assertEqual(len(self.requests()), 1)

    def test_an_existing_pairing_is_never_replaced_automatically(self):
        a = self.rig.add_display("COM9")
        self.rig.pair_instantly("COM9")
        self.rig.run_until(lambda: self.rig.manager.status()["detail"] == "starting_session")
        a.open_session()
        self.rig.run_until(lambda: self.rig.state() == "connected")
        self.rig.unplug("COM9")
        self.rig.run_until(lambda: self.rig.state() == "searching")
        self.rig.add_display("COM10")                                                          # a different display, not paired with anybody
        self.rig.run_until(lambda: self.rig.state() == "unpaired", timeout=30)
        self.rig.advance(30)
        self.assertEqual(self.requests(), [])
        self.assertFalse(self.rig.pairing.window_open)
        self.assertEqual(self.rig.manager.status()["detail"], "not_paired")
        self.assertTrue(self.rig.pairing.paired_with(a.dev))                                    # still the first one

    def test_forgetting_the_display_does_not_pop_the_window_up_again_at_once(self):
        sim = self.rig.add_display("COM9")
        self.rig.pair_instantly("COM9")
        self.rig.run_until(lambda: self.rig.manager.status()["detail"] == "starting_session")
        sim.open_session()
        self.rig.run_until(lambda: self.rig.state() == "connected")
        self.rig.pairing.forget()
        self.rig.advance(30)
        self.assertEqual(self.rig.state(), "unpaired")
        self.assertEqual(self.requests(), [])                                                   # the user's decision stands ...
        self.assertFalse(self.rig.pairing.window_open)
        self.rig.advance(620)
        sim.reboot()
        self.rig.run_until(lambda: len(self.requests()) == 1, timeout=30)                      # ... for ten minutes

    def test_auto_pair_can_be_switched_off_and_the_manual_window_still_works(self):
        rig = ManagerRig(self.tmp.name, auto_pair=False)
        sim = rig.add_display("COM9")
        rig.advance(10)
        self.assertEqual([e for e in rig.events if e["type"] == "pair-request"], [])
        self.assertEqual(rig.state(), "unpaired")
        rig.pairing.open_window()                                                               # the tray's "Pair a display..."
        self.assertEqual(rig.state(), "pairing")
        sim.pair(after_confirm=rig.pairing.pc_confirm)
        self.assertTrue(rig.pairing.paired)

    def test_a_display_that_lost_its_key_is_reported_and_nothing_is_replaced(self):
        sim = self.rig.add_display("COM9")
        self.rig.pair_instantly("COM9")
        sim.forget_pairing()                                                                    # the display's NVS was erased
        self.rig.advance(5)
        st = self.rig.manager.status()
        self.assertEqual((st["state"], st["detail"], st["paired_with_you"], st["display_paired"]), ("unpaired", "display_forgot", True, False))
        self.assertEqual(self.requests(), [])
        self.assertTrue(self.rig.pairing.paired)

    def test_a_display_with_another_key_is_reported_as_a_key_mismatch(self):
        sim = self.rig.add_display("COM9")
        self.rig.pair_instantly("COM9")
        self.rig.run_until(lambda: self.rig.manager.status()["detail"] == "starting_session")
        # a display that skips the companion's proof check and sends its own (wrong) answer, as a defective one might
        sim.post("/link/open", {"dev": sim.dev.hex(), "nd": "ab" * 16})
        sim.post("/link/auth", {"dev": sim.dev.hex(), "proof": "00" * 32})
        self.rig.run_until(lambda: self.rig.manager.status()["detail"] == "key_mismatch")
        self.assertEqual(self.rig.state(), "unpaired")


# ---- real threads ------------------------------------------------------------------------------------------------------------------------------

class Threads(unittest.TestCase):
    """The same Link with its own reader and worker thread, in real time."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pipe = DuplexPipe(3)
        self.store = PairingStore(self.tmp.name, cid=bytes.fromhex("c1c2c3c4c5c6c7c8"))
        self.pairing = Pairing(self.store, name="TEST-PC")
        self.monitor = FakeMonitor()
        self.api = Api(self.monitor, None, self.pairing)
        self.sim = DisplaySim(self.pipe.display, driver=RealTimeDriver(5.0))
        key = bytes(range(32))
        self.store.set_device(self.sim.dev, key)
        self.sim.key, self.sim.cid = key, self.pairing.cid

    def start_link(self, **kw) -> Link:
        link = Link(self.pipe.attach(), self.api, name="COM9", threaded=True, read_timeout=0.02, **kw)
        link.start()
        self.addCleanup(lambda: (link.close("test over"), link.join(2.0)))
        return link

    def connect(self, link: Link) -> None:
        end = time.monotonic() + 5
        while not self.sim.companion_present and time.monotonic() < end:
            self.sim.pump()
            time.sleep(0.002)
        self.assertTrue(self.sim.companion_present)
        self.assertEqual(self.sim.open_session().status, 200)

    def test_a_slow_route_does_not_stop_the_beacon_and_ping_answers(self):
        link = self.start_link()
        self.connect(link)
        self.monitor.artwork_gate = threading.Event()
        p = self.sim.request_async("GET", "/artwork?size=312")
        self.assertTrue(self.monitor.artwork_entered.wait(3))                                # the worker is now stuck inside the "monitor"
        self.sim.ping(b"are you there", msg_id=9)
        time.sleep(0.3)
        acks_before = link.counters()["acks"]
        self.sim.last_beacon = -1e9                                                          # a BEACON is due right now
        end = time.monotonic() + 3
        while time.monotonic() < end and (self.sim.pongs != [b"are you there"] or link.counters()["acks"] == acks_before):
            self.sim.pump()
            time.sleep(0.002)
        self.assertEqual(self.sim.pongs, [b"are you there"])                                 # the reader answered while the worker waited
        self.assertGreater(link.counters()["acks"], acks_before)
        self.assertFalse(p.done)
        self.monitor.artwork_gate.set()
        r = self.sim.wait(p)
        self.assertEqual((r.status, r.body), (200, self.monitor.cover))

    def test_a_retry_of_a_request_that_is_still_running_is_ignored_and_a_late_one_is_replayed(self):
        link = self.start_link()
        self.connect(link)
        self.monitor.artwork_gate = threading.Event()
        p = self.sim.request_async("GET", "/artwork?size=312")
        self.assertTrue(self.monitor.artwork_entered.wait(3))
        self.sim.send_frame(p.frame)
        self.sim.send_frame(p.frame)                                                          # the display's impatient retries
        time.sleep(0.3)
        self.monitor.artwork_gate.set()
        self.sim.wait(p)
        self.assertEqual(self.monitor.art_calls, 1)
        self.assertGreaterEqual(link.counters()["duplicates_ignored"], 1)
        self.sim.send_frame(p.frame)                                                          # after completion: answered from memory
        time.sleep(0.5)
        self.sim.pump()
        self.assertEqual(self.monitor.art_calls, 1)
        self.assertEqual(link.counters()["duplicates_replayed"], 1)

    def test_a_request_that_waited_too_long_is_dropped_unexecuted(self):
        clock = FakeClock()
        sim = DisplaySim(self.pipe.display, clock=clock, driver=RealTimeDriver(5.0), dev=self.sim.dev, key=self.sim.key, cid=self.sim.cid)
        link = self.start_link(clock=clock)
        end = time.monotonic() + 5
        while not sim.companion_present and time.monotonic() < end:
            sim.pump()
            time.sleep(0.002)
        self.assertEqual(sim.open_session().status, 200)
        self.monitor.artwork_gate = threading.Event()
        sim.request_async("GET", "/artwork?size=312")
        self.assertTrue(self.monitor.artwork_entered.wait(3))
        sim.request_async("POST", "/control/next")                                            # queued behind the stuck one
        time.sleep(0.2)
        clock.advance(11)                                                                     # the display gave up on it long ago
        self.monitor.artwork_gate.set()
        time.sleep(0.5)
        self.assertEqual(self.monitor.control_count, 0)
        self.assertEqual(link.counters()["stale_requests"], 1)

    def test_a_flood_of_requests_is_bounded(self):
        link = self.start_link()
        self.connect(link)
        self.monitor.artwork_gate = threading.Event()
        self.sim.request_async("GET", "/artwork?size=312")
        self.assertTrue(self.monitor.artwork_entered.wait(3))
        for i in range(20):
            self.sim.request_async("GET", f"/health?{i}")
        time.sleep(0.3)
        self.assertGreaterEqual(link.counters()["queue_full"], 10)
        self.monitor.artwork_gate.set()

    def test_a_crashed_reader_closes_the_link_so_the_manager_can_reconnect(self):
        link = self.start_link()
        self.connect(link)
        with mock.patch.object(Link, "pump", side_effect=RuntimeError("bug")), self.assertLogs("usb", "WARNING"):
            end = time.monotonic() + 3
            while not link.closed and time.monotonic() < end:
                time.sleep(0.01)
        self.assertTrue(link.closed)
        self.assertEqual(link.close_reason, "reader failed (RuntimeError)")

    def test_close_stops_both_threads_promptly(self):
        link = self.start_link()
        self.connect(link)
        started = time.monotonic()
        link.close("done")
        link.join(2.0)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertTrue(all(not t.is_alive() for t in link._threads))

    def test_the_threaded_manager_finds_a_paired_display_and_stops_cleanly(self):
        listed = [PortInfo("COM9", usblink.USB_VID, usblink.USB_PID, "synthetic")]
        manager = LinkManager(self.api, lister=lambda: list(listed), opener=lambda name: self.pipe.attach(), threaded=True,
                              scan_period=0.05, read_timeout=0.02)
        self.addCleanup(manager.stop)
        events = []
        manager.add_listener(events.append)
        manager.start()
        end = time.monotonic() + 8
        while manager.status()["state"] != "probing" or not self.sim.companion_present:
            self.sim.pump()
            time.sleep(0.002)
            self.assertLess(time.monotonic(), end, "the manager did not find the display")
        self.assertEqual(self.sim.open_session().status, 200)
        end = time.monotonic() + 5
        while manager.status()["state"] != "connected" and time.monotonic() < end:
            time.sleep(0.01)
        self.assertEqual(manager.status()["state"], "connected")
        end = time.monotonic() + 3
        while time.monotonic() < end and not any(e["type"] == "status" and e["status"]["state"] == "connected" for e in events):
            time.sleep(0.01)                                                                  # the scanner thread reports it a moment later
        self.assertTrue(any(e["type"] == "status" and e["status"]["state"] == "connected" for e in events))
        manager.stop()
        self.assertFalse(self.pipe.pc.is_open)


# ---- the pyserial adapter and the pure helpers (no hardware, no real serial module state) ---------------------------------------------------------

class FakeSerialModule(types.ModuleType):
    """A stand-in for pyserial that records, in order, what the adapter does to the port object."""

    EIGHTBITS, PARITY_NONE, STOPBITS_ONE = 8, "N", 1

    class SerialException(OSError):
        pass

    class SerialTimeoutException(SerialException):
        pass

    def __init__(self) -> None:
        super().__init__("serial")
        outer = self
        self.events: list[tuple] = []
        self.waiting = 0
        self.fail_write = False

        class Serial:
            def __init__(self) -> None:
                object.__setattr__(self, "is_open", False)
                object.__setattr__(self, "_ready", True)

            def __setattr__(self, name, value):
                outer.events.append(("set", name, value))
                object.__setattr__(self, name, value)

            def open(self):
                outer.events.append(("open",))
                object.__setattr__(self, "is_open", True)

            @property
            def in_waiting(self):
                return outer.waiting

            def read(self, n):
                outer.events.append(("read", n))
                return b"x" * min(n, max(outer.waiting, 1))

            def write(self, data):
                outer.events.append(("write", len(data)))
                if outer.fail_write:
                    raise outer.SerialTimeoutException("Write timeout")
                return len(data)

            def close(self):
                outer.events.append(("close",))
                object.__setattr__(self, "is_open", False)

        self.Serial = Serial


class PySerialAdapter(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSerialModule()
        patcher = mock.patch.dict(sys.modules, {"serial": self.fake})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_dtr_and_rts_are_set_low_before_the_port_is_opened_and_never_touched_again(self):
        port = usblink.PySerialPort("COM9")
        ev = self.fake.events
        open_at = ev.index(("open",))
        before = ev[:open_at]
        self.assertIn(("set", "dtr", False), before)                                         # low BEFORE open(): opening cannot assert them
        self.assertIn(("set", "rts", False), before)
        for expected in (("set", "port", "COM9"), ("set", "baudrate", 115200), ("set", "bytesize", 8), ("set", "parity", "N"),
                         ("set", "stopbits", 1), ("set", "xonxoff", False), ("set", "rtscts", False), ("set", "dsrdtr", False)):
            self.assertIn(expected, before)
        self.assertTrue(any(e[:2] == ("set", "timeout") for e in before) and any(e[:2] == ("set", "write_timeout") for e in before))
        port.read(100, 0.1)
        port.write(b"abc")
        port.close()
        self.assertEqual([e for e in ev[open_at + 1:] if e[0] == "set"], [])                  # no setting is ever changed after open
        self.assertFalse(any(e[1] in ("dtr", "rts", "break_condition") for e in ev[open_at:] if e[0] == "set"))

    def test_reads_ask_for_what_is_buffered_or_one_byte_and_a_write_timeout_is_a_timeouterror(self):
        port = usblink.PySerialPort("COM9")
        self.fake.waiting = 0
        port.read(4096, 0.1)
        self.fake.waiting = 300
        port.read(4096, 0.1)
        self.fake.waiting = 9000
        port.read(4096, 0.1)
        self.assertEqual([e[1] for e in self.fake.events if e[0] == "read"], [1, 300, 4096])
        self.fake.fail_write = True
        with self.assertRaises(TimeoutError):
            port.write(b"frame")
        self.assertTrue(port.is_open)
        port.close()
        self.assertFalse(port.is_open)
        port.close()                                                                          # closing twice is fine

    def test_a_failed_open_propagates_for_the_manager_to_classify(self):
        def boom(self):
            raise FakeSerialModule.SerialException("could not open port 'COM9': PermissionError(13, 'Access is denied.', None, 5)")
        with mock.patch.object(self.fake.Serial, "open", boom):
            with self.assertRaises(OSError) as ctx:
                usblink.PySerialPort("COM9")
        self.assertEqual(usblink.classify_open_error(ctx.exception), "busy")

    def test_the_port_listing_reads_the_system_list_without_opening_anything(self):
        class Info:
            def __init__(self, device, vid, pid, description):
                self.device, self.vid, self.pid, self.description = device, vid, pid, description
        list_ports = types.ModuleType("serial.tools.list_ports")
        list_ports.comports = lambda: [Info("COM5", 0x303A, 0x1001, "USB Serial Device"), Info("COM1", None, None, None)]
        tools = types.ModuleType("serial.tools")
        tools.list_ports = list_ports
        with mock.patch.dict(sys.modules, {"serial.tools": tools, "serial.tools.list_ports": list_ports}):
            ports = usblink.list_serial_ports()
        self.assertEqual(ports, [PortInfo("COM5", 0x303A, 0x1001, "USB Serial Device"), PortInfo("COM1", None, None, "")])
        self.assertEqual([e for e in self.fake.events if e[0] == "open"], [])
        self.assertEqual(usblink.select_ports(ports), ["COM5"])


class PureHelpers(unittest.TestCase):
    def test_select_ports(self):
        ports = [PortInfo("COM3", 0x0403, 0x6001), PortInfo("COM5", 0x303A, 0x1001), PortInfo("COM7", 0x303A, 0x4001), PortInfo("COM8", 0x303A, 0x1001)]
        self.assertEqual(usblink.select_ports(ports), ["COM5", "COM8"])
        self.assertEqual(usblink.select_ports(ports, "com3"), ["COM3"])
        self.assertEqual(usblink.select_ports(ports, "COM99"), [])

    def test_parse_request(self):
        self.assertEqual(usblink.parse_request(b"GET /now-playing\n"), ("GET", "/now-playing", b""))
        self.assertEqual(usblink.parse_request(b"POST /pair/offer\n{\"a\":1}\nmore"), ("POST", "/pair/offer", b'{"a":1}\nmore'))
        for bad in (b"", b"GET /x", b"HEAD /x\n", b"GET /x y\n", b"GET\t/x\n", b"GET /x\r\n", b"GET /\x7f\n", b"GET /\x80\n", b"GET /" + b"a" * 512 + b"\n"):
            with self.assertRaises(ValueError, msg=bad[:20]):
                usblink.parse_request(bad)

    def test_text_lines_are_bounded_and_clean(self):
        t = usblink.TextLines(limit=50)
        self.assertEqual(t.feed(b"abc\r\ndef"), ["abc"])
        self.assertEqual(t.feed(b"ghi\n"), ["defghi"])
        self.assertEqual(t.feed(b"x" * 120), ["x" * 50, "x" * 50])                            # a flood without line breaks is emitted in pieces
        self.assertEqual(t.feed(b"y\n"), ["x" * 20 + "y"])
        self.assertEqual(usblink.TextLines().feed(b"\xff\xfe\xfd\xfc\xfb\n"), [])               # undecodable junk is not a log line
        self.assertEqual(usblink.clean_text("a\x00b\x1bc d\te"), "abcd e")


if __name__ == "__main__":
    unittest.main()

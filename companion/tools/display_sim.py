"""A SIMULATED DISPLAY for the USB link (docs/usb-protocol.md), the display role of the protocol over an in-memory duplex pipe, so the
companion's whole USB side can be tested - and developed against - without any hardware and without ever opening a real serial port.

    DuplexPipe     two `SerialLike` ends (display side, host side) with a FaultInjector on each direction: drop / corrupt / truncate /
                   duplicate / split frames, garbage and floods, a randomly splitting pipe, an unplug, a write that dies half way
    DisplaySim     the display: BEACONs on the 1 s / 2 s schedule, REQUEST client with the 3 s timeout and one retry with the same ID,
                   RESPONSE reassembly with the display's own checks (SEQ, total, per-route size limits, 8 s), the pairing handshake of
                   docs/protocol.md section 3 ported from display_client.py to frames, the session handshake of section 4, reboot (new
                   `boot`), LOG frames, raw text between frames
    FakeMonitor    a stand-in for the media monitor with side-effect counters (so "not executed twice" can be asserted)
    SimRig         pipe + simulated display + `Link` + fake clock, stepped deterministically (`settle`, `advance`, `run_until`)
    ManagerRig     the same around a `LinkManager`: several ports, several displays, ports that are busy or vanish

Two ways to drive it. LOCKSTEP (what the tests use): everything runs on one thread on a fake clock; `rig.advance(8)` is eight simulated
seconds that take a few milliseconds. REAL TIME: give the sim the default `RealTimeDriver`, start `Link(threaded=True)` and let threads
run, for the tests that are about threads (a slow route must not stop the PING answers).

The crypto of the session handshake is written out here again from the text of the specification (not imported from api.py), so the two
implementations check each other.
"""
from __future__ import annotations

import collections
import hashlib
import hmac
import json
import os
import random
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from desk_media_companion import framing  # noqa: E402
from desk_media_companion.api import Api  # noqa: E402
from desk_media_companion.framing import (TYPE_ACK, TYPE_BEACON, TYPE_LOG, TYPE_PING, TYPE_PONG, TYPE_RESPONSE, Frame,  # noqa: E402
                                          FrameParser)
from desk_media_companion.pairing import Pairing, PairingStore  # noqa: E402
from desk_media_companion.usblink import Link, LinkManager, PortInfo, USB_PID, USB_VID  # noqa: E402
from display_client import DisplayAttempt, PairingFailed, ProofError  # noqa: E402


class FakeClock:
    """A clock tests move by hand (also usable as the `time.monotonic` replacement of a Pairing, a Link or a LinkManager)."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


# ---- the pipe ----------------------------------------------------------------------------------------------------------------------------

class FaultInjector:
    """Damages what one end writes. Per write (a Link writes one frame per call): either the next action of a script ("drop", "corrupt",
    "truncate", "duplicate", "split", "garbage", "flood", "pass") or, with the configured probabilities, a random one. `max_chunk` > 0
    additionally cuts everything delivered into random pieces of 1..max_chunk bytes (a pipe that splits reads at random places)."""

    ACTIONS = ("drop", "corrupt", "truncate", "duplicate", "split", "garbage", "flood", "pass")

    def __init__(self, seed: int = 0) -> None:
        self.rng = random.Random(seed)
        self.p = {a: 0.0 for a in self.ACTIONS if a != "pass"}
        self.script: collections.deque[str] = collections.deque()
        self.max_chunk = 0
        self.flood_bytes = 20_000
        self.counts: collections.Counter = collections.Counter()

    def queue(self, *actions: str) -> None:
        for a in actions:
            if a not in self.ACTIONS:
                raise ValueError(a)
        self.script.extend(actions)

    def set(self, **probabilities: float) -> None:
        for name, value in probabilities.items():
            if name not in self.p:
                raise ValueError(name)
            self.p[name] = value

    def _action(self) -> str:
        if self.script:
            return self.script.popleft()
        r = self.rng.random()
        acc = 0.0
        for name, prob in self.p.items():
            acc += prob
            if r < acc:
                return name
        return "pass"

    def _garbage(self, n: int) -> bytes:
        return bytes(self.rng.choice((0xFA, 0xDE, 0x01, 0x00, 0xFF, self.rng.randrange(256))) for _ in range(n))

    def process(self, data: bytes) -> list[bytes]:
        action = self._action()
        self.counts[action] += 1
        rng = self.rng
        if action == "drop":
            out = []
        elif action == "corrupt":
            b = bytearray(data)
            b[rng.randrange(len(b))] ^= 1 << rng.randrange(8)
            out = [bytes(b)]
        elif action == "truncate":
            out = [data[:rng.randrange(1, max(2, len(data)))]]
        elif action == "duplicate":
            out = [data, data]
        elif action == "split":
            cut = rng.randrange(1, max(2, len(data)))
            out = [data[:cut], data[cut:]]
        elif action == "garbage":
            out = [self._garbage(rng.randrange(1, 40)), data]
        elif action == "flood":
            out = [self._garbage(self.flood_bytes), data]
        else:
            out = [data]
        if self.max_chunk > 0:
            chopped = []
            for chunk in out:
                i = 0
                while i < len(chunk):
                    step = rng.randrange(1, self.max_chunk + 1)
                    chopped.append(chunk[i:i + step])
                    i += step
            out = chopped
        return [c for c in out if c]


class PipeEnd:
    """One end of a `DuplexPipe`; satisfies `usblink.SerialLike`. `read` returns at most one delivered chunk per call (so the boundaries
    of writes, splits and chops are the boundaries the reader sees)."""

    def __init__(self, pipe: "DuplexPipe", name: str, faults: FaultInjector) -> None:
        self._pipe = pipe
        self.name = name
        self.faults = faults
        self._rx: collections.deque[bytes] = collections.deque()
        self._cond = threading.Condition()
        self._open = True
        self.written: list[bytes] = []                 # the last writes, for assertions
        self.bytes_written = 0
        self.fail_after: Optional[tuple[int, BaseException]] = None      # (bytes still allowed, error): a write that dies half way

    @property
    def is_open(self) -> bool:
        return self._open and not self._pipe.unplugged

    def pending(self) -> int:
        with self._cond:
            return sum(len(c) for c in self._rx)

    def _check(self) -> None:
        if self._pipe.unplugged:
            raise OSError("the device does not recognize the command (unplugged)")
        if not self._open:
            raise OSError("port closed")

    def read(self, n: int, timeout: float = 0.0) -> bytes:
        with self._cond:
            self._check()
            if not self._rx and timeout > 0:
                self._cond.wait(timeout)
                self._check()
            if not self._rx:
                return b""
            chunk = self._rx.popleft()
            if len(chunk) > n:
                self._rx.appendleft(chunk[n:])
                chunk = chunk[:n]
            return chunk

    def write(self, data: bytes) -> None:
        data = bytes(data)
        with self._cond:
            self._check()
        if self.fail_after is not None:
            allowed, exc = self.fail_after
            if len(data) > allowed:                      # the cable went away in the middle of this frame
                if allowed > 0:
                    self._send(data[:allowed])
                self.fail_after = None
                raise exc
            self.fail_after = (allowed - len(data), exc)
        self._send(data)

    def _send(self, data: bytes) -> None:
        self.bytes_written += len(data)
        self.written.append(data)
        del self.written[:-4000]
        peer = self._pipe.peer_of(self)
        for chunk in self.faults.process(data):
            if peer is not None:
                peer.inject(chunk)

    def inject(self, data: bytes) -> None:
        """Bytes arriving at this end (what the other end wrote, or whatever a test wants to put on the wire). Dropped when closed."""
        if not data:
            return
        with self._cond:
            if self._open and not self._pipe.unplugged:
                self._rx.append(bytes(data))
                self._cond.notify_all()

    def close(self) -> None:
        with self._cond:
            self._open = False
            self._cond.notify_all()

    def wake(self) -> None:
        with self._cond:
            self._cond.notify_all()


class DuplexPipe:
    """The cable. `display` is the display's end, `attach()` gives the host's end (each call is "the companion opens the port": the previous
    host end is closed, bytes the display wrote while nobody had the port open are gone, as with a real CDC-ACM port)."""

    def __init__(self, seed: int = 0) -> None:
        self.unplugged = False
        self.to_pc = FaultInjector(seed)               # applied to what the display writes
        self.to_display = FaultInjector(seed + 1)      # applied to what the companion writes
        self.display = PipeEnd(self, "display", self.to_pc)
        self.pc: Optional[PipeEnd] = None

    def attach(self) -> PipeEnd:
        if self.pc is not None:
            self.pc.close()
        self.pc = PipeEnd(self, "pc", self.to_display)
        return self.pc

    def peer_of(self, end: PipeEnd) -> Optional[PipeEnd]:
        return self.pc if end is self.display else self.display

    def unplug(self) -> None:
        """The cable is pulled: every read and write fails from now on, and the host's handle is dead for good (a new `attach()` is a new
        open after the device is back)."""
        self.unplugged = True
        self.display.wake()
        if self.pc is not None:
            self.pc.close()

    def plug(self) -> None:
        """The cable is back: the display works again, the host has to open the port anew (a fresh `attach()`)."""
        self.unplugged = False
        self.display._rx.clear()


# ---- the display ------------------------------------------------------------------------------------------------------------------------

@dataclass
class Response:
    status: int
    body: bytes

    def json(self) -> dict:
        return json.loads(self.body.decode("utf-8"))


@dataclass
class Pending:
    id: int
    method: str
    target: str
    frame: bytes
    max_body: int
    sent_at: float
    attempts: int = 1
    done: bool = False
    response: Optional[Response] = None
    error: str = ""


@dataclass
class _Assembly:
    status: int
    total: int
    data: bytearray = field(default_factory=bytearray)
    next_seq: int = 1
    last_chunk_at: float = 0.0


class LinkError(Exception):
    """A request did not get an answer (timeout after the retry) or the link is down."""


class RealTimeDriver:
    """Waits in real time (for tests with real threads): pumps the simulated display until the answer arrives or the deadline passes."""

    def __init__(self, timeout_s: float = 10.0) -> None:
        self.timeout_s = timeout_s

    def wait(self, sim: "DisplaySim", pending: Pending) -> None:
        end = time.monotonic() + self.timeout_s
        while not pending.done and time.monotonic() < end:
            sim.pump()
            time.sleep(0.001)

    def sleep(self, sim: "DisplaySim", seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            sim.pump()
            time.sleep(0.001)


class DisplaySim:
    """The display's side of docs/usb-protocol.md. All methods run on the caller's thread; nothing here starts a thread.

    `driver` decides how waiting works: `RealTimeDriver` (default) or a `SimRig` (fake clock, lockstep with the companion's Link).
    """

    BEACON_NO_COMPANION_S = 1.0
    BEACON_WITH_COMPANION_S = 2.0
    COMPANION_PRESENT_S = 6.0
    REQUEST_TIMEOUT_S = 3.0
    RESPONSE_GAP_S = 8.0
    JSON_LIMIT, COVER_LIMIT, IDLE_LIMIT = 16 * 1024, 160 * 1024, 300 * 1024

    def __init__(self, port, *, clock: Callable[[], float] = time.monotonic, rand: Callable[[int], bytes] = secrets.token_bytes,
                 dev: Optional[bytes] = None, boot: Optional[bytes] = None, fw: str = "1.0.0", api: int = 2, min_api: int = 2,
                 key: Optional[bytes] = None, cid: Optional[bytes] = None, driver=None, retries: int = 1) -> None:
        self.port = port
        self.clock = clock
        self._rand = rand
        self.dev = dev if dev is not None else rand(8)
        self.boot = boot if boot is not None else rand(4)
        self.fw, self.api, self.min_api = fw, api, min_api
        self.key, self.cid = key, cid                  # the pairing record in the display's "NVS"
        self.driver = driver or RealTimeDriver()
        self.retries = retries
        self.parser = FrameParser(on_noise=lambda _b: None, clock=clock)
        self.beacons_enabled = True
        self.beacon_override: Optional[bytes] = None   # raw BEACON payload instead of the real one (invalid-beacon tests)
        self.last_beacon = -1e9
        self.last_companion_rx = -1e9
        self.ack: Optional[dict] = None
        self.authenticated = False
        self.link_down = False
        self.next_id = 1
        self.pending: dict[int, Pending] = {}
        self._assembly: dict[int, _Assembly] = {}
        self.pongs: list[bytes] = []
        self.debug_actions: list[str] = []
        self.frames_rx: list[Frame] = []               # everything valid the companion sent (bounded)
        self.counts: collections.Counter = collections.Counter()

    # -- identity and state ------------------------------------------------------------------------------------------------------------
    @property
    def paired(self) -> bool:
        return self.key is not None

    @property
    def companion_present(self) -> bool:
        return self.clock() - self.last_companion_rx <= self.COMPANION_PRESENT_S

    def beacon_payload(self) -> bytes:
        if self.beacon_override is not None:
            return self.beacon_override
        return json.dumps({"v": 1, "api": self.api, "min_api": self.min_api, "dev": self.dev.hex(), "boot": self.boot.hex(),
                           "fw": self.fw, "paired": self.paired}, separators=(",", ":")).encode("utf-8")

    def reboot(self) -> None:
        """A software reset: a new `boot`, the request counter starts again, the session is gone; the pairing (NVS) and the port stay."""
        self.boot = self._rand(4)
        self.next_id = 1
        self.authenticated = False
        self.pending.clear()
        self._assembly.clear()
        self.parser.reset()
        self.ack = None
        self.last_companion_rx = -1e9
        self.last_beacon = -1e9

    def forget_pairing(self) -> None:
        self.key = self.cid = None
        self.authenticated = False

    # -- the wire --------------------------------------------------------------------------------------------------------------------------
    def send_frame(self, frame: bytes) -> bool:
        try:
            self.port.write(frame)
        except OSError:
            self.link_down = True
            return False
        return True

    def send_raw(self, data: bytes) -> bool:
        return self.send_frame(data)

    def log(self, text: str, msg_id: int = 0) -> None:
        self.send_frame(framing.encode_frame(TYPE_LOG, text.encode("utf-8")[:framing.MAX_PAYLOAD], msg_id=msg_id))

    def noise(self, text: str) -> None:
        self.send_frame(text.encode("utf-8"))

    def ping(self, payload: bytes = b"hello", msg_id: int = 7) -> None:
        self.send_frame(framing.encode_frame(TYPE_PING, payload, msg_id=msg_id))

    def pump(self) -> bool:
        """One step: send a BEACON when due, read and handle what arrived, retry or fail the requests that timed out. True if anything moved."""
        now = self.clock()
        did = False
        period = self.BEACON_WITH_COMPANION_S if self.companion_present else self.BEACON_NO_COMPANION_S
        if self.beacons_enabled and now - self.last_beacon >= period:
            self.last_beacon = now
            self.send_frame(framing.encode_frame(TYPE_BEACON, self.beacon_payload()))
            did = True
        try:
            data = self.port.read(4096, 0.0)
            self.link_down = False
        except OSError:
            self.link_down = True
            data = b""
        for f in self.parser.feed(data):
            did = True
            self._on_frame(f, now)
        for p in list(self.pending.values()):
            if not p.done and now - p.sent_at >= self.REQUEST_TIMEOUT_S:
                if p.attempts <= self.retries:                 # one retry, with the same ID and the same bytes
                    p.attempts += 1
                    p.sent_at = now
                    self._assembly.pop(p.id, None)
                    self.send_frame(p.frame)
                else:
                    p.done, p.error = True, "timeout"
                did = True
        for mid, asm in list(self._assembly.items()):
            if now - asm.last_chunk_at >= self.RESPONSE_GAP_S:
                del self._assembly[mid]                        # no chunk for 8 s: the response is abandoned
                self.counts["response_gap"] += 1
        return did

    def _on_frame(self, f: Frame, now: float) -> None:
        self.last_companion_rx = now
        self.frames_rx.append(f)
        del self.frames_rx[:-500]
        if f.type == TYPE_ACK:
            try:
                self.ack = json.loads(f.payload.decode("utf-8"))
            except ValueError:
                self.counts["bad_ack"] += 1
        elif f.type == TYPE_RESPONSE:
            self._on_response(f, now)
        elif f.type == TYPE_PONG:
            self.pongs.append(f.payload)
        elif f.type == framing.TYPE_DEBUG:                       # development only: a scripted-input action for a dev firmware
            self.debug_actions.append(f.payload.decode("ascii", "replace"))
        else:
            self.counts["unexpected_type"] += 1

    def _on_response(self, f: Frame, now: float) -> None:
        """The display's checks of section 5: SEQ continuity, the running length against `total` and the per-route size limit."""
        p = self.pending.get(f.msg_id)
        if p is None or p.done:
            self.counts["response_for_nobody"] += 1
            return
        if f.first:
            head = framing.split_response_head(f.payload)
            if head is None or f.seq != 0:
                self._assembly.pop(f.msg_id, None)
                self.counts["response_abandoned"] += 1
                return
            status, total, data = head
            if total > p.max_body or len(data) > total:        # does not fit: a failure, never truncated
                self._assembly.pop(f.msg_id, None)
                self.counts["response_too_big"] += 1
                return
            asm = self._assembly[f.msg_id] = _Assembly(status, total, bytearray(data), 1, now)
        else:
            asm = self._assembly.get(f.msg_id)
            if asm is None or f.seq != (asm.next_seq & 0xFF) or len(asm.data) + len(f.payload) > asm.total:
                self._assembly.pop(f.msg_id, None)             # a gap, a repeat or too much: abandon (the retry is answered from memory)
                self.counts["response_abandoned"] += 1
                return
            asm.data += f.payload
            asm.next_seq += 1
            asm.last_chunk_at = now
        if f.last:
            del self._assembly[f.msg_id]
            if len(asm.data) != asm.total:
                self.counts["response_abandoned"] += 1
                return
            p.done, p.response = True, Response(asm.status, bytes(asm.data))

    # -- requests ----------------------------------------------------------------------------------------------------------------------------
    def _next_id(self) -> int:
        mid = self.next_id
        self.next_id = 1 if self.next_id >= 65535 else self.next_id + 1
        return mid

    def limit_for(self, target: str) -> int:
        path = target.split("?", 1)[0]
        return self.COVER_LIMIT if path == "/artwork" else self.IDLE_LIMIT if path == "/idle/image" else self.JSON_LIMIT

    def request_async(self, method: str, target: str, body: bytes = b"", *, msg_id: Optional[int] = None) -> Pending:
        mid = msg_id if msg_id is not None else self._next_id()
        frame = framing.encode_request(mid, method, target, body)
        p = Pending(mid, method, target, frame, self.limit_for(target), self.clock())
        self.pending[mid] = p
        self.send_frame(frame)
        return p

    def wait(self, pending: Pending) -> Response:
        self.driver.wait(self, pending)
        if not pending.done:
            raise LinkError("no answer")
        del self.pending[pending.id]
        if pending.error:
            raise LinkError(pending.error)
        assert pending.response is not None
        return pending.response

    def request(self, method: str, target: str, body: bytes = b"") -> Response:
        return self.wait(self.request_async(method, target, body))

    def get(self, target: str) -> Response:
        return self.request("GET", target)

    def post(self, target: str, obj=None) -> Response:
        return self.request("POST", target, b"" if obj is None else json.dumps(obj).encode("utf-8"))

    def sleep(self, seconds: float) -> None:
        self.driver.sleep(self, seconds)

    # -- pairing (docs/protocol.md section 3, on frames) -------------------------------------------------------------------------------------
    def _pair_post(self, route: str, body: dict) -> dict:
        r = self.post(route, body)
        data = r.json()
        if r.status != 200:
            raise PairingFailed(r.status, str(data.get("error", "")))
        return data

    def pair(self, on_code: Callable[[str], bool] = lambda code: True, after_confirm: Optional[Callable[[], None]] = None,
             timeout_s: float = 10.0, poll_s: float = 0.05) -> DisplayAttempt:
        """The display's side of the pairing: offer, reveal, show the code (`on_code(code)` returns True when the user taps Pair), confirm,
        poll the status, check the companion's proof. On success `key` and `cid` hold the pairing. The pairing uses the display's
        persistent `dev`, the same one its BEACON carries."""
        a = DisplayAttempt(dev=self.dev, rand=self._rand)
        offer = self._pair_post("/pair/offer", a.offer_body())
        a.complete(bytes.fromhex(offer["id"]), bytes.fromhex(offer["pub"]), bytes.fromhex(offer["nc"]))
        self._pair_post("/pair/reveal", a.reveal_body())
        if not on_code(a.code):
            self._pair_post("/pair/cancel", {"dev": self.dev.hex()})
            raise PairingFailed(0, "cancelled_by_user")
        self._pair_post("/pair/confirm", a.confirm_body())
        if after_confirm is not None:
            after_confirm()
        waited = 0.0
        while True:
            r = self.get(f"/pair/status?dev={self.dev.hex()}")
            data = r.json()
            if r.status != 200:
                raise PairingFailed(r.status, str(data.get("error", "")))
            if data["state"] == "paired":
                if not a.check_pc_proof(data.get("proof", "")):
                    raise ProofError("the companion's pairing proof is wrong")
                assert a.derived is not None
                self.key, self.cid = a.derived.key, a.cid
                return a
            if data["state"] in ("cancelled", "expired"):
                raise PairingFailed(0, data["state"])
            if waited > timeout_s:
                raise PairingFailed(0, "timeout")
            self.sleep(poll_s)
            waited += poll_s

    # -- the session (section 4) ---------------------------------------------------------------------------------------------------------------
    def open_session(self) -> Response:
        """/link/open, verify the companion's proof in constant time, /link/auth. Returns the last response; `authenticated` is set on
        success. Raises ProofError when the companion's proof is wrong (the display would then refuse to talk to it)."""
        if self.key is None or self.cid is None:
            raise LinkError("not paired")
        nd = self._rand(16)
        r = self.post("/link/open", {"dev": self.dev.hex(), "nd": nd.hex()})
        if r.status != 200:
            return r
        body = r.json()
        nc = bytes.fromhex(body["nc"])
        expect = hmac.new(self.key, b"DMLINK-PC\n" + self.cid + self.dev + nd + nc, hashlib.sha256).digest()
        if not hmac.compare_digest(expect, bytes.fromhex(body["proof"])):
            raise ProofError("the companion's session proof is wrong")
        proof = hmac.new(self.key, b"DMLINK-DEV\n" + self.cid + self.dev + nd + nc, hashlib.sha256).digest()
        r = self.post("/link/auth", {"dev": self.dev.hex(), "proof": proof.hex()})
        self.authenticated = r.status == 200
        return r


# ---- a stand-in for the media monitor -------------------------------------------------------------------------------------------------------

class FakeMonitor:
    """Satisfies `api.MediaBackend` with synthetic data and counts what was executed (the side-effect counter of the duplicate tests)."""

    def __init__(self, cover_bytes: int = 160_000) -> None:
        self.calls: list[tuple] = []
        self.control_count = 0
        self.art_calls = 0
        self.cover = b"\xff\xd8\xff\xe0" + bytes((i * 131 + 7) & 0xFF for i in range(cover_bytes - 4))
        self.artwork_gate: Optional[threading.Event] = None      # real-thread tests: /artwork blocks until the test sets it
        self.artwork_entered = threading.Event()

    def now_playing(self) -> dict:
        return {"ok": True, "has_session": True, "playing": True, "app": "Synthetic.exe", "title": "Synthetic Song",
                "artist": "Synthetic Artist", "position_ms": 12000, "duration_ms": 180000, "art_id": "a1"}

    def control(self, action, position_ms=None, app=""):
        self.control_count += 1
        self.calls.append(("control", action, position_ms, app))
        return {"ok": True, "action": action, "app": app, "n": self.control_count}

    def volume(self, action, step=5, level=None, app=""):
        self.calls.append(("volume", action, step, level, app))
        return {"ok": True, "action": action, "volume": 50}

    def switch_source(self, direction, shown_app=""):
        self.calls.append(("source", direction, shown_app))
        return {"ok": True, "changed": False}

    def auto_source(self):
        self.calls.append(("auto",))
        return {"ok": True}

    def list_sources(self):
        return {"ok": True, "sources": []}

    def artwork(self, size=300, max_w=None):
        self.art_calls += 1
        self.artwork_entered.set()
        if self.artwork_gate is not None:
            self.artwork_gate.wait(10.0)
        return "a1", self.cover

    def take_debug(self):
        return ""

    def request_debug(self, action):
        self.calls.append(("debug", action))


# ---- rigs ---------------------------------------------------------------------------------------------------------------------------------------

class SimRig:
    """One display, one cable, one Link, one Api, one fake clock, all in lockstep on the calling thread.

        rig = SimRig(tmpdir)                  # an unpaired display on a new cable; the companion's Link is open on it
        rig.pair_display()                    # the whole first-time pairing (the PC's click included)
        rig.connect()                         # beacons, session handshake
        rig.sim.get("/now-playing")           # a request from the display, answered by the Link through the Api
        rig.advance(10)                       # ten simulated seconds
    """

    def __init__(self, directory: str, *, paired: bool = False, seed: int = 1, monitor: Optional[FakeMonitor] = None,
                 dev_tools: bool = False, sim_kwargs: Optional[dict] = None, link_kwargs: Optional[dict] = None,
                 name: str = "COM9") -> None:
        self.clock = FakeClock()
        self.dir = directory
        self.rng = random.Random(seed)
        self.monitor = monitor or FakeMonitor()
        self.store = PairingStore(directory, cid=bytes.fromhex("a1a2a3a4a5a6a7a8"))
        self.pairing = Pairing(self.store, name="TEST-PC", clock=self.clock, rand=self._rand)
        self.api = Api(self.monitor, None, self.pairing, dev_tools)
        self.pipe = DuplexPipe(seed)
        self.name = name
        self.sim = DisplaySim(self.pipe.display, clock=self.clock, rand=self._rand, driver=_RigDriver(self), **(sim_kwargs or {}))
        self.link_kwargs = link_kwargs or {}
        self.link = self.new_link()
        if paired:
            self.pair_instantly()

    def _rand(self, n: int) -> bytes:
        return bytes(self.rng.getrandbits(8) for _ in range(n))

    def new_link(self) -> Link:
        """The companion opens the port (again): a fresh Link on a fresh host end of the same cable (a restart of the companion)."""
        return Link(self.pipe.attach(), self.api, name=self.name, clock=self.clock, rand=self._rand, threaded=False, **self.link_kwargs)

    # -- stepping ----------------------------------------------------------------------------------------------------------------------------
    def settle(self, limit: int = 20000) -> None:
        """Run both sides until no bytes are left in flight."""
        for _ in range(limit):
            moved = False
            if self.pipe.pc is not None and self.pipe.pc.pending():
                self.link.pump(0.0)
                moved = True
            if self.pipe.display.pending():
                self.sim.pump()
                moved = True
            if not moved:
                return
        raise AssertionError("the conversation did not settle")

    def step(self) -> None:
        """One tick of both sides (timers included), then settle."""
        self.link.pump(0.0)
        self.sim.pump()
        self.settle()

    def advance(self, seconds: float, step: float = 0.05) -> None:
        end = self.clock.t + seconds
        while self.clock.t < end:
            self.clock.advance(min(step, end - self.clock.t))
            self.step()

    def run_until(self, predicate: Callable[[], bool], timeout: float = 30.0, step: float = 0.01) -> None:
        end = self.clock.t + timeout
        self.step()
        while not predicate():
            if self.clock.t >= end:
                raise AssertionError("condition not reached in the simulated time")
            self.clock.advance(step)
            self.step()

    # -- shortcuts -----------------------------------------------------------------------------------------------------------------------------
    def pair_instantly(self) -> None:
        """Both sides hold the same pairing without going through the handshake (the persistent state after an earlier pairing)."""
        key, cid = bytes(range(32)), self.pairing.cid
        self.store.set_device(self.sim.dev, key)
        self.sim.key, self.sim.cid = key, cid

    def pair_display(self, confirm_on_pc: bool = True) -> DisplayAttempt:
        """The full first-time pairing: the PC's pairing window, the display's handshake, the user's tap and click."""
        self.pairing.open_window()
        return self.sim.pair(on_code=lambda code: code == self.pairing.snapshot()["code"],
                             after_confirm=(lambda: self.pairing.pc_confirm()) if confirm_on_pc else None)

    def connect(self, timeout: float = 20.0) -> None:
        """Beacons until the companion answers, then the session handshake; returns when the link is AUTHENTICATED."""
        self.run_until(lambda: self.sim.companion_present, timeout)
        self.sim.open_session()
        assert self.link.authenticated and self.sim.authenticated


class _RigDriver:
    """Waiting in lockstep: simulated time advances while the display waits."""

    def __init__(self, rig: "SimRig") -> None:
        self.rig = rig

    def wait(self, sim: DisplaySim, pending: Pending) -> None:
        try:
            self.rig.run_until(lambda: pending.done, timeout=30.0)
        except AssertionError:
            pass

    def sleep(self, sim: DisplaySim, seconds: float) -> None:
        self.rig.advance(seconds)


class ManagerRig:
    """A `LinkManager` (unthreaded) over several simulated cables; ports can be added, removed, made busy. `step()` pumps every link and
    simulated display once, `advance()` moves the clock and runs a scan every `scan_period`."""

    def __init__(self, directory: str, *, seed: int = 2, auto_pair: bool = True, only_port: str = "") -> None:
        self.clock = FakeClock()
        self.dir = directory
        self.rng = random.Random(seed)
        self.monitor = FakeMonitor(cover_bytes=2000)
        self.store = PairingStore(directory, cid=bytes.fromhex("b1b2b3b4b5b6b7b8"))
        self.pairing = Pairing(self.store, name="TEST-PC", clock=self.clock, rand=self._rand)
        self.api = Api(self.monitor, None, self.pairing)
        self.pipes: dict[str, DuplexPipe] = {}
        self.sims: dict[str, DisplaySim] = {}
        self.vid_pid: dict[str, tuple[int, int]] = {}
        self.busy: dict[str, BaseException] = {}
        self.opens: list[str] = []
        self.open_times: list[tuple[str, float]] = []
        self.events: list[dict] = []
        self._auto_pair, self._only_port = auto_pair, only_port
        self.manager = self._make_manager()
        self._next_scan = self.clock.t

    def _make_manager(self) -> LinkManager:
        manager = LinkManager(self.api, lister=self._list, opener=self._open, clock=self.clock, only_port=self._only_port, threaded=False,
                              auto_pair=self._auto_pair, rand=self._rand)
        manager.add_listener(self.events.append)
        return manager

    def restart_companion(self) -> None:
        """The companion process is restarted: every link is closed, the pairing is read back from its file, a new manager starts scanning.
        The displays and their cables are untouched (they do not notice anything but the missing ACKs)."""
        self.manager.stop()
        self.pairing = Pairing(PairingStore(self.dir), name="TEST-PC", clock=self.clock, rand=self._rand)
        self.api = Api(self.monitor, None, self.pairing)
        self.manager = self._make_manager()
        self._next_scan = self.clock.t

    def _rand(self, n: int) -> bytes:
        return bytes(self.rng.getrandbits(8) for _ in range(n))

    def _list(self) -> list:
        return [PortInfo(n, *self.vid_pid.get(n, (USB_VID, USB_PID)), "synthetic") for n, p in self.pipes.items() if not p.unplugged]

    def _open(self, name: str):
        self.opens.append(name)
        self.open_times.append((name, self.clock.t))
        if name in self.busy:
            raise self.busy[name]
        return self.pipes[name].attach()

    def add_display(self, name: str = "COM9", *, vid_pid: Optional[tuple[int, int]] = None, **sim_kwargs) -> DisplaySim:
        pipe = DuplexPipe(self.rng.randrange(1 << 30))
        self.pipes[name] = pipe
        if vid_pid is not None:
            self.vid_pid[name] = vid_pid
        sim = DisplaySim(pipe.display, clock=self.clock, rand=self._rand, driver=_ManagerDriver(self), **sim_kwargs)
        self.sims[name] = sim
        return sim

    def pair_instantly(self, name: str) -> None:
        sim = self.sims[name]
        key = bytes(range(32))
        self.store.set_device(sim.dev, key)
        sim.key, sim.cid = key, self.pairing.cid

    def step(self) -> None:
        for _ in range(200):
            self.manager.pump()
            moved = False
            for sim in self.sims.values():
                waiting = sim.port.pending() > 0
                moved = sim.pump() or waiting or moved
            if not any(p.pc is not None and p.pc.pending() for p in self.pipes.values()) and not moved:
                break

    def unplug(self, name: str) -> None:
        self.pipes[name].unplug()

    def replug(self, name: str) -> None:
        self.pipes[name].plug()

    def _scan_if_due(self) -> None:
        """What the scanner thread does: a scan when the next time-based step is due or a link reported a change."""
        if self.clock.t >= self._next_scan or self.manager.consume_wake():
            self.manager.tick()
            self._next_scan = self.manager.next_due()

    def advance(self, seconds: float, step: float = 0.1) -> None:
        end = self.clock.t + seconds
        while self.clock.t < end:
            self._scan_if_due()
            self.clock.advance(min(step, end - self.clock.t))
            self.step()

    def run_until(self, predicate: Callable[[], bool], timeout: float = 60.0, step: float = 0.1) -> None:
        end = self.clock.t + timeout
        while not predicate():
            if self.clock.t >= end:
                raise AssertionError("condition not reached in the simulated time: " + json.dumps(self.manager.status()["state"]))
            self._scan_if_due()
            self.clock.advance(step)
            self.step()

    def state(self) -> str:
        return self.manager.status()["state"]


class _ManagerDriver:
    def __init__(self, rig: ManagerRig) -> None:
        self.rig = rig

    def wait(self, sim: DisplaySim, pending: Pending) -> None:
        try:
            self.rig.run_until(lambda: pending.done, timeout=30.0)
        except AssertionError:
            pass

    def sleep(self, sim: DisplaySim, seconds: float) -> None:
        self.rig.advance(seconds)

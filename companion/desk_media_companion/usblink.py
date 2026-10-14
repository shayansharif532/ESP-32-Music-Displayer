"""The USB link (docs/usb-protocol.md): finds the display on the cable, speaks DMLINK v1 frames over its serial port and serves the same API
routes as the LAN server.

    LinkManager   background scanner: lists the serial ports (VID 303A / PID 1001), opens a candidate, keeps at most one display active,
                  reconnects after unplug / replug / reboot / restart, applies the pairing policy and reports `status()` + events to the UI
    Link          one connection to one port: frame parser, BEACON detection, requests -> Api -> chunked responses, duplicate memory,
                  the session handshake, keep-alive, LOG frames, PING / PONG
    SerialLike    the five-line port interface everything is written against; `PySerialPort` is the only implementation that touches
                  hardware, tests inject an in-memory one (companion/tools/display_sim.py)

Threads (the design that keeps this simple):
  * Each Link has a READER thread (reads, parses, answers BEACON / PING itself, validates and queues REQUESTs) and a WORKER thread
    (runs the Api for one request at a time and writes the chunked response). A slow route such as /artwork therefore never stops the
    BEACON / PING answers; one request at a time is the protocol anyway. ACK / PONG are single frames, written by the reader between the
    chunks of a response: the write lock is per frame, and a receiver accepts frames of different types in any order.
  * The manager has one SCANNER thread. Link threads never call listeners; they set a flag and wake the scanner, which compares states
    and notifies the listeners from ITS thread (the UI marshals to its own).
  * Lock order: manager lock -> link lock -> session lock; a Link never calls the manager (the arbiter) or a callback while holding its
    own lock.
  * For tests a Link runs without threads: `Link(threaded=False)` and `pump()` does one read / parse / dispatch / job step on the
    caller's thread, so a whole conversation with a simulated display runs deterministically on a fake clock.

Hardware safety, in one place: the port is opened with DTR and RTS low and they are NEVER touched again (see `PySerialPort`). An RTS / DTR
edge is how esptool resets the chip, so a companion that toggled them would reboot the display whenever it opened or closed the port.
"""
from __future__ import annotations

import collections
import json
import logging
import math
import os
import re
import secrets
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable, NamedTuple, Optional, Protocol

from . import __version__
from .api import (API_VERSION, MIN_API_VERSION, PAIR_BODY_MAX, Api, ClientCtx, LinkSession, Reply, error_reply, split_target,
                  takes_body)
from .framing import (TYPE_ACK, TYPE_BEACON, TYPE_DEBUG, TYPE_LOG, TYPE_PING, TYPE_PONG, TYPE_REQUEST, Frame, FrameParser, crc32,
                      encode_frame, encode_response)
from .pairing import IN_PROGRESS

log = logging.getLogger("usb")

USB_VID = 0x303A                 # Espressif's USB-Serial/JTAG controller: shared by every ESP32-S3 board, so never trusted alone
USB_PID = 0x1001

LINK_VERSION = 1
READ_CHUNK = 4096
READ_TIMEOUT_S = 0.1             # the reader wakes at least this often (parser timer, keep-alive, shutdown)
WRITE_TIMEOUT_S = 2.0            # one frame; a longer write leaves a truncated frame on the wire, which the receiver handles
ACK_MIN_INTERVAL_S = 0.2         # at most one ACK per 200 ms
KEEPALIVE_S = 8.0                # no valid frame for this long: the display is gone, close the port
PROBE_TIMEOUT_S = 3.0            # a port that shows no valid BEACON within this long is not (yet) a display
SCAN_PERIOD_S = 2.0
PROBE_BACKOFF = (3.0, 4.8, 7.7, 10.0)   # re-probe a silent port after this long (the display may be rebooting), growing up to ~10 s
BUSY_BACKOFF = (2.0, 3.2, 5.1, 8.2, 10.0)
REOPEN_DELAY_S = 0.5             # after a link that had a display ended: try again quickly
DUP_MEMORY = 8                   # the last 8 exchanges are remembered ...
DUP_TTL_S = 10.0                 # ... for 10 s
QUEUE_LIMIT = 4                  # requests waiting for the worker (a conforming display has one in flight)
STALE_REQUEST_S = 10.0           # a queued request older than this is dropped unexecuted: the display gave up on it long ago
MAX_TARGET = 512
MAX_PING = 64
MAX_DEBUG = 400                  # a DEBUG action name is shorter than this
PAIR_WINDOW_S = 120.0            # the pairing window the companion opens by itself
PAIR_SUPPRESS_S = 600.0          # a cancelled attempt is not offered again for 10 minutes
PAIR_EARLY_SLACK_S = 5.0         # a window that ends more than this before its deadline was closed by somebody (the user's Cancel)
LOG_LINE_MAX = 400               # characters per delivered log line
LOG_LINES_PER_FRAME = 64
LOG_RATE_PER_S = 100.0
LOG_BURST = 200.0


# ---- the port ----------------------------------------------------------------------------------------------------------------------------

class SerialLike(Protocol):
    """What the link needs from a serial port. Any failure of a dead port raises OSError (pyserial's SerialException is one); a write
    that times out raises TimeoutError (the frame is then partly on the wire, which is fine: the receiver resynchronises)."""

    @property
    def is_open(self) -> bool: ...
    def read(self, n: int, timeout: float) -> bytes: ...      # up to n bytes; b"" when nothing arrived within `timeout`
    def write(self, data: bytes) -> None: ...
    def close(self) -> None: ...


class PortInfo(NamedTuple):
    name: str                    # "COM5"
    vid: Optional[int]
    pid: Optional[int]
    description: str = ""


class PySerialPort:
    """`SerialLike` on top of pyserial. The only code in the product that opens a serial port.

    DTR and RTS: the ESP32-S3's USB-Serial/JTAG controller treats particular DTR / RTS sequences as "reset" and "enter the bootloader"
    (that is how esptool restarts the chip). pyserial's defaults would ASSERT both lines when the port is opened, so they are set low on the
    (still closed) object BEFORE `open()`, and nothing in this module ever writes them again; in particular the timeout is not changed
    after opening either, because pyserial re-applies the whole communication state when it is (`_reconfigure_port`).
    Baud rate and framing are ignored by the hardware (it is not a UART); 115200 8N1 without flow control is just a valid setting.
    """

    def __init__(self, name: str, *, read_timeout: float = READ_TIMEOUT_S, write_timeout: float = WRITE_TIMEOUT_S) -> None:
        import serial
        ser = serial.Serial()
        ser.port = name
        ser.baudrate = 115200
        ser.bytesize = serial.EIGHTBITS
        ser.parity = serial.PARITY_NONE
        ser.stopbits = serial.STOPBITS_ONE
        ser.xonxoff = False
        ser.rtscts = False
        ser.dsrdtr = False
        ser.dtr = False                     # before open(): see the class docstring
        ser.rts = False
        ser.timeout = read_timeout
        ser.write_timeout = write_timeout
        ser.open()
        self._ser = ser
        self._serial = serial

    @property
    def is_open(self) -> bool:
        return bool(self._ser.is_open)

    def read(self, n: int, timeout: float) -> bytes:
        """Up to n bytes. The wait is the read timeout fixed at open (`timeout` is accepted for the interface: changing it later would
        re-apply the port state); asking for what is already buffered, or one byte, keeps the call from waiting for a full buffer."""
        waiting = self._ser.in_waiting
        return self._ser.read(min(max(waiting, 1), n))

    def write(self, data: bytes) -> None:
        try:
            self._ser.write(data)
        except self._serial.SerialTimeoutException as exc:
            raise TimeoutError("serial write timeout") from exc

    def close(self) -> None:
        try:
            self._ser.close()
        except Exception:
            pass


def open_serial_port(name: str) -> SerialLike:
    return PySerialPort(name)


def list_serial_ports() -> list[PortInfo]:
    """Every serial port Windows knows (name, VID, PID, description). Enumeration only: it opens no port."""
    if os.name == "nt":
        try:
            import serial.tools.list_ports_windows  # noqa: F401  (list_ports imports it conditionally: name it so a frozen exe bundles it)
        except ImportError:
            pass
    from serial.tools import list_ports
    return [PortInfo(str(p.device), p.vid, p.pid, p.description or "") for p in list_ports.comports()]


def select_ports(ports: list[PortInfo], only_port: str = "") -> list[str]:
    """The ports worth probing: VID 303A / PID 1001, or - for development - exactly the one the user named (any VID / PID, but only if
    the system lists it). Either way a port is a display only after a valid BEACON."""
    if only_port:
        return [p.name for p in ports if p.name.upper() == only_port.upper()]
    return [p.name for p in ports if p.vid == USB_VID and p.pid == USB_PID]


def classify_open_error(exc: BaseException) -> str:
    """'busy' (another program holds the port: "Access is denied"), 'gone' (the port vanished meanwhile) or 'error'."""
    if isinstance(exc, PermissionError) or getattr(exc, "winerror", None) == 5:
        return "busy"
    if isinstance(exc, FileNotFoundError):
        return "gone"
    text = str(exc).lower()
    if "permissionerror" in text or "access is denied" in text or "(13," in text:
        return "busy"
    if "filenotfounderror" in text or "cannot find the file" in text or "(2," in text:
        return "gone"
    return "error"


# ---- BEACON, requests, text ----------------------------------------------------------------------------------------------------------

_HEX16 = re.compile(r"[0-9a-fA-F]{16}")
_HEX8 = re.compile(r"[0-9a-fA-F]{8}")
_TARGET = re.compile(rb"/[\x21-\x7e]*")


@dataclass(frozen=True)
class DisplayInfo:
    """What a valid BEACON says about the display."""
    dev: bytes                   # persistent identity, 8 bytes
    boot: bytes                  # fresh per boot, 4 bytes
    fw: str
    api: int
    min_api: int
    paired: bool                 # the display holds a key

    @property
    def dev_hex(self) -> str:
        return self.dev.hex()


def _is_int(v: object, lo: int, hi: int) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def clean_text(text: str, limit: int = LOG_LINE_MAX) -> str:
    """Printable characters only (control characters, escapes and separators removed, tabs become spaces), at most `limit` long."""
    out = "".join(" " if c == "\t" else c for c in text if c == "\t" or c.isprintable())
    return out if len(out) <= limit else out[:limit - 3] + "..."


def parse_beacon(payload: bytes) -> Optional[DisplayInfo]:
    """A BEACON's JSON as `DisplayInfo`, or None when it is not a valid one (wrong type, field missing, out of range): the sender is then
    not a display. `fw` is display text and is cleaned and capped."""
    if len(payload) > 512:
        return None
    try:
        obj = json.loads(payload.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    if not isinstance(obj, dict):
        return None
    api, min_api, dev, boot = obj.get("api"), obj.get("min_api"), obj.get("dev"), obj.get("boot")
    if obj.get("v") != LINK_VERSION or not _is_int(obj.get("v"), 1, 1):
        return None
    if not (_is_int(api, 1, 255) and _is_int(min_api, 1, 255) and min_api <= api):
        return None
    if not (isinstance(dev, str) and _HEX16.fullmatch(dev) and isinstance(boot, str) and _HEX8.fullmatch(boot)):
        return None
    fw = obj.get("fw", "")
    paired = obj.get("paired", False)
    if not isinstance(fw, str) or not isinstance(paired, bool):
        return None
    return DisplayInfo(bytes.fromhex(dev), bytes.fromhex(boot), clean_text(fw, 32), api, min_api, paired)


def is_compatible(display_api: int, display_min_api: int) -> bool:
    """docs section 4: compatible when display.api >= companion.min_api and companion.api >= display.min_api."""
    return display_api >= MIN_API_VERSION and API_VERSION >= display_min_api


def parse_request(payload: bytes) -> tuple[str, str, bytes]:
    """`METHOD SP target LF [body]` -> (method, target, body). ValueError for anything else: a request line without LF, a METHOD other than
    GET / POST, more or less than one space, a target that is empty, longer than 512 bytes, does not start with '/' or contains bytes
    outside 0x21..0x7E. Nothing of the request is put into the exception."""
    nl = payload.find(b"\n")
    if nl < 0:
        raise ValueError("malformed request")
    method, sep, target = payload[:nl].partition(b" ")
    if sep != b" " or method not in (b"GET", b"POST") or len(target) > MAX_TARGET or _TARGET.fullmatch(target) is None:
        raise ValueError("malformed request")
    return method.decode("ascii"), target.decode("ascii"), payload[nl + 1:]


class TextLines:
    """Turns raw text (the boot banner and other bytes between frames) into clean lines. Bounded: a partial line never grows past the cap."""

    def __init__(self, limit: int = LOG_LINE_MAX) -> None:
        self._limit = limit
        self._partial = ""

    def feed(self, data: bytes) -> list[str]:
        parts = re.split(r"\r\n|\n|\r", self._partial + data.decode("utf-8", "replace"))
        self._partial = parts.pop()
        lines = parts
        while len(self._partial) > self._limit:          # a flood without line breaks: emit it in pieces instead of growing
            lines.append(self._partial[:self._limit])
            self._partial = self._partial[self._limit:]
        out = []
        for line in lines:
            line = clean_text(line, self._limit).strip()
            if line and line.count("�") * 2 <= len(line):         # mostly undecodable bytes = a damaged frame, not text
                out.append(line)
        return out


LogSink = Callable[[str, str], None]       # (kind "log" | "raw", one clean line)


def default_log_sink(kind: str, line: str) -> None:
    """Display text goes to the log as DATA (`%s`), never as a format string and never interpreted."""
    logging.getLogger("usb.display").log(logging.INFO if kind == "log" else logging.DEBUG, "%s", line)


# ---- one connection --------------------------------------------------------------------------------------------------------------------------

@dataclass
class _Job:
    kind: str                    # "exec" (run the Api), "reply" (an answer that is already decided: 400 ...), "replay" (duplicate)
    msg_id: int
    crc: int
    queued_at: float
    epoch: int
    method: str = ""
    target: str = ""
    body: bytes = b""
    reply: Optional[Reply] = None
    frames: Optional[list] = None


@dataclass
class _Exchange:
    msg_id: int
    crc: int
    frames: list
    at: float


class Link:
    """One connection to one serial port (see the module docstring for the threading design).

    A port is just a CANDIDATE until a valid BEACON arrives; until then the link sends nothing at all. After the first BEACON the display
    is known (`display`); it is SERVED when the arbiter allows it (at most one display is active, a second one is only reported).
    """

    def __init__(self, port: SerialLike, api: Api, *, name: str = "", clock: Callable[[], float] = time.monotonic,
                 rand: Callable[[int], bytes] = secrets.token_bytes, arbiter: Optional[Callable[["Link"], bool]] = None,
                 log_sink: Optional[LogSink] = None, on_change: Optional[Callable[[], None]] = None, threaded: bool = True,
                 read_timeout: float = READ_TIMEOUT_S) -> None:
        self.name = name
        self._port = port
        self._api = api
        self._clock = clock
        self._arbiter = arbiter
        self._sink = log_sink or default_log_sink
        self._on_change = on_change
        self._threaded = threaded
        self._read_timeout = read_timeout
        self._session = LinkSession(api.pairing, clock, rand)
        self._text = TextLines()
        self._parser = FrameParser(on_noise=self._on_noise, clock=clock)

        self._lock = threading.Lock()                      # state below; never held while doing I/O, calling the Api or a callback
        self._cv = threading.Condition(self._lock)         # wakes the worker
        self._wlock = threading.Lock()                     # one frame on the wire at a time
        self._closed = False
        self._close_reason = ""
        self._display: Optional[DisplayInfo] = None
        self._serving = False
        self._opened_at = clock()
        self._display_since: Optional[float] = None
        self._last_rx = self._opened_at
        self._last_ack: Optional[float] = None
        self._epoch = 0                                    # bumped when the display restarts: queued work of the old life is dropped
        self._jobs: collections.deque[_Job] = collections.deque()
        self._pending: set[tuple[int, int]] = set()        # (id, request crc) queued or running
        self._memory: collections.OrderedDict[int, _Exchange] = collections.OrderedDict()
        self._counters: collections.Counter = collections.Counter()
        self._log_tokens = LOG_BURST
        self._log_stamp = self._opened_at
        self._signature: tuple = ()
        self._threads: list[threading.Thread] = []

    # -- public state ------------------------------------------------------------------------------------------------------------------
    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def close_reason(self) -> str:
        return self._close_reason

    @property
    def display(self) -> Optional[DisplayInfo]:
        return self._display

    @property
    def serving(self) -> bool:
        return self._serving

    @property
    def opened_at(self) -> float:
        return self._opened_at

    @property
    def session(self) -> LinkSession:
        return self._session

    @property
    def compatible(self) -> bool:
        d = self._display
        return d is None or is_compatible(d.api, d.min_api)

    @property
    def authenticated(self) -> bool:
        return self._session.authenticated

    def counters(self) -> dict:
        """Link counters plus the parser's error classes (bad_crc, bad_hcrc, bad_len, unknown_ver, partial_timeouts, ...)."""
        with self._lock:
            out = dict(self._counters)
        out.update({"parser_" + k: v for k, v in self._parser.stats.as_dict().items()})
        return out

    def info(self) -> dict:
        d = self._display
        authenticated = self._session.authenticated
        paired_with_you = bool(d is not None and self._api.pairing is not None and self._api.pairing.paired_with(d.dev))
        with self._lock:
            since = self._display_since
            serving, closed, reason = self._serving, self._closed, self._close_reason
        return {"port": self.name, "closed": closed, "reason": reason, "display": d is not None, "serving": serving,
                "dev": d.dev_hex if d else "", "dev_short": d.dev_hex[:8] if d else "", "boot": d.boot.hex() if d else "",
                "fw": d.fw if d else "", "api": d.api if d else 0, "min_api": d.min_api if d else 0,
                "display_paired": d.paired if d else False, "paired_with_you": paired_with_you,
                "compatible": self.compatible, "authenticated": authenticated, "auth_failed": self._session.auth_failed,
                "since": since, "counters": self.counters()}

    # -- lifecycle ---------------------------------------------------------------------------------------------------------------------
    def start(self) -> None:
        """Threaded mode: the reader and the worker thread."""
        for target, label in ((self._rx_loop, "rx"), (self._tx_loop, "tx")):
            t = threading.Thread(target=target, name=f"usb-{label}-{self.name}", daemon=True)
            self._threads.append(t)
            t.start()

    def join(self, timeout: float = 2.0) -> None:
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(timeout)

    def close(self, reason: str = "closed") -> None:
        """Idempotent; safe from any thread. Ends the session, closes the port (which also wakes a blocked reader)."""
        with self._cv:
            if self._closed:
                return
            self._closed = True
            self._close_reason = reason
            self._jobs.clear()
            self._pending.clear()
            self._cv.notify_all()
        self._session.drop()
        try:
            self._port.close()
        except Exception:
            pass
        log.info("usb link on %s closed (%s)", self.name, reason)
        self._notify()

    def _notify(self) -> None:
        cb = self._on_change
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    def _count(self, name: str, n: int = 1) -> None:
        with self._lock:
            self._counters[name] += n

    # -- the reader ------------------------------------------------------------------------------------------------------------------------
    def _rx_loop(self) -> None:
        try:
            while self.pump(self._read_timeout):
                pass
        except Exception as exc:                          # a bug must not leave a dead reader behind an "open" link: close it, the manager reconnects
            log.warning("usb reader failed (%s)", type(exc).__name__)
            self.close(f"reader failed ({type(exc).__name__})")

    def pump(self, timeout: float = 0.0) -> bool:
        """One step of the reader: read what is there (waiting up to `timeout`), parse, dispatch the frames, housekeeping. In unthreaded
        mode it also runs the queued requests. False once the link is closed."""
        if self._closed:
            return False
        try:
            data = self._port.read(READ_CHUNK, timeout)
        except Exception as exc:                          # unplugged, closed under us, driver error: all the same to us
            self.close(f"read failed ({type(exc).__name__})")
            return False
        now = self._clock()
        if data:
            self._count("bytes_rx", len(data))
        for frame in self._parser.feed(data):             # an empty read still runs the parser's 1 s limit
            try:
                self._handle_frame(frame, now)
            except Exception as exc:                      # one bad frame must not stop the reader (a handler bug is counted and logged)
                self._count("handler_errors")
                log.warning("usb frame handler failed (%s)", type(exc).__name__)
            if self._closed:
                return False
        self._housekeeping(now)
        if not self._threaded and not self._closed:
            self._drain_jobs()
        return not self._closed

    def _housekeeping(self, now: float) -> None:
        if self._display is not None and now - self._last_rx > KEEPALIVE_S:
            self.close("display silent")
            return
        sig = (self._display is not None, self._serving, self.compatible, self._session.authenticated, self._closed,
               self._display.boot if self._display else b"")
        if sig != self._signature:
            self._signature = sig
            self._notify()

    def _handle_frame(self, f: Frame, now: float) -> None:
        self._last_rx = now
        self._count("frames_rx")
        if f.type == TYPE_BEACON:
            self._on_beacon(f, now)
        elif self._display is None:                       # a candidate: nothing but ACKs for BEACONs
            self._count("ignored_before_beacon")
        elif f.type == TYPE_REQUEST:
            self._on_request(f, now)
        elif f.type == TYPE_PING:
            self._on_ping(f)
        elif f.type == TYPE_LOG:
            self._on_log(f)
        elif f.type == TYPE_PONG:
            self._count("pongs")
        else:
            self._count("unknown_types")                  # ignored and counted: a newer peer may send frames we do not know

    def _on_noise(self, data: bytes) -> None:
        for line in self._text.feed(data):
            self._emit("raw", line)

    def _emit(self, kind: str, line: str) -> None:
        """Hand one clean line to the sink, unless the line budget (100 / s, bursts of 200) is used up: a display that floods the cable with
        text must not flood the log. The sink is the caller's code and may fail; that never touches the link."""
        now = self._clock()
        with self._lock:
            self._log_tokens = min(LOG_BURST, self._log_tokens + (now - self._log_stamp) * LOG_RATE_PER_S)
            self._log_stamp = now
            allowed = self._log_tokens >= 1.0
            if allowed:
                self._log_tokens -= 1.0
                self._counters["log_lines"] += 1
            else:
                self._counters["log_dropped"] += 1
        if not allowed:
            return
        try:
            self._sink(kind, line)
        except Exception:
            self._count("sink_errors")

    # -- BEACON / ACK / PING / LOG ----------------------------------------------------------------------------------------------------
    def _on_beacon(self, f: Frame, now: float) -> None:
        info = parse_beacon(f.payload)
        if info is None or not f.single:
            self._count("bad_beacons")
            return
        reset = ""
        with self._lock:
            old = self._display
            self._display = info
            self._counters["beacons"] += 1
            if old is None:
                self._display_since = now
                log.info("display %s (firmware %s, api %d) answered on %s", info.dev_hex[:8], info.fw or "?", info.api, self.name)
            elif old.dev != info.dev:
                reset = "another display"
            elif old.boot != info.boot:
                reset = "display restarted"
        if reset:
            self._reset_exchanges(reset)
        if not self._serving:
            allowed = True if self._arbiter is None else bool(self._arbiter(self))      # no lock held: the arbiter takes the manager's
            if allowed:
                with self._lock:
                    self._serving = True
            else:
                self._count("beacons_unserved")
        if self._serving:
            self._ack(now)

    def _reset_exchanges(self, why: str) -> None:
        """A new `boot` (or another `dev`): the display's request counter starts again, so nothing remembered about the old life may be
        replayed or executed, and the session is over."""
        with self._cv:
            self._epoch += 1
            self._jobs.clear()
            self._pending.clear()
            self._memory.clear()
            self._counters["display_resets"] += 1
        self._session.drop()
        log.info("usb link on %s: %s, session dropped", self.name, why)

    def _ack(self, now: float) -> None:
        if self._last_ack is not None and now - self._last_ack < ACK_MIN_INTERVAL_S:
            self._count("acks_suppressed")
            return
        self._last_ack = now
        d = self._display
        p = self._api.pairing
        ack = {"v": LINK_VERSION, "api": API_VERSION, "min_api": MIN_API_VERSION, "app": __version__,
               "name": p.name if p is not None else socket.gethostname()[:63], "id": p.cid_hex if p is not None else "0" * 16,
               "compat": bool(d is not None and is_compatible(d.api, d.min_api))}
        if self._write(encode_frame(TYPE_ACK, json.dumps(ack, separators=(",", ":")).encode("utf-8"))):
            self._count("acks")

    def send_debug(self, action: str) -> bool:
        """Development only: a DEBUG frame (type 0x7E, companion -> display) carrying the name of a scripted-input action for a development
        firmware. The product never calls this. Refused (False) unless the display is served and the action is 1..399 printable ASCII
        characters; True when the frame was written."""
        if self._closed or not self._serving or not (1 <= len(action) < MAX_DEBUG) or not all(0x20 <= ord(c) <= 0x7E for c in action):
            return False
        return self._write(encode_frame(TYPE_DEBUG, action.encode("ascii")))

    def _on_ping(self, f: Frame) -> None:
        if not self._serving:
            return
        if len(f.payload) > MAX_PING or not f.single:
            self._count("bad_pings")
            return
        self._write(encode_frame(TYPE_PONG, f.payload, msg_id=f.msg_id))

    def _on_log(self, f: Frame) -> None:
        """Display text: decoded, split into lines, control characters stripped, capped, rate-limited, handed to the sink as data."""
        for line in f.payload.decode("utf-8", "replace").splitlines()[:LOG_LINES_PER_FRAME]:
            line = clean_text(line).strip()
            if line:
                self._emit("log", line)

    # -- requests ----------------------------------------------------------------------------------------------------------------------------
    def _on_request(self, f: Frame, now: float) -> None:
        if not self._serving:
            self._count("ignored_unserved")
            return
        if f.msg_id == 0:                                 # ID 0 means "not part of an exchange": nobody could match an answer
            self._count("bad_requests")
            return
        crc = crc32(f.payload)
        key = (f.msg_id, crc)
        with self._cv:
            if key in self._pending:                      # the display's retry of a request we are still working on
                self._counters["duplicates_ignored"] += 1
                return
            exchange = self._memory.get(f.msg_id)
            if exchange is not None and now - exchange.at > DUP_TTL_S:
                del self._memory[f.msg_id]
                exchange = None
            if exchange is not None and exchange.crc == crc:      # same ID, same bytes: answer again, execute nothing
                self._counters["duplicates_replayed"] += 1
                self._queue(_Job("replay", f.msg_id, crc, now, self._epoch, frames=exchange.frames), key)
                return
        job = self._build_job(f, crc, now)
        with self._cv:
            self._queue(job, key)

    def _build_job(self, f: Frame, crc: int, now: float) -> _Job:
        """A job that runs the Api for a valid request, or one that answers 400 / 413 for a malformed one (fixed texts, nothing echoed)."""
        def refuse(status: int, text: str) -> _Job:
            self._count("bad_requests")
            return _Job("reply", f.msg_id, crc, now, self._epoch, reply=error_reply(status, text))
        if not f.single:                                  # a request is one frame
            return refuse(400, "bad request")
        try:
            method, target, body = parse_request(f.payload)
            route, _query = split_target(target)
        except ValueError:
            return refuse(400, "bad request")
        if body and not takes_body(method, route):
            return refuse(400, "bad request")
        if len(body) > PAIR_BODY_MAX:
            return refuse(413, "body too large")
        self._count("requests")
        return _Job("exec", f.msg_id, crc, now, self._epoch, method=method, target=target, body=body)

    def _queue(self, job: _Job, key: tuple[int, int]) -> None:
        """(lock held)"""
        if len(self._jobs) >= QUEUE_LIMIT:                # a conforming display has one request in flight; a flood is simply not answered
            self._counters["queue_full"] += 1
            return
        self._pending.add(key)
        self._jobs.append(job)
        self._cv.notify()

    # -- the worker ----------------------------------------------------------------------------------------------------------------------------
    def _tx_loop(self) -> None:
        while True:
            with self._cv:
                while not self._jobs and not self._closed:
                    self._cv.wait(0.5)
                if self._closed:
                    return
                job = self._jobs.popleft()
            self._run_job(job)

    def _drain_jobs(self) -> None:
        while True:
            with self._cv:
                if not self._jobs or self._closed:
                    return
                job = self._jobs.popleft()
            self._run_job(job)

    def _run_job(self, job: _Job) -> None:
        key = (job.msg_id, job.crc)
        try:
            now = self._clock()
            if job.epoch != self._epoch or now - job.queued_at > STALE_REQUEST_S or self._closed:
                self._count("stale_requests")
                return
            if job.kind == "replay":
                frames = job.frames or []
            else:
                frames = self._answer(job, now)
            self._send_response(frames)
        except Exception as exc:                          # a bug must not kill the worker: the display retries, the link lives on
            log.warning("usb request failed (%s)", type(exc).__name__)
            self._count("worker_errors")
        finally:
            with self._lock:
                self._pending.discard(key)

    def _answer(self, job: _Job, now: float) -> list:
        """Run the request and turn the reply into frames; remember the exchange so a retry is replayed, not executed again."""
        if job.kind == "exec":
            d = self._display
            ctx = ClientCtx("usb", self._session.authenticated, "usb:" + self.name, d.dev if d else b"",
                            d is None or is_compatible(d.api, d.min_api), self._session)
            try:
                reply = self._api.handle(job.method, job.target, job.body, ctx)
            except Exception as exc:
                log.warning("usb api error (%s)", type(exc).__name__)
                reply = error_reply(500, "internal error")
        else:
            reply = job.reply or error_reply(500, "internal error")
        try:
            frames = encode_response(job.msg_id, reply.status, reply.body)
        except ValueError:                                # a body over 512 KB, or a status that does not fit
            frames = encode_response(job.msg_id, 500, error_reply(500, "response too large").body)
        with self._lock:
            self._memory[job.msg_id] = _Exchange(job.msg_id, job.crc, frames, now)
            self._memory.move_to_end(job.msg_id)
            while len(self._memory) > DUP_MEMORY:
                self._memory.popitem(last=False)
            self._counters["responses"] += 1
        return frames

    def _send_response(self, frames: list) -> None:
        for frame in frames:
            if self._closed or not self._write(frame):
                self._count("responses_abandoned")        # the display times out and retries with the same ID (answered from memory)
                return

    # -- writing ---------------------------------------------------------------------------------------------------------------------------------
    def _write(self, frame: bytes) -> bool:
        """One frame with one `write` call under the writer lock. True when it was written. A timeout leaves a truncated frame on the wire
        (the receiver resynchronises) and is only counted; any other error means the port is dead and closes the link."""
        try:
            with self._wlock:
                self._port.write(frame)
        except TimeoutError:
            self._count("write_timeouts")
            return False
        except Exception as exc:
            self.close(f"write failed ({type(exc).__name__})")
            return False
        with self._lock:
            self._counters["frames_tx"] += 1
            self._counters["bytes_tx"] += len(frame)
        return True


# ---- the manager -----------------------------------------------------------------------------------------------------------------------------

PortLister = Callable[[], list]
PortOpener = Callable[[str], SerialLike]


@dataclass
class _PortRec:
    name: str
    link: Optional[Link] = None
    opened_at: float = 0.0
    next_try: float = 0.0
    silent_probes: int = 0       # consecutive probes without a BEACON (drives the back-off)
    busy_tries: int = 0
    busy: bool = False


class LinkManager:
    """Finds the display, keeps one Link to it alive and reports what is going on. See the module docstring for the threads.

    `status()` is the snapshot for the UI (`state` is one of searching / probing / connected / unpaired / pairing / incompatible / busy);
    `add_listener(cb)` receives events from the scanner thread: {"type": "status", "status": {...}} whenever anything a user could see changed,
    and {"type": "pair-request", "dev": "<8 hex>", "port": "COM5", "seconds": 120} when the pairing window was opened by itself.

    Pairing policy: a display that is not paired with this PC appears while the companion has NO paired display -> the pairing window opens for
    120 s and "pair-request" is emitted. Never when a display is already paired (nothing is replaced behind the user's back), not again for the
    same appearance of the display (same `dev` and `boot`), not for 10 minutes after the user cancelled or forgot the pairing.
    Everything time-based uses the injected clock; ports are listed and opened through the injected `lister` / `opener`.
    """

    def __init__(self, api: Api, *, lister: PortLister = list_serial_ports, opener: PortOpener = open_serial_port,
                 clock: Callable[[], float] = time.monotonic, only_port: str = "", threaded: bool = True, log_sink: Optional[LogSink] = None,
                 auto_pair: bool = True, scan_period: float = SCAN_PERIOD_S, rand: Callable[[int], bytes] = secrets.token_bytes,
                 read_timeout: float = READ_TIMEOUT_S) -> None:
        self._api = api
        self._lister = lister
        self._opener = opener
        self._clock = clock
        self._only_port = only_port
        self._threaded = threaded
        self._log_sink = log_sink
        self.auto_pair = auto_pair
        self._scan_period = scan_period
        self._rand = rand
        self._read_timeout = read_timeout
        self._lock = threading.RLock()
        self._recs: dict[str, _PortRec] = {}
        self._active: Optional[Link] = None
        self._listeners: list[Callable[[dict], None]] = []
        self._events: list[dict] = []
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_key: Optional[tuple] = None
        self._suppress_until = -math.inf
        self._offered: set[tuple[bytes, bytes]] = set()
        self._was_paired: Optional[bool] = None
        self._last_pair_state = "idle"
        self._auto_deadline: Optional[float] = None       # when the window this manager opened is due to end (None: none open)
        self._listing_error = False

    # -- public --------------------------------------------------------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="usb-scan", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(5.0)
        self._thread = None
        with self._lock:
            links = [r.link for r in self._recs.values() if r.link is not None]
            self._recs.clear()
            self._active = None
        for link in links:
            link.close("stopped")
        for link in links:
            link.join(1.0)

    def add_listener(self, cb: Callable[[dict], None]) -> None:
        with self._lock:
            self._listeners.append(cb)

    def remove_listener(self, cb: Callable[[dict], None]) -> None:
        with self._lock:
            if cb in self._listeners:
                self._listeners.remove(cb)

    def status(self) -> dict:
        """The snapshot for the UI (see the class docstring). Cheap and I/O free: callable as often as the UI likes."""
        with self._lock:
            return self._build_status()

    def send_debug(self, action: str) -> bool:
        """Development only (the dev tools; the product never calls it): send a DEBUG frame to the display that is being served. False when
        there is none or the action name is not 1..399 printable ASCII characters."""
        with self._lock:
            active = self._active if (self._active is not None and not self._active.closed) else None
        return active.send_debug(action) if active is not None else False

    def pump(self) -> None:
        """Unthreaded mode (tests): one reader step for every open link."""
        with self._lock:
            links = [r.link for r in self._recs.values() if r.link is not None]
        for link in links:
            link.pump(0.0)

    def tick(self) -> None:
        """One scan: sync the port list, service every port, apply the pairing policy, notify listeners of changes. The scanner thread calls
        it every `scan_period` (or sooner when a link reports a change); tests call it directly."""
        now = self._clock()
        ports: Optional[list] = None
        try:
            ports = select_ports(self._lister(), self._only_port)
            self._listing_error = False
        except Exception as exc:                          # the registry hiccuped: keep what we have, try again next time
            if not self._listing_error:
                log.warning("listing the serial ports failed (%s)", type(exc).__name__)
            self._listing_error = True
        with self._lock:
            if ports is not None:
                self._sync_ports(ports)
            for rec in list(self._recs.values()):
                self._service(rec, now)
            self._pairing_policy(now)
            status = self._build_status()
            events = self._events
            self._events = []
            listeners = list(self._listeners)
            key = self._status_key(status)
            changed = key != self._last_key
            self._last_key = key
        if changed:
            events.insert(0, {"type": "status", "status": status})
        for event in events:
            for cb in listeners:
                try:
                    cb(event)
                except Exception:                         # a broken listener must not stop the scanner
                    log.warning("a usb status listener failed")

    def next_due(self) -> float:
        """The clock time at which the next time-based step is due (a probe timeout, a retry, or the regular scan): the scanner sleeps
        until then, so a 3 s probe is not stretched to the next 2 s scan."""
        now = self._clock()
        due = now + self._scan_period
        with self._lock:
            for rec in self._recs.values():
                if rec.link is None:
                    due = min(due, max(rec.next_try, now))
                elif rec.link.display is None and not rec.link.closed:
                    due = min(due, rec.opened_at + PROBE_TIMEOUT_S + 0.01)
        return due

    def consume_wake(self) -> bool:
        """True (once) when a link reported a change since the last call: the scanner should run `tick` now. (Tests; the thread waits on it.)"""
        woken = self._wake.is_set()
        self._wake.clear()
        return woken

    # -- the scanner thread ---------------------------------------------------------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:                             # the scanner must never die
                log.exception("usb scan failed")
            self._wake.wait(max(0.05, self.next_due() - self._clock()))
            self._wake.clear()
            self._stop.wait(0.05)                         # coalesce bursts of changes

    def _arbitrate(self, link: Link) -> bool:
        """At most one display is served: the first one to ask wins; a second one is only reported until the first is gone."""
        with self._lock:
            active = self._active
            if active is None or active is link or active.closed:
                self._active = link
                return True
            return False

    # -- ports -----------------------------------------------------------------------------------------------------------------------
    def _sync_ports(self, names: list[str]) -> None:
        for name in list(self._recs):
            if name not in names:                         # unplugged
                rec = self._recs.pop(name)
                if rec.link is not None:
                    rec.link.close("port vanished")
                    if rec.link is self._active:
                        self._active = None
        for name in names:
            self._recs.setdefault(name, _PortRec(name))

    def _service(self, rec: _PortRec, now: float) -> None:
        link = rec.link
        if link is not None and not link.closed and link.display is None and now - rec.opened_at > PROBE_TIMEOUT_S:
            link.close("no beacon")                       # not a display (yet): maybe it is rebooting; look again later
        if link is not None and link.closed:
            had_display = link.display is not None
            rec.link = None
            if link is self._active:
                self._active = None
            if had_display:
                rec.silent_probes = 0
                rec.next_try = now + REOPEN_DELAY_S
            else:
                rec.next_try = now + PROBE_BACKOFF[min(rec.silent_probes, len(PROBE_BACKOFF) - 1)]
                rec.silent_probes += 1
        elif link is not None and link.display is not None:
            rec.silent_probes = 0
        if rec.link is None and now >= rec.next_try:
            self._open(rec, now)

    def _open(self, rec: _PortRec, now: float) -> None:
        try:
            port = self._opener(rec.name)
        except Exception as exc:                          # PermissionError, SerialException, OSError: all handled the same way
            kind = classify_open_error(exc)
            if kind == "busy":
                if not rec.busy:
                    log.info("%s is in use by another program: waiting", rec.name)
                rec.busy = True
                rec.next_try = now + BUSY_BACKOFF[min(rec.busy_tries, len(BUSY_BACKOFF) - 1)]
                rec.busy_tries += 1
            else:
                rec.busy = False
                rec.next_try = now + self._scan_period
                log.debug("cannot open %s (%s)", rec.name, kind)
            return
        rec.busy, rec.busy_tries = False, 0
        link = Link(port, self._api, name=rec.name, clock=self._clock, rand=self._rand, arbiter=self._arbitrate, log_sink=self._log_sink,
                    on_change=self._wake.set, threaded=self._threaded, read_timeout=self._read_timeout)
        rec.link, rec.opened_at = link, now
        log.info("probing %s", rec.name)
        if self._threaded:
            link.start()

    # -- pairing policy ----------------------------------------------------------------------------------------------------------------
    def _pairing_policy(self, now: float) -> None:
        p = self._api.pairing
        if p is None:
            return
        paired = p.paired
        active = self._active if (self._active is not None and not self._active.closed) else None
        display = active.display if active is not None else None
        if self._was_paired and not paired and display is not None:      # "Forget paired display" with the display on the cable: the user decided
            self._suppress_until = now + PAIR_SUPPRESS_S
        self._was_paired = paired
        snap = p.snapshot()
        state = snap["state"]
        if state == "cancelled" and self._last_pair_state != "cancelled":
            self._suppress_until = now + PAIR_SUPPRESS_S         # an attempt cancelled by the user or by the display
        self._last_pair_state = state
        if self._auto_deadline is not None:                      # a window this manager opened: did it end before its time?
            if paired:
                self._auto_deadline = None
            elif snap["window_open"]:
                self._auto_deadline = now + snap["seconds_left"]       # follows extensions (an offer extends the window)
            else:
                if now + PAIR_EARLY_SLACK_S < self._auto_deadline:      # closed early without a pairing: the user said no (Cancel / closed dialog)
                    self._suppress_until = now + PAIR_SUPPRESS_S
                self._auto_deadline = None
        if not (self.auto_pair and active is not None and display is not None and active.serving and active.compatible and not paired):
            return
        key = (display.dev, display.boot)
        if key in self._offered or now < self._suppress_until or snap["window_open"] or state in IN_PROGRESS:
            return
        self._offered.add(key)
        p.open_window(PAIR_WINDOW_S)
        self._auto_deadline = now + PAIR_WINDOW_S
        log.info("a display that is not paired appeared: pairing window opened")
        self._events.append({"type": "pair-request", "dev": display.dev_hex[:8], "port": active.name, "seconds": int(PAIR_WINDOW_S)})

    # -- status ------------------------------------------------------------------------------------------------------------------------------
    def _build_status(self) -> dict:
        p = self._api.pairing
        active = self._active if (self._active is not None and not self._active.closed) else None
        infos = {name: r.link.info() for name, r in self._recs.items() if r.link is not None and not r.link.closed}
        others = [{"port": i["port"], "dev": i["dev"], "dev_short": i["dev_short"]} for n, i in infos.items()
                  if i["display"] and (active is None or n != active.name)]
        st = {"state": "searching", "detail": "", "port": "", "dev": "", "dev_short": "", "fw": "", "api": 0, "min_api": 0,
              "display_paired": False, "paired_with_you": False, "compatible": True, "update_needed": "", "authenticated": False,
              "link": {}, "others": others, "ports": sorted(self._recs), "auto_pair": self.auto_pair}
        if active is not None and active.display is not None:
            i = infos.get(active.name) or active.info()
            st.update(port=i["port"], dev=i["dev"], dev_short=i["dev_short"], fw=i["fw"], api=i["api"], min_api=i["min_api"],
                      display_paired=i["display_paired"], paired_with_you=i["paired_with_you"], compatible=i["compatible"],
                      authenticated=i["authenticated"], link=i["counters"])
            if not i["compatible"]:
                st["state"] = "incompatible"
                st["update_needed"] = "display" if i["api"] < MIN_API_VERSION else "app"
                st["detail"] = "update_display" if st["update_needed"] == "display" else "update_app"
            elif i["authenticated"]:
                st["state"] = "connected"
            elif not i["paired_with_you"] or not i["display_paired"]:
                busy = p is not None and (p.window_open or p.snapshot()["state"] in IN_PROGRESS)
                st["state"] = "pairing" if busy else "unpaired"
                st["detail"] = "not_paired" if not i["paired_with_you"] else "display_forgot"
            elif i["auth_failed"]:
                st["state"], st["detail"] = "unpaired", "key_mismatch"
            else:
                st["state"], st["detail"] = "probing", "starting_session"
        elif infos or any(r.link is not None for r in self._recs.values()):
            st["state"], st["detail"], st["port"] = "probing", "no_response", sorted(infos)[0] if infos else ""
        elif any(r.busy for r in self._recs.values()):
            st["state"], st["detail"] = "busy", "port_in_use"
            st["port"] = next(r.name for r in self._recs.values() if r.busy)
        elif self._recs:
            st["state"], st["detail"], st["port"] = "probing", "waiting", sorted(self._recs)[0]
        return st

    @staticmethod
    def _status_key(st: dict) -> tuple:
        """What a user could see: counters are left out so a busy link does not produce an event per frame."""
        return (st["state"], st["detail"], st["port"], st["dev"], st["fw"], st["api"], st["paired_with_you"], st["display_paired"],
                st["compatible"], st["authenticated"], tuple((o["port"], o["dev"]) for o in st["others"]), tuple(st["ports"]))

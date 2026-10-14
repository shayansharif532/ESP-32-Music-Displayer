"""The companion's API, independent of how a request arrives (docs/protocol.md section 4, docs/usb-protocol.md section 6).

`Api.handle(method, target, body, ctx)` answers one request with a `Reply(status, body, content_type, headers)`. Two transports feed it:
the LAN HTTP server (`server.py`: Host / IP guard, X-Desk-Client, X-DM-* signatures, Content-Length, limiter, connection cap) and the USB
link (`usblink.py`: frames, session handshake, duplicate memory). Everything those transports have in common lives here, so a route
behaves the same over both.

    GET  /now-playing              current media session as JSON
    GET  /artwork[?size=312[&maxw=432]]
                                   current artwork, baseline JPEG (ETag = art_id + size). Without maxw: a size x size square.
                                   With maxw: the artwork keeps its aspect ratio inside a maxw x size box (square art stays
                                   square, 16:9 art becomes a landscape card); never stretched. App logos baked into the
                                   image (Spotify) and black letterbox bars are removed first (see artwork.py).
                                   size is clamped to 16..600, maxw to 16..800.
    POST /control/playpause        also: /control/play  /control/pause  /control/next  /control/previous
    POST /control/seek?position_ms=N
                                   every control takes an optional  &app=Spotify.exe  = the app the display shows: the command
                                   then goes to exactly that session and is refused if it is gone (never to another session)
    GET  /volume                   volume of the app that is displayed (its own Windows audio-session volume, NOT the master volume)
    POST /control/volume-up[?step=5][&app=Spotify.exe]   also: /control/volume-down   POST /volume?level=0..100
                                   -> {"ok":true,"app":"Spotify.exe","volume":65,"muted":false,"sessions":1}
    POST /control/source-next[?from=Spotify.exe]   also: /control/source-prev   (vertical swipe: switch the display to the next /
                                   previous usable media session, wrapping around; pins the display to it. `from` = the app on
                                   screen. -> {"ok":true,"changed":true,"app":"chrome.exe","source":"Chrome","index":1,"count":2,
                                   "now":{...the new /now-playing...}}; changed=false when there is only one source)
    POST /control/source-auto      back to automatic (follow the best session; also leaves a manual "Idle Screen" choice)
    GET  /sources                  the swipe list: [{app, name, state, title, artist, displayed}], mode auto|manual. The last row is the
                                   synthetic idle-screen item {"app":"idle","name":"Idle Screen","state":"idle","synthetic":true} (ADR-008):
                                   always offered, never a Windows session. A swipe onto it (`from=` may be "idle" when the display shows it)
                                   is a manual, temporary display choice: /now-playing then has idle_selected:true, media keeps playing,
                                   any control / volume request with app=idle is refused, and it ends when any session starts playing
    POST /control/shuffle[?app=Spotify.exe]   Spotify only: toggle shuffle (also /shuffle-on /shuffle-off)
    POST /control/repeat[?app=...]            Spotify only: cycle repeat off -> list -> track -> off (also /repeat-off /repeat-list /repeat-track)
                                   -> {"ok":true,"app":"Spotify.exe","spotify":true,"shuffle":true,"repeat":1,"can_shuffle":true,"can_repeat":true,"now":{...}}
                                   refused (ok:false) for every other app. /now-playing carries spotify, shuffle, repeat (0 off, 1 list, 2 track), can_shuffle, can_repeat.
    GET  /idle                     the idle-screen settings (theme, accent, clock, custom line, delay, picture info) + their version
    GET  /idle/image               the background picture for the Photo theme: a ready-made 480x480 baseline JPEG (X-Idle-Crc = CRC-32);
                                   404 when no picture is set. /now-playing carries `idle_v` (the settings version: the display
                                   re-fetches /idle when it differs from the one it stored), `any_playing` (false = nothing plays
                                   anywhere -> idle screen) and `clock_ms` (the PC's local time as ms since the epoch, for the clock).
    GET  /health                   {"ok":true,"service":"desk-media-companion","version":"..."}   (open)
    GET  /hello                    {"ok":true,"v":2,"id":"<companion id>","name":"<PC name>","paired":bool,"pairing":bool}   (open)
                                   over USB also "transport":"usb" and "paired_with_you":bool (the paired display sent the BEACON)
    POST /pair/offer | /pair/reveal | /pair/confirm | /pair/cancel     GET /pair/status?dev=<16 hex>
                                   the pairing handshake (pairing.py; JSON bodies of at most 1 KB; open, they are how a key comes to exist)
    POST /link/open | /link/auth   USB only: the session handshake (LinkSession below; open, they are how a session comes to exist)
    (--dev-tools only)  POST /debug/<action>   scripted input for the development firmware; same authentication as everything else

Authentication is the transport's business, the decision whether a route needs it is shared: every route except GET /hello, GET /health,
/pair/* and /link/* needs `ctx.authenticated` (HTTP: a valid signature, or the PC itself; USB: an established session). The HTTP server
checks the signature before it calls `handle` (it needs the headers), so `handle` never sees an unauthenticated HTTP request for a
protected route; it re-checks anyway, so both transports share one gate.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol
from urllib.parse import parse_qs, urlparse

from . import __version__
from .auth import RateLimiter
from .idle import IdleStore, crc32, local_clock_ms
from .pairing import Pairing, PairingError, parse_hex

log = logging.getLogger("api")

CONTROL_ACTIONS = {"playpause", "play", "pause", "next", "previous", "seek",
                   "shuffle", "shuffle-on", "shuffle-off", "repeat", "repeat-off", "repeat-list", "repeat-track"}   # last 7: Spotify only
VOLUME_ACTIONS = {"volume-up", "volume-down"}
SOURCE_ACTIONS = {"source-next", "source-prev", "source-auto"}
DEBUG_ACTIONS = {"screenshot", "tap_play", "tap_next", "tap_prev", "tap_art",
                 "swipe_right", "swipe_left", "swipe_up", "swipe_down", "swipe_diag", "swipe_bar", "swipe_bar_v", "swipe_text",
                 "touch_cover", "touch_play", "touch_shuffle", "touch_repeat", "key_up", "key_down", "keyhold_up", "keyhold_down",
                 "swipe_right_shot", "swipe_left_shot", "swipe_up_shot", "swipe_down_shot", "key_up_shot", "key_down_shot",
                 "overlay_none_shot", "overlay_pc_shot", "overlay_muted_shot", "overlay_vol_shot",
                 "pair_accept", "pair_cancel", "pair_shot", "touch_pair", "touch_paircancel", "reboot",
                 "link_0", "link_1", "link_2", "link_3", "link_4", "link_5", "link_6",
                 "usb_detach_1500", "usb_detach_3000", "usb_detach_9000", "usb_detach_20000"}

MAX_BODY = 4096                 # the API has no big requests: control routes carry no body at all
PAIR_BODY_MAX = 1024            # /pair/* and /link/*
ART_SIZE_RANGE = (16, 600)
ART_MAXW_RANGE = (16, 800)
STEP_RANGE = (1, 50)
SEEK_MAX_MS = 2 ** 31 - 1
MAX_QUERY_FIELDS = 32

API_VERSION = 2                 # "api" of docs/usb-protocol.md: the routes and JSON of protocol.md
MIN_API_VERSION = 2             # the lowest api this companion still serves


class MediaBackend(Protocol):
    """What `Api` needs from the media monitor (`media.MediaMonitor`; tests pass a stub, so nothing here imports WinRT)."""

    def now_playing(self) -> dict: ...
    def artwork(self, size: int = ..., max_w: Optional[int] = ...) -> Optional[tuple[str, bytes]]: ...
    def control(self, action: str, position_ms: Optional[int] = ..., app: str = ...) -> dict: ...
    def volume(self, action: str, step: int = ..., level: Optional[int] = ..., app: str = ...) -> dict: ...
    def switch_source(self, direction: int, shown_app: str = ...) -> dict: ...
    def auto_source(self) -> dict: ...
    def list_sources(self) -> dict: ...
    def take_debug(self) -> Any: ...
    def request_debug(self, action: str) -> None: ...


@dataclass(frozen=True)
class ClientCtx:
    """Who is asking, as the transport knows it.

    transport       "http" or "usb"
    authenticated   HTTP: the signature was verified, or the request comes from this PC; USB: the link has an established session
    remote          client address (HTTP) or port name (USB); for logs only
    dev_id          USB: the display's persistent id (8 bytes) from its BEACON; empty for HTTP
    compatible      USB: False when api / min_api of the two sides do not overlap: every route except GET /hello then answers 426
    session         USB: this link's `LinkSession` (None: the /link/* routes do not exist)
    if_none_match   HTTP: the request's If-None-Match header (artwork 304)
    """
    transport: str
    authenticated: bool
    remote: str = ""
    dev_id: bytes = b""
    compatible: bool = True
    session: Optional["LinkSession"] = None
    if_none_match: str = ""


@dataclass
class Reply:
    """One answer: HTTP status code, body bytes, content type and the extra headers HTTP sends (USB ignores the headers)."""
    status: int
    body: bytes
    content_type: str = "application/json; charset=utf-8"
    headers: dict = field(default_factory=dict)


JSON_TYPE = "application/json; charset=utf-8"


def json_reply(status: int, obj: dict, headers: Optional[dict] = None) -> Reply:
    return Reply(status, json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), JSON_TYPE, dict(headers or {}))


def error_reply(status: int, text: str, headers: Optional[dict] = None) -> Reply:
    """Error answers are small fixed texts: they never echo anything the client sent."""
    return json_reply(status, {"ok": False, "error": text}, headers)


def split_target(target: str) -> tuple[str, dict]:
    """(route, query as parse_qs would give it) of a request target; ValueError for a target that cannot be parsed
    (too many query fields, malformed escapes in the number fields)."""
    url = urlparse(target)
    return url.path, parse_qs(url.query, max_num_fields=MAX_QUERY_FIELDS)


def is_open_route(method: str, route: str) -> bool:
    """Routes that need no authentication: they are how pairing and the session come to exist, plus two harmless probes."""
    if method in ("GET", "HEAD") and route in ("/hello", "/health"):
        return True
    return route.startswith(("/pair/", "/link/"))


def takes_body(method: str, route: str) -> bool:
    """Only the small JSON POSTs of the pairing and session routes have a body (the USB link rejects a body anywhere else)."""
    return method == "POST" and route.startswith(("/pair/", "/link/"))


def body_limit(route: str) -> int:
    """Largest body a request to this route may carry over USB (HTTP has its own, historical limits in server.py)."""
    return PAIR_BODY_MAX if route.startswith(("/pair/", "/link/")) else 0


def _int_param(q: dict, name: str, default: Optional[int], lo: int, hi: int) -> Optional[int]:
    """A query integer clamped to lo..hi; absent or not a number -> default (which may be None)."""
    try:
        value = int(q[name][0]) if name in q else default
    except ValueError:
        value = default
    return None if value is None else max(lo, min(hi, value))


# ---- the USB session -----------------------------------------------------------------------------------------------------------------

@dataclass
class _Pending:
    dev: bytes
    nd: bytes
    nc: bytes
    key: bytes
    expires: float


class LinkSession:
    """The session handshake of one USB link (docs/usb-protocol.md section 4); one instance per Link, created with it.

        display -> POST /link/open {"dev","nd"}     companion -> {"ok":true,"nc","proof"}  proof = HMAC-SHA256(K, "DMLINK-PC\\n"  cid dev nd nc)
        display -> POST /link/auth {"dev","proof"}  proof = HMAC-SHA256(K, "DMLINK-DEV\\n" cid dev nd nc)       companion -> {"ok":true}

    The session lasts until `drop()` (the link closed, the display rebooted) or until the pairing it was built on is forgotten or
    replaced: `authenticated` re-checks the key on every call, so "Forget paired display" locks the display out at once.
    Nonces are fresh per `/link/open` and are consumed by the first `/link/auth` that follows, whatever its outcome.
    Failures (a bad or missing proof, a malformed body) are counted; ten within 60 s block the handshake for 60 s (429). "unpaired" is not
    a failure: a display whose pairing was forgotten keeps asking until it pairs again. No key, nonce or proof is ever logged.

    Thread-safety: one lock guards all state; the Link's worker thread calls `handle`, any thread may read `authenticated` / `drop`.
    """

    OPEN_TTL_S = 30.0               # /link/auth must follow /link/open within this long

    def __init__(self, pairing: Optional[Pairing], clock: Callable[[], float] = time.monotonic,
                 rand: Callable[[int], bytes] = secrets.token_bytes, limiter: Optional[RateLimiter] = None) -> None:
        self._pairing = pairing
        self._clock = clock
        self._rand = rand
        self._limiter = limiter or RateLimiter(clock=clock)
        self._lock = threading.Lock()
        self._pending: Optional[_Pending] = None
        self._key: Optional[bytes] = None          # K of the established session (None = not authenticated)
        self._dev = b""
        self.auth_failed = False                   # the last handshake failed its proof (the display holds a different key): for status

    # -- state -----------------------------------------------------------------------------------------------------------------------
    @property
    def authenticated(self) -> bool:
        with self._lock:
            if self._key is None:
                return False
            p = self._pairing
            key = p.key() if p is not None else None
            dev = p.device_id if p is not None else None
            if key is None or dev is None or not hmac.compare_digest(key, self._key) or not hmac.compare_digest(dev, self._dev):
                self._key, self._dev = None, b""
                log.info("usb session ended: the pairing changed")
                return False
            return True

    def blocked_for(self) -> int:
        """Seconds until the handshake may be tried again (0 = not blocked)."""
        return self._limiter.blocked_for("usb")

    def drop(self) -> None:
        """End the session and forget a half-done handshake (link closed, display rebooted)."""
        with self._lock:
            had = self._key is not None
            self._key, self._dev, self._pending = None, b"", None
            self.auth_failed = False
        if had:
            log.info("usb session dropped")

    # -- the two routes --------------------------------------------------------------------------------------------------------------
    def handle(self, route: str, body: bytes, beacon_dev: bytes) -> Reply:
        """POST /link/open or /link/auth. `beacon_dev` = the display's id from its BEACON: the handshake must name the same display."""
        wait = self.blocked_for()
        if wait:
            return error_reply(429, "too many failures")
        try:
            obj = json.loads(body.decode("utf-8"))
            if not isinstance(obj, dict):
                raise ValueError
        except (ValueError, RecursionError):
            return self._bad_request()
        try:
            if route == "/link/open":
                return self._open(obj, beacon_dev)
            if route == "/link/auth":
                return self._auth(obj, beacon_dev)
        except PairingError:                       # parse_hex: wrong type, length or alphabet
            return self._bad_request()
        return error_reply(404, "not found")

    def _bad_request(self) -> Reply:
        self._limiter.fail("usb")
        return error_reply(400, "bad_request")

    def _auth_failed(self) -> Reply:
        self._limiter.fail("usb")
        return error_reply(401, "auth")

    def _cid(self) -> bytes:
        return self._pairing.cid if self._pairing is not None else b"\0" * 8

    def _open(self, obj: dict, beacon_dev: bytes) -> Reply:
        dev, nd = parse_hex(obj.get("dev"), 8), parse_hex(obj.get("nd"), 16)
        p = self._pairing
        key = p.key() if p is not None else None
        if key is None or not p.paired_with(dev) or (beacon_dev and not hmac.compare_digest(beacon_dev, dev)):
            return error_reply(401, "unpaired")     # no key, or this is not the paired display: the display offers to pair again
        with self._lock:
            self._key, self._dev = None, b""         # a new handshake revokes the old session until it completes
            self.auth_failed = False
            nc = self._rand(16)
            self._pending = _Pending(dev, nd, nc, key, self._clock() + self.OPEN_TTL_S)
        proof = hmac.new(key, b"DMLINK-PC\n" + self._cid() + dev + nd + nc, hashlib.sha256).digest()
        return json_reply(200, {"ok": True, "nc": nc.hex(), "proof": proof.hex()})

    def _auth(self, obj: dict, beacon_dev: bytes) -> Reply:
        dev, proof = parse_hex(obj.get("dev"), 8), parse_hex(obj.get("proof"), 32)
        p = self._pairing
        key = p.key() if p is not None else None
        with self._lock:
            pend, self._pending = self._pending, None        # one attempt per /link/open, whatever the outcome
        if key is None:
            return error_reply(401, "unpaired")
        if (pend is None or self._clock() > pend.expires or not hmac.compare_digest(pend.dev, dev) or not hmac.compare_digest(pend.key, key)
                or (beacon_dev and not hmac.compare_digest(beacon_dev, dev))):
            self.auth_failed = True
            return self._auth_failed()
        expected = hmac.new(pend.key, b"DMLINK-DEV\n" + self._cid() + dev + pend.nd + pend.nc, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, proof):
            self.auth_failed = True
            return self._auth_failed()
        with self._lock:
            self._key, self._dev = pend.key, dev
            self.auth_failed = False
        log.info("usb session established")
        return json_reply(200, {"ok": True})


# ---- the routes ------------------------------------------------------------------------------------------------------------------------

class Api:
    """Route logic shared by the HTTP server and the USB link. Stateless apart from what its collaborators hold, hence safe to call from
    any number of threads (the HTTP server is threaded, the USB worker is one more caller)."""

    def __init__(self, monitor: MediaBackend, idle: Optional[IdleStore] = None, pairing: Optional[Pairing] = None, dev_tools: bool = False) -> None:
        self.monitor = monitor
        self.idle = idle
        self.pairing = pairing
        self.dev_tools = dev_tools

    def handle(self, method: str, target: str, body: bytes, ctx: ClientCtx) -> Reply:
        """Answer one request. `method` is GET, HEAD (answered like GET; the transport drops the body) or POST."""
        try:
            route, q = split_target(target)
        except ValueError:
            return error_reply(400, "bad request")
        gate = self._gate(method, route, ctx)
        if gate is not None:
            return gate
        if route.startswith("/link/"):
            if ctx.transport != "usb" or ctx.session is None or method != "POST" or route not in ("/link/open", "/link/auth"):
                return error_reply(404, "not found")
            return ctx.session.handle(route, body, ctx.dev_id)
        if route.startswith("/pair/"):
            return self._pair(method, route, q, body)
        if method == "POST":
            return self._post(route, q)
        return self._get(route, q, ctx)

    # -- gate ------------------------------------------------------------------------------------------------------------------------
    def _gate(self, method: str, route: str, ctx: ClientCtx) -> Optional[Reply]:
        if not ctx.compatible and not (method in ("GET", "HEAD") and route == "/hello"):
            return error_reply(426, "update needed")                       # incompatible api versions: only /hello still answers
        if is_open_route(method, route) or ctx.authenticated:
            return None
        if ctx.session is not None and ctx.session.blocked_for():
            return error_reply(429, "too many failures")
        key = self.pairing.key() if self.pairing is not None else None
        return error_reply(401, "unpaired" if key is None else "auth")

    # -- GET -------------------------------------------------------------------------------------------------------------------------
    def _get(self, route: str, q: dict, ctx: ClientCtx) -> Reply:
        monitor, idle = self.monitor, self.idle
        if route == "/now-playing":
            snap = monitor.now_playing()
            if idle is not None:                           # per request, not cached with the snapshot: the clock moves on
                snap = dict(snap, idle_v=idle.version, clock_ms=local_clock_ms())
            if self.dev_tools:
                pending = monitor.take_debug()
                if pending:
                    snap = dict(snap, debug=pending)         # one-shot, only for dev firmware builds
            return json_reply(200, snap)
        if route == "/artwork":
            size = _int_param(q, "size", 300, *ART_SIZE_RANGE)
            max_w = _int_param(q, "maxw", None, *ART_MAXW_RANGE)
            art = monitor.artwork(size, max_w)
            if art is None:
                return error_reply(404, "no artwork")
            art_id, jpeg = art
            etag = f'"{art_id}-{size}-{max_w or 0}"'
            if ctx.if_none_match == etag:
                return Reply(304, b"", "image/jpeg", {"ETag": etag})
            return Reply(200, jpeg, "image/jpeg", {"ETag": etag, "X-Art-Id": art_id})
        if route == "/idle" and idle is not None:
            return json_reply(200, idle.config())
        if route == "/idle/image" and idle is not None:
            data = idle.image()
            if data is None:
                return error_reply(404, "no idle picture")
            return Reply(200, data, "image/jpeg", {"X-Idle-Crc": str(crc32(data))})
        if route == "/volume":
            return json_reply(200, monitor.volume("get", app=q.get("app", [""])[0][:96]))
        if route == "/sources":
            return json_reply(200, monitor.list_sources())
        if route == "/health":
            return json_reply(200, {"ok": True, "service": "desk-media-companion", "version": __version__})
        if route == "/hello":
            return json_reply(200, self._with_debug(self._hello(ctx)))
        return error_reply(404, "not found")

    def _hello(self, ctx: ClientCtx) -> dict:
        p = self.pairing
        hello = {"ok": True, "v": 2, "id": p.cid_hex if p is not None else "0" * 16,
                 "name": p.name if p is not None else "",
                 "paired": p.paired if p is not None else False,
                 "pairing": p.window_open if p is not None else False}
        if ctx.transport == "usb":                           # the LAN answer is unchanged (existing clients compare it exactly)
            hello["transport"] = "usb"
            hello["paired_with_you"] = bool(p is not None and p.paired_with(ctx.dev_id))
        return hello

    def _with_debug(self, obj: dict) -> dict:
        """Development builds only (--dev-tools): the one-shot /debug request also rides on the answers a display that is not paired
        yet polls (it does not read /now-playing then), so the pairing screen can be driven and photographed from the PC."""
        if self.dev_tools:
            pending = self.monitor.take_debug()
            if pending:
                return dict(obj, debug=pending)
        return obj

    # -- pairing ---------------------------------------------------------------------------------------------------------------------
    def _pair(self, method: str, route: str, q: dict, data: bytes) -> Reply:
        pairing = self.pairing
        try:
            if pairing is None:
                raise PairingError(403, "pairing_closed")
            if method == "POST":
                try:
                    obj = json.loads(data.decode("utf-8"))
                except (ValueError, RecursionError):
                    raise PairingError(400, "bad_request") from None
                if not isinstance(obj, dict):
                    raise PairingError(400, "bad_request")
                if route == "/pair/offer":
                    out = pairing.offer(obj.get("dev"), obj.get("pub"), obj.get("ce"))
                elif route == "/pair/reveal":
                    out = pairing.reveal(obj.get("dev"), obj.get("ne"))
                elif route == "/pair/confirm":
                    out = pairing.confirm(obj.get("dev"), obj.get("proof"))
                elif route == "/pair/cancel":
                    out = pairing.cancel(obj.get("dev"))
                else:
                    raise PairingError(404, "not found")
            elif route == "/pair/status":
                out = self._with_debug(pairing.status(q.get("dev", [""])[0]))
            else:
                raise PairingError(404, "not found")
        except PairingError as exc:
            return error_reply(exc.status, exc.error)
        return json_reply(200, out)

    # -- POST ------------------------------------------------------------------------------------------------------------------------
    def _post(self, route: str, q: dict) -> Reply:
        monitor = self.monitor
        dbg = re.fullmatch(r"/debug/(\w+)", route)
        if dbg and self.dev_tools and dbg.group(1) in DEBUG_ACTIONS:
            monitor.request_debug(dbg.group(1))
            return json_reply(200, {"ok": True, "debug": dbg.group(1)})
        if route == "/volume":                          # absolute: POST /volume?level=40
            level = _int_param(q, "level", None, 0, 100)
            if level is None:
                return error_reply(400, "level (0..100) required")
            return json_reply(200, monitor.volume("volume-set", level=level, app=q.get("app", [""])[0][:96]))
        m = re.fullmatch(r"/control/([\w-]+)", route)
        shown_app = q.get("app", q.get("from", [""]))[0][:96]       # the app the display says it is showing
        if m and m.group(1) in VOLUME_ACTIONS:           # relative: the displayed app's own volume, +/- step percent
            return json_reply(200, monitor.volume(m.group(1), step=_int_param(q, "step", 5, *STEP_RANGE), app=shown_app))
        if m and m.group(1) in SOURCE_ACTIONS:           # vertical swipe: next / previous media source (session)
            if m.group(1) == "source-auto":
                return json_reply(200, monitor.auto_source())
            return json_reply(200, monitor.switch_source(1 if m.group(1) == "source-next" else -1, shown_app))
        if not m or m.group(1) not in CONTROL_ACTIONS:
            return error_reply(404, "unknown control")
        action = m.group(1)
        position = None
        if action == "seek":
            position = _int_param(q, "position_ms", None, 0, SEEK_MAX_MS)
            if position is None:
                return error_reply(400, "position_ms required")
        result = monitor.control(action, position, shown_app)
        if action.startswith(("shuffle", "repeat")):                 # Spotify's buttons: the answer carries the fresh state
            result["now"] = monitor.now_playing()
        return json_reply(200, result)

"""Small local HTTP API for the desk display (plain HTTP, LAN only; authenticated after pairing - docs/protocol.md).

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
    POST /pair/offer | /pair/reveal | /pair/confirm | /pair/cancel     GET /pair/status?dev=<16 hex>
                                   the pairing handshake (pairing.py; JSON bodies of at most 1 KB; open, they are how a key comes to exist)
    (--dev-tools only)  POST /debug/<action>   scripted input for the development firmware; same authentication as everything else

Safety rails, in this order:
  1. only private / loopback / link-local clients are served; the Host header must be an IP address (blocks DNS rebinding); every POST
     must carry  X-Desk-Client  (a browser page cannot add it cross-origin without a CORS pre-flight, which is never approved:
     OPTIONS is answered 405 and no CORS header is ever sent);
  2. every route except /hello, /health and /pair/* needs a signature from the paired display: X-DM-Nonce (the nonce of the previous
     answer's X-DM-Next, single use, 60 s) + X-DM-Auth = HMAC-SHA256(K, "DMREQ1\\n" nonce "\\n" METHOD "\\n" target). Nothing paired
     -> 401 {"error":"unpaired"}; bad / missing / replayed signature -> 401 {"error":"auth"} with a fresh X-DM-Next; more than 10
     failures from one address in a minute -> 429 + Retry-After for a minute. Answers to signed requests carry X-DM-Proof. Requests
     from this PC itself (loopback: the tray UI, development tools) are exempt - any local process can reach the media APIs anyway;
  3. bounded input: Content-Length must be a plain integer (else 400), bodies are limited to 4 KB (1 KB for /pair/*; larger -> 413 and
     the connection is closed), numeric parameters are clamped, at most 32 connections are served at once, idle ones are dropped
     after 30 s, and error messages are fixed texts that never echo request data.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

from . import __version__
from .auth import Authenticator, response_proof
from .idle import IdleStore, crc32, local_clock_ms
from .media import MediaMonitor
from .pairing import Pairing, PairingError

log = logging.getLogger("http")

CONTROL_ACTIONS = {"playpause", "play", "pause", "next", "previous", "seek",
                   "shuffle", "shuffle-on", "shuffle-off", "repeat", "repeat-off", "repeat-list", "repeat-track"}   # last 7: Spotify only
VOLUME_ACTIONS = {"volume-up", "volume-down"}
SOURCE_ACTIONS = {"source-next", "source-prev", "source-auto"}
_HOST_IP = re.compile(r"^(\d{1,3}\.){3}\d{1,3}(:\d+)?$|^\[[0-9a-fA-F:]+\](:\d+)?$|^localhost(:\d+)?$")

MAX_BODY = 4096                 # the API has no big requests: control routes carry no body at all
PAIR_BODY_MAX = 1024
MAX_CONNECTIONS = 32
ART_SIZE_RANGE = (16, 600)
ART_MAXW_RANGE = (16, 800)
STEP_RANGE = (1, 50)
SEEK_MAX_MS = 2 ** 31 - 1
_CONTENT_LENGTH = re.compile(r"[0-9]{1,9}")


def _ip(addr: str):
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _client_allowed(addr: str) -> bool:
    ip = _ip(addr)
    return ip is not None and (ip.is_loopback or ip.is_private or ip.is_link_local)


def is_loopback(addr: str) -> bool:
    """Requests from this PC itself (127.0.0.0/8, ::1) are exempt from the signature check."""
    ip = _ip(addr)
    return ip is not None and ip.is_loopback


def requires_signature(addr: str, method: str, path: str) -> bool:
    """Does this request need a valid X-DM-Auth? Not for the PC itself, /hello, /health and the pairing routes."""
    if is_loopback(addr):
        return False
    if method in ("GET", "HEAD") and path in ("/hello", "/health"):
        return False
    return not path.startswith("/pair/")


DEBUG_ACTIONS = {"screenshot", "tap_play", "tap_next", "tap_prev", "tap_art",
                 "swipe_right", "swipe_left", "swipe_up", "swipe_down", "swipe_diag", "swipe_bar", "swipe_bar_v", "swipe_text",
                 "touch_cover", "touch_play", "touch_shuffle", "touch_repeat", "key_up", "key_down", "keyhold_up", "keyhold_down",
                 "swipe_right_shot", "swipe_left_shot", "swipe_up_shot", "swipe_down_shot", "key_up_shot", "key_down_shot",
                 "overlay_none_shot", "overlay_pc_shot", "overlay_muted_shot", "overlay_vol_shot",
                 "pair_accept", "pair_cancel", "pair_shot", "touch_pair", "touch_paircancel"}


def _int_param(q: dict, name: str, default: Optional[int], lo: int, hi: int) -> Optional[int]:
    """A query integer clamped to lo..hi; absent or not a number -> default (which may be None)."""
    try:
        value = int(q[name][0]) if name in q else default
    except ValueError:
        value = default
    return None if value is None else max(lo, min(hi, value))


def make_server(monitor: MediaMonitor, port: int, dev_tools: bool = False, idle: IdleStore | None = None, *,
                pairing: Pairing | None = None, bind: str = "0.0.0.0", auth: Authenticator | None = None) -> ThreadingHTTPServer:   # nosec B104: every interface by design (private clients only, signed)
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "DeskMediaCompanion/" + __version__
        timeout = 30        # drop idle keep-alive connections (e.g. after the display's Wi-Fi dropped) instead of leaking a thread each

        # state of the request being answered (the handler object lives as long as the keep-alive connection: reset per request)
        _proof: Optional[tuple[bytes, str]] = None       # (key, nonce) of an authenticated, non-loopback request
        _nonce_out = False
        _close_after = False

        # -- plumbing ------------------------------------------------------------------------
        def version_string(self) -> str:                # no Python version in the Server header
            return self.server_version

        def log_message(self, fmt, *args):          # silence the default per-request stderr line
            log.debug("%s %s", self.client_address[0], fmt % args)

        def send_error(self, code, message=None, explain=None):
            """Malformed requests the stock parser rejects: a fixed JSON answer (the stock page echoes the request line back)."""
            self.close_connection = True
            try:
                text = HTTPStatus(code).phrase.lower()
            except ValueError:
                text = "error"
            body = json.dumps({"ok": False, "error": text}, separators=(",", ":")).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD" and code >= 200 and code not in (204, 304):
                self.wfile.write(body)

        def _begin(self) -> None:
            self._proof = None
            self._nonce_out = False
            self._close_after = False

        def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if self._nonce_out:                                   # the nonce for the client's next signed request
                self.send_header("X-DM-Next", self.server.auth.next_nonce(self.client_address[0]))
            if self._proof is not None and status not in (401, 429):
                key, nonce = self._proof
                self.send_header("X-DM-Proof", response_proof(key, nonce, status, body))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            if self._close_after:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, obj: dict, extra: dict | None = None) -> None:
            self._send(status, json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                       "application/json; charset=utf-8", extra)

        def _error(self, status: int, text: str, extra: dict | None = None) -> None:
            self._json(status, {"ok": False, "error": text}, extra)

        def _guard(self, needs_client_header: bool) -> bool:
            if not _client_allowed(self.client_address[0]):
                self._error(403, "forbidden")
                return False
            host = self.headers.get("Host", "")
            if not _HOST_IP.match(host):
                self._error(403, "bad host header")
                return False
            if needs_client_header and not self.headers.get("X-Desk-Client"):
                self._error(403, "missing X-Desk-Client header")
                return False
            return True

        def _read_body(self, limit: int) -> Optional[bytes]:
            """The request body (b"" when none), or None after answering 400 / 413 and closing the connection: with an unusable
            Content-Length the framing of the next request on this connection cannot be trusted."""
            if self.headers.get("Transfer-Encoding") is not None:          # chunked bodies are not supported
                self._close_after = True
                self._error(400, "bad request")
                return None
            values = self.headers.get_all("Content-Length") or []
            if len(values) > 1 or (values and not _CONTENT_LENGTH.fullmatch(values[0].strip())):
                self._close_after = True
                self._error(400, "bad content-length")
                return None
            length = int(values[0]) if values else 0
            if length > limit:
                self._close_after = True
                self._error(413, "body too large")
                return None
            data = self.rfile.read(length) if length else b""
            if len(data) != length:                                          # the client hung up mid-body
                self.close_connection = True
                return None
            return data

        def _authenticate(self) -> bool:
            """The signature check for a non-loopback request; answers 401 / 429 itself and returns False when it fails."""
            key = pairing.key() if pairing is not None else None
            if key is None:
                self._error(401, "unpaired")                 # not counted as a failure: a display whose pairing was forgotten keeps asking
                return False
            ip = self.client_address[0]
            wait = self.server.auth.limiter.blocked_for(ip)
            if wait:
                self._error(429, "too many failures", {"Retry-After": str(wait)})
                return False
            nonce = self.headers.get("X-DM-Nonce", "")
            if not self.server.auth.verify(self.command, self.path, nonce, self.headers.get("X-DM-Auth", ""), key):
                self.server.auth.limiter.fail(ip)
                self._error(401, "auth")
                return False
            self._proof = (key, nonce.strip().lower())
            return True

        # -- entry points ------------------------------------------------------------------------
        def _serve(self) -> None:
            self._begin()
            post = self.command == "POST"
            if not self._guard(post):
                return
            self._nonce_out = True
            data = self._read_body(PAIR_BODY_MAX if self.path.startswith("/pair/") else MAX_BODY)
            if data is None:
                return
            try:
                url = urlparse(self.path)
                q = parse_qs(url.query, max_num_fields=32)
            except ValueError:
                self._error(400, "bad request")
                return
            route = url.path
            if requires_signature(self.client_address[0], self.command, route) and not self._authenticate():
                return
            if route.startswith("/pair/"):
                self._pair(route, q, data)
            elif post:
                self._post(route, q)
            else:
                self._get(route, q)

        def do_GET(self):
            self._serve()

        def do_HEAD(self):
            self._serve()

        def do_POST(self):
            self._serve()

        def _refuse(self, status: int, text: str, allow: str = "") -> None:
            """Methods the API does not have. Guarded like the rest; the body is drained so keep-alive framing survives. No CORS headers
            ever: an OPTIONS pre-flight from a web page therefore fails."""
            self._begin()
            if not self._guard(False):
                return
            if self._read_body(MAX_BODY) is None:
                return
            self._error(status, text, {"Allow": allow} if allow else None)

        def do_OPTIONS(self):
            self._refuse(405, "method not allowed", "GET, HEAD, POST")

        def do_PUT(self):
            self._refuse(501, "not implemented")

        do_PATCH = do_DELETE = do_PUT

        # -- routes ----------------------------------------------------------------------------
        def _get(self, route: str, q: dict) -> None:
            if route == "/now-playing":
                snap = monitor.now_playing()
                if idle is not None:                           # per request, not cached with the snapshot: the clock moves on
                    snap = dict(snap, idle_v=idle.version, clock_ms=local_clock_ms())
                if dev_tools:
                    pending = monitor.take_debug()
                    if pending:
                        snap = dict(snap, debug=pending)         # one-shot, only for dev firmware builds
                self._json(200, snap)
            elif route == "/artwork":
                size = _int_param(q, "size", 300, *ART_SIZE_RANGE)
                max_w = _int_param(q, "maxw", None, *ART_MAXW_RANGE)
                art = monitor.artwork(size, max_w)
                if art is None:
                    self._error(404, "no artwork")
                    return
                art_id, jpeg = art
                etag = f'"{art_id}-{size}-{max_w or 0}"'
                if self.headers.get("If-None-Match") == etag:
                    self._send(304, b"", "image/jpeg", {"ETag": etag})
                else:
                    self._send(200, jpeg, "image/jpeg", {"ETag": etag, "X-Art-Id": art_id})
            elif route == "/idle" and idle is not None:
                self._json(200, idle.config())
            elif route == "/idle/image" and idle is not None:
                data = idle.image()
                if data is None:
                    self._error(404, "no idle picture")
                else:
                    self._send(200, data, "image/jpeg", {"X-Idle-Crc": str(crc32(data))})
            elif route == "/volume":
                self._json(200, monitor.volume("get", app=q.get("app", [""])[0][:96]))
            elif route == "/sources":
                self._json(200, monitor.list_sources())
            elif route == "/health":
                self._json(200, {"ok": True, "service": "desk-media-companion", "version": __version__})
            elif route == "/hello":
                hello = {"ok": True, "v": 2, "id": pairing.cid_hex if pairing is not None else "0" * 16,
                         "name": pairing.name if pairing is not None else "",
                         "paired": pairing.paired if pairing is not None else False,
                         "pairing": pairing.window_open if pairing is not None else False}
                self._json(200, self._with_debug(hello))
            else:
                self._error(404, "not found")

        def _with_debug(self, obj: dict) -> dict:
            """Development builds only (--dev-tools): the one-shot /debug request also rides on the answers a display that is not paired
            yet polls (it does not read /now-playing then), so the pairing screen can be driven and photographed from the PC."""
            if dev_tools:
                pending = monitor.take_debug()
                if pending:
                    return dict(obj, debug=pending)
            return obj

        def _pair(self, route: str, q: dict, data: bytes) -> None:
            try:
                if pairing is None:
                    raise PairingError(403, "pairing_closed")
                if self.command == "POST":
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
                self._error(exc.status, exc.error)
                return
            self._json(200, out)

        def _post(self, route: str, q: dict) -> None:
            dbg = re.fullmatch(r"/debug/(\w+)", route)
            if dbg and dev_tools and dbg.group(1) in DEBUG_ACTIONS:
                monitor.request_debug(dbg.group(1))
                self._json(200, {"ok": True, "debug": dbg.group(1)})
                return
            if route == "/volume":                          # absolute: POST /volume?level=40
                level = _int_param(q, "level", None, 0, 100)
                if level is None:
                    self._error(400, "level (0..100) required")
                    return
                self._json(200, monitor.volume("volume-set", level=level, app=q.get("app", [""])[0][:96]))
                return
            m = re.fullmatch(r"/control/([\w-]+)", route)
            shown_app = q.get("app", q.get("from", [""]))[0][:96]       # the app the display says it is showing
            if m and m.group(1) in VOLUME_ACTIONS:           # relative: the displayed app's own volume, +/- step percent
                self._json(200, monitor.volume(m.group(1), step=_int_param(q, "step", 5, *STEP_RANGE), app=shown_app))
                return
            if m and m.group(1) in SOURCE_ACTIONS:           # vertical swipe: next / previous media source (session)
                if m.group(1) == "source-auto":
                    self._json(200, monitor.auto_source())
                else:
                    self._json(200, monitor.switch_source(1 if m.group(1) == "source-next" else -1, shown_app))
                return
            if not m or m.group(1) not in CONTROL_ACTIONS:
                self._error(404, "unknown control")
                return
            action = m.group(1)
            position = None
            if action == "seek":
                position = _int_param(q, "position_ms", None, 0, SEEK_MAX_MS)
                if position is None:
                    self._error(400, "position_ms required")
                    return
            result = monitor.control(action, position, shown_app)
            if action.startswith(("shuffle", "repeat")):                 # Spotify's buttons: the answer carries the fresh state
                result["now"] = monitor.now_playing()
            self._json(200, result)

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

        def __init__(self, address, handler) -> None:
            super().__init__(address, handler)
            self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

        @property
        def active_connections(self) -> int:
            return MAX_CONNECTIONS - self._slots._value             # type: ignore[attr-defined]

        def process_request(self, request, client_address):
            """At most MAX_CONNECTIONS connections are served at once; the next one is closed immediately, so a flood of idle
            sockets from one host cannot grow the thread count without bound."""
            if not self._slots.acquire(blocking=False):
                log.debug("connection limit reached: dropping a connection from %s", client_address[0])
                self.shutdown_request(request)
                return
            try:
                super().process_request(request, client_address)
            except BaseException:
                self._slots.release()
                raise

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                self._slots.release()

        def handle_error(self, request, client_address):   # connection resets from the ESP32 are routine
            log.debug("connection error from %s", client_address[0])

    server = Server((bind, port), Handler)
    server.auth = auth or Authenticator()                    # type: ignore[attr-defined]  (read by the handler; tests may swap in a fake clock)
    return server

"""The LAN transport of the companion API: a small local HTTP server (plain HTTP, LAN only; authenticated after pairing - docs/protocol.md).

The routes themselves (`GET /now-playing`, `/artwork`, `POST /control/...`, `/volume`, `/sources`, `/idle`, `/hello`, `/health`, `/pair/*`,
the --dev-tools `/debug/*`) live in `api.py` and are shared with the USB link; this module is only the HTTP plumbing around them.
It is OPTIONAL since the USB-first release: the product opens no listening socket unless the LAN transport is enabled (`--lan` or the
setting), so a USB-only installation never triggers a Windows firewall prompt.

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

from . import __version__
from .api import (MAX_BODY, PAIR_BODY_MAX, Api, ClientCtx, MediaBackend,  # noqa: F401  (the constants are re-exported for existing importers)
                  CONTROL_ACTIONS, DEBUG_ACTIONS, SOURCE_ACTIONS, VOLUME_ACTIONS, split_target)
from .auth import Authenticator, response_proof
from .idle import IdleStore
from .pairing import Pairing

log = logging.getLogger("http")

_HOST_IP = re.compile(r"^(\d{1,3}\.){3}\d{1,3}(:\d+)?$|^\[[0-9a-fA-F:]+\](:\d+)?$|^localhost(:\d+)?$")

MAX_CONNECTIONS = 32
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


def make_server(monitor: MediaBackend, port: int, dev_tools: bool = False, idle: IdleStore | None = None, *,
                pairing: Pairing | None = None, bind: str = "0.0.0.0", auth: Authenticator | None = None,   # nosec B104: every interface by design (private clients only, signed)
                api: Api | None = None) -> ThreadingHTTPServer:
    """`api` lets the application share one `Api` between this server and the USB link; without it one is built from the other arguments."""
    api = api or Api(monitor, idle, pairing, dev_tools)
    pairing = api.pairing

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
                route, _query = split_target(self.path)
            except ValueError:
                self._error(400, "bad request")
                return
            addr = self.client_address[0]
            needs_signature = requires_signature(addr, self.command, route)
            if needs_signature and not self._authenticate():
                return
            ctx = ClientCtx("http", authenticated=needs_signature or is_loopback(addr), remote=addr,
                            if_none_match=self.headers.get("If-None-Match", ""))
            reply = api.handle(self.command, self.path, data, ctx)
            self._send(reply.status, reply.body, reply.content_type, reply.headers)

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

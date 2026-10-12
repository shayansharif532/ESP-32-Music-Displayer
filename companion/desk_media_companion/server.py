"""Small local HTTP API for the desk display (plain HTTP, LAN only).

    GET  /now-playing              current media session as JSON
    GET  /artwork[?size=300]       current artwork, baseline JPEG, square (ETag = art_id)
    POST /control/playpause        also: /control/play  /control/pause  /control/next  /control/previous
    POST /control/seek?position_ms=N
    GET  /health

Safety rails: only private/loopback clients are served; the Host header must be an IP address (blocks DNS
rebinding); every POST must carry the header  X-Desk-Client  (a browser page on the internet cannot add that
header cross-origin without a CORS pre-flight, which we never approve - so web pages cannot press your buttons).
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__
from .media import MediaMonitor

log = logging.getLogger("http")

CONTROL_ACTIONS = {"playpause", "play", "pause", "next", "previous", "seek"}
_HOST_IP = re.compile(r"^(\d{1,3}\.){3}\d{1,3}(:\d+)?$|^\[[0-9a-fA-F:]+\](:\d+)?$|^localhost(:\d+)?$")


def _client_allowed(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def make_server(monitor: MediaMonitor, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "DeskMediaCompanion/" + __version__
        timeout = 30        # drop idle keep-alive connections (e.g. after the display's Wi-Fi dropped) instead of leaking a thread each

        # -- plumbing ------------------------------------------------------------------------
        def log_message(self, fmt, *args):          # silence the default per-request stderr line
            log.debug("%s %s", self.client_address[0], fmt % args)

        def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, obj: dict) -> None:
            self._send(status, json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _guard(self) -> bool:
            if not _client_allowed(self.client_address[0]):
                self._json(403, {"ok": False, "error": "forbidden"})
                return False
            host = self.headers.get("Host", "")
            if not _HOST_IP.match(host):
                self._json(403, {"ok": False, "error": "bad host header"})
                return False
            return True

        # -- routes ----------------------------------------------------------------------------
        def do_GET(self):
            if not self._guard():
                return
            url = urlparse(self.path)
            if url.path == "/now-playing":
                self._json(200, monitor.now_playing())
            elif url.path == "/artwork":
                q = parse_qs(url.query)
                try:
                    size = int(q.get("size", ["300"])[0])
                except ValueError:
                    size = 300
                art = monitor.artwork(size)
                if art is None:
                    self._json(404, {"ok": False, "error": "no artwork"})
                    return
                art_id, jpeg = art
                etag = f'"{art_id}-{size}"'
                if self.headers.get("If-None-Match") == etag:
                    self._send(304, b"", "image/jpeg", {"ETag": etag})
                else:
                    self._send(200, jpeg, "image/jpeg", {"ETag": etag, "X-Art-Id": art_id})
            elif url.path == "/health":
                self._json(200, {"ok": True, "service": "desk-media-companion", "version": __version__})
            else:
                self._json(404, {"ok": False, "error": "not found"})

        do_HEAD = do_GET

        def do_POST(self):
            if not self._guard():
                return
            if not self.headers.get("X-Desk-Client"):
                self._json(403, {"ok": False, "error": "missing X-Desk-Client header"})
                return
            # drain any request body so the keep-alive connection stays usable
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(min(length, 4096))
            url = urlparse(self.path)
            m = re.fullmatch(r"/control/(\w+)", url.path)
            if not m or m.group(1) not in CONTROL_ACTIONS:
                self._json(404, {"ok": False, "error": "unknown control"})
                return
            action = m.group(1)
            position = None
            if action == "seek":
                try:
                    position = int(parse_qs(url.query).get("position_ms", [""])[0])
                except ValueError:
                    self._json(400, {"ok": False, "error": "position_ms required"})
                    return
            self._json(200, monitor.control(action, position))

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

        def handle_error(self, request, client_address):   # connection resets from the ESP32 are routine
            log.debug("connection error from %s", client_address[0])

    return Server(("0.0.0.0", port), Handler)

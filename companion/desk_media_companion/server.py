"""Small local HTTP API for the desk display (plain HTTP, LAN only).

    GET  /now-playing              current media session as JSON
    GET  /artwork[?size=312[&maxw=432]]
                                   current artwork, baseline JPEG (ETag = art_id + size). Without maxw: a size x size square.
                                   With maxw: the artwork keeps its aspect ratio inside a maxw x size box (square art stays
                                   square, 16:9 art becomes a landscape card); never stretched. App logos baked into the
                                   image (Spotify) and black letterbox bars are removed first (see artwork.py).
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
    POST /control/source-auto      back to automatic (follow the best session)
    GET  /sources                  the swipe list: [{app, name, state, title, artist, displayed}], mode auto|manual
    POST /control/shuffle[?app=Spotify.exe]   Spotify only: toggle shuffle (also /shuffle-on /shuffle-off)
    POST /control/repeat[?app=...]            Spotify only: cycle repeat off -> list -> track -> off (also /repeat-off /repeat-list /repeat-track)
                                   -> {"ok":true,"app":"Spotify.exe","spotify":true,"shuffle":true,"repeat":1,"can_shuffle":true,"can_repeat":true,"now":{...}}
                                   refused (ok:false) for every other app. /now-playing carries spotify, shuffle, repeat (0 off, 1 list, 2 track), can_shuffle, can_repeat.
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

CONTROL_ACTIONS = {"playpause", "play", "pause", "next", "previous", "seek",
                   "shuffle", "shuffle-on", "shuffle-off", "repeat", "repeat-off", "repeat-list", "repeat-track"}   # last 7: Spotify only
VOLUME_ACTIONS = {"volume-up", "volume-down"}
SOURCE_ACTIONS = {"source-next", "source-prev", "source-auto"}
_HOST_IP = re.compile(r"^(\d{1,3}\.){3}\d{1,3}(:\d+)?$|^\[[0-9a-fA-F:]+\](:\d+)?$|^localhost(:\d+)?$")


def _client_allowed(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


DEBUG_ACTIONS = {"screenshot", "tap_play", "tap_next", "tap_prev", "tap_art",
                 "swipe_right", "swipe_left", "swipe_up", "swipe_down", "swipe_diag", "swipe_bar", "swipe_bar_v", "swipe_text",
                 "touch_cover", "touch_play", "touch_shuffle", "touch_repeat", "key_up", "key_down", "keyhold_up", "keyhold_down",
                 "swipe_right_shot", "swipe_left_shot", "swipe_up_shot", "swipe_down_shot", "key_up_shot", "key_down_shot",
                 "overlay_none_shot", "overlay_pc_shot", "overlay_muted_shot", "overlay_vol_shot"}


def make_server(monitor: MediaMonitor, port: int, dev_tools: bool = False) -> ThreadingHTTPServer:
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
                snap = monitor.now_playing()
                if dev_tools:
                    pending = monitor.take_debug()
                    if pending:
                        snap = dict(snap, debug=pending)         # one-shot, only for dev firmware builds
                self._json(200, snap)
            elif url.path == "/artwork":
                q = parse_qs(url.query)
                try:
                    size = int(q.get("size", ["300"])[0])
                except ValueError:
                    size = 300
                try:
                    max_w = int(q["maxw"][0]) if "maxw" in q else None
                except ValueError:
                    max_w = None
                art = monitor.artwork(size, max_w)
                if art is None:
                    self._json(404, {"ok": False, "error": "no artwork"})
                    return
                art_id, jpeg = art
                etag = f'"{art_id}-{size}-{max_w or 0}"'
                if self.headers.get("If-None-Match") == etag:
                    self._send(304, b"", "image/jpeg", {"ETag": etag})
                else:
                    self._send(200, jpeg, "image/jpeg", {"ETag": etag, "X-Art-Id": art_id})
            elif url.path == "/volume":
                self._json(200, monitor.volume("get", app=parse_qs(url.query).get("app", [""])[0][:96]))
            elif url.path == "/sources":
                self._json(200, monitor.list_sources())
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
            dbg = re.fullmatch(r"/debug/(\w+)", url.path)
            if dbg and dev_tools and dbg.group(1) in DEBUG_ACTIONS:
                monitor.request_debug(dbg.group(1))
                self._json(200, {"ok": True, "debug": dbg.group(1)})
                return
            if url.path == "/volume":                       # absolute: POST /volume?level=40
                try:
                    level = int(parse_qs(url.query).get("level", [""])[0])
                except ValueError:
                    self._json(400, {"ok": False, "error": "level (0..100) required"})
                    return
                self._json(200, monitor.volume("volume-set", level=level, app=parse_qs(url.query).get("app", [""])[0][:96]))
                return
            m = re.fullmatch(r"/control/([\w-]+)", url.path)
            q = parse_qs(url.query)
            shown_app = q.get("app", q.get("from", [""]))[0][:96]       # the app the display says it is showing
            if m and m.group(1) in VOLUME_ACTIONS:           # relative: the displayed app's own volume, +/- step percent
                try:
                    step = int(q.get("step", ["5"])[0])
                except ValueError:
                    step = 5
                self._json(200, monitor.volume(m.group(1), step=step, app=shown_app))
                return
            if m and m.group(1) in SOURCE_ACTIONS:           # vertical swipe: next / previous media source (session)
                if m.group(1) == "source-auto":
                    self._json(200, monitor.auto_source())
                else:
                    self._json(200, monitor.switch_source(1 if m.group(1) == "source-next" else -1, shown_app))
                return
            if not m or m.group(1) not in CONTROL_ACTIONS:
                self._json(404, {"ok": False, "error": "unknown control"})
                return
            action = m.group(1)
            position = None
            if action == "seek":
                try:
                    position = int(q.get("position_ms", [""])[0])
                except ValueError:
                    self._json(400, {"ok": False, "error": "position_ms required"})
                    return
            result = monitor.control(action, position, shown_app)
            if action.startswith(("shuffle", "repeat")):                 # Spotify's buttons: the answer carries the fresh state
                result["now"] = monitor.now_playing()
            self._json(200, result)

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

        def handle_error(self, request, client_address):   # connection resets from the ESP32 are routine
            log.debug("connection error from %s", client_address[0])

    return Server(("0.0.0.0", port), Handler)

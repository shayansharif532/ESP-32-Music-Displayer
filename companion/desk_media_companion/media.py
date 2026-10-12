"""Windows media-session access (GlobalSystemMediaTransportControlsSessionManager / Windows.Media.Control).

Everything that touches WinRT runs on one private asyncio loop thread; the public methods are plain blocking calls
that the HTTP server threads can use.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import io
import logging
import threading
import time
from typing import Any, Optional

from PIL import Image, ImageEnhance, ImageFilter, ImageOps
from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)
from winrt.windows.storage.streams import DataReader

log = logging.getLogger("media")

STATE_NAMES = {
    PlaybackStatus.CLOSED: "closed",
    PlaybackStatus.OPENED: "opened",
    PlaybackStatus.CHANGING: "changing",
    PlaybackStatus.STOPPED: "stopped",
    PlaybackStatus.PAUSED: "paused",
    PlaybackStatus.PLAYING: "playing",
}

SNAPSHOT_TTL_S = 0.25          # several clients polling at once share one WinRT read
ART_RETRY_INTERVAL_S = 1.5     # apps often publish the thumbnail a moment after the title
ART_MAX_TRIES = 8
ART_DEFAULT_SIZE = 300
ART_BG = (16, 16, 22)          # same dark tone as the device UI, used for letterboxing
CONTROL_TIMEOUT_S = 4.0


def _ms(delta: Optional[dt.timedelta]) -> int:
    return int(delta.total_seconds() * 1000) if delta is not None else 0


class MediaMonitor:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="winrt-loop", daemon=True)
        self._manager = None
        self._snap: Optional[dict[str, Any]] = None
        self._snap_at = 0.0
        # artwork state (only touched on the loop thread, except reads under _art_lock)
        self._art_lock = threading.Lock()
        self._art_raw: Optional[bytes] = None
        self._art_id = ""
        self._art_cache: dict[int, bytes] = {}
        self._track_key = ""
        self._art_tries = 0
        self._art_last_try = 0.0
        self._seq = 0
        self._last_sig = ""
        self._last_playing_app = ""      # when everything is paused, stay on the app that was playing last
        self._debug_lock = threading.Lock()
        self._debug_request = ""          # dev tools: one-shot request handed to the display with the next /now-playing

    # ---- lifecycle ------------------------------------------------------------------------------
    def start(self) -> None:
        self._thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _call(self, coro, timeout: float):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=timeout)

    # ---- public blocking API --------------------------------------------------------------------
    def now_playing(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._snap is not None and now - self._snap_at < SNAPSHOT_TTL_S:
            return self._snap
        try:
            snap = self._call(self._snapshot(), 5.0)
        except Exception as exc:                      # never let a WinRT hiccup kill the API
            log.warning("snapshot failed: %s", exc)
            snap = {"ok": False, "error": str(exc)[:120], "has_session": False}
        self._snap, self._snap_at = snap, time.monotonic()
        return snap

    # ---- dev tools (only reachable when the companion runs with --dev-tools) ---------------------
    def request_debug(self, action: str) -> None:
        with self._debug_lock:
            self._debug_request = action

    def take_debug(self) -> str:
        with self._debug_lock:
            action, self._debug_request = self._debug_request, ""
        return action

    def artwork(self, size: int = ART_DEFAULT_SIZE) -> Optional[tuple[str, bytes]]:
        """(art_id, baseline JPEG bytes at size x size) for the current track, or None."""
        size = max(64, min(int(size), 640))
        with self._art_lock:
            raw, art_id = self._art_raw, self._art_id
            cached = self._art_cache.get(size)
        if not raw:
            return None
        if cached is None:
            cached = _render_square_jpeg(raw, size)
            with self._art_lock:
                if art_id == self._art_id:
                    self._art_cache[size] = cached
        return art_id, cached

    def control(self, action: str, position_ms: Optional[int] = None) -> dict[str, Any]:
        try:
            result = self._call(self._control(action, position_ms), CONTROL_TIMEOUT_S)
        except Exception as exc:
            log.warning("control %s failed: %s", action, exc)
            result = {"ok": False, "action": action, "error": str(exc)[:120]}
        self._snap = None                             # next poll must see the effect
        return result

    # ---- session selection ----------------------------------------------------------------------
    async def _get_manager(self):
        if self._manager is None:
            self._manager = await SessionManager.request_async()
        return self._manager

    @staticmethod
    def _status(session) -> Optional[PlaybackStatus]:
        try:
            return session.get_playback_info().playback_status
        except Exception:
            return None

    def _candidates(self, mgr) -> list:
        """Sessions in preference order: the playing one first (the system's current session if it is playing),
        then the current session, then the rest."""
        try:
            sessions = list(mgr.get_sessions())
        except Exception:
            sessions = []
        try:
            current = mgr.get_current_session()
        except Exception:
            current = None
        ordered: list = []

        def add(s) -> None:
            if s is not None and all(s.source_app_user_model_id != o.source_app_user_model_id for o in ordered):
                ordered.append(s)

        if current is not None and self._status(current) == PlaybackStatus.PLAYING:
            add(current)
        for s in sessions:
            if self._status(s) == PlaybackStatus.PLAYING:
                add(s)
        if self._last_playing_app:
            for s in sessions:
                if s.source_app_user_model_id == self._last_playing_app:
                    add(s)
        add(current)
        for s in sessions:
            add(s)
        return ordered

    # ---- snapshot -------------------------------------------------------------------------------
    async def _select(self):
        """The session shown on the display AND controlled by the buttons: the first candidate that answers.
        Returns (session, media properties, playback info, timeline) or None. Dead / not-ready sessions are skipped."""
        mgr = await self._get_manager()
        for session in self._candidates(mgr):
            try:
                props = await session.try_get_media_properties_async()
                info = session.get_playback_info()
                tl = session.get_timeline_properties()
            except Exception as exc:
                log.debug("skipping session %s: %s", getattr(session, "source_app_user_model_id", "?"), exc)
                continue
            return session, props, info, tl
        return None

    async def _snapshot(self) -> dict[str, Any]:
        selected = await self._select()
        if selected is not None:
            session, props, info, tl = selected

            app = session.source_app_user_model_id or ""
            status = info.playback_status
            state = STATE_NAMES.get(status, "unknown")
            if status == PlaybackStatus.PLAYING:
                self._last_playing_app = app
            title = props.title or ""
            artist = props.artist or props.album_artist or props.subtitle or ""
            album = props.album_title or ""

            start_ms = _ms(tl.start_time)
            duration_ms = max(0, _ms(tl.end_time) - start_ms)
            pos_ms = max(0, _ms(tl.position) - start_ms)
            if status == PlaybackStatus.PLAYING:
                updated = tl.last_updated_time
                if updated is not None and updated.year > 2000:
                    now = dt.datetime.now(dt.timezone.utc)
                    if updated.tzinfo is None:
                        updated = updated.replace(tzinfo=dt.timezone.utc)
                    rate = info.playback_rate if info.playback_rate else 1.0
                    pos_ms += int(max(0.0, (now - updated).total_seconds()) * 1000 * rate)
            if duration_ms:
                pos_ms = min(pos_ms, duration_ms)

            track_key = hashlib.sha1(f"{app}|{title}|{artist}|{album}|{duration_ms}".encode()).hexdigest()[:10]
            await self._update_artwork(track_key, props)

            ctl = info.controls
            snap = {
                "ok": True,
                "version": 1,
                "has_session": True,
                "app": app,
                "state": state,
                "playing": status == PlaybackStatus.PLAYING,
                "title": title,
                "artist": artist,
                "album": album,
                "position_ms": pos_ms,
                "duration_ms": duration_ms,
                "track_id": track_key,
                "art_id": self._art_id,
                "can_play": bool(ctl.is_play_enabled),
                "can_pause": bool(ctl.is_pause_enabled),
                "can_next": bool(ctl.is_next_enabled),
                "can_prev": bool(ctl.is_previous_enabled),
                "can_seek": bool(ctl.is_playback_position_enabled),
            }
            sig = f"{app}|{state}|{track_key}|{self._art_id}"
            if sig != self._last_sig:
                self._last_sig = sig
                self._seq += 1
                log.info("now: [%s] %s - %s (%s) %s", app, title, artist, state,
                         "art " + self._art_id if self._art_id else "no art yet")
            snap["seq"] = self._seq
            return snap

        sig = "none"
        if sig != self._last_sig:
            self._last_sig = sig
            self._seq += 1
            log.info("no media session")
        with self._art_lock:
            self._art_raw, self._art_id, self._art_cache = None, "", {}
        self._track_key = ""
        return {"ok": True, "version": 1, "has_session": False, "state": "none", "playing": False,
                "title": "", "artist": "", "album": "", "position_ms": 0, "duration_ms": 0,
                "track_id": "", "art_id": "", "can_play": False, "can_pause": False, "can_next": False,
                "can_prev": False, "can_seek": False, "seq": self._seq}

    # ---- artwork --------------------------------------------------------------------------------
    async def _update_artwork(self, track_key: str, props) -> None:
        if track_key != self._track_key:
            self._track_key = track_key
            self._art_tries = 0
            self._art_last_try = 0.0
            with self._art_lock:
                self._art_raw, self._art_id, self._art_cache = None, "", {}
        if self._art_id or self._art_tries >= ART_MAX_TRIES:
            return
        thumb = props.thumbnail
        if thumb is None:
            return
        now = time.monotonic()
        if now - self._art_last_try < ART_RETRY_INTERVAL_S:
            return
        self._art_last_try = now
        self._art_tries += 1
        try:
            stream = await thumb.open_read_async()
            size = int(stream.size)
            if size <= 0 or size > 16 * 1024 * 1024:
                return
            reader = DataReader(stream)
            await reader.load_async(size)
            data = bytearray(size)
            reader.read_bytes(data)
            raw = bytes(data)
            Image.open(io.BytesIO(raw)).verify()      # must be a decodable image
        except Exception as exc:
            log.debug("thumbnail read failed (try %d): %s", self._art_tries, exc)
            return
        art_id = hashlib.sha1(raw).hexdigest()[:10]
        with self._art_lock:
            self._art_raw, self._art_id, self._art_cache = raw, art_id, {}
        log.info("artwork loaded: %d bytes, id %s", len(raw), art_id)

    # ---- controls -------------------------------------------------------------------------------
    async def _control(self, action: str, position_ms: Optional[int]) -> dict[str, Any]:
        selected = await self._select()               # the very session the display is showing
        if selected is None:
            return {"ok": False, "action": action, "error": "no media session"}
        session, _props, info, _tl = selected
        for _once in (0,):
            try:
                ctl = info.controls
                playing = info.playback_status == PlaybackStatus.PLAYING
                if action == "playpause":
                    if playing and ctl.is_pause_enabled:
                        ok = await session.try_pause_async()
                    elif not playing and ctl.is_play_enabled:
                        ok = await session.try_play_async()
                    else:
                        ok = await session.try_toggle_play_pause_async()
                elif action == "play":
                    ok = await session.try_play_async()
                elif action == "pause":
                    ok = await session.try_pause_async()
                elif action == "next":
                    ok = await session.try_skip_next_async() if ctl.is_next_enabled else False
                elif action == "previous":
                    ok = await session.try_skip_previous_async() if ctl.is_previous_enabled else False
                elif action == "seek":
                    if position_ms is None or not ctl.is_playback_position_enabled:
                        ok = False
                    else:
                        ok = await session.try_change_playback_position_async(int(position_ms) * 10_000)
                else:
                    return {"ok": False, "action": action, "error": "unknown action"}
                log.info("control %s on %s -> %s", action, session.source_app_user_model_id, "ok" if ok else "refused")
                return {"ok": bool(ok), "action": action, "app": session.source_app_user_model_id}
            except Exception as exc:
                log.warning("control %s on %s failed: %s", action, session.source_app_user_model_id, exc)
                return {"ok": False, "action": action, "error": str(exc)[:120]}
        return {"ok": False, "action": action, "error": "no media session"}


def _render_square_jpeg(raw: bytes, size: int) -> bytes:
    """Any image -> size x size baseline JPEG. Near-square art is centre-cropped; wide/tall art (e.g. 16:9 video
    thumbnails) is letterboxed so nothing is cut off."""
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, ART_BG + (255,))
        bg.alpha_composite(img)
        img = bg
    img = img.convert("RGB")
    w, h = img.size
    ratio = w / h if h else 1.0
    if 0.88 <= ratio <= 1.14:
        img = ImageOps.fit(img, (size, size), Image.LANCZOS)
    else:
        # Wide / tall art (e.g. a 16:9 video thumbnail): keep all of it, sharp, in the middle, and fill the rest of the square
        # with a blurred, dimmed copy of itself instead of dead black bars - no empty borders around the cover.
        fitted = ImageOps.contain(img, (size, size), Image.LANCZOS)
        canvas = ImageOps.fit(img, (size, size), Image.BILINEAR)
        canvas = canvas.filter(ImageFilter.GaussianBlur(max(4, size // 14)))
        canvas = ImageEnhance.Brightness(canvas).enhance(0.6)
        canvas.paste(fitted, ((size - fitted.width) // 2, (size - fitted.height) // 2))
        img = canvas
    out = io.BytesIO()
    # baseline (non-progressive) 4:2:0 JPEG: what the ESP32's TJpgDec decoder handles
    img.save(out, format="JPEG", quality=88, optimize=False, progressive=False, subsampling=2)
    return out.getvalue()

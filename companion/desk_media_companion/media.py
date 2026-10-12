"""Windows media-session access (GlobalSystemMediaTransportControlsSessionManager / Windows.Media.Control).

Everything that touches WinRT runs on one private asyncio loop thread; the public methods are plain blocking calls
that the HTTP server threads can use.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from PIL import Image
from . import artwork as art_tools
from . import sources as src_model
from . import volume as app_volume
from winrt.windows.media import MediaPlaybackAutoRepeatMode as RepeatMode
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

# Spotify's repeat button: off -> repeat the playlist / album ("list") -> repeat the track -> off
_REPEAT_TO_INT = {RepeatMode.NONE: 0, RepeatMode.LIST: 1, RepeatMode.TRACK: 2}
_INT_TO_REPEAT = {v: k for k, v in _REPEAT_TO_INT.items()}
_REPEAT_NAMES = {"off": 0, "list": 1, "track": 2}

SNAPSHOT_TTL_S = 0.25          # several clients polling at once share one WinRT read
WATCH_INTERVAL_S = 0.4         # how often the selection model looks at all sessions in the background
ART_RETRY_INTERVAL_S = 1.5     # apps often publish the thumbnail a moment after the title
ART_MAX_TRIES = 8
ART_DEFAULT_SIZE = 300
CONTROL_TIMEOUT_S = 4.0


def _ms(delta: Optional[dt.timedelta]) -> int:
    return int(delta.total_seconds() * 1000) if delta is not None else 0


@dataclass
class _Selection:
    """The session the display shows: the session plus a consistent read of it, and the swipe list at that moment."""
    session: Any
    props: Any
    info: Any
    timeline: Any
    sources: list = field(default_factory=list)

    @property
    def app(self) -> str:
        return self.session.source_app_user_model_id or ""


class MediaMonitor:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="winrt-loop", daemon=True)
        self._manager = None
        self._snap: Optional[dict[str, Any]] = None
        self._snap_at = 0.0
        # artwork state (only touched on the loop thread, except reads under _art_lock)
        self._art_lock = threading.Lock()
        self._art_img: Optional[Image.Image] = None     # the artwork itself (app logo / letterbox already removed)
        self._art_id = ""
        self._art_cache: dict[tuple[int, Optional[int]], bytes] = {}
        self._track_key = ""
        self._art_tries = 0
        self._art_last_try = 0.0
        self._seq = 0
        self._last_sig = ""
        self.sel = src_model.Selector()  # AUTO / MANUAL_TEMP selection state machine (loop thread only)
        self.ignore_apps: set[str] = set()   # dev only: never look at these app ids (keeps fake test sessions off the real display)
        self.only_apps: set[str] = set()     # dev only: ignore every session except these app ids (isolated tests next to the owner's real apps)
        self.spotify_ids: set[str] = set()   # dev only: extra app ids treated as Spotify (--treat-as-spotify python.exe, the fake session)
        self._debug_lock = threading.Lock()
        self._debug_request = ""          # dev tools: one-shot request handed to the display with the next /now-playing

    @property
    def prefer_app(self) -> str:         # dev only: always show this app's session when it exists (e.g. the fake test session)
        return self.sel.prefer

    @prefer_app.setter
    def prefer_app(self, app: str) -> None:
        self.sel.prefer = app

    def _is_spotify(self, app_id: str) -> bool:
        return src_model.is_spotify(app_id) or app_id.lower() in self.spotify_ids

    # ---- lifecycle ------------------------------------------------------------------------------
    def start(self) -> None:
        self._thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.create_task(self._watch())
        self._loop.run_forever()

    async def _watch(self) -> None:
        """Keeps the selection model up to date even when nobody polls: a session that starts playing, or a selected session that
        disappears, is noticed within WATCH_INTERVAL_S (reading every session costs a few ms)."""
        while True:
            try:
                await self._read_sources(await self._get_manager())
            except Exception as exc:
                log.debug("watcher: %s", exc)
            await asyncio.sleep(WATCH_INTERVAL_S)

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

    def artwork(self, size: int = ART_DEFAULT_SIZE, max_w: Optional[int] = None) -> Optional[tuple[str, bytes]]:
        """(art_id, baseline JPEG bytes) for the current track, or None.

        Without max_w: a size x size square (legacy). With max_w: the artwork keeps its aspect ratio inside a max_w x size box
        (square art -> size x size, 16:9 art -> a landscape card), never stretched."""
        size = max(64, min(int(size), 640))
        max_w = None if max_w is None else max(size, min(int(max_w), 960))
        key = (size, max_w)
        with self._art_lock:
            img, art_id = self._art_img, self._art_id
            cached = self._art_cache.get(key)
            img = img.copy() if (img is not None and cached is None) else img
        if img is None:
            return None
        if cached is None:
            cached = art_tools.render_jpeg(img, size, max_w)
            with self._art_lock:
                if art_id == self._art_id:
                    self._art_cache[key] = cached
        return art_id, cached

    def control(self, action: str, position_ms: Optional[int] = None, app: str = "") -> dict[str, Any]:
        """Transport control of the session on screen. `app` = the app id the display says it shows: the command goes to
        exactly that session (and is refused if it no longer exists), never to whatever else became active meanwhile."""
        try:
            result = self._call(self._control(action, position_ms, app), CONTROL_TIMEOUT_S)
        except Exception as exc:
            log.warning("control %s failed: %s", action, exc)
            result = {"ok": False, "action": action, "error": str(exc)[:120]}
        self._snap = None                             # next poll must see the effect
        return result

    def switch_source(self, direction: int, shown_app: str = "") -> dict[str, Any]:
        """Vertical swipe: select the next (+1) / previous (-1) usable media session - a TEMPORARY manual selection (MANUAL_TEMP,
        see sources.py), not a pin; the answer carries the new now-playing snapshot so the display can switch at once.
        Does nothing when there is only one source. shown_app = the app the display shows, so the swipe is relative to what
        the user sees."""
        try:
            result = self._call(self._switch_source(direction, shown_app), CONTROL_TIMEOUT_S)
        except Exception as exc:
            log.warning("source switch failed: %s", exc)
            return {"ok": False, "action": "source", "error": str(exc)[:120]}
        self._snap = None
        if result.get("ok"):
            result["now"] = self.now_playing()
        return result

    def auto_source(self) -> dict[str, Any]:
        """Back to automatic mode (follow the best session). Not wired to a gesture; a development / support aid."""
        async def work() -> None:
            self.sel.to_auto()
        self._call(work(), CONTROL_TIMEOUT_S)
        self._snap = None
        log.info("source selection: automatic (requested)")
        return {"ok": True, "action": "source-auto", "now": self.now_playing()}

    def list_sources(self) -> dict[str, Any]:
        """The sessions the display can switch between, in swipe order (read-only; for tests and support)."""
        try:
            return self._call(self._list_sources(), CONTROL_TIMEOUT_S)
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:120]}

    def volume(self, action: str, step: int = app_volume.STEP_DEFAULT, level: Optional[int] = None,
               app: str = "") -> dict[str, Any]:
        """Per-application volume of the app whose media session is shown on the display (NOT the Windows master volume).
        action: "get" | "volume-up" | "volume-down" | "volume-set" (level 0..100).
        `app` is the app id the display says it is showing: the volume goes to that app's audio session, and is refused if
        that app no longer has a media session (the screen is about to change), so it can never hit a different app."""
        try:
            app = self._call(self._resolve_app(app), CONTROL_TIMEOUT_S)
        except Exception as exc:
            log.warning("volume %s: cannot read the media session: %s", action, exc)
            return {"ok": False, "action": action, "error": str(exc)[:120], "volume": None}
        if not app:
            return {"ok": False, "action": action, "error": "no media session", "volume": None}
        step = max(1, min(int(step), 50))
        if action == "volume-up":
            result = app_volume.change_volume(app, delta=step)
        elif action == "volume-down":
            result = app_volume.change_volume(app, delta=-step)
        elif action == "volume-set" and level is not None:
            result = app_volume.change_volume(app, level=level)
        else:
            result = app_volume.get_volume(app)
        result["action"] = action
        return result

    async def _resolve_app(self, wanted: str) -> str:
        """The app a volume request goes to: the one the display says it shows (if it still has a session), else - only when the
        display named none - the session the display would show now."""
        selected = await self._select(wanted)
        if selected is not None and wanted:
            self.sel.touch(time.monotonic(), selected.app)       # a volume key press is an interaction with the shown session
        return selected.app if selected else ""

    # ---- session selection (the model itself is in sources.py) ------------------------------------
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

    async def _read_sources(self, mgr) -> tuple[list[src_model.Source], dict[str, Any], str]:
        """Every Windows media session, read once: (sources, {app id: session}, app id of the system's current session).
        A dead session (device not ready ...) is kept as an unreadable source and never offered. Cheap: a few ms in total."""
        try:
            sessions = list(mgr.get_sessions())
        except Exception:
            sessions = []
        srcs: list[src_model.Source] = []
        objs: dict[str, Any] = {}
        for s in sessions:
            app = s.source_app_user_model_id or ""
            if (not app or app in objs or app.lower() in self.ignore_apps
                    or (self.only_apps and app.lower() not in self.only_apps)):
                continue
            objs[app] = s
            state = STATE_NAMES.get(self._status(s), "")
            try:
                props = await s.try_get_media_properties_async()
                srcs.append(src_model.Source(app, state, props.title or "",
                                             props.artist or props.album_artist or props.subtitle or ""))
            except Exception:
                srcs.append(src_model.Source(app, "", readable=False))
        try:
            cur = mgr.get_current_session()
            cur_app = (cur.source_app_user_model_id or "") if cur is not None else ""
        except Exception:
            cur_app = ""
        if cur_app not in objs:
            cur_app = ""
        for note in self.sel.observe(srcs, time.monotonic()):         # who started playing / disappeared; may end MANUAL_TEMP
            log.info("selection: %s", note)
        return srcs, objs, cur_app

    # ---- snapshot -------------------------------------------------------------------------------
    async def _select(self, wanted: str = "") -> Optional[_Selection]:
        """The session shown on the display AND controlled by the buttons / gestures / volume keys.

        wanted = the app the display says it shows. When given, ONLY that session is returned (None if it is gone), so a command
        can never land on a different session than the one the user looked at. Otherwise the selection model decides:
        the development preference, the temporary manual selection (MANUAL_TEMP, while it lasts), then automatic - the
        most recently started playing session first (sources.Selector). Dead / not-ready sessions are skipped."""
        mgr = await self._get_manager()
        srcs, objs, cur_app = await self._read_sources(mgr)
        if wanted:
            order = [a for a in objs if a.lower() == wanted.lower()]
        else:
            order = self.sel.order(srcs, current=cur_app)
        for app in order:
            session = objs[app]
            try:
                props = await session.try_get_media_properties_async()
                info = session.get_playback_info()
                tl = session.get_timeline_properties()
            except Exception as exc:
                log.debug("skipping session %s: %s", app, exc)
                continue
            return _Selection(session, props, info, tl, srcs)
        return None

    async def _switch_source(self, direction: int, shown_app: str) -> dict[str, Any]:
        mgr = await self._get_manager()
        srcs, objs, cur_app = await self._read_sources(mgr)
        shown = src_model._find(objs.keys(), shown_app) if shown_app else None
        if shown is None:                                # the display is out of date (its session is gone): relative to what we show
            selected = await self._select()
            shown = selected.app if selected else ""
        ring = src_model.ring(srcs, shown)
        target = src_model.step(srcs, shown, direction)
        if target is None:
            return {"ok": True, "action": "source", "changed": False, "count": len(ring), "app": shown,
                    "source": src_model.friendly_name(shown) if shown else "", "mode": self.sel.mode.value}
        mode = self.sel.select(target, srcs, cur_app, time.monotonic())
        idx, count = src_model.position(srcs, target)
        log.info("source -> %s (%d of %d) by swipe %s from %s; mode %s", target, idx, count, "up" if direction >= 0 else "down", shown,
                 mode.value)
        return {"ok": True, "action": "source", "changed": True, "app": target, "source": src_model.friendly_name(target),
                "index": idx, "count": count, "mode": mode.value}

    async def _list_sources(self) -> dict[str, Any]:
        mgr = await self._get_manager()
        selected = await self._select()
        shown = selected.app if selected else ""
        srcs = selected.sources if selected else (await self._read_sources(mgr))[0]
        by_app = {s.app: s for s in srcs}
        rows = []
        for app in src_model.ring(srcs, shown):
            s = by_app.get(app)
            rows.append({"app": app, "name": src_model.friendly_name(app), "state": s.state if s else "", "title": s.title if s else "",
                         "artist": s.artist if s else "", "displayed": app == shown})
        return {"ok": True, "displayed": shown, "mode": self.sel.mode.value, "manual": self.sel.manual, "sources": rows}

    # ---- snapshot -------------------------------------------------------------------------------
    async def _snapshot(self) -> dict[str, Any]:
        selected = await self._select()
        if selected is not None:
            props, info, tl = selected.props, selected.info, selected.timeline

            app = selected.app
            status = info.playback_status
            state = STATE_NAMES.get(status, "unknown")
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
            index, count = src_model.position(selected.sources, app)
            snap = {
                "ok": True,
                "version": 1,
                "has_session": True,
                "app": app,
                "source": src_model.friendly_name(app),     # short name for the on-screen label
                "sources": count,                           # how many sessions a vertical swipe can switch between
                "source_index": index,
                "mode": self._mode_for(app),               # "manual" while this session is a temporary manual selection, else "auto"
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
            snap.update(self._transport_fields(app, info))
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
            self._art_img, self._art_id, self._art_cache = None, "", {}
        self._track_key = ""
        return {"ok": True, "version": 1, "has_session": False, "state": "none", "playing": False,
                "source": "", "sources": 0, "source_index": 0, "mode": self.sel.mode.value,
                "title": "", "artist": "", "album": "", "position_ms": 0, "duration_ms": 0,
                "track_id": "", "art_id": "", "can_play": False, "can_pause": False, "can_next": False,
                "can_prev": False, "can_seek": False, "spotify": False, "shuffle": False, "repeat": 0,
                "can_shuffle": False, "can_repeat": False, "seq": self._seq}

    def _mode_for(self, app: str) -> str:
        return "manual" if self.sel.mode is src_model.Mode.MANUAL_TEMP and self.sel.manual.lower() == app.lower() else "auto"

    def _transport_fields(self, app: str, info) -> dict[str, Any]:
        """Shuffle / repeat state, offered for Spotify only (decided by the session's app identity, never by metadata).
        Windows reports them for Spotify (`is_shuffle_active`, `auto_repeat_mode` = off / list / track) and accepts changes."""
        if not self._is_spotify(app):
            return {"spotify": False, "shuffle": False, "repeat": 0, "can_shuffle": False, "can_repeat": False}
        ctl = info.controls
        return {"spotify": True, "shuffle": bool(info.is_shuffle_active), "repeat": _REPEAT_TO_INT.get(info.auto_repeat_mode, 0),
                "can_shuffle": bool(ctl.is_shuffle_enabled), "can_repeat": bool(ctl.is_repeat_enabled)}

    # ---- artwork --------------------------------------------------------------------------------
    async def _update_artwork(self, track_key: str, props) -> None:
        if track_key != self._track_key:
            self._track_key = track_key
            self._art_tries = 0
            self._art_last_try = 0.0
            with self._art_lock:
                self._art_img, self._art_id, self._art_cache = None, "", {}
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
            image, notes = art_tools.clean(art_tools.decode(raw))      # must be decodable; strips app logos / letterboxing
        except Exception as exc:
            log.debug("thumbnail read failed (try %d): %s", self._art_tries, exc)
            return
        art_id = hashlib.sha1(raw).hexdigest()[:10]
        with self._art_lock:
            self._art_img, self._art_id, self._art_cache = image, art_id, {}
        log.info("artwork loaded: %d bytes -> %dx%d, id %s%s", len(raw), image.width, image.height, art_id,
                 ("; " + "; ".join(notes)) if notes else "")

    # ---- controls -------------------------------------------------------------------------------
    async def _control(self, action: str, position_ms: Optional[int], wanted: str = "") -> dict[str, Any]:
        selected = await self._select(wanted)         # the very session the display is showing
        if selected is None:
            return {"ok": False, "action": action, "error": "that media session is gone" if wanted else "no media session"}
        session, info = selected.session, selected.info
        self.sel.touch(time.monotonic(), selected.app)    # the user is dealing with this session: restarts the manual idle timer
        for _once in (0,):
            try:
                ctl = info.controls
                playing = info.playback_status == PlaybackStatus.PLAYING
                if action.startswith(("shuffle", "repeat")):
                    return await self._spotify_transport(selected, action)
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

    async def _spotify_transport(self, selected: _Selection, action: str) -> dict[str, Any]:
        """Spotify's shuffle toggle and repeat button through Windows' own session controls (no Spotify account / OAuth involved).
        action: shuffle | shuffle-on | shuffle-off | repeat (cycles off -> list -> track -> off) | repeat-off | repeat-list | repeat-track.
        Refused for every other app: the buttons exist for Spotify only. The answer carries the state Windows reports afterwards."""
        app, session, info = selected.app, selected.session, selected.info
        if not self._is_spotify(app):
            return {"ok": False, "action": action, "app": app, "error": "shuffle / repeat are only offered for Spotify"}
        ctl = info.controls
        if action.startswith("shuffle"):
            if not ctl.is_shuffle_enabled:
                return {"ok": False, "action": action, "app": app, "error": "Spotify does not allow shuffle changes right now"}
            want = (not bool(info.is_shuffle_active)) if action == "shuffle" else action == "shuffle-on"
            ok = await session.try_change_shuffle_active_async(want)
            await self._settle(session, lambda i: bool(i.is_shuffle_active) == want)
        else:
            if not ctl.is_repeat_enabled:
                return {"ok": False, "action": action, "app": app, "error": "Spotify does not allow repeat changes right now"}
            if action == "repeat":
                want_mode = (_REPEAT_TO_INT.get(info.auto_repeat_mode, 0) + 1) % 3
            else:
                want_mode = _REPEAT_NAMES.get(action[len("repeat-"):], -1)
            if want_mode not in _INT_TO_REPEAT:
                return {"ok": False, "action": action, "app": app, "error": "unknown repeat mode"}
            ok = await session.try_change_auto_repeat_mode_async(_INT_TO_REPEAT[want_mode])
            await self._settle(session, lambda i: _REPEAT_TO_INT.get(i.auto_repeat_mode, 0) == want_mode)
        fresh = session.get_playback_info()
        log.info("control %s on %s -> %s (shuffle %s, repeat %s)", action, app, "ok" if ok else "refused", bool(fresh.is_shuffle_active),
                 _REPEAT_TO_INT.get(fresh.auto_repeat_mode, 0))
        return {"ok": bool(ok), "action": action, "app": app, **self._transport_fields(app, fresh)}

    @staticmethod
    async def _settle(session, done, timeout: float = 0.6) -> bool:
        """Windows reports a shuffle / repeat change ~100 ms after the app applied it: wait (briefly) so the answer shows the new state."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if done(session.get_playback_info()):
                return True
            await asyncio.sleep(0.05)
        return False

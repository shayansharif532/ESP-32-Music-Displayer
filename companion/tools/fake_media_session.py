"""Test helper: registers a *real* Windows media session (title, artist, artwork, timeline, next/previous/play/pause/seek)
without playing any sound, so the companion and the ESP32 can be tested end to end without Spotify / a browser.

    companion\\.venv\\Scripts\\python.exe companion\\tools\\fake_media_session.py          (Ctrl+C to stop)
        --art-dir DIR      use the images in DIR as the covers of the tracks (see make_test_covers.py)
        --silent-audio     also open a silent audio stream, so per-app volume can be tested (the session is python.exe)
        --paused           start paused (a paused session must not take over a display that follows the playing one)
        (a second fake source needs a second executable name: see tools/start_fakes.ps1, which runs a renamed copy of python.exe)

Needs:  pip install winrt-Windows.Media.Playback winrt-Windows.Media.Core
"""
from __future__ import annotations

import asyncio
import datetime as dt
import io
import sys
import threading
import time

from PIL import Image, ImageDraw
from winrt.windows.media import (
    MediaPlaybackStatus,
    MediaPlaybackType,
    SystemMediaTransportControlsButton as Btn,
    SystemMediaTransportControlsTimelineProperties as Timeline,
)
from winrt.windows.media.playback import MediaPlayer
from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream, RandomAccessStreamReference

TRACKS = [
    ("Harbor Lights", "Example Band", "Late Shift", 214, (120, 40, 200), (1, 1)),
    ("A Very Long Track Title That Has To Scroll Across The Screen Several Times", "Artist With A Long Name feat. Someone", "Long Names", 187, (230, 120, 20), (1, 1)),
    ("Wide 16:9 thumbnail test (like a YouTube video)", "Some Channel", "YouTube", 95, (20, 140, 200), (16, 9)),
]


def load_tracks_from_dir(path: str) -> None:
    import os
    global TRACKS, ART_FILES
    files = sorted(f for f in os.listdir(path) if f.lower().endswith((".jpg", ".jpeg", ".png")))
    TRACKS = [(os.path.splitext(f)[0], "Test Artist", "Test Covers", 200, (0, 0, 0), (1, 1)) for f in files]
    ART_FILES = [os.path.join(path, f) for f in files]


ART_FILES: list = []


def make_cover(idx: int, color, aspect) -> bytes:
    if ART_FILES:
        with open(ART_FILES[idx], "rb") as f:
            return f.read()
    w = 640 * aspect[0] // max(aspect) if aspect[0] >= aspect[1] else 640
    h = int(w * aspect[1] / aspect[0])
    img = Image.new("RGB", (w, h), color)
    d = ImageDraw.Draw(img)
    for i in range(0, h, 8):                                  # simple gradient bands
        shade = int(120 * i / h)
        d.rectangle([0, i, w, i + 8], fill=tuple(max(0, c - shade) for c in color))
    d.ellipse([w * 0.2, h * 0.2, w * 0.8, h * 0.8], outline=(255, 255, 255), width=10)
    d.text((w // 2 - 10, h // 2 - 10), str(idx + 1), fill=(255, 255, 255))
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


class Fake:
    def __init__(self, paused: bool = False) -> None:
        self.player = MediaPlayer()
        self.player.command_manager.is_enabled = False
        self.smtc = self.player.system_media_transport_controls
        self.lock = threading.Lock()
        self.idx = 0
        self.playing = not paused
        self.pos = 0.0
        self.t_ref = time.monotonic()
        s = self.smtc
        s.is_enabled = True
        s.is_play_enabled = s.is_pause_enabled = s.is_next_enabled = s.is_previous_enabled = True
        for name in ('is_playback_position_enabled',):
            if hasattr(s, name):
                setattr(s, name, True)
        s.add_button_pressed(self.on_button)
        if hasattr(s, 'add_shuffle_enabled_change_requested'):            # behave like Spotify: shuffle on/off and repeat off / track / list
            s.add_shuffle_enabled_change_requested(self.on_shuffle)
            s.add_auto_repeat_mode_change_requested(self.on_repeat)
            s.shuffle_enabled = False
        if hasattr(s, 'add_playback_position_change_requested'):
            s.add_playback_position_change_requested(self.on_seek)
        self.publish_track()

    def _now_pos(self) -> float:
        return self.pos + ((time.monotonic() - self.t_ref) if self.playing else 0.0)

    def on_button(self, sender, args) -> None:
        with self.lock:
            b = args.button
            if b == Btn.NEXT:
                self.idx = (self.idx + 1) % len(TRACKS)
                self.pos, self.playing = 0.0, True
                self.publish_track()
            elif b == Btn.PREVIOUS:
                if self._now_pos() > 3:
                    self.pos = 0.0
                else:
                    self.idx = (self.idx - 1) % len(TRACKS)
                    self.pos = 0.0
                    self.publish_track()
            elif b == Btn.PLAY:
                self.pos, self.playing = self._now_pos(), True
            elif b == Btn.PAUSE:
                self.pos, self.playing = self._now_pos(), False
            self.t_ref = time.monotonic()
            print("button", b, "-> track", self.idx, "playing", self.playing, flush=True)
            self.publish_state()

    def on_shuffle(self, sender, args) -> None:
        self.smtc.shuffle_enabled = bool(args.requested_shuffle_enabled)
        print("shuffle ->", self.smtc.shuffle_enabled, flush=True)

    def on_repeat(self, sender, args) -> None:
        self.smtc.auto_repeat_mode = args.requested_auto_repeat_mode
        print("repeat ->", self.smtc.auto_repeat_mode, flush=True)

    def on_seek(self, sender, args) -> None:
        with self.lock:
            self.pos = args.requested_playback_position.total_seconds()
            self.t_ref = time.monotonic()
            print("seek ->", round(self.pos, 1), flush=True)
            self.publish_state()

    def publish_track(self) -> None:
        title, artist, album, dur, color, aspect = TRACKS[self.idx]
        u = self.smtc.display_updater
        u.type = MediaPlaybackType.MUSIC
        u.music_properties.title = title
        u.music_properties.artist = artist
        u.music_properties.album_title = album
        stream = InMemoryRandomAccessStream()
        w = DataWriter(stream)
        w.write_bytes(make_cover(self.idx, color, aspect))
        asyncio.run(self._store(w))
        stream.seek(0)
        u.thumbnail = RandomAccessStreamReference.create_from_stream(stream)
        u.update()
        self.publish_state()

    @staticmethod
    async def _store(writer) -> None:
        await writer.store_async()
        await writer.flush_async()
        writer.detach_stream()

    def publish_state(self) -> None:
        dur = TRACKS[self.idx][3]
        tl = Timeline()
        tl.start_time = dt.timedelta(0)
        tl.min_seek_time = dt.timedelta(0)
        tl.max_seek_time = dt.timedelta(seconds=dur)
        tl.end_time = dt.timedelta(seconds=dur)
        tl.position = dt.timedelta(seconds=self.pos)
        self.smtc.update_timeline_properties(tl)
        self.smtc.playback_status = MediaPlaybackStatus.PLAYING if self.playing else MediaPlaybackStatus.PAUSED


def start_silent_audio() -> None:
    """Loops a silent WAV so this process owns a real (but inaudible) Windows audio session: lets the per-app volume
    control be tested end to end without touching Spotify / the browser."""
    import os
    import tempfile
    import wave
    import winsound
    path = os.path.join(tempfile.gettempdir(), "desk-media-silence.wav")       # winsound cannot loop a WAV from memory
    with wave.open(path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\x00\x00" * 8000)
    winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_LOOP | winsound.SND_ASYNC)
    print("silent audio stream started (python.exe now has an audio session)", flush=True)


def main() -> None:
    args = sys.argv[1:]
    paused = "--paused" in args
    if paused:
        args.remove("--paused")
    if "--silent-audio" in args:
        args.remove("--silent-audio")
        start_silent_audio()
    if len(args) > 1 and args[0] == "--art-dir":
        load_tracks_from_dir(args[1])
    fake = Fake(paused)
    print("fake media session is up (silent). Ctrl+C to stop.", flush=True)
    try:
        while True:
            time.sleep(1)
            with fake.lock:                                  # roll over at the end of a track
                if fake.playing and fake._now_pos() >= TRACKS[fake.idx][3]:
                    fake.idx = (fake.idx + 1) % len(TRACKS)
                    fake.pos, fake.t_ref = 0.0, time.monotonic()
                    fake.publish_track()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())

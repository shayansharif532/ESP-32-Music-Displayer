#!/usr/bin/env python3
"""Board-in-the-loop test of the idle screen (needs the board on USB, flashed with a -DAPP_DEV_TOOLS=1 build of this checkout).

It runs its OWN companion in this process, on port 8765 with the discovery beacon, so the board talks to it (stop the normal companion
first). That companion sees ONLY two silent fake media sessions - A = python.exe ("Spotify") and B = deskfakeb.exe ("YouTube") - never the
owner's real apps. Settings are changed through the same IdleStore the tray settings window uses; the board's behaviour is read from
its serial log (screen transitions, settings sync, health) and, where it matters, from real screenshots of its LVGL screen.

    set DESK_VENV=C:\\path\\to\\companion\\.venv          (only when this is a git worktree without its own venv)
    companion\\.venv\\Scripts\\python.exe tools\\idle_test.py [--port COM5] [--soak-cycles 30] [--skip-soak]

Checks (numbers = the user's list): 1 idle when nothing plays, 2 a starting session leaves idle at once, 3 A pauses / B plays -> B,
4 everything pauses -> idle after the delay (and not during a short gap), 5 the picture survives a board reboot without being resent,
6 settings survive a companion restart, 7 Fill / Fit / Blur Fill, 8 the clock, 9 broken / missing picture falls back, 10 a repeated
idle <-> playback soak with heap / stack / reset accounting.
The "carousel" phase checks the idle screen as an item of the vertical source ring (ADR-008): reachable while a session plays, no
playback command, auto-follow leaves it, swipes up / down from it, horizontal swipes and volume keys do nothing on it.
"""
import argparse
import datetime as dt
import io
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "companion"))
sys.path.insert(0, os.path.join(ROOT, "companion", "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import fakes  # noqa: E402
import screenshot as shot  # noqa: E402
import serial_capture as sc  # noqa: E402
from PIL import Image, ImageChops, ImageDraw, ImageFont  # noqa: E402

from desk_media_companion import idle as idle_mod  # noqa: E402
from desk_media_companion import volume as app_volume  # noqa: E402
from desk_media_companion.discovery import Beacon  # noqa: E402
from desk_media_companion.media import MediaMonitor  # noqa: E402
from desk_media_companion.pairing import Pairing, PairingStore  # noqa: E402
from desk_media_companion.server import make_server  # noqa: E402

PORT, DISCOVERY = 8765, 8766
A, B = "python.exe", "deskfakeb.exe"
LOGS = os.path.join(ROOT, "logs")
TTF = os.path.join(ROOT, "firmware", "managed_components", "lvgl__lvgl", "scripts", "built_in_font", "Montserrat-Medium.ttf")
failures: list[str] = []
results: list[dict] = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""), flush=True)
    results.append({"check": name, "ok": bool(ok), "detail": detail})
    if not ok:
        failures.append(name)
    return ok


def wait_for(pred, timeout=8.0, step=0.2):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(step)
    return False


# ---- the board's serial log -------------------------------------------------------------------------------------------------

class Board:
    def __init__(self, port: str) -> None:
        self.ser = sc.open_port(port, reset=False)
        self.lines: list[tuple[float, str]] = []
        self.lock = threading.Lock()
        self.stop = False
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self) -> None:
        buf = b""
        while not self.stop:
            try:
                chunk = self.ser.read(8192)
            except Exception:
                time.sleep(0.2)
                continue
            if not chunk:
                continue
            buf += chunk
            *done, buf = buf.split(b"\n")
            now = time.time()
            with self.lock:
                for raw in done:
                    self.lines.append((now, raw.strip().decode("ascii", errors="ignore")))

    def mark(self) -> int:
        with self.lock:
            return len(self.lines)

    def since(self, mark: int) -> list[tuple[float, str]]:
        with self.lock:
            return list(self.lines[mark:])

    def find(self, pattern: str, mark: int = 0):
        rx = re.compile(pattern)
        for t, ln in self.since(mark):
            if rx.search(ln):
                return t, ln
        return None

    def count(self, pattern: str, mark: int = 0) -> int:
        rx = re.compile(pattern)
        return sum(1 for _t, ln in self.since(mark) if rx.search(ln))

    def wait(self, pattern: str, mark: int, timeout: float):
        end = time.time() + timeout
        while time.time() < end:
            hit = self.find(pattern, mark)
            if hit:
                return hit
            time.sleep(0.1)
        return None

    def touches(self) -> int:
        """Real finger presses the board has counted so far (virtual-finger actions are not counted): a person using the board while
        a test runs shows up here, and explains an odd result."""
        last = 0
        for _t, ln in self.since(0):
            m = re.search(r"health: up=\d+s touches=(\d+)", ln)
            if m:
                last = int(m.group(1))
        return last

    def reset(self) -> None:
        sc.set_dtr(self.ser, False)
        sc.set_rts(self.ser, False)
        time.sleep(0.1)
        sc.set_rts(self.ser, True)
        time.sleep(0.1)
        sc.set_rts(self.ser, False)
        time.sleep(0.05)
        sc.set_dtr(self.ser, True)

    def screenshot(self, monitor: MediaMonitor, name: str, timeout: float = 60.0) -> Image.Image | None:
        """Asks the board (through the companion's one-shot debug request) for a picture of its LVGL screen."""
        out = os.path.join(LOGS, name)
        for _attempt in range(3):
            mark = self.mark()
            monitor.request_debug("screenshot")
            begin = self.wait(r"SHOT_BEGIN", mark, 12.0)
            if not begin:
                continue
            end = self.wait(r"SHOT_END", mark, timeout)
            if not end:
                continue
            lines = [ln for _t, ln in self.since(mark)]
            if shot.assemble(lines, out) == 0:
                return Image.open(out).convert("RGB")
        return None

    def screen_events(self, mark: int) -> list[tuple[float, str]]:
        return [(t, "idle" if "-> idle" in ln else "player") for t, ln in self.since(mark) if re.search(r"app: screen -> (idle|player)", ln)]


# ---- the companion under test ----------------------------------------------------------------------------------------------

class Companion:
    def __init__(self, data_dir: str, pairing_dir: str | None = None) -> None:
        self.data_dir = data_dir
        self.monitor = MediaMonitor()
        self.monitor.only_apps = {A, B}
        self.monitor.spotify_ids = {A}
        self.monitor.start()
        self.pairing = Pairing(PairingStore(pairing_dir))              # the DEFAULT store (pairing_dir None): the board stays paired with the normal companion's key
        self.calls: list[tuple] = []                                   # every media command / volume request that reached the companion
        real_control, real_volume = self.monitor.control, self.monitor.volume

        def control(*a, **k):
            self.calls.append(("control",) + a)
            return real_control(*a, **k)

        def volume(*a, **k):
            self.calls.append(("volume",) + a)
            return real_volume(*a, **k)

        self.monitor.control, self.monitor.volume = control, volume
        self._last_act = 0.0
        self.counts = {}
        self.store = None
        self.server = None
        self.beacon = None
        self.start_http()

    def start_http(self) -> None:
        self.counts = {"idle": 0, "image": 0}                          # the board's downloads since this (re)start
        self.store = idle_mod.IdleStore(self.data_dir)
        real_config, real_image = self.store.config, self.store.image

        def config():
            self.counts["idle"] += 1
            return real_config()

        def image():
            self.counts["image"] += 1
            return real_image()

        self.store.config, self.store.image = config, image          # count the board's downloads
        self.server = make_server(self.monitor, PORT, dev_tools=True, idle=self.store, pairing=self.pairing)
        orig_get_request = self.server.get_request
        self.conns = []

        def tracking_get_request():                                   # remember the board's kept-alive connections ...
            sock, addr = orig_get_request()
            self.conns.append(sock)
            return sock, addr

        self.server.get_request = tracking_get_request
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        if self.beacon is None:
            self.beacon = Beacon(PORT, DISCOVERY, self.pairing)
            self.beacon.start()

    def stop_http(self) -> None:
        """Stops the server AND drops its open connections, as the exit of a real companion process would (a handler thread of the old
        server would otherwise keep answering the board's kept-alive connection with its old state)."""
        self.server.shutdown()
        self.server.server_close()
        for c in self.conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                c.close()
            except OSError:
                pass
        self.conns.clear()

    def act(self, action: str, gap: float = 2.8) -> None:
        """Asks the board to perform a scripted action (virtual finger / key). The board ignores a request that arrives while the
        previous action is still running, so requests are spaced out."""
        wait = self._last_act + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        self.monitor.request_debug(action)
        self._last_act = time.time()

    def now(self) -> dict:
        return self.monitor.now_playing()

    def states(self) -> dict:
        return {s["app"]: s["state"] for s in self.monitor.list_sources().get("sources", []) if not s.get("synthetic")}

    def set_state(self, app: str, playing: bool) -> None:
        want = "playing" if playing else "paused"
        if self.states().get(app) != want:
            self.monitor.control("play" if playing else "pause", None, app)
            wait_for(lambda: self.states().get(app) == want, 4.0)


# ---- picture helpers ---------------------------------------------------------------------------------------------------------

def test_picture() -> bytes:
    """800x400: left half red, right half blue (so fill / fit / blur can be told apart on the screen)."""
    img = Image.new("RGB", (800, 400), (30, 60, 220))
    img.paste((220, 40, 40), (0, 0, 400, 400))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def lum(p) -> int:
    return (p[0] + p[1] + p[2]) // 3


def clock_mask(img: Image.Image, region=(20, 60, 460, 300)) -> Image.Image:
    return img.crop(region).convert("L").point(lambda v: 255 if v > 215 else 0)


def clock_matches(img: Image.Image, text: str, twelve: bool = False) -> tuple[bool, float]:
    """The big digits on the screen against the same text rendered with the same typeface: bounding boxes and overlap.
    (In 12 h mode the AM / PM label right of the digits is left out: the clock label is centred, so its right edge is known.)"""
    region = (20, 60, 460, 300)
    if twelve:
        label_w = sum(86 if c.isdigit() else 29 for c in text)
        region = (20, 60, 240 + label_w // 2 + 3, 300)
    got = clock_mask(img, region)
    box = got.getbbox()
    if not box:
        return False, 0.0
    font = ImageFont.truetype(TTF, 128)
    digit_adv = max(font.getlength(c) for c in "0123456789")              # the display's clock font has tabular digits
    ref = Image.new("L", (600, 220), 0)
    d, x = ImageDraw.Draw(ref), 20.0
    for ch in text:
        adv = digit_adv if ch.isdigit() else font.getlength(ch)
        d.text((x + ((adv - font.getlength(ch)) / 2 if ch.isdigit() else 0), 170), ch, font=font, fill=255, anchor="ls")
        x += adv
    ref = ref.point(lambda v: 255 if v > 100 else 0)
    rbox = ref.getbbox()
    if not rbox:
        return False, 0.0
    g, r = got.crop(box), ref.crop(rbox)
    if abs(g.width - r.width) > 6 or abs(g.height - r.height) > 6:
        return False, 0.0
    r = r.resize(g.size)
    inter = sum(ImageChops.multiply(g, r).point(lambda v: 255 if v > 0 else 0).histogram()[255:])
    union = sum(ImageChops.lighter(g, r).histogram()[255:])
    iou = inter / union if union else 0.0
    return iou > 0.80, round(iou, 3)


def clock_ok(img: Image.Image, twelve: bool = False) -> tuple[bool, str]:
    """Does the screen show the PC's current time (this minute or the one before, in case the minute just changed)?"""
    now = dt.datetime.now()
    tried = []
    for back in (0, 1):
        t = now - dt.timedelta(minutes=back)
        text = (t.strftime("%I:%M").lstrip("0") if twelve else t.strftime("%H:%M"))
        ok, iou = clock_matches(img, text, twelve)
        tried.append(f"{text}:{iou}")
        if ok:
            return True, " ".join(tried)
    return False, " ".join(tried)


# ---- tests ---------------------------------------------------------------------------------------------------------------------

def phase_idle_basics(board: Board, comp: Companion, procs: dict) -> None:
    print("\n== 1. nothing playing -> idle screen")
    mark = board.mark()
    board.reset()
    hit = board.wait(r"app: screen -> idle", mark, 45.0)
    check("1  after boot with the PC connected and nothing playing the idle screen appears", hit is not None)
    time.sleep(3.0)
    img = board.screenshot(comp.monitor, "idle-t1-default.png")
    check("1  a screenshot of it shows a large bright clock on the default (Dark) theme", img is not None and clock_mask(img).getbbox() is not None)
    if img is not None:
        ok, detail = clock_ok(img)
        check("8  the clock shows the PC's current time", ok, detail)
        check("1  the generic default is the Dark gradient (not photo, no custom text)", 20 < img.getpixel((5, 5))[2] < 80 and img.getpixel((5, 470))[2] < 30,
              str((img.getpixel((5, 5)), img.getpixel((5, 470)))))

    print("\n== 2. a session starts playing -> the player at once")
    procs[A] = fakes.start_fake(A, paused=False, log_path=os.path.join(LOGS, "idle-fake-A.log"))
    check("fake A is a media session", wait_for(lambda: A in comp.states(), 20.0, 0.5))
    mark = board.mark()
    t0 = time.time()
    comp.set_state(A, True)
    hit = board.wait(r"app: screen -> player", mark, 8.0)
    check("2  starting A leaves the idle screen for the player", hit is not None, f"{hit[0] - t0:.1f} s" if hit else "")
    check("2  ... within 3 s (one poll)", hit is not None and hit[0] - t0 <= 3.0)
    check("2  ... showing A", comp.now().get("app") == A)

    print("\n== 3. A pauses, B plays -> B (no detour through idle)")
    procs[B] = fakes.start_fake(B, paused=True, log_path=os.path.join(LOGS, "idle-fake-B.log"))
    check("fake B is a media session", wait_for(lambda: B in comp.states(), 20.0, 0.5))
    mark = board.mark()
    comp.set_state(B, True)
    comp.set_state(A, False)
    time.sleep(comp_delay(comp) + 3.0)
    events = board.screen_events(mark)
    check("3  the display follows B and never went to the idle screen", comp.now().get("app") == B and not any(e == "idle" for _t, e in events), str([e for _t, e in events]))
    check("3  the board reported B", board.find(r"now playing \[deskfakeb\.exe\]", mark) is not None)

    print("\n== 4. everything pauses -> idle after the delay, but not during a short gap")
    mark = board.mark()
    comp.set_state(B, False)
    t0 = time.time()
    hit = board.wait(r"app: screen -> idle", mark, comp_delay(comp) + 6.0)
    dt_idle = (hit[0] - t0) if hit else None
    check("4  the idle screen appears after the pause", hit is not None, f"{dt_idle:.1f} s" if dt_idle else "")
    check("4  ... not before the delay minus a poll", dt_idle is not None and dt_idle >= comp_delay(comp) - 1.5, f"delay {comp_delay(comp)} s")
    check("4  ... and not much after it", dt_idle is not None and dt_idle <= comp_delay(comp) + 2.5)
    comp.set_state(A, True)
    board.wait(r"app: screen -> player", mark, 6.0)
    mark = board.mark()
    comp.set_state(A, False)                                              # a 2 s gap between two tracks
    time.sleep(2.0)
    comp.set_state(A, True)
    time.sleep(comp_delay(comp) + 2.0)
    check("4  a 2 s gap does not flash the idle screen", not any(e == "idle" for _t, e in board.screen_events(mark)), str(board.screen_events(mark)))
    mark = board.mark()
    comp.set_state(A, False)
    board.wait(r"app: screen -> idle", mark, comp_delay(comp) + 6.0)


def comp_delay(comp: Companion) -> int:
    return comp.store.settings().delay_s


def apply_and_wait(board: Board, comp: Companion, what: str, timeout: float = 30.0, **changes) -> bool:
    mark = board.mark()
    comp.store.update(**changes)
    v = comp.store.version
    ok = board.wait(rf"idle settings version {v} applied", mark, timeout) is not None
    return check(f"   board applied the new settings ({what})", ok)


def phase_themes(board: Board, comp: Companion) -> None:
    print("\n== themes, accent, text, 12 h")
    for theme in ("minimal", "neon", "dark"):
        apply_and_wait(board, comp, theme, theme=theme, text="Welcome back", accent="#FF4FA3" if theme == "neon" else "#4DA3FF")
        time.sleep(1.5)
        img = board.screenshot(comp.monitor, f"idle-theme-{theme}.png")
        check(f"   {theme}: screenshot received", img is not None)
        if img is not None:
            check(f"   {theme}: clock digits are there", clock_mask(img).getbbox() is not None)
            if theme == "neon":
                px = img.getpixel((240, 470))
                check("   neon: an accent-coloured glow along the bottom edge", px[0] > px[1] + 20, str(px))
            if theme == "minimal":
                check("   minimal: near-black background", lum(img.getpixel((240, 20))) < 15, str(img.getpixel((240, 20))))
    print("\n== 8. 12-hour clock")
    apply_and_wait(board, comp, "12 h, no text", clock_24h=False, text="")
    time.sleep(1.5)
    img = board.screenshot(comp.monitor, "idle-12h.png")
    if img is not None:
        ok, detail = clock_ok(img, twelve=True)
        check("8  12-hour time shows the right digits", ok, detail)
        right = img.crop((400, 150, 470, 250)).convert("L").point(lambda v: 255 if v > 150 else 0).getbbox()
        check("8  with an AM / PM mark beside it", right is not None)
    apply_and_wait(board, comp, "24 h again", clock_24h=True)
    apply_and_wait(board, comp, "no clock, big text", show_clock=False, text="Hello there")
    time.sleep(1.5)
    img = board.screenshot(comp.monitor, "idle-noclock.png")
    if img is not None:
        check("8  clock hidden: no big digits", clock_mask(img, (20, 60, 460, 150)).getbbox() is None)
    apply_and_wait(board, comp, "clock back", show_clock=True, text="")


def phase_minute_rollover(board: Board, comp: Companion) -> None:
    print("\n== 8. the clock moves on at the minute change")
    now = dt.datetime.now()
    wait_s = 60 - now.second + 3
    img1 = board.screenshot(comp.monitor, "idle-clock-1.png")
    t1 = dt.datetime.now()
    time.sleep(max(0, (60 - dt.datetime.now().second) + 4))
    img2 = board.screenshot(comp.monitor, "idle-clock-2.png")
    t2 = dt.datetime.now()
    if img1 is not None and img2 is not None:
        ok1 = clock_matches(img1, t1.strftime("%H:%M"))[0] or clock_matches(img1, (t1 - dt.timedelta(minutes=1)).strftime("%H:%M"))[0]
        ok2 = clock_matches(img2, t2.strftime("%H:%M"))[0] or clock_matches(img2, (t2 - dt.timedelta(minutes=1)).strftime("%H:%M"))[0]
        check("8  before the minute change the right time is shown", ok1)
        check("8  after it the new minute is shown", ok2 and t2.strftime("%H:%M") != t1.strftime("%H:%M"), f"{t1:%H:%M} -> {t2:%H:%M}")
    else:
        check("8  screenshots for the minute test", False)


def phase_modes(board: Board, comp: Companion) -> None:
    print("\n== 7. Fill / Fit / Blur Fill with a red | blue 2:1 picture")
    mark = board.mark()
    comp.store.update(theme="photo")
    comp.store.set_picture(test_picture(), "test-wide.png", "fill")
    v = comp.store.version
    check("   the board receives the picture", board.wait(rf"idle settings version {v} applied", mark, 40.0) is not None)
    time.sleep(2.0)
    shots = {}
    for mode in ("fill", "fit", "blur"):
        if mode != "fill":
            apply_and_wait(board, comp, f"mode {mode}", bg_mode=mode)
            time.sleep(1.5)
        shots[mode] = board.screenshot(comp.monitor, f"idle-mode-{mode}.png")
        check(f"7  {mode}: screenshot received", shots[mode] is not None)
    f, t, b = shots.get("fill"), shots.get("fit"), shots.get("blur")
    if f is not None:
        tl, br = f.getpixel((12, 12)), f.getpixel((468, 468))
        check("7  Fill: the picture covers the whole screen (red top-left, blue bottom-right, no bars)", tl[0] > tl[2] + 40 and br[2] > br[0] + 40, f"{tl} {br}")
    if t is not None:
        top, mid_l, mid_r = t.getpixel((240, 20)), t.getpixel((60, 320)), t.getpixel((420, 320))
        check("7  Fit: dark bars above / below the whole picture", lum(top) < 25 and lum(t.getpixel((240, 460))) < 25, f"{top}")
        check("7  Fit: the picture itself reaches both side edges (red | blue)", mid_l[0] > mid_l[2] + 40 and mid_r[2] > mid_r[0] + 40, f"{mid_l} {mid_r}")
    if b is not None:
        corner, fg_l, fg_r = b.getpixel((12, 12)), b.getpixel((120, 320)), b.getpixel((360, 320))     # below the clock digits
        check("7  Blur Fill: the screen corners carry the picture's blurred colour (not black bars)", sum(corner) > 45 and corner[0] > corner[2], str(corner))
        check("7  Blur Fill: the sharp picture sits in the foreground (bright red | blue)", fg_l[0] > 110 and fg_r[2] > 110, f"{fg_l} {fg_r}")
        check("7  Blur Fill: the foreground is brighter than the blurred surround", lum(fg_l) > lum(corner) + 20)


def phase_persistence(board: Board, comp: Companion) -> None:
    print("\n== 5. the picture survives a board reboot, without being resent")
    apply_and_wait(board, comp, "mode fill again", bg_mode="fill")
    v = comp.store.version
    before = dict(comp.counts)
    mark = board.mark()
    board.reset()
    hit = board.wait(r"idle: stored settings loaded: version (\d+), theme 3, picture", mark, 30.0)
    check("5  after the reboot the board loaded its stored settings + picture from flash", hit is not None and f"version {v}," in hit[1], hit[1][-70:] if hit else "")
    check("5  and went to the idle screen again", board.wait(r"app: screen -> idle", mark, 40.0) is not None)
    time.sleep(4.0)
    check("5  without downloading the settings or the picture again", comp.counts["image"] == before["image"] and comp.counts["idle"] == before["idle"], f"{before} -> {comp.counts}")
    img = board.screenshot(comp.monitor, "idle-after-reboot.png")
    if img is not None:
        tl, br = img.getpixel((12, 12)), img.getpixel((468, 468))
        check("5  the picture is on screen after the reboot", tl[0] > tl[2] + 40 and br[2] > br[0] + 40, f"{tl} {br}")

    print("\n== 6. the companion restarts: settings stay, nothing is resent")
    before_settings = comp.store.settings()
    mark = board.mark()
    comp.stop_http()
    time.sleep(1.0)
    comp.start_http()                                                    # a fresh IdleStore from the same folder, a new server
    check("6  the restarted companion has the same settings and version", comp.store.settings() == before_settings and comp.store.version == v)
    check("6  ... and still the picture", comp.store.has_image())
    time.sleep(10.0)
    check("6  nothing had to be resent to the board", comp.counts["image"] == 0 and comp.counts["idle"] == 0, str(comp.counts))
    check("6  the board kept running (no reset) and still shows the picture", board.count(r"ESP-ROM|rst:0x|Guru Meditation", mark) == 0)
    img = board.screenshot(comp.monitor, "idle-after-companion-restart.png")
    if img is not None:
        tl, br = img.getpixel((12, 12)), img.getpixel((468, 468))
        check("6  ... the picture is still on screen", tl[0] > tl[2] + 40 and br[2] > br[0] + 40, f"{tl} {br}")


def phase_fallbacks(board: Board, comp: Companion) -> None:
    print("\n== 9. missing / broken pictures fall back to the built-in theme")
    comp.store.clear_picture()
    comp.store.update(theme="photo")
    v = comp.store.version
    mark = board.mark()
    check("9  the board takes 'Photo theme without a picture'", board.wait(rf"idle settings version {v} applied", mark, 30.0) is not None)
    time.sleep(1.5)
    img = board.screenshot(comp.monitor, "idle-fallback-nopicture.png")
    if img is not None:
        top = img.getpixel((5, 5))
        check("9  Photo theme with no picture shows the Dark theme (never a black / broken screen)", 15 < top[2] < 90 and clock_mask(img).getbbox() is not None, str(top))
    # a picture the PC believes is fine but the board cannot decode: bytes that are not a JPEG with a correct length / CRC
    garbage = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 20
    mark = board.mark()
    with comp.store._lock:
        comp.store._image, comp.store._image_crc = garbage, idle_mod.crc32(garbage)
        comp.store._image_v += 1
        comp.store._version += 1
        comp.store._settings.image_name = "corrupt.jpg"
        comp.store._save()
    v = comp.store.version
    check("9  the board survives a corrupt picture (applies the settings, logs the failure)", board.wait(rf"idle settings version {v} applied", mark, 40.0) is not None)
    check("9  ... it says the picture cannot be decoded", board.find(r"cannot be decoded|decode failed", mark) is not None)
    time.sleep(1.5)
    img = board.screenshot(comp.monitor, "idle-fallback-corrupt.png")
    if img is not None:
        check("9  ... and shows the Dark theme instead", 15 < img.getpixel((5, 5))[2] < 90 and clock_mask(img).getbbox() is not None, str(img.getpixel((5, 5))))
    mark = board.mark()
    comp.store.set_picture(test_picture(), "ok-again.png", "fill")
    v = comp.store.version
    check("9  a good picture afterwards works again", board.wait(rf"idle settings version {v} applied", mark, 40.0) is not None)
    try:
        comp.store.set_picture(os.urandom(5000), "junk.png")
        check("9  the PC rejects a malformed picture", False)
    except idle_mod.ImageError:
        check("9  the PC rejects a malformed picture and keeps the old one", comp.store.version == v and comp.store.has_image())
    comp.store.update(theme="dark")
    board.wait(rf"idle settings version {comp.store.version} applied", mark, 30.0)


def phase_carousel(board: Board, comp: Companion, procs: dict) -> None:
    print("\n== the idle screen as an item of the vertical source ring")
    phase_mark = board.mark()
    touches0 = board.touches()
    comp.store.update(theme="dark", delay_s=4, text="", show_clock=True)
    for app in (A, B):
        if app not in comp.states():
            procs[app] = fakes.start_fake(app, paused=True, log_path=os.path.join(LOGS, f"idle-fake-{app}.log"))
    wait_for(lambda: A in comp.states() and B in comp.states(), 25.0, 0.5)
    comp.set_state(B, False)
    comp.set_state(A, True)
    mark = board.mark()
    board.wait(r"app: screen -> player", mark, 8.0) or time.sleep(1.5)
    time.sleep(2.0)
    rows = [r["app"] for r in comp.monitor.list_sources().get("sources", [])]
    check("1  Spotify-like + YouTube-like + Idle all appear in the vertical ring", rows == [B, A, "idle"], str(rows))
    check("   the board shows A (player), 2 of 3", comp.now().get("app") == A and comp.now().get("source_index") == 2 and comp.now().get("sources") == 3)

    # ---- 2./3. reach Idle while A plays; A is not paused
    calls_before = len(comp.calls)
    mark = board.mark()
    t0 = time.time()
    comp.act("swipe_up_shot")                         # a real finger swipe on the player; the board photographs the result
    hit = board.wait(r"app: screen -> idle", mark, 8.0)
    check("2  a vertical swipe on the player reaches the idle screen while A plays", hit is not None, f"{hit[0] - t0:.1f} s" if hit else "")
    check("   the companion records the idle selection (manual, temporary)", comp.now().get("idle_selected") is True and comp.now().get("mode") == "manual")
    img = None
    for attempt in range(3):
        end = board.wait(r"SHOT_END", mark, 40.0)
        if end:
            lines = [ln for _t, ln in board.since(mark)]
            out = os.path.join(LOGS, "idle-carousel-pill.png")
            if shot.assemble(lines, out) == 0:
                img = Image.open(out).convert("RGB")
                break
        if attempt < 2:                                                 # not captured: go back to the player and swipe onto Idle again
            comp.act("swipe_down")
            wait_for(lambda: comp.now().get("idle_selected") is False, 6.0)
            mark = board.mark()
            comp.act("swipe_up_shot")
            wait_for(lambda: comp.now().get("idle_selected") is True, 6.0)
    if img is not None:
        pill = img.crop((130, 28, 350, 72)).convert("L").point(lambda v: 255 if v > 215 else 0).getbbox()
        check("   the 'Idle Screen' pill is drawn over the idle screen (subtle source feedback)", pill is not None, str(pill))
        check("   the idle screen (big clock) is what is shown", clock_mask(img).getbbox() is not None)
    else:
        check("   screenshot of the pill", False)
    time.sleep(2.0)
    check("3  selecting Idle did not pause A", comp.states().get(A) == "playing", str(comp.states()))
    check("   ... and sent no media command", not any(c[0] == "control" for c in comp.calls[calls_before:]), str(comp.calls[calls_before:]))
    track = comp.now().get("track_id")

    # ---- 6. horizontal swipes do nothing on Idle
    print("-- horizontal gestures and volume keys on Idle")
    calls_before = len(comp.calls)
    mark = board.mark()
    for action in ("swipe_left", "swipe_right", "swipe_left"):
        comp.act(action)
        time.sleep(0.5)
    check("6  left / right swipes on Idle send no next / previous", not any(c[0] == "control" for c in comp.calls[calls_before:]), str(comp.calls[calls_before:]))
    check("   ... the track did not change and Idle is still selected", comp.now().get("track_id") == track and comp.now().get("idle_selected") is True)
    check("   ... and the display stayed on the idle screen", not any(e == "player" for _t, e in board.screen_events(mark)), str(board.screen_events(mark)))

    # ---- 7. volume keys on Idle
    calls_before = len(comp.calls)
    vol_before = {a: app_volume.get_volume(a).get("volume") for a in (A,)}
    for action in ("key_up", "key_down", "key_up", "key_down"):
        comp.act(action)
        time.sleep(0.5)
    check("7  the side volume keys on Idle send no volume request at all", not any(c[0] == "volume" for c in comp.calls[calls_before:]), str(comp.calls[calls_before:]))
    check("   ... and no app's volume moved", {a: app_volume.get_volume(a).get("volume") for a in (A,)} == vol_before, str(vol_before))

    # ---- 5. swipes from Idle reach the neighbours
    print("-- swipes up / down from Idle")
    mark = board.mark()
    comp.act("swipe_down")
    check("5  swipe down from Idle -> the previous item (A, 2 of 3), player back", wait_for(lambda: comp.now().get("app") == A and comp.now().get("idle_selected") is False, 6.0) and board.wait(r"app: screen -> player", mark, 4.0) is not None, str((comp.now().get("app"), comp.now().get("idle_selected"))))
    # the side keys are not disabled in general: on the player they still change the displayed session's own volume
    vol0 = app_volume.get_volume(A).get("volume")
    comp.act("key_down")
    time.sleep(1.2)
    vol1 = app_volume.get_volume(A).get("volume")
    comp.act("key_up")
    time.sleep(1.2)
    vol2 = app_volume.get_volume(A).get("volume")
    check("   on the player the side volume keys still change that session's own volume (-5, then back)", vol0 is not None and vol1 == vol0 - 5 and vol2 == vol0, f"{vol0} -> {vol1} -> {vol2}")
    comp.act("swipe_up")
    check("   up again -> Idle", wait_for(lambda: comp.now().get("idle_selected") is True, 6.0))
    mark = board.mark()
    comp.act("swipe_up")
    ok = wait_for(lambda: comp.now().get("app") == B and comp.now().get("idle_selected") is False, 6.0)
    check("5  swipe up from Idle wraps to the first item (B, a paused session)", ok, str(comp.now().get("app")))
    check("   ... and the display shows its player (A keeps playing: no idle)", board.wait(r"app: screen -> player", mark, 4.0) is not None and comp.states().get(A) == "playing")
    comp.act("swipe_down")
    check("   swipe down from the first item wraps backwards onto Idle", wait_for(lambda: comp.now().get("idle_selected") is True, 6.0))

    # ---- 4. auto-follow leaves Idle
    print("-- auto-follow")
    mark = board.mark()
    t0 = time.time()
    comp.set_state(B, True)                                              # YouTube starts (a pause -> play transition)
    hit = board.wait(r"app: screen -> player", mark, 8.0)
    check("4  a session starting makes the display leave Idle", hit is not None and comp.now().get("idle_selected") is False and comp.now().get("app") == B, f"{hit[0] - t0:.1f} s, now {comp.now().get('app')}" if hit else str(comp.now().get("app")))
    check("   ... automatic mode again, and A was not paused", comp.now().get("mode") == "auto" and comp.states().get(A) == "playing")

    # ---- the idle choice holds while something keeps playing (no 20 s return)
    comp.act("swipe_down")                                               # displayed B is the first item: down wraps backwards onto Idle
    wait_for(lambda: comp.now().get("idle_selected") is True, 6.0) or (comp.act("swipe_up"), time.sleep(3.0))
    mark = board.mark()
    time.sleep(24.0)
    check("   the idle choice stays for 24 s although both sessions play (no timeout)", comp.now().get("idle_selected") is True and not any(e == "player" for _t, e in board.screen_events(mark)))

    # ---- a tap on a SELECTED idle screen leaves that choice
    print("-- tap")
    mark = board.mark()
    comp.act("touch_cover")
    hit = board.wait(r"app: screen -> player", mark, 6.0)
    check("   tapping a selected idle screen returns to the player (back to automatic)", hit is not None and wait_for(lambda: comp.now().get("idle_selected") is False, 4.0))
    time.sleep(3.0)
    check("   ... and it does not bounce back to idle", not any(e == "idle" for _t, e in board.screen_events(mark)), str(board.screen_events(mark)))

    # ---- swiping from the automatically shown idle screen (nothing plays)
    print("-- swipes from the automatic idle screen (nothing playing)")
    comp.set_state(A, False)
    comp.set_state(B, False)
    mark = board.mark()
    check("   everything paused: the idle screen appears by itself", board.wait(r"app: screen -> idle", mark, 12.0) is not None)
    time.sleep(1.5)
    mark = board.mark()
    comp.act("swipe_up")
    hit = board.wait(r"app: screen -> player", mark, 8.0)
    check("   swipe up from it walks on to the first item and shows its (paused) player", hit is not None and comp.now().get("idle_selected") is False and comp.now().get("app") in (A, B), str(comp.now().get("app")))
    time.sleep(6.0)
    check("   ... and the player stays up (the swipe counts as using it)", not any(e == "idle" for _t, e in board.screen_events(mark)), str(board.screen_events(mark)))
    check("   no reset happened during the carousel checks", board.count(r"ESP-ROM|rst:0x|Guru Meditation|abort\(\)|Backtrace", phase_mark) == 0)
    time.sleep(10.5)                                                    # (one more health line)
    print(f"   (real finger presses on the board during this phase: {board.touches() - touches0} - anything above 0 is a person using it)")
    comp.act("swipe_down")
    time.sleep(1.0)
    http_auto = comp.monitor.auto_source()
    check("   back to automatic", http_auto.get("ok") is True)



def phase_peek(board: Board, comp: Companion, procs: dict) -> None:
    print("\n== tap-to-peek: a paused session can still be resumed from the display")
    comp.store.update(theme="dark", delay_s=4, text="")
    comp.set_state(A, False)
    comp.set_state(B, False)
    mark = board.mark()
    # (a swipe on the player keeps it up for 30 s: wait for the idle screen to come back by itself before tapping it)
    check("   (setup) the idle screen is up", board.wait(r"app: screen -> idle", mark, 45.0) is not None or comp.now().get("idle_selected") is True)
    time.sleep(2.0)
    mark = board.mark()
    t0 = time.time()
    comp.act("touch_cover")                              # a real tap through the display's LVGL input path
    hit = board.wait(r"app: screen -> player", mark, 6.0)
    check("   tapping the idle screen with a paused session brings the player back", hit is not None, f"{hit[0] - t0:.1f} s" if hit else "")
    mark2 = board.mark()
    hit = board.wait(r"app: screen -> idle", mark2, 45.0)
    held = (hit[0] - t0) if hit else 0
    check("   it stays for about 30 s, then the idle screen returns", hit is not None and 27.0 <= held <= 38.0, f"{held:.0f} s")
    # no session at all: a tap has nothing to show
    for app in (A, B):
        fakes.stop_fake(procs.pop(app, None))
    check("   (both fake sessions closed)", wait_for(lambda: not comp.states(), 12.0, 0.5))
    mark = board.mark()
    check("   with no session at all the idle screen is up", board.wait(r"app: screen -> idle", mark, 12.0) is not None or comp.now().get("has_session") is False)
    time.sleep(3.0)
    mark = board.mark()
    comp.act("touch_cover")
    time.sleep(5.0)
    check("   tapping the idle screen with no session does nothing", not any(e == "player" for _t, e in board.screen_events(mark)), str(board.screen_events(mark)))
    procs[A] = fakes.start_fake(A, paused=True, log_path=os.path.join(LOGS, "idle-fake-A.log"))          # the soak needs them again
    procs[B] = fakes.start_fake(B, paused=True, log_path=os.path.join(LOGS, "idle-fake-B.log"))
    wait_for(lambda: A in comp.states() and B in comp.states(), 25.0, 0.5)


def synthetic_landscape() -> bytes:
    """A made-up landscape (sky gradient, sun, two mountains) - never a real photo - for documentation screenshots."""
    img = Image.new("RGB", (1200, 800))
    d = ImageDraw.Draw(img)
    for y in range(800):
        d.line((0, y, 1200, y), fill=(30 + y // 8, 60 + y // 6, 140 + y // 10))
    d.ellipse((820, 120, 1000, 300), fill=(255, 214, 120))
    d.polygon([(0, 800), (300, 420), (560, 800)], fill=(30, 40, 60))
    d.polygon([(380, 800), (700, 350), (1000, 800)], fill=(22, 30, 48))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def phase_evidence(board: Board, comp: Companion) -> None:
    print("\n== documentation screenshots (synthetic picture only)")
    comp.store.set_picture(synthetic_landscape(), "sample-landscape.png", "fill")
    shots = [("minimal", dict(theme="minimal", text="Welcome back")), ("dark", dict(theme="dark", text="Welcome back")),
             ("neon", dict(theme="neon", text="Welcome back", accent="#FF4FA3")), ("photo-fill", dict(theme="photo", bg_mode="fill", text="Welcome back")),
             ("photo-fit", dict(theme="photo", bg_mode="fit", text="")), ("photo-blur", dict(theme="photo", bg_mode="blur", text="")),
             ("12h", dict(theme="dark", bg_mode="fill", clock_24h=False, text="Good evening", accent="#1ED760"))]
    for name, changes in shots:
        apply_and_wait(board, comp, name, **changes)
        time.sleep(1.5)
        board.screenshot(comp.monitor, f"evidence-{name}.png")


def health_stats(lines):
    rx = re.compile(r"health: up=(\d+)s touches=(\d+) int_free=(\d+) int_min=(\d+) int_big=(\d+) psram_free=(\d+) lvgl_stack_free_min=(\d+)(?: poll_stack_free_min=(\d+))?")
    out = []
    for _t, ln in lines:
        m = rx.search(ln)
        if m:
            out.append([int(g) if g else 0 for g in m.groups()])
    return out


def phase_ring_shots(board: Board, comp: Companion, procs: dict) -> None:
    print("\n== documentation screenshots of the ring item (synthetic fake sessions only)")
    comp.store.update(theme="dark", delay_s=4, text="", show_clock=True)
    for app in (A, B):
        if app not in comp.states():
            procs[app] = fakes.start_fake(app, paused=True, log_path=os.path.join(LOGS, f"idle-fake-{app}.log"))
    wait_for(lambda: A in comp.states() and B in comp.states(), 25.0, 0.5)
    comp.monitor.auto_source()
    comp.set_state(B, False)
    comp.set_state(A, True)
    time.sleep(4.0)
    board.screenshot(comp.monitor, "ring-1-player.png")
    mark = board.mark()
    comp.act("swipe_up_shot")
    board.wait(r"SHOT_END", mark, 40.0)
    lines = [ln for _t, ln in board.since(mark)]
    shot.assemble(lines, os.path.join(LOGS, "ring-2-pill.png"))
    time.sleep(3.0)
    board.screenshot(comp.monitor, "ring-3-idle.png")
    comp.act("swipe_down")
    time.sleep(1.0)
    comp.monitor.auto_source()



def phase_ring_soak(board: Board, comp: Companion, procs: dict, cycles: int) -> None:
    print(f"\n== soak of the ring item: {cycles} x (swipe up onto Idle, swipe down back to the session)")
    comp.store.update(theme="neon", delay_s=4, text="Ring")
    for app in (A, B):
        if app not in comp.states():
            procs[app] = fakes.start_fake(app, paused=True, log_path=os.path.join(LOGS, f"idle-fake-{app}.log"))
    wait_for(lambda: A in comp.states() and B in comp.states(), 25.0, 0.5)
    comp.monitor.auto_source()
    comp.set_state(B, False)
    comp.set_state(A, True)
    time.sleep(3.0)
    if comp.now().get("app") != A or comp.now().get("idle_selected"):
        comp.monitor.auto_source()
        time.sleep(3.0)
    mark = board.mark()
    calls_before = len(comp.calls)
    t_start = time.time()
    bad_up = bad_down = 0
    for i in range(cycles):
        comp.act("swipe_up", gap=2.6)                                  # A (2 of 3) -> Idle Screen
        bad_up += 0 if wait_for(lambda: comp.now().get("idle_selected") is True, 2.5, 0.1) else 1
        comp.act("swipe_down", gap=2.6)                                # Idle -> A
        bad_down += 0 if wait_for(lambda: comp.now().get("idle_selected") is False and comp.now().get("app") == A, 2.5, 0.1) else 1
        if (i + 1) % 10 == 0:
            print(f"   ... {i + 1}/{cycles} cycles, {int(time.time() - t_start)} s", flush=True)
    time.sleep(3.0)
    events = board.screen_events(mark)
    idles = sum(1 for _t, e in events if e == "idle")
    players = sum(1 for _t, e in events if e == "player")
    check("10 every swipe onto Idle reached the companion as an idle selection, and every swipe down brought the session back", bad_up == 0 and bad_down == 0, f"{bad_up} / {bad_down} misses in {cycles} cycles")
    check("10 the screen changes are in the log for (almost) every cycle (the USB console occasionally drops a log line)", idles >= cycles - 2 and players >= cycles - 2, f"{idles} idle / {players} player transitions for {cycles} cycles")
    check("10 Spotify kept playing through all of it (nothing was ever paused)", comp.states().get(A) == "playing")
    check("10 no media command was sent by the swipes", not any(c[0] == "control" for c in comp.calls[calls_before:]), str(comp.calls[calls_before:][:3]))
    check("10 the board never reset", board.count(r"ESP-ROM|rst:0x|Guru Meditation|abort\(\)|Backtrace", mark) == 0)
    stats = health_stats(board.since(mark))
    if len(stats) >= 3:
        first, last = stats[1], stats[-1]
        steady_first, steady_last = max(x[2] for x in stats[1:7]), max(x[2] for x in stats[-7:])
        check("10 internal heap returns to the same steady level (peak of the last samples)", steady_last >= steady_first - 2048, f"{steady_first} -> {steady_last} B")
        check("10 internal heap low-water mark stays above a safe floor (momentary dips when allocations coincide are normal)", last[3] >= 60000, f"{first[3]} -> {last[3]} B")
        check("10 PSRAM did not leak", last[5] >= first[5] - 65536, f"{first[5]} -> {last[5]} B")
        check("10 LVGL stack headroom steady", last[6] >= 8000 and last[6] >= first[6] - 256, f"{first[6]} -> {last[6]} B")
        print(f"   health (first -> last): int_free {first[2]} -> {last[2]}, int_min {first[3]} -> {last[3]}, psram_free {first[5]} -> {last[5]}, lvgl_stack_min {first[6]} -> {last[6]}")
    comp.monitor.auto_source()



def phase_soak(board: Board, comp: Companion, cycles: int) -> None:
    print(f"\n== 10. soak: {cycles} idle <-> playback cycles")
    comp.store.update(delay_s=3, theme="neon", text="Soak")                  # the most expensive built-in theme
    time.sleep(3.0)
    comp.set_state(A, False)
    comp.set_state(B, False)
    time.sleep(6.0)
    mark = board.mark()
    t_start = time.time()
    ok_cycles = 0
    for i in range(cycles):
        comp.set_state(A, True)
        time.sleep(3.0)
        comp.set_state(A, False)
        time.sleep(7.5)
        ok_cycles += 1
        if (i + 1) % 10 == 0:
            print(f"   ... {i + 1}/{cycles} cycles, {int(time.time() - t_start)} s", flush=True)
    time.sleep(3.0)
    events = board.screen_events(mark)
    players = sum(1 for _t, e in events if e == "player")
    idles = sum(1 for _t, e in events if e == "idle")
    check("10 every playback start left the idle screen and every pause returned to it", players >= cycles and idles >= cycles, f"{players} player / {idles} idle transitions for {cycles} cycles")
    check("10 the board never reset during the soak", board.count(r"ESP-ROM|rst:0x|Guru Meditation|abort\(\)|Backtrace", mark) == 0)
    stats = health_stats(board.since(mark))
    check("10 health lines were logged throughout", len(stats) >= max(3, int((time.time() - t_start) / 12)), f"{len(stats)} lines")
    if len(stats) >= 3:
        first, last = stats[1], stats[-1]
        check("10 uptime is continuous", [s[0] for s in stats] == sorted(s[0] for s in stats))
        steady_first, steady_last = max(x[2] for x in stats[1:7]), max(x[2] for x in stats[-7:])
        check("10 internal heap returns to the same steady level (peak of the last samples)", steady_last >= steady_first - 2048, f"{steady_first} -> {steady_last} B")
        check("10 internal heap low-water mark stays above a safe floor (momentary dips when allocations coincide are normal)", last[3] >= 60000, f"{first[3]} -> {last[3]} B")
        check("10 PSRAM did not leak", last[5] >= first[5] - 65536, f"{first[5]} -> {last[5]} B")
        check("10 LVGL stack headroom steady and healthy", last[6] >= 8000 and last[6] >= first[6] - 256, f"{first[6]} -> {last[6]} B")
        if last[7]:
            check("10 poll task stack headroom healthy", last[7] >= 1500, f"{last[7]} B")
        print(f"   health (first -> last): int_free {first[2]} -> {last[2]}, int_min {first[3]} -> {last[3]}, psram_free {first[5]} -> {last[5]}, lvgl_stack_min {first[6]} -> {last[6]}, poll_stack_min {first[7]} -> {last[7]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="auto")
    ap.add_argument("--transport", choices=("lan", "usb"), default="lan",
                    help="lan: the companion of this test serves the board over Wi-Fi (the board has the LAN credentials); usb: it talks to the board "
                         "over the USB cable (tools/usb_harness.py; scratch data folder, the board is paired fresh)")
    ap.add_argument("--soak-cycles", type=int, default=30)
    ap.add_argument("--skip-soak", action="store_true")
    ap.add_argument("--skip-slow", action="store_true", help="skip the minute-rollover wait")
    ap.add_argument("--ring-cycles", type=int, default=40, help="cycles of the ring soak (swipe onto Idle and back)")
    ap.add_argument("--only", default="", help="comma separated phases to run: basics,themes,minute,modes,persistence,fallbacks,carousel,peek,ringshots,ringsoak,soak,evidence")
    args = ap.parse_args()

    if args.transport == "lan":
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2)
            print(f"something already listens on port {PORT}: stop the normal companion first (this test brings its own).")
            return 2
        except OSError:
            pass
    os.makedirs(LOGS, exist_ok=True)
    data_dir = tempfile.mkdtemp(prefix="idle-test-")
    procs: dict = {}
    if args.transport == "usb":
        import usb_harness as uh
        board = uh.UsbBoard()
        comp = uh.UsbCompanion(os.path.join(data_dir, "idle"), board, usb_port="" if args.port == "auto" else args.port)
        if not uh.ensure_paired(board, comp):
            print("the display could not be paired over USB (is it connected, running a development build, not in use by another program?)")
            return 2
    else:
        port = sc.detect_port() if args.port == "auto" else args.port
        board = Board(port)
        comp = Companion(os.path.join(data_dir, "idle"))
    try:
        comp.store.update(delay_s=4)
        only = {x.strip() for x in args.only.split(",") if x.strip()}
        want = lambda name: (not only) or name in only
        if want("basics") or only:
            phase_idle_basics(board, comp, procs)                          # (also starts the fakes the other phases use)
        if want("themes"):
            phase_themes(board, comp)
        if want("minute") and not args.skip_slow:
            phase_minute_rollover(board, comp)
        if want("modes"):
            phase_modes(board, comp)
        if want("persistence"):
            phase_persistence(board, comp)
        if want("fallbacks"):
            phase_fallbacks(board, comp)
        if want("carousel"):
            phase_carousel(board, comp, procs)
        if want("peek"):
            phase_peek(board, comp, procs)
        if want("evidence") and only:
            phase_evidence(board, comp)
        if want("ringshots"):
            phase_ring_shots(board, comp, procs)
        if want("ringsoak") and not args.skip_soak:
            phase_ring_soak(board, comp, procs, args.ring_cycles)
        if want("soak") and not args.skip_soak:
            phase_soak(board, comp, args.soak_cycles)
    finally:
        for p in procs.values():
            fakes.stop_fake(p)
        try:
            comp.stop_http()
        except Exception:
            pass
        board.stop = True
        with open(os.path.join(LOGS, "idle-test-serial.log"), "w", encoding="utf-8") as f:
            for t, ln in board.lines:
                if not re.match(r"^S\d+ ", ln):
                    f.write(f"{t:.2f} {ln}\n")
        shutil.rmtree(data_dir, ignore_errors=True)
    print(f"\n{len(failures)} failure(s)" + (": " + "; ".join(failures) if failures else ""))
    with open(os.path.join(LOGS, "idle-test-result.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, indent=1)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

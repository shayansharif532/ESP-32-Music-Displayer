#!/usr/bin/env python3
"""Hardware-in-the-loop test of gestures, media-session switching and per-app volume (development tool).

Needs: a board running the dev firmware (-DAPP_DEV_TOOLS=1) that talks to a companion started with --dev-tools on this PC.
The script starts its own silent fake media session (python.exe, with a silent audio stream) next to whatever real sessions
(Spotify, Chrome ...) exist, drives the display with the board's virtual finger / simulated side keys, and judges what happened
from the companion's log (which command reached which session) and from /sources + /now-playing.

Real sessions are only ever *selected* (the vertical swipe) - never played / paused / skipped. The only commands that reach a real
session are one down+up pair of volume steps (net change 0), unless --no-real-volume is given. Run it when nobody is watching
the display: it changes what the display shows.

    tools\\venv\\Scripts\\python.exe tools\\session_test.py
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PY = os.path.join(ROOT, "companion", ".venv", "Scripts", "python.exe")
FAKE = os.path.join(ROOT, "companion", "tools", "fake_media_session.py")
COMPANION_LOG = os.path.join(os.environ.get("DESK_MEDIA_DATA_DIR") or os.path.join(os.environ.get("LOCALAPPDATA", ""), "DeskMediaCompanion"), "companion.log")
ACTION_WAIT_S = 3.4          # a debug action reaches the board with its next poll (<= 1 s), then the gesture itself takes ~1 s

BASE = "http://127.0.0.1:8765"
failures: list[str] = []
fake_proc = None


def http(method: str, path: str, base: str = BASE):
    req = urllib.request.Request(base + path, method=method, headers={"X-Desk-Client": "session-test"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"http_error": exc.code}


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""), flush=True)
    if not ok:
        failures.append(name)
    return ok


class LogTail:
    """Text appended to the companion log since mark()."""

    def __init__(self) -> None:
        self.mark()

    def mark(self) -> None:
        self.pos = os.path.getsize(COMPANION_LOG) if os.path.exists(COMPANION_LOG) else 0

    def read(self) -> str:
        if not os.path.exists(COMPANION_LOG):
            return ""
        size = os.path.getsize(COMPANION_LOG)
        if size < self.pos:
            self.pos = 0                                  # rotated
        with open(COMPANION_LOG, "rb") as f:
            f.seek(self.pos)
            text = f.read().decode("utf-8", errors="replace")
        self.pos = size
        return text


LOG = LogTail()


SELECTION_ONLY = {"swipe_up", "swipe_down"}


def act(action: str, wait: float = ACTION_WAIT_S, allow_real: bool = False) -> str:
    """Run a dev action on the board; returns the companion log text produced meanwhile. Actions that can press a media control or
    change a volume are refused unless the fake session is the one on screen (allow_real: the deliberate volume down+up pair)."""
    if action not in SELECTION_ONLY and not allow_real and now().get("app") != "python.exe":
        raise SystemExit(f"refusing '{action}': the displayed session is {now().get('app')!r}, not the fake test session")
    LOG.mark()
    http("POST", f"/debug/{action}")
    time.sleep(wait)
    return LOG.read()


def now() -> dict:
    return http("GET", "/now-playing")


def sources() -> dict:
    return http("GET", "/sources")


def states() -> dict[str, str]:
    return {s["app"]: s["state"] for s in sources().get("sources", []) if not s.get("synthetic")}       # sessions only: the idle item is not one


def session_apps() -> list[str]:
    return [s["app"] for s in sources().get("sources", []) if not s.get("synthetic")]


def shown() -> str:
    """What the display shows: "idle" when the idle screen was selected as a source (ADR-008), else the session's app id."""
    n = now()
    return "idle" if n.get("idle_selected") else n.get("app", "")


def start_fake(paused: bool = False) -> None:
    global fake_proc
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    out = open(os.path.join(ROOT, "logs", "fake-session.log"), "w")
    args = [PY, FAKE, "--silent-audio", "--art-dir", os.path.join(ROOT, "logs", "test-art2")] + (["--paused"] if paused else [])
    fake_proc = subprocess.Popen(args, stdout=out, stderr=subprocess.STDOUT)
    for _ in range(30):
        time.sleep(0.5)
        if any(s["app"] == "python.exe" for s in sources().get("sources", [])):
            return
    raise SystemExit("the fake session did not show up in /sources")


def stop_fake() -> None:
    global fake_proc
    if fake_proc and fake_proc.poll() is None:
        fake_proc.terminate()
        try:
            fake_proc.wait(5)
        except subprocess.TimeoutExpired:
            fake_proc.kill()
    fake_proc = None


def select(app: str, direction: str = "swipe_up") -> bool:
    """Swipe vertically until `app` is displayed (selection only)."""
    for _ in range(8):                                        # (the ring also holds the idle item: up to one extra step)
        if shown().lower() == app.lower():
            return True
        act(direction)
    return shown().lower() == app.lower()


def ctl(action: str, app: str):
    return http("POST", f"/control/{action}?app={app}")


def wait_for(pred, timeout: float = 5.0, step: float = 0.25) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(step)
    return False


def volume_of(app: str):
    r = http("GET", f"/volume?app={app}")
    return r.get("volume") if r.get("ok") else None


def main() -> int:
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-real-volume", action="store_true", help="do not send the volume down+up pair to the real Spotify / Chrome sessions")
    ap.add_argument("--companion", default=BASE)
    args = ap.parse_args()
    BASE = args.companion

    check("companion reachable (needs --dev-tools)", http("GET", "/health").get("ok") is True)
    http("POST", "/control/source-auto")
    real_before = {a: s for a, s in states().items() if a != "python.exe"}
    print("real sessions before:", real_before)
    start_fake()
    try:
        ring = [s["app"] for s in sources()["sources"]]
        names = [s["name"] for s in sources()["sources"]]
        print("swipe ring:", names)
        check("fake + real sources are all in the swipe list", "python.exe" in ring and len(ring) >= 2, ", ".join(names))

        # ---- 1. selecting the fake session ------------------------------------------------------------------------------------
        check("the fake session (just started playing) is shown, by auto-follow or by a swipe", select("python.exe"))
        n = now()
        check("the snapshot carries the source fields", n["source_index"] >= 1 and n["sources"] == len(ring) and n.get("mode") in ("auto", "manual"), f"{n['source']} {n['source_index']}/{n['sources']} mode {n.get('mode')}")

        # ---- 2. horizontal swipes: LEFT = next, RIGHT = previous -------------------------------------------------------------
        log = act("swipe_left")
        check("finger LEFT sends NEXT to the displayed session", "control next on python.exe -> ok" in log, log.strip().splitlines()[-1] if log.strip() else "no log")
        check("... and not previous", "control previous" not in log)
        log = act("swipe_right")
        check("finger RIGHT sends PREVIOUS to the displayed session", "control previous on python.exe -> ok" in log, log.strip().splitlines()[-1] if log.strip() else "no log")
        check("... and not next", "control next" not in log)
        log = act("swipe_text")
        check("a swipe that starts on the title text works (right = previous)", "control previous on python.exe" in log)

        # ---- 3. gestures that must do nothing / only seek -----------------------------------------------------------------------
        before = now()
        log = act("swipe_diag")
        after = now()
        check("a diagonal swipe sends nothing", "control " not in log and "source" not in log and after["app"] == before["app"], log.strip()[-120:])
        log = act("swipe_bar")
        check("a horizontal swipe from the progress bar only seeks (no track change)", "control seek on python.exe" in log and "control next" not in log and "control previous" not in log)
        log = act("swipe_bar_v")
        check("a vertical swipe from the progress bar only seeks (no source change)", "control seek on python.exe" in log and "source ->" not in log and now()["app"] == "python.exe")
        log = act("touch_cover")
        check("tapping the cover sends no media command", "control " not in log and "source" not in log)
        act("touch_cover")                                    # back out of full screen

        # ---- 4. vertical swipes walk the ring, wrap around, and never touch playback ----------------------------------------------
        st0 = states()
        seen = []
        order = [s["app"] for s in sources()["sources"]]          # sessions alphabetical + the synthetic "idle" item last
        check("the ring ends with the idle screen item", order[-1] == "idle", " -> ".join(order))
        here = order.index("python.exe")
        ok_all = True
        for i in range(len(order) + 1):
            expect = order[(here + i + 1) % len(order)]
            log = act("swipe_up")
            got = shown()
            seen.append(got)
            ok_all &= got == expect and "control " not in log
        check("swipe UP walks the ring forwards and wraps around (and sends no media command)", ok_all, " -> ".join(seen))
        seen = []
        ok_all = True
        here = order.index(shown())
        for i in range(len(order) + 1):
            expect = order[(here - i - 1) % len(order)]
            log = act("swipe_down")
            got = shown()
            seen.append(got)
            ok_all &= got == expect and "control " not in log
        check("swipe DOWN walks it backwards and wraps around", ok_all, " -> ".join(seen))
        st1 = states()
        check("switching sources changed no playback state anywhere", {k: v for k, v in st0.items()} == {k: st1.get(k) for k in st0}, f"{st0} vs {st1}")

        # ---- 5. a paused session stays selectable, shows its state, and play resumes THAT session ---------------------------------
        check("select the fake again", select("python.exe"))
        log = act("touch_play")
        check("tap play/pause pauses the displayed (fake) session", "control playpause on python.exe -> ok" in log)
        time.sleep(1.5)
        others_playing = [a for a, st in states().items() if a != "python.exe" and st == "playing"]
        if others_playing:                                   # auto-follow: the fake stopped playing, another session still does
            check("the fake paused while another app plays: auto-follow moves to the playing one", wait_for(lambda: now().get("app") in others_playing, 4.0), now().get("app", ""))
        order = session_apps()
        other = [a for a in order if a != "python.exe"][0]
        check("the paused fake is still offered: swipe to it", select("python.exe", "swipe_up"))
        n = now()
        check("it is shown as paused (play icon state), with its timeline", n["state"] == "paused" and n["playing"] is False and n["app"] == "python.exe", n["state"])
        check("swipe away to another source", select(other, "swipe_up"))
        check("swipe back: the paused session is still offered and shown", select("python.exe", "swipe_down") and now()["state"] == "paused")
        log = act("touch_play")
        check("play resumes that session (not another one)", "control playpause on python.exe -> ok" in log and log.count("control playpause on") == 1)
        time.sleep(1.5)
        check("it plays again and stays on screen (the selected session is the playing one)", now()["state"] == "playing" and now()["app"] == "python.exe")

        # ---- 6. a manual selection is temporary: another session STARTING to play brings auto-follow back -----------------------
        paused_real = [a for a in order if a != "python.exe" and states().get(a) == "paused"]
        target = paused_real[0] if paused_real else other
        check(f"swipe to {target} ({states().get(target)}) - a manual selection", select(target, "swipe_up"))
        check("the snapshot says so", now().get("mode") in ("manual", "auto"), f"mode {now().get('mode')}")
        ctl("pause", "python.exe")                             # as if the user paused / started things in that app itself
        wait_for(lambda: states().get("python.exe") == "paused", 4.0)
        time.sleep(1.5)
        check("the fake pausing does not move the display", now().get("app") == target)
        ctl("play", "python.exe")                              # the fake STARTS playing again
        check("another session starts playing: the display returns to it (auto-follow)", wait_for(lambda: now().get("app") == "python.exe" and now().get("mode") == "auto", 5.0), f"{now().get('app')} {now().get('mode')}")
        log = act("touch_play")
        check("the controls follow the auto-switched display: play/pause goes to the new session", "control playpause on python.exe" in log)
        act("touch_play")                                      # and back to playing

        # ---- 7. if the selected session disappears, the display falls back to automatic ---------------------------------------------
        ctl("pause", "python.exe")
        wait_for(lambda: states().get("python.exe") == "paused", 4.0)
        check("select the (paused) fake once more", select("python.exe"))
        stop_fake()
        time.sleep(4.0)
        n = now()
        s = sources()
        check("selected session gone: back to automatic on another session", n.get("app") != "python.exe" and n.get("has_session") and s["mode"] == "auto" and n.get("mode") == "auto", f"{n.get('app')} mode={s['mode']}")
        check("... and the swipe list no longer offers it", all(x["app"] != "python.exe" for x in s["sources"]))
        if len(s["sources"]) >= 2:                                    # (the idle item counts: one session + Idle Screen = two sources)
            log = act("swipe_up")
            check("with the remaining sources (and the idle item) vertical swipes still work", "source ->" in log)
            select(session_apps()[0], "swipe_up") if session_apps() else None        # back off the idle item, if the swipe landed on it
        else:
            log = act("swipe_up")
            check("only the idle item left: vertical swipes do nothing", "source ->" not in log and "control " not in log)

        # ---- 8. volume follows the displayed source ----------------------------------------------------------------------------
        start_fake()
        time.sleep(2.0)
        check("select the fake (silent audio session)", select("python.exe"))
        http("POST", "/volume?level=50&app=python.exe")           # earlier runs may have left the fake's own volume at 0
        v0 = volume_of("python.exe")
        sp0 = volume_of("Spotify.exe")
        log = act("key_down")
        v1 = volume_of("python.exe")
        check("side key DOWN lowers the displayed (fake) app's own volume by 5", "volume python.exe:" in log and v0 is not None and v1 == v0 - 5, f"{v0} -> {v1}")
        check("... and left the real Spotify volume alone", volume_of("Spotify.exe") == sp0, f"Spotify {sp0}")
        log = act("key_up")
        check("side key UP raises it again", volume_of("python.exe") == v0, f"-> {volume_of('python.exe')}")
        if not args.no_real_volume:
            for app_id, label in (("Spotify.exe", "Spotify"), ("chrome.exe", "Chrome")):
                if app_id not in ring:
                    continue
                check(f"swipe to {label}", select(app_id, "swipe_up"))
                before_v = volume_of(app_id)
                if before_v is None:
                    print(f"      {label} has no audio session right now (a paused browser may not hold one): the display would show 'No volume control'")
                    continue
                other_apps = {a: volume_of(a) for a in ("python.exe", "Spotify.exe", "chrome.exe") if a != app_id}
                first, second = ("key_down", "key_up") if before_v >= 95 else ("key_up", "key_down")
                log1 = act(first, allow_real=True)
                mid = volume_of(app_id)
                log2 = act(second, allow_real=True)
                end = volume_of(app_id)
                check(f"side keys change {label}'s volume (and restore it)", f"volume {app_id}:" in log1 and mid != before_v and end == before_v, f"{before_v} -> {mid} -> {end}")
                check(f"... and no other app's volume moved", {a: volume_of(a) for a in other_apps} == other_apps)
        else:
            print("(real-session volume skipped)")

        # ---- 9. Spotify-only shuffle / repeat buttons ---------------------------------------------------------------------------
        print("-- Spotify-only shuffle / repeat")
        if now().get("app") != "python.exe":
            select("python.exe")
        if now().get("spotify") is True:                          # the companion runs with --treat-as-spotify python.exe: test on the fake
            check("the fake 'Spotify' reports shuffle / repeat", now().get("can_shuffle") and now().get("can_repeat"))
            sh0 = now()["shuffle"]
            log = act("touch_shuffle")
            check("tapping the shuffle button toggles shuffle of the displayed Spotify", "control shuffle on python.exe -> ok" in log and now()["shuffle"] == (not sh0), f"{sh0} -> {now().get('shuffle')}")
            act("touch_shuffle")
            check("... and back", now()["shuffle"] == sh0)
            seq = []
            for _ in range(3):
                act("touch_repeat")
                seq.append(now()["repeat"])
            check("tapping repeat cycles off -> playlist -> track -> off", seq == [(now_r + 1) % 3 for now_r in ([seq[-1]] + seq[:2])] and sorted(seq) == [0, 1, 2], str(seq))
        else:
            print("      (the companion is not run with --treat-as-spotify: fake-Spotify checks skipped)")
        for app_id, label in (("chrome.exe", "Chrome"),):
            if app_id in ring:
                check(f"swipe to {label}", select(app_id, "swipe_up"))
                check(f"{label} is not Spotify: no shuffle / repeat state", now().get("spotify") is False and not now().get("can_shuffle"))
                log = act("touch_shuffle", allow_real=True) + act("touch_repeat", allow_real=True)
                check(f"tapping where the buttons would be sends nothing for {label}", "control shuffle" not in log and "control repeat" not in log)
        if "Spotify.exe" in ring and not args.no_real_volume:
            check("swipe to the real Spotify", select("Spotify.exe", "swipe_up"))
            n = now()
            check("the real Spotify is identified (by app identity) and reports shuffle / repeat", n.get("spotify") is True and n.get("can_shuffle") and n.get("can_repeat"), json.dumps({k: n.get(k) for k in ("shuffle", "repeat")}))
            sh0, rp0 = n["shuffle"], n["repeat"]
            log = act("touch_shuffle", allow_real=True)
            check("a tap on the shuffle button changes the real Spotify's shuffle", "control shuffle on Spotify.exe -> ok" in log and now()["shuffle"] == (not sh0), f"{sh0} -> {now().get('shuffle')}")
            act("touch_shuffle", allow_real=True)
            check("... restored", now()["shuffle"] == sh0)
            seq = []
            for _ in range(3):
                act("touch_repeat", allow_real=True)
                seq.append(now()["repeat"])
            check("three taps on repeat walk through all three modes and end where they began", sorted(seq) == [0, 1, 2] and seq[-1] == rp0, f"start {rp0}: {seq}")
    finally:
        stop_fake()
        http("POST", "/control/source-auto")
    real_after = {a: s for a, s in states().items()}
    check("the real sessions' playback states are as before", all(real_after.get(a) == s for a, s in real_before.items()), f"{real_before} vs {real_after}")
    print(f"\n{len(failures)} failure(s)" + (": " + "; ".join(failures) if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

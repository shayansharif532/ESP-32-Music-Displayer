#!/usr/bin/env python3
"""Isolated end-to-end test of the idle screen as an item of the vertical source ring (ADR-008). No board needed.

It starts its own companion on port 8792 that sees ONLY two silent fake media sessions - A = python.exe (the "Spotify") and B =
deskfakeb.exe (the "YouTube") - so neither the user's real sessions nor the real display are involved, and checks through the real
companion code and the real Windows media-session API:

  the ring is [B, A, Idle Screen]; swiping onto Idle is only a display choice (A keeps playing, no command is sent, media keys aimed
  at "idle" are refused); it never times out; swiping from Idle reaches the neighbours and wraps; any session that starts playing -
  including a paused one resuming - ends it (auto-follow).

    companion\\.venv\\Scripts\\python.exe tools\\idle_source_test.py        (or tools\\venv; it needs the companion venv for the fakes)
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "companion", "tools"))
import fakes  # noqa: E402

PORT = 8792
BASE = f"http://127.0.0.1:{PORT}"
A, B = "python.exe", "deskfakeb.exe"
IDLE = "idle"
failures: list[str] = []
procs: dict[str, subprocess.Popen] = {}


def http(method, path):
    req = urllib.request.Request(BASE + path, method=method, headers={"X-Desk-Client": "idle-source-test"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"http_error": exc.code, **json.loads(exc.read().decode("utf-8") or "{}")}


def now():
    return http("GET", "/now-playing")


def sources():
    return http("GET", "/sources")


def states():
    return {s["app"]: s["state"] for s in sources().get("sources", []) if not s.get("synthetic")}


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""), flush=True)
    if not ok:
        failures.append(name)
    return ok


def wait_for(pred, timeout=6.0, step=0.25):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(step)
    return False


def set_state(app, playing):
    want = "playing" if playing else "paused"
    if states().get(app) != want:
        http("POST", f"/control/{'play' if playing else 'pause'}?app={app}")
        wait_for(lambda: states().get(app) == want, 4.0)


def swipe(direction, frm):
    return http("POST", f"/control/source-{direction}?from={frm}")


def start(app, paused):
    procs[app] = fakes.start_fake(app, paused=paused, log_path=os.path.join(ROOT, "logs", f"fake-{app}.log"))
    return wait_for(lambda: app in states(), 20.0, 0.5)


def main() -> int:
    data_dir = tempfile.mkdtemp(prefix="idle-source-test-")
    comp = subprocess.Popen([fakes.VENV_PY, "-m", "desk_media_companion", "--port", str(PORT), "--no-discovery", "--quiet", "--no-tray",
                             "--only-apps", f"{A},{B}", "--data-dir", data_dir],
                            cwd=os.path.join(ROOT, "companion"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_for(lambda: http("GET", "/health").get("ok"), 15.0, 0.5):
            raise SystemExit("test companion did not start")
        print("== the ring before any media session exists")
        r = sources()
        check("with no session the idle item alone is offered (always available)", [x["app"] for x in r["sources"]] == [IDLE], str(r["sources"]))
        n = now()
        check("/now-playing: one source, idle not selected", n["sources"] == 1 and n["idle_selected"] is False, f"{n['sources']} {n['idle_selected']}")
        check("a swipe with only the idle item does nothing", swipe("next", IDLE).get("changed") is False)

        check("fake A (the 'Spotify') is a media session", start(A, paused=False))
        check("fake B (the 'YouTube') is a media session", start(B, paused=True))
        time.sleep(1.0)
        set_state(B, False)
        set_state(A, True)

        print("\n== 1. Spotify + YouTube + Idle all appear in vertical cycling")
        rows = sources()["sources"]
        check("the ring is [YouTube-like, Spotify-like, Idle Screen]", [x["app"] for x in rows] == [B, A, IDLE], str([x["app"] for x in rows]))
        check("the idle row is a synthetic item named 'Idle Screen'", rows[-1].get("synthetic") is True and rows[-1]["name"] == "Idle Screen")
        check("A is displayed, 2 of 3", wait_for(lambda: now().get("app") == A and now().get("sources") == 3 and now().get("source_index") == 2), str((now().get("app"), now().get("source_index"), now().get("sources"))))
        check("idle is not selected yet", now()["idle_selected"] is False and now()["mode"] == "auto")

        print("\n== 2./3. Idle is reachable while Spotify plays, and selecting it does not touch playback")
        r = swipe("next", A)
        check("swipe up from Spotify -> the idle item", r.get("changed") and r.get("app") == IDLE and r.get("source") == "Idle Screen", str({k: r.get(k) for k in ('app', 'source', 'index', 'count', 'idle')}))
        check("... position 3 of 3 and flagged as the idle item", r.get("index") == 3 and r.get("count") == 3 and r.get("idle") is True)
        n = r["now"]
        check("the snapshot says idle is selected (a manual, temporary choice)", n["idle_selected"] is True and n["mode"] == "manual" and n["source_index"] == 3)
        time.sleep(1.5)
        check("Spotify keeps playing in the background (selecting Idle sent no command)", states().get(A) == "playing", str(states()))
        check("the snapshot still describes what AUTO would show underneath", now()["app"] == A and now()["playing"] is True)
        check("/sources marks the idle row as displayed", [x for x in sources()["sources"] if x["app"] == IDLE][0]["displayed"] is True)

        print("\n== 7. media keys and volume aimed at the idle item are refused (no app is touched)")
        vol_before = http("GET", f"/volume?app={A}").get("volume")
        for path in (f"/control/next?app={IDLE}", f"/control/playpause?app={IDLE}", f"/control/seek?position_ms=1000&app={IDLE}",
                     f"/control/volume-up?app={IDLE}", f"/control/volume-down?step=5&app={IDLE}", f"/volume?level=1&app={IDLE}"):
            r = http("POST", path)
            check(f"refused: POST {path.split('?')[0]}", r.get("ok") is False and "idle" in json.dumps(r).lower(), str(r)[:90])
        check("Spotify is still playing and its volume did not move", states().get(A) == "playing" and http("GET", f"/volume?app={A}").get("volume") == vol_before, f"{vol_before}")

        print("\n== the idle choice never times out while something plays in the background (the 20 s return does not apply)")
        time.sleep(23.0)
        check("still on the idle item after 23 s with Spotify playing", now()["idle_selected"] is True)

        print("\n== 5. swipe up / down from Idle reaches the neighbouring sessions")
        r = swipe("prev", IDLE)
        check("swipe down from Idle -> the previous item (Spotify)", r.get("app") == A and r.get("index") == 2 and r["now"]["idle_selected"] is False, str(r.get("app")))
        r = swipe("next", A)
        check("up again -> Idle", r.get("app") == IDLE)
        r = swipe("next", IDLE)
        check("swipe up from Idle wraps to the first item (the other session)", r.get("app") == B and r.get("index") == 1, str(r.get("app")))
        check("... a paused session is a normal manual selection", r["now"]["app"] == B and r["now"]["idle_selected"] is False and r["now"]["mode"] == "manual")
        r = swipe("prev", B)
        check("swipe down from the first item wraps backwards onto Idle", r.get("app") == IDLE and r["now"]["idle_selected"] is True)

        print("\n== 4. a session starting leaves Idle by itself (auto-follow)")
        t0 = time.time()
        set_state(B, True)                                  # YouTube starts
        ok = wait_for(lambda: now().get("idle_selected") is False and now().get("app") == B, 5.0)
        check("YouTube starting: the display leaves Idle and follows it", ok, f"{time.time() - t0:.1f} s" if ok else str(now().get("app")))
        check("... automatic mode again", now()["mode"] == "auto")
        check("... Spotify was not paused by any of this", states().get(A) == "playing", str(states()))

        print("\n== a paused session RESUMING also ends it")
        swipe("next", B) if now()["app"] == B and now()["source_index"] == 1 else None
        r = swipe("next", A) if now()["app"] == A else r
        # go to Idle from wherever we are
        for _ in range(4):
            if now().get("idle_selected"):
                break
            swipe("next", now().get("app", ""))
        check("Idle selected again", now().get("idle_selected") is True)
        set_state(B, False)
        set_state(A, False)
        time.sleep(1.0)
        check("everything paused: the user may remain on Idle", now()["idle_selected"] is True)
        set_state(A, True)
        check("a paused Spotify resuming brings it back", wait_for(lambda: now().get("idle_selected") is False and now().get("app") == A, 5.0), str(now().get("app")))

        print("\n== back to automatic")
        for _ in range(4):
            if now().get("idle_selected"):
                break
            swipe("next", now().get("app", ""))
        r = http("POST", "/control/source-auto")
        check("source-auto clears an idle selection", r["now"]["idle_selected"] is False and r["now"]["mode"] == "auto")
        set_state(A, False)
    finally:
        for p in procs.values():
            fakes.stop_fake(p)
        comp.terminate()
        try:
            comp.wait(5)
        except subprocess.TimeoutExpired:
            comp.kill()
    print(f"\n{len(failures)} failure(s)" + (": " + "; ".join(failures) if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

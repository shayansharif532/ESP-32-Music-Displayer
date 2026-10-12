#!/usr/bin/env python3
"""Isolated end-to-end test of the AUTO / MANUAL_TEMP source selection and of Spotify's shuffle / repeat controls (no board needed).

It starts its own companion on port 8790 that sees ONLY two silent fake media sessions - A = python.exe (treated as "Spotify" through
the dev flag --treat-as-spotify) and B = deskfakeb.exe (the "YouTube") - so neither the owner's real sessions nor the real display are
involved. Everything goes through the real companion code and the real Windows media-session API (GSMTC); the fakes obey play / pause /
seek / shuffle / repeat like real apps.

    tools\\venv\\Scripts\\python.exe tools\\auto_follow_test.py
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "companion", "tools"))
import fakes  # noqa: E402

PORT = 8790
BASE = f"http://127.0.0.1:{PORT}"
A, B = "python.exe", "deskfakeb.exe"
failures: list[str] = []
procs: dict[str, subprocess.Popen] = {}


def http(method, path):
    req = urllib.request.Request(BASE + path, method=method, headers={"X-Desk-Client": "auto-follow-test"})
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
    return {s["app"]: s["state"] for s in sources().get("sources", [])}


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


def shows(app, timeout=6.0):
    return wait_for(lambda: now().get("app") == app, timeout)


def ctl(action, app, extra=""):
    return http("POST", f"/control/{action}?app={app}{extra}")


def set_state(app, playing):
    """Press play / pause on that app (as the user would in the app itself), then wait until Windows reports it."""
    want = "playing" if playing else "paused"
    if states().get(app) != want:
        ctl("play" if playing else "pause", app)
        wait_for(lambda: states().get(app) == want, 4.0)


def swipe_to(app, direction="source-next"):
    for _ in range(4):
        if now().get("app") == app:
            return True
        http("POST", f"/control/{direction}?from={now().get('app', '')}")
    return now().get("app") == app


def start(app, paused):
    procs[app] = fakes.start_fake(app, paused=paused, log_path=os.path.join(ROOT, "logs", f"fake-{app}.log"))
    return wait_for(lambda: app in states(), 20.0, 0.5)


def main() -> int:
    comp = subprocess.Popen([fakes.VENV_PY, "-m", "desk_media_companion", "--port", str(PORT), "--no-discovery", "--quiet",
                             "--only-apps", f"{A},{B}", "--treat-as-spotify", A],
                            cwd=os.path.join(ROOT, "companion"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_for(lambda: http("GET", "/health").get("ok"), 15.0, 0.5):
            raise SystemExit("test companion did not start")
        check("fake A (the 'Spotify') is a media session", start(A, paused=False))
        check("fake B (the 'YouTube') is a media session", start(B, paused=True))
        time.sleep(1.0)
        check("the isolated companion sees exactly the two fakes", sorted(states()) == sorted([A, B]), str(states()))

        # ---- auto-follow before any swipe -------------------------------------------------------------------------------------
        print("\n== auto-follow, no swipes yet")
        set_state(B, False)
        check("A playing, B paused: the display shows A", shows(A), now().get("app", ""))
        set_state(A, False)
        set_state(B, True)
        check("A paused, B starts playing: the display moves to B", shows(B), now().get("app", ""))
        set_state(A, True)
        check("A starts playing while B plays: the one that started last (A) wins", shows(A), now().get("app", ""))
        set_state(B, False)
        check("B pauses: still A", shows(A))
        set_state(A, False)
        check("everything paused: the session that played last stays (A)", shows(A) and now()["state"] == "paused", now().get("state", ""))

        # ---- case A ---------------------------------------------------------------------------------------------------------------
        print("\n== case A: Spotify playing, browse to paused YouTube, Spotify starts again")
        set_state(A, True)
        set_state(B, False)
        check("start: A shows", shows(A))
        r = http("POST", f"/control/source-next?from={A}")
        check("a vertical swipe selects B", r.get("ok") and r.get("app") == B and r.get("mode") == "manual" and now()["app"] == B and now()["mode"] == "manual", json.dumps({k: r.get(k) for k in ("app", "mode")}))
        check("B is paused and says so (play icon state)", now()["state"] == "paused" and now()["playing"] is False)
        time.sleep(3.0)
        check("manual browsing holds for a while although A is playing", now()["app"] == B)
        ctl("pause", A)
        wait_for(lambda: states().get(A) == "paused", 4.0)
        check("A paused: B stays (nothing else is playing)", now()["app"] == B)
        ctl("play", A)
        check("A STARTS playing: back to AUTO, the display returns to A", shows(A, 4.0) and now()["mode"] == "auto", f"{now().get('app')} {now().get('mode')}")
        a2 = ctl("playpause", A)
        check("controls follow the shown session (play/pause answered by A)", a2.get("app") == A, json.dumps(a2)[:90])
        set_state(A, True)

        print("\n== case A2: Spotify just keeps playing - the manual selection times out")
        http("POST", f"/control/source-next?from={A}")
        check("manual on B", now()["app"] == B and now()["mode"] == "manual")
        t0 = time.time()
        time.sleep(15.0)
        check("still on B after 15 s without touching it", now()["app"] == B)
        returned = wait_for(lambda: now().get("app") == A, 12.0)
        dt = time.time() - t0
        check("returns to A (AUTO) after the 20 s idle time", returned and 19.0 <= dt <= 24.0, f"{dt:.1f} s")

        print("\n== case A3: touching the selected session restarts the idle timer")
        http("POST", f"/control/source-next?from={A}")
        time.sleep(14.0)
        ctl("seek", B, "&position_ms=5000")                                                # the user seeks the paused video
        time.sleep(14.0)
        check("28 s after the swipe but 14 s after the seek: still on B", now()["app"] == B)
        check("... and back to A once it has been idle for 20 s", wait_for(lambda: now().get("app") == A, 12.0))

        # ---- case B ---------------------------------------------------------------------------------------------------------------
        print("\n== case B: YouTube playing, browse to Spotify, YouTube resumes")
        set_state(A, False)
        set_state(B, True)
        check("start: B shows", shows(B))
        http("POST", f"/control/source-next?from={B}")
        check("a swipe selects A (paused)", now()["app"] == A and now()["mode"] == "manual" and now()["state"] == "paused", f"{now().get('app')} {now().get('state')}")
        ctl("pause", B)
        wait_for(lambda: states().get(B) == "paused", 4.0)
        check("YouTube paused: A stays", now()["app"] == A)
        ctl("play", B)
        check("YouTube resumes: the display moves to B automatically", shows(B, 4.0) and now()["mode"] == "auto")

        # ---- case C ---------------------------------------------------------------------------------------------------------------
        print("\n== case C: select paused YouTube, press play on it")
        set_state(B, False)
        set_state(A, True)
        check("start: A shows (playing), B paused", shows(A) and states()[B] == "paused")
        http("POST", f"/control/source-next?from={A}")
        check("manual on B", now()["app"] == B)
        r = ctl("playpause", B)
        check("play pressed on the board resumes THAT session", r.get("ok") and r.get("app") == B)
        wait_for(lambda: states().get(B) == "playing", 4.0)
        stayed = True
        for _ in range(8):
            time.sleep(1.0)
            stayed &= now().get("app") == B
        check("B now plays, A plays too: the display remains on B", stayed and states()[A] == "playing" and states()[B] == "playing")
        time.sleep(14.0)
        check("... and keeps it past the idle time (the selected session is the playing one)", now().get("app") == B)

        # ---- case D ---------------------------------------------------------------------------------------------------------------
        print("\n== case D: the selected session disappears")
        set_state(B, False)
        set_state(A, True)
        shows(A)
        http("POST", f"/control/source-next?from={A}")
        check("manual on B", now()["app"] == B and now()["mode"] == "manual")
        fakes.stop_fake(procs.pop(B))
        check("B closes: the display falls back to automatic (A) at once", shows(A, 4.0) and now()["mode"] == "auto" and sources()["mode"] == "auto", f"{now().get('app')} {sources().get('mode')}")
        check("... and B is no longer offered", B not in states())
        r = http("POST", f"/control/source-next?from={A}")
        check("with a single source a swipe does nothing", r.get("ok") and r.get("changed") is False, json.dumps(r)[:80])
        r = ctl("playpause", B)
        check("a command aimed at the closed session is refused, not redirected", r.get("ok") is False, json.dumps(r)[:90])

        # ---- case E ---------------------------------------------------------------------------------------------------------------
        print("\n== case E: several paused sessions still cycle")
        start(B, paused=True)
        time.sleep(1.0)
        set_state(A, False)
        set_state(B, False)
        order = [s["app"] for s in sources()["sources"]]
        here = now()["app"]
        seen = []
        ok = True
        for i in range(4):
            r = http("POST", f"/control/source-next?from={here}")
            expect = order[(order.index(here) + 1) % len(order)]
            ok &= r.get("app") == expect and now()["app"] == expect
            here = expect
            seen.append(here)
        check("vertical swipes cycle between the paused sessions", ok, " -> ".join(seen))
        time.sleep(22.0)
        check("with nothing playing the selection is simply kept (no idle return)", now()["app"] == here)

        # ---- Spotify-only shuffle / repeat -------------------------------------------------------------------------------------
        print("\n== Spotify-only shuffle / repeat (A is treated as Spotify by the dev flag, B is not)")
        set_state(A, True)
        swipe_to(A)
        n = now()
        check("the Spotify session reports spotify + shuffle / repeat state", n["spotify"] is True and "shuffle" in n and n["can_shuffle"] and n["can_repeat"], json.dumps({k: n.get(k) for k in ("spotify", "shuffle", "repeat", "can_shuffle", "can_repeat")}))
        sh0 = n["shuffle"]
        r = ctl("shuffle", A)
        check("shuffle toggles and the answer carries the new state", r.get("ok") and r.get("shuffle") == (not sh0) and r["now"]["shuffle"] == (not sh0), f"{sh0} -> {r.get('shuffle')}")
        r = ctl("shuffle", A)
        check("... and toggles back", r.get("ok") and r.get("shuffle") == sh0)
        modes = []
        for _ in range(4):
            r = ctl("repeat", A)
            modes.append(r.get("repeat"))
        check("repeat cycles off -> list -> track -> off", modes[:3] == [1, 2, 0] or modes == [(m0 + 1) % 3 for m0 in range(3)] or len(set(modes)) == 3, str(modes))
        r = ctl("repeat-track", A)
        check("an explicit repeat mode can be set (track)", r.get("ok") and r.get("repeat") == 2 and now()["repeat"] == 2)
        r = ctl("repeat-off", A)
        check("... and off", r.get("ok") and r.get("repeat") == 0)
        ctl("shuffle-on", A)
        ctl("repeat-list", A)
        ctl("next", A)
        time.sleep(1.5)
        n = now()
        check("shuffle / repeat state survives a track change", n["shuffle"] is True and n["repeat"] == 1 and n["spotify"], json.dumps({k: n.get(k) for k in ("title", "shuffle", "repeat")}))
        ctl("shuffle-off", A)
        ctl("repeat-off", A)

        swipe_to(B)
        n = now()
        check("the non-Spotify session reports no shuffle / repeat", n["app"] == B and n["spotify"] is False and n["can_shuffle"] is False and n["can_repeat"] is False)
        for action in ("shuffle", "repeat", "repeat-track", "shuffle-on"):
            r = ctl(action, B)
            check(f"{action} on the non-Spotify session is refused", r.get("ok") is False and "Spotify" in r.get("error", ""), r.get("error", ""))
    finally:
        for p in list(procs.values()):
            fakes.stop_fake(p)
        comp.terminate()
    print(f"\n{len(failures)} failure(s)" + (": " + "; ".join(failures) if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

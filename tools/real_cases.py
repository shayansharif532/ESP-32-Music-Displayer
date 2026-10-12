#!/usr/bin/env python3
"""Cases A-E of the auto-follow model with the owner's REAL Spotify and Chrome / YouTube sessions (no fakes), through the companion on
port 8765. It presses play / pause on the real apps a few times, so it refuses to start unless BOTH are paused right now (the owner is
not listening / watching), mutes both apps' own Windows volume for the duration (restored at the end), and puts every play state
back as it found it.

    tools\\venv\\Scripts\\python.exe tools\\real_cases.py
"""
import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8765"
SP, CH = "Spotify.exe", "chrome.exe"
failures: list[str] = []


def http(method, path):
    req = urllib.request.Request(BASE + path, method=method, headers={"X-Desk-Client": "real-cases"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"http_error": exc.code}


def hop(frm, direction="source-next"):
    """One step along the ring of SESSIONS: the synthetic idle-screen item at the end of the ring (ADR-008) is stepped over."""
    r = http("POST", f"/control/{direction}?from={frm}")
    if r.get("app") == "idle":
        r = http("POST", f"/control/{direction}?from=idle")
    return r


def now():
    return http("GET", "/now-playing")


def states():
    return {s["app"]: s["state"] for s in http("GET", "/sources").get("sources", [])}


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""), flush=True)
    if not ok:
        failures.append(name)
    return ok


def wait_for(pred, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def shows(app, timeout=6.0):
    return wait_for(lambda: now().get("app") == app, timeout)


def mute_all():
    """Both apps' OWN Windows volume to 0 (a browser only has an audio session while it plays, so this is repeated after every play)."""
    for a in (SP, CH):
        http("POST", f"/volume?level=0&app={a}")


def set_state(app, playing):
    want = "playing" if playing else "paused"
    if states().get(app) != want:
        http("POST", f"/control/{'play' if playing else 'pause'}?app={app}")
        mute_all()
        wait_for(lambda: states().get(app) == want, 5.0)
        mute_all()


def main() -> int:
    st0 = states()
    if SP not in st0 or CH not in st0:
        print("needs both Spotify and a Chrome media session; found", st0)
        return 2
    if st0[SP] == "playing" or st0[CH] == "playing":
        print("refusing: something is playing right now (", st0, ") - the owner may be using it. Pause both first.")
        return 3
    vol0 = {a: http("GET", f"/volume?app={a}").get("volume") for a in (SP, CH)}
    print("start:", st0, "volumes", vol0)
    vol0 = {a: (v if v is not None else 100) for a, v in vol0.items()}   # a paused browser may have no audio session: it was at 100 %
    mute_all()                                                  # silence for the duration of the test
    http("POST", "/control/source-auto")
    try:
        print("\n== auto-follow before any swipe")
        set_state(SP, True)
        check("Spotify starts: the display shows Spotify", shows(SP) and now()["mode"] == "auto", now().get("app", ""))
        check("Spotify is identified and reports shuffle / repeat", now().get("spotify") is True and now().get("can_shuffle") and now().get("can_repeat"))
        set_state(SP, False)
        set_state(CH, True)
        check("Spotify paused, YouTube starts: the display moves to Chrome", shows(CH), now().get("app", ""))
        check("Chrome is not Spotify: no shuffle / repeat", now().get("spotify") is False and not now().get("can_shuffle"))

        print("\n== case B: YouTube playing, browse to Spotify, YouTube resumes")
        r = hop(CH)
        check("a swipe selects Spotify (paused)", r.get("app") == SP and now()["state"] == "paused" and now()["mode"] == "manual", f"{now().get('app')} {now().get('mode')}")
        time.sleep(2.0)
        check("manual browsing holds while YouTube plays", now()["app"] == SP)
        http("POST", f"/control/pause?app={CH}")
        wait_for(lambda: states().get(CH) == "paused", 5.0)
        time.sleep(1.0)
        check("YouTube paused: still Spotify", now()["app"] == SP)
        http("POST", f"/control/play?app={CH}")
        mute_all()
        check("YouTube resumes: the display moves to Chrome automatically", shows(CH, 5.0) and now()["mode"] == "auto", f"{now().get('app')} {now().get('mode')}")

        print("\n== case A: Spotify playing, browse to paused YouTube, Spotify starts")
        set_state(CH, False)
        set_state(SP, True)
        check("start: Spotify shows", shows(SP))
        hop(SP)
        check("a swipe selects Chrome (paused)", now()["app"] == CH and now()["mode"] == "manual" and now()["state"] == "paused")
        http("POST", f"/control/pause?app={SP}")
        wait_for(lambda: states().get(SP) == "paused", 5.0)
        time.sleep(1.0)
        check("Spotify paused: Chrome stays", now()["app"] == CH)
        http("POST", f"/control/play?app={SP}")
        mute_all()
        check("Spotify STARTS: back to auto, the display returns to Spotify", shows(SP, 5.0) and now()["mode"] == "auto", f"{now().get('app')} {now().get('mode')}")

        print("\n== case C: select paused YouTube, press play on it")
        hop(SP)
        check("manual on Chrome", now()["app"] == CH and now()["mode"] == "manual")
        r = http("POST", f"/control/playpause?app={CH}")
        mute_all()
        check("play resumes THAT session", r.get("ok") and r.get("app") == CH)
        wait_for(lambda: states().get(CH) == "playing", 5.0)
        stayed = True
        for _ in range(6):
            time.sleep(1.0)
            stayed &= now().get("app") == CH
        check("Chrome now plays (Spotify too): the display stays on Chrome", stayed and states()[SP] == "playing" and states()[CH] == "playing")

        print("\n== case E: both paused, swipes still cycle")
        set_state(SP, False)
        set_state(CH, False)
        here = now()["app"]
        seen = []
        ok = True
        for _ in range(4):
            r = hop(here)
            ok &= r.get("changed") is True and r.get("app") != here
            here = r.get("app")
            seen.append(here)
        check("vertical swipes cycle between the two paused sessions", ok, " -> ".join(seen))
    finally:
        http("POST", "/control/source-auto")
        for a, want in st0.items():
            if a in (SP, CH):
                set_state(a, want == "playing")
        for a, v in vol0.items():
            http("POST", f"/volume?level={v}&app={a}")
        print("\nrestored:", states(), "volumes", {a: http("GET", f"/volume?app={a}").get("volume") for a in (SP, CH)})
    print(f"\n{len(failures)} failure(s)" + (": " + "; ".join(failures) if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

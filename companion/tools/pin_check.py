"""Developer tool: live check of the pin rule "a manually selected session is not taken over by another app that plays".

    companion\\.venv\\Scripts\\python.exe companion\\tools\\pin_check.py --port 8790

Run against a SEPARATE companion instance (python -m desk_media_companion --port 8790 --no-discovery). It starts the silent fake
session (python.exe, playing) for ~15 s, pins a *paused* real source in the test instance, and verifies that the display of that
instance stays on it while the fake plays; then it stops the fake and checks that auto mode returns. Nothing real is played / paused.
Note: while the fake plays, any OTHER companion in automatic mode (the one serving the board) follows it - keep this short.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def call(base, method, path):
    req = urllib.request.Request(base + path, method=method, headers={"X-Desk-Client": "pin-check"})
    with urllib.request.urlopen(req, timeout=6) as r:
        return json.loads(r.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8790)
    args = ap.parse_args()
    base = f"http://127.0.0.1:{args.port}"
    call(base, "POST", "/control/source-auto")
    real = [s for s in call(base, "GET", "/sources")["sources"] if s["app"] != "python.exe"]
    if not real:
        print("no real source to pin; nothing to check")
        return 2
    target = ([s for s in real if s["state"] == "paused"] or real)[0]["app"]      # a paused one if there is one (the strongest case)
    fake = subprocess.Popen([os.path.join(ROOT, "companion", ".venv", "Scripts", "python.exe"),
                             os.path.join(ROOT, "companion", "tools", "fake_media_session.py"), "--art-dir", os.path.join(ROOT, "logs", "test-art2")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ok = False
    try:
        for _ in range(20):
            time.sleep(0.5)
            if any(s["app"] == "python.exe" and s["state"] == "playing" for s in call(base, "GET", "/sources")["sources"]):
                break
        auto = call(base, "GET", "/now-playing")["app"]
        print(f"automatic mode while the fake plays shows: {auto}")
        for _ in range(6):                                           # walk the ring to the paused target
            if call(base, "GET", "/now-playing")["app"] == target:
                break
            call(base, "POST", "/control/source-next?from=" + call(base, "GET", "/now-playing")["app"])
        stayed = True
        for i in range(8):
            time.sleep(1.0)
            n = call(base, "GET", "/now-playing")
            stayed &= n["app"] == target and n["pinned"] is True
        states = {s["app"]: s["state"] for s in call(base, "GET", "/sources")["sources"]}
        print(f"pinned {target} (paused) for 8 s while {[a for a, s in states.items() if s == 'playing']} played: stayed = {stayed}")
        ok = stayed and states.get("python.exe") == "playing"
    finally:
        fake.terminate()
        time.sleep(3)
        after = call(base, "GET", "/now-playing")
        print(f"after the fake closed: mode = {call(base, 'GET', '/sources')['mode']}, displayed = {after['app']}")
        call(base, "POST", "/control/source-auto")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

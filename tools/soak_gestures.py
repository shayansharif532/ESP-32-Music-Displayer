#!/usr/bin/env python3
"""Development soak test: drives scripted swipes / taps / simulated key presses on the board (dev firmware + companion started
with --dev-tools) for a while, so a serial log captured at the same time can be checked for resets, panics and heap / stack drift.

    tools\\venv\\Scripts\\python.exe tools\\soak_gestures.py --seconds 300 [--companion http://127.0.0.1:8765]

Needs the silent fake test session running (companion\\tools\\fake_media_session.py --silent-audio). Every action except the two
vertical swipes can send next / previous / seek / play-pause / volume commands, so they only run while the fake session (python.exe)
is the one on screen. The vertical swipes just switch the shown source: when they have moved the display onto a real session
(Spotify, Chrome ...) the soak keeps swiping - selection only, nothing is played or changed - until the fake session is back.
"""
import argparse
import json
import random
import time
import urllib.request

FAKE = "python.exe"
SELECTION_ONLY = {"swipe_up", "swipe_down"}
ACTIONS = [
    ("swipe_right", 4), ("swipe_left", 4), ("swipe_up", 4), ("swipe_down", 3), ("swipe_diag", 2), ("swipe_bar", 2), ("swipe_bar_v", 2),
    ("swipe_text", 3), ("touch_cover", 3), ("touch_play", 2), ("touch_shuffle", 2), ("touch_repeat", 2), ("key_up", 3), ("key_down", 3), ("keyhold_down", 1), ("keyhold_up", 1),
]


def call(base: str, path: str, post: bool = False) -> dict:
    req = urllib.request.Request(base + path, method="POST" if post else "GET", headers={"X-Desk-Client": "soak"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=300)
    ap.add_argument("--companion", default="http://127.0.0.1:8765")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()
    rnd = random.Random(args.seed)
    pool = [name for name, weight in ACTIONS for _ in range(weight)]
    end = time.time() + args.seconds
    done: dict[str, int] = {}
    returned = 0
    last_selection = 0.0
    while time.time() < end:
        app = call(args.companion, "/now-playing").get("app", "")
        if app != FAKE:
            n = call(args.companion, "/now-playing").get("sources", 0)
            if n < 2:
                print(f"stopping: the fake session is not on screen ({app!r}) and there is no other source to swipe to")
                break
            call(args.companion, "/debug/swipe_up", post=True)           # selection only: walk back to the fake session
            done["swipe_up(return)"] = done.get("swipe_up(return)", 0) + 1
            last_selection = time.time()
            time.sleep(3.2)
            returned += 1
            continue
        if time.time() - last_selection < 3.0:                            # let the display catch up with the new source first
            time.sleep(3.0)
        action = rnd.choice(pool)
        call(args.companion, f"/debug/{action}", post=True)
        done[action] = done.get(action, 0) + 1
        if action in SELECTION_ONLY:
            last_selection = time.time()
        time.sleep(rnd.uniform(1.6, 2.8) + (3.2 if action.startswith("keyhold") else 0))
    print("actions sent:", sum(done.values()), done)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Development soak test: drives scripted swipes / taps / simulated key presses on the board (dev firmware + companion started
with --dev-tools) for a while, so a serial log captured at the same time can be checked for resets, panics and heap / stack drift.

    tools\\venv\\Scripts\\python.exe tools\\soak_gestures.py --seconds 300 [--companion http://127.0.0.1:8765]

Refuses to run unless the displayed session is the silent fake test session (python.exe): every action here can send next /
previous / seek / play-pause / volume commands.
"""
import argparse
import json
import random
import time
import urllib.request

ACTIONS = [
    ("swipe_right", 4), ("swipe_left", 4), ("swipe_up", 2), ("swipe_diag", 2), ("swipe_bar", 2), ("swipe_text", 3),
    ("touch_cover", 3), ("touch_play", 2), ("key_up", 3), ("key_down", 3), ("keyhold_down", 1), ("keyhold_up", 1),
    ("overlay_none_shot_off", 0),
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
    last_check = 0.0
    while time.time() < end:
        if time.time() - last_check > 20:
            app = call(args.companion, "/now-playing").get("app", "")
            if app != "python.exe":
                print(f"stopping: the displayed session is {app!r}, not the fake test session")
                break
            last_check = time.time()
        action = rnd.choice(pool)
        call(args.companion, f"/debug/{action}", post=True)
        done[action] = done.get(action, 0) + 1
        time.sleep(rnd.uniform(1.4, 2.6) + (3.2 if action.startswith("keyhold") else 0))
    print("actions sent:", sum(done.values()), done)


if __name__ == "__main__":
    main()

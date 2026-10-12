#!/usr/bin/env python3
"""Development tool: captures the synthetic-cover evidence pictures for docs/milestones/M8c (source pill, paused source, landscape
card) from the real display, using the silent fake session next to whatever real sessions exist.

    tools\\venv\\Scripts\\python.exe tools\\evidence_sources.py logs\\shots\\m8c

Only selection swipes touch real sessions; everything that presses a control runs while the fake session is on screen.
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
import session_test as st  # noqa: E402

ROOT = st.ROOT
PYT = os.path.join(ROOT, "tools", "venv", "Scripts", "python.exe")
SHOT = os.path.join(ROOT, "tools", "screenshot.py")


def shot(name: str, out_dir: str, *extra: str) -> str:
    path = os.path.join(out_dir, name + ".png")
    subprocess.run([PYT, SHOT, "--out", path, *extra], check=True, cwd=ROOT)
    return path


def to_title(prefix: str, tries: int = 14) -> bool:
    for _ in range(tries):
        if st.now().get("title", "").startswith(prefix):
            return True
        st.act("swipe_left")
    return st.now().get("title", "").startswith(prefix)


def main(out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    st.http("POST", "/control/source-auto")
    st.start_fake()
    time.sleep(3)
    try:
        # land on Chrome first so that one swipe up goes Chrome -> Python and the pill shows the new source
        for _ in range(4):
            if st.now().get("app") == "chrome.exe":
                break
            st.act("swipe_up")
        files = [shot("a_pill_after_swipe", out_dir, "--do", "swipe_up_shot")]          # pill: "Python", dots 2/3
        time.sleep(1.5)
        assert st.now().get("app") == "python.exe", st.now()
        st.act("touch_play")                                                              # pause the fake session
        time.sleep(2.0)
        files.append(shot("b_paused_source", out_dir))                                    # play icon, own timeline
        st.act("touch_play")                                                              # resume it
        to_title("8 wide")
        time.sleep(2.5)
        files.append(shot("c_landscape_card", out_dir))
        time.sleep(1.0)
        print("\n".join(files))
    finally:
        st.stop_fake()
        st.http("POST", "/control/source-auto")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "logs", "shots", "m8c"))

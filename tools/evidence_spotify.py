#!/usr/bin/env python3
"""Development tool: captures the synthetic-cover evidence pictures for docs/milestones/M8d (Spotify shuffle / repeat buttons).

    tools\\venv\\Scripts\\python.exe tools\\evidence_spotify.py logs\\shots\\m8d

Needs the board with dev firmware and a companion started with  --dev-tools --treat-as-spotify python.exe , so that the silent fake
test session (synthetic cover) counts as "Spotify" and shows the buttons. The owner's real sessions are only ever *selected*.
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


def shot(name: str, out_dir: str) -> str:
    path = os.path.join(out_dir, name + ".png")
    subprocess.run([PYT, SHOT, "--out", path], check=True, cwd=ROOT)
    return path


def main(out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    st.http("POST", "/control/source-auto")
    st.start_fake()
    try:
        time.sleep(3)
        assert st.select("python.exe"), "the fake session did not become the displayed one"
        assert st.now().get("spotify") is True, "start the companion with --treat-as-spotify python.exe"
        if st.now()["shuffle"]:                                        # normalise first: shuffle off, repeat off
            st.act("touch_shuffle")
        while st.now()["repeat"] != 0:
            st.act("touch_repeat")
        st.act("touch_shuffle")                                        # shuffle on, repeat off
        time.sleep(1.0)
        files = [shot("a_shuffle_on_repeat_off", out_dir)]
        st.act("touch_repeat")                                         # repeat the playlist
        time.sleep(1.0)
        files.append(shot("b_repeat_playlist", out_dir))
        st.act("touch_repeat")                                         # repeat the track
        time.sleep(1.0)
        files.append(shot("c_repeat_track", out_dir))
        print("\n".join(files))
    finally:
        st.stop_fake()
        st.http("POST", "/control/source-auto")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "logs", "shots", "m8d"))

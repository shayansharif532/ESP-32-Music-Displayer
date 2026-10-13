"""Captures the documentation demo from the real board: docs/screenshots/demo.gif and docs/screenshots/pairing-prompt.png.

    companion\\.venv\\Scripts\\python.exe tools\\demo_capture.py [--port COM5] [--out docs\\screenshots]

Everything shown is synthetic: two silent fake media sessions (companion/tools/fake_media_session.py with generated covers and fictional
tracks), the built-in idle themes, and a made-up PC name on the pairing prompt. Nothing from a real music library, desktop or network.

Needs: a development firmware build (-DAPP_DEV_TOOLS=1), the normal companion stopped, a paired board (the pairing prompt is captured by
forgetting the pairing and pairing again, so the board is paired again at the end), and logs/test-art2 (companion/tools/make_test_covers.py).
"""
import argparse
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import idle_test as it  # noqa: E402
import fakes  # noqa: E402
import screenshot as shot  # noqa: E402
import serial_capture as sc  # noqa: E402
from PIL import Image  # noqa: E402

from desk_media_companion import sources  # noqa: E402

A, B, LOGS, wait_for = it.A, it.B, it.LOGS, it.wait_for


def grab_after_action(board, comp, action, name):
    """Runs a '<action>_shot' dev action and returns the picture the board sends while the effect is visible."""
    out = os.path.join(LOGS, name)
    for _ in range(3):
        mark = board.mark()
        comp.act(action)
        if board.wait(r"SHOT_END", mark, 45.0) and shot.assemble([ln for _t, ln in board.since(mark)], out) == 0:
            return Image.open(out).convert("RGB")
    raise SystemExit("no screenshot for " + action)


def settle(board, comp, name):
    img = board.screenshot(comp.monitor, name)
    if img is None:
        raise SystemExit("no screenshot " + name)
    return img


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="auto")
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "screenshots"))
    ap.add_argument("--pc-name", default="Desk PC", help="the PC name shown on the pairing prompt")
    args = ap.parse_args()
    os.makedirs(LOGS, exist_ok=True)
    os.makedirs(args.out, exist_ok=True)
    port = sc.detect_port() if args.port == "auto" else args.port
    data_dir = tempfile.mkdtemp(prefix="demo-")
    board = it.Board(port)
    comp = it.Companion(os.path.join(data_dir, "idle"))
    procs = {}
    frames = []                                                     # (image, milliseconds)
    try:
        comp.store.update(theme="dark", delay_s=4, text="", show_clock=True, clock_24h=True, accent="#4DA3FF")
        sources._KNOWN["python"] = "Music app"                       # the fake sessions are renamed interpreters: give them neutral names
        sources._KNOWN["deskfakeb"] = "Video app"
        for app in (A, B):
            procs[app] = fakes.start_fake(app, paused=True, log_path=os.path.join(LOGS, f"demo-fake-{app}.log"))
        wait_for(lambda: A in comp.states() and B in comp.states(), 25.0, 0.5)
        comp.monitor.auto_source()
        for app, skips in ((A, 2), (B, 4)):                          # different synthetic covers per session (track 3 and track 5 of the set)
            for _ in range(skips):
                comp.monitor.control("next", None, app)
                time.sleep(1.2)
        comp.set_state(B, False)
        comp.set_state(A, True)
        time.sleep(6.0)
        frames.append((settle(board, comp, "demo-1-player.png"), 2200))
        frames.append((grab_after_action(board, comp, "swipe_up_shot", "demo-2-pill-source.png"), 1500))
        time.sleep(2.0)
        frames.append((settle(board, comp, "demo-3-second-source.png"), 2000))
        frames.append((grab_after_action(board, comp, "swipe_up_shot", "demo-4-pill-idle.png"), 1500))
        time.sleep(3.0)
        frames.append((settle(board, comp, "demo-5-idle.png"), 2000))
        comp.store.update(theme="neon", text="Welcome back", accent="#FF4FA3")
        wait_for(lambda: comp.counts.get("idle", 0) > 0, 20.0)
        time.sleep(4.0)
        frames.append((settle(board, comp, "demo-6-idle-neon.png"), 2200))
        comp.store.update(theme="dark", text="", accent="#4DA3FF")
        time.sleep(4.0)
        comp.act("swipe_down")
        time.sleep(2.5)
        comp.monitor.auto_source()
        comp.set_state(B, True)                                     # another session starts playing: auto-follow
        time.sleep(4.0)
        frames.append((settle(board, comp, "demo-7-autofollow.png"), 2200))
        comp.set_state(B, False)
        comp.set_state(A, False)
        comp.monitor.auto_source()

        gif = os.path.join(args.out, "demo.gif")
        pal = [im.quantize(256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.FLOYDSTEINBERG) for im, _ms in frames]
        pal[0].save(gif, save_all=True, append_images=pal[1:], duration=[ms for _im, ms in frames], loop=0, optimize=True, disposal=2)
        print("wrote", gif, os.path.getsize(gif) // 1024, "KB,", len(frames), "frames")

        # the pairing prompt with a made-up PC name (forget, open the window, let the display prompt, photograph, complete the pairing)
        comp.pairing.name = args.pc_name
        comp.pairing.forget()
        time.sleep(6.0)
        mark = board.mark()
        comp.pairing.open_window()
        if not board.wait(r"pairing code shown", mark, 40.0):
            raise SystemExit("the display did not show the pairing prompt")
        time.sleep(1.0)
        out = os.path.join(args.out, "pairing-prompt.png")
        for _ in range(3):
            mark = board.mark()
            comp.monitor.request_debug("pair_shot")
            if board.wait(r"SHOT_END", mark, 45.0) and shot.assemble([ln for _t, ln in board.since(mark)], out) == 0:
                break
        comp.pairing.pc_confirm()
        time.sleep(0.5)
        comp.act("touch_pair")
        print("paired again:", wait_for(lambda: comp.pairing.paired, 25.0))
    finally:
        for p in procs.values():
            fakes.stop_fake(p)
        try:
            comp.stop_http()
        except Exception:
            pass
        board.stop = True
        shutil.rmtree(data_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

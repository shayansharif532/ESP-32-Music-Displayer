#!/usr/bin/env python3
"""Capture what the desk display is actually showing, as a PNG (development tool).

Needs: a firmware built with  .\\tools\\idf.ps1 -DAPP_DEV_TOOLS=1 build  and a companion started with --dev-tools.
The companion hands a one-shot "screenshot" request to the display with its next /now-playing; the display renders its LVGL
screen to RAM and sends it over the USB serial console as base64 lines, which this script reassembles (and CRC-checks).

    tools\\venv\\Scripts\\python.exe tools\\screenshot.py --out logs\\shot.png
    ... --tap next          also press the on-screen control first (play|next|prev|art), then capture
    ... --do swipe_right    scripted virtual-finger gesture / simulated side-button press first (see --help), then capture
    ... --no-trigger        only listen (something else asked for the screenshot)
    ... --from-log FILE     assemble the picture from a saved serial log
"""
import argparse
import base64
import re
import sys
import time
import json
import urllib.request
import zlib

from PIL import Image

COMPANION = "http://127.0.0.1:8765"
# Dev-firmware actions (see firmware/main/dev_tools.c). Almost every one can reach a media control, so they are guarded: they only
# run while the displayed session is the fake test session. swipe_up / swipe_down only change WHICH session is shown (the vertical
# swipe selects a media source; nothing is played, paused, skipped or seeked), so they are allowed anywhere.
DEV_ACTIONS = ["swipe_right", "swipe_left", "swipe_up", "swipe_down", "swipe_diag", "swipe_bar", "swipe_bar_v", "swipe_text",
               "touch_cover", "touch_play", "key_up", "key_down", "keyhold_up", "keyhold_down",
               "swipe_right_shot", "swipe_left_shot", "swipe_up_shot", "swipe_down_shot", "key_up_shot", "key_down_shot",
               "overlay_none_shot", "overlay_pc_shot", "overlay_muted_shot", "overlay_vol_shot"]
SELECTION_ONLY = {"swipe_up", "swipe_down", "swipe_up_shot", "swipe_down_shot"}
CHUNK = 192
NL = bytes([10])

# A log line from another task can be glued in front of a picture line, so search instead of anchoring.
RE_BEGIN = re.compile(r"SHOT_BEGIN (\d+) (\d+) (\d+) (\d+)")
RE_LINE = re.compile(r"(?:^|[^A-Za-z0-9])S(\d+) ([A-Za-z0-9+/=]+)\s*$")
RE_END = re.compile(r"SHOT_END (\d+)")


def post_debug(base: str, action: str) -> None:
    req = urllib.request.Request(f"{base}/debug/{action}", method="POST", headers={"X-Desk-Client": "screenshot-tool"})
    with urllib.request.urlopen(req, timeout=5) as r:
        r.read()


def media_app(base: str) -> str:
    with urllib.request.urlopen(f"{base}/now-playing", timeout=5) as r:
        return json.load(r).get("app", "")


def rgb565_to_image(raw: bytes, w: int, h: int, stride: int) -> Image.Image:
    r5 = [(i << 3) | (i >> 2) for i in range(32)]
    g6 = [(i << 2) | (i >> 4) for i in range(64)]
    out = bytearray(w * h * 3)
    o = 0
    for y in range(h):
        row = raw[y * stride:y * stride + w * 2]
        for x in range(w):
            p = row[2 * x] | (row[2 * x + 1] << 8)
            out[o] = r5[(p >> 11) & 31]
            out[o + 1] = g6[(p >> 5) & 63]
            out[o + 2] = r5[p & 31]
            o += 3
    return Image.frombytes("RGB", (w, h), bytes(out))


def assemble(text_lines, out_path: str) -> int:
    """Parse SHOT_* lines (from a live port or a saved log) and write the PNG."""
    header = None
    lines: dict[int, bytes] = {}
    crc_expected = None
    for ln in text_lines:
        m = RE_BEGIN.search(ln)
        if m:
            header = tuple(int(v) for v in m.groups())
            lines.clear()
            continue
        if header is None:
            continue
        m = RE_END.search(ln)
        if m:
            crc_expected = int(m.group(1))
            break
        m = RE_LINE.search(ln)
        if m:
            try:
                lines[int(m.group(1))] = base64.b64decode(m.group(2), validate=True)
            except Exception:
                pass
    if header is None or crc_expected is None:
        print("no complete screenshot found (firmware built with -DAPP_DEV_TOOLS=1? companion started with --dev-tools?)",
              file=sys.stderr)
        return 2
    w, h, stride, _cf = header
    total = stride * h
    n_lines = (total + CHUNK - 1) // CHUNK
    missing = [i for i in range(n_lines) if i not in lines]
    data = b"".join(lines.get(i, bytes(CHUNK)) for i in range(n_lines))[:total]
    crc_ok = (zlib.crc32(data) & 0xFFFFFFFF) == crc_expected
    rgb565_to_image(data, w, h, stride).save(out_path)
    print(f"saved {out_path}: {w}x{h}, {'CRC ok' if crc_ok else 'CRC MISMATCH'}, missing lines: {len(missing)}")
    return 0 if crc_ok else 3


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="auto")
    ap.add_argument("--out", default="screenshot.png")
    ap.add_argument("--companion", default=COMPANION, help="companion base URL used to trigger the capture")
    ap.add_argument("--tap", choices=["play", "next", "prev", "art"], help="press this control first")
    ap.add_argument("--do", choices=DEV_ACTIONS,
                    help="perform a scripted touch / key action on the board first (a virtual finger through the real LVGL input path, "
                         "or a simulated side-button press), then capture. Refused unless the active session is the fake test session")
    ap.add_argument("--no-trigger", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="allow --tap play/next/prev on a REAL media session (default: only on the fake test session, python.exe)")
    ap.add_argument("--reset", action="store_true", help="reset the board first (then wait for it to show a track)")
    ap.add_argument("--timeout", type=float, default=45.0)
    ap.add_argument("--from-log", help="assemble the picture from a saved serial log instead of using the port")
    args = ap.parse_args()

    if args.from_log:
        with open(args.from_log, encoding="utf-8", errors="ignore") as f:
            return assemble([ln.strip() for ln in f], args.out)

    import serial_capture as sc          # same folder: port detection + the DTR/RTS rules for the native USB console

    port = sc.detect_port() if args.port == "auto" else args.port
    ser = sc.open_port(port, args.reset)
    if args.reset:
        time.sleep(9)
    ser.reset_input_buffer()
    try:
        if args.tap:
            if args.tap != "art" and not args.force and media_app(args.companion) != "python.exe":
                print("refusing to press a media control: the active session is not the fake test session "
                      "(this would control real media). Use --force if you really want that.", file=sys.stderr)
                return 4
            post_debug(args.companion, f"tap_{args.tap}")
            time.sleep(3.0)                                   # let the new track's artwork load first
            ser.reset_input_buffer()
        if args.do:
            if not args.force and args.do not in SELECTION_ONLY and media_app(args.companion) != "python.exe":
                print("refusing: this action could send a real command (next / previous / seek / play / volume) to whatever the "
                      "PC is playing. It only runs against the fake test session. Use --force if you really want that.", file=sys.stderr)
                return 4
            post_debug(args.companion, args.do)
            if args.do.endswith("_shot"):                     # the board photographs itself while the effect is on screen
                args.no_trigger = True
            else:
                time.sleep(3.5)                               # the gesture takes under a second; then the new track's artwork loads
                ser.reset_input_buffer()
        rc = 2
        for attempt in range(3):                              # a dropped serial byte fails the CRC: just ask again
            if not args.no_trigger:
                post_debug(args.companion, "screenshot")
            deadline = time.time() + args.timeout
            buf = b""
            text: list[str] = []
            finished = False
            while time.time() < deadline and not finished:
                chunk = ser.read(8192)
                if not chunk:
                    continue
                buf += chunk
                *done, buf = buf.split(NL)
                for raw in done:
                    ln = raw.strip().decode("ascii", errors="ignore")
                    text.append(ln)
                    if RE_END.search(ln):
                        finished = True
            rc = assemble(text, args.out)
            if rc == 0 or args.no_trigger:
                break
            print(f"attempt {attempt + 1} failed (rc {rc}), retrying", file=sys.stderr)
            time.sleep(2.0)
            ser.reset_input_buffer()
        return rc
    finally:
        ser.close()


if __name__ == "__main__":
    sys.exit(main())

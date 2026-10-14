#!/usr/bin/env python3
"""Board-in-the-loop test of the version check over USB: incompatible peers get a clear message, never silence, and recover without pairing again.

    companion\\.venv\\Scripts\\python.exe tools\\usb_versions_test.py

Needs what tools\\usb_test.py needs (dev firmware, -DAPP_NO_WIFI=1, the real companion stopped, scratch data folder; DM_TEST_DATA_DIR keeps the pairing).
The companion under test pretends to be OLDER than the display needs (API 1) and then NEWER than the display speaks (minimum API 3); in both cases
the companion answers every route but /hello with 426, reports the right side as the one to update, and the display must show the matching screen
("Update DeskMedia Companion" / "Update the display") on its glass (compared with the picture the `link_n` development action draws for the same
screen). Afterwards the real versions are restored: the display must carry on by itself, still paired.
"""
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import idle_test as it  # noqa: E402
import usb_harness as uh  # noqa: E402

from PIL import Image, ImageChops, ImageStat  # noqa: E402

import desk_media_companion.usblink as usblink  # noqa: E402

check, wait_for, LOGS = it.check, it.wait_for, it.LOGS
USB_DIR = os.path.join(LOGS, "usb")


def close_to(img: Image.Image, reference_png: str, limit: float = 6.0) -> tuple[bool, str]:
    """Mean absolute difference of two screens (the reference was drawn by the `link_n` action: same overlay, another moment: only a clock-free
    static screen, so they agree closely)."""
    if not os.path.isfile(reference_png):
        return False, "no reference picture " + os.path.basename(reference_png)
    ref = Image.open(reference_png).convert("RGB")
    diff = ImageChops.difference(img.convert("RGB"), ref)
    mean = sum(ImageStat.Stat(diff).mean) / 3
    return mean <= limit, f"mean difference {mean:.2f}"


def main() -> int:
    kept = os.environ.get("DM_TEST_DATA_DIR", "")
    data_dir = kept or tempfile.mkdtemp(prefix="usb-versions-")
    os.makedirs(data_dir, exist_ok=True)
    board = uh.UsbBoard()
    comp = uh.UsbCompanion(os.path.join(data_dir, "idle"), board)
    real = (usblink.API_VERSION, usblink.MIN_API_VERSION)
    try:
        print("== 0. connected and paired")
        ok = uh.ensure_paired(board, comp)
        check("0 the display is paired and served", ok)
        if not ok:
            return 2

        for name, versions, update_needed, headline, reference in (
                ("companion too old", (1, 1), "app", "Update DeskMedia Companion", "link-5-updatecompanion.png"),
                ("display too old", (3, 3), "display", "Update the display", "link-6-updatefirmware.png")):
            print(f"\n== {name}: the companion claims API {versions[0]}, minimum {versions[1]} (the display speaks 2)")
            usblink.API_VERSION, usblink.MIN_API_VERSION = versions
            comp.stop_http()
            time.sleep(2.0)
            comp.start_http()                                                 # a new link: the ACK now carries the pretended versions
            ok = wait_for(lambda: comp.runtime.usb_status().get("state") == "incompatible", 40.0, 0.2)
            st = comp.runtime.usb_status()
            check(f"{name}: the companion reports the pair as incompatible", ok, f"state {st.get('state')}")
            check(f"{name}: and names the side that must be updated", st.get("update_needed") == update_needed, str(st.get("update_needed")))
            time.sleep(3.0)
            img = board.screenshot(comp.monitor, f"usb-versions-{update_needed}.png")
            if img is None:
                check(f"{name}: a screenshot of the display", False)
            else:
                same, detail = close_to(img, os.path.join(USB_DIR, reference))
                check(f"{name}: the display says \"{headline}\"", same, detail)

        print("\n== the real versions again: the display carries on by itself")
        usblink.API_VERSION, usblink.MIN_API_VERSION = real
        comp.stop_http()
        time.sleep(2.0)
        mark = board.mark()
        comp.start_http()
        ok = wait_for(lambda: comp.usb_state() == "connected", 40.0, 0.2)
        check("the link is back, authenticated, by itself", ok, f"state {comp.usb_state()}")
        check("... with the same pairing (no pairing prompt)", comp.pairing.paired and board.count(r"pairing code shown", mark) == 0)
        time.sleep(3.0)
        img = board.screenshot(comp.monitor, "usb-versions-recovered.png")
        if img is not None:
            same, detail = close_to(img, os.path.join(USB_DIR, "link-5-updatecompanion.png"), 6.0)
            check("the update screens are gone", not same, detail)
    finally:
        usblink.API_VERSION, usblink.MIN_API_VERSION = real
        try:
            comp.stop_http()
        except Exception:
            pass
        board.stop = True
        if not kept:
            shutil.rmtree(data_dir, ignore_errors=True)
    print(f"\n{len(it.failures)} failure(s)" + (": " + "; ".join(it.failures) if it.failures else ""))
    return 1 if it.failures else 0


if __name__ == "__main__":
    sys.exit(main())

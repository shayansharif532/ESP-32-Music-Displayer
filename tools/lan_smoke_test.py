#!/usr/bin/env python3
"""LAN regression for the USB-first firmware: the optional Wi-Fi / LAN transport still works.

    DM_TEST_DATA_DIR=<folder> companion\\.venv\\Scripts\\python.exe tools\\lan_smoke_test.py [--seconds 90]

Needs: the board flashed with a development build that HAS Wi-Fi credentials (firmware\\secrets.local.h present, no -DAPP_NO_WIFI=1), on the same
network as this PC, already paired with the companion whose data folder is DM_TEST_DATA_DIR (run tools\\usb_test.py with the same variable
first: the pairing key lives there), the real companion stopped (this test starts a LAN-only one on port 8765: Windows may need to allow it on
private networks once).

The companion under test runs with `--no-usb --lan --debug`, so the display cannot reach it over the cable and must use the LAN: the check is that
its requests arrive from a non-loopback address and are answered 200 (a signed request of a paired display), that the artwork is fetched, and that
the display's polling continues. Nothing else is touched; the fake media session is silent.
"""
import argparse
import os
import re
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "companion"))
sys.path.insert(0, os.path.join(ROOT, "companion", "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import fakes  # noqa: E402
import idle_test as it  # noqa: E402

check, wait_for = it.check, it.wait_for


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=90.0, help="how long to wait for the display to show up on the LAN")
    args = ap.parse_args()
    data_dir = os.environ.get("DM_TEST_DATA_DIR", "")
    if not data_dir or not os.path.isfile(os.path.join(data_dir, "pairing.bin")):
        print("DM_TEST_DATA_DIR must name the data folder of a companion the display is paired with (run tools\\usb_test.py with it first)")
        return 2
    log_path = os.path.join(data_dir, "companion.log")
    try:
        os.remove(log_path)
    except OSError:
        pass
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    fake = fakes.start_fake(it.A, paused=False, log_path=os.path.join(ROOT, "logs", "lan-fake-A.log"))
    comp = subprocess.Popen([fakes.VENV_PY, "-m", "desk_media_companion", "--no-usb", "--lan", "--port", "8765", "--bind", "0.0.0.0", "--dev-tools", "--debug",
                             "--no-tray", "--only-apps", f"{it.A},{it.B}", "--data-dir", data_dir, "--quiet"],
                            cwd=os.path.join(ROOT, "companion"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        def text() -> str:
            try:
                with open(log_path, encoding="utf-8", errors="replace") as f:
                    return f.read()
            except OSError:
                return ""

        def lan_requests(path: str, status: str = "200") -> int:
            rx = re.compile(r"DEBUG http: (\d+\.\d+\.\d+\.\d+) \"GET " + re.escape(path) + r"[^\"]* HTTP/1\.[01]\" " + status)
            return sum(1 for m in rx.finditer(text()) if m.group(1) not in ("127.0.0.1", "::1"))

        print("== the display reaches the LAN-only companion over Wi-Fi (USB is not available to it)")
        ok = wait_for(lambda: lan_requests("/now-playing") >= 3, args.seconds, 1.0)
        check("1 the display polls /now-playing from a LAN address and is answered 200 (signed requests of a paired display)", ok, f"{lan_requests('/now-playing')} requests")
        check("1 ... never refused (no 401 to a LAN client)", lan_requests("/now-playing", "401") == 0, f"{lan_requests('/now-playing', '401')} refusals")
        check("2 it fetches the artwork of the playing session", wait_for(lambda: lan_requests("/artwork") >= 1, 30.0, 1.0))
        before = lan_requests("/now-playing")
        time.sleep(10.0)
        check("3 and keeps polling (about once a second)", lan_requests("/now-playing") - before >= 6, f"{lan_requests('/now-playing') - before} in 10 s")
        check("4 the LAN transport is the one in use: the companion has no USB link", "USB transport started" not in text())
    finally:
        comp.terminate()
        try:
            comp.wait(10)
        except subprocess.TimeoutExpired:
            comp.kill()
        fakes.stop_fake(fake)
    print(f"\n{len(it.failures)} failure(s)" + (": " + "; ".join(it.failures) if it.failures else ""))
    return 1 if it.failures else 0


if __name__ == "__main__":
    sys.exit(main())

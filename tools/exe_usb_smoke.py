#!/usr/bin/env python3
"""The PACKAGED companion (DeskMediaCompanion.exe) against the real board over USB.

    python tools\\exe_usb_smoke.py <path to DeskMediaCompanion.exe> <data folder with the display's pairing>

Needs: the board (any firmware of this project that is paired with the companion whose data folder is given: run tools\\usb_test.py with
DM_TEST_DATA_DIR first), no other program holding its COM port. The exe runs windowless with `--debug --no-tray --quiet` and that data folder, so
nothing of the real installation is touched. Checks, from the exe's own log: it found the display's port by itself, the display answered, the
paired session was established without any pairing prompt, the display's log lines arrive, and the exe opened **no listening or UDP socket** (USB mode
must not trigger a firewall prompt). Then `--quit` ends it the way the installer does.
"""
import os
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import idle_test as it  # noqa: E402

check, wait_for = it.check, it.wait_for


def sockets_of(pid: int) -> list[str]:
    out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW).stdout
    found = []
    for line in out.splitlines():
        parts = line.split()
        # listening TCP sockets and UDP sockets are what a firewall asks about; the loopback pair an asyncio loop makes for itself (ESTABLISHED) is not one
        if len(parts) >= 4 and parts[-1] == str(pid) and parts[0] in ("TCP", "UDP") and (parts[0] == "UDP" or "LISTENING" in parts):
            found.append(f"{parts[0]} {parts[1]}")
    return found


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    exe, data = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
    if not os.path.isfile(exe) or not os.path.isfile(os.path.join(data, "pairing.bin")):
        print("need the exe and a data folder that holds the display's pairing (pairing.bin)")
        return 2
    log = os.path.join(data, "companion.log")
    try:
        os.remove(log)
    except OSError:
        pass
    proc = subprocess.Popen([exe, "--debug", "--no-tray", "--quiet", "--data-dir", data], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)

    def text() -> str:
        try:
            with open(log, encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return ""
    try:
        check("1 the exe finds the display's port by itself and the display answers", wait_for(lambda: "answered on COM" in text(), 40.0, 0.5))
        check("2 the paired session is established (no pairing prompt)", wait_for(lambda: "usb session established" in text(), 40.0, 0.5) and "pairing window opened" not in text())
        check("3 the display's own log lines arrive (--debug)", wait_for(lambda: "usb.display" in text() and "pc_media" in text(), 30.0, 0.5))
        time.sleep(3.0)
        check("4 USB mode opened no listening or UDP socket (no firewall prompt)", not sockets_of(proc.pid), str(sockets_of(proc.pid)))
        check("5 no error in the log", " ERROR " not in text(), "")
    finally:
        quit_proc = subprocess.run([exe, "--quit"], capture_output=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            proc.wait(20)
        except subprocess.TimeoutExpired:
            proc.kill()
        check("6 --quit ends it (the way the installer closes it)", proc.returncode is not None, f"quit exit {quit_proc.returncode}")
    print(f"\n{len(it.failures)} failure(s)" + (": " + "; ".join(it.failures) if it.failures else ""))
    return 1 if it.failures else 0


if __name__ == "__main__":
    sys.exit(main())

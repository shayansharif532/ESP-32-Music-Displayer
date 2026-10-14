"""Proves that a running DeskMediaCompanion.exe obeys `--quit` (what the installer uses to replace / remove a running copy) - with no
HTTP port at all, as in the USB-first product, and also with the optional LAN transport on.

    companion\\.venv\\Scripts\\python.exe tools\\quit_listener_test.py [path\\to\\DeskMediaCompanion.exe]

This one DOES start the packaged companion, so read what it avoids: it starts it with `--no-usb` (no serial port is probed or
opened - a display may be attached), `--no-tray` (no icon, no window) and its data folder, TEMP and LOCALAPPDATA in a throw-away
directory. It never takes the product's single-instance lock (`Local\\DeskMediaCompanion`: that one is taken only by instances that
use USB), and the quit event has a private name (DESK_MEDIA_QUIT_EVENT), so it can neither touch a running real companion nor be
touched by one. Scenario 1 opens no socket at all; scenario 2 adds `--lan` on 127.0.0.1:8799 only, without the discovery beacon
(never port 8765). Do not run it as part of the installer smoke test (that one must not start anything).

Checks: with nothing running `--quit` exits 0 and says so; scenario 1: the companion runs, listens on no TCP / UDP port, `--quit`
exits 0, the process is gone within a few seconds through the normal shutdown path (exit code 0, not the 6 s hard exit) and its log
says so; scenario 2: the same with the LAN transport serving /health; a second `--quit` is harmless.
"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import packaging_checks as pc  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DEFAULT_EXE = os.path.join(REPO, "companion", "dist", "DeskMediaCompanion", "DeskMediaCompanion.exe")
LAN_PORT = 8799
EVENT = "Local\\DeskMediaCompanion-Quit-Test-" + str(os.getpid())
SYNCHRONIZE = 0x00100000

fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def event_exists(name: str) -> bool:
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenEventW.restype = wintypes.HANDLE
    k.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k.OpenEventW(SYNCHRONIZE, False, name)
    if h:
        k.CloseHandle(h)
    return bool(h)


def sockets_of(pid: int) -> list[str]:
    """TCP listeners and UDP endpoints owned by the process (from netstat)."""
    out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW).stdout
    found = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[-1] == str(pid) and parts[0] in ("TCP", "UDP") and (parts[0] == "UDP" or "LISTENING" in parts):
            found.append(f"{parts[0]} {parts[1]}")
    return found


def scenario(exe: str, work: str, env: dict, label: str, extra: list[str], lan: bool) -> None:
    data = os.path.join(work, "data-" + label)
    proc = subprocess.Popen([exe, "--quiet", "--no-usb", "--no-tray", "--data-dir", data, *extra], env=env, cwd=work,
                            creationflags=subprocess.CREATE_NO_WINDOW, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        up = False
        for _ in range(80):
            if proc.poll() is not None:
                break
            if event_exists(EVENT):                       # the quit listener exists: the app is past start-up
                up = True
                break
            time.sleep(0.25)
        check(f"{label}: the companion started and is listening for --quit", up, f"exit {proc.poll()}")
        if not up:
            return
        time.sleep(1.5)                                   # let the transports settle before looking at sockets
        socks = sockets_of(proc.pid)
        if lan:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{LAN_PORT}/health", timeout=3) as resp:
                    served = resp.status == 200
            except Exception:
                served = False
            check(f"{label}: the LAN transport answers /health on 127.0.0.1:{LAN_PORT}", served, str(socks))
        else:
            check(f"{label}: no TCP listener and no UDP socket (no firewall prompt is possible)", not socks, str(socks))
        t = time.monotonic()
        r = pc.run_exe(exe, ["--quit"], env, timeout=60)
        took = time.monotonic() - t
        out = pc.read_text_result(r, work, "deskmedia-quit.txt")
        check(f"{label}: --quit exits 0 once the companion is gone", r.returncode == 0 and out == "stopped", f"exit {r.returncode}, {out!r}, {took:.1f} s")
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            pass
        check(f"{label}: the process ended through the normal shutdown path (exit code 0, inside the 6 s hard-exit grace)",
              proc.poll() == 0 and took < 5.5, f"exit {proc.poll()}, {took:.1f} s")
    finally:
        if proc.poll() is None:
            proc.kill()


def main() -> int:
    exe = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_EXE)
    if not os.path.isfile(exe):
        print(f"no such exe: {exe}", file=sys.stderr)
        return 2
    work = tempfile.mkdtemp(prefix="dm-quit-")
    env = pc.scrubbed_env(work, os.path.join(work, "data"))
    env["DESK_MEDIA_QUIT_EVENT"] = EVENT
    try:
        r = pc.run_exe(exe, ["--quit"], env)
        out = pc.read_text_result(r, work, "deskmedia-quit.txt")
        check("--quit with nothing running exits 0 and says so", r.returncode == 0 and out == "not running", f"exit {r.returncode}, {out!r}")

        scenario(exe, work, env, "1 default (no HTTP port)", [], lan=False)
        scenario(exe, work, env, "2 with --lan", ["--lan", "--bind", "127.0.0.1", "--port", str(LAN_PORT), "--no-discovery"], lan=True)

        import glob
        text = ""
        for log in sorted(glob.glob(os.path.join(work, "data-*", "companion.log"))):         # one data folder (and log) per scenario
            text += open(log, encoding="utf-8", errors="replace").read()
        check("the log says quits were requested", text.count("quit requested") == 2, f"{text.count('quit requested')} entries (logs in the sandbox)")
        check("neither run needed the hard exit", "did not stop" not in text)
        r = pc.run_exe(exe, ["--quit"], env)
        check("a second --quit is harmless", r.returncode == 0)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print()
    print("FAILED: " + "; ".join(fails) if fails else "all quit checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

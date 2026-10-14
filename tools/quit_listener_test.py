"""Proves that a running DeskMediaCompanion.exe obeys `--quit` (what the installer uses to replace / remove a running copy).

    companion\\.venv\\Scripts\\python.exe tools\\quit_listener_test.py [path\\to\\DeskMediaCompanion.exe]

This one DOES start the companion: the packaged exe, on 127.0.0.1:8799 only, without tray icon and without the discovery beacon, with
its data folder and LOCALAPPDATA in a throw-away directory and a private quit-event name (DESK_MEDIA_QUIT_EVENT), so it can neither
reach the display / network nor touch another running companion or the real data folder. It does not use port 8765. Do not run it
as part of the installer smoke test (that one must not start anything that listens).

Checks: the app comes up and serves /health on loopback; `--quit` exits 0; the process is gone within a few seconds (a clean exit through
the normal shutdown path, not the hard fallback); `--quit` with nothing running exits 0 and says so; a second `--quit` is harmless.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import packaging_checks as pc  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DEFAULT_EXE = os.path.join(REPO, "companion", "dist", "DeskMediaCompanion", "DeskMediaCompanion.exe")
PORT = 8799
EVENT = "Local\\DeskMediaCompanion-Quit-Test-" + str(os.getpid())

fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def main() -> int:
    exe = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_EXE)
    if not os.path.isfile(exe):
        print(f"no such exe: {exe}", file=sys.stderr)
        return 2
    work = tempfile.mkdtemp(prefix="dm-quit-")
    env = pc.scrubbed_env(work, os.path.join(work, "data"))
    env["DESK_MEDIA_QUIT_EVENT"] = EVENT
    proc = None
    try:
        t = time.monotonic()
        r = pc.run_exe(exe, ["--quit"], env)
        out = pc.read_text_result(r, work, "deskmedia-quit.txt")
        check("--quit with nothing running exits 0 and says so", r.returncode == 0 and out == "not running", f"exit {r.returncode}, {out!r}")

        proc = subprocess.Popen([exe, "--quiet", "--no-tray", "--no-discovery", "--bind", "127.0.0.1", "--port", str(PORT),
                                 "--data-dir", os.path.join(work, "data")], env=env, cwd=work, creationflags=subprocess.CREATE_NO_WINDOW,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        up = False
        for _ in range(60):
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2) as resp:
                    up = resp.status == 200
                    break
            except Exception:
                time.sleep(0.5)
        check("the companion started and answers /health on 127.0.0.1", up, f"exit {proc.poll()}")
        if not up:
            return 1

        t = time.monotonic()
        r = pc.run_exe(exe, ["--quit"], env, timeout=60)
        took = time.monotonic() - t
        out = pc.read_text_result(r, work, "deskmedia-quit.txt")
        check("--quit exits 0 once the companion is gone", r.returncode == 0 and out == "stopped", f"exit {r.returncode}, {out!r}, {took:.1f} s")
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            pass
        check("the process has ended", proc.poll() is not None)
        check("it ended through the normal shutdown path (exit code 0, well inside the 6 s hard-exit grace)", proc.poll() == 0 and took < 5.5,
              f"exit {proc.poll()}, {took:.1f} s")
        log = os.path.join(env["LOCALAPPDATA"], "DeskMediaCompanion", "companion.log")
        text = open(log, encoding="utf-8", errors="replace").read() if os.path.isfile(log) else ""
        check("the log says a quit was requested", "quit requested" in text, "(log in the sandbox)")
        check("it did not need the hard exit", "did not stop" not in text)

        r = pc.run_exe(exe, ["--quit"], env)
        check("a second --quit is harmless", r.returncode == 0)
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        shutil.rmtree(work, ignore_errors=True)
    print()
    print("FAILED: " + "; ".join(fails) if fails else "all quit checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

"""Test helper: starts silent fake media sessions, up to two at once.

Windows names a session after the executable that owns it, so a second fake source needs a second executable name: the first one
is the ordinary `python.exe`, the second one is a renamed copy of the Python interpreter (`logs/fake2/deskfakeb.exe`, created on
demand next to its DLLs; it runs with PYTHONHOME pointing at the real installation, nothing is installed or modified anywhere).

    from fakes import start_fake
    a = start_fake("python.exe", paused=True)       # session app id "python.exe"
    b = start_fake("deskfakeb.exe")                 # session app id "deskfakeb.exe"
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
VENV_PY = os.path.join(ROOT, "companion", ".venv", "Scripts", "python.exe")
FAKE = os.path.join(ROOT, "companion", "tools", "fake_media_session.py")
ART_DIR = os.path.join(ROOT, "logs", "test-art2")
SECOND_DIR = os.path.join(ROOT, "logs", "fake2")
SECOND_EXE = os.path.join(SECOND_DIR, "deskfakeb.exe")


def _python_home() -> str:
    with open(os.path.join(ROOT, "companion", ".venv", "pyvenv.cfg"), encoding="utf-8") as f:
        for line in f:
            if line.startswith("home"):
                return line.split("=", 1)[1].strip()
    raise RuntimeError("pyvenv.cfg has no home")


def _ensure_second_exe() -> None:
    if os.path.exists(SECOND_EXE):
        return
    home = _python_home()
    os.makedirs(SECOND_DIR, exist_ok=True)
    shutil.copy2(os.path.join(home, "python.exe"), SECOND_EXE)
    for dll in ("python3.dll", "python312.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
        if os.path.exists(os.path.join(home, dll)):
            shutil.copy2(os.path.join(home, dll), SECOND_DIR)


def start_fake(app: str = "python.exe", paused: bool = False, silent_audio: bool = True, log_path: str = "") -> subprocess.Popen:
    args = [FAKE, "--art-dir", ART_DIR] + (["--silent-audio"] if silent_audio else []) + (["--paused"] if paused else [])
    env = dict(os.environ)
    if app.lower() == "python.exe":
        exe = VENV_PY
    else:
        _ensure_second_exe()
        exe = SECOND_EXE
        env["PYTHONHOME"] = _python_home()
        env["PYTHONPATH"] = os.path.join(ROOT, "companion", ".venv", "Lib", "site-packages")
    out = open(log_path, "w") if log_path else subprocess.DEVNULL
    return subprocess.Popen([exe, *args], stdout=out, stderr=subprocess.STDOUT, env=env)


def stop_fake(proc) -> None:
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    print(sys.argv)

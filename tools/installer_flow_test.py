"""Lifecycle test of the installer script in a SANDBOX variant: install, upgrade, auto-start handling, running-copy handling, uninstall.

    companion\\.venv\\Scripts\\python.exe tools\\installer_flow_test.py        (needs Inno Setup 6 and PyInstaller in that Python)

Why a variant: the shipped installer writes the real uninstall entry, the real HKCU Run value "DeskMediaCompanion" and removes data in
%LOCALAPPDATA%\\DeskMediaCompanion. This test compiles installer\\DeskMediaCompanion.iss with ISCC /D overrides - another AppId, another
Run value name, another data folder, a tiny stand-in application (a PyInstaller exe that only records its command-line) - so every
install / upgrade / uninstall branch of the real script can be exercised without touching a real installation. Everything lives
in a throw-away folder; the real Run value, the real data folder and the real uninstall key are snapshot before and after and must not
change. (installer_smoke_test.py is the complementary test of the REAL installer and the REAL exe.)

Checked: fresh install with auto-start + launch; upgrade keeps data, replaces stale runtime files, asks the running copy to quit, keeps
the auto-start state the tray menu left (and /TASKS overrides it); the running-copy gate and /SKIPMUTEXCHECK; shortcuts;
uninstall keeps data / removes it with /DELETEDATA, removes the Run value only when it is ours; no reboot request.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import winreg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import packaging_checks as pc  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
INSTALLER_DIR = os.path.join(REPO, "installer")
SANDBOX_ID = "0F7C5B0A-0000-4000-8000-0000F10F7E57"
SANDBOX_KEY_ID = "{" + SANDBOX_ID + "}"
RUN_NAME = "DeskMediaCompanionFlowTest"
REAL_ID = "{73454063-9F9F-4C5A-9818-1D7B6B45C5F7}"
REAL_RUN = "DeskMediaCompanion"

FAKE_APP = '''import os, sys
calls = os.environ.get("FAKE_APP_CALLS") or os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "calls.txt")
with open(calls, "a", encoding="utf-8") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
sys.exit(0)
'''

fails: list[str] = []


current: "Sandbox | None" = None


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    if not ok:
        fails.append(name)
        if current is not None and current.last_log and os.path.isfile(current.last_log):      # what the last Setup / uninstall run logged
            lines = open(current.last_log, encoding="utf-8", errors="replace").read().splitlines()
            keep = [ln for ln in lines if "upgrade=" in ln or "Run" in ln or "Command line" in ln or "Asking" in ln or "Exception" in ln]
            for line in keep[-12:]:
                print("      | " + line)


def sha(path: str) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def shortcuts() -> list[str]:
    base = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs")
    return [os.path.join(base, f) for f in os.listdir(base) if f.lower() == "deskmedia companion.lnk"] if os.path.isdir(base) else []


class Sandbox:
    def __init__(self, work: str, setup: str) -> None:
        self.work, self.setup = work, setup
        self.install = os.path.join(work, "inst dir")
        self.data = os.path.join(work, "data")
        self.calls = os.path.join(work, "calls.txt")
        self.logs = 0
        self.last_log = ""
        self.env = dict(os.environ, FAKE_APP_CALLS=self.calls)

    def run(self, exe: str, flags: str, timeout: float = 300) -> int:
        self.logs += 1
        log = os.path.join(self.work, f"run{self.logs}.log")
        self.last_log = log
        cmd = f'"{exe}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART {flags} /LOG="{log}"'
        return subprocess.run(cmd, env=self.env, timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW).returncode

    def install_cmd(self, flags: str) -> int:
        return self.run(self.setup, f'/CURRENTUSER /DIR="{self.install}" /NOICONS /SKIPMUTEXCHECK {flags}')

    def uninstall(self, flags: str = "") -> int:
        rc = self.run(os.path.join(self.install, "unins000.exe"), flags)
        pc_gone = wait(lambda: not os.path.exists(self.install), 60)
        return rc if pc_gone else 99

    def call_lines(self) -> list[str]:
        if not os.path.isfile(self.calls):
            return []
        return [ln.rstrip("\n") for ln in open(self.calls, encoding="utf-8")]

    def reset_calls(self) -> None:
        if os.path.exists(self.calls):
            os.remove(self.calls)


def wait(cond, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.25)
    return bool(cond())


def hold_mutex(name: str):
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW.restype = ctypes.c_void_p
    k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    h = k.CreateMutexW(None, 0, name)
    return k, h


def main() -> int:
    iscc = pc.find_iscc()
    if not iscc:
        print("Inno Setup 6 (ISCC.exe) not found", file=sys.stderr)
        return 2
    if pc.uninstall_key_values(SANDBOX_KEY_ID) is not None or pc.read_run_value(RUN_NAME) is not None:
        print("leftovers of an earlier run of this test exist (sandbox uninstall key or Run value): remove them first", file=sys.stderr)
        return 2
    if shortcuts():
        print("a 'DeskMedia Companion' Start menu shortcut already exists: this test would remove it; not running", file=sys.stderr)
        return 2
    if not os.path.isfile(os.path.join(INSTALLER_DIR, "version.iss")):
        print("installer\\version.iss is missing: run tools\\release_meta.py (or tools\\build_release.ps1) first", file=sys.stderr)
        return 2

    work = tempfile.mkdtemp(prefix="dm-flow-")
    real_data = os.path.join(os.environ.get("LOCALAPPDATA", ""), "DeskMediaCompanion")
    data_before, run_before = pc.snapshot_dir(real_data), pc.read_run_value(REAL_RUN)
    real_key_before = pc.uninstall_key_values(REAL_ID)
    print(f"sandbox: {work}")
    sb = None
    try:
        # ---- build the stand-in app and the sandbox installer ----------------------------------------------------------------
        fake_src = os.path.join(work, "fake_app.py")
        with open(fake_src, "w", encoding="utf-8") as f:
            f.write(FAKE_APP)
        app = os.path.join(work, "fakeapp")
        os.makedirs(os.path.join(app, "_internal"))
        pyi = subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--noconsole", "--name", "DeskMediaCompanion",
                              "--distpath", os.path.join(work, "pyi-dist"), "--workpath", os.path.join(work, "pyi-build"),
                              "--specpath", os.path.join(work, "pyi-spec"), "--log-level", "ERROR", fake_src],
                             capture_output=True, text=True, timeout=600, creationflags=subprocess.CREATE_NO_WINDOW)
        if pyi.returncode != 0:
            print(pyi.stdout, pyi.stderr, file=sys.stderr)
            print("could not build the stand-in app (PyInstaller needed in this Python)", file=sys.stderr)
            return 2
        shutil.copy(os.path.join(work, "pyi-dist", "DeskMediaCompanion.exe"), os.path.join(app, "DeskMediaCompanion.exe"))
        with open(os.path.join(app, "_internal", "runtime.txt"), "w") as f:
            f.write("v1\n")
        out_dir = os.path.join(work, "out")
        defines = [f"/DAppIdValue={SANDBOX_ID}", f"/DRunValueName={RUN_NAME}", f"/DDataDirExpr={os.path.join(work, 'data')}",
                   f"/DSourceDir={app}", "/DOutputBase=flowtest-setup", f"/O{out_dir}"]
        # notices must exist for the real script; build them if the real build has not
        notices = os.path.join(INSTALLER_DIR, "build", "THIRD_PARTY_NOTICES.txt")
        if not os.path.isfile(notices):
            subprocess.run([sys.executable, os.path.join(INSTALLER_DIR, "make_notices.py")], check=True, capture_output=True)
        comp = subprocess.run([iscc, "/Q", *defines, "DeskMediaCompanion.iss"], cwd=INSTALLER_DIR, capture_output=True, text=True, timeout=600,
                              creationflags=subprocess.CREATE_NO_WINDOW)
        if comp.returncode != 0:
            print(comp.stdout, comp.stderr, file=sys.stderr)
            return 2
        global current
        sb = current = Sandbox(work, os.path.join(out_dir, "flowtest-setup.exe"))
        exe = os.path.join(sb.install, "DeskMediaCompanion.exe")
        run_cmd = f'"{exe}" --quiet'

        # ---- A. fresh install: auto-start task + launch ---------------------------------------------------------------------
        rc = sb.install_cmd('/TASKS="autostart"')
        check("A fresh silent install exits 0", rc == 0, f"exit {rc}")
        check("A files installed", all(os.path.exists(os.path.join(sb.install, p)) for p in
                                       ("DeskMediaCompanion.exe", "_internal\\runtime.txt", "unins000.exe", "THIRD_PARTY_NOTICES.txt",
                                        "LICENSES\\LGPL-3.0.txt")))
        check("A auto-start value written with the exact command the tray toggle uses", pc.read_run_value(RUN_NAME) == run_cmd,
              repr(pc.read_run_value(RUN_NAME)))
        key = pc.uninstall_key_values(SANDBOX_KEY_ID) or {}
        check("A uninstall entry has DisplayName / Publisher / version", key.get("DisplayName") == "DeskMedia Companion"
              and key.get("Publisher") == "DeskMedia Display project" and bool(key.get("DisplayVersion")), str({k: key.get(k) for k in ("DisplayName", "Publisher", "DisplayVersion")}))
        check("A the app was launched after installing, also when silent (no /NOLAUNCH)", wait(lambda: "" in sb.call_lines(), 15),
              repr(sb.call_lines()))
        check("A no shortcut (/NOICONS) and no desktop icon", not shortcuts())

        # ---- B. upgrade: data kept, stale runtime files replaced, running copy asked to quit ------------------------------------
        os.makedirs(os.path.join(sb.data, "idle"), exist_ok=True)
        files = {os.path.join(sb.data, "pairing.bin"): b"DMP1D" + os.urandom(40), os.path.join(sb.data, "idle", "settings.json"): b'{"theme": "neon"}'}
        for p, blob in files.items():
            open(p, "wb").write(blob)
        before = {p: sha(p) for p in files}
        with open(os.path.join(sb.install, "_internal", "stale.txt"), "w") as f:
            f.write("a file of an older version")
        sb.reset_calls()
        rc = sb.install_cmd("/NOLAUNCH")
        check("B upgrade over the existing installation exits 0", rc == 0, f"exit {rc}")
        check("B settings and pairing outside the install folder are untouched", all(os.path.isfile(p) and sha(p) == h for p, h in before.items()))
        check("B files of an older version are removed from the runtime folder", not os.path.exists(os.path.join(sb.install, "_internal", "stale.txt"))
              and os.path.exists(os.path.join(sb.install, "_internal", "runtime.txt")))
        quits = [c for c in sb.call_lines() if c == "--quit"]
        check("B a running copy is asked to quit (before Setup's own check and again before copying)", len(quits) >= 2, repr(sb.call_lines()))
        check("B /NOLAUNCH: the app is not started", "" not in sb.call_lines())
        check("B auto-start still on after the upgrade (no task given: the state the user has now)", pc.read_run_value(RUN_NAME) == run_cmd)

        # ---- C. the user switched auto-start off in the tray menu; an upgrade must not switch it back on -------------------------
        pc.delete_run_value(RUN_NAME)
        rc = sb.install_cmd("/NOLAUNCH")
        check("C upgrade after the tray toggle exits 0", rc == 0, f"exit {rc}")
        check("C auto-start stays off", pc.read_run_value(RUN_NAME) is None, repr(pc.read_run_value(RUN_NAME)))
        rc = sb.install_cmd('/NOLAUNCH /TASKS="autostart"')
        check("C an explicit /TASKS=autostart switches it on again", rc == 0 and pc.read_run_value(RUN_NAME) == run_cmd)
        rc = sb.install_cmd('/NOLAUNCH /TASKS=""')
        check("C an explicit empty /TASKS switches our entry off", rc == 0 and pc.read_run_value(RUN_NAME) is None,
              repr(pc.read_run_value(RUN_NAME)))
        pc.write_run_value(RUN_NAME, run_cmd)                       # the user switches it ON in the tray menu (Setup's last choice was off)
        rc = sb.install_cmd("/NOLAUNCH")
        check("C an upgrade keeps what the tray menu switched on", rc == 0 and pc.read_run_value(RUN_NAME) == run_cmd,
              repr(pc.read_run_value(RUN_NAME)))
        foreign = r'"C:\somewhere\else\pythonw.exe" -m desk_media_companion --quiet'
        pc.write_run_value(RUN_NAME, foreign)
        rc = sb.install_cmd('/NOLAUNCH /TASKS=""')
        check("C an explicit empty /TASKS does not remove an entry that is not ours", rc == 0 and pc.read_run_value(RUN_NAME) == foreign)
        pc.write_run_value(RUN_NAME, run_cmd)
        rc = sb.install_cmd('/NOLAUNCH /TASKS="autostart"')

        # ---- D. running-copy gate -----------------------------------------------------------------------------------------------
        k, h = hold_mutex("Local\\DeskMediaCompanion")
        try:
            sb.reset_calls()
            rc = sb.run(sb.setup, f'/CURRENTUSER /DIR="{sb.install}" /NOICONS /NOLAUNCH')
            check("D another running copy (single-instance mutex held) blocks a silent install", rc != 0, f"exit {rc}")
            rc = sb.install_cmd("/NOLAUNCH")                        # includes /SKIPMUTEXCHECK
            check("D /SKIPMUTEXCHECK overrides the gate (automated tests)", rc == 0, f"exit {rc}")
        finally:
            k.CloseHandle(ctypes.c_void_p(h))
        k, h = hold_mutex("Local\\DeskMediaCompanion-lan-8765")      # a LAN-only development instance (--no-usb --lan) holds only this one
        try:
            rc = sb.run(sb.setup, f'/CURRENTUSER /DIR="{sb.install}" /NOICONS /NOLAUNCH')
            check("D a LAN-only development instance (Local\\DeskMediaCompanion-lan-<port> only) does not block an install", rc == 0, f"exit {rc}")
        finally:
            k.CloseHandle(ctypes.c_void_p(h))

        # ---- E. shortcuts -------------------------------------------------------------------------------------------------------
        rc = sb.run(sb.setup, f'/CURRENTUSER /DIR="{sb.install}" /SKIPMUTEXCHECK /NOLAUNCH /TASKS=""')          # no /NOICONS
        check("E Start menu shortcut created by default", rc == 0 and len(shortcuts()) == 1, f"exit {rc}, {shortcuts()}")

        # ---- F. uninstall keeps the data; removes the Run value because it points at this installation ---------------------------
        pc.write_run_value(RUN_NAME, run_cmd)                       # as if switched on from the tray menu
        sb.reset_calls()
        rc = sb.uninstall()
        check("F silent uninstall exits 0 and removes the folder", rc == 0 and not os.path.exists(sb.install), f"exit {rc}")
        check("F uninstall entry removed", pc.uninstall_key_values(SANDBOX_KEY_ID) is None)
        check("F auto-start value removed (also one the tray menu created)", pc.read_run_value(RUN_NAME) is None)
        check("F Start menu shortcut removed", not shortcuts())
        check("F settings and pairing kept by a silent uninstall", all(os.path.isfile(p) and sha(p) == h for p, h in before.items()))
        check("F the running copy was asked to quit before files were removed", any(c == "--quit" for c in sb.call_lines()), repr(sb.call_lines()))

        # ---- G. a Run value of something else is left alone; /DELETEDATA removes the data -------------------------------------------
        rc = sb.install_cmd('/NOLAUNCH /TASKS=""')
        check("G reinstall after uninstall exits 0", rc == 0, f"exit {rc}")
        foreign = r'"C:\somewhere\else\pythonw.exe" -m desk_media_companion --quiet'
        pc.write_run_value(RUN_NAME, foreign)
        rc = sb.uninstall("/DELETEDATA")
        check("G uninstall /DELETEDATA exits 0", rc == 0, f"exit {rc}")
        check("G a Run value that does not point at this installation is left alone", pc.read_run_value(RUN_NAME) == foreign)
        check("G /DELETEDATA removed settings and pairing", not os.path.exists(sb.data))
        pc.delete_run_value(RUN_NAME)

        # ---- H. the default install folder is per-user: %LOCALAPPDATA%\Programs\DeskMedia Companion -----------------------------------
        default_dir = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "DeskMedia Companion")
        if os.path.exists(default_dir):
            print(f"[SKIP] H default folder test: {default_dir} already exists")
        else:
            rc = sb.run(sb.setup, '/CURRENTUSER /SKIPMUTEXCHECK /NOLAUNCH /NOICONS /TASKS=""')                 # no /DIR
            check("H without /DIR the program goes to %LOCALAPPDATA%\\Programs\\DeskMedia Companion (no admin rights)",
                  rc == 0 and os.path.isfile(os.path.join(default_dir, "DeskMediaCompanion.exe")), f"exit {rc}")
            key = pc.uninstall_key_values(SANDBOX_KEY_ID) or {}
            check("H the uninstall entry records that folder", os.path.normcase(key.get("InstallLocation", "").rstrip("\\")) == os.path.normcase(default_dir))
            sb.install = default_dir
            rc = sb.uninstall()
            check("H uninstall removes it again", rc == 0 and not os.path.exists(default_dir), f"exit {rc}")

        # ---- the real things were never touched ---------------------------------------------------------------------------------
        check("real Run value 'DeskMediaCompanion' unchanged", pc.read_run_value(REAL_RUN) == run_before)
        changes = [c for c in pc.diff_snapshots(data_before, pc.snapshot_dir(real_data)) if not c.startswith("changed: companion.log")]
        check("real %LOCALAPPDATA%\\DeskMediaCompanion unchanged", not changes, "; ".join(changes[:5]))
        check("real uninstall entry unchanged", pc.uninstall_key_values(REAL_ID) == real_key_before)
    finally:
        # whatever happened: nothing of the sandbox survives
        try:
            if sb is not None and pc.uninstall_key_values(SANDBOX_KEY_ID) is not None and os.path.isfile(os.path.join(sb.install, "unins000.exe")):
                sb.uninstall("/DELETEDATA")
            if pc.uninstall_key_values(SANDBOX_KEY_ID) is not None:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{SANDBOX_KEY_ID}_is1")
        except OSError:
            pass
        pc.delete_run_value(RUN_NAME)
        for lnk in shortcuts():
            os.remove(lnk)
        shutil.rmtree(work, ignore_errors=True)
    print()
    print("FAILED: " + "; ".join(fails) if fails else "all flow checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

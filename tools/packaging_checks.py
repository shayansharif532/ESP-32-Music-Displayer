"""Shared helpers of the packaging tools (standard library only, Windows only):

* scrubbed_env()        the environment of a "clean PC" for running a packaged exe (nothing of this machine's developer set-up)
* run_exe()             runs a packaged exe with that environment, from another working directory, with a timeout
* snapshot_dir()        fingerprint of a folder (names, sizes, mtimes) to prove that something was NOT touched
* Run value helpers     read / compare HKCU\\...\\Run values
* find_iscc()           locates the Inno Setup compiler
* verify_exe()          checks the properties the release exe must have (windowless x64 PE, manifest, version resource, icon)
* frozen_selftest CLI   python tools\\packaging_checks.py exe <path-to-exe> [--version X]   (used by tools\\build_release.ps1)

It never starts the companion itself: only `--version` and `--selftest`, which open no window, no socket and no serial port.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import winreg
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pe_inspect  # noqa: E402
import release_meta  # noqa: E402

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
SYSTEM_PATH = r"C:\Windows\System32;C:\Windows"


def scrubbed_env(temp_dir: str, data_dir: Optional[str] = None, contain_localappdata: bool = True) -> dict[str, str]:
    """A minimal environment: system PATH only, no PYTHON*, no VIRTUAL_ENV, no developer variables. TEMP / TMP (and, by default,
    LOCALAPPDATA) point into `temp_dir` so that a stray write lands in a throw-away folder; DESK_MEDIA_DATA_DIR points to
    `data_dir`. The user profile variables stay real: DPAPI and the shell folders need them."""
    root = os.environ.get("SystemRoot", r"C:\Windows")
    env = {
        "PATH": SYSTEM_PATH, "SystemRoot": root, "windir": root, "SystemDrive": os.environ.get("SystemDrive", "C:"),
        "ComSpec": os.path.join(root, "System32", "cmd.exe"), "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "TEMP": temp_dir, "TMP": temp_dir,
        "USERPROFILE": os.environ.get("USERPROFILE", ""), "APPDATA": os.environ.get("APPDATA", ""),
        "ProgramData": os.environ.get("ProgramData", r"C:\ProgramData"),
        "ProgramFiles": os.environ.get("ProgramFiles", r"C:\Program Files"),
        "NUMBER_OF_PROCESSORS": os.environ.get("NUMBER_OF_PROCESSORS", "1"),
        "PROCESSOR_ARCHITECTURE": os.environ.get("PROCESSOR_ARCHITECTURE", "AMD64"),
        "OS": "Windows_NT",
    }
    env["LOCALAPPDATA"] = os.path.join(temp_dir, "localappdata") if contain_localappdata else os.environ.get("LOCALAPPDATA", "")
    if contain_localappdata:
        os.makedirs(env["LOCALAPPDATA"], exist_ok=True)
    if data_dir:
        env["DESK_MEDIA_DATA_DIR"] = data_dir
    return env


def run_exe(exe: str, args: list[str], env: dict[str, str], cwd: Optional[str] = None, timeout: float = 120.0) -> subprocess.CompletedProcess:
    """Runs a packaged exe with a scrubbed environment from a different working directory (stdout / stderr captured)."""
    return subprocess.run([exe, *args], env=env, cwd=cwd or tempfile.gettempdir(), capture_output=True, text=True,
                          timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW)


def read_text_result(proc: subprocess.CompletedProcess, temp_dir: str, temp_name: str) -> str:
    """What a windowless exe printed: stdout when the harness got one, else the file the exe writes to %TEMP% instead."""
    out = (proc.stdout or "").strip()
    if out:
        return out
    path = os.path.join(temp_dir, temp_name)
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    return ""


# ---- fingerprints -------------------------------------------------------------------------------------------------------------

def snapshot_dir(path: str) -> Optional[dict[str, tuple[int, int]]]:
    """{relative path: (size, mtime_ns)} of every file under `path`, None when the folder does not exist."""
    if not os.path.isdir(path):
        return None
    snap: dict[str, tuple[int, int]] = {}
    for base, _dirs, files in os.walk(path):
        for name in files:
            full = os.path.join(base, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            snap[os.path.relpath(full, path)] = (st.st_size, st.st_mtime_ns)
    return snap


def diff_snapshots(before, after) -> list[str]:
    if before is None and after is None:
        return []
    if before is None:
        return ["folder was created"]
    if after is None:
        return ["folder was removed"]
    out = [f"added: {k}" for k in sorted(set(after) - set(before))]
    out += [f"removed: {k}" for k in sorted(set(before) - set(after))]
    out += [f"changed: {k}" for k in sorted(set(before) & set(after)) if before[k] != after[k]]
    return out


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---- registry ------------------------------------------------------------------------------------------------------------------

def read_run_value(name: str) -> Optional[str]:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as k:
            return winreg.QueryValueEx(k, name)[0]
    except FileNotFoundError:
        return None


def write_run_value(name: str, data: str) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_SZ, data)


def delete_run_value(name: str) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)
        return True
    except FileNotFoundError:
        return False


def uninstall_key_values(app_id_braced: str) -> Optional[dict[str, str]]:
    """The values of HKCU\\...\\Uninstall\\<app id>_is1, or None when the key does not exist."""
    path = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{app_id_braced}_is1"
    try:
        out: dict[str, str] = {}
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ) as k:
            i = 0
            while True:
                try:
                    name, value, _t = winreg.EnumValue(k, i)
                except OSError:
                    break
                out[name] = str(value)
                i += 1
        return out
    except FileNotFoundError:
        return None


# ---- tools ---------------------------------------------------------------------------------------------------------------------

def find_iscc() -> Optional[str]:
    """The Inno Setup 6 compiler: $env:ISCC, PATH, then the usual per-machine and per-user install folders."""
    candidates = [os.environ.get("ISCC", "")]
    on_path = shutil.which("ISCC.exe")
    if on_path:
        candidates.append(on_path)
    for var in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if base:
            sub = os.path.join("Programs", "Inno Setup 6") if var == "LOCALAPPDATA" else "Inno Setup 6"
            candidates.append(os.path.join(base, sub, "ISCC.exe"))
    return next((c for c in candidates if c and os.path.isfile(c)), None)


# ---- the release exe -----------------------------------------------------------------------------------------------------------

def verify_exe(exe: str, version: str) -> list[str]:
    """Problems with the properties the release exe must have (empty list = all good)."""
    problems: list[str] = []
    info = pe_inspect.inspect(exe)
    if info["machine"] != "x64" or not info["pe32plus"]:
        problems.append(f"not an x64 PE32+ file ({info['machine']})")
    if info["subsystem"] != "gui":
        problems.append(f"subsystem is {info['subsystem']}, not gui: a console window would open")
    man = info.get("manifest") or ""
    for needle, what in (('level="asInvoker"', "asInvoker execution level"), ("8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a", "Windows 10/11 supportedOS id"),
                         ("dpiAware", "DPI awareness"), ("Microsoft.Windows.Common-Controls", "common controls v6")):
        if needle not in man:
            problems.append(f"manifest lacks {what}")
    if "requireAdministrator" in man or "highestAvailable" in man:
        problems.append("manifest asks for elevation")
    v4 = ".".join(str(n) for n in release_meta.version_tuple(version))
    ver = info.get("version", {})
    expect = {"FileVersion": v4, "file_version_fixed": v4, "ProductVersion": version, "FileDescription": "DeskMedia Companion",
              "ProductName": "DeskMedia Display", "CompanyName": "DeskMedia Display project",
              "OriginalFilename": "DeskMediaCompanion.exe", "InternalName": "DeskMediaCompanion"}
    for key, want in expect.items():
        if ver.get(key) != want:
            problems.append(f"version resource {key} is {ver.get(key)!r}, expected {want!r}")
    if not ver.get("LegalCopyright", "").startswith("Copyright (c) "):
        problems.append("version resource has no copyright line")
    missing = [s for s in (16, 24, 32, 48, 64, 128, 256) if s not in info.get("icon_sizes", [])]
    if missing:
        problems.append(f"icon lacks sizes {missing}")
    return problems


def selftest_exe(exe: str, version: str, quiet: bool = False) -> int:
    """--version and --selftest of the packaged exe in a scrubbed environment; returns 0 when both are fine."""
    work = tempfile.mkdtemp(prefix="dm-selftest-")
    try:
        data = os.path.join(work, "data")
        env = scrubbed_env(work, data)
        problems = verify_exe(exe, version)
        for p in problems:
            print("PROBLEM:", p)
        t = time.time()
        proc = run_exe(exe, ["--version"], env)
        shown = read_text_result(proc, work, "deskmedia-version.txt")
        if proc.returncode != 0 or shown != version:
            problems.append(f"--version gave exit {proc.returncode} and {shown!r}, expected {version!r}")
        proc = run_exe(exe, ["--selftest"], env)
        line = read_text_result(proc, work, "deskmedia-selftest.json")
        result = None
        try:
            result = json.loads(line)
        except ValueError:
            problems.append(f"--selftest did not print one JSON line (exit {proc.returncode}): {line[:200]!r} {proc.stderr[:200]!r}")
        if result is not None:
            if proc.returncode != 0 or not result.get("ok"):
                bad = {k: v for k, v in result.get("checks", {}).items() if not v.get("ok")}
                problems.append(f"--selftest failed (exit {proc.returncode}): {bad}")
            if not quiet:
                print(line)
        if os.path.exists(data):
            problems.append("--selftest created the data directory")
        left = [n for n in os.listdir(env["LOCALAPPDATA"])] if os.path.isdir(env["LOCALAPPDATA"]) else []
        if left:
            problems.append(f"--version / --selftest wrote into LOCALAPPDATA: {left}")
        print(f"checked in {time.time() - t:.1f} s: {'OK' if not problems else 'FAILED'}")
        for p in problems:
            print("  -", p)
        return 0 if not problems else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="packaged-exe checks")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("exe", help="verify the exe's PE properties and run --version / --selftest in a scrubbed environment")
    p.add_argument("path")
    p.add_argument("--version", required=True, help="the version the exe must report")
    args = ap.parse_args(argv)
    if args.cmd == "exe":
        return selftest_exe(os.path.abspath(args.path), args.version)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""Clean-machine-style packaging test of a DeskMedia Companion installer.

    python tools\\installer_smoke_test.py installer\\output\\DeskMediaCompanion-Setup-0.1.0.exe [--report report.json] [--keep]

Standard library only (it can use PyInstaller, when importable, to look inside the exe's archive). It builds nothing and starts
nothing that listens or opens a serial port. What it does:

  1. installs the installer silently into a throw-away folder (/DIR, /CURRENTUSER, no auto-start task, no shortcuts, no launch,
     /LOG) and checks the result: files, the uninstall registry entry, no HKLM entry, no process left running
  2. runs the INSTALLED exe with a scrubbed environment (PATH = System32 + Windows only, no PYTHON*, no VIRTUAL_ENV, another working
     directory, TEMP / LOCALAPPDATA / DESK_MEDIA_DATA_DIR in a throw-away folder) for --version and --selftest
  3. checks the exe's PE properties: windowless x64, manifest (asInvoker, Windows 10/11, DPI), version resource, icon
  4. scans every installed file - raw bytes as ASCII and UTF-16, zip members, and the code inside the exe's PyInstaller archive -
     for anything that refers to THIS PC: the Windows user name, C:\\Users\\<you>, the profile folder, the repository / checkout path,
     .venv, the computer name (plus --needle values); other C:\\Users\\<name> paths are reported as warnings
  5. uninstalls silently (never with /DELETEDATA) and verifies the folder, the uninstall key and the Run value are gone
  6. verifies that the REAL %LOCALAPPDATA%\\DeskMediaCompanion and the REAL HKCU Run value "DeskMediaCompanion" were not created or
     modified by any of it, and that no Start menu shortcut was left

Safety: it refuses to run when the product is already installed on this PC (the real uninstall key exists), it does not touch a
running companion (/SKIPMUTEXCHECK is passed so another copy running from elsewhere does not block the silent install), and a
failed run still uninstalls what it installed. Exit code 0 = everything passed, 1 = a check failed, 2 = could not run.
"""
from __future__ import annotations

import argparse
import getpass
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import winreg
import zipfile
from typing import Iterable, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import packaging_checks as pc  # noqa: E402
import pe_inspect  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
APP_ID = "{73454063-9F9F-4C5A-9818-1D7B6B45C5F7}"        # must equal AppIdValue in installer\DeskMediaCompanion.iss
RUN_VALUE = "DeskMediaCompanion"
EXE_NAME = "DeskMediaCompanion.exe"

results: list[dict] = []


def record(name: str, ok: bool, detail: str = "", warn: bool = False) -> bool:
    status = "PASS" if ok else ("WARN" if warn else "FAIL")
    results.append({"check": name, "status": status, "detail": detail})
    print(f"[{status}] {name}" + (f" - {detail}" if detail else ""))
    return ok


def real_data_dir() -> str:
    return os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "DeskMediaCompanion")


def start_menu_shortcuts() -> list[str]:
    base = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs")
    found = []
    if os.path.isdir(base):
        for root, _dirs, files in os.walk(base):
            found += [os.path.join(root, f) for f in files if f.lower().startswith("deskmedia companion") and f.lower().endswith(".lnk")]
    desktop = os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")
    if os.path.isdir(desktop):
        found += [os.path.join(desktop, f) for f in os.listdir(desktop) if f.lower().startswith("deskmedia companion") and f.lower().endswith(".lnk")]
    return found


def processes_under(folder: str) -> list[str]:
    ps = ("Get-Process | Where-Object { $_.Path -and $_.Path.StartsWith('" + folder.replace("'", "''") + "', [StringComparison]::OrdinalIgnoreCase) } "
          "| ForEach-Object { $_.Path }")
    out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True, timeout=60,
                         creationflags=subprocess.CREATE_NO_WINDOW)
    return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]


def signature_status(path: str) -> str:
    ps = f"(Get-AuthenticodeSignature -LiteralPath '{path}').Status"
    out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True, timeout=60,
                         creationflags=subprocess.CREATE_NO_WINDOW)
    return out.stdout.strip() or "unknown"


# ---- the scan for references to this PC -----------------------------------------------------------------------------------------

def _path_regex(path: str, wide: bool) -> bytes:
    parts = [p for p in re.split(r"[\\/]+", path.strip("\\/")) if p]
    enc = (lambda s: s.encode("utf-16le")) if wide else (lambda s: s.encode("utf-8"))
    sep = (b"(?:\\\\\x00|/\x00){1,2}" if wide else b"[\\\\/]{1,2}")
    return sep.join(re.escape(enc(p)) for p in parts)


def build_scanner(extra: Iterable[str]):
    """Compiled patterns [(label, severity, bytes-regex)] for ASCII / UTF-8 and UTF-16LE text."""
    user = getpass.getuser()
    profile = os.environ.get("USERPROFILE", "")
    host = os.environ.get("COMPUTERNAME", "")
    needles: list[tuple[str, str, str]] = []          # (label, kind, value)
    needles.append(("windows user name", "token", user))
    if host and len(host) >= 4:
        needles.append(("computer name", "token", host))
    if profile:
        needles.append(("user profile folder", "path", profile))
    needles.append(("repository path", "path", REPO))
    probe = REPO
    while True:                                         # every ancestor that is itself a git checkout (the main checkout of a worktree)
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
        if os.path.exists(os.path.join(probe, ".git")):
            needles.append(("checkout path", "path", probe))
    needles.append((".venv folder", "path-part", ".venv"))
    for e in extra:
        needles.append((f"needle {e!r}", "token", e))
    compiled = []
    for label, kind, value in needles:
        for wide in (False, True):
            enc = (lambda s: s.encode("utf-16le")) if wide else (lambda s: s.encode("utf-8"))
            if kind == "path":
                pat = _path_regex(value, wide)
            elif kind == "path-part":
                pat = (b"(?:\\\\\x00|/\x00)" if wide else b"[\\\\/]") + re.escape(enc(value)) + (b"(?:\\\\\x00|/\x00)" if wide else b"[\\\\/]")
            else:
                word = b"[A-Za-z0-9_]\x00" if wide else b"[A-Za-z0-9_]"
                pat = b"(?<!" + word + b")" + re.escape(enc(value)) + b"(?!" + word + b")"
            compiled.append((label, "fail", re.compile(pat, re.IGNORECASE)))
    generic_ascii = re.compile(rb"[A-Za-z]:[\\/]{1,2}Users[\\/]{1,2}([A-Za-z0-9 ._-]{1,40})", re.IGNORECASE)
    generic_wide = re.compile(rb"[A-Za-z]\x00:\x00(?:[\\/]\x00){1,2}U\x00s\x00e\x00r\x00s\x00(?:[\\/]\x00){1,2}((?:[A-Za-z0-9 ._-]\x00){1,40})", re.IGNORECASE)
    return compiled, [generic_ascii, generic_wide]


def _context(data: bytes, m: re.Match) -> str:
    chunk = data[max(0, m.start() - 24): m.end() + 24]
    text = chunk.replace(b"\x00", b"").decode("utf-8", "replace")
    return re.sub(r"[^\x20-\x7e]", ".", text)


def scan_bytes(data: bytes, where: str, scanner, hits: list, warns: dict) -> None:
    compiled, generic = scanner
    for label, _sev, rx in compiled:
        m = rx.search(data)
        if m:
            hits.append((label, where, _context(data, m)))
    user = getpass.getuser().lower()
    for rx in generic:
        for m in rx.finditer(data):
            name = m.group(1).replace(b"\x00", b"").decode("utf-8", "replace").strip().lower()
            if name and name != user and not name.startswith(("%", "<", "{")):
                warns.setdefault(name, set()).add(where)


def scanner_positive_control(extra: Iterable[str]) -> Optional[str]:
    """Proves the scan is not vacuous: planted strings (this PC's user name, profile, repository and .venv paths, ASCII and UTF-16)
    must all be found. Returns a problem description or None."""
    scanner = build_scanner(extra)
    user = getpass.getuser()
    samples = [f"x C:\\Users\\{user}\\AppData y", f"/c/Users/{user}/Videos", f"name={user};", REPO, REPO.replace("\\", "/"),
               "C:\\work\\.venv\\Lib\\site-packages", "C:\\\\work\\\\.venv\\\\Scripts"]
    missed = []
    for s in samples:
        for enc in ("utf-8", "utf-16le"):
            hits: list = []
            scan_bytes(b"\x01\x02" + s.encode(enc) + b"\x03", "control", scanner, hits, {})
            if not hits:
                missed.append(f"{s!r} as {enc}")
    clean: list = []
    scan_bytes(b"nothing personal here, C:\\Program Files\\Something", "control", scanner, clean, {})
    if clean:
        missed.append("false positive on neutral text")
    return "; ".join(missed) or None


def code_strings(code) -> Iterable[str]:
    yield code.co_filename
    for c in code.co_consts:
        if isinstance(c, str):
            yield c
        elif isinstance(c, bytes):
            yield c.decode("latin-1")
        elif hasattr(c, "co_consts"):
            yield from code_strings(c)
    yield from code.co_names


def scan_pyinstaller_exe(exe: str, scanner, hits: list, warns: dict) -> str:
    """Looks inside the exe's PyInstaller archive (the compressed modules the raw bytes do not show). Returns a note."""
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except Exception as exc:
        return f"PyInstaller is not importable ({type(exc).__name__}): archive inside the exe was NOT unpacked"
    try:
        pkg = CArchiveReader(exe)
        modules = 0
        for name, entry in pkg.toc.items():
            if name.endswith(".pyz"):
                pyz = pkg.open_embedded_archive(name)
                for mod in list(pyz.toc):
                    try:
                        code = pyz.extract(mod)
                    except Exception:
                        continue
                    if code is None:
                        continue
                    blob = "\n".join(code_strings(code)).encode("utf-8", "replace")
                    scan_bytes(blob, f"{os.path.basename(exe)}:{name}:{mod}", scanner, hits, warns)
                    modules += 1
            else:
                data = pkg.extract(name)
                if isinstance(data, bytes):
                    scan_bytes(data, f"{os.path.basename(exe)}:{name}", scanner, hits, warns)
        return f"unpacked {modules} compiled modules from the exe's archive"
    except Exception as exc:
        return f"could not unpack the exe's archive ({type(exc).__name__}: {exc})"


def scan_tree(root: str, extra: Iterable[str]):
    scanner = build_scanner(extra)
    hits: list = []
    warns: dict = {}
    files = 0
    nbytes = 0
    notes = []
    for base, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(base, name)
            rel = os.path.relpath(path, root)
            if re.fullmatch(r"unins\d{3}\.dat", name, re.IGNORECASE):
                # Written by Setup on the installing PC: it records that PC's user name, computer name and install paths by
                # design (it is the target PC's own identity, not the build machine's; this test installs on the build PC).
                notes.append(f"{name} skipped (Setup's own uninstall log)")
                continue
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            files += 1
            nbytes += len(data)
            scan_bytes(data, rel, scanner, hits, warns)
            if name.lower().endswith((".zip", ".whl")) or name == "base_library.zip":
                try:
                    with zipfile.ZipFile(io.BytesIO(data)) as z:
                        for member in z.namelist():
                            scan_bytes(z.read(member), f"{rel}!{member}", scanner, hits, warns)
                except (zipfile.BadZipFile, OSError):
                    pass
    exe = os.path.join(root, EXE_NAME)
    if os.path.isfile(exe):
        notes.append(scan_pyinstaller_exe(exe, scanner, hits, warns))
    return files, nbytes, hits, warns, notes


# ---- the steps ----------------------------------------------------------------------------------------------------------------------

def run_setup(cmd: str, timeout: float) -> int:
    return subprocess.run(cmd, timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW).returncode


def wait_gone(path: str, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not os.path.exists(path):
            return True
        time.sleep(0.5)
    return not os.path.exists(path)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="clean-machine-style smoke test of a DeskMedia Companion installer")
    ap.add_argument("installer", help="path to DeskMediaCompanion-Setup-<version>.exe")
    ap.add_argument("--report", help="write the results as JSON to this file")
    ap.add_argument("--keep", action="store_true", help="leave the throw-away folder in place (the installation itself is still removed)")
    ap.add_argument("--needle", action="append", default=[], help="an extra string that must not appear in any installed file (repeatable)")
    ap.add_argument("--no-skip-mutex", action="store_true", help="do not pass /SKIPMUTEXCHECK (use on a machine where no companion runs)")
    args = ap.parse_args(argv)

    setup = os.path.abspath(args.installer)
    if not os.path.isfile(setup):
        print(f"no such installer: {setup}", file=sys.stderr)
        return 2
    if os.name != "nt":
        print("Windows only", file=sys.stderr)
        return 2
    if pc.uninstall_key_values(APP_ID) is not None:
        print("DeskMedia Companion is already installed on this PC (its uninstall entry exists). This test installs and removes the "
              "real entry, so it refuses to run here. Uninstall the product first.", file=sys.stderr)
        return 2

    work = tempfile.mkdtemp(prefix="dm-smoke-")
    install_dir = os.path.join(work, "install dir")             # with a space, like "Program Files"
    log_path = os.path.join(work, "setup.log")
    temp_dir = os.path.join(work, "temp")
    data_dir = os.path.join(work, "data")
    os.makedirs(temp_dir)
    uninstall_log = os.path.join(work, "uninstall.log")

    real_data = real_data_dir()
    data_before = pc.snapshot_dir(real_data)
    run_before = pc.read_run_value(RUN_VALUE)
    shortcuts_before = start_menu_shortcuts()
    print(f"installer: {setup} ({os.path.getsize(setup) / 1048576:.1f} MB)")
    print(f"sandbox:   {work}")
    print(f"real data dir {'exists' if data_before is not None else 'does not exist'}: {real_data} (must stay as it is)")
    print(f"real Run value '{RUN_VALUE}': {'present' if run_before is not None else 'absent'} (must stay as it is)")

    problem = scanner_positive_control(args.needle)
    record("the scan for references to this PC finds planted user / repo / .venv strings (positive control)", problem is None, problem or "")
    installed = False
    try:
        # ---- 1. install ----------------------------------------------------------------------------------------------------
        flags = '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CURRENTUSER /NOLAUNCH /NOICONS /TASKS=""'
        if not args.no_skip_mutex:
            flags += " /SKIPMUTEXCHECK"
        t = time.time()
        rc = run_setup(f'"{setup}" {flags} /DIR="{install_dir}" /LOG="{log_path}"', 600)
        installed = True
        record("silent install exits 0", rc == 0, f"exit code {rc}, {time.time() - t:.0f} s, log {log_path}")
        if rc != 0:
            return finish(args, work, 1)

        exe = os.path.join(install_dir, EXE_NAME)
        need = [EXE_NAME, "unins000.exe", "unins000.dat", "THIRD_PARTY_NOTICES.txt", os.path.join("LICENSES", "LGPL-3.0.txt"),
                os.path.join("LICENSES", "Pillow-LICENSE.txt"), "_internal"]
        missing = [n for n in need if not os.path.exists(os.path.join(install_dir, n))]
        record("installed files present", not missing, f"missing: {missing}" if missing else "exe, runtime, uninstaller, notices, licences")
        files = [os.path.join(b, f) for b, _d, fs in os.walk(install_dir) for f in fs]
        size = sum(os.path.getsize(f) for f in files)
        print(f"         {len(files)} files, {size / 1048576:.1f} MB")

        key = pc.uninstall_key_values(APP_ID) or {}
        want = {"DisplayName": "DeskMedia Companion", "Publisher": "DeskMedia Display project"}
        bad = {k: key.get(k) for k, v in want.items() if key.get(k) != v}
        loc = key.get("InstallLocation", "").rstrip("\\")
        ok = bool(key) and not bad and os.path.normcase(loc) == os.path.normcase(install_dir) and "DisplayVersion" in key \
            and os.path.isfile(key.get("UninstallString", "").strip('"').split('" ')[0].strip('"'))
        record("uninstall registry entry (HKCU)", ok, f"DisplayVersion {key.get('DisplayVersion')}, Publisher {key.get('Publisher')!r}, "
                                                       f"InstallLocation {loc!r}" + (f"; wrong: {bad}" if bad else ""))
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}_is1"):
                record("no per-machine (HKLM) uninstall entry", False, "found one: the install was not per-user")
        except FileNotFoundError:
            record("no per-machine (HKLM) uninstall entry", True, "per-user install")
        record("Run value untouched by the install", pc.read_run_value(RUN_VALUE) == run_before)
        running = processes_under(install_dir)
        record("nothing from the install folder is running", not running, ", ".join(running))
        record("no shortcuts created", start_menu_shortcuts() == shortcuts_before)

        # ---- 2/3. the exe, scrubbed -------------------------------------------------------------------------------------
        version = key.get("DisplayVersion", "")
        problems = pc.verify_exe(exe, version)
        record("exe: windowless x64, manifest (asInvoker, Win10/11, DPI), version resource, icon", not problems, "; ".join(problems))
        info = pe_inspect.inspect(exe)
        print(f"         version resource: {info['version'].get('FileVersion')} / {info['version'].get('ProductVersion')}, "
              f"{info['version'].get('CompanyName')!r}, {info['version'].get('LegalCopyright')!r}")
        print(f"         signature: exe {signature_status(exe)}, installer {signature_status(setup)}")

        env = pc.scrubbed_env(temp_dir, data_dir)
        proc = pc.run_exe(exe, ["--version"], env, cwd=work)
        shown = pc.read_text_result(proc, temp_dir, "deskmedia-version.txt")
        record("--version (scrubbed environment)", proc.returncode == 0 and shown == version, f"exit {proc.returncode}, printed {shown!r}")
        t = time.time()
        proc = pc.run_exe(exe, ["--selftest"], env, cwd=work, timeout=180)
        line = pc.read_text_result(proc, temp_dir, "deskmedia-selftest.json")
        try:
            result = json.loads(line)
        except ValueError:
            result = None
        if result is None:
            record("--selftest (scrubbed environment)", False, f"no JSON line; exit {proc.returncode}; stderr {proc.stderr[:200]!r}")
        else:
            failed = {k: v.get("reason") for k, v in result["checks"].items() if not v.get("ok")}
            record("--selftest (scrubbed environment)", proc.returncode == 0 and result.get("ok") and result.get("frozen") is True,
                   f"exit {proc.returncode}, {len(result['checks'])} checks, {time.time() - t:.1f} s" + (f"; failed: {failed}" if failed else ""))
            print("         selftest: " + json.dumps(result, separators=(",", ":")))
        record("--selftest / --version left the data directory alone", not os.path.exists(data_dir),
               "DESK_MEDIA_DATA_DIR was created" if os.path.exists(data_dir) else "")
        contained = os.listdir(env["LOCALAPPDATA"])
        record("nothing written to LOCALAPPDATA by the exe", not contained, str(contained))

        # ---- 4. references to this PC ------------------------------------------------------------------------------------
        t = time.time()
        n_files, n_bytes, hits, warns, notes = scan_tree(install_dir, args.needle)
        with open(setup, "rb") as f:                         # the installer itself (its data is compressed: only the loader and
            scan_bytes(f.read(), "(the installer exe)", build_scanner(args.needle), hits, warns)       # its resources are visible)
        print(f"         scanned {n_files} files, {n_bytes / 1048576:.1f} MB in {time.time() - t:.0f} s; " + "; ".join(notes))
        if hits:
            for label, where, ctx in hits[:40]:
                print(f"         HIT {label}: {where}  ...{ctx}...")
        record("no reference to this PC in the installed files (user name, C:\\Users\\<you>, repo / checkout path, .venv, computer name)",
               not hits, f"{len(hits)} hit(s)" if hits else f"{n_files} files incl. the exe's compiled modules")
        if warns:
            summary = {name: sorted(places)[:3] for name, places in sorted(warns.items())}
            record("other C:\\Users\\<name> strings (third-party documentation text, informational)", True, "")
            for name, places in summary.items():
                print(f"         info: C:\\Users\\{name}  in {', '.join(places)}")
        if not any("PyInstaller is not importable" in n or "could not unpack" in n for n in notes):
            record("exe's PyInstaller archive was unpacked and scanned", True, "; ".join(notes))
        else:
            record("exe's PyInstaller archive was unpacked and scanned", False, "; ".join(notes), warn=True)

        # ---- 5. uninstall -------------------------------------------------------------------------------------------------
        t = time.time()
        rc = run_setup(f'"{os.path.join(install_dir, "unins000.exe")}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /LOG="{uninstall_log}"', 300)
        record("silent uninstall exits 0", rc == 0, f"exit code {rc}")
        gone = wait_gone(install_dir, 60)
        left = []
        if not gone:
            left = [os.path.relpath(os.path.join(b, f), install_dir) for b, _d, fs in os.walk(install_dir) for f in fs][:10]
        record("install folder removed by the uninstaller", gone, f"left over: {left}" if left else f"{time.time() - t:.0f} s")
        installed = os.path.exists(install_dir)
        record("uninstall registry entry removed", pc.uninstall_key_values(APP_ID) is None)
        record("Run value unchanged after uninstall", pc.read_run_value(RUN_VALUE) == run_before)
        record("no shortcuts left behind", start_menu_shortcuts() == shortcuts_before)

        # ---- 6. the real data directory ------------------------------------------------------------------------------------
        changes = pc.diff_snapshots(data_before, pc.snapshot_dir(real_data))
        # A companion running from somewhere else on this PC keeps writing its own log: that is not this test's doing.
        other = [c for c in changes if re.match(r"changed: companion\.log(\.\d+)?$", c)]
        changes = [c for c in changes if c not in other]
        record("the real %LOCALAPPDATA%\\DeskMediaCompanion was not created or modified (kept by a silent uninstall)", not changes,
               "; ".join(changes[:6]))
        if other:
            record("companion.log in the real data folder changed", True, "a companion running elsewhere writes it; ignored", warn=True)
        return finish(args, work, 0)
    finally:
        cleanup(setup, install_dir, work, args.keep)


def cleanup(setup: str, install_dir: str, work: str, keep: bool) -> None:
    """Whatever happened: the registry entry and the install folder must not survive the test."""
    try:
        key = pc.uninstall_key_values(APP_ID)
        unins = os.path.join(install_dir, "unins000.exe")
        if key is not None and os.path.isfile(unins):
            subprocess.run(f'"{unins}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART', timeout=300, creationflags=subprocess.CREATE_NO_WINDOW)
            wait_gone(install_dir, 60)
        key = pc.uninstall_key_values(APP_ID)
        if key is not None and os.path.normcase(key.get("InstallLocation", "").rstrip("\\")) == os.path.normcase(install_dir):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}_is1")
            except OSError:
                pass
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)


def finish(args, work: str, code: int) -> int:
    failed = [r for r in results if r["status"] == "FAIL"]
    warned = [r for r in results if r["status"] == "WARN"]
    print()
    print(f"{len(results) - len(failed) - len(warned)} passed, {len(warned)} warning(s), {len(failed)} failed")
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump({"installer": os.path.abspath(args.installer), "results": results}, f, indent=2)
    return 1 if failed or code else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec of DeskMediaCompanion: the single source of truth for how the packaged app is built.

    python -m PyInstaller --noconfirm --clean DeskMediaCompanion.spec          (from this folder; tools\\build_release.ps1 does this)

Result: dist\\DeskMediaCompanion\\DeskMediaCompanion.exe + dist\\DeskMediaCompanion\\_internal\\  (a folder, "onedir").

Why onedir and not one file: a one-file exe unpacks itself (~25 MB of Python, Tk and DLLs) into %TEMP% on EVERY start - slowly, with
a visible delay before the tray icon appears, and the "self-extracting executable that runs code from a temp folder" pattern is
exactly what antivirus heuristics flag. The installer puts the folder in place once; every start then loads from disk directly.
No UPX: compressed binaries are another antivirus trigger and save little once the installer compresses everything with LZMA.

Nothing machine-specific is embedded: the icon, manifest and version resource come from files in the repository.
"""
import os
import sys
from importlib.util import find_spec

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules, copy_metadata

HERE = os.path.abspath(SPECPATH)                       # noqa: F821  (SPECPATH is provided by PyInstaller)
ROOT = os.path.abspath(os.path.join(HERE, ".."))
PACKAGE_DIR = os.path.join(HERE, "desk_media_companion")
ICON = os.path.join(ROOT, "installer", "assets", "DeskMediaCompanion.ico")
MANIFEST = os.path.join(HERE, "DeskMediaCompanion.manifest")
VERSION_FILE = os.path.join(HERE, "version_info.txt")

# The version resource is generated from desk_media_companion.__version__ (tools/release_meta.py), so the spec also works on its own.
sys.path.insert(0, os.path.join(ROOT, "tools"))
import release_meta  # noqa: E402
release_meta.write(VERSION_FILE, release_meta.version_info_text(release_meta.read_version(), release_meta.commit_year()))

for required in (ICON, MANIFEST):
    if not os.path.isfile(required):
        raise SystemExit(f"missing build input: {required}")

# ---- what to bundle -----------------------------------------------------------------------------------------------------------
hiddenimports = []
binaries = []
datas = []

# Every module of the application, so one that is only imported lazily (or not wired in yet) is still inside the build.
hiddenimports += collect_submodules("desk_media_companion")

# WinRT projections (Windows.Media.Control and friends): pure-Python namespace packages plus one native extension per namespace.
hiddenimports += collect_submodules("winrt")
binaries += collect_dynamic_libs("winrt")
hiddenimports += ["winrt.windows.media", "winrt.windows.media.control", "winrt.windows.storage.streams",
                  "winrt.windows.foundation", "winrt.windows.foundation.collections"]

# Core Audio per-app volume (pycaw sits on comtypes, which also builds COM wrappers at run time).
hiddenimports += collect_submodules("pycaw") + collect_submodules("comtypes")

# Tray icon: the Windows backend of pystray is chosen at run time, so name it.
hiddenimports += collect_submodules("pystray") + ["pystray._win32", "six"]

# Pillow's Tk bridge (PIL.ImageTk) is only imported inside the settings window code.
hiddenimports += ["PIL.ImageTk", "PIL._tkinter_finder"]

# tkinter (the settings / pairing windows): PyInstaller's own hook bundles Tcl / Tk and their script libraries.
hiddenimports += ["tkinter", "tkinter.ttk", "tkinter.filedialog", "tkinter.messagebox", "tkinter.colorchooser"]

# pyserial (the USB link, usblink.py - the product's primary transport): required. pyserial picks list_ports_windows at run time, and
# usblink names it in a conditional import, so list it here as well.
if find_spec("serial") is None:
    raise SystemExit("pyserial is not installed in the build environment (companion\\requirements.txt): the USB link cannot be packaged without it")
hiddenimports += ["serial", "serial.serialutil", "serial.serialwin32", "serial.win32", "serial.tools",
                  "serial.tools.list_ports", "serial.tools.list_ports_common", "serial.tools.list_ports_windows"]

# Package metadata (the dist-info folders) so that importlib.metadata - and --selftest - can report the bundled versions.
for dist in ("pycaw", "pystray", "winrt-runtime", "comtypes", "pyserial", "pillow", "psutil"):
    try:
        datas += copy_metadata(dist)
    except Exception:                       # no metadata for it: only the version report in --selftest is poorer
        pass

# Data files of the application: desk_media_companion/assets (Montserrat-Medium.ttf for the idle-screen preview, with its licence text),
# found at run time through desk_media_companion/resources.py (sys._MEIPASS\desk_media_companion\assets). Optional for the BUILD, so a
# source tree without the folder still builds; tools\build_release.ps1 verifies that every file of the folder made it into the app.
assets = os.path.join(PACKAGE_DIR, "assets")
if os.path.isdir(assets) and os.listdir(assets):
    datas.append((assets, os.path.join("desk_media_companion", "assets")))

excludes = ["unittest", "pydoc", "test", "tkinter.test"]       # nothing at run time needs them (as in the previous build script)
excludes += ["serial.tools.list_ports_osx", "serial.tools.list_ports_linux", "serial.tools.list_ports_posix",      # other platforms' code that
             "serial.serialposix", "serial.serialjava", "serial.serialcli"]                                       # pyserial imports conditionally

a = Analysis(
    [os.path.join(HERE, "run_companion.py")],
    pathex=[HERE],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,                  # onedir: the binaries and data go to the folder (COLLECT below), not into the exe
    name="DeskMediaCompanion",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                          # no console window, ever (so there is also no stdout: see run_companion.py)
    disable_windowed_traceback=True,        # a crash is written to the log file instead of opening a dialog (run_companion.py)
    icon=ICON,
    version=VERSION_FILE,
    manifest=MANIFEST,                      # asInvoker + Windows 10/11 + DPI awareness (DeskMediaCompanion.manifest)
    uac_admin=False,
    uac_uiaccess=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="DeskMediaCompanion",
)

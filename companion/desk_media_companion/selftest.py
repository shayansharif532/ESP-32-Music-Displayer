"""Self-test of a packaged build:  DeskMediaCompanion.exe --selftest   (or: python -m desk_media_companion.selftest)

Proves, on a machine without Python or any developer tool, that everything the companion needs at run time was bundled and loads:
the WinRT media-session projection, Core Audio (pycaw / comtypes), Pillow (including its JPEG encoder), psutil, pystray, tkinter / Tcl,
pyserial's port enumeration, the application's own modules (every module of the package is imported, so a forgotten hidden import
shows up here) and the DPAPI-protected pairing store.

It prints exactly ONE JSON line and exits 0 (all checks ok) or 1:

    {"selftest": "DeskMediaCompanion", "ok": true, "version": "0.1.0", "frozen": true, "checks": {"pillow": {"ok": true, "detail": "..."}, ...}, ...}

When the process has no stdout (the windowless exe) the same line is also written to %TEMP%\\deskmedia-selftest.json.

What it never does: open a window, bind a socket, open a serial port (comports() only lists), start the media monitor, change
the auto-start entry, or touch the real data directory (the pairing store and the idle-screen store are exercised in a throw-away
temporary directory that is deleted again). It does read two things from the machine: the number of media sessions (a read-only
WinRT call) and the number of audio devices (a read-only Core Audio call), and it reports counts only, never titles or names.

Checks that cannot apply yet report {"ok": true, "status": "absent"} (e.g. the USB-link modules before they exist); a missing
runtime dependency, an import error or a wrong result is {"ok": false, "reason": "..."}.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Optional

from . import __version__

NAME = "DeskMediaCompanion"
RESULT_FILE = "deskmedia-selftest.json"       # in %TEMP%, written only when the process has no stdout
OPTIONAL_MODULES = ("framing", "usblink", "api", "config")      # the USB-first modules: "absent" is fine until they exist
THREAD_TIMEOUT_S = 25.0

_results: dict[str, dict[str, Any]] = {}
_versions: dict[str, str] = {}


def _short(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".replace("\n", " ").replace("\r", " ")
    return text[:240]


def _record(name: str, fn: Callable[[], Any]) -> None:
    """Runs one check; a string result is the detail, a dict is merged into the result, an exception is a failure."""
    try:
        out = fn()
        res: dict[str, Any] = {"ok": True}
        if isinstance(out, dict):
            res.update(out)
        elif out:
            res["detail"] = str(out)[:200]
        _results[name] = res
    except Exception as exc:                       # a failing check must never take the whole test down
        _results[name] = {"ok": False, "reason": _short(exc)}


def _in_thread(fn: Callable[[], Any], timeout: float = THREAD_TIMEOUT_S) -> Any:
    """Runs fn in a fresh thread (own COM apartment, own event loop) and returns its result / re-raises its exception."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:               # noqa: BLE001
            box["error"] = exc

    t = threading.Thread(target=run, name="selftest-worker", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"no answer within {timeout:.0f} s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _module_version(mod: Any, *attrs: str) -> str:
    for a in attrs:
        v = getattr(mod, a, None)
        if v:
            return str(v)
    return ""


def _find(name: str) -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


# ---- the checks ---------------------------------------------------------------------------------------------------------

def check_runtime() -> dict[str, Any]:
    import struct
    if struct.calcsize("P") * 8 != 64:
        raise RuntimeError("not a 64-bit Python")
    win = sys.getwindowsversion() if hasattr(sys, "getwindowsversion") else None
    build = win.build if win else 0
    if win is not None and build < 17763:
        raise RuntimeError(f"Windows build {build} is older than 17763 (Windows 10 1809): no media-session API")
    _versions["python"] = sys.version.split()[0]
    return {"detail": f"python {sys.version.split()[0]} x64, windows build {build}"}


def check_winrt() -> dict[str, Any]:
    """The GSMTC projection: import the same names media.py uses, then ask Windows for the session manager (read-only)."""
    from winrt.windows.media import MediaPlaybackAutoRepeatMode
    from winrt.windows.media.control import (GlobalSystemMediaTransportControlsSessionManager as Manager,
                                             GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status)
    from winrt.windows.storage.streams import DataReader
    import winrt.windows.foundation  # noqa: F401  (the projection's own hidden imports)
    import winrt.windows.foundation.collections  # noqa: F401
    if not (hasattr(MediaPlaybackAutoRepeatMode, "TRACK") and hasattr(Status, "PLAYING") and hasattr(DataReader, "from_buffer")):
        raise RuntimeError("the winrt projection loaded but its types are incomplete")

    def ask() -> int:
        import asyncio

        async def go() -> int:
            mgr = await Manager.request_async()
            return len(list(mgr.get_sessions()))
        return asyncio.run(asyncio.wait_for(go(), 12))
    count = _in_thread(ask)
    _versions["winrt"] = _module_version(sys.modules.get("winrt"), "__version__") or _dist_version("winrt-runtime")
    return {"detail": f"session manager reachable, {count} media session(s) right now"}


def _dist_version(name: str) -> str:
    try:
        from importlib import metadata
        return metadata.version(name)
    except Exception:                              # metadata is not always bundled; the version is informational only
        return ""


def check_coreaudio() -> dict[str, Any]:
    import comtypes
    from pycaw.pycaw import AudioUtilities, EDataFlow, IAudioSessionManager2  # noqa: F401
    _versions["comtypes"] = _module_version(comtypes, "__version__") or _dist_version("comtypes")
    _versions["pycaw"] = _dist_version("pycaw")

    def enumerate_devices() -> str:
        comtypes.CoInitialize()
        try:
            try:
                enumerator = AudioUtilities.GetDeviceEnumerator()
                devices = enumerator.EnumAudioEndpoints(EDataFlow.eRender.value, 1)
                return f"{devices.GetCount()} active playback device(s)"
            except comtypes.COMError as exc:       # a PC without an audio service / device: a machine limit, not a packaging fault
                return f"audio devices not enumerable here ({_short(exc)})"
        finally:
            comtypes.CoUninitialize()
    return {"detail": _in_thread(enumerate_devices)}


def _test_image(size: int = 96):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (size, size))
    d = ImageDraw.Draw(img)
    for y in range(size):
        d.line((0, y, size, y), fill=(int(255 * y / size), 80, 255 - int(255 * y / size)))
    d.ellipse((size // 4, size // 4, size * 3 // 4, size * 3 // 4), outline=(255, 255, 255), width=3)
    return img


def check_pillow() -> dict[str, Any]:
    import PIL
    from PIL import Image, ImageFilter, ImageFont, features
    _versions["pillow"] = PIL.__version__
    missing = [f for f in ("jpg", "zlib", "freetype2") if not features.check(f)]
    if missing:
        raise RuntimeError("Pillow built without: " + ", ".join(missing))
    img = _test_image()
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85, optimize=False, progressive=False)        # the baseline JPEG the display decodes
    jpeg = buf.getvalue()
    if jpeg[:3] != b"\xff\xd8\xff" or len(jpeg) < 600:
        raise RuntimeError("the JPEG encoder produced no usable file")
    back = Image.open(io.BytesIO(jpeg))
    back.load()
    if back.size != img.size or back.format != "JPEG" or "progressive" in back.info:
        raise RuntimeError("JPEG round trip changed the image")
    png = io.BytesIO()
    img.save(png, "PNG")
    Image.open(io.BytesIO(png.getvalue())).load()
    img.filter(ImageFilter.GaussianBlur(2)).resize((48, 48), Image.LANCZOS)
    font = ImageFont.load_default(size=20)         # FreeType font (the idle screen clock)
    if font.getlength("12:34") <= 0:
        raise RuntimeError("the default font has no metrics")
    from PIL import ImageTk  # noqa: F401  (imports _imagingtk; creates nothing)
    return {"detail": f"jpeg {len(jpeg)} bytes, png, blur, freetype, ImageTk; libjpeg {features.version('jpg')}"}


def check_psutil() -> dict[str, Any]:
    import psutil
    _versions["psutil"] = psutil.__version__
    me = psutil.Process()
    if not me.name() or psutil.cpu_count() is None or not psutil.pids():
        raise RuntimeError("psutil returned nothing")
    return {"detail": f"process '{me.name()}', {len(psutil.pids())} processes"}


def check_pystray() -> dict[str, Any]:
    import pystray
    from PIL import Image
    _versions["pystray"] = _module_version(pystray, "__version__") or ".".join(str(p) for p in getattr(pystray, "__version_info__", ())) \
        or _dist_version("pystray")
    if pystray.Icon.__module__ != "pystray._win32":
        raise RuntimeError(f"pystray picked the wrong backend: {pystray.Icon.__module__}")
    menu = pystray.Menu(pystray.MenuItem("x", lambda icon, item: None), pystray.Menu.SEPARATOR)
    pystray.Icon("selftest", Image.new("RGBA", (16, 16), (0, 0, 0, 255)), "selftest", menu)          # not run: no window, no tray icon
    from . import ui
    tray = ui.make_tray_icon_image()
    if tray.size != (64, 64):
        raise RuntimeError("the tray icon image has the wrong size")
    return {"detail": "backend pystray._win32, tray icon image generated"}


def check_tkinter() -> dict[str, Any]:
    """Tcl/Tk are importable and their script libraries were bundled. No Tk() root is created: that would open a window."""
    import _tkinter
    import tkinter
    from tkinter import colorchooser, filedialog, messagebox, ttk  # noqa: F401
    tcl = tkinter.Tcl()
    patch = tcl.eval("info patchlevel")
    tcl.eval("package require msgcat")             # needs init.tcl and the bundled library: fails if the Tcl data is missing
    tcl_lib = tcl.eval("info library")
    if not os.path.isfile(os.path.join(tcl_lib, "init.tcl")):
        raise RuntimeError(f"Tcl library not found at {tcl_lib}")
    tk_lib = os.environ.get("TK_LIBRARY", "")
    if getattr(sys, "frozen", False) and not os.path.isfile(os.path.join(tk_lib, "tk.tcl")):
        raise RuntimeError("the Tk script library was not bundled")
    _versions["tcl_tk"] = f"{patch} / Tk {_tkinter.TK_VERSION}"
    return {"detail": f"Tcl {patch}, Tk {_tkinter.TK_VERSION}, ttk / dialogs importable"}


def check_serial() -> dict[str, Any]:
    if not _find("serial"):
        if _find(f"{__package__}.usblink"):
            raise ImportError("pyserial is not bundled but the USB link module is")
        return {"status": "absent", "detail": "pyserial is not part of this build"}
    import serial
    import serial.tools.list_ports
    import serial.tools.list_ports_windows  # noqa: F401  (hidden import)
    _versions["pyserial"] = _module_version(serial, "VERSION", "__version__")
    ports = list(serial.tools.list_ports.comports())            # enumerates; never opens a port
    return {"detail": f"{len(ports)} serial port(s) enumerated, none opened"}


def check_package() -> dict[str, Any]:
    """Imports every module of the application: an import that PyInstaller did not see fails here, not in front of a user."""
    import importlib
    import pkgutil
    pkg = importlib.import_module(__package__)
    names = sorted(m.name for m in pkgutil.iter_modules(pkg.__path__))
    names = [n for n in names if n != "selftest"]
    if len(names) < 10:                                         # the iteration itself must work in a frozen build
        raise RuntimeError(f"only {len(names)} modules found in the package: {names}")
    failed = []
    for n in names:
        try:
            importlib.import_module(f"{__package__}.{n}")
        except BaseException as exc:                           # noqa: BLE001
            failed.append(f"{n} ({_short(exc)})")
    if failed:
        raise ImportError("; ".join(failed)[:240])
    return {"detail": f"{len(names)} modules imported"}


def check_optional_modules() -> dict[str, Any]:
    state = {}
    for n in OPTIONAL_MODULES:
        state[n] = "ok" if _find(f"{__package__}.{n}") else "absent"
    return {"detail": " ".join(f"{k}={v}" for k, v in state.items()), "modules": state}


def check_assets() -> dict[str, Any]:
    """The bundled data files (fonts for the idle screen): every file of the assets folder can be read; fonts load."""
    folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
    if not os.path.isdir(folder):
        return {"status": "absent", "detail": "no assets folder in this build"}
    files = sorted(f for f in os.listdir(folder) if os.path.isfile(os.path.join(folder, f)))
    if not files:
        raise RuntimeError("the assets folder is empty")
    from PIL import ImageFont
    for f in files:
        path = os.path.join(folder, f)
        if os.path.getsize(path) == 0:
            raise RuntimeError(f"{f} is empty")
        if f.lower().endswith((".ttf", ".otf")):
            if ImageFont.truetype(path, 24).getlength("12:34") <= 0:
                raise RuntimeError(f"{f} has no glyph metrics")
    return {"detail": f"{len(files)} file(s): {', '.join(files)}"[:200]}


def check_data_stores() -> dict[str, Any]:
    """The pairing store (DPAPI) and the idle-screen store, in a throw-away directory: write, read back, render, delete."""
    from . import idle
    from .pairing import FILE_NAME, PairingStore
    tmp = tempfile.mkdtemp(prefix="deskmedia-selftest-")
    try:
        dev, key = bytes(range(8)), bytes(range(32, 64))
        store = PairingStore(os.path.join(tmp, "data"))
        cid = store.cid
        store.set_device(dev, key)
        raw = open(os.path.join(tmp, "data", FILE_NAME), "rb").read()
        if key in raw or key.hex().encode() in raw:
            raise RuntimeError("the pairing file holds the key unprotected")
        if os.name == "nt" and raw[:5] != b"DMP1D":
            raise RuntimeError("the pairing file is not DPAPI-protected")
        again = PairingStore(os.path.join(tmp, "data"))
        if again.cid != cid or again.device is None or again.device.key != key or again.device.id != dev:
            raise RuntimeError("DPAPI round trip returned different data")
        again.clear_device()
        if PairingStore(os.path.join(tmp, "data")).device is not None:
            raise RuntimeError("forgetting the display did not stick")

        idle_dir = os.path.join(tmp, "data", "idle")
        istore = idle.IdleStore(idle_dir)
        istore.update(theme="neon", text="selftest")
        if idle.IdleStore(idle_dir).settings().theme != "neon":
            raise RuntimeError("idle-screen settings were not persisted")
        buf = io.BytesIO()
        _test_image(256).save(buf, "PNG")
        jpeg = idle.render_background(buf.getvalue(), "blur")      # decode -> fit -> baseline JPEG, as for a user's picture
        if jpeg[:3] != b"\xff\xd8\xff" or len(jpeg) > idle.JPEG_MAX_BYTES:
            raise RuntimeError("the idle-screen picture pipeline produced no valid JPEG")
        preview = idle.render_preview(istore.settings(), jpeg, 120)
        if preview.size != (120, 120):
            raise RuntimeError("the idle-screen preview has the wrong size")
        return {"detail": f"DPAPI round trip ok, idle settings persisted, picture {len(jpeg)} bytes, preview rendered"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_autostart() -> dict[str, Any]:
    """Only reads: the command the tray toggle would write, and whether the Run value exists. Writes nothing."""
    from . import autostart
    cmd = autostart._command()
    if getattr(sys, "frozen", False) and cmd != f'"{sys.executable}" --quiet':
        raise RuntimeError(f"unexpected auto-start command: {cmd}")
    return {"detail": f"command '{cmd}', registered={autostart.is_installed()}"}


CHECKS: list[tuple[str, Callable[[], Any]]] = [
    ("runtime", check_runtime),
    ("winrt_gsmtc", check_winrt),
    ("coreaudio_pycaw_comtypes", check_coreaudio),
    ("pillow", check_pillow),
    ("psutil", check_psutil),
    ("pystray", check_pystray),
    ("tkinter", check_tkinter),
    ("pyserial", check_serial),
    ("package_modules", check_package),
    ("usb_modules", check_optional_modules),
    ("assets", check_assets),
    ("data_stores_dpapi", check_data_stores),
    ("autostart_command", check_autostart),
]


def run() -> dict[str, Any]:
    started = time.monotonic()
    for name, fn in CHECKS:
        _record(name, fn)
    failed = sorted(k for k, v in _results.items() if not v.get("ok"))
    return {
        "selftest": NAME,
        "ok": not failed,
        "version": __version__,
        "frozen": bool(getattr(sys, "frozen", False)),
        "exe": sys.executable,
        "windows": ".".join(str(p) for p in sys.getwindowsversion()[:3]) if hasattr(sys, "getwindowsversion") else "",
        "seconds": round(time.monotonic() - started, 2),
        "versions": dict(sorted(_versions.items())),
        "failed": failed,
        "checks": _results,
    }


def emit(line: str, stdout: Optional[Any] = None) -> None:
    """The result line to stdout when there is one; in any case to %TEMP%\\deskmedia-selftest.json when stdout is missing."""
    out = sys.stdout if stdout is None else stdout
    written = False
    if out is not None:
        try:
            out.write(line + "\n")
            out.flush()
            written = True
        except (OSError, ValueError):
            written = False
    if not written:
        try:
            with open(os.path.join(tempfile.gettempdir(), RESULT_FILE), "w", encoding="utf-8", newline="\n") as f:
                f.write(line + "\n")
        except OSError:
            pass


def main() -> int:
    result = run()
    emit(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

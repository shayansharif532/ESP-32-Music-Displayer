"""Self-test of a packaged build:  DeskMediaCompanion.exe --selftest   (or: python -m desk_media_companion.selftest)

Proves, on a machine without Python or any developer tool, that everything the companion needs at run time was bundled and loads:
the WinRT media-session projection, Core Audio (pycaw / comtypes), Pillow (including its JPEG encoder), psutil, pystray, tkinter / Tcl,
the USB link (DMLINK framing against the test vectors of docs/usb-protocol.md section 10, pyserial's port enumeration - also with
zero ports), the bundled idle-screen font and the idle-screen preview with the date, the application's own modules (every module of the
package is imported, so a forgotten hidden import shows up here), the settings store and the DPAPI-protected pairing store.

It prints exactly ONE JSON line and exits 0 (all checks ok) or 1:

    {"selftest": "DeskMediaCompanion", "ok": true, "version": "1.0.0", "frozen": true, "checks": {"pillow": {"ok": true, "detail": "..."}, ...}, ...}

When the process has no stdout (the windowless exe) the same line is also written to %TEMP%\\deskmedia-selftest.json.

What it never does: open a window, bind a socket, open a serial port (comports() only lists), start the media monitor or the USB
link, change the auto-start entry, or touch the real data directory (the pairing, settings and idle-screen stores are exercised in a
throw-away temporary directory that is deleted again). It does read three things from the machine: the number of media sessions (a
read-only WinRT call), the number of audio devices (a read-only Core Audio call) and the number of serial ports Windows lists (a
SetupAPI enumeration); it reports counts only, never titles or names.

A missing runtime dependency, a missing bundled file, an import error or a wrong result is {"ok": false, "reason": "..."}; a check
that cannot apply on this machine says {"ok": true, "status": "..."}.
"""
from __future__ import annotations

import dataclasses
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
REQUIRED_MODULES = ("framing", "usblink", "api", "config", "runtime", "resources", "idle_date", "idle_layout")    # the USB-first core
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
    from . import ui                                # imports the UI module (tkinter / Pillow only: no window)
    make_image = getattr(ui, "make_tray_icon_image", None)
    if make_image is None:
        return {"detail": "backend pystray._win32 (the UI module has no tray image function to try)"}
    tray = make_image()
    if tray.width < 16 or tray.width != tray.height:
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
    """pyserial and the port enumeration the USB link scans with - on this machine and with zero ports. No port is ever opened."""
    import serial
    import serial.tools.list_ports
    import serial.tools.list_ports_windows  # noqa: F401  (hidden import: list_ports picks it at run time)
    from . import usblink
    _versions["pyserial"] = _module_version(serial, "VERSION", "__version__")
    real = usblink.list_serial_ports()                           # exactly what the scanner calls: enumeration only
    original = serial.tools.list_ports.comports
    serial.tools.list_ports.comports = lambda *a, **k: []         # a PC with no serial port at all
    try:
        none = usblink.list_serial_ports()
    finally:
        serial.tools.list_ports.comports = original
    if none != [] or usblink.select_ports(none) != [] or usblink.select_ports(none, "COM3") != []:
        raise RuntimeError("zero serial ports were not handled")
    sample = [usblink.PortInfo("COM1", None, None, "Communications Port"), usblink.PortInfo("COM7", usblink.USB_VID, usblink.USB_PID, "display")]
    if usblink.select_ports(sample) != ["COM7"]:
        raise RuntimeError("port selection by VID / PID is wrong")
    if not hasattr(usblink, "PySerialPort") or not hasattr(serial, "Serial"):
        raise RuntimeError("the serial port class is missing")
    return {"detail": f"{len(real)} serial port(s) listed (none opened), zero-port case ok, VID/PID selection ok"}


def check_framing() -> dict[str, Any]:
    """DMLINK framing against the independent test vectors of docs/usb-protocol.md section 10, byte by byte through the parser."""
    from . import framing as fr
    if fr.crc16_ccitt(b"123456789") != 0x29B1 or fr.crc32(b"123456789") != 0xCBF43926:
        raise RuntimeError("CRC check values are wrong")
    vectors = {                                                  # the hex strings are copied from the specification
        "ping": ("fa de 01 30 03 00 07 00 05 00 c9 f0 68 65 6c 6c 6f c0 c9 b3 e1", lambda: fr.encode_frame(fr.TYPE_PING, b"hello", msg_id=7)),
        "request": ("fa de 01 10 03 00 01 00 11 00 11 76 47 45 54 20 2f 6e 6f 77 2d 70 6c 61 79 69 6e 67 0a 64 50 41 b9",
                    lambda: fr.encode_request(1, "GET", "/now-playing")),
        "response": ("fa de 01 11 03 00 01 00 11 00 70 ce c8 00 0b 00 00 00 7b 22 6f 6b 22 3a 74 72 75 65 7d 48 39 aa 33",
                     lambda: fr.encode_response(1, 200, b'{"ok":true}')[0]),
        "beacon": ("fa de 01 01 03 00 00 00 0f 00 c3 af 7b 22 76 22 3a 31 2c 22 61 70 69 22 3a 32 7d 2e 27 e7 37",
                   lambda: fr.encode_frame(fr.TYPE_BEACON, b'{"v":1,"api":2}')),
        "chunk1": ("fa de 01 11 01 00 09 00 09 00 29 4a c8 00 06 00 00 00 61 62 63 7a bc ca 83",
                   lambda: fr.encode_response(9, 200, b"abcdef", max_payload=9)[0]),
        "chunk2": ("fa de 01 11 02 01 09 00 03 00 53 c1 64 65 66 3d 27 ed b5",
                   lambda: fr.encode_response(9, 200, b"abcdef", max_payload=9)[1]),
        "pong": ("fa de 01 31 03 00 00 00 00 00 70 e6 50 85 17 b6", lambda: fr.encode_frame(fr.TYPE_PONG)),
    }
    stream = b""
    for name, (hexstr, make) in vectors.items():
        want = bytes.fromhex(hexstr)
        if make() != want:
            raise RuntimeError(f"vector {name}: the encoder's bytes differ from the specification")
        stream += want
    parser = fr.FrameParser()
    frames = []
    for b in stream:                                              # one byte at a time: the worst chopping a serial port can do
        frames += parser.feed(bytes([b]))
    if [f.type for f in frames] != [0x30, 0x10, 0x11, 0x01, 0x11, 0x11, 0x31] or frames[0].payload != b"hello" or frames[0].msg_id != 7:
        raise RuntimeError("the parser did not return the vectors' frames")
    flipped = bytearray(bytes.fromhex(vectors["ping"][0]))
    flipped[14] ^= 0x01                                           # a payload byte: the specification's resynchronisation case
    after = fr.FrameParser().feed(b"boot banner\r\n" + bytes(flipped) + bytes.fromhex(vectors["ping"][0]))
    if len(after) != 1 or after[0].payload != b"hello":
        raise RuntimeError("the parser did not resynchronise after a corrupted frame")
    body = bytes(range(256)) * 40                                 # 10 KB: a multi-chunk response and back
    chunks = fr.FrameParser().feed(b"".join(fr.encode_response(5, 200, body)))
    status, total, first = fr.split_response_head(chunks[0].payload)
    if len(chunks) < 3 or status != 200 or total != len(body) or first + b"".join(c.payload for c in chunks[1:]) != body:
        raise RuntimeError("a chunked response did not round-trip")
    return {"detail": f"CRC check values, {len(vectors)} specification vectors, byte-wise parsing, resync, {len(chunks)}-chunk round trip"}


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


def check_link_modules() -> dict[str, Any]:
    """The USB-first core must be in the build: present and importable (check_package imports them too; this one names them)."""
    import importlib
    missing = [n for n in REQUIRED_MODULES if not _find(f"{__package__}.{n}")]
    if missing:
        raise ImportError("not in this build: " + ", ".join(missing))
    for n in REQUIRED_MODULES:
        importlib.import_module(f"{__package__}.{n}")
    return {"detail": ", ".join(REQUIRED_MODULES)}


def check_assets() -> dict[str, Any]:
    """The bundled data files, found the way the application finds them (resources.resource_path): the idle screen's typeface loads
    and is really the one in use (idle.py silently falls back to a stand-in font when the file is missing), its licence text is
    there, and every file of the assets folder is readable."""
    from PIL import ImageFont
    from .resources import resource_path
    font_file = resource_path("Montserrat-Medium.ttf")
    folder = os.path.dirname(font_file)
    if not os.path.isfile(font_file):
        raise FileNotFoundError(f"Montserrat-Medium.ttf is not bundled (looked in {os.path.basename(os.path.dirname(folder))}/{os.path.basename(folder)})")
    font = ImageFont.truetype(font_file, 40)
    family = font.getname()[0]
    if family != "Montserrat" or font.getlength("12:34") <= 0:
        raise RuntimeError(f"the bundled font is not Montserrat ({family})")
    if not os.path.isfile(resource_path("OFL-1.1-Montserrat.txt")):
        raise FileNotFoundError("the font's licence text (OFL-1.1-Montserrat.txt) is not bundled")
    files = sorted(f for f in os.listdir(folder) if os.path.isfile(os.path.join(folder, f)))
    for f in files:
        with open(os.path.join(folder, f), "rb") as fh:
            if not fh.read(16):
                raise RuntimeError(f"{f} is empty")
    from . import idle
    used = getattr(idle, "_montserrat", None)
    if used is not None and used(24).getname()[0] != "Montserrat":
        raise RuntimeError("the idle screen uses its stand-in font instead of Montserrat")
    return {"detail": f"{len(files)} file(s): {', '.join(files)}; Montserrat Medium loads and is the idle screen's font"[:200]}


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

        # the idle screen as the settings window draws it, with clock, date and a custom line, at the display's own 480 px
        import datetime as dt
        shown = dataclasses.replace(istore.settings(), theme="dark", show_clock=True, clock_24h=True, show_date=True,
                                    date_format="mon_d_y", text="selftest")
        boxes: dict = {}
        full = idle.render_preview(shown, None, 480, now=dt.datetime(2026, 10, 13, 14, 5), boxes=boxes)
        if full.size != (480, 480) or not all(k in boxes for k in ("clock", "date", "text")):
            raise RuntimeError(f"the idle-screen preview lacks elements: {sorted(boxes)}")
        for name in ("clock", "date"):
            left, top, right, bottom = boxes[name]
            if right - left < 60 or bottom - top < 10 or abs((left + right) / 2 - 240) > 30:
                raise RuntimeError(f"the preview's {name} is not where the display puts it: {boxes[name]}")
        if idle.format_date(2026, 10, 13, "mon_d_y") != "Oct 13, 2026" or idle.format_clock_ms(0, "dmy") != "01/01/1970":
            raise RuntimeError("the date formatting is wrong")

        from .config import SettingsStore
        settings_dir = os.path.join(tmp, "data")
        st = SettingsStore(settings_dir)
        if st.get().usb_enabled is not True or st.get().lan_enabled is not False:
            raise RuntimeError("unexpected default settings (USB on, LAN off expected)")
        st.update(lan_enabled=True)
        if SettingsStore(settings_dir).get().lan_enabled is not True:
            raise RuntimeError("the application settings were not persisted")
        from .runtime import describe_usb
        if not describe_usb({"enabled": True, "state": "searching"}):
            raise RuntimeError("no status text for the USB state")
        return {"detail": f"DPAPI round trip ok, idle settings + settings persisted, picture {len(jpeg)} bytes, 480 px idle preview with "
                          f"clock / date / text rendered"}
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
    ("link_modules", check_link_modules),
    ("usb_framing_vectors", check_framing),
    ("assets_font", check_assets),
    ("data_stores_dpapi_idle_preview", check_data_stores),
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

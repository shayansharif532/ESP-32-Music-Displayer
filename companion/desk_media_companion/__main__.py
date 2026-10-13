"""python -m desk_media_companion [--console] [--lan] [--no-usb] [--usb-port COM5] ...   (or DeskMediaCompanion.exe)

The display is reached over the USB cable by default. The LAN transport (HTTP server + UDP discovery beacon) is optional: it runs when the
setting `lan_enabled` is on or the process is started with --lan.
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import sys

from . import __version__

DEFAULT_PORT = 8765
DEFAULT_DISCOVERY_PORT = 8766
INSTANCE_MUTEX = "Local\\DeskMediaCompanion"


def _say(message: str) -> None:
    """A line for the user's console - when there is one (under pythonw / the frozen exe there is no stderr)."""
    if sys.stderr is not None:
        print(message, file=sys.stderr)


def _setup_logging(console: bool, debug: bool = False) -> str:
    base = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "DeskMediaCompanion")
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, "companion.log")
    handlers: list[logging.Handler] = [logging.handlers.RotatingFileHandler(path, maxBytes=512_000, backupCount=2, encoding="utf-8")]
    if console and sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    return path


def _pair_console(pairing) -> None:
    """Headless pairing (--pair-console): prints the code once the display has revealed it and reads y / n from stdin. The code goes to
    the console only - never into the log file."""
    import time
    prompted = False
    while True:
        snap = pairing.snapshot()
        if snap["state"] == "paired":
            print("Display paired.", flush=True)
            return
        if snap["code"] and not prompted:
            prompted = True
            print(f"Pairing offer: code {snap['code']} - confirm? [y/N]", flush=True)
            try:
                answer = sys.stdin.readline().strip().lower() if sys.stdin is not None else ""
            except (OSError, ValueError):
                answer = ""
            if answer in ("y", "yes"):
                print("Confirmed." if pairing.pc_confirm() else "Too late: the offer is gone.", flush=True)
            else:
                pairing.pc_cancel()
                print("Cancelled.", flush=True)
            continue
        if snap["state"] in ("cancelled", "expired") or not snap["window_open"]:
            print("The pairing window closed without a pairing.", flush=True)
            return
        time.sleep(0.3)


_instance_locks: list = []        # keeps the named mutexes alive for the life of the process


def _acquire_lock(name: str) -> bool:
    """Takes the named Windows mutex; False when another process already holds it. (A listening port is no "already running" signal: on
    Windows two sockets may share a port when SO_REUSEADDR is set, so a second copy would silently share it and answer some requests with
    its own, differently paired, state. A COM port is exclusive, but a second copy would still fight over the tray, the pairing and the
    settings.) Elsewhere there is nothing to take and the answer is True."""
    if os.name != "nt":
        return True
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)           # type: ignore[attr-defined]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    handle = kernel32.CreateMutexW(None, 0, name)
    already = ctypes.get_last_error() == 183                             # ERROR_ALREADY_EXISTS
    _instance_locks.append(handle)
    return not already


def _acquire_instance_locks(use_usb: bool, lan_port: int | None) -> str:
    """The single-instance rules; returns "" when this process may run, else the name of the lock somebody else holds.

    * One companion per Windows session owns the display: `Local\\DeskMediaCompanion`, taken by every instance that may use USB. It does not
      depend on the HTTP port (the USB-first product has none by default).
    * An instance with the LAN transport on also takes a per-port lock: two companions sharing one HTTP port is exactly the bug the old
      port mutex prevented. A LAN-only development instance (`--no-usb --lan --port N`, what the test tools start) takes only this one, so
      it can run next to the product's companion.
    """
    if use_usb and not _acquire_lock(INSTANCE_MUTEX):
        return INSTANCE_MUTEX
    if lan_port is not None:
        name = f"{INSTANCE_MUTEX}-lan-{lan_port}"
        if not _acquire_lock(name):
            return name
    return ""


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="desk-media-companion", description=__doc__)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="HTTP port of the LAN transport (default %(default)s; only used with --lan / the LAN setting)")
    ap.add_argument("--discovery-port", type=int, default=DEFAULT_DISCOVERY_PORT, help="UDP port of the LAN discovery beacon")
    ap.add_argument("--no-discovery", action="store_true", help="do not broadcast the LAN discovery beacon")
    ap.add_argument("--lan", action="store_true", help="also serve the display over the local network (HTTP server + discovery beacon) for this run, "
                                                       "even when the LAN setting is off. Without it (and with the setting off) no port is opened")
    ap.add_argument("--no-usb", action="store_true", help="do not talk to the display over USB (development: LAN only)")
    ap.add_argument("--usb-port", default="", metavar="COMx", help="development: probe only this serial port (any VID / PID) instead of every "
                                                                  "USB-Serial/JTAG port (VID 303A / PID 1001)")
    ap.add_argument("--console", action="store_true", help="also log to the console")
    ap.add_argument("--quiet", action="store_true", help="background mode (used by auto-start): file log only")
    ap.add_argument("--debug", action="store_true", help="verbose log (every HTTP request)")
    ap.add_argument("--dev-tools", action="store_true", help="enable /debug/* routes used by tools/screenshot.py (development only)")
    ap.add_argument("--prefer-app", default="", help="development only: always show this app's media session when it exists "
                                                     "(e.g. python.exe for the fake test session)")
    ap.add_argument("--treat-as-spotify", default="", help="development only: treat this app id (e.g. python.exe, the fake test session) as Spotify, "
                                                          "so the shuffle / repeat buttons can be tested without touching the real Spotify")
    ap.add_argument("--only-apps", default="", help="development only: ignore every media session except these app ids (comma separated), "
                                                   "for isolated tests next to real apps")
    ap.add_argument("--ignore-apps", default="", help="development only: never look at these app ids (comma separated), e.g. the fake test "
                                                     "sessions python.exe,deskfakeb.exe, so tests do not show up on the real display")
    ap.add_argument("--no-tray", action="store_true", help="no system-tray icon / settings window (tests, headless runs)")
    ap.add_argument("--open-settings", action="store_true", help="open the Idle Screen settings window at start (it is always reachable from the tray icon)")
    ap.add_argument("--data-dir", default="", help="where the idle-screen settings, the picture, the pairing key and the app settings are kept "
                                                   "(default: the DeskMediaCompanion folder in your Local AppData)")
    ap.add_argument("--bind", default="0.0.0.0", metavar="ADDR",   # nosec B104: all interfaces by design (private clients only, paired)
                    help="LAN transport: local address the HTTP API listens on (default %(default)s = every interface; only private / loopback / "
                         "link-local clients are served either way, and all but this PC itself must be paired). 127.0.0.1 = this PC only")
    ap.add_argument("--pair-console", action="store_true", help="headless pairing: open the pairing window at start (120 s); the code is printed "
                                                                "here and y / n on stdin confirms or cancels (no tray needed)")
    ap.add_argument("--probe", action="store_true", help="print the current media state once and exit")
    ap.add_argument("--install-startup", action="store_true", help="start automatically when you log in to Windows")
    ap.add_argument("--remove-startup", action="store_true", help="undo --install-startup")
    ap.add_argument("--version", action="version", version=__version__)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.install_startup or args.remove_startup:
        from . import autostart
        if args.install_startup:
            print("Auto-start installed:", autostart.install())
        else:
            print("Auto-start removed." if autostart.remove() else "Auto-start was not installed.")
        return 0

    log_path = _setup_logging(console=args.console or args.probe, debug=args.debug)
    log = logging.getLogger("main")

    from .media import MediaMonitor
    monitor = MediaMonitor()
    monitor.prefer_app = args.prefer_app
    if args.ignore_apps:
        monitor.ignore_apps = {a.strip().lower() for a in args.ignore_apps.split(",") if a.strip()}
    if args.only_apps:
        monitor.only_apps = {a.strip().lower() for a in args.only_apps.split(",") if a.strip()}
    if args.treat_as_spotify:
        monitor.spotify_ids = {a.strip().lower() for a in args.treat_as_spotify.split(",") if a.strip()}
    monitor.start()

    if args.probe:
        snap = monitor.now_playing()
        print(json.dumps(snap, indent=2, ensure_ascii=False))
        art = monitor.artwork()
        print("artwork:", f"{len(art[1])} bytes (id {art[0]})" if art else "none")
        return 0

    from .config import SettingsStore
    settings = SettingsStore(args.data_dir or None)

    from .idle import IdleStore
    store = IdleStore(os.path.join(args.data_dir, "idle") if args.data_dir else None)
    log.info("idle screen settings: %s (version %d, theme %s)", store.dir, store.version, store.settings().theme)

    from .pairing import Pairing, PairingStore
    pairing = Pairing(PairingStore(args.data_dir or None))
    log.info("display: %s", "paired" if pairing.paired else "not paired")

    use_usb = not args.no_usb and settings.get().usb_enabled
    use_lan = args.lan or settings.get().lan_enabled
    held = _acquire_instance_locks(not args.no_usb, args.port if use_lan else None)
    if held:
        log.error("another Desk Media Companion is already running (%s)", held)
        _say("Another Desk Media Companion is already running.")
        return 2

    from .runtime import AppRuntime
    runtime = AppRuntime(monitor, store, pairing, settings, dev_tools=args.dev_tools, http_port=args.port, discovery_port=args.discovery_port,
                         bind=args.bind, discovery=not args.no_discovery, force_lan=args.lan, disable_usb=args.no_usb, usb_port=args.usb_port)
    runtime.start()
    if use_lan and not runtime.lan_running:
        _say(f"Cannot listen on port {args.port} ({runtime.lan_error or 'unknown error'}): is another copy running?")
        if not use_usb:                                  # nothing left to serve the display with: the old behaviour (exit 2)
            runtime.stop()
            return 2
    log.info("Desk Media Companion %s: USB %s, LAN %s (log: %s)", __version__, "on" if use_usb else "off",
             f"on, port {args.port}" if runtime.lan_running else "off", log_path)

    import threading
    if args.pair_console:
        pairing.open_window()
        threading.Thread(target=_pair_console, args=(pairing,), name="pair-console", daemon=True).start()
    tray = None
    if not args.no_tray:
        from . import ui
        window = ui.SettingsWindow(store, pairing)
        tray = ui.start_tray(window, on_quit=runtime.request_quit, pairing=pairing)
        if args.open_settings:
            window.open()
    try:
        runtime.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if tray is not None:
            try:
                tray.stop()
            except Exception:
                pass
        runtime.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

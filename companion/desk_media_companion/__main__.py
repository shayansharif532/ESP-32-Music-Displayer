"""python -m desk_media_companion [--console] [--port 8765] ...   (or DeskMediaCompanion.exe)"""
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


_instance_lock = None        # keeps the named mutex alive for the life of the process


def _single_instance(port: int) -> bool:
    """False when another companion already serves this port. (On Windows two sockets may share a listening port when
    SO_REUSEADDR is set, so a failed bind is not a reliable "already running" signal: a second auto-started copy would
    silently share the port and answer some requests with its own, differently paired, state.)"""
    global _instance_lock
    if os.name != "nt":
        return True
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)           # type: ignore[attr-defined]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    _instance_lock = kernel32.CreateMutexW(None, 0, f"Local\\DeskMediaCompanion-{port}")
    return ctypes.get_last_error() != 183                                # ERROR_ALREADY_EXISTS


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="desk-media-companion", description=__doc__)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="HTTP port for the display (default %(default)s)")
    ap.add_argument("--discovery-port", type=int, default=DEFAULT_DISCOVERY_PORT, help="UDP port of the discovery beacon")
    ap.add_argument("--no-discovery", action="store_true", help="do not broadcast the discovery beacon")
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
    ap.add_argument("--data-dir", default="", help="where the idle-screen settings, the picture and the pairing key are kept (default: the DeskMediaCompanion "
                                                   "folder in your Local AppData)")
    ap.add_argument("--bind", default="0.0.0.0", metavar="ADDR",   # nosec B104: all interfaces by design (private clients only, paired)
                    help="local address the HTTP API listens on (default %(default)s = every interface; only private / loopback / "
                         "link-local clients are served either way, and all but this PC itself must be paired). 127.0.0.1 = this PC only")
    ap.add_argument("--pair-console", action="store_true", help="headless pairing: open the pairing window at start (120 s); the code is printed "
                                                                "here and y / n on stdin confirms or cancels (no tray needed)")
    ap.add_argument("--probe", action="store_true", help="print the current media state once and exit")
    ap.add_argument("--install-startup", action="store_true", help="start automatically when you log in to Windows")
    ap.add_argument("--remove-startup", action="store_true", help="undo --install-startup")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)

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

    from .idle import IdleStore
    store = IdleStore(os.path.join(args.data_dir, "idle") if args.data_dir else None)
    log.info("idle screen settings: %s (version %d, theme %s)", store.dir, store.version, store.settings().theme)

    from .pairing import Pairing, PairingStore
    pairing = Pairing(PairingStore(args.data_dir or None))
    log.info("display: %s", "paired" if pairing.paired else "not paired")

    if not _single_instance(args.port):
        log.error("another Desk Media Companion is already running on port %d", args.port)
        if sys.stderr is not None:
            print(f"Another Desk Media Companion is already running on port {args.port}.", file=sys.stderr)
        return 2

    from .server import make_server
    try:
        server = make_server(monitor, args.port, dev_tools=args.dev_tools, idle=store, pairing=pairing, bind=args.bind)
    except OSError as exc:
        log.error("cannot listen on %s:%d (already running?): %s", args.bind, args.port, exc)
        if sys.stderr is not None:
            print(f"Cannot listen on port {args.port}: {exc}", file=sys.stderr)
        return 2

    beacon = None
    if not args.no_discovery:
        from .discovery import Beacon
        beacon = Beacon(args.port, args.discovery_port, pairing)
        beacon.start()

    log.info("Desk Media Companion %s listening on %s:%d (log: %s)", __version__, args.bind, args.port, log_path)
    import threading
    if args.pair_console:
        pairing.open_window()
        threading.Thread(target=_pair_console, args=(pairing,), name="pair-console", daemon=True).start()
    tray = None
    if not args.no_tray:
        from . import ui
        window = ui.SettingsWindow(store, pairing)
        tray = ui.start_tray(window, on_quit=lambda: threading.Thread(target=server.shutdown, daemon=True).start(), pairing=pairing)
        if args.open_settings:
            window.open()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if tray is not None:
            try:
                tray.stop()
            except Exception:
                pass
        if beacon:
            beacon.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

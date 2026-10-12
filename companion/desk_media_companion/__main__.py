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

    from .server import make_server
    try:
        server = make_server(monitor, args.port, dev_tools=args.dev_tools)
    except OSError as exc:
        log.error("cannot listen on port %d (already running?): %s", args.port, exc)
        if sys.stderr is not None:
            print(f"Cannot listen on port {args.port}: {exc}", file=sys.stderr)
        return 2

    beacon = None
    if not args.no_discovery:
        from .discovery import Beacon
        beacon = Beacon(args.port, args.discovery_port)
        beacon.start()

    log.info("Desk Media Companion %s listening on 0.0.0.0:%d (log: %s)", __version__, args.port, log_path)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if beacon:
            beacon.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

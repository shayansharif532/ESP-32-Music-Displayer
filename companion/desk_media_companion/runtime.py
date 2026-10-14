"""The running companion as one object: the shared `Api`, the USB link manager and the (optional) LAN server, plus the status the tray UI reads.

    runtime = AppRuntime(monitor, idle_store, pairing, settings)
    runtime.start()                      # USB scanner (unless disabled) + HTTP server and UDP beacon (only when LAN is enabled)
    runtime.status()                     # {"usb": {...LinkManager.status()..., "enabled": bool}, "lan": {...}, "paired": bool, ...}
    runtime.describe()                   # one human line for a tooltip: "Display connected (USB)"
    runtime.add_listener(callback)       # events from the USB scanner thread (+ "lan" / "settings" events): the UI marshals to its thread
    runtime.set_lan_enabled(True)        # starts / stops the HTTP server and the beacon at once, and remembers the choice
    runtime.set_usb_enabled(False)
    runtime.request_quit(); runtime.wait()

Both transports serve the same `Api` object, so a route behaves identically on the cable and on the network. The LAN part is OFF unless
the setting `lan_enabled` is on or the process was started with `--lan` (then only for this run): with it off the product opens no
listening socket at all, so a USB-only installation never triggers the Windows Firewall prompt.

No WinRT here: the media monitor is passed in, so this module is testable anywhere. Thread-safety: one RLock guards the transport
switches; `status()` can be called from any thread.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from . import __version__
from .api import Api, MediaBackend
from .config import SettingsStore
from .idle import IdleStore
from .pairing import Pairing

log = logging.getLogger("runtime")

DEFAULT_HTTP_PORT = 8765
DEFAULT_DISCOVERY_PORT = 8766

_DESCRIPTIONS = {
    "searching": "Looking for the display (USB)...",
    "probing": "Connecting to the display...",
    "connected": "Display connected (USB)",
    "unpaired": "Display found, but not paired with this PC",
    "pairing": "Pairing a display...",
    "busy": "The display's USB port is in use by another program",
    "disabled": "USB is switched off",
}


def describe_usb(usb: dict) -> str:
    """One short English line for a `LinkManager.status()` (plus `enabled`): what a tooltip or a status row should say."""
    if not usb.get("enabled", True):
        return _DESCRIPTIONS["disabled"]
    state = usb.get("state", "searching")
    if state == "incompatible":
        return "The display's firmware needs an update" if usb.get("update_needed") == "display" else "This app needs an update for the display"
    if state == "unpaired" and usb.get("detail") == "key_mismatch":
        return "The display has a different pairing key: pair it again"
    if state == "unpaired" and usb.get("detail") == "display_forgot":
        return "The display lost its pairing: pair it again"
    return _DESCRIPTIONS.get(state, _DESCRIPTIONS["searching"])


class AppRuntime:
    """Owns the transports. Construct, `start()`, read `status()`, `stop()`."""

    def __init__(self, monitor: MediaBackend, idle: Optional[IdleStore], pairing: Pairing, settings: SettingsStore, *,
                 dev_tools: bool = False, http_port: int = DEFAULT_HTTP_PORT, discovery_port: int = DEFAULT_DISCOVERY_PORT,
                 bind: str = "0.0.0.0",   # nosec B104: every interface by design (private clients only, paired), as before
                 discovery: bool = True, force_lan: bool = False, disable_usb: bool = False, usb_port: str = "",
                 lister=None, opener=None, log_sink=None) -> None:
        self.monitor = monitor
        self.idle = idle
        self.pairing = pairing
        self.settings = settings
        self.api = Api(monitor, idle, pairing, dev_tools)
        self._dev_tools = dev_tools
        self._http_port = http_port
        self._discovery_port = discovery_port
        self._bind = bind
        self._discovery = discovery
        # None = the saved setting decides; True / False = a command-line override for this run (--lan, --no-usb). A switch made through
        # set_*_enabled(persist=True) clears the override: the user's last word wins.
        self._lan_override: Optional[bool] = True if force_lan else None
        self._usb_override: Optional[bool] = False if disable_usb else None
        self._usb_port = usb_port                   # --usb-port COMx
        self._lister, self._opener, self._log_sink = lister, opener, log_sink
        self._lock = threading.RLock()              # serialises the transport switches (never taken by the scanner thread)
        self._listener_lock = threading.Lock()      # the listener list only: events come from the scanner thread, which stop() joins
        self._listeners: list[Callable[[dict], None]] = []
        self._manager = None
        self._server = None
        self._server_thread: Optional[threading.Thread] = None
        self._beacon = None
        self._lan_error = ""
        self._started = False
        self._quit = threading.Event()

    # -- what is wanted ---------------------------------------------------------------------------------------------------------------
    @property
    def usb_wanted(self) -> bool:
        return self.settings.get().usb_enabled if self._usb_override is None else self._usb_override

    @property
    def lan_wanted(self) -> bool:
        return self.settings.get().lan_enabled if self._lan_override is None else self._lan_override

    # -- lifecycle ----------------------------------------------------------------------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            self._started = True
            self._apply()

    def stop(self) -> None:
        with self._lock:
            self._started = False
            self._stop_usb()
            self._stop_lan()

    def request_quit(self) -> None:
        """For the tray's Quit item (any thread): lets `wait()` return."""
        self._quit.set()

    def wait(self) -> None:
        """Blocks the calling (main) thread until `request_quit()`; wakes twice a second so Ctrl+C is honoured on Windows."""
        while not self._quit.wait(0.5):
            pass

    @property
    def quit_requested(self) -> bool:
        return self._quit.is_set()

    # -- switches for the UI ------------------------------------------------------------------------------------------------------------
    def set_lan_enabled(self, enabled: bool, persist: bool = True) -> bool:
        """Starts / stops the HTTP server and the discovery beacon now. `persist=True` saves the choice as the setting (and ends a `--lan`
        override of this run); `persist=False` only switches for this run. Returns whether the LAN transport is running afterwards."""
        with self._lock:
            if persist:
                self.settings.update(lan_enabled=bool(enabled))
                self._lan_override = None
            else:
                self._lan_override = bool(enabled)
            if self._started:
                self._apply()
            running = self._server is not None
        self._emit({"type": "settings"})
        return running

    def set_usb_enabled(self, enabled: bool, persist: bool = True) -> None:
        with self._lock:
            if persist:
                self.settings.update(usb_enabled=bool(enabled))
                self._usb_override = None
            else:
                self._usb_override = bool(enabled)
            if self._started:
                self._apply()
        self._emit({"type": "settings"})

    def set_auto_pair(self, enabled: bool) -> None:
        """The setting `usb_auto_pair`: whether an unpaired display on the cable opens the pairing window by itself."""
        with self._lock:
            self.settings.update(usb_auto_pair=bool(enabled))
            if self._manager is not None:
                self._manager.auto_pair = bool(enabled)
        self._emit({"type": "settings"})

    def _apply(self) -> None:
        """Bring the transports in line with what is wanted (lock held)."""
        if self.usb_wanted and self._manager is None:
            self._start_usb()
        elif not self.usb_wanted and self._manager is not None:
            self._stop_usb()
        if self.lan_wanted and self._server is None:
            self._start_lan()
        elif not self.lan_wanted and self._server is not None:
            self._stop_lan()

    # -- USB ---------------------------------------------------------------------------------------------------------------------------------
    def _start_usb(self) -> None:
        from .usblink import LinkManager, list_serial_ports, open_serial_port
        manager = LinkManager(self.api, lister=self._lister or list_serial_ports, opener=self._opener or open_serial_port,
                              only_port=self._usb_port, log_sink=self._log_sink, auto_pair=self.settings.get().usb_auto_pair)
        manager.add_listener(self._emit)
        manager.start()
        self._manager = manager
        log.info("USB transport started%s", f" (port {self._usb_port} only)" if self._usb_port else "")

    def _stop_usb(self) -> None:
        manager, self._manager = self._manager, None
        if manager is not None:
            manager.stop()
            log.info("USB transport stopped")

    # -- LAN ---------------------------------------------------------------------------------------------------------------------------------
    def _start_lan(self) -> bool:
        from .server import make_server
        try:
            server = make_server(self.monitor, self._http_port, dev_tools=self._dev_tools, idle=self.idle, pairing=self.pairing,
                                 bind=self._bind, api=self.api)
        except OSError as exc:                          # the port is taken, the address does not exist, ...
            self._lan_error = f"{type(exc).__name__}"
            log.error("cannot listen on %s:%d: %s", self._bind, self._http_port, exc)
            return False
        self._lan_error = ""
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, name="http", daemon=True)
        thread.start()
        self._server, self._server_thread = server, thread
        if self._discovery:
            from .discovery import Beacon
            self._beacon = Beacon(server.server_address[1], self._discovery_port, self.pairing)
            self._beacon.start()
        log.info("LAN transport listening on %s:%d", self._bind, server.server_address[1])
        return True

    def _stop_lan(self) -> None:
        server, self._server = self._server, None
        beacon, self._beacon = self._beacon, None
        thread, self._server_thread = self._server_thread, None
        if beacon is not None:
            beacon.stop()
        if server is not None:
            server.shutdown()
            server.server_close()
            if thread is not None:
                thread.join(2.0)
            log.info("LAN transport stopped")

    @property
    def lan_error(self) -> str:
        """Why the LAN server could not start ("" = no problem); `--lan` with a taken port ends up here instead of crashing."""
        return self._lan_error

    @property
    def lan_running(self) -> bool:
        return self._server is not None

    # -- events and status ----------------------------------------------------------------------------------------------------------------
    def add_listener(self, cb: Callable[[dict], None]) -> None:
        """Events: the USB scanner's {"type": "status" | "pair-request", ...} (see usblink.LinkManager) and {"type": "settings"} after a
        switch. Called from the thread that raised them (the scanner thread for USB events), never with a lock of this object held by
        the caller: marshal to your own thread."""
        with self._listener_lock:
            self._listeners.append(cb)

    def remove_listener(self, cb: Callable[[dict], None]) -> None:
        with self._listener_lock:
            if cb in self._listeners:
                self._listeners.remove(cb)

    def _emit(self, event: dict) -> None:
        with self._listener_lock:
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(event)
            except Exception:
                log.warning("a status listener failed")

    def usb_status(self) -> dict:
        manager = self._manager                        # a plain reference read: status() must never wait for a transport switch
        if manager is None:
            return {"enabled": False, "state": "disabled", "detail": "", "port": "", "dev": "", "dev_short": "", "fw": "", "api": 0,
                    "min_api": 0, "display_paired": False, "paired_with_you": False, "compatible": True, "update_needed": "",
                    "authenticated": False, "link": {}, "others": [], "ports": [], "auto_pair": self.settings.get().usb_auto_pair}
        return dict(manager.status(), enabled=True)

    def lan_status(self) -> dict:
        server = self._server
        return {"enabled": self.lan_wanted, "running": server is not None, "forced": self._lan_override is True and not self.settings.get().lan_enabled,
                "port": server.server_address[1] if server is not None else self._http_port, "bind": self._bind,
                "discovery": server is not None and self._beacon is not None, "error": self._lan_error}

    def status(self) -> dict:
        s = self.settings.get()
        return {"version": __version__, "paired": self.pairing.paired, "pairing_window": self.pairing.window_open,
                "usb": self.usb_status(), "lan": self.lan_status(),
                "settings": {"usb_enabled": s.usb_enabled, "lan_enabled": s.lan_enabled, "usb_auto_pair": s.usb_auto_pair}}

    def describe(self) -> str:
        """One line for a tooltip: the USB state, plus a note when the LAN transport is on."""
        text = describe_usb(self.usb_status())
        lan = self.lan_status()
        if lan["running"]:
            text += f" - LAN on (port {lan['port']})"
        return text

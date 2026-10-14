"""The companion's notification-area (tray) icon: device status, idle-screen settings, pairing, the start-with-Windows switch, the log folder,
"About" and Exit - all the user interface the product has.

    app = TrayApp(runtime, settings_window, pairing, on_quit=runtime.request_quit)
    app.start()          # puts the icon in the notification area; returns False (and logs why) when pystray is not available
    app.stop()

The icon follows the USB link: its tooltip is the one-line status (`runtime.describe()`), a small coloured badge shows the state at a glance
(green = connected, amber = something to do: pair / waiting, red = needs attention: update / port busy, grey = looking for the display),
and the user is told with a notification when action is needed - an unpaired display was plugged in (the pairing dialog opens by itself),
or the app / firmware need an update. Everything that needs a decision runs in its own short-lived Tk thread, so the tray's menu thread is
never blocked.

The pure parts (`status_text`, `badge_colour`, `about_text`, the event handling) take the runtime as a plain object and do not need Windows
or a display; tests drive them with a fake.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Optional

from . import __version__
from . import autostart
from .framing import VER as LINK_VERSION
from .pairing import Pairing, default_data_dir
from .ui import _make_dpi_aware, make_icon_image

log = logging.getLogger("tray")

APP_TITLE = "DeskMedia Companion"
NOTIFY_MIN_GAP_S = 30.0            # the same kind of notification at most this often

# What the badge colour means (RGB), by USB state (`LinkManager.status()["state"]`).
_GREEN, _AMBER, _RED, _GREY = (46, 184, 92), (240, 168, 40), (222, 70, 60), (140, 146, 160)
_BADGE = {"connected": _GREEN, "unpaired": _AMBER, "pairing": _AMBER, "probing": _AMBER, "searching": _GREY, "disabled": _GREY,
          "incompatible": _RED, "busy": _RED}


def badge_colour(usb: dict) -> tuple[int, int, int]:
    """The colour of the state dot on the tray icon."""
    return _BADGE.get(usb.get("state", "searching") if usb.get("enabled", True) else "disabled", _GREY)


def status_text(runtime) -> str:
    """The one line the tooltip and the first menu row show (never longer than Windows' 127 characters for a tooltip)."""
    try:
        return runtime.describe()[:120]
    except Exception:
        return APP_TITLE


def about_text(runtime, data_dir: str) -> str:
    """The About box: what a person needs to tell a helper - versions on both ends, how the display is connected, where the files are."""
    try:
        st = runtime.status()
    except Exception:
        st = {"usb": {}, "lan": {}}
    usb, lan = st.get("usb", {}), st.get("lan", {})
    lines = [f"{APP_TITLE} {__version__}", f"USB link protocol: DMLINK v{LINK_VERSION}, media API {usb.get('api') or 2}", ""]
    if usb.get("enabled", True) and usb.get("dev"):
        lines.append(f"Display: {status_text(runtime)}")
        lines.append(f"Display firmware: {usb.get('fw') or 'unknown'}   (id {usb.get('dev_short', '')})")
        if usb.get("port"):
            lines.append(f"Connected on: {usb['port']}")
    else:
        lines.append(f"Display: {status_text(runtime)}")
    lines.append(f"Wi-Fi / LAN transport: {'on, port ' + str(lan.get('port')) if lan.get('running') else 'off'}")
    lines += ["", "Your settings and the pairing key are kept in:", data_dir]
    return "\n".join(lines)


def _tk_thread(name: str, fn: Callable[[], None]) -> None:
    """Runs a small Tk dialog in its own thread (Tk must not be used from the tray's menu thread)."""
    threading.Thread(target=fn, name=name, daemon=True).start()


def _ask_yes_no(title: str, message: str) -> bool:
    try:
        import tkinter as tk
        from tkinter import messagebox
    except ImportError as exc:
        log.error("a confirmation needs tkinter: %s", exc)
        return False
    _make_dpi_aware()
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        return bool(messagebox.askyesno(title, message, parent=root))
    finally:
        root.destroy()


def _show_info(title: str, message: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox
    except ImportError as exc:
        log.error("the About box needs tkinter: %s", exc)
        return
    _make_dpi_aware()
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        messagebox.showinfo(title, message, parent=root)
    finally:
        root.destroy()


LAN_QUESTION = ("Also let the display connect over Wi-Fi (the local network)?\n\n"
                "The USB cable is all the display needs. The network mode opens a port on this PC, so Windows may ask whether "
                "to allow it through the firewall. Only do this for a display that has no USB cable to this PC.\n\n"
                "Turn it on?")


class TrayApp:
    """Owns the tray icon. `runtime` is an `AppRuntime`; `window` the idle-screen settings window; `pairing` the `Pairing`."""

    def __init__(self, runtime, window, pairing: Pairing, on_quit: Callable[[], None], *, pair_dialog=None, data_dir: str = "",
                 clock: Callable[[], float] = time.monotonic, pystray_module=None, opener: Optional[Callable[[str], None]] = None,
                 confirm: Optional[Callable[[str, str], bool]] = None, inform: Optional[Callable[[str, str], None]] = None) -> None:
        self.runtime = runtime
        self.window = window
        self.pairing = pairing
        self._on_quit = on_quit
        self.data_dir = data_dir or default_data_dir()
        self._clock = clock
        self._pystray = pystray_module
        self._opener = opener or self._open_folder
        self._confirm = confirm or (lambda title, text: _ask_yes_no(title, text))
        self._inform = inform or (lambda title, text: _show_info(title, text))
        if pair_dialog is None:
            from .ui import PairDialog
            pair_dialog = PairDialog(pairing)
        self.dialog = pair_dialog
        self.icon = None
        self._last_key: tuple = ()
        self._notified: dict[str, float] = {}

    # -- the menu ---------------------------------------------------------------------------------------------------------------------------
    def menu_items(self) -> list:
        """The pystray menu. Text, enabled and checked are callables, re-evaluated whenever the menu is shown."""
        pystray = self._pystray
        item, sep = pystray.MenuItem, pystray.Menu.SEPARATOR
        rt = self.runtime
        return [
            item(lambda _i: status_text(rt), None, enabled=False),
            sep,
            item("Idle screen settings...", lambda _i, _j: self.window.open(), default=True),
            item("Pair a display...", lambda _i, _j: self.dialog.open()),
            item("Forget paired display", lambda _i, _j: self.forget(), enabled=lambda _i: self.pairing.paired),
            sep,
            item("Start with Windows", lambda _i, _j: self.toggle_autostart(), checked=lambda _i: self._autostart_on()),
            item("Advanced", pystray.Menu(
                item("Also connect over Wi-Fi (LAN)", lambda _i, _j: self.toggle_lan(), checked=lambda _i: rt.lan_wanted),
                item("Pair a new display automatically", lambda _i, _j: self.toggle_auto_pair(),
                     checked=lambda _i: bool(rt.settings.get().usb_auto_pair)))),
            item("Open log folder", lambda _i, _j: self.open_logs()),
            item(f"About {APP_TITLE} {__version__}", lambda _i, _j: self.about()),
            sep,
            item("Exit", lambda _i, _j: self.quit()),
        ]

    # -- the actions ----------------------------------------------------------------------------------------------------------------------
    def _autostart_on(self) -> bool:
        try:
            return autostart.is_installed()
        except OSError:
            return False

    def toggle_autostart(self) -> None:
        try:
            autostart.remove() if autostart.is_installed() else autostart.install()
        except OSError as exc:
            log.warning("the start-with-Windows switch failed (%s)", type(exc).__name__)

    def toggle_lan(self) -> None:
        def run() -> None:
            if self.runtime.lan_wanted:
                self.runtime.set_lan_enabled(False)
            elif self._confirm(APP_TITLE, LAN_QUESTION):
                running = self.runtime.set_lan_enabled(True)
                if not running:
                    self._inform(APP_TITLE, "The network mode could not start (the port is probably in use by another program). "
                                            "The USB connection is not affected.")
            self.refresh()
        _tk_thread("lan-ui", run)

    def toggle_auto_pair(self) -> None:
        self.runtime.set_auto_pair(not self.runtime.settings.get().usb_auto_pair)
        self.refresh()

    def forget(self) -> None:
        from .ui import forget_with_confirmation
        forget_with_confirmation(self.pairing)

    def open_logs(self) -> None:
        try:
            self._opener(self.data_dir)
        except OSError as exc:
            log.warning("cannot open the log folder (%s)", type(exc).__name__)

    @staticmethod
    def _open_folder(path: str) -> None:
        os.makedirs(path, exist_ok=True)
        os.startfile(path)                            # type: ignore[attr-defined]  # Windows only, as is the whole product

    def about(self) -> None:
        text = about_text(self.runtime, self.data_dir)
        _tk_thread("about-ui", lambda: self._inform(f"About {APP_TITLE}", text))

    def quit(self) -> None:
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass
        self._on_quit()

    # -- events from the USB scanner ---------------------------------------------------------------------------------------------------
    def on_event(self, event: dict) -> None:
        """Called from the scanner thread (`runtime.add_listener`)."""
        kind = event.get("type")
        if kind == "pair-request":
            self.notify("pair", "Display found", "Check that the code on the display and on this PC match, then click Pair.")
            self.dialog.open()
        elif kind in ("status", "settings"):
            if kind == "status":
                self._notify_state(event.get("status") or {})
            self.refresh()

    def _notify_state(self, usb: dict) -> None:
        state = usb.get("state")
        if state == "incompatible":
            if usb.get("update_needed") == "display":
                self.notify("update-display", "The display needs a firmware update", "See the installation guide for how to update it.")
            else:
                self.notify("update-app", f"{APP_TITLE} needs an update", "Install the newest version to use this display.")
        elif state == "busy":
            self.notify("busy", "The display's USB port is in use", "Another program is using it: close the other program or unplug and re-plug the display.")

    def notify(self, key: str, title: str, message: str) -> None:
        now = self._clock()
        if now - self._notified.get(key, -1e9) < NOTIFY_MIN_GAP_S:
            return
        self._notified[key] = now
        if self.icon is not None:
            try:
                self.icon.notify(message, title)
            except Exception:
                log.debug("the notification could not be shown")

    def refresh(self) -> None:
        """Brings the badge, the tooltip and the menu in line with the status (any thread)."""
        icon = self.icon
        if icon is None:
            return
        try:
            usb = self.runtime.usb_status()
            key = (usb.get("state"), usb.get("detail"), usb.get("enabled", True), status_text(self.runtime))
            if key != self._last_key:
                self._last_key = key
                icon.icon = make_icon_image(badge_colour(usb))
                icon.title = f"{APP_TITLE}: {status_text(self.runtime)}"[:127]
            icon.update_menu()
        except Exception:
            log.debug("the tray icon could not be updated")

    # -- lifecycle ---------------------------------------------------------------------------------------------------------------------------
    def start(self) -> bool:
        if self._pystray is None:
            try:
                import pystray
            except ImportError:
                log.warning("pystray is not installed: no tray icon")
                return False
            self._pystray = pystray
        pystray = self._pystray
        usb = self.runtime.usb_status()
        self.icon = pystray.Icon("DeskMediaCompanion", make_icon_image(badge_colour(usb)), f"{APP_TITLE}: {status_text(self.runtime)}",
                                 pystray.Menu(*self.menu_items()))
        self.runtime.add_listener(self.on_event)
        self.icon.run_detached()
        return True

    def stop(self) -> None:
        self.runtime.remove_listener(self.on_event)
        icon, self.icon = self.icon, None
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass

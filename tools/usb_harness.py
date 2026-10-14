"""USB version of the board-in-the-loop harness: the same `Board` / `Companion` interface as tools/idle_test.py, but the companion talks to the
display over the USB cable (the product's own AppRuntime + LinkManager) and the display's log comes back as LOG frames.

    import usb_harness as uh
    board = uh.UsbBoard()
    comp = uh.UsbCompanion(data_dir, board)         # data_dir: settings, pairing key and the idle folder (data_dir/idle); NEVER the real one
    uh.ensure_paired(board, comp)

The tests written against `Board` (mark / since / find / count / wait / screenshot / reset) and `Companion` (monitor, store, counts, act, stop_http /
start_http) run unchanged: `reset()` is the development firmware's `reboot` action (a software reset, the native port stays enumerated) and
stop_http / start_http stop and start the whole runtime (what a companion restart does). The development firmware is needed (-DAPP_DEV_TOOLS=1).
"""
import os
import sys
import threading
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import idle_test as it  # noqa: E402

from desk_media_companion.config import SettingsStore  # noqa: E402
from desk_media_companion.pairing import Pairing, PairingStore  # noqa: E402
from desk_media_companion.runtime import AppRuntime  # noqa: E402


class UsbBoard(it.Board):
    """The display's log lines as the companion's USB link delivers them (kind "log": the firmware's own log lines; "raw": ROM / bootloader
    text); the interface of `idle_test.Board`, without a serial port of its own (the companion owns the cable)."""

    def __init__(self) -> None:                                  # deliberately no super().__init__: no serial port here
        self.lines: list[tuple[float, str]] = []
        self.lock = threading.Lock()
        self.stop = False
        self.comp = None

    def feed(self, kind: str, line: str) -> None:
        with self.lock:
            self.lines.append((time.time(), line.strip()))

    def reset(self) -> None:
        """Reboots the display (software reset) and waits until it is gone and back: the next line after this call is the boot."""
        if self.comp is None:
            raise RuntimeError("UsbBoard.reset needs the companion (board.comp)")
        self.comp.monitor.request_debug("reboot")
        end = time.time() + 40
        mark = self.mark()
        while time.time() < end:
            if self.find(r"ESP-ROM|rst:0x|debug reboot", mark):
                return
            time.sleep(0.2)


class UsbCompanion(it.Companion):
    """`idle_test.Companion` with the USB transport (no HTTP server, no beacon) and its own data folder."""

    def __init__(self, data_dir: str, board: UsbBoard, usb_port: str = "") -> None:
        self._board = board
        self._usb_port = usb_port
        self.runtime = None
        self.base = os.path.dirname(data_dir.rstrip("\\/"))
        board.comp = self
        super().__init__(data_dir, pairing_dir=self.base)

    def start_http(self) -> None:
        self.counts = {"idle": 0, "image": 0}
        self.store = it.idle_mod.IdleStore(self.data_dir)
        real_config, real_image = self.store.config, self.store.image

        def config():
            self.counts["idle"] += 1
            return real_config()

        def image():
            self.counts["image"] += 1
            return real_image()

        self.store.config, self.store.image = config, image
        self.pairing = Pairing(PairingStore(self.base))
        self.runtime = AppRuntime(self.monitor, self.store, self.pairing, SettingsStore(self.base), dev_tools=True,
                                  disable_usb=False, usb_port=self._usb_port, log_sink=self._board.feed)
        self.runtime.start()

    def stop_http(self) -> None:
        if self.runtime is not None:
            self.runtime.stop()
            self.runtime = None

    def usb_state(self) -> str:
        return self.runtime.usb_status().get("state", "") if self.runtime else ""


def ensure_paired(board: UsbBoard, comp: UsbCompanion, timeout: float = 90.0) -> bool:
    """Pairs the display with this companion's (scratch) pairing store if it is not paired yet: waits for the automatic pairing window, confirms on the
    PC, taps Pair on the display through the touch path, and waits for the USB session. Returns True when the link is up and paired."""
    pairing = comp.pairing
    if not it.wait_for(lambda: comp.usb_state() in ("connected", "unpaired", "pairing"), 40.0, 0.3):
        return False
    if comp.usb_state() == "connected" and pairing.paired:
        return True
    if not pairing.window_open:
        pairing.open_window()
    mark = board.mark()
    if not board.wait(r"pairing code shown", mark, 30.0):
        return False
    it.wait_for(lambda: pairing.snapshot()["code"], 5.0)
    pairing.pc_confirm()
    time.sleep(0.6)
    comp.act("touch_pair")
    return it.wait_for(lambda: pairing.paired and comp.usb_state() == "connected", timeout, 0.3)

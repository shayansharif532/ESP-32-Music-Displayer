"""AppRuntime (the transports as one object, the status the tray reads), the command line and the single-instance rules.
No real serial port is listed or opened (the lister / opener are injected); the LAN part uses 127.0.0.1 and an ephemeral port, no beacon.

    companion\\.venv\\Scripts\\python.exe -m unittest discover -s companion/tests
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from desk_media_companion import __main__ as main_mod  # noqa: E402
from desk_media_companion.config import SettingsStore  # noqa: E402
from desk_media_companion.pairing import Pairing, PairingStore  # noqa: E402
from desk_media_companion.runtime import AppRuntime, describe_usb  # noqa: E402
from display_sim import DuplexPipe, DisplaySim, FakeMonitor, RealTimeDriver  # noqa: E402
from desk_media_companion.usblink import PortInfo, USB_PID, USB_VID  # noqa: E402


def wait_for(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > end:
            return False
        time.sleep(0.01)
    return True


class RuntimeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = SettingsStore(self.tmp.name)
        self.pairing = Pairing(PairingStore(self.tmp.name), name="TEST-PC")
        self.listed: list = []
        self.opened: list = []

    def runtime(self, **kw) -> AppRuntime:
        kw.setdefault("lister", lambda: list(self.listed))
        kw.setdefault("opener", lambda name: self.opened.append(name))
        kw.setdefault("http_port", 0)
        kw.setdefault("bind", "127.0.0.1")
        kw.setdefault("discovery", False)
        rt = AppRuntime(FakeMonitor(cover_bytes=100), None, self.pairing, self.settings, **kw)
        self.addCleanup(rt.stop)
        return rt

    @staticmethod
    def health(port: int) -> int:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
            return r.status

    @staticmethod
    def refused(port: int) -> bool:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
        except OSError:
            return True
        return False


class Transports(RuntimeCase):
    def test_by_default_usb_runs_and_no_socket_is_opened(self):
        rt = self.runtime()
        with mock.patch("socket.socket.bind", side_effect=AssertionError("the runtime must not bind a socket")), \
                mock.patch("socket.socket.listen", side_effect=AssertionError("or listen")):
            rt.start()
            self.assertTrue(wait_for(lambda: rt.usb_status()["state"] == "searching"))
        st = rt.status()
        self.assertTrue(st["usb"]["enabled"])
        self.assertEqual((st["lan"]["enabled"], st["lan"]["running"], st["lan"]["forced"], st["lan"]["error"]), (False, False, False, ""))
        self.assertFalse(rt.lan_running)

    def test_lan_flag_starts_the_http_server_for_this_run_only(self):
        rt = self.runtime(force_lan=True)
        rt.start()
        self.assertTrue(rt.lan_running)
        lan = rt.status()["lan"]
        self.assertEqual((lan["enabled"], lan["running"], lan["forced"], lan["bind"]), (True, True, True, "127.0.0.1"))
        self.assertEqual(self.health(lan["port"]), 200)
        self.assertFalse(self.settings.get().lan_enabled)                                    # --lan is not remembered
        with urllib.request.urlopen(f"http://127.0.0.1:{lan['port']}/hello", timeout=3) as r:
            self.assertEqual(json.loads(r.read())["id"], self.pairing.cid_hex)               # the shared Api answers over HTTP too
        rt.stop()
        self.assertTrue(wait_for(lambda: self.refused(lan["port"])))

    def test_the_lan_setting_starts_it_and_the_switch_stops_and_remembers_it(self):
        self.settings.update(lan_enabled=True)
        rt = self.runtime()
        rt.start()
        self.assertTrue(rt.lan_running)
        port = rt.lan_status()["port"]
        self.assertEqual(self.health(port), 200)
        events = []
        rt.add_listener(events.append)
        self.assertFalse(rt.set_lan_enabled(False))
        self.assertTrue(self.refused(port))
        self.assertFalse(SettingsStore(self.tmp.name).get().lan_enabled)
        self.assertIn({"type": "settings"}, events)
        self.assertTrue(rt.set_lan_enabled(True))
        self.assertEqual(self.health(rt.lan_status()["port"]), 200)
        self.assertTrue(SettingsStore(self.tmp.name).get().lan_enabled)

    def test_switching_off_ends_a_lan_override_and_a_non_persistent_switch_is_for_this_run(self):
        rt = self.runtime(force_lan=True)
        rt.start()
        self.assertFalse(rt.set_lan_enabled(False))
        self.assertFalse(rt.lan_wanted)
        self.assertFalse(rt.lan_status()["forced"])
        self.assertTrue(rt.set_lan_enabled(True, persist=False))
        self.assertFalse(self.settings.get().lan_enabled)
        self.assertTrue(rt.lan_status()["forced"])

    def test_a_lan_server_that_cannot_start_is_reported_not_fatal(self):
        rt = self.runtime(force_lan=True, bind="203.0.113.1")                                 # not an address of this PC: bind fails
        rt.start()
        self.assertFalse(rt.lan_running)
        self.assertNotEqual(rt.lan_error, "")
        self.assertEqual(rt.status()["lan"]["error"], rt.lan_error)
        self.assertTrue(rt.usb_wanted)                                                        # USB carries on
        self.assertIn(rt.usb_status()["state"], ("searching", "probing"))

    def test_usb_can_be_disabled_by_flag_by_setting_and_by_the_switch(self):
        rt = self.runtime(disable_usb=True)                                                    # --no-usb
        rt.start()
        self.assertEqual(rt.usb_status()["state"], "disabled")
        self.assertFalse(rt.usb_status()["enabled"])
        self.assertEqual(describe_usb(rt.usb_status()), "USB is switched off")
        rt.set_usb_enabled(True)                                                               # the user's switch beats the flag
        self.assertTrue(wait_for(lambda: rt.usb_status()["state"] == "searching"))
        rt.set_usb_enabled(False)
        self.assertEqual(rt.usb_status()["state"], "disabled")
        self.assertFalse(SettingsStore(self.tmp.name).get().usb_enabled)
        rt2 = self.runtime()                                                                    # a new run honours the saved setting
        rt2.start()
        self.assertEqual(rt2.usb_status()["state"], "disabled")

    def test_the_usb_port_option_reaches_the_scanner(self):
        self.listed = [PortInfo("COM3", 0x1234, 0x5678), PortInfo("COM5", USB_VID, USB_PID)]
        rt = self.runtime(usb_port="com3")
        rt.start()
        self.assertTrue(wait_for(lambda: self.opened == ["COM3"] or "COM3" in self.opened))
        self.assertEqual(set(self.opened), {"COM3"})
        self.assertEqual(rt.usb_status()["ports"], ["COM3"])

    def test_auto_pair_setting_reaches_the_manager_and_is_remembered(self):
        rt = self.runtime()
        rt.start()
        self.assertTrue(rt.usb_status()["auto_pair"])
        rt.set_auto_pair(False)
        self.assertFalse(rt.usb_status()["auto_pair"])
        self.assertFalse(SettingsStore(self.tmp.name).get().usb_auto_pair)
        rt.stop()
        self.settings.update(usb_auto_pair=False)
        rt3 = self.runtime()
        rt3.start()
        self.assertFalse(rt3.usb_status()["auto_pair"])

    def test_stop_is_idempotent_and_closes_the_transports(self):
        rt = self.runtime(force_lan=True)
        rt.start()
        port = rt.lan_status()["port"]
        rt.stop()
        rt.stop()
        self.assertTrue(wait_for(lambda: self.refused(port)))
        self.assertEqual(rt.usb_status()["state"], "disabled")

    def test_quit_wakes_the_main_thread(self):
        rt = self.runtime()
        done = threading.Event()
        threading.Thread(target=lambda: (rt.wait(), done.set()), daemon=True).start()
        time.sleep(0.2)
        self.assertFalse(done.is_set())
        rt.request_quit()
        self.assertTrue(done.wait(2))
        self.assertTrue(rt.quit_requested)


class StatusAndEvents(RuntimeCase):
    def test_listeners_get_the_scanners_events_and_a_broken_one_is_survived(self):
        rt = self.runtime()
        events = []
        rt.add_listener(lambda e: (_ for _ in ()).throw(RuntimeError("broken")))
        rt.add_listener(events.append)
        with self.assertLogs("runtime", "WARNING"):
            rt.start()
            self.assertTrue(wait_for(lambda: any(e["type"] == "status" for e in events)))
        self.assertEqual(events[0]["status"]["state"], "searching")
        rt.remove_listener(events.append)

    def test_the_status_carries_what_the_tray_needs(self):
        rt = self.runtime()
        rt.start()
        st = rt.status()
        self.assertEqual(set(st), {"version", "paired", "pairing_window", "usb", "lan", "settings"})
        self.assertEqual(st["settings"], {"usb_enabled": True, "lan_enabled": False, "usb_auto_pair": True})
        self.assertFalse(st["paired"])
        self.pairing.open_window()
        self.assertTrue(rt.status()["pairing_window"])
        self.assertTrue({"state", "detail", "port", "dev", "fw", "api", "paired_with_you", "compatible", "authenticated", "link",
                         "others", "enabled"} <= set(st["usb"]))
        self.assertEqual(rt.describe(), "Looking for the display (USB)...")

    def test_describe_usb_covers_every_state(self):
        base = {"enabled": True}
        cases = [({"state": "searching"}, "Looking for the display (USB)..."), ({"state": "probing"}, "Connecting to the display..."),
                 ({"state": "connected"}, "Display connected (USB)"), ({"state": "unpaired", "detail": "not_paired"}, "Display found, but not paired with this PC"),
                 ({"state": "unpaired", "detail": "key_mismatch"}, "The display has a different pairing key: pair it again"),
                 ({"state": "unpaired", "detail": "display_forgot"}, "The display lost its pairing: pair it again"),
                 ({"state": "pairing"}, "Pairing a display..."), ({"state": "busy"}, "The display's USB port is in use by another program"),
                 ({"state": "incompatible", "update_needed": "display"}, "The display's firmware needs an update"),
                 ({"state": "incompatible", "update_needed": "app"}, "This app needs an update for the display"),
                 ({"state": "weird"}, "Looking for the display (USB)..."), ({"state": "connected", "enabled": False}, "USB is switched off")]
        for extra, text in cases:
            self.assertEqual(describe_usb(dict(base, **extra)), text, extra)

    def test_the_whole_stack_connects_a_simulated_display_through_the_runtime(self):
        pipe = DuplexPipe(11)
        sim = DisplaySim(pipe.display, driver=RealTimeDriver(5.0))
        key = bytes(range(32))
        self.pairing._store.set_device(sim.dev, key)
        sim.key, sim.cid = key, self.pairing.cid
        self.listed = [PortInfo("COM9", USB_VID, USB_PID, "synthetic")]
        rt = self.runtime(opener=lambda name: pipe.attach())
        rt.start()
        end = time.monotonic() + 8
        while not sim.companion_present:
            sim.pump()
            time.sleep(0.002)
            self.assertLess(time.monotonic(), end)
        self.assertEqual(sim.open_session().status, 200)
        self.assertTrue(wait_for(lambda: rt.usb_status()["state"] == "connected"))
        self.assertEqual(rt.describe(), "Display connected (USB)")
        self.assertEqual(sim.get("/now-playing").json()["title"], "Synthetic Song")


class CommandLine(unittest.TestCase):
    def test_the_new_flags_and_their_defaults(self):
        ap = main_mod.build_parser()
        a = ap.parse_args([])
        self.assertEqual((a.lan, a.no_usb, a.usb_port, a.port, a.discovery_port, a.bind), (False, False, "", 8765, 8766, "0.0.0.0"))
        a = ap.parse_args(["--lan", "--no-usb", "--usb-port", "COM7", "--port", "8800", "--no-discovery", "--quiet", "--no-tray"])
        self.assertEqual((a.lan, a.no_usb, a.usb_port, a.port, a.no_discovery, a.quiet, a.no_tray), (True, True, "COM7", 8800, True, True, True))

    def test_every_existing_flag_is_still_there(self):
        ap = main_mod.build_parser()
        flags = {s for action in ap._actions for s in action.option_strings}
        for flag in ("--port", "--discovery-port", "--no-discovery", "--console", "--quiet", "--debug", "--dev-tools", "--prefer-app",
                     "--treat-as-spotify", "--only-apps", "--ignore-apps", "--no-tray", "--open-settings", "--data-dir", "--bind",
                     "--pair-console", "--probe", "--install-startup", "--remove-startup", "--version"):
            self.assertIn(flag, flags)


@unittest.skipUnless(os.name == "nt", "named mutexes are a Windows thing")
class SingleInstance(unittest.TestCase):
    def setUp(self):
        # unique names: the tests must never touch the mutex of a companion that is really running on this PC
        patcher = mock.patch.object(main_mod, "INSTANCE_MUTEX", "Local\\DeskMediaCompanionTest-" + uuid.uuid4().hex)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_usb_lock_does_not_depend_on_the_http_port(self):
        self.assertEqual(main_mod._acquire_instance_locks(True, None), "")
        self.assertEqual(main_mod._acquire_instance_locks(True, None), main_mod.INSTANCE_MUTEX)       # a second copy is refused
        self.assertEqual(main_mod._acquire_instance_locks(True, 18765), main_mod.INSTANCE_MUTEX)       # whatever its port

    def test_lan_instances_are_unique_per_port_and_a_lan_only_one_runs_next_to_the_product(self):
        self.assertEqual(main_mod._acquire_instance_locks(True, None), "")                           # the product: USB only
        self.assertEqual(main_mod._acquire_instance_locks(False, 18801), "")                          # a development companion: --no-usb --lan
        self.assertEqual(main_mod._acquire_instance_locks(False, 18802), "")
        self.assertEqual(main_mod._acquire_instance_locks(False, 18801), main_mod.INSTANCE_MUTEX + "-lan-18801")
        self.assertEqual(main_mod._acquire_instance_locks(False, None), "")                          # nothing to serve, nothing to lock


if __name__ == "__main__":
    unittest.main()

"""The tray icon's logic (tray.py): status text, badge colours, the About text, the menu, event handling, the Wi-Fi switch.
pystray, Tk and the registry are replaced by fakes: nothing here needs Windows, a display or a tray.

    companion\\.venv\\Scripts\\python.exe -m unittest discover -s companion/tests
"""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from desk_media_companion import tray as tray_mod  # noqa: E402
from desk_media_companion.tray import TrayApp, about_text, badge_colour, make_icon_image, status_text  # noqa: E402


class FakeMenuItem:
    def __init__(self, text, action=None, checked=None, default=False, enabled=True, **kw):
        self.text, self.action, self.checked, self.default, self.enabled = text, action, checked, default, enabled


class FakeMenu:
    SEPARATOR = object()

    def __init__(self, *items):
        self.items = list(items)


class FakeIconObj:
    instances = []

    def __init__(self, name, image, title, menu):
        self.name, self.icon, self.title, self.menu = name, image, title, menu
        self.detached, self.stopped, self.menu_updates, self.notes = False, False, 0, []
        FakeIconObj.instances.append(self)

    def run_detached(self):
        self.detached = True

    def stop(self):
        self.stopped = True

    def update_menu(self):
        self.menu_updates += 1

    def notify(self, message, title):
        self.notes.append((title, message))


FAKE_PYSTRAY = SimpleNamespace(MenuItem=FakeMenuItem, Menu=FakeMenu, Icon=FakeIconObj)


class FakeSettings:
    def __init__(self):
        self.usb_auto_pair, self.lan_enabled = True, False

    def get(self):
        return self


class FakeRuntime:
    def __init__(self, usb=None):
        self.usb = usb or {"enabled": True, "state": "connected", "dev": "b60c0a1dc6dc4c31", "dev_short": "b60c0a1d", "fw": "1.0.0", "api": 2,
                           "port": "COM5", "detail": ""}
        self.lan = {"running": False, "port": 8765}
        self.listeners, self.lan_wanted, self.settings = [], False, FakeSettings()
        self.lan_calls, self.autopair_calls, self.lan_start_ok = [], [], True

    def describe(self):
        return "Display connected (USB)"

    def usb_status(self):
        return self.usb

    def status(self):
        return {"usb": self.usb, "lan": self.lan}

    def add_listener(self, cb):
        self.listeners.append(cb)

    def remove_listener(self, cb):
        self.listeners.remove(cb)

    def set_lan_enabled(self, enabled):
        self.lan_calls.append(enabled)
        self.lan_wanted = enabled
        return enabled and self.lan_start_ok

    def set_auto_pair(self, enabled):
        self.autopair_calls.append(enabled)
        self.settings.usb_auto_pair = enabled


class Case(unittest.TestCase):
    def setUp(self):
        FakeIconObj.instances.clear()
        self.rt = FakeRuntime()
        self.pairing = SimpleNamespace(paired=True)
        self.dialog = mock.Mock()
        self.window = mock.Mock()
        self.quit_calls = []
        self.now = [1000.0]
        self.opened, self.asked, self.told = [], [], []
        self.answer = [True]
        self.app = TrayApp(self.rt, self.window, self.pairing, on_quit=lambda: self.quit_calls.append(1), pair_dialog=self.dialog,
                           data_dir="C:\\data", clock=lambda: self.now[0], pystray_module=FAKE_PYSTRAY, opener=self.opened.append,
                           confirm=lambda t, m: (self.asked.append((t, m)), self.answer[0])[1], inform=lambda t, m: self.told.append((t, m)))
        patcher = mock.patch.object(tray_mod, "_tk_thread", lambda name, fn: fn())          # the dialogs run inline in the tests
        patcher.start()
        self.addCleanup(patcher.stop)

    def items(self):
        return [i for i in self.app.menu_items() if i is not FakeMenu.SEPARATOR]


class Pure(unittest.TestCase):
    def test_badge_colours_follow_the_state(self):
        self.assertEqual(badge_colour({"state": "connected"}), (46, 184, 92))
        self.assertEqual(badge_colour({"state": "unpaired"}), (240, 168, 40))
        self.assertEqual(badge_colour({"state": "incompatible"}), (222, 70, 60))
        self.assertEqual(badge_colour({"state": "busy"}), (222, 70, 60))
        self.assertEqual(badge_colour({"state": "searching"}), (140, 146, 160))
        self.assertEqual(badge_colour({"state": "connected", "enabled": False}), (140, 146, 160))      # USB switched off: grey whatever the state was
        self.assertEqual(badge_colour({"state": "something new"}), (140, 146, 160))

    def test_status_text_is_short_and_never_raises(self):
        long = SimpleNamespace(describe=lambda: "x" * 500)
        self.assertEqual(len(status_text(long)), 120)
        bad = SimpleNamespace(describe=lambda: 1 / 0)
        self.assertEqual(status_text(bad), tray_mod.APP_TITLE)

    def test_the_icon_image_has_the_size_and_a_badge(self):
        plain, badged = make_icon_image(None, 64), make_icon_image((46, 184, 92), 64)
        self.assertEqual(plain.size, (64, 64))
        self.assertNotEqual(plain.tobytes(), badged.tobytes())
        r, g, b, _a = badged.getpixel((54, 54))
        self.assertGreater(g, r)                                                                  # the green dot sits in the lower right corner

    def test_about_text_names_the_versions_the_port_and_the_folder(self):
        text = about_text(FakeRuntime(), "C:\\data")
        for part in (tray_mod.APP_TITLE, "DMLINK v1", "1.0.0", "b60c0a1d", "COM5", "C:\\data", "Wi-Fi / LAN transport: off"):
            self.assertIn(part, text)
        rt = FakeRuntime()
        rt.lan = {"running": True, "port": 8765}
        self.assertIn("on, port 8765", about_text(rt, "C:\\data"))

    def test_about_text_survives_a_broken_runtime(self):
        broken = SimpleNamespace(status=lambda: 1 / 0, describe=lambda: "x")
        self.assertIn(tray_mod.APP_TITLE, about_text(broken, "C:\\data"))

    def test_about_text_for_no_display(self):
        rt = FakeRuntime({"enabled": True, "state": "searching", "dev": "", "dev_short": "", "fw": "", "api": 0, "port": ""})
        text = about_text(rt, "C:\\data")
        self.assertNotIn("Display firmware", text)


class Menu(Case):
    def test_menu_has_everything_the_product_promises(self):
        texts = [i.text if isinstance(i.text, str) else i.text(None) for i in self.items()]
        self.assertEqual(texts[0], "Display connected (USB)")
        for wanted in ("Idle screen settings...", "Pair a display...", "Forget paired display", "Start with Windows", "Advanced", "Open log folder",
                       "Exit"):
            self.assertIn(wanted, texts)
        self.assertTrue(any(t.startswith("About DeskMedia Companion") for t in texts))

    def test_the_status_row_is_a_label_and_the_settings_row_is_the_default(self):
        items = self.items()
        self.assertFalse(items[0].enabled)
        self.assertTrue([i for i in items if i.default][0].text == "Idle screen settings...")

    def test_forget_is_only_enabled_when_paired(self):
        forget = [i for i in self.items() if i.text == "Forget paired display"][0]
        self.assertTrue(forget.enabled(None))
        self.pairing.paired = False
        self.assertFalse(forget.enabled(None))

    def test_start_with_windows_reflects_and_flips_the_registry_value(self):
        item = [i for i in self.items() if i.text == "Start with Windows"][0]
        with mock.patch.object(tray_mod.autostart, "is_installed", return_value=True), \
                mock.patch.object(tray_mod.autostart, "remove") as remove, mock.patch.object(tray_mod.autostart, "install") as install:
            self.assertTrue(item.checked(None))
            item.action(None, None)
            remove.assert_called_once()
            install.assert_not_called()
        with mock.patch.object(tray_mod.autostart, "is_installed", return_value=False), \
                mock.patch.object(tray_mod.autostart, "remove") as remove, mock.patch.object(tray_mod.autostart, "install") as install:
            self.assertFalse(item.checked(None))
            item.action(None, None)
            install.assert_called_once()
            remove.assert_not_called()

    def test_a_registry_failure_is_not_fatal(self):
        item = [i for i in self.items() if i.text == "Start with Windows"][0]
        with mock.patch.object(tray_mod.autostart, "is_installed", side_effect=OSError("denied")):
            self.assertFalse(item.checked(None))
            item.action(None, None)                      # no exception

    def test_pair_settings_exit_and_logs_do_their_jobs(self):
        by = {i.text: i for i in self.items() if isinstance(i.text, str)}
        by["Pair a display..."].action(None, None)
        self.dialog.open.assert_called_once()
        by["Idle screen settings..."].action(None, None)
        self.window.open.assert_called_once()
        by["Open log folder"].action(None, None)
        self.assertEqual(self.opened, ["C:\\data"])
        by["Exit"].action(None, None)
        self.assertEqual(self.quit_calls, [1])

    def test_an_unopenable_log_folder_is_not_fatal(self):
        def boom(path):
            raise OSError("no explorer")
        self.app._opener = boom
        self.app.open_logs()

    def test_about_shows_the_text_in_a_box(self):
        self.app.about()
        self.assertEqual(len(self.told), 1)
        self.assertIn("DMLINK v1", self.told[0][1])

    def test_advanced_submenu_holds_the_lan_and_auto_pair_switches(self):
        advanced = [i for i in self.items() if i.text == "Advanced"][0]
        sub = [i for i in advanced.action.items if i is not FakeMenu.SEPARATOR]
        lan, auto = sub
        self.assertFalse(lan.checked(None))
        self.assertTrue(auto.checked(None))
        auto.action(None, None)
        self.assertEqual(self.rt.autopair_calls, [False])


class Lan(Case):
    def test_turning_lan_on_asks_first_and_explains_the_firewall(self):
        self.app.toggle_lan()
        self.assertEqual(len(self.asked), 1)
        self.assertIn("firewall", self.asked[0][1])
        self.assertEqual(self.rt.lan_calls, [True])

    def test_declining_leaves_everything_as_it_was(self):
        self.answer[0] = False
        self.app.toggle_lan()
        self.assertEqual(self.rt.lan_calls, [])

    def test_turning_lan_off_needs_no_question(self):
        self.rt.lan_wanted = True
        self.app.toggle_lan()
        self.assertEqual(self.asked, [])
        self.assertEqual(self.rt.lan_calls, [False])

    def test_a_port_that_cannot_be_taken_is_reported_and_usb_is_untouched(self):
        self.rt.lan_start_ok = False
        self.app.toggle_lan()
        self.assertEqual(len(self.told), 1)
        self.assertIn("USB connection is not affected", self.told[0][1])


class Events(Case):
    def start(self):
        self.assertTrue(self.app.start())
        return FakeIconObj.instances[-1]

    def test_start_registers_the_listener_and_runs_detached(self):
        icon = self.start()
        self.assertTrue(icon.detached)
        self.assertEqual(self.rt.listeners, [self.app.on_event])
        self.assertIn("Display connected (USB)", icon.title)
        self.app.stop()
        self.assertEqual(self.rt.listeners, [])
        self.assertTrue(icon.stopped)

    def test_a_pair_request_opens_the_dialog_and_tells_the_user_once(self):
        icon = self.start()
        self.app.on_event({"type": "pair-request", "dev": "b60c0a1d", "port": "COM5", "seconds": 120})
        self.app.on_event({"type": "pair-request", "dev": "b60c0a1d", "port": "COM5", "seconds": 120})
        self.assertEqual(self.dialog.open.call_count, 2)                 # the dialog itself de-duplicates (it raises the open one)
        self.assertEqual(len(icon.notes), 1)                             # but the balloon is shown once per half minute
        self.now[0] += 31
        self.app.on_event({"type": "pair-request", "dev": "b60c0a1d", "port": "COM5", "seconds": 120})
        self.assertEqual(len(icon.notes), 2)

    def test_status_changes_update_badge_title_and_menu(self):
        icon = self.start()
        before = icon.icon.tobytes()
        self.rt.usb = dict(self.rt.usb, state="unpaired", detail="not_paired")
        self.app.on_event({"type": "status", "status": self.rt.usb})
        self.assertNotEqual(icon.icon.tobytes(), before)
        self.assertGreaterEqual(icon.menu_updates, 1)

    def test_an_unchanged_status_keeps_the_image(self):
        icon = self.start()
        self.app.on_event({"type": "status", "status": self.rt.usb})
        image = icon.icon
        self.app.on_event({"type": "status", "status": self.rt.usb})
        self.assertIs(icon.icon, image)

    def test_incompatible_versions_are_announced_with_the_right_advice(self):
        icon = self.start()
        self.rt.usb = dict(self.rt.usb, state="incompatible", update_needed="display")
        self.app.on_event({"type": "status", "status": self.rt.usb})
        self.assertIn("firmware", icon.notes[-1][0])
        self.now[0] += 31
        self.rt.usb = dict(self.rt.usb, state="incompatible", update_needed="app")
        self.app.on_event({"type": "status", "status": self.rt.usb})
        self.assertIn("needs an update", icon.notes[-1][0])

    def test_a_busy_port_is_announced(self):
        icon = self.start()
        self.rt.usb = dict(self.rt.usb, state="busy")
        self.app.on_event({"type": "status", "status": self.rt.usb})
        self.assertIn("in use", icon.notes[-1][0])

    def test_unknown_events_are_ignored(self):
        self.start()
        self.app.on_event({"type": "mystery"})
        self.app.on_event({})

    def test_without_pystray_the_app_still_runs(self):
        app = TrayApp(self.rt, self.window, self.pairing, on_quit=lambda: None, pair_dialog=self.dialog)
        with mock.patch.dict(sys.modules, {"pystray": None}):             # "import pystray" raises ImportError
            self.assertFalse(app.start())
        app.on_event({"type": "status", "status": {}})                    # and the handlers are harmless without an icon


if __name__ == "__main__":
    unittest.main()

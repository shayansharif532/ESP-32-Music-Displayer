"""The persistent application settings: defaults, atomic writes, tolerance of damaged files.

    companion\\.venv\\Scripts\\python.exe -m unittest discover -s companion/tests
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from desk_media_companion import config  # noqa: E402
from desk_media_companion.config import Settings, SettingsStore  # noqa: E402


class Defaults(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_usb_on_lan_off_by_default_and_nothing_is_written_until_a_change(self):
        store = SettingsStore(self.tmp.name)
        self.assertEqual(store.get(), Settings(usb_enabled=True, lan_enabled=False, usb_auto_pair=True))
        self.assertFalse(os.path.exists(store.path))

    def test_the_default_directory_follows_the_data_dir_variable(self):
        with mock.patch.dict(os.environ, {"DESK_MEDIA_DATA_DIR": self.tmp.name}):
            store = SettingsStore()
        self.assertEqual(os.path.dirname(store.path), self.tmp.name)

    def test_changes_persist_across_instances(self):
        store = SettingsStore(self.tmp.name)
        new = store.update(lan_enabled=True, usb_enabled=False)
        self.assertEqual((new.lan_enabled, new.usb_enabled, new.usb_auto_pair), (True, False, True))
        again = SettingsStore(self.tmp.name)
        self.assertEqual(again.get(), new)
        self.assertEqual(store.last_error, "")

    def test_unknown_names_and_non_bool_values_are_ignored(self):
        store = SettingsStore(self.tmp.name)
        store.update(lan_enabled="yes", nonsense=True, usb_enabled=1)
        self.assertEqual(store.get(), Settings())


class Tolerance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, config.FILE_NAME)

    def test_damaged_files_fall_back_to_the_defaults_and_never_raise(self):
        for blob in (b"", b"\x00\xff\xfe garbage", b"{", b"[]", b"null", b'"text"', b"5", b'{"usb_enabled": ', b"\xef\xbb\xbf{}x",
                     b"{" * 5000, b"x" * 40_000, json.dumps({"lan_enabled": {"a": 1}}).encode()):
            with open(self.path, "wb") as f:
                f.write(blob)
            with self.assertLogs("config", "WARNING"):
                store = SettingsStore(self.tmp.name)
            self.assertEqual(store.get(), Settings(), blob[:20])

    def test_a_value_of_the_wrong_type_only_costs_that_value(self):
        with open(self.path, "w") as f:
            json.dump({"usb_enabled": False, "lan_enabled": "true", "usb_auto_pair": 0}, f)
        with self.assertLogs("config", "WARNING"):
            store = SettingsStore(self.tmp.name)
        self.assertEqual(store.get(), Settings(usb_enabled=False, lan_enabled=False, usb_auto_pair=True))

    def test_unknown_keys_survive_a_save(self):
        with open(self.path, "w") as f:
            json.dump({"lan_enabled": True, "from_a_newer_build": {"x": [1, 2]}}, f)
        store = SettingsStore(self.tmp.name)
        store.update(usb_enabled=False)
        with open(self.path) as f:
            doc = json.load(f)
        self.assertEqual(doc["from_a_newer_build"], {"x": [1, 2]})
        self.assertEqual((doc["lan_enabled"], doc["usb_enabled"]), (True, False))

    def test_a_damaged_file_is_replaced_by_the_next_save(self):
        with open(self.path, "wb") as f:
            f.write(b"\x00garbage")
        with self.assertLogs("config", "WARNING"):
            store = SettingsStore(self.tmp.name)
        store.update(lan_enabled=True)
        self.assertEqual(SettingsStore(self.tmp.name).get().lan_enabled, True)

    def test_a_failing_write_never_raises_and_the_change_still_applies(self):
        store = SettingsStore(self.tmp.name)
        with mock.patch.object(config.os, "replace", side_effect=PermissionError("locked")):
            with self.assertLogs("config", "WARNING"):
                new = store.update(lan_enabled=True)
        self.assertTrue(new.lan_enabled and store.get().lan_enabled)
        self.assertEqual(store.last_error, "PermissionError")
        self.assertEqual([n for n in os.listdir(self.tmp.name) if n.endswith(".tmp")], [])         # no temporary file left behind
        store.update(lan_enabled=True)                                                               # the next try works and clears the error
        self.assertEqual(store.last_error, "")

    def test_an_unwritable_directory_is_survived(self):
        blocker = os.path.join(self.tmp.name, "file")
        with open(blocker, "w") as f:
            f.write("x")
        store = SettingsStore(os.path.join(blocker, "inside"))                                      # a "directory" below a regular file
        with self.assertLogs("config", "WARNING"):
            store.update(usb_enabled=False)
        self.assertFalse(store.get().usb_enabled)
        self.assertNotEqual(store.last_error, "")

    def test_writes_are_atomic_the_old_content_stays_until_the_new_one_is_complete(self):
        store = SettingsStore(self.tmp.name)
        store.update(lan_enabled=True)
        seen = []
        real_replace = os.replace

        def spy(src, dst):
            with open(dst) as f:
                seen.append(json.load(f))                                                            # the target is still the complete old file
            with open(src) as f:
                seen.append(json.load(f))                                                            # the temp file is already complete
            real_replace(src, dst)
        with mock.patch.object(config.os, "replace", spy):
            store.update(lan_enabled=False)
        self.assertEqual((seen[0]["lan_enabled"], seen[1]["lan_enabled"]), (True, False))

    def test_concurrent_updates_do_not_corrupt_the_file(self):
        store = SettingsStore(self.tmp.name)

        def hammer(i):
            for n in range(40):
                store.update(lan_enabled=bool(n % 2), usb_auto_pair=bool(i % 2))
        threads = [threading.Thread(target=hammer, args=(i,)) for i in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        with open(store.path) as f:
            json.load(f)
        self.assertEqual([n for n in os.listdir(self.tmp.name) if n.endswith(".tmp")], [])


if __name__ == "__main__":
    unittest.main()

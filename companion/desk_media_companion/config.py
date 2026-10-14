"""Small persistent application settings: a JSON file in the data directory (`settings.json`, next to the pairing store).

    usb_enabled     talk to the display over the USB cable (default True)
    lan_enabled     ALSO serve the display over the local network: the HTTP server plus the UDP discovery beacon (default False, so
                    a USB-only installation opens no listening socket and never triggers the Windows Firewall prompt)
    usb_auto_pair   open the pairing window by itself when an unpaired display appears while nothing is paired (default True)

The file is tiny, written atomically (a temporary file in the same folder, then `os.replace`) and read tolerantly: a missing, unreadable,
oversized or corrupt file, a value of the wrong type or an unknown key never raises - the defaults apply for whatever cannot be used, and
the settings window (or anything else) can simply write a fresh file. Keys this version does not know are kept and written back, so a
newer or older build sharing the file does not lose its own flags. Nothing secret lives here (the pairing key is in `pairing.bin`).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, fields, replace
from typing import Optional

from .pairing import default_data_dir

log = logging.getLogger("config")

FILE_NAME = "settings.json"
_FILE_MAX = 16 * 1024
_EXTRA_MAX = 32               # unknown keys that are carried along


@dataclass(frozen=True)
class Settings:
    usb_enabled: bool = True
    lan_enabled: bool = False
    usb_auto_pair: bool = True


_DEFAULTS = Settings()
_BOOLS = {f.name for f in fields(Settings)}


class SettingsStore:
    """The settings of this installation. Thread-safe; `get()` is cheap, `update()` persists."""

    def __init__(self, directory: Optional[str] = None) -> None:
        self.dir = directory or default_data_dir()
        self.path = os.path.join(self.dir, FILE_NAME)
        self._lock = threading.Lock()              # the in-memory values
        self._save_lock = threading.Lock()         # one writer at a time
        self._settings = _DEFAULTS
        self._extra: dict = {}
        self.last_error = ""                  # why the last save failed ("" = it did not); the UI may show it
        self.load()

    # -- reading -----------------------------------------------------------------------------------------------------------------------
    def get(self) -> Settings:
        with self._lock:
            return self._settings

    def load(self) -> Settings:
        """(Re)read the file. Never raises; whatever cannot be used falls back to the default."""
        values: dict = {}
        extra: dict = {}
        try:
            with open(self.path, "rb") as f:
                blob = f.read(_FILE_MAX + 1)
            if len(blob) > _FILE_MAX:
                raise ValueError("file too large")
            doc = json.loads(blob.decode("utf-8"))
            if not isinstance(doc, dict):
                raise ValueError("not an object")
            for key, value in doc.items():
                if key in _BOOLS:
                    if isinstance(value, bool):
                        values[key] = value
                    else:
                        log.warning("setting %s has a wrong type: using the default", key)
                elif isinstance(key, str) and len(key) <= 64 and len(extra) < _EXTRA_MAX:
                    extra[key] = value
        except FileNotFoundError:
            pass
        except Exception as exc:                  # corrupt / unreadable: start from the defaults, never crash
            log.warning("settings unreadable (%s): using the defaults", type(exc).__name__)
            values, extra = {}, {}
        with self._lock:
            self._settings = replace(_DEFAULTS, **values)
            self._extra = extra
            return self._settings

    # -- writing -----------------------------------------------------------------------------------------------------------------------
    def update(self, **changes: bool) -> Settings:
        """Change some settings and persist them. Unknown names or non-bool values are ignored (ValueError would only crash a UI for
        nothing). A failing write is logged and reported in `last_error`; the new values still apply for this run."""
        clean = {k: v for k, v in changes.items() if k in _BOOLS and isinstance(v, bool)}
        with self._save_lock:                      # saves are serialised, and each writes the newest state: the file ends up as the last caller left it
            with self._lock:
                self._settings = replace(self._settings, **clean)
                current, extra = self._settings, dict(self._extra)
            self._save(current, extra)
        return current

    def _save(self, settings: Settings, extra: dict) -> None:
        doc = dict(extra)
        doc.update({f.name: getattr(settings, f.name) for f in fields(Settings)})
        tmp = ""
        try:
            os.makedirs(self.dir, exist_ok=True)
            data = json.dumps(doc, indent=2, sort_keys=True).encode("utf-8")
            fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=FILE_NAME + ".", suffix=".tmp")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            for attempt in range(3):
                try:
                    os.replace(tmp, self.path)
                    break
                except PermissionError:            # Windows: a reader (an editor, a virus scanner) holds the file for a moment
                    if attempt == 2:
                        raise
                    time.sleep(0.02)
            tmp = ""
            self.last_error = ""
        except (OSError, TypeError, ValueError) as exc:
            self.last_error = type(exc).__name__
            log.warning("could not save the settings (%s)", type(exc).__name__)
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

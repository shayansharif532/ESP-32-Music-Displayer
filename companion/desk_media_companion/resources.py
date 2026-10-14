"""Data files that ship inside the package (`desk_media_companion/assets/`), found both when running from source and from the
PyInstaller exe.

The PyInstaller spec (companion/DeskMediaCompanion.spec) puts the assets folder into the exe's folder (under `sys._MEIPASS`), keeping the
structure.
"""
from __future__ import annotations

import os
import sys

PACKAGE = "desk_media_companion"
ASSETS = "assets"


def resource_path(name: str) -> str:
    """Absolute path of the asset `name` (a plain file name, no folders). The file is not checked for existence: the caller decides
    what a missing asset means."""
    if not name or name != os.path.basename(name) or name in (".", ".."):
        raise ValueError(f"not a plain asset file name: {name!r}")
    bundle = getattr(sys, "_MEIPASS", None)
    base = os.path.join(bundle, PACKAGE) if bundle else os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, ASSETS, name)

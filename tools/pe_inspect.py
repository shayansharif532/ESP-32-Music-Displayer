"""Reads what matters about a Windows executable without running it (standard library only, Windows only):

    python tools\\pe_inspect.py path\\to\\DeskMediaCompanion.exe        # prints JSON

    machine / subsystem   x64 and "gui" (a windowless exe; "console" would open a console window)
    manifest              the embedded application manifest as text (None if absent)
    version               the string table of the version resource and the fixed 4-part file version
    icon_sizes            the pixel sizes in the exe's icon group

Used by tools/build_release.ps1 (after the build) and tools/installer_smoke_test.py (on the installed exe).
"""
from __future__ import annotations

import ctypes
import json
import struct
import sys
from ctypes import wintypes

MACHINES = {0x8664: "x64", 0x14C: "x86", 0xAA64: "arm64"}
SUBSYSTEMS = {2: "gui", 3: "console"}
LOAD_LIBRARY_AS_DATAFILE = 0x2
LOAD_LIBRARY_AS_IMAGE_RESOURCE = 0x20
RT_ICON_GROUP, RT_MANIFEST, RT_VERSION = 14, 24, 16


def pe_header(path: str) -> dict:
    with open(path, "rb") as f:
        head = f.read(4096)
    if head[:2] != b"MZ":
        raise ValueError("not a PE file")
    (pe_off,) = struct.unpack_from("<I", head, 0x3C)
    if head[pe_off:pe_off + 4] != b"PE\0\0":
        raise ValueError("no PE signature")
    machine, _n, _ts, _p, _s, opt_size, _chars = struct.unpack_from("<HHIIIHH", head, pe_off + 4)
    opt = pe_off + 24
    (magic,) = struct.unpack_from("<H", head, opt)
    (subsystem,) = struct.unpack_from("<H", head, opt + 68)
    (timestamp,) = struct.unpack_from("<I", head, pe_off + 8)
    return {"machine": MACHINES.get(machine, hex(machine)), "pe32plus": magic == 0x20B,
            "subsystem": SUBSYSTEMS.get(subsystem, str(subsystem)), "timestamp": timestamp}


class _Resources:
    """The exe loaded as a data file (no code is run) for resource lookups."""

    def __init__(self, path: str) -> None:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k = k
        k.LoadLibraryExW.restype = ctypes.c_void_p
        k.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
        k.FindResourceW.restype = ctypes.c_void_p
        k.FindResourceW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        k.LoadResource.restype = ctypes.c_void_p
        k.LoadResource.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k.LockResource.restype = ctypes.c_void_p
        k.LockResource.argtypes = [ctypes.c_void_p]
        k.SizeofResource.restype = wintypes.DWORD
        k.SizeofResource.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k.FreeLibrary.argtypes = [ctypes.c_void_p]
        k.EnumResourceNamesW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        self.h = k.LoadLibraryExW(path, None, LOAD_LIBRARY_AS_DATAFILE | LOAD_LIBRARY_AS_IMAGE_RESOURCE)
        if not self.h:
            raise OSError(ctypes.get_last_error(), "cannot load the file for resource access")

    def names(self, rtype: int) -> list:
        found = []
        proto = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)

        def cb(_h, _t, name, _p):
            found.append(name)
            return True
        self._cb = proto(cb)
        self.k.EnumResourceNamesW(self.h, ctypes.c_void_p(rtype), self._cb, None)
        return found

    def data(self, rtype: int, name) -> bytes:
        res = self.k.FindResourceW(self.h, ctypes.c_void_p(name), ctypes.c_void_p(rtype))
        if not res:
            return b""
        size = self.k.SizeofResource(self.h, res)
        ptr = self.k.LockResource(self.k.LoadResource(self.h, res))
        return ctypes.string_at(ptr, size)

    def close(self) -> None:
        self.k.FreeLibrary(self.h)


def read_manifest(path: str):
    r = _Resources(path)
    try:
        for name in r.names(RT_MANIFEST):
            blob = r.data(RT_MANIFEST, name)
            if blob:
                return blob.decode("utf-8-sig", "replace")
        return None
    finally:
        r.close()


def icon_sizes(path: str) -> list[int]:
    r = _Resources(path)
    try:
        sizes: list[int] = []
        for name in r.names(RT_ICON_GROUP):
            blob = r.data(RT_ICON_GROUP, name)
            if len(blob) >= 6:
                (count,) = struct.unpack_from("<H", blob, 4)
                for i in range(count):
                    w = blob[6 + 14 * i]
                    sizes.append(w or 256)
        return sorted(set(sizes))
    finally:
        r.close()


def read_version(path: str) -> dict:
    v = ctypes.WinDLL("version", use_last_error=True)
    v.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    v.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    v.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    v.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    size = v.GetFileVersionInfoSizeW(path, None)
    if not size:
        return {}
    buf = ctypes.create_string_buffer(size)
    if not v.GetFileVersionInfoW(path, 0, size, buf):
        return {}
    out: dict = {}
    ptr, length = ctypes.c_void_p(), wintypes.UINT()
    if v.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(length)) and length.value >= 52:
        _sig, _sv, ms, ls = struct.unpack("<IIII", ctypes.string_at(ptr, 16))
        out["file_version_fixed"] = f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    if v.VerQueryValueW(buf, "\\VarFileInfo\\Translation", ctypes.byref(ptr), ctypes.byref(length)) and length.value >= 4:
        lang, cp = struct.unpack("<HH", ctypes.string_at(ptr, 4))
        table = f"{lang:04x}{cp:04x}"
    else:
        table = "040904b0"
    for key in ("CompanyName", "FileDescription", "FileVersion", "InternalName", "LegalCopyright", "OriginalFilename",
                "ProductName", "ProductVersion"):
        if v.VerQueryValueW(buf, f"\\StringFileInfo\\{table}\\{key}", ctypes.byref(ptr), ctypes.byref(length)) and length.value:
            out[key] = ctypes.wstring_at(ptr.value, length.value).rstrip("\0")
    return out


def inspect(path: str) -> dict:
    info = pe_header(path)
    info["manifest"] = read_manifest(path)
    info["version"] = read_version(path)
    info["icon_sizes"] = icon_sizes(path)
    return info


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: pe_inspect.py <file.exe>")
    print(json.dumps(inspect(sys.argv[1]), indent=2))

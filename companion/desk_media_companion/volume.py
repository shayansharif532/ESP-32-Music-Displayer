"""Per-application volume through the Windows Core Audio session API (the same per-app sliders as the Volume Mixer).

Only ISimpleAudioVolume of the audio sessions that belong to the displayed media app is touched. The Windows master volume
(IAudioEndpointVolume) and every other application are never read or written.

How a media session is tied to audio sessions: GSMTC gives an app id ("Spotify.exe", "chrome.exe", "msedge.exe", or for Store
apps "Family_hash!App"). Every Core Audio session reports the process that owns it; a session belongs to the app when that
process has the app's executable name (or lives in the app's package folder). Spotify and the Chromium browsers run several
processes, so all processes of the right name are considered, on every active playback device.

Browser limitation: Chrome / Edge mix the audio of ALL tabs in one audio process that owns one session, so the volume of
"the YouTube tab" cannot be addressed separately - the whole browser's volume changes. Firefox has an opaque app id and is
not matched (the call reports that no audio session was found).

COM is used from one dedicated worker thread so the HTTP threads never touch it.
"""
from __future__ import annotations

import concurrent.futures
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Optional

import psutil

log = logging.getLogger("volume")

STEP_DEFAULT = 5
_CALL_TIMEOUT_S = 4.0

_executor: Optional[concurrent.futures.ThreadPoolExecutor] = None


def _com_init() -> None:
    import comtypes
    comtypes.CoInitialize()


def _run(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    global _executor
    if _executor is None:
        _executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="coreaudio", initializer=_com_init)
    try:
        return _executor.submit(fn).result(timeout=_CALL_TIMEOUT_S)
    except Exception as exc:                      # never let an audio hiccup break the API
        log.warning("audio call failed: %s", exc)
        return {"ok": False, "error": f"audio error: {str(exc)[:100]}"}


@dataclass
class _Session:
    pid: int
    name: str                 # process executable name, e.g. "chrome.exe"
    exe: str                  # full path (may be empty)
    vol: Any                  # ISimpleAudioVolume
    active: bool


def app_match_keys(app_id: str) -> tuple[set[str], str]:
    """(lower-case executable names, package-folder fragment) that identify the app's processes."""
    a = (app_id or "").strip()
    exes: set[str] = set()
    pkg = ""
    if "!" in a:                                   # Store app: PackageFamilyName!AppId
        fam, _, app = a.partition("!")
        pkg = fam.split("_")[0].lower()
        if app:
            exes.add(app.lower() + ".exe")
    elif a.lower().endswith(".exe"):
        exes.add(a.lower())
    elif a:
        exes.add(a.lower() + ".exe")
    return exes, pkg


def process_matches(app_id: str, name: str, exe: str) -> bool:
    exes, pkg = app_match_keys(app_id)
    if name.lower() in exes:
        return True
    return bool(pkg) and pkg in exe.lower().replace("\\", "/")


_proc_cache: dict[int, tuple[float, str, str]] = {}      # pid -> (process creation time, name, exe path)


def _process_info(pid: int) -> Optional[tuple[str, str]]:
    """(name, exe path) of a process, cached per pid (validated by the process creation time, so a reused pid is not confused)."""
    try:
        proc = psutil.Process(pid)
        created = proc.create_time()
        hit = _proc_cache.get(pid)
        if hit and hit[0] == created:
            return hit[1], hit[2]
        name = proc.name()
        try:
            exe = proc.exe()
        except (psutil.AccessDenied, psutil.ZombieProcess):
            exe = ""
        if len(_proc_cache) > 512:
            _proc_cache.clear()
        _proc_cache[pid] = (created, name, exe)
        return name, exe
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def _sessions_for(app_id: str) -> list[_Session]:
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, EDataFlow, IAudioSessionControl2, IAudioSessionManager2, ISimpleAudioVolume

    found: list[_Session] = []
    enumerator = AudioUtilities.GetDeviceEnumerator()
    devices = enumerator.EnumAudioEndpoints(EDataFlow.eRender.value, 1)         # 1 = DEVICE_STATE_ACTIVE
    for d in range(devices.GetCount()):
        try:
            mgr = devices.Item(d).Activate(IAudioSessionManager2._iid_, CLSCTX_ALL, None).QueryInterface(IAudioSessionManager2)
            sessions = mgr.GetSessionEnumerator()
            for i in range(sessions.GetCount()):
                ctl = sessions.GetSession(i)
                ctl2 = ctl.QueryInterface(IAudioSessionControl2)
                pid = ctl2.GetProcessId()
                if pid <= 0:
                    continue                                                      # system sounds
                info = _process_info(pid)
                if info is None:
                    continue
                name, exe = info
                if not process_matches(app_id, name, exe):
                    continue
                vol = ctl.QueryInterface(ISimpleAudioVolume)
                vol.GetMasterVolume()                                              # an expired session raises here: skip it
                found.append(_Session(pid, name, exe, vol, ctl.GetState() == 1))
        except Exception as exc:                                                  # one odd device must not hide the others
            log.debug("device %d skipped: %s", d, exc)
    return found


def _describe(app_id: str, sessions: list[_Session]) -> dict[str, Any]:
    pick = [s for s in sessions if s.active] or sessions
    level = max(s.vol.GetMasterVolume() for s in pick)
    muted = all(bool(s.vol.GetMute()) for s in pick)
    return {"ok": True, "app": app_id, "volume": int(round(level * 100)), "muted": muted, "sessions": len(sessions)}


def _no_session(app_id: str) -> dict[str, Any]:
    exes, pkg = app_match_keys(app_id)
    return {"ok": False, "app": app_id, "error": "no audio session found for " + (", ".join(sorted(exes)) or pkg or "this app"),
            "volume": None}


def get_volume(app_id: str) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        sessions = _sessions_for(app_id)
        return _describe(app_id, sessions) if sessions else _no_session(app_id)
    return _run(work)


def change_volume(app_id: str, delta: Optional[int] = None, level: Optional[int] = None) -> dict[str, Any]:
    """Relative (delta, percentage points) or absolute (level 0..100) change of the app's session volume(s)."""
    def work() -> dict[str, Any]:
        sessions = _sessions_for(app_id)
        if not sessions:
            return _no_session(app_id)
        before = _describe(app_id, sessions)
        base = before["volume"]
        target = level if level is not None else base + (delta or 0)
        target = max(0, min(100, int(target)))
        for s in sessions:
            s.vol.SetMasterVolume(target / 100.0, None)
            if target > 0 and (delta or 0) > 0 and s.vol.GetMute():
                s.vol.SetMute(False, None)                # pressing "up" on a muted app un-mutes it
        after = _describe(app_id, sessions)
        log.info("volume %s: %d%% -> %d%% (%d session(s))", app_id, base, after["volume"], len(sessions))
        return after
    return _run(work)


def list_sessions() -> list[dict[str, Any]]:
    """Developer aid: every audio session on every active playback device (name, pid, volume, state)."""
    def work() -> dict[str, Any]:
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, EDataFlow, IAudioSessionControl2, IAudioSessionManager2, ISimpleAudioVolume
        rows = []
        enumerator = AudioUtilities.GetDeviceEnumerator()
        devices = enumerator.EnumAudioEndpoints(EDataFlow.eRender.value, 1)
        for d in range(devices.GetCount()):
            dev = devices.Item(d)
            mgr = dev.Activate(IAudioSessionManager2._iid_, CLSCTX_ALL, None).QueryInterface(IAudioSessionManager2)
            sessions = mgr.GetSessionEnumerator()
            for i in range(sessions.GetCount()):
                ctl = sessions.GetSession(i)
                ctl2 = ctl.QueryInterface(IAudioSessionControl2)
                pid = ctl2.GetProcessId()
                try:
                    name = psutil.Process(pid).name() if pid else "(system sounds)"
                except psutil.Error:
                    name = "?"
                vol = ctl.QueryInterface(ISimpleAudioVolume)
                rows.append({"device": d, "pid": pid, "process": name, "volume": round(vol.GetMasterVolume() * 100),
                             "muted": bool(vol.GetMute()), "state": ["inactive", "active", "expired"][ctl.GetState()]})
        return {"ok": True, "rows": rows}
    return _run(work).get("rows", [])


if __name__ == "__main__":
    import json
    import sys
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(list_sessions(), indent=1))
    if len(sys.argv) > 1:
        print(json.dumps(get_volume(sys.argv[1]), indent=1))

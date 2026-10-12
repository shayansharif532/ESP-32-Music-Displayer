"""Developer tool: does a media session really ACCEPT shuffle / repeat changes through Windows (GSMTC)? Changes the state, reads it
back, and puts everything back to what it was.

    companion\\.venv\\Scripts\\python.exe companion\\tools\\probe_transport.py Spotify.exe

It does not play, pause or skip anything - only shuffle on/off and the three repeat modes are touched, and restored at the end
(also when something fails).
"""
import asyncio
import sys
import time

from winrt.windows.media import MediaPlaybackAutoRepeatMode as Repeat
from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as SessionManager


async def settle(session, what, want, getter, tries=20):
    t0 = time.perf_counter()
    for _ in range(tries):
        await asyncio.sleep(0.1)
        if getter(session.get_playback_info()) == want:
            return round((time.perf_counter() - t0) * 1000)
    return None


async def main(app: str) -> None:
    mgr = await SessionManager.request_async()
    session = next((s for s in mgr.get_sessions() if (s.source_app_user_model_id or "").lower() == app.lower()), None)
    if session is None:
        print("no such session")
        return
    info = session.get_playback_info()
    shuffle0, repeat0 = info.is_shuffle_active, info.auto_repeat_mode
    print(f"start: shuffle={shuffle0} repeat={repeat0.name if repeat0 is not None else None}  (shuffle_enabled={info.controls.is_shuffle_enabled} repeat_enabled={info.controls.is_repeat_enabled})")
    try:
        for want in (not shuffle0, shuffle0):
            ok = await session.try_change_shuffle_active_async(want)
            ms = await settle(session, "shuffle", want, lambda i: i.is_shuffle_active)
            print(f"shuffle -> {want}: call returned {ok}, state now {session.get_playback_info().is_shuffle_active} (settled in {ms} ms)")
        for want in (Repeat.LIST, Repeat.TRACK, Repeat.NONE):
            ok = await session.try_change_auto_repeat_mode_async(want)
            ms = await settle(session, "repeat", want, lambda i: i.auto_repeat_mode)
            print(f"repeat -> {want.name}: call returned {ok}, state now {session.get_playback_info().auto_repeat_mode.name} (settled in {ms} ms)")
    finally:
        await session.try_change_shuffle_active_async(shuffle0)
        if repeat0 is not None:
            await session.try_change_auto_repeat_mode_async(repeat0)
        await asyncio.sleep(0.6)
        info = session.get_playback_info()
        print(f"restored: shuffle={info.is_shuffle_active} repeat={info.auto_repeat_mode.name if info.auto_repeat_mode is not None else None}")


asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "Spotify.exe"))

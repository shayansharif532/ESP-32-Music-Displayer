"""Developer tool (READ ONLY): what each media session reports about shuffle / repeat and which transport controls it enables.

    companion\\.venv\\Scripts\\python.exe companion\\tools\\dump_transport.py

Prints, per session: playback status, is_shuffle_active, auto_repeat_mode and every enabled-flag of the controls
(play / pause / stop / next / previous / seek / shuffle / repeat / playback-rate ...). Nothing is changed.
"""
import asyncio

from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as SessionManager


async def main() -> None:
    mgr = await SessionManager.request_async()
    for s in mgr.get_sessions():
        app = s.source_app_user_model_id
        try:
            info = s.get_playback_info()
        except Exception as exc:
            print(f"{app}: not readable ({exc})")
            continue
        ctl = info.controls
        flags = {n: getattr(ctl, n) for n in dir(ctl) if n.startswith("is_") and not callable(getattr(ctl, n))}
        print(f"== {app}")
        print(f"   status={info.playback_status.name}  type={info.playback_type}")
        print(f"   is_shuffle_active={info.is_shuffle_active!r}   auto_repeat_mode={info.auto_repeat_mode!r}")
        print("   controls: " + ", ".join(f"{k[3:]}={'Y' if v else '-'}" for k, v in sorted(flags.items())))


asyncio.run(main())

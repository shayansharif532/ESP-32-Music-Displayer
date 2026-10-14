"""Entry point used by PyInstaller (DeskMediaCompanion.exe). Same as: python -m desk_media_companion

Handled here, before the real program starts, because they are about the packaged build rather than about the companion:

  --version    prints the version. The packaged exe is windowless and has no stdout, so without a stdout the line is written to
               %TEMP%\\deskmedia-version.txt instead (no message box: a dialog would block unattended runs such as the installer's).
  --selftest   loads and exercises every runtime dependency and prints one JSON line (desk_media_companion/selftest.py).
  --quit       asks a running companion to exit and waits for it (used by the installer for upgrades and uninstalling).
               Exit code 0 = no companion is running any more, 1 = it did not stop in time.

A running companion listens for that request on a named event (`Local\\DeskMediaCompanion-Quit`; the name can be changed with
the environment variable DESK_MEDIA_QUIT_EVENT, which the tests use so they never touch a real companion).
"""
import multiprocessing
import os
import sys

VERSION_FILE = "deskmedia-version.txt"        # in %TEMP%, written only when the process has no stdout
QUIT_EVENT = "Local\\DeskMediaCompanion-Quit"
QUIT_WAIT_S = 15.0                            # how long --quit waits for the companion to be gone
QUIT_GRACE_S = 6.0                            # how long a companion that was asked to quit may take before it is ended hard

# flags after which the process is not a running companion (no quit listener is started for them)
_NOT_A_SERVER = ("--version", "--selftest", "--quit", "--probe", "--install-startup", "--remove-startup", "-h", "--help")


def _quit_event_name() -> str:
    return os.environ.get("DESK_MEDIA_QUIT_EVENT") or QUIT_EVENT


def _write_text(text: str, temp_name: str) -> None:
    """Text to stdout when there is one, else to %TEMP%\\<temp_name> (a --noconsole exe has no stdout)."""
    out = sys.stdout
    if out is not None:
        try:
            out.write(text + "\n")
            out.flush()
            return
        except (OSError, ValueError):
            pass
    import tempfile
    try:
        with open(os.path.join(tempfile.gettempdir(), temp_name), "w", encoding="utf-8", newline="\n") as f:
            f.write(text + "\n")
    except OSError:
        pass


def _kernel32():
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateEventW.restype = wintypes.HANDLE
    k.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    k.OpenEventW.restype = wintypes.HANDLE
    k.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k.SetEvent.argtypes = [wintypes.HANDLE]
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k.WaitForSingleObject.restype = wintypes.DWORD
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    return k


def request_quit(wait_s: float = QUIT_WAIT_S) -> int:
    """--quit: signal the running companion and wait until its event object is gone (= the process ended)."""
    if os.name != "nt":
        return 0
    import time
    k = _kernel32()
    SYNCHRONIZE, EVENT_MODIFY_STATE = 0x00100000, 0x0002
    name = _quit_event_name()
    h = k.OpenEventW(EVENT_MODIFY_STATE | SYNCHRONIZE, False, name)
    if not h:
        _write_text("not running", "deskmedia-quit.txt")
        return 0
    k.SetEvent(h)
    k.CloseHandle(h)                         # our own handle must not keep the object alive while we wait for it to vanish
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        probe = k.OpenEventW(SYNCHRONIZE, False, name)
        if not probe:
            _write_text("stopped", "deskmedia-quit.txt")
            return 0
        k.CloseHandle(probe)
        time.sleep(0.2)
    _write_text("still running", "deskmedia-quit.txt")
    return 1


def _start_quit_listener() -> None:
    """In a running companion: a daemon thread that waits for --quit and then ends the process (Ctrl+C-style first, so the
    tray icon and the beacon are cleaned up; hard exit if that does not finish within QUIT_GRACE_S)."""
    if os.name != "nt":
        return
    import threading

    def run() -> None:
        try:
            k = _kernel32()
            h = k.CreateEventW(None, True, False, _quit_event_name())      # manual reset: OpenEvent users see "signalled" until we are gone
            if not h:
                return
            INFINITE = 0xFFFFFFFF
            if k.WaitForSingleObject(h, INFINITE) != 0:                     # WAIT_OBJECT_0
                return
            import logging
            import _thread
            import time
            logging.getLogger("main").info("quit requested (installer or --quit)")
            _thread.interrupt_main()
            time.sleep(QUIT_GRACE_S)
            logging.getLogger("main").warning("did not stop in %.0f s: ending the process", QUIT_GRACE_S)
            logging.shutdown()
            os._exit(0)
        except Exception:
            pass                                                            # the listener is a convenience; it must never hurt the app

    threading.Thread(target=run, name="quit-listener", daemon=True).start()


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--version" in argv:
        from desk_media_companion import __version__
        _write_text(__version__, VERSION_FILE)
        return 0
    if "--selftest" in argv:
        from desk_media_companion.selftest import main as selftest_main
        return selftest_main()
    if "--quit" in argv:
        return request_quit()
    if not any(a in _NOT_A_SERVER for a in argv):
        _start_quit_listener()
    try:
        from desk_media_companion.__main__ import main as companion_main
        return companion_main(argv)
    except KeyboardInterrupt:                # the quit listener (or Ctrl+C) interrupted an unusual moment: that is a normal exit
        return 0
    except Exception:
        _log_crash()                         # the exe has no console and no error dialog: the log file is the only place to look
        return 1


def _log_crash() -> None:
    """Appends the traceback of an unexpected error to companion.log in the data folder (best effort, never raises)."""
    try:
        import time
        import traceback
        base = os.environ.get("DESK_MEDIA_DATA_DIR") or os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "DeskMediaCompanion")
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "companion.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} CRITICAL main: unexpected error, the companion stopped\n{traceback.format_exc()}")
    except Exception:
        pass


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())

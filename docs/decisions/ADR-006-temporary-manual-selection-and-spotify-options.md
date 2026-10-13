# ADR-006 — Manual source selection is temporary (AUTO / MANUAL_TEMP); Spotify shuffle / repeat through Windows' own session controls (2026-10-12) — **current**

Status: **accepted** (maintainer request after using M8c). Details and evidence: [`docs/development-history/M8d-auto-follow-spotify-options.md`](../development-history/M8d-auto-follow-spotify-options.md). Supersedes the "pinned until the session disappears" rule of ADR-005.

* **Auto-follow is the long-term mode; a swipe never disables it.** The companion's selection is an explicit two-state machine (`sources.Selector`, pure, 43 unit tests in test_sources.py):
  * `AUTO`: follow the playing session that **started playing most recently** (ties: the system's current session); nothing playing: the app that played last, then the current session.
  * `MANUAL_TEMP` (entered by a vertical swipe): the chosen session is shown while the user deals with it, and the display returns to `AUTO` when (a) **another** session starts playing (a pause -> play transition or a new playing session appearing),
    (b) the selected session disappears, or (c) the selected session is *not* playing, another one is, and the user has not touched the selected session for **20 s** (`MANUAL_IDLE_RETURN_S`) - the only timeout, and only for the case
    that no event can ever arrive ("Spotify just keeps playing"). A selected session that starts playing (the user pressed play on it) stays; a swipe that lands on what AUTO would show anyway is simply AUTO.
    Brief `changing` / `opened` states (track changes) are not transitions. Interactions that restart the idle timer: any control, seek, shuffle / repeat or volume key aimed at the selected session, and another swipe.
  * A background watcher (every 0.4 s, a few ms per look) keeps the model current even when the display is not polling, so transitions are never missed.
* **Commands still always target the session on screen** (ADR-005): the display names it in every request, the companion refuses it if it is gone. When auto-follow switches the display, the next poll (<= 1 s) moves the screen and
  from then on every control, seek and volume key follows the new session.
* **Shuffle and repeat are Spotify-only and need no Spotify account.** Windows' GSMTC exposes them for Spotify desktop: `is_shuffle_active`, `auto_repeat_mode` (none / list / track), the enabled flags, and
  `try_change_shuffle_active_async` / `try_change_auto_repeat_mode_async` are accepted by Spotify (state read back ~100 ms later). All three repeat modes are real (off, playlist / album, track). No OAuth / Web API.
  Chrome, Edge, VLC ... report none of this. "Spotify" is decided by the session's **app identity** (`Spotify.exe` or the Store package `SpotifyAB.SpotifyMusic_...`), never by metadata, and the companion refuses the commands for every other app.
* **UI**: two small buttons flank the transport row only while Spotify is shown (shuffle left of previous, repeat right of next); subtle when off, green with a dot when on, a "1" on the repeat icon for repeat-track, dimmed when Spotify
  does not allow the change; previous / play / next are exactly where they were; hidden in full-screen mode and for every other app.

# ADR-005 — Media-source switching by vertical swipe; the display, its controls and its volume always target one explicit session (2026-10-12) — **current, except the "pinned" rule (superseded by ADR-006)**

Status: **accepted** (maintainer request after using M8b). Details and evidence: [`docs/development-history/M8c-session-switching.md`](../development-history/M8c-session-switching.md).

* **Gestures**: finger **left = next track, right = previous track** (reversed from M8b); finger **up = next media source, down = previous source** (a source = one Windows media session = one app).
  Same thresholds and safeguards as before (>= 72 px along the axis, a 44 px cross-axis limit at any time, 2:1 dominance, < 1 s, a travelled press is never a tap, the progress bar never starts a swipe).
  A diagonal strays on both axes and is neither. With fewer than two sources a vertical swipe does nothing at all.
* **The companion owns the session model** (`companion/desk_media_companion/sources.py`, pure logic with unit tests; `media.py` applies it to the live GSMTC sessions):
  * the *swipe list* ("ring") = sessions that are readable, not closed and carry a title or artist (playing **and paused** ones; dead / metadata-less sessions such as `PSExpressCore.exe` are left out),
    plus whatever is on screen; fixed alphabetical order by app name, wrapping at both ends, so repeated swipes walk the same ring regardless of playback state;
  * **auto** (initial): follow the best session - the system's current session when it plays, any playing session, the app that played last, the current session, the rest;
  * **pinned**: a swipe pins the display to that session. It stays there - another app starting to play does not take over - for as long as the session exists; when it disappears the pin is dropped and
    auto takes over (`POST /control/source-auto` and a companion restart also return to auto). Documented consequence: a pinned session that is paused keeps the screen even if something else plays.
* **Every command names the session on screen** (`app=` on every `/control/*`, volume and source request): it goes to exactly that session, and is *refused* ("that media session is gone") if it no longer
  exists - never redirected to whatever else is active. This closes the class of bug seen in the MVP (controls on a different session than the one displayed) for good, and it is what makes the side-button
  volume follow a source switch immediately. The source switch itself is relative to the app on screen (`from=`), and its answer carries the new `/now-playing` snapshot so the display switches at once.
* **UI**: no permanent widgets. A faint disc with an up / down chevron drifts in the swipe direction, then a translucent pill (shared with the volume pill) names the new source (`Spotify`, `Chrome`) with
  small position dots, ~1.1 s. The existing status line already shows the source name.
* **Artwork quality** (same pass): Chromium asks the OS for artwork at a fixed "desired 150 px" (`kMinImageSize = 71`, `kDesiredImageSize = 150` in `system_media_controls_notifier.cc`), Windows passes exactly one
  thumbnail stream through unchanged, and GSMTC exposes no URL / larger size / other image, so a browser thumbnail cannot be improved by asking more nicely. PC-side up-scaling was improved (edge-only, halo-limited,
  strength scaled by enlargement; Spotify-sized enlargements are bit-identical to before). **No browser extension**: it is the only route to a real high-resolution image (it can read the page's own
  `MediaSession` artwork list) but needs per-browser installation on the user's PC; recorded as an option in the M8c doc, deliberately not built.

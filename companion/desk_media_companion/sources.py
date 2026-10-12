"""Which Windows media sessions the display can show, and how it moves between them. Pure logic: no WinRT, no I/O.

Model (see docs/milestones/M8c-session-switching.md):

* AUTO (the default): the display follows the "best" session - the system's current session when it is playing, else any
  playing session, else the app that played last, else the current session, else the rest.
* PINNED: a vertical swipe on the display selects a session. From then on the display (artwork, title, timeline, controls,
  volume) stays on exactly that session - another app that becomes active does not take over - for as long as the session
  exists. When it disappears the pin is dropped and AUTO takes over again.
* The swipe list ("sources") holds the sessions that are worth showing: playing or paused ones that carry media metadata.
  Dead / metadata-less sessions are left out. The session that is on screen is always part of the list. The order is fixed
  (alphabetical by app name), so repeated swipes walk through the same ring and wrap around at the ends.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

# Windows app ids -> short names for the on-screen label.
_KNOWN = {
    "spotify": "Spotify", "chrome": "Chrome", "msedge": "Edge", "firefox": "Firefox", "brave": "Brave", "opera": "Opera",
    "vivaldi": "Vivaldi", "vlc": "VLC", "itunes": "iTunes", "applemusic": "Apple Music", "musicbee": "MusicBee",
    "foobar2000": "foobar2000", "tidal": "TIDAL", "deezer": "Deezer", "amazonmusic": "Amazon Music", "soundcloud": "SoundCloud",
    "zunemusic": "Media Player", "mediaplayer": "Media Player", "zunevideo": "Movies & TV", "wmplayer": "Media Player",
    "claude": "Claude", "python": "Python",
}


@dataclass(frozen=True)
class Source:
    app: str                  # GSMTC source app id: "Spotify.exe", "chrome.exe", "Family_hash!App" ...
    state: str                # "playing" "paused" "stopped" "opened" "changing" "closed", "" when the session could not be read
    title: str = ""
    artist: str = ""
    readable: bool = True     # False: the session raised when it was read (dead / not ready)


def friendly_name(app_id: str) -> str:
    """Short, human name of a Windows app id (<= 23 characters)."""
    a = (app_id or "").strip()
    if not a:
        return "Unknown"
    base = a.split("!")[-1] if "!" in a else a                   # Store app: PackageFamilyName!AppId
    base = re.sub(r"\.exe$", "", base, flags=re.I)
    key = re.sub(r"[^a-z0-9]", "", base.lower())
    last = re.sub(r"[^a-z0-9]", "", base.split(".")[-1].lower())  # "Microsoft.ZuneMusic" -> "zunemusic"
    for token, name in _KNOWN.items():
        if token in (key, last) or (len(token) >= 6 and key.endswith(token)):
            return name
    base = base.split(".")[-1] if "." in base else base           # "Publisher.App" -> "App"
    base = base.replace("_", " ").strip() or a
    name = base[:1].upper() + base[1:] if base.islower() else base
    return name[:23]


def is_usable(src: Source) -> bool:
    """Worth offering in the swipe list: readable, not closed, and with something to show (a title or an artist)."""
    return (src.readable and src.state not in ("", "closed") and bool(src.title.strip() or src.artist.strip()))


def _same(a: str, b: str) -> bool:
    return a.lower() == b.lower()


def _find(apps: Iterable[str], wanted: str) -> Optional[str]:
    for a in apps:
        if _same(a, wanted):
            return a
    return None


def candidate_order(sources: list[Source], *, current: str = "", pinned: str = "", last_playing: str = "", prefer: str = "",
                    wanted: str = "") -> list[str]:
    """App ids in the order the display should try them (the first one that answers is shown).

    wanted  - the app the caller says is on screen (controls / volume are applied to that session, never to a different one)
    prefer  - development only (--prefer-app): pin a test session no matter what
    pinned  - the user's own selection (vertical swipe)
    then AUTO: current session if playing, any playing session, the app that played last, the current session, the rest."""
    apps = [s.app for s in sources]
    state = {s.app: s.state for s in sources}
    out: list[str] = []

    def add(app: Optional[str]) -> None:
        if app and not any(_same(app, o) for o in out):
            out.append(app)

    for key in (wanted, prefer, pinned):
        if key:
            add(_find(apps, key))
    cur = _find(apps, current) if current else None
    if cur and state.get(cur) == "playing":
        add(cur)
    for a in apps:
        if state.get(a) == "playing":
            add(a)
    if last_playing:
        add(_find(apps, last_playing))
    add(cur)
    for a in apps:
        add(a)
    return out


def ring(sources: list[Source], displayed: str = "") -> list[str]:
    """The fixed, wrapping list of apps a vertical swipe walks through (usable sessions + the one on screen)."""
    apps = [s.app for s in sources if is_usable(s)]
    if displayed:
        shown = _find((s.app for s in sources), displayed)
        if shown and not _find(apps, shown):
            apps.append(shown)
    return sorted(apps, key=lambda a: (friendly_name(a).lower(), a.lower()))


def step(sources: list[Source], displayed: str, direction: int) -> Optional[str]:
    """The app a swipe moves to: direction +1 = next source, -1 = previous source (wraps around). None when there is
    nothing to switch to (fewer than two sources)."""
    apps = ring(sources, displayed)
    if len(apps) < 2:
        return None
    here = next((i for i, a in enumerate(apps) if displayed and _same(a, displayed)), None)
    if here is None:
        return apps[0] if direction >= 0 else apps[-1]
    return apps[(here + (1 if direction >= 0 else -1)) % len(apps)]


def position(sources: list[Source], displayed: str) -> tuple[int, int]:
    """(1-based index of the displayed app in the ring, ring size); (0, size) when it is not in the ring."""
    apps = ring(sources, displayed)
    for i, a in enumerate(apps):
        if displayed and _same(a, displayed):
            return i + 1, len(apps)
    return 0, len(apps)

"""Which Windows media sessions the display can show, and how it moves between them. Pure logic: no WinRT, no I/O.

The selection model (ADR-005, refined by ADR-006) is an explicit two-state machine, `Selector`:

* AUTO (the default, and always the long-term mode): follow the most relevant PLAYING session - when several play, the one
  that started playing most recently; when nothing plays, the app that played last, then the system's current session.
* MANUAL_TEMP: a vertical swipe selects another existing session. It is shown while the user is dealing with it, and it is
  only ever temporary. The display returns to AUTO
    - as soon as ANOTHER session starts playing (a pause -> play transition, or a new playing session appearing),
    - as soon as the selected session disappears,
    - when the selected session is not playing, some other session is, and the user has not touched the selected session for
      MANUAL_IDLE_RETURN_S seconds (nobody is "interacting" with it any more),
    - or when a swipe lands on the session AUTO would show anyway (that is simply AUTO).
  A selected session that starts playing (the user pressed play on it) is not a reason to leave it.
* The idle screen (ADR-008) is one more item of the swipe ring: a SYNTHETIC source with the identity `IDLE_ID` ("idle"), always last, never
  a Windows session. Swiping onto it is a MANUAL_TEMP selection like any other - a display choice, never a playback command - with two
  differences: it cannot disappear, and the 20 s return does not apply (it is chosen precisely while something plays in the
  background). It ends when ANY session starts playing (a pause -> play transition or a new playing session), exactly as the user
  asked: "if a media session newly starts playing after Idle was selected, switch back to it". Until then the content of the
  snapshot is what AUTO would show (so the player is current underneath).
* The swipe list ("sources", the ring) holds the sessions that are worth showing: playing or paused ones that carry media
  metadata. Dead / metadata-less sessions are left out; the session on screen is always part of it. The order is fixed
  (alphabetical by app name), so repeated swipes walk through the same ring and wrap around at the ends.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional

MANUAL_IDLE_RETURN_S = 20.0      # see MANUAL_TEMP above
IDLE_ID = "idle"                 # the synthetic ring item for the idle screen (not a Windows session; no real app has this id)
IDLE_NAME = "Idle Screen"        # what the source pill shows

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
    if a.lower() == IDLE_ID:
        return IDLE_NAME
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


def is_spotify(app_id: str) -> bool:
    """True only for the Spotify desktop app, judged by the Windows app identity of the session (never by track metadata):
    the classic `Spotify.exe`, or the Microsoft Store package (`SpotifyAB.SpotifyMusic_...!Spotify`)."""
    a = (app_id or "").strip().lower()
    return a == "spotify.exe" or a.startswith("spotifyab.spotifymusic_") or a == "spotify"


def candidate_order(sources: list[Source], *, current: str = "", manual: str = "", last_playing: str = "", prefer: str = "",
                    wanted: str = "", since: Optional[dict[str, float]] = None) -> list[str]:
    """App ids in the order the display should try them (the first one that answers is shown).

    wanted  - the app the caller says is on screen (controls / volume are applied to that session, never to a different one)
    prefer  - development only (--prefer-app): force a test session no matter what
    manual  - the user's temporary selection (vertical swipe, MANUAL_TEMP), empty in AUTO
    then AUTO: the playing sessions, the one that started playing most recently first (ties: the system's current session,
    then Windows' order), the app that played last, the current session, the rest.
    since   - {app: when it last started playing} (any monotonically increasing number)"""
    since = since or {}
    apps = [s.app for s in sources]
    state = {s.app: s.state for s in sources}
    out: list[str] = []

    def add(app: Optional[str]) -> None:
        if app and not any(_same(app, o) for o in out):
            out.append(app)

    for key in (wanted, prefer, manual):
        if key:
            add(_find(apps, key))
    cur = _find(apps, current) if current else None
    playing = [a for a in apps if state.get(a) == "playing"]
    for a in sorted(playing, key=lambda x: (-since.get(x, 0.0), 0 if x == cur else 1, apps.index(x))):
        add(a)
    if last_playing:
        add(_find(apps, last_playing))
    add(cur)
    for a in apps:
        add(a)
    return out


class Mode(str, Enum):
    AUTO = "auto"
    MANUAL_TEMP = "manual"


class Selector:
    """Which session the display shows. Feed it every reading of the sessions with observe(); ask order() for the candidates.
    Time is passed in (monotonic seconds) so the whole model is testable without a clock."""

    def __init__(self, idle_return_s: float = MANUAL_IDLE_RETURN_S) -> None:
        self.idle_return_s = idle_return_s
        self.mode = Mode.AUTO
        self.manual = ""                      # the app selected by a swipe (MANUAL_TEMP only)
        self.last_interaction = 0.0
        self.last_playing = ""                # the app that played most recently (what AUTO shows when nothing plays)
        self.prefer = ""                      # development only (--prefer-app)
        self._stable: dict[str, str] = {}     # app -> last settled state (playing / paused / stopped); transient states are ignored
        self._since: dict[str, float] = {}    # app -> when it last started playing

    # -- observing -------------------------------------------------------------------------------------------------------
    def observe(self, sources: list[Source], now: float) -> list[str]:
        """Update from a reading of all sessions. Returns human-readable notes about what changed (for the log)."""
        notes: list[str] = []
        apps = {s.app for s in sources}
        for gone in [a for a in self._stable if a not in apps]:
            del self._stable[gone]
            self._since.pop(gone, None)
        started: list[str] = []
        for s in sources:
            if not s.readable or s.state not in ("playing", "paused", "stopped"):
                continue                                      # 'changing' / 'opened' blips (track changes) are not transitions
            before = self._stable.get(s.app)
            if s.state == "playing" and before != "playing":
                self._since[s.app] = now
                started.append(s.app)
            elif s.state != "playing":
                self._since.pop(s.app, None)
            self._stable[s.app] = s.state
        playing = [a for a, st in self._stable.items() if st == "playing"]
        if playing:
            self.last_playing = max(playing, key=lambda a: self._since.get(a, 0.0))
        if started:
            notes.append("started playing: " + ", ".join(started))

        if self.mode is Mode.MANUAL_TEMP and _same(self.manual, IDLE_ID):
            # the idle screen was chosen: it can neither disappear nor "time out" (it is chosen while something plays in the
            # background); any session that starts playing - including a paused one resuming - ends the selection
            if started:
                notes.append(self._to_auto(f"{started[0]} started playing (leaving the idle screen)"))
        elif self.mode is Mode.MANUAL_TEMP:
            manual = _find(apps, self.manual)
            others_started = [a for a in started if not _same(a, self.manual)]
            others_playing = [a for a in playing if not _same(a, self.manual)]
            if manual is None:
                notes.append(self._to_auto(f"the selected session {self.manual} disappeared"))
            elif others_started:
                notes.append(self._to_auto(f"{others_started[0]} started playing"))
            elif (self._stable.get(manual) != "playing" and others_playing
                  and now - self.last_interaction >= self.idle_return_s):
                notes.append(self._to_auto(f"no interaction with {manual} for {self.idle_return_s:.0f} s while {others_playing[0]} plays"))
        return notes

    # -- the user -------------------------------------------------------------------------------------------------------
    def select(self, app: str, sources: list[Source], current: str, now: float) -> Mode:
        """A vertical swipe landed on `app`. Landing on what AUTO would show anyway is simply AUTO. The idle screen is always a manual choice
        (AUTO shows sessions, never the synthetic item)."""
        if _same(app, IDLE_ID):
            self.mode, self.manual, self.last_interaction = Mode.MANUAL_TEMP, IDLE_ID, now
            return self.mode
        auto_first = candidate_order(sources, current=current, last_playing=self.last_playing, since=self._since)[:1]
        if auto_first and _same(auto_first[0], app):
            self._to_auto("")
        else:
            self.mode, self.manual, self.last_interaction = Mode.MANUAL_TEMP, app, now
        return self.mode

    @property
    def idle_selected(self) -> bool:
        """The user swiped onto the idle screen (and nothing has started playing since)."""
        return self.mode is Mode.MANUAL_TEMP and _same(self.manual, IDLE_ID)

    def touch(self, now: float, app: str = "") -> None:
        """The user dealt with the displayed session (a control, seek, volume, another swipe): restarts the idle timer."""
        if self.mode is Mode.MANUAL_TEMP and (not app or _same(app, self.manual)):
            self.last_interaction = now

    def to_auto(self) -> None:
        self._to_auto("")

    def _to_auto(self, why: str) -> str:
        self.mode, self.manual = Mode.AUTO, ""
        return f"back to automatic: {why}" if why else ""

    # -- the answer -----------------------------------------------------------------------------------------------------
    def order(self, sources: list[Source], current: str = "", wanted: str = "") -> list[str]:
        manual = self.manual if self.mode is Mode.MANUAL_TEMP and not self.idle_selected else ""    # idle selected: what AUTO would show
        return candidate_order(sources, current=current, manual=manual,
                               last_playing=self.last_playing, prefer=self.prefer, wanted=wanted, since=self._since)


def ring(sources: list[Source], displayed: str = "", idle: bool = False) -> list[str]:
    """The fixed, wrapping list of apps a vertical swipe walks through (usable sessions + the one on screen), alphabetical.
    idle=True adds the synthetic idle-screen item at the end ("Spotify, Chrome, Idle Screen")."""
    apps = [s.app for s in sources if is_usable(s)]
    if displayed and not _same(displayed, IDLE_ID):
        shown = _find((s.app for s in sources), displayed)
        if shown and not _find(apps, shown):
            apps.append(shown)
    apps = sorted(apps, key=lambda a: (friendly_name(a).lower(), a.lower()))
    return apps + [IDLE_ID] if idle else apps


def step(sources: list[Source], displayed: str, direction: int, idle: bool = False) -> Optional[str]:
    """The app a swipe moves to: direction +1 = next source, -1 = previous source (wraps around). None when there is
    nothing to switch to (fewer than two sources). `displayed` may be IDLE_ID when idle=True."""
    apps = ring(sources, displayed, idle)
    if len(apps) < 2:
        return None
    here = next((i for i, a in enumerate(apps) if displayed and _same(a, displayed)), None)
    if here is None:
        return apps[0] if direction >= 0 else apps[-1]
    return apps[(here + (1 if direction >= 0 else -1)) % len(apps)]


def position(sources: list[Source], displayed: str, idle: bool = False) -> tuple[int, int]:
    """(1-based index of the displayed app in the ring, ring size); (0, size) when it is not in the ring."""
    apps = ring(sources, displayed, idle)
    for i, a in enumerate(apps):
        if displayed and _same(a, displayed):
            return i + 1, len(apps)
    return 0, len(apps)

# Desk Media Companion (Windows)

Reads the PC's active **media session** (Windows.Media.Control / GlobalSystemMediaTransportControlsSessionManager) and serves it to the
ESP32 desk display over the local network. Works with anything that publishes to the Windows media overlay: Spotify desktop, YouTube /
YouTube Music (and any site using the browser Media Session API) in Chrome / Edge / Firefox, VLC, Apple Music, ...

Protocol and design: [../docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md).

## Run (development)

```powershell
cd companion
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m desk_media_companion --console     # Ctrl+C to stop
```

| Option | Meaning |
|---|---|
| `--port 8765` | HTTP port the display connects to |
| `--discovery-port 8766` / `--no-discovery` | UDP beacon port / disable the beacon (then set the PC's IP in the firmware's `secrets.local.h`) |
| `--console` / `--quiet` | also log to the console / background mode (file log only) |
| `--probe` | print the current media state once and exit |
| `--dev-tools` / `--prefer-app python.exe` | development only: enable the `/debug/*` routes used by `tools/screenshot.py` / always show that app's session (pins the display to the fake test session) |
| `--install-startup` / `--remove-startup` | start with Windows (per-user `HKCU\...\Run` entry; no admin rights needed) / undo |

Log file: `%LOCALAPPDATA%\DeskMediaCompanion\companion.log` (rotating, titles of tracks only - no secrets exist in this app).

## Build the single-file exe

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\companion\build.ps1     # -> companion\dist\DeskMediaCompanion.exe (~17 MB, no console window)
```

The exe needs no Python on the target PC. `DeskMediaCompanion.exe --install-startup` makes it start with Windows.

## Windows Firewall

The first time the app listens, Windows asks whether to allow it - choose **Allow**. The display connects to **TCP 8765**. If Windows never asked
(or it was dismissed), the display shows "Can't reach your PC". The fix needs one administrator approval, e.g. in an elevated PowerShell:

```powershell
New-NetFirewallRule -DisplayName "Desk Media Companion" -Direction Inbound -Protocol TCP -LocalPort 8765 -RemoteAddress LocalSubnet -Action Allow
```

(`LocalSubnet` keeps it to your home network.) A friendlier one-click helper is part of Milestone 12.

## Artwork and volume (what the companion does beyond relaying the session)

* **Artwork** (`desk_media_companion/artwork.py`): Windows delivers Spotify's thumbnail as a 300x300 RGBA *composite* (the real cover as an opaque block, a transparent gap, then the Spotify logo). The companion splits that
  structurally (alpha bands: exactly one solid block + sparse logo band; never a fixed crop; ambiguous images are left alone), trims symmetric black letterbox bars, and renders the cover at the size the display draws it,
  keeping its aspect ratio (`/artwork?size=312&maxw=432`). Chromium apps only supply ~150 px thumbnails (a Windows limit), so wide covers are soft.
  `tools/dump_sessions.py` (read-only) shows exactly what each app supplies; `tools/render_art_preview.py` shows what the display will receive; `python -m unittest discover -s tests` runs the checks.
* **Per-application volume** (`desk_media_companion/volume.py`): `GET /volume`, `POST /control/volume-up|volume-down[?step=5]`, `POST /volume?level=N` change the **Windows audio-session volume of the displayed app**
  (the same slider as the Volume Mixer) - never the master volume (`tools/check_master_volume.py` proves it). The app's audio sessions are found by process name on every playback device. Chrome / Edge: all tabs share one audio
  process, so the whole browser changes; Firefox cannot be matched. Spotify's own in-app volume multiplies with the Windows one.
* **Media sources** (`desk_media_companion/sources.py` = the model, `media.py` = the live sessions): the display can switch between every usable Windows media session (playing **and paused** ones that have a title or
  artist; dead sessions are left out). `POST /control/source-next[?from=Spotify.exe]` / `source-prev` (a vertical swipe on the display) select the next / previous source in a fixed, wrapping order (alphabetical by app name) and answer
  with the new `/now-playing`; `GET /sources` lists them; `POST /control/source-auto` returns to automatic. **Selection is `AUTO` or `MANUAL_TEMP`**: AUTO follows the playing session that started playing most recently; a swipe makes the
  chosen session a *temporary* manual selection that ends (back to AUTO) when another session starts playing, when the chosen session disappears, or - if it is paused while another one plays - after 20 s without touching it
  (`MANUAL_IDLE_RETURN_S`). A background watcher looks at all sessions every 0.4 s. **Every control / volume request may carry `&app=<app id>`** (the display always does): the command then goes to exactly that session and is refused
  (`"that media session is gone"`) if it no longer exists, never to another one.
* **Spotify shuffle / repeat** (Spotify desktop only - identified by the session's app id): `/now-playing` carries `spotify`, `shuffle`, `repeat` (0 off, 1 playlist / album, 2 track), `can_shuffle`, `can_repeat`;
  `POST /control/shuffle` and `/control/repeat` (also `/shuffle-on|off`, `/repeat-off|list|track`) use Windows' own session controls (`try_change_shuffle_active_async`, `try_change_auto_repeat_mode_async`) - no Spotify account or
  OAuth - and are refused for every other app. `tools/dump_transport.py` shows what each session reports (read-only), `tools/probe_transport.py` proves a session accepts the changes (and restores them).
  `tools/api_check.py` exercises source switching without touching playback; `tools/auto_follow_test.py` (isolated, two silent fake sessions) and `tools/session_test.py` (with the board) are the full tests.
  Development flags: `--ignore-apps`, `--only-apps`, `--treat-as-spotify`, `--prefer-app`.

## Test without any media app

```powershell
.\.venv\Scripts\python.exe -m pip install winrt-Windows.Media.Playback winrt-Windows.Media.Core
.\.venv\Scripts\python.exe tools\fake_media_session.py        # silent session: 3 tracks, artwork (incl. a 16:9 one), next/previous/play/pause/seek
curl http://127.0.0.1:8765/now-playing
curl -X POST -H "X-Desk-Client: test" http://127.0.0.1:8765/control/next
.\.venv\Scripts\python.exe tools\make_test_covers.py logs\test-art                       # synthetic covers: square, 16:9, 4:3 letterboxed, banner, Spotify-style composite
.\.venv\Scripts\python.exe tools\fake_media_session.py --art-dir logs\test-art --silent-audio   # + an inaudible audio session (python.exe) for volume tests
```

## Known limits

* Session choice (automatic mode): a *playing* session wins over a paused one; otherwise the app that played last, then the system's current session. Pausing one app while another plays moves the display to the playing one.
  After a vertical swipe the choice is temporary (MANUAL_TEMP) - see "Media sources" above. Two sessions with the same app id (e.g. two browser profiles reported under one id) appear as one source.
* Chrome / Edge artwork is at most ~150 px (Chromium requests it that way: `kDesiredImageSize = 150`); the companion enlarges it with a restrained edge-only sharpener (`artwork.enhance_upscale`), it cannot add detail.
  YouTube Music "topic" videos are a square cover inside a 16:9 frame, so the real picture can be < 90 px. A real high-resolution source would need a browser extension (see docs/milestones/M8c-session-switching.md); not built.
* A paused browser tab may hold no audio session, so the side-button volume shows "No volume control" until it plays again.
* Position is extrapolated from the session's timeline; apps that report a stale timeline can show a small drift until they update it.
* Some sources have no artwork or no duration (live streams): the display shows a placeholder / an empty bar.
* Plain HTTP on the LAN, unauthenticated (fenced: LAN-only clients, IP-literal Host header, `X-Desk-Client` required on POSTs). A pairing token comes with Milestone 11.

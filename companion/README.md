# DeskMedia Companion (Windows)

Reads the PC's active **media sessions** (Windows Global System Media Transport Controls, `Windows.Media.Control`) and serves them to the
DeskMedia ESP32 display over the USB cable (or, optionally, the local network). It works with anything that publishes to the Windows media overlay: Spotify desktop,
YouTube / YouTube Music (and any site using the browser Media Session API) in Chrome / Edge / Firefox, VLC, Apple Music, ... Per-application
volume comes from the Windows Core Audio session API. No Spotify account, OAuth or internet access is involved.

Design: [../docs/architecture.md](../docs/architecture.md). Wire protocol, pairing and request authentication:
[../docs/protocol.md](../docs/protocol.md). Threat model: [../docs/security.md](../docs/security.md).
Build, test and development workflow: [../docs/development.md](../docs/development.md).

## Run (development)

```powershell
cd companion
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m desk_media_companion --console     # Ctrl+C to stop
```

| Option | Meaning |
|---|---|
| `--lan` | also serve the display over the local network for this run (HTTP server + UDP discovery beacon), even when the LAN setting is off. The default is USB only: with LAN off the companion opens **no** listening socket, so Windows never shows a firewall prompt |
| `--no-usb` | do not talk to the display over USB (development: LAN only, e.g. `--lan --no-usb --port 8790`) |
| `--usb-port COM5` | development: probe only this serial port instead of every USB-Serial/JTAG port (VID 303A / PID 1001); a port only counts as a display after it sent a valid BEACON |
| `--port 8765` | HTTP port of the LAN transport (only used with `--lan` or the LAN setting) |
| `--discovery-port 8766` / `--no-discovery` | LAN transport: UDP beacon port / disable the beacon (then set the PC's address in the firmware's `secrets.local.h`) |
| `--console` / `--quiet` | also log to the console / background mode (file log only, used by auto-start) |
| `--debug` | verbose log (every HTTP request) |
| `--probe` | print the current media state once and exit |
| `--install-startup` / `--remove-startup` | start with Windows (per-user `HKCU\...\Run` entry; no admin rights needed) / undo |
| `--no-tray` / `--open-settings` | no tray icon or settings window (tests, headless runs) / open the Idle Screen settings window at start |
| `--data-dir DIR` | keep the idle-screen data in `DIR\idle`, the pairing store and the app settings (`settings.json`: `usb_enabled`, `lan_enabled`, `usb_auto_pair`) in `DIR` instead of `%LOCALAPPDATA%\DeskMediaCompanion` |
| `--bind ADDR` | local address the API listens on (default `0.0.0.0` = every interface; only private / loopback / link-local clients are served either way and all but this PC must be paired; `127.0.0.1` = this PC only) |
| `--pair-console` | headless pairing: opens the 120 s pairing window at start, prints the code on the console, `y` / `n` on stdin confirms or cancels (no tray needed) |
| `--dev-tools`, `--prefer-app APP`, `--only-apps A,B`, `--ignore-apps A,B`, `--treat-as-spotify APP` | development only: enable the `/debug/*` routes used by `tools/screenshot.py`; always show that app's session; isolate or hide sessions so fake test sessions never reach the display you use; treat a fake session as Spotify to test the shuffle / repeat buttons |

Data locations: log `%LOCALAPPDATA%\DeskMediaCompanion\companion.log` (rotating; it contains track titles, no secrets); idle-screen settings and
pictures `%LOCALAPPDATA%\DeskMediaCompanion\idle\`; the pairing store `pairing.bin` (companion id + key) is per user and protected with Windows DPAPI (`pairing.py`); it is useless on another account or another PC and is never logged.

## Pairing

A display pairs with exactly one companion, once. Open the tray menu, choose **Pair a display...**, compare the six-digit code shown on
the display and on the PC and confirm on both ends. Afterwards every request from the display is signed and the companion refuses
unsigned requests from the network (requests from the PC itself are exempt). **Forget paired display** in the tray menu revokes the
display at once; pairing again replaces it. Over USB the pairing window opens by itself (120 s) when an unpaired display appears and nothing is paired yet
(never when a display is already paired, and not again for 10 minutes after a cancel). Only one copy of the companion runs per Windows session (a second start exits);
a LAN-only development instance (`--lan --no-usb --port N`) is limited to one per port instead. The full procedure and the cryptography are in
[../docs/protocol.md](../docs/protocol.md); the USB transport is [../docs/usb-protocol.md](../docs/usb-protocol.md).

## Build the single-file exe

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\companion\build.ps1     # -> companion\dist\DeskMediaCompanion.exe (about 24 MB, no console window)
```

The exe needs no Python on the target PC. `DeskMediaCompanion.exe --install-startup` makes it start with Windows.

## Windows Firewall

Only relevant when the optional LAN transport is on (`--lan` or the setting); over USB nothing listens and Windows never asks.
The first time the app listens, Windows asks whether to allow it - choose **Allow**. The display connects to **TCP 8765**. If Windows never
asked (or it was dismissed), the display shows "Can't reach your PC". The fix needs one administrator approval, e.g. in an elevated PowerShell:

```powershell
New-NetFirewallRule -DisplayName "DeskMedia Companion" -Direction Inbound -Protocol TCP -LocalPort 8765 -RemoteAddress LocalSubnet -Action Allow
```

(`LocalSubnet` keeps it to your local network.) A one-click helper is future work.

## Artwork, volume, sources (what the companion does beyond relaying the session)

* **Artwork** (`desk_media_companion/artwork.py`): Windows delivers Spotify's thumbnail as a 300x300 RGBA *composite* (the real cover as an opaque
  block, a transparent gap, then the Spotify logo). The companion splits that structurally (alpha bands: exactly one solid block plus a sparse
  logo band; never a fixed crop; ambiguous images are left alone), trims symmetric black letterbox bars, and renders the cover at the size the
  display draws it, keeping its aspect ratio (`/artwork?size=312&maxw=432`). Chromium apps only supply ~150 px thumbnails (a Windows limit),
  so wide covers are soft. `tools/dump_sessions.py` (read-only) shows exactly what each app supplies; `tools/render_art_preview.py` shows what
  the display will receive.
* **Per-application volume** (`desk_media_companion/volume.py`): `GET /volume`, `POST /control/volume-up|volume-down[?step=5]`,
  `POST /volume?level=N` change the **Windows audio-session volume of the displayed app** (the same slider as the Volume Mixer), never the
  master volume (`tools/check_master_volume.py` proves it). The app's audio sessions are found by process name on every playback device.
  Chrome / Edge: all tabs share one audio process, so the whole browser changes; Firefox cannot be matched. Spotify's own in-app volume
  multiplies with the Windows one.
* **Media sources** (`desk_media_companion/sources.py` = the model, `media.py` = the live sessions): the display can switch between every usable
  Windows media session (playing **and paused** ones that have a title or artist; dead sessions are left out) and the synthetic Idle Screen item.
  `POST /control/source-next[?from=Spotify.exe]` / `source-prev` (a vertical swipe on the display) select the next / previous source in a fixed,
  wrapping order (alphabetical by app name, Idle last) and answer with the new `/now-playing`; `GET /sources` lists them;
  `POST /control/source-auto` returns to automatic. **Selection is `AUTO` or `MANUAL_TEMP`**: AUTO follows the playing session that started
  playing most recently; a swipe makes the chosen session a *temporary* manual selection that ends (back to AUTO) when another session starts
  playing, when the chosen session disappears, or - if it is paused while another one plays - after 20 s without touching it
  (`MANUAL_IDLE_RETURN_S`). A background watcher looks at all sessions every 0.4 s. **Every control / volume request may carry
  `&app=<app id>`** (the display always sends it): the command then goes to exactly that session and is refused
  (`"that media session is gone"`) if it no longer exists, never to another one.
* **Spotify shuffle / repeat** (Spotify desktop only, identified by the session's app id): `/now-playing` carries `spotify`, `shuffle`,
  `repeat` (0 off, 1 playlist / album, 2 track), `can_shuffle`, `can_repeat`; `POST /control/shuffle` and `/control/repeat` (also
  `/shuffle-on|off`, `/repeat-off|list|track`) use Windows' own session controls (`try_change_shuffle_active_async`,
  `try_change_auto_repeat_mode_async`) and are refused for every other app. `tools/dump_transport.py` shows what each session reports (read-only),
  `tools/probe_transport.py` proves a session accepts the changes (and restores them).

## Idle screen settings (tray icon)

While the companion runs there is an icon in the Windows notification area (next to the clock; it may be under the `^` arrow). Right-click or
double-click it -> **Settings...** opens the *Idle Screen* window - no command line, no config file to edit:

| Setting | What it does |
|---|---|
| Theme | **Minimal** (near-black), **Dark** (default: soft gradient), **Neon** (violet gradient, accent glow, glowing clock), **Photo** (your picture) |
| Accent colour | seven presets or **Custom...**: the line under the clock, the small connection dot, the Neon glow |
| Photo background | **Choose Image...** (JPG or PNG) with **Fill** (covers the screen), **Fit** (the whole picture, dark bars) or **Blur Fill** (a blurred copy fills the screen, the sharp picture in front). It is resized and prepared here; the display never receives your original. Without a picture the Photo theme shows Dark |
| Show clock / 24-hour | a large digital clock (the PC's local time) |
| Custom line | one line of text under the clock (letters, numbers, basic punctuation; accents are simplified, other scripts dropped, because the display fonts cannot draw them) |
| Show idle screen after | how long nothing must have played before the idle screen appears (2-60 s, default 4 s) |

The window shows a preview (before you press **Apply**); the display picks the change up within a second and keeps it in its own flash, so it
survives a reboot of the display, the companion or Windows. The tray menu also has **Pair a display...**, **Start with Windows** and **Quit**.
`pystray` is in `requirements.txt`; without it the companion still runs, just without the icon.

On the display: tap the idle screen while a (paused) session exists to bring the player back for 30 s; anything that starts playing brings it back at once.

## Test without any media app

```powershell
.\.venv\Scripts\python.exe -m pip install winrt-Windows.Media.Playback winrt-Windows.Media.Core
.\.venv\Scripts\python.exe tools\fake_media_session.py        # silent session: 3 tracks, artwork (incl. a 16:9 one), next/previous/play/pause/seek
curl http://127.0.0.1:8765/now-playing
curl -X POST -H "X-Desk-Client: test" http://127.0.0.1:8765/control/next
.\.venv\Scripts\python.exe tools\make_test_covers.py logs\test-art                       # synthetic covers: square, 16:9, 4:3 letterboxed, banner, Spotify-style composite
.\.venv\Scripts\python.exe tools\fake_media_session.py --art-dir logs\test-art --silent-audio   # + an inaudible audio session (python.exe) for volume tests
```

The unit tests: `.\.venv\Scripts\python.exe -m unittest discover -s tests`. All developer tools are described in
[../docs/development.md](../docs/development.md#board-in-the-loop-and-developer-tools).

## Known limits

* Session choice (automatic mode): a *playing* session wins over a paused one; otherwise the app that played last, then the system's current
  session. Pausing one app while another plays moves the display to the playing one. After a vertical swipe the choice is temporary
  (MANUAL_TEMP, see above). Two sessions with the same app id (e.g. two browser profiles reported under one id) appear as one source.
* Chrome / Edge artwork is at most ~150 px (Chromium requests it that way: `kDesiredImageSize = 150`); the companion enlarges it with a
  restrained edge-only sharpener (`artwork.enhance_upscale`), it cannot add detail. YouTube Music "topic" videos are a square cover inside a 16:9
  frame, so the real picture can be < 90 px. A real high-resolution source would need a browser extension (analysis in
  [M8c](../docs/development-history/M8c-session-switching.md)); not built.
* A paused browser tab may hold no audio session, so the side-button volume shows "No volume control" until it plays again.
* Position is extrapolated from the session's timeline; apps that report a stale timeline can show a small drift until they update it.
* Some sources have no artwork or no duration (live streams): the display shows a placeholder / an empty bar.
* The API is plain HTTP on the local network: authenticated after pairing, but not encrypted ([security.md](../docs/security.md)).

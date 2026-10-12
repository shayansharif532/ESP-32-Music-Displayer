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

## Test without any media app

```powershell
.\.venv\Scripts\python.exe -m pip install winrt-Windows.Media.Playback winrt-Windows.Media.Core
.\.venv\Scripts\python.exe tools\fake_media_session.py        # silent session: 3 tracks, artwork (incl. a 16:9 one), next/previous/play/pause/seek
curl http://127.0.0.1:8765/now-playing
curl -X POST -H "X-Desk-Client: test" http://127.0.0.1:8765/control/next
```

## Known limits

* Session choice: a *playing* session wins over a paused one; otherwise the system's current session. Pausing one app while another plays moves the display to the playing one.
* Position is extrapolated from the session's timeline; apps that report a stale timeline can show a small drift until they update it.
* Some sources have no artwork or no duration (live streams): the display shows a placeholder / an empty bar.
* Plain HTTP on the LAN, unauthenticated (fenced: LAN-only clients, IP-literal Host header, `X-Desk-Client` required on POSTs). A pairing token comes with Milestone 11.

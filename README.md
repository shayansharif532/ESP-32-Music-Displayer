# ESP32 Desk Media Display — "Night City" edition

A birthday-gift desk display for the recipient's **Windows PC**: it shows and controls whatever the PC is playing — Spotify desktop,
YouTube / YouTube Music in a browser, VLC, Apple Music, … (it makes no sound itself) — big square artwork, title, artist / channel,
progress/seek, play-pause-previous-next, and a Cyberpunk-Edgerunners-style idle screen with a clock.

How: a small **Windows companion** ([`companion/`](companion/)) reads the PC's active *media session* and serves it over the home LAN; the
ESP32 finds it automatically and polls it. **No Spotify account, Client ID, Premium, OAuth or tokens** — see
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and ADR-003 in [docs/DECISIONS.md](docs/DECISIONS.md). (The earlier Spotify-Web-API plan is
obsolete; its code is parked in [`legacy/`](legacy/README.md).)

| | |
|---|---|
| **Board** | **Waveshare ESP32-S3-Touch-LCD-4B, SKU 32052** ("Smart 86 Box"): ESP32-S3-WROOM-1-N16R8 (16 MB quad flash, 8 MB octal PSRAM), 4" 480x480 ST7701 RGB panel, GT911 capacitive touch, native USB, BOOT + PWR buttons (no RESET button) |
| **Framework** | **ESP-IDF 5.5.5** (C / FreeRTOS) — not Arduino, see [docs/DECISIONS.md](docs/DECISIONS.md) |
| **UI** | **LVGL 9.5.0** via `espressif/esp_lvgl_port` 2.9.0 |
| **Board support** | Waveshare BSP **`waveshare/esp32_s3_touch_lcd_4b` 2.0.0** (with `esp_lcd_st7701` 2.0.3, `esp_lcd_touch_gt911` 1.2.1) — every version is pinned in [`firmware/dependencies.lock`](firmware/dependencies.lock) |
| **Windows companion** | Python 3.12 + `winrt-*` (Windows.Media.Control) + Pillow + psutil + pycaw/comtypes (per-app volume) — [`companion/`](companion/) (single `.exe` for the recipient: Milestone 12) |
| **Dev host** | Windows 10/11, PowerShell 5.1 (the tested path). Linux/macOS: see the note below (untested, firmware only) |
| **Repository** | <https://github.com/shayansharif532/ESP-32-Spotify> (private) |

> **Status (2026-10-12):** the architecture was changed to the Windows companion (ADR-003) and the **MVP works**: Spotify desktop and YouTube/other
> media in Chrome show real artwork, title, artist/channel and progress on the display, and play/pause/next/previous work from the touchscreen
> (24 real touches, no reset). Evidence and the few open points: [docs/milestones/MVP-media-companion.md](docs/milestones/MVP-media-companion.md).

## Using the display

| Control | What it does |
|---|---|
| tap play / previous / next | transport controls of the session being shown |
| **swipe right / swipe left** on the screen | **next / previous track** (needs >= 72 px of mostly horizontal travel; does nothing if the app has no next / previous) |
| drag on the thin progress line | seek |
| tap the cover | full-screen cover (tap again to return) |
| **upper side button (PWR)** / **lower side button (BOOT)** | **volume up / down of the app being shown** (Windows per-app volume, not the PC's master volume), 5 % per press, hold to repeat (max 3 s); a "Volume 65%" pill shows for a second |

Square covers are shown large and square; widescreen thumbnails (YouTube) as a landscape card. Do not hold the PWR button for more than ~4 seconds: the power chip turns the board off by itself.
Chrome / Edge change the volume of the whole browser (all tabs share one audio session); Spotify changes only Spotify.

## Start here (new computer or new Claude Code session)

1. Read [`CLAUDE.md`](CLAUDE.md) — the authoritative project orientation (hardware, rules, milestone plan, flashing safety).
2. Read this README, [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), then the latest file in [`docs/milestones/`](docs/milestones/).
3. `git status` and `git log --oneline -10`, then continue from the latest *completed* milestone.
4. Never assume the COM port (`tools\find-board.ps1`) or the ESP-IDF location (`tools\idf.ps1` discovers it).

## Where things live

| What | Where |
|---|---|
| Orientation / rules for Claude Code sessions | [`CLAUDE.md`](CLAUDE.md) |
| **Architecture + companion HTTP protocol** | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) |
| **Milestone docs and evidence** (one per milestone) | [`docs/milestones/`](docs/milestones/) — [M1](docs/milestones/M1-lcd-touch.md), [M2](docs/milestones/M2-ui.md), [MVP](docs/milestones/MVP-media-companion.md), [M8](docs/milestones/M8-player-redesign.md), [M8b](docs/milestones/M8b-art-layout-gestures-volume.md) |
| Hardware facts, verified pin map, sources | [`docs/HARDWARE.md`](docs/HARDWARE.md) |
| Decisions: framework (ADR-001), companion architecture (ADR-003), obsolete Spotify rules (ADR-002) | [`docs/DECISIONS.md`](docs/DECISIONS.md) |
| **Windows companion app** | [`companion/`](companion/) |
| Obsolete Spotify client (kept, not built) | [`legacy/`](legacy/README.md) |
| Firmware (ESP-IDF project) and its provenance | [`firmware/`](firmware/), [`firmware/NOTICE.md`](firmware/NOTICE.md) |
| Helper scripts | [`tools/`](tools/): `idf.ps1`, `find-board.ps1`, `serial_capture.py`, `set-dev-secrets.ps1`, `analyze_flash_dump.py`, dev-only `screenshot.py` (real display -> PNG, scripted swipes), `contact_sheet.py`, `soak_gestures.py` |
| Factory-backup manifest (hashes, layout, restore steps) | [`backup/factory/MANIFEST.md`](backup/factory/MANIFEST.md) |
| Waveshare reference downloads (git-ignored, re-fetchable) | `docs/waveshare/` |

## Set up a new computer (Windows)

Prerequisites: Windows 10/11 with `winget` (App Installer), Git and Python 3.12+. PowerShell must be allowed to run local scripts:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` (once) — or prefix script runs with `powershell -NoProfile -ExecutionPolicy Bypass -File`.

```powershell
# 1. Clone (private repo: Git Credential Manager opens a browser sign-in on first use) and look around
git clone https://github.com/shayansharif532/ESP-32-Spotify.git
cd ESP-32-Spotify
git status ; git log --oneline -10

# 2. ESP-IDF 5.5.5 for the ESP32-S3 via Espressif's installer (~8 GB, ~10 min; tools\eim_config.toml records the original options)
winget install --id Espressif.EIM-CLI --exact
#    close and reopen the terminal here so `eim` is on PATH
eim install -i v5.5.5 -t esp32s3 -p C:\esp -n true --do-not-track true --cleanup true --skip-components-download true

# 3. esptool environment — independent of ESP-IDF, used for backup / restore / low-level flashing / serial log capture
python -m venv tools\venv
tools\venv\Scripts\python.exe -m pip install -r tools\requirements.txt

# 4. (optional, ~230 MB, git-ignored) Waveshare's official repo: schematic, pin audit, examples
git clone --depth 1 https://github.com/waveshareteam/ESP32-S3-Touch-LCD-4B docs\waveshare\official-repo

# 5. Developer Wi-Fi credentials (temporary; password typed hidden; written to the git-ignored firmware\secrets.local.h)
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\set-dev-secrets.ps1

# 6. Build (the first build also downloads LVGL, the Waveshare BSP, ... pinned by dependencies.lock; ~7 min, later builds ~20 s)
.\tools\idf.ps1 set-target esp32s3
.\tools\idf.ps1 build
```

Without `secrets.local.h` the firmware still builds and runs a UI-only demo (no network). `tools\idf.ps1` finds the ESP-IDF install through
EIM's registry (override: `$env:IDF_ACTIVATE_SCRIPT`), activates it, runs `idf.py` inside `firmware\` and returns you to your directory — no
special "ESP-IDF shell" needed.

### Flash and watch the board

Plug the USB-C port that is wired to the ESP32-S3 (the one that enumerates as **"USB JTAG/serial debug unit"**, not the UART bridge). Then:

```powershell
$port = .\tools\find-board.ps1                       # prints e.g. COM5 — the number differs per computer
.\tools\idf.ps1 -p $port flash                       # writes bootloader @0x0, partition table @0x8000, app @0x10000; never erases the chip
.\tools\venv\Scripts\python.exe .\tools\serial_capture.py --seconds 20 --reset   # boot log (auto-detects the port; --out FILE to keep it)
```

Flashing safety (full list in [`CLAUDE.md`](CLAUDE.md)): build first; stop anything holding the COM port; never `erase-flash` casually; never
burn eFuses; capture the boot log after every flash. Optional dev self-test: `.\tools\idf.ps1 -DAPP_WIFI_SELFTEST=1 build` drops the Wi-Fi link
once a minute after it comes up and logs how long the automatic reconnect takes.

Notes learned the hard way (this board, native USB port, Windows):
* The console only streams while the host asserts **DTR**; a **RTS rising edge** resets the chip and the DTR level at that edge selects
  download mode (DTR=1) or normal boot (DTR=0). Windows' `usbser.sys` only sends a line change when the *other* line is written too —
  `serial_capture.py` handles all this; never open the port with both DTR and RTS asserted.
* `idf.py monitor` needs an interactive terminal, so use `serial_capture.py`.
* `sdkconfig.defaults` routes the console to the native USB-Serial/JTAG port (the second USB-C port, the UART bridge, is not used).
* After editing `firmware/sdkconfig.defaults`, run `.\tools\idf.ps1 set-target esp32s3` (or delete `firmware\sdkconfig`) so it is re-applied.

### Linux / macOS (untested)

The firmware itself is OS-independent; only the PowerShell wrappers are Windows-specific (and the companion is Windows-only by design).
Install ESP-IDF 5.5.5 following Espressif's "Get Started" for that version, `source export.sh`, then
`cd firmware && idf.py set-target esp32s3 build && idf.py -p /dev/ttyACM0 flash` (`/dev/cu.usbmodem*` on macOS), and run
`python tools/serial_capture.py` from a venv built from `tools/requirements.txt`.

## The Windows companion (PC side)

```powershell
cd companion
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m desk_media_companion --console           # runs until Ctrl+C; log: %LOCALAPPDATA%\DeskMediaCompanion\companion.log
.\.venv\Scripts\python.exe -m desk_media_companion --probe             # print the current media state once and exit
.\.venv\Scripts\python.exe -m desk_media_companion --install-startup   # optional: start with Windows (per user, no admin); --remove-startup undoes it
```

* **Windows Firewall:** the first time it listens, Windows may ask whether to allow it on the network — choose **Allow**. The display connects to
  TCP **8765**; the discovery beacon is UDP 8766 outbound. If the display shows "Can't reach your PC", check this first.
* The PC and the display must be on the same network (same router; "client isolation" must be off).
* No media app running? Test without any: `pip install winrt-Windows.Media.Playback winrt-Windows.Media.Core`, then
  `.\.venv\Scripts\python.exe tools\fake_media_session.py` registers a silent test session (title, artwork, progress, next/previous/play/pause).
* Protocol and design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Quick check from any shell: `curl http://127.0.0.1:8765/now-playing`.

## The factory flash backup is intentionally NOT in GitHub

Before anything was flashed, the board's complete 16 MiB flash (Waveshare's factory firmware) was backed up and verified byte-for-byte.
That image is **deliberately kept out of this repository**: it is a 16 MiB device-specific dump of Waveshare's proprietary firmware that
can contain data from earlier users, and Git history would keep it forever. `.gitignore` blocks `*.bin` and everything in
`backup/factory/` except the manifest. Only the **hashes, partition layout and restore procedure** are committed
([`backup/factory/MANIFEST.md`](backup/factory/MANIFEST.md)).

* The one copy lives on the original development PC: `backup\factory\factory_full_flash_16MB_mac-2884-85a2-4860_20261011-230234.bin`
  (SHA-256 `8b4afd76d598df61cb7fb0db086ffd895a1d1f1b798531e9d46eeb377791c6f5`). **Keep a second copy somewhere safe** (cloud drive / USB stick).
* On a new computer the factory image is **not recoverable from GitHub**. To restore: copy the file back into `backup/factory/`,
  check the SHA-256, then:

```powershell
$port = .\tools\find-board.ps1
.\tools\venv\Scripts\python.exe -m esptool --port $port --flash-mode keep --flash-freq keep --flash-size keep write-flash 0x0 backup\factory\factory_full_flash_16MB_mac-2884-85a2-4860_20261011-230234.bin
```

If the board does not respond: unplug USB-C, hold **BOOT**, plug USB-C back in, release BOOT, run the command again (ROM download mode can not
be overwritten, and we never burn eFuses, so the board can not be bricked by flashing).

## Secrets policy

Nothing secret is ever committed. There are **no Spotify credentials anywhere** in the current design. Wi-Fi credentials: during development
only in the git-ignored `firmware/secrets.local.h`; in the product they live only in the device's NVS (entered through the setup flow,
Milestone 11). Nothing sensitive is printed to the serial log (`serial_capture.py` also masks credential-shaped strings). `.gitignore` blocks
`.env*`, `secrets.*`, `*.local.*`, token/credential files, keys, flash dumps, build output, logs and vendor downloads — only `*.example.*`
templates may be committed. The companion's LAN API is unauthenticated plain HTTP, fenced by LAN-only / Host-header / custom-header rules
(see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)); a pairing token comes with Milestone 13. Before pushing: `git status` and `git diff --cached`.

## First-run setup / recovery / reset

*(designed and documented at Milestone 11; planned flow: device opens a temporary Wi-Fi access point -> phone joins -> captive page picks the
Wi-Fi network and stores it in NVS -> the display finds the PC companion by itself -> Now Playing; a long press of BOOT *while the app is running* (not at power-up: that is the ROM download-mode gesture, and BOOT is the volume-down key during normal use) wipes the
stored Wi-Fi and re-enters setup. Pairing with the companion comes later, Milestone 13.)*

## Milestones (plan revised 2026-10-12 for the companion architecture)

| # | Milestone | State |
|---|---|---|
| 0 | Detect board, confirm hardware, find official docs, choose framework, **back up factory flash** | **done** |
| 1 | Flash an official Waveshare LCD + touch example; confirm display and touch | **done** (the touch-crash finding is addressed by the M2 display config; see the M1 addendum) |
| 2 | Own 480x480 UI (placeholder art, title, artist, progress, prev/play/next, art toggle) | **done** - 24 touches / 941 s, no reset ([M2 doc](docs/milestones/M2-ui.md)); a targeted stress soak is still advisable |
| 3 | Wi-Fi connection + automatic reconnect | **done** - drop recovered in 2.05 s ([MVP doc](docs/milestones/MVP-media-companion.md)) |
| 4 | **Windows companion** (media session -> LAN HTTP API) *(was: HTTPS test)* | **done** - tested with a fake session, YouTube, Spotify desktop, Chrome |
| 5 | **ESP32 <-> companion**: discovery, now-playing, progress *(was: Spotify OAuth)* | **done** on hardware |
| 6 | Real artwork (companion-normalised JPEG) | **done** on hardware (110-177 ms per image) |
| 7 | Play / pause / previous / next (+ seek) through the media session | **done** for play/pause/next/previous (real taps, Spotify + Chrome); seek wired, not yet seen in a log |
| — | **MVP gate**: Spotify desktop + YouTube in a browser, real playback | **passed by log evidence** (owner's visual confirmation of the screen still to be given) |
| 8 | **Main player visual redesign** (big frameless cover, artwork-driven blurred background, clean foreground) | **done** - see [M8 doc](docs/milestones/M8-player-redesign.md) |
| 8b | **Refinement pass**: widescreen art as a landscape card, Spotify logo removed, swipe gestures (right = next, left = previous), per-app Windows volume, side buttons = volume up / down | **done** - see [M8b doc](docs/milestones/M8b-art-layout-gestures-volume.md) |
| 9 | Cyberpunk idle screen (nothing playing) | next |
| 10 | Full-screen album-art polish | |
| 11 | First-run Wi-Fi setup | |
| 12 | Companion installer (single `.exe`, auto-start, firewall helper) | |
| 13 | Pairing / security (token between display and companion) | |
| — | ~~Birthday intro~~ | **dropped by the owner (2026-10-12)** |

## Git workflow

`main` holds the latest known-good, bootable state; risky or unverified work goes on a branch (`feature/mvp` was merged into `main` at the MVP). Commit after each
verified milestone or stable checkpoint — see [`CLAUDE.md`](CLAUDE.md) for the commit and testing rules.

## Repository layout

```
CLAUDE.md         project orientation for Claude Code sessions (authoritative; code + milestone docs override stale details)
backup/factory/   MANIFEST.md only — the 16 MiB factory dump itself is git-ignored on purpose
companion/        Windows companion app (Python): desk_media_companion/, tools/fake_media_session.py, requirements.txt
docs/             ARCHITECTURE.md, HARDWARE.md, DECISIONS.md, milestones/ (per-milestone evidence); waveshare/ = git-ignored downloads
firmware/         the ESP-IDF project: main/, components/{ui,wifi_mgr,pc_media}, sdkconfig.defaults, secrets.example.h
legacy/           obsolete Spotify-Web-API client (kept for reference, not built)
tools/            idf.ps1, find-board.ps1, serial_capture.py, set-dev-secrets.ps1, m1_probe/ (M1 diagnostic sidecar), analyze_flash_dump.py
```

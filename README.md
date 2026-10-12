# ESP32 Spotify Now-Playing Desk Display — "Night City" edition

A birthday-gift desk display: it shows and controls what is playing on the owner's phone / computer / speaker / TV through the
Spotify Web API (it makes no sound itself) — big square album art, title, artist, progress/seek, play-pause-previous-next,
a Cyberpunk-Edgerunners-style idle screen with a clock, a one-time birthday intro, and a first-run Wi-Fi + Spotify setup flow
that needs no PC.

| | |
|---|---|
| **Board** | **Waveshare ESP32-S3-Touch-LCD-4B, SKU 32052** ("Smart 86 Box"): ESP32-S3-WROOM-1-N16R8 (16 MB quad flash, 8 MB octal PSRAM), 4" 480x480 ST7701 RGB panel, GT911 capacitive touch, native USB, BOOT + PWR buttons (no RESET button) |
| **Framework** | **ESP-IDF 5.5.5** (C / FreeRTOS) — not Arduino, see [docs/DECISIONS.md](docs/DECISIONS.md) |
| **UI** | **LVGL 9.5.0** via `espressif/esp_lvgl_port` 2.9.0 |
| **Board support** | Waveshare BSP **`waveshare/esp32_s3_touch_lcd_4b` 2.0.0** (with `esp_lcd_st7701` 2.0.3, `esp_lcd_touch_gt911` 1.2.1) — every version is pinned in [`firmware/dependencies.lock`](firmware/dependencies.lock) |
| **Dev host** | Windows 10/11, PowerShell 5.1 (the tested path). Linux/macOS: see the note in step 6 (untested) |
| **Repository** | <https://github.com/shayansharif532/ESP-32-Spotify> (private) |

> **Status (2026-10-12):** Milestone 0 is done and Milestone 1 is done **with one open issue**. The board runs Waveshare's official
> LVGL demo plus a small diagnostic sidecar. The owner confirmed the display renders correctly and the serial log shows touch input
> working, **but a touch test crashed the board 4 times (taps in the demo's top tab-bar area)** — see the *Touch-test addendum* in
> [docs/milestones/M1-lcd-touch.md](docs/milestones/M1-lcd-touch.md). It is the first thing to resolve in Milestone 2.
> Milestone 2 has not started. Nothing Spotify-related exists yet.

## Start here (new computer or new Claude Code session)

1. Read [`CLAUDE.md`](CLAUDE.md) — the authoritative project orientation (hardware, rules, milestone plan, flashing safety).
2. Read this README, then the latest file in [`docs/milestones/`](docs/milestones/).
3. `git status` and `git log --oneline -10`, then continue from the latest *completed* milestone.
4. Never assume the COM port (`tools\find-board.ps1`) or the ESP-IDF location (`tools\idf.ps1` discovers it).

## Where things live

| What | Where |
|---|---|
| Orientation / rules for Claude Code sessions | [`CLAUDE.md`](CLAUDE.md) |
| **Milestone docs and evidence** (one per milestone) | [`docs/milestones/`](docs/milestones/) — [M1-lcd-touch.md](docs/milestones/M1-lcd-touch.md) |
| Hardware facts, verified pin map, sources | [`docs/HARDWARE.md`](docs/HARDWARE.md) |
| Decisions: framework choice, Spotify rules | [`docs/DECISIONS.md`](docs/DECISIONS.md) |
| Firmware (ESP-IDF project) and its provenance | [`firmware/`](firmware/), [`firmware/NOTICE.md`](firmware/NOTICE.md) |
| Helper scripts | [`tools/`](tools/): `idf.ps1`, `find-board.ps1`, `serial_capture.py`, `analyze_flash_dump.py` |
| Factory-backup manifest (hashes, layout, restore steps) | [`backup/factory/MANIFEST.md`](backup/factory/MANIFEST.md) |
| Waveshare reference downloads (git-ignored, re-fetchable) | `docs/waveshare/` |

## Set up a new computer (Windows)

Prerequisites: Windows 10/11 with `winget` (App Installer), Git and Python 3.12+. PowerShell must be allowed to run local scripts:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` (once) — or prefix commands with `powershell -ExecutionPolicy Bypass -File`.

```powershell
# 1. Clone (private repo: Git Credential Manager opens a browser sign-in on first use) and look around
git clone https://github.com/shayansharif532/ESP-32-Spotify.git
cd ESP-32-Spotify
git status ; git log --oneline -10

# 2. ESP-IDF 5.5.5 for the ESP32-S3 via Espressif's installer (~8 GB, ~10 min; tools\eim_config.toml records the original options)
winget install --id Espressif.EIM-CLI --exact
#    close and reopen the terminal here so `eim` is on PATH
eim install -i v5.5.5 -t esp32s3 -p C:\esp -n true --do-not-track true --cleanup true --skip-components-download true

# 3. esptool environment — independent of ESP-IDF, used for backup / restore / low-level flashing
python -m venv tools\venv
tools\venv\Scripts\python.exe -m pip install -r tools\requirements.txt

# 4. (optional, ~230 MB, git-ignored) Waveshare's official repo: schematic, pin audit, examples
git clone --depth 1 https://github.com/waveshareteam/ESP32-S3-Touch-LCD-4B docs\waveshare\official-repo

# 5. Build (the first build also downloads LVGL, the Waveshare BSP, ... pinned by dependencies.lock; ~7 min, later builds ~20 s)
.\tools\idf.ps1 set-target esp32s3
.\tools\idf.ps1 build
```

`tools\idf.ps1` finds the ESP-IDF install through EIM's registry (override: `$env:IDF_ACTIVATE_SCRIPT`), activates it, runs
`idf.py` inside `firmware\` and returns you to your directory — no special "ESP-IDF shell" needed.

### Flash and watch the board

Plug the USB-C port that is wired to the ESP32-S3 (the one that enumerates as **"USB JTAG/serial debug unit"**, not the UART bridge). Then:

```powershell
$port = .\tools\find-board.ps1                       # prints e.g. COM5 — the number differs per computer
.\tools\idf.ps1 -p $port flash                       # writes bootloader @0x0, partition table @0x8000, app @0x10000; never erases the chip
.\tools\venv\Scripts\python.exe .\tools\serial_capture.py --seconds 20 --reset   # boot log (auto-detects the port; --out FILE to keep it)
```

Flashing safety (full list in [`CLAUDE.md`](CLAUDE.md)): build first; stop anything holding the COM port; never `erase-flash` casually; never
burn eFuses; capture the boot log after every flash. Milestone 1 only: `.\tools\idf.ps1 -DM1_PROBE=0 build` builds Waveshare's demo without the diagnostic sidecar.

Notes learned the hard way (this board, native USB port, Windows):
* The console only streams while the host asserts **DTR**; a **RTS rising edge** resets the chip and the DTR level at that edge selects
  download mode (DTR=1) or normal boot (DTR=0). Windows' `usbser.sys` only sends a line change when the *other* line is written too —
  `serial_capture.py` handles all this; never open the port with both DTR and RTS asserted.
* `idf.py monitor` needs an interactive terminal, so use `serial_capture.py`.
* `sdkconfig.defaults` routes the console to the native USB-Serial/JTAG port (the second USB-C port, the UART bridge, is not used).

### Linux / macOS (untested)

The firmware itself is OS-independent; only the PowerShell wrappers are Windows-specific. Install ESP-IDF 5.5.5 following Espressif's
"Get Started" for that version, `source export.sh`, then `cd firmware && idf.py set-target esp32s3 build && idf.py -p /dev/ttyACM0 flash`
(`/dev/cu.usbmodem*` on macOS), and run `python tools/serial_capture.py` from a venv built from `tools/requirements.txt`.

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

Nothing secret is ever committed. Wi-Fi credentials and Spotify tokens live only in the device's NVS (entered through the on-device setup flow);
the Spotify Client ID is entered at setup time too; nothing sensitive is printed to the serial log (`serial_capture.py` also masks
credential-shaped strings). `.gitignore` blocks `.env*`, `secrets.*`, `*.local.*` (e.g. `secrets.local.h`), token/credential files, keys, flash
dumps, build output, logs and vendor downloads — only `*.example.*` templates may be committed. Before pushing: `git status` and `git diff --cached`.

## Spotify developer setup (rules as of Feb 2026 — details in [docs/DECISIONS.md](docs/DECISIONS.md))

1. Someone with **Spotify Premium** creates an app at <https://developer.spotify.com/dashboard> (Development Mode apps need a
   **Premium app owner**, are limited to **5 users per app**, and the owner's Premium must stay active; a developer account can
   own up to 25 Client IDs since Spotify's July 2026 update).
2. Select **Web API**; add the redirect URI the firmware shows on its setup page (HTTPS, or `http://127.0.0.1…`; no `localhost`,
   no plain-HTTP LAN addresses). Authorization Code with **PKCE** only — never Implicit Grant.
3. Under **User Management** add the Spotify account (name + e-mail) of whoever will use the display.
4. Copy the **Client ID** into the device's setup page. No client secret is needed, and nothing is hard-coded in the firmware or this repo.
5. The listener needs **Premium** for play/pause/next/previous/seek; with a free account the display still shows what is playing
   and disables the controls.

*(exact screens and redirect-page details are finalised at Milestones 5 and 11)*

## First-run setup / recovery / reset

*(designed and documented at Milestone 11; planned flow: device opens a temporary Wi-Fi access point -> phone joins -> captive page
picks the Wi-Fi network -> device shows a QR code -> phone authorises Spotify -> credentials stored in NVS; a long-press gesture on the
idle screen / holding BOOT at power-up wipes Wi-Fi + Spotify settings and re-enters setup.)*

## Milestones

| # | Milestone | State |
|---|---|---|
| 0 | Detect board, confirm hardware, find official docs, choose framework, **back up factory flash** | **done** |
| 1 | Flash an official/simple Waveshare LCD + touch example; confirm display and touch | **done, one open issue**: display confirmed by the owner, touch events work, but taps in the demo's top tab bar crashed the board (4 resets) — see the M1 addendum; fix + soak test come first in Milestone 2 |
| 2 | Own 480x480 UI (non-Spotify prototype), touch buttons verified | next |
| 3 | Wi-Fi connection | |
| 4 | HTTPS / API requests | |
| 5 | Spotify OAuth (PKCE) + currently-playing metadata | |
| 6 | Album art | |
| 7 | Play / pause / previous / next (+ seek) | |
| 8 | Polished UI | |
| 9 | Cyberpunk idle screen | |
| 10 | Birthday intro | |
| 11 | First-run Wi-Fi + Spotify setup | |

## Git workflow

`main` holds the latest known-good, bootable state; risky experiments go on a branch (or behind a checkpoint commit). Commit after each verified
milestone or stable checkpoint — see [`CLAUDE.md`](CLAUDE.md) for the commit and testing rules.

## Repository layout

```
CLAUDE.md         project orientation for Claude Code sessions (authoritative; code + milestone docs override stale details)
backup/factory/   MANIFEST.md only — the 16 MiB factory dump itself is git-ignored on purpose
docs/             HARDWARE.md, DECISIONS.md, milestones/ (per-milestone evidence); waveshare/ = git-ignored downloads
firmware/         the ESP-IDF project (currently Waveshare's official LVGL demo + components/m1_probe; see firmware/NOTICE.md)
tools/            idf.ps1, find-board.ps1, serial_capture.py, analyze_flash_dump.py, requirements.txt, eim_config.toml
```

Milestone 2 turns `firmware/` into our own app: `components/{board,display,touch,ui,wifi,spotify,storage}` + `screens/{now_playing,idle,setup,birthday}`.

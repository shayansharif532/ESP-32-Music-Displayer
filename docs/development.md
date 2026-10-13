# Development guide

How to set up a machine, build and flash the firmware, run and package the Windows companion, run the tests and troubleshoot.
For the design see [architecture.md](architecture.md); for the board see [hardware.md](hardware.md).

Contents: [Prerequisites](#prerequisites) · [Set up a new machine](#set-up-a-new-machine) · [Build and flash the firmware](#build-and-flash-the-firmware) ·
[Serial console on the native USB port](#serial-console-on-the-native-usb-port) · [Run the companion](#run-the-windows-companion) ·
[Package the companion](#package-the-companion-as-a-single-exe) · [Pair the display](#pair-the-display) · [Unit tests](#unit-tests) ·
[Board-in-the-loop test tools](#board-in-the-loop-and-developer-tools) · [Repository layout](#repository-layout) · [Conventions](#conventions) ·
[Secrets](#secrets) · [Troubleshooting](#troubleshooting)

## Prerequisites

| Need | Notes |
|---|---|
| Windows 10 / 11 | The companion is Windows-only by design (it uses the Windows media-session and Core Audio APIs). The PowerShell scripts are also Windows-specific; the firmware itself is OS-independent, see [Linux / macOS](#linux--macos-firmware-only-untested) |
| PowerShell 5.1 | The tested shell. Allow local scripts once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, or prefix script runs with `powershell -NoProfile -ExecutionPolicy Bypass -File` |
| `winget` (App Installer), Git, Python 3.12+ | `winget` installs Espressif's installer; Python runs the companion and the tools |
| ESP-IDF **5.5.5** for the ESP32-S3 | Installed with Espressif's `eim` (about 8 GB, about 10 minutes); `tools\eim_config.toml` records the original options |
| The reference board | Waveshare ESP32-S3-Touch-LCD-4B ([hardware.md](hardware.md)) and a USB-C cable (data, not charge-only) |
| A 2.4 GHz Wi-Fi network | The ESP32-S3 has no 5 GHz radio. The PC and the display must be on the same network, and the router's "client isolation" must be off |

## Set up a new machine

```powershell
# 1. Clone and look around (use the URL of this repository)
git clone <repo-url> deskmedia-display
cd deskmedia-display
git status ; git log --oneline -10

# 2. ESP-IDF 5.5.5 for the ESP32-S3 through Espressif's installer
winget install --id Espressif.EIM-CLI --exact
#    close and reopen the terminal here so `eim` is on PATH
eim install -i v5.5.5 -t esp32s3 -p C:\esp -n true --do-not-track true --cleanup true --skip-components-download true

# 3. esptool / pyserial environment, independent of ESP-IDF: backup, restore, low-level flashing, serial log capture
python -m venv tools\venv
tools\venv\Scripts\python.exe -m pip install -r tools\requirements.txt

# 4. Companion environment (also used by the tests and several tools)
python -m venv companion\.venv
companion\.venv\Scripts\python.exe -m pip install -r companion\requirements.txt

# 5. (optional, ~230 MB, git-ignored) Waveshare's official repository: schematic, pin audit, examples
git clone --depth 1 https://github.com/waveshareteam/ESP32-S3-Touch-LCD-4B docs\waveshare\official-repo

# 6. Developer Wi-Fi credentials (typed hidden, written to the git-ignored firmware\secrets.local.h)
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\set-dev-secrets.ps1
```

Before the first flash of a board, **make your own factory flash backup** (the procedure is in
[development-history/M0-factory-backup.md](development-history/M0-factory-backup.md); `backup/` is where it goes and `*.bin` is git-ignored).

## Build and flash the firmware

`tools\idf.ps1` finds the ESP-IDF install through EIM's registry (override: `$env:IDF_ACTIVATE_SCRIPT`), activates it, runs `idf.py` inside
`firmware\` and returns you to your directory. No special "ESP-IDF shell" is needed.

```powershell
.\tools\idf.ps1 set-target esp32s3      # once; also after every edit of firmware\sdkconfig.defaults
.\tools\idf.ps1 build                   # first build ~7 min (downloads LVGL, the Waveshare BSP, ... pinned by dependencies.lock); later builds ~20 s
```

Without `firmware\secrets.local.h` the firmware still builds and runs a UI-only demo (no network).

Plug in the USB-C port that is wired to the ESP32-S3 (it enumerates as "USB JTAG/serial debug unit", **not** the UART bridge), then:

```powershell
$port = .\tools\find-board.ps1                                   # prints e.g. COM5; the number differs per computer
.\tools\idf.ps1 -p $port flash                                   # bootloader @0x0, partition table @0x8000, app @0x10000; never erases the chip
.\tools\venv\Scripts\python.exe .\tools\serial_capture.py --seconds 20 --reset     # boot log; --out FILE keeps it
```

Build options (CMake cache variables):

| Option | Effect |
|---|---|
| `-DAPP_DEV_TOOLS=1` | Dev-only: screenshot dump and simulated touches / key presses for the test tools. **Never ship a build with it** |
| `-DAPP_WIFI_SELFTEST=1` | Dev self-test: drops the Wi-Fi link once a minute after it comes up and logs how long the automatic reconnect takes |

Example: `.\tools\idf.ps1 -DAPP_DEV_TOOLS=1 build`.

### Flashing safety

1. Confirm the target (`esp32s3`) and the serial device (`tools\find-board.ps1`; never assume a COM number).
2. Stop anything that holds the COM port; build successfully first.
3. Never `erase-flash` casually; never burn eFuses; before changing the partition table document the old and new layouts.
4. Capture the boot log after every flash.
5. If flashing fails, do not erase repeatedly: check USB mode, BOOT behaviour, port ownership, esptool output and boot logs first.
   The ROM download mode cannot be overwritten, so a board is not bricked by flashing.
6. Download mode by hand: unplug USB-C, hold **BOOT**, plug USB-C back in, release BOOT.

Do not hold the upper side button (PWR) for more than about 4 seconds: the power chip turns the board off by itself. At power-up BOOT is the
ROM download-mode gesture; while the app runs it is the volume-down key.

### Serial console on the native USB port

Notes learned the hard way (this board, native USB port, Windows):

* The console only streams while the host asserts **DTR**. A **RTS rising edge** resets the chip, and the DTR level at that edge selects
  download mode (DTR=1) or normal boot (DTR=0). Windows' `usbser.sys` only sends a line change when the *other* line is written too.
  `tools\serial_capture.py` handles all this; never open the port with both DTR and RTS asserted.
* `idf.py monitor` needs an interactive terminal, so use `serial_capture.py`.
* `sdkconfig.defaults` routes the console to the native USB-Serial/JTAG port; the second USB-C port (the UART bridge) is not used.
* After editing `firmware\sdkconfig.defaults` run `.\tools\idf.ps1 set-target esp32s3` (or delete `firmware\sdkconfig`) so it is re-applied.
* With no serial reader attached for a long time the console output can stop until the next reset; the firmware keeps running. Keep
  `serial_capture.py` attached (with `--reset`) when you need logs.

### Linux / macOS (firmware only, untested)

Install ESP-IDF 5.5.5 following Espressif's "Get Started" for that version, `source export.sh`, then
`cd firmware && idf.py set-target esp32s3 build && idf.py -p /dev/ttyACM0 flash` (`/dev/cu.usbmodem*` on macOS), and run
`python tools/serial_capture.py` from a venv built from `tools/requirements.txt`. The companion cannot run there.

## Run the Windows companion

```powershell
cd companion
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m desk_media_companion --console           # runs until Ctrl+C; log: %LOCALAPPDATA%\DeskMediaCompanion\companion.log
.\.venv\Scripts\python.exe -m desk_media_companion --probe             # print the current media state once and exit
.\.venv\Scripts\python.exe -m desk_media_companion --install-startup   # optional: start with Windows (per user, no admin); --remove-startup undoes it
```

All options and the data locations are in [../companion/README.md](../companion/README.md). Quick check from any shell:
`curl http://127.0.0.1:8765/now-playing` (loopback is exempt from request signing, see [protocol.md](protocol.md)).

* **Windows Firewall:** the first time it listens, Windows may ask whether to allow it on the network; choose **Allow**. The display connects
  to TCP **8765**; the discovery beacon is UDP **8766**. If the display shows "Can't reach your PC", check this first.
* No media app running? `companion\tools\fake_media_session.py` registers a silent test session; see the companion README.

## Package the companion as a single exe

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\companion\build.ps1     # -> companion\dist\DeskMediaCompanion.exe (about 24 MB, no console window)
```

The script creates `companion\.venv` if it is missing and uses PyInstaller (`--onefile --noconsole`, with the WinRT, pycaw, comtypes and
pystray hidden imports). The exe needs no Python on the target PC and includes the tray icon and the Settings window.
`DeskMediaCompanion.exe --install-startup` makes it start with Windows. `dist/` and `*.spec` are git-ignored.

## Pair the display

Pairing happens once per display / PC pair, with a human on both ends ([protocol.md](protocol.md) section 3):

1. Start the companion and power the display (it finds the PC by itself).
2. On the PC open the tray menu and choose **Pair a display...** (the window stays open for 120 s).
3. Compare the six-digit code shown on the display and on the PC and confirm on both ends.

The threat model and what pairing does and does not protect are in [security.md](security.md).

## Unit tests

The pure logic (selection state machine, artwork pipeline, idle-screen picture processing, HTTP routing, pairing / request-signing test
vectors) is covered by `unittest` suites in `companion/tests/`:

```powershell
cd companion
.\.venv\Scripts\python.exe -m unittest discover -s tests          # or, from the repository root: companion\.venv\Scripts\python.exe -m unittest discover -s companion\tests
```

The model tests (`test_sources.py`) run on scripted timelines without a clock or any Windows API.

## Board-in-the-loop and developer tools

Scripts in `tools/` (run with `tools\venv\Scripts\python.exe` unless noted; the ones that start their own companion need `companion\.venv`).

| Tool | What it does | Needs |
|---|---|---|
| `idf.ps1` | Runs `idf.py` in the ESP-IDF 5.5.5 environment from anywhere (`$env:IDF_RUN` runs another command in that environment) | ESP-IDF installed through `eim` |
| `find-board.ps1` | Prints the COM port of the board (VID 303A, PID 1001); read-only, never opens the port | the board on USB |
| `set-dev-secrets.ps1` | Writes the git-ignored `firmware\secrets.local.h` (Wi-Fi name, password typed hidden, optional fixed PC address) | PowerShell |
| `serial_capture.py` | Passive capture of the native USB console into a log (`--seconds`, `--out`, `--reset`); masks credential-shaped strings | the board |
| `analyze_flash_dump.py` | Read-only analysis of a full flash dump: partition layout and firmware identity; never prints NVS values | a dump |
| `screenshot.py` | Captures what the display really shows as a PNG (the board renders its LVGL screen to RAM and sends it over the console, CRC-checked); `--tap art`, `--do <gesture>` scripted virtual-finger gestures or simulated key presses | dev firmware (`-DAPP_DEV_TOOLS=1`), companion with `--dev-tools` |
| `session_test.py` | Hardware-in-the-loop test of gestures, source switching, auto-follow, Spotify buttons and per-app volume; judged from the companion's log and `/sources` | dev firmware + companion `--dev-tools`; starts its own silent fake session |
| `idle_test.py` | Hardware-in-the-loop test of the idle screen (transitions, delay, picture persistence across reboots, fit modes, the clock, corrupt pictures, repeated idle <-> playback soak, the source carousel). Runs its own companion on port 8765 | dev firmware; stop the normal companion first |
| `soak_gestures.py` + `analyze_soak.py` | Scripted random swipes, taps and key presses for a few minutes while a serial log is captured; the analyzer reports resets, panics and heap / stack drift | dev firmware, silent fake session |
| `pairing_test.py` | Hardware-in-the-loop test of pairing and request authentication over the real LAN: unpaired clients refused, the code prompt, Cancel / Pair taps, replay / tamper / wrong-key refusals, forget and re-pair, reboot persistence, a rogue beacon and a rogue server. Runs its own companion on port 8765 and **un-pairs / re-pairs the board** (it ends paired) | dev firmware; stop the normal companion first |
| `demo_capture.py` | Regenerates `docs/screenshots/demo.gif` and `pairing-prompt.png` from the board with two silent fake sessions (synthetic covers, neutral names, a made-up PC name). Un-pairs and re-pairs the board | dev firmware; stop the normal companion first; `logs/test-art2` from `companion/tools/make_test_covers.py` |
| `auto_follow_test.py` | Isolated end-to-end test of AUTO / MANUAL_TEMP selection and the Spotify shuffle / repeat controls (no board): its own companion sees only two silent fake sessions | `companion\.venv` |
| `idle_source_test.py` | Isolated API-level test of the idle screen as a ring item (no board) | `companion\.venv` |
| `real_cases.py` | The auto-follow cases with *real* Spotify and browser sessions. It presses play / pause on the real apps, so it refuses to start unless both are paused, mutes both through their own Windows volume and restores everything | real sessions; run only when nobody is listening |
| `evidence_sources.py`, `evidence_spotify.py`, `contact_sheet.py` | Capture and tile the documentation screenshots (synthetic covers only) | dev firmware |
| `gen_clock_font.py` | Regenerates the idle screen's large clock font (`firmware/components/idle_screen/font_clock.c`) | Pillow, the Montserrat TTF from LVGL's repository |
| `m1_probe/` | The Milestone 1 diagnostic sidecar (touch trace, heap / redraw stats); historical | see [M1](development-history/M1-lcd-touch.md) |

Developer tools in `companion/tools/` (run with `companion\.venv\Scripts\python.exe`):

| Tool | What it does |
|---|---|
| `fake_media_session.py` | Registers a silent *real* Windows media session (title, artist, artwork, timeline, controls) so everything can be tested without a media app; `--art-dir`, `--silent-audio` (adds an inaudible audio session for volume tests), `--paused`. Needs `pip install winrt-Windows.Media.Playback winrt-Windows.Media.Core` |
| `fakes.py` | Starts up to two silent fake sessions (`python.exe` and a renamed copy of the interpreter, because Windows names a session after its executable) |
| `make_test_covers.py` | Writes synthetic covers (square, 16:9, 4:3 with bars, banner, Spotify-style composite) |
| `dump_sessions.py`, `dump_transport.py` | Read-only: what Windows really supplies per session (metadata, the raw thumbnail, shuffle / repeat state, enabled controls) |
| `probe_transport.py` | Proves a session accepts shuffle / repeat changes through Windows and restores them |
| `api_check.py` | Exercises source switching over HTTP without touching playback (use a separate companion instance, `--port 8790 --no-discovery`) |
| `check_master_volume.py` | Proves the per-app volume calls leave the master volume and every other app alone |
| `art_benchmark.py`, `art_compare.py`, `analyze_art.py`, `render_art_preview.py`, `side_by_side.py` | Artwork pipeline analysis and before / after comparison tools |

### Safety rules for the test tools

* **Never pin, flash or send commands while someone is using the display by hand**: their key presses and swipes would hit the wrong app.
* Tests that press controls only run while the silent fake session is the one on screen; the display's `--tap play|next|prev` is refused unless the
  active session is the fake one. Only `swipe_up` / `swipe_down` (they just change *which* session is shown) are allowed on real sessions.
* End every test with `POST /control/source-auto` so the display is not left on a manual selection.
* While developing, run the product companion with `--ignore-apps python.exe,deskfakeb.exe` so fake sessions never reach the display you use.
* Fake sessions are silent. The tools that touch real sessions restore what they change (net-zero volume steps, play states put back).
* Never ship a firmware built with `APP_DEV_TOOLS`.

## Repository layout

```
README.md             landing page
PORTFOLIO.md          short project summary for CVs / portfolios
CLAUDE.md             orientation for contributors and AI assistants (rules, pitfalls)
docs/                 architecture.md, protocol.md, security.md, hardware.md, development.md, decisions/ (ADRs),
                      development-history/ (engineering logs per milestone), screenshots/
companion/            Windows companion (Python): desk_media_companion/, tests/, tools/, build.ps1
firmware/             the ESP-IDF project: main/, components/{ui,wifi_mgr,pc_media,idle_screen,side_keys}, partitions.csv, sdkconfig.defaults, secrets.example.h
tools/                PowerShell and Python helpers: idf.ps1, find-board.ps1, serial_capture.py, board-in-the-loop tests, ...
backup/               put your own factory flash backup here (git-ignored *.bin)
```

## Conventions

* **Branches:** `main` holds the latest known-good, bootable state; risky or unverified work goes on a feature branch.
* **Commits:** one verified step per commit, never a giant "everything" commit. Prefixes in use: `feat:`, `fix:`, `docs:`, `chore:`, `milestone N:`.
  Before major changes run `git status` and `git log --oneline -10`; before pushing run `git status` and `git diff --cached`.
* **Evidence:** a milestone is documented in [development-history/](development-history/README.md) with what was tested and how. Say
  "hardware-tested" for checks driven by the board's virtual finger and the test tools, and "verified by hand" only for what a person did
  on the real glass.
* **Design decisions** get an ADR in [decisions/](decisions/README.md).
* **Firmware rules:** keep LVGL work on the LVGL task; never block it; reuse buffers and use PSRAM intentionally; poll the companion about once
  a second; measure heap and stack headroom when changing anything big. Do not add full-screen LVGL shadows, blur or overlays (they redraw on every progress tick).

## Secrets

Nothing secret is ever committed. There are no Spotify credentials anywhere. Wi-Fi credentials exist only in the git-ignored
`firmware/secrets.local.h` (development) and in RAM at run time. The pairing key is stored in the display's NVS and the companion's per-user
data, never in the repository or the logs. `.gitignore` blocks `.env*`, `secrets.*`, `*.local.*`, token and credential files, keys, flash
dumps (`*.bin`), build output, logs and vendor downloads; only `*.example.*` templates may be committed. Committed images are synthetic
(generated covers, mock sessions, UI chrome), never real artwork or personal screenshots. Personal idle-screen customisation (settings and
photo) lives in `%LOCALAPPDATA%\DeskMediaCompanion\idle` and is also blocked by `.gitignore` should it ever land in a checkout.

## Troubleshooting

| Symptom | Check |
|---|---|
| The display says "Can't reach your PC" | PC on and not asleep; companion running (tray icon); Windows Firewall allowed TCP 8765 (see [../companion/README.md](../companion/README.md#windows-firewall)); same network and no client isolation; `curl http://127.0.0.1:8765/health` on the PC |
| The display says "Looking for your PC" | The beacon (UDP 8766) is not arriving: other VLAN / guest network, VPN adapter being the only interface, firewall. Pin the address in `firmware\secrets.local.h` (`DEV_COMPANION_HOST`) as a workaround |
| The display shows a pairing prompt, or says it is not paired | Open **Pair a display...** in the companion's tray menu and confirm the code on both ends ([protocol.md](protocol.md), [security.md](security.md)) |
| Nothing on the screen after a flash | Capture the boot log with `serial_capture.py --reset`; check that Wi-Fi credentials were built in (without them the UI-only demo runs) |
| `find-board.ps1` finds nothing | Use the USB-C port wired to the ESP32-S3 (the one that is not the UART bridge); try another cable; Device Manager > Ports |
| Flashing fails or the board does not respond | Close anything holding the port; use the manual download mode (hold BOOT while plugging in); then retry. Do not erase repeatedly |
| No serial output | The native USB console needs DTR; use `serial_capture.py`, not `idf.py monitor` |
| `idf.ps1` says ESP-IDF is not installed | Install it with `eim` (see above) or set `$env:IDF_ACTIVATE_SCRIPT` |
| Side-button volume shows "No volume control" | The shown app has no audio session yet (paused browser tab, nothing played). Firefox cannot be matched |
| A browser cover is soft | A Windows / Chromium limit: browsers publish ~150 px thumbnails ([M8c](development-history/M8c-session-switching.md)) |
| Titles with accents, CJK, Cyrillic or emoji show boxes | The UI fonts are ASCII Montserrat; a fallback font is future work |

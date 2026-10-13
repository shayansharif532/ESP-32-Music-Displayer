# CLAUDE.md

Orientation for contributors and AI coding assistants working on **DeskMedia Display**. Current source code and the milestone logs override
anything stale here. The project overview is in [README.md](README.md); the long milestone narrative that used to live in this file is in
[docs/development-history/project-plan.md](docs/development-history/project-plan.md).

## What this project is

A standalone ESP32-S3 touchscreen that shows and controls whatever a **Windows PC** is playing. A Windows companion (Python) reads the
Windows media sessions (GSMTC) and per-application audio sessions (Core Audio) and serves a local protocol (HTTP on 8765, UDP beacon on 8766).
The ESP32 firmware (ESP-IDF 5.5.5, LVGL 9.5, C) discovers the PC, polls it about once a second, draws the 480x480 UI and sends commands back.
The device makes no sound. No Spotify account, OAuth or Premium is used: Spotify desktop, browsers and any app that publishes a Windows
media session are all just sessions.

It is a **generic, reusable product**. Keep it that way: no personal names, photos, themes, networks or addresses in the repository, no
one-off or person-specific features, no Spotify-only assumptions (Spotify shuffle / repeat are the one deliberate Spotify-only feature, decided by
the session's app id, never by metadata).

## Facts to keep straight

* Board (the only supported one): **Waveshare ESP32-S3-Touch-LCD-4B**, SKU 32052: ESP32-S3-WROOM-1-N16R8 (16 MB flash, 8 MB octal PSRAM),
  4" 480x480 ST7701 RGB panel, GT911 touch, native USB, BOOT (GPIO0) and PWR (AXP2101, polled over I2C) buttons used as volume keys.
  Never substitute settings from a similar Waveshare board without checking them against this one ([docs/hardware.md](docs/hardware.md)).
* Toolchain: ESP-IDF **5.5.5** (installed with `eim`), LVGL **9.5.0**, Waveshare BSP 2.0.0, esptool 5.5.0 in `tools/venv`. ESP-IDF, not
  Arduino ([ADR-001](docs/decisions/ADR-001-esp-idf-and-lvgl.md)); do not migrate without a documented reason.
* Companion: Python 3.12, `winrt-*`, pycaw / comtypes, Pillow, psutil, pystray; packaged with PyInstaller (`companion/build.ps1`).
* Protocol, pairing and request authentication: [docs/protocol.md](docs/protocol.md); threat model: [docs/security.md](docs/security.md).
* Design and diagrams: [docs/architecture.md](docs/architecture.md); decisions: [docs/decisions/](docs/decisions/README.md); build, flash and
  test tools: [docs/development.md](docs/development.md).

## Repository layout

```
companion/        Windows companion: desk_media_companion/ (media.py, sources.py, artwork.py, volume.py, server.py, discovery.py, idle.py, ui.py,
                  pairing.py, auth.py, ...), tests/, tools/, build.ps1
firmware/         ESP-IDF project: main/ (glue, dev_tools.c), components/{ui, wifi_mgr, pc_media, idle_screen, side_keys}, partitions.csv, sdkconfig.defaults
tools/            idf.ps1, find-board.ps1, set-dev-secrets.ps1, serial_capture.py, screenshot.py and the board-in-the-loop tests
docs/             architecture, protocol, security, hardware, development, decisions/, development-history/, screenshots/
backup/           your own factory flash backup goes here (*.bin is git-ignored)
```

## Rules: building and flashing

1. Never assume the serial port or the ESP-IDF location: `tools\find-board.ps1` prints the port, `tools\idf.ps1` finds ESP-IDF.
2. Before flashing: confirm the target (`esp32s3`) and the device, stop anything holding the port, build successfully first, inspect the
   partition impact, capture the serial log after the reboot.
3. Never `erase-flash` casually, never burn eFuses, never change the partition table without documenting the old and new layout
   (the current one is `firmware/partitions.csv`; `nvs` and `factory` offsets are kept so stored data survives a flash).
4. If flashing fails, do not erase repeatedly: diagnose USB mode, BOOT behaviour, port ownership, esptool output and the boot log. The ROM
   download mode cannot be overwritten, so flashing cannot brick the board. Tell the user exactly what to press and when.
5. After editing `firmware/sdkconfig.defaults` run `tools\idf.ps1 set-target esp32s3` (or delete `firmware\sdkconfig`).
6. **Factory backup:** make a verified full-flash backup of a board before its first flash; never commit it (`*.bin` is git-ignored); keep a
   second copy outside the repository ([docs/development-history/M0-factory-backup.md](docs/development-history/M0-factory-backup.md)).
7. Never ship a firmware built with `-DAPP_DEV_TOOLS=1`.

## Rules: secrets and publication

* Treat the repository as public (it is meant to be published): everything committed, and the whole history, is world-readable.
* Never commit Wi-Fi credentials, pairing keys, tokens, private keys, factory or flash dumps, logs, `.env` files, or personal data (names,
  addresses, e-mail, device ids, MAC addresses, home network details, absolute personal paths). Use documentation addresses (`192.168.1.50`,
  `192.0.2.x`) and placeholders in docs.
* Wi-Fi credentials live only in the git-ignored `firmware/secrets.local.h` (made by `tools\set-dev-secrets.ps1`) and in RAM. The templates
  are the `*.example.*` files. Never print credentials, keys, nonces or MACs to a log.
* Only **synthetic** images are committed: generated covers (`companion/tools/make_test_covers.py`), mock sessions, UI chrome. Never real
  album art, third-party artwork, real desktops or screenshots of someone's library. Look at an image before adding it.
* Personal idle-screen customisation (settings, photo) stays in `%LOCALAPPDATA%\DeskMediaCompanion\idle`, outside the repository
  (`.gitignore` also blocks those names). A personal theme is a local asset, not part of the product.
* Before committing: `git status` and `git diff --cached`; grep the staged files for personal names and file names. Do not `git add -A` blindly.

## Rules: tests and test tools

* "**Hardware-tested**" = driven through the board's real LVGL input path by the test tools (virtual finger, simulated key presses) and
  judged from logs or frame-buffer screenshots. Do not claim the screen, touch feel or a physical button is visually / physically correct
  unless a person confirmed it on the real device. Ask the user only for physical checks that cannot be observed from the computer.
* **Never pin, flash or send commands while someone is using the display by hand**: their swipes and key presses would hit the wrong app.
* `tools/screenshot.py --do ...` actions and test tools that press controls are refused unless the active session is the silent fake test
  session (`companion/tools/fake_media_session.py`); only `swipe_up` / `swipe_down` (selection only) are safe on real sessions.
* End every test with `POST /control/source-auto`. While developing, run the product companion with `--ignore-apps python.exe,deskfakeb.exe` so
  fake sessions never reach the display in use. `tools/real_cases.py` presses play / pause on real apps: only when nobody is listening.
* Every milestone: build, flash, capture the boot log, check there is no reset loop or fatal error, check heap / stack where relevant, run the
  automated checks, document the result in `docs/development-history/`, then commit the verified state.
* Unit tests: `companion\.venv\Scripts\python.exe -m unittest discover -s companion\tests`. Keep the pure logic (`sources.py`, `artwork.py`,
  `idle.py`) free of WinRT so it stays testable.

## Rules: firmware and companion engineering

* Keep LVGL work on the LVGL task (core 1, 24 KB stack, **one** software draw unit); never block it. Network code only calls LVGL through the
  thread-safe `ui_*` functions; LVGL callbacks only enqueue. No logging or relabelling inside LVGL timer callbacks.
* Reuse buffers, allocate large ones once in PSRAM, never per track. Poll the companion about once a second (the UI interpolates progress). A
  held key must never flood the command queue; the poll is time-scheduled and goes first.
* Do not add full-screen LVGL shadows, blur or overlays (they redraw on every progress tick); overlays are children of the screen, not of
  `lv_layer_top` (the screenshot tool does not render it). Keep the player foreground clean.
* **Every command names the session on screen** (`app=` / `from=`); the companion refuses it if that session is gone. Never route a command
  to "whatever is active". While the idle screen is shown nothing reaches any app (swipes, volume keys, `app=idle` requests are refused).
* Selection of what is shown is the pure state machine in `sources.py` (AUTO / MANUAL_TEMP + synthetic Idle item, [ADR-005](docs/decisions/ADR-005-source-switching-by-vertical-swipe.md),
  [ADR-006](docs/decisions/ADR-006-temporary-manual-selection-and-spotify-options.md), [ADR-008](docs/decisions/ADR-008-idle-screen-as-source-ring-item.md)). Change it with tests.
* All picture work happens on the PC (the ESP32 decoder is baseline-JPEG only); the display never sees an original.
* Settings of the idle screen are never writable over HTTP.
* Persisted formats (NVS keys, the `idle_img` partition header) need a version / magic and a fallback; a corrupt value must degrade to defaults.

## Pitfalls

* **BOOT** (GPIO0) at power-up is the ROM download-mode gesture, while the app runs it is volume down: a Wi-Fi reset gesture must be a long
  press while running. **PWR** is the AXP2101 power key: never hold it longer than about 4 s (the PMIC powers the board off).
* The native USB console only streams while the host asserts DTR; `idf.py monitor` needs an interactive terminal, so use
  `tools/serial_capture.py` (never open the port with both DTR and RTS asserted). The console can drop an occasional log line.
* Chromium browsers publish ~150 px thumbnails and Windows cannot give more; browser volume is per browser, not per tab; Firefox cannot be
  matched for volume; a paused browser tab may have no audio session.
* The UI fonts are ASCII Montserrat: non-ASCII titles render with missing glyphs. LVGL's built-in Montserrat stops at 48 px (the idle clock font is generated by `tools/gen_clock_font.py`).
* Touch: an early demo build crashed on taps in a top band (7 KB LVGL stack, two draw units, logging inside timers); the cause was never
  proven, so the 24 KB stack, one draw unit and "no logging in LVGL timers" stay ([M1](docs/development-history/M1-lcd-touch.md)).
* Wi-Fi credentials are compile-time developer configuration; there is no provisioning portal yet.
* The removed Spotify Web-API client lives in git history (tag `legacy-spotify-web-api`); do not revive it unless asked.

## Git

* `main` holds the latest known-good, bootable state; risky work goes on a branch or a checkpoint commit first. Commit after each verified
  step, not one giant commit. Before major changes: `git status` and `git log --oneline -10`.
* After cloning on a new machine: read this file and the README, `docs/development.md`, the latest log in `docs/development-history/`;
  re-download git-ignored material (`docs/waveshare/`); confirm the board still boots before flashing.
* Add a decision record in `docs/decisions/` for a design choice that is hard to reverse.

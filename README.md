# Spotify Now-Playing Desk Display — "Night City" edition

A birthday-gift desk display for the **Waveshare ESP32-S3-Touch-LCD-4B** (SKU 32052, the "Smart 86 Box":
4" 480x480 capacitive touchscreen, ESP32-S3, Wi-Fi + BLE). It makes no sound itself — it shows and
controls what is playing on the owner's phone / computer / speaker / TV via the Spotify Web API:
big square album art, title, artist, progress/seek, play-pause-previous-next, a Cyberpunk-Edgerunners-style
idle screen with a clock, a one-time birthday intro, and a first-run Wi-Fi + Spotify setup flow.

> **Status: investigation complete, nothing flashed yet.** The factory firmware is backed up and verified.
> Flashing starts only after the owner says **PROCEED**. See [Milestones](#milestones).

## Hardware (verified on this unit)

ESP32-S3-WROOM-1-N16R8 · 16 MB quad flash · 8 MB octal PSRAM · ST7701 RGB panel · GT911 touch (I2C 0x14) ·
TCA9554 IO expander · AXP2101 PMIC · QMI8658 · PCF85063 · ES8311/ES7210 (unused). Full pin map, evidence and
sources: [docs/HARDWARE.md](docs/HARDWARE.md). The board has **two USB-C ports**; the one connected to the PC is the
ESP32-S3 **native USB** (`USB JTAG/serial debug unit`, shows up as a COM port, currently `COM5`), and it has only
**BOOT** and **PWR** buttons (no RESET).

## Development environment

| Tool | Purpose | State |
|---|---|---|
| Python 3.12, Git (Windows) | host | installed |
| `tools/venv` — esptool 5.5.0 + pyserial | backup / restore / low-level flashing, independent of the build toolchain | installed — `python -m venv tools\venv; tools\venv\Scripts\python.exe -m pip install -r tools\requirements.txt` |
| ESP-IDF **5.5.x** + `idf.py` | build the firmware (decision: [docs/DECISIONS.md](docs/DECISIONS.md#adr-001--framework-esp-idf-55x--lvgl-9-via-esp_lvgl_port-not-arduino)) | installed at Milestone 1 via Espressif's `eim` (`winget install Espressif.EIM-CLI`) |
| LVGL 9.5.0, `esp_lvgl_port`, Waveshare BSP/panel drivers | UI + display + touch | fetched by the ESP-IDF component manager at first build |
| VS Code | optional editor | present |

## Build / flash

*(filled in at Milestone 1 once ESP-IDF is installed)*

Rules of the road: every flash is preceded by a successful build; `idf.py erase-flash` / `esptool erase-flash`
are never run casually (the factory backup is the safety net, but NVS — Wi-Fi/Spotify tokens, "birthday shown" flag —
lives in flash). We **never burn eFuses**, so the board cannot be bricked by a bad image.

## Spotify developer setup (rules as of Feb 2026 — details in [docs/DECISIONS.md](docs/DECISIONS.md#adr-002--spotify-rules-that-shape-the-design-verified-against-spotifys-own-pages-2026-10-11))

1. Someone with **Spotify Premium** creates an app at <https://developer.spotify.com/dashboard> (Development Mode apps need a
   **Premium app owner**, are limited to **5 users**, and the owner's Premium must stay active).
2. Select **Web API**; add the redirect URI the firmware shows on its setup page (HTTPS, or `http://127.0.0.1…`; no `localhost`,
   no plain-HTTP LAN addresses).
3. Under **User Management** add the Spotify account (name + e-mail) of whoever will use the display.
4. Copy the **Client ID** into the device's setup page. No client secret is needed (Authorization Code + PKCE), and nothing
   is hard-coded in the firmware or this repo.
5. The listener needs **Premium** for play/pause/next/previous/seek; with a free account the display still shows what is playing
   and disables the controls.

*(exact screens and redirect-page details are finalised at Milestones 5 and 11)*

## First-run setup / recovery / reset

*(designed and documented at Milestone 11; planned flow: device opens a temporary Wi-Fi access point -> phone joins -> captive page
picks the Wi-Fi network -> device shows a QR code -> phone authorises Spotify -> credentials stored in NVS; a long-press gesture on the
idle screen / holding BOOT at power-up wipes Wi-Fi + Spotify settings and re-enters setup.)*

## Restore the factory firmware

The untouched board's complete 16 MiB flash is saved in `backup/factory/` (git-ignored; **keep a second copy somewhere safe**).
Hashes, partition layout and the exact restore commands are in [backup/factory/MANIFEST.md](backup/factory/MANIFEST.md).
In short, from the project root:

```powershell
.\tools\venv\Scripts\python.exe -m esptool --port COM5 --flash-mode keep --flash-freq keep --flash-size keep write-flash 0x0 backup\factory\factory_full_flash_16MB_mac-2884-85a2-4860_20261011-230234.bin
```

If the board does not respond: unplug USB-C, hold **BOOT**, plug USB-C back in, release BOOT, run the command again.

## Milestones

| # | Milestone | State |
|---|---|---|
| 0 | Detect board, confirm hardware, find official docs, choose framework, **back up factory flash** | **done** |
| 1 | Flash an official/simple Waveshare LCD + touch example; confirm display and touch | waiting for PROCEED |
| 2 | Own 480x480 UI, touch buttons verified | |
| 3 | Wi-Fi connection | |
| 4 | HTTPS / API requests | |
| 5 | Spotify OAuth + currently-playing metadata | |
| 6 | Album art | |
| 7 | Play / pause / previous / next (+ seek) | |
| 8 | Polished UI | |
| 9 | Cyberpunk idle screen | |
| 10 | Birthday intro | |
| 11 | First-run Wi-Fi + Spotify setup | |

## Repository layout

```
backup/factory/   verified factory flash dump (+ MANIFEST.md with hashes and restore steps)
docs/             HARDWARE.md (pins, evidence, sources), DECISIONS.md (framework + Spotify rules)
tools/            requirements.txt (esptool venv), analyze_flash_dump.py (inspect any 16 MiB dump)
docs/waveshare/   vendor downloads: schematic, demos, official repo, BSP (git-ignored, re-fetchable)
```

(`main/`, `components/` and friends appear at Milestone 1: `board`, `display`, `touch`, `ui`, `screens/{now_playing,idle,setup,birthday}`,
`wifi`, `spotify`, `storage`.)

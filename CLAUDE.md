# CLAUDE.md

## Project: ESP32 Spotify Now Playing Gift Display

### Purpose

Build a polished, standalone Spotify "Now Playing" touchscreen desk display as a birthday gift.

The device should feel like a finished consumer product, not a development board. It will show the current Spotify track, album art, artist, playback progress, and touch controls. It will also include a Cyberpunk / Night City-inspired idle screen and a one-time birthday intro.

The device does **not** need to output audio. It acts as a Spotify status display and remote controller for playback occurring on another Spotify device.

---

## Hardware

Exact board:

- **Waveshare ESP32-S3-Touch-LCD-4B**
- Waveshare SKU: **32052**
- Product family: **Smart 86 Box**
- MCU/module: **ESP32-S3-WROOM-1-N16R8**
- Flash: **16 MB**
- PSRAM: **8 MB octal**
- Display: **4-inch 480 x 480**
- LCD controller/panel path: **ST7701 RGB**
- Touch controller: **GT911**
- Wi-Fi + BLE
- Native USB / USB JTAG Serial
- Known Windows port during development: **COM5**
- USB VID:PID observed: **303A:1001**
- Physical buttons: **BOOT** and **PWR**
- Two USB-C ports
- Current physical unit includes the white front shell/bezel; rear PCB remains exposed.

Do **not** substitute settings from a similar Waveshare board without verifying them against this exact model.

---

## Current Development Stack

Preferred stack:

- **ESP-IDF 5.5.5**
- **LVGL 9.5.0**
- Waveshare BSP **2.0.0**
- Windows development machine
- ESP-IDF installed under `C:\esp`
- Espressif tools installed under `C:\Espressif`
- esptool 5.5.0 available in the project-local environment

Why ESP-IDF instead of Arduino:

- 480 x 480 RGB framebuffer uses PSRAM.
- Stable RGB display + Wi-Fi/TLS benefits from explicit `sdkconfig` control.
- Waveshare's maintained ESP-IDF BSP path is preferred for this board.
- ESP-IDF provides first-class HTTPS, NVS, networking, captive portal primitives, JPEG support, FreeRTOS task control, etc.

Do not migrate to Arduino unless there is a strong technical reason and it is documented first.

---

## Repository State / History

Important known commits:

- Baseline / Milestone 0 commit: `95f724d`
- Milestone 1 commit: `d64ad06`

If these hashes are not present after cloning, inspect the actual repository history before making assumptions.

Important existing project documentation may include:

- `docs/DECISIONS.md`
- `docs/milestones/M1-lcd-touch.md`
- `firmware/NOTICE.md`
- `README.md`
- `tools/serial_capture.py`

Waveshare reference material was previously downloaded under:

- `docs/waveshare/`

That directory may be git-ignored due to size (~700 MB). On a new machine, re-download the required official documentation/examples rather than assuming it exists.

---

## Factory Firmware Backup — CRITICAL

A complete 16 MiB factory flash backup was created before development.

Original local path:

`backup/factory/factory_full_flash_16MB_mac-2884-85a2-4860_20261011-230234.bin`

Associated manifest:

`backup/factory/MANIFEST.md`

The backup was verified against the chip byte-for-byte / by digest.

Important facts:

- Secure boot: off
- Flash encryption: off
- No eFuses were intentionally changed
- Factory image contains the original Waveshare apps
- The board remains recoverable through ROM download mode

### Rules

1. **Never commit the factory `.bin` to GitHub.**
2. Keep the backup in at least one location outside the repository.
3. Never burn security-related eFuses.
4. Never perform a destructive full-chip erase unless specifically required and approved.
5. Before changing the partition table, document the old and new layouts.
6. Preserve documented restore instructions.

If the backup is unavailable on a new computer, development may continue, but do not claim the factory image is recoverable from Git unless a separately stored copy is confirmed.

---

## Milestone Status

### Milestone 0 — DONE

Completed:

- Exact hardware detected.
- COM/USB connection verified.
- Flash and PSRAM identified.
- Factory boot log inspected.
- Factory image backed up and verified.
- ESP-IDF + LVGL chosen.
- Baseline committed.

### Milestone 1 — DONE / PHYSICALLY VERIFIED

Completed:

- ESP-IDF 5.5.5 installed.
- Official Waveshare LVGL demo built successfully.
- Display firmware flashed successfully.
- ST7701 display initialises.
- GT911 touch initialises.
- No boot loops or reset errors observed.
- PSRAM/internal RAM remained stable.
- User physically confirmed the LVGL demo is visible and properly rendered.
- Display appeared correctly aligned with normal colours and no visible tearing/flicker.
- Diagnostic touch support was added for physical validation.

Before assuming touch calibration is perfect on another build, preserve or rerun the touch diagnostic if needed.

---

## Remaining Milestones

### Milestone 2 — Custom 480 x 480 UI + Touch

Goal:

- Replace the generic Waveshare demo with the initial music-display UI.
- Confirm LVGL layout, fonts, buttons, image container, progress bar, and touch interactions.

Required prototype screen:

- album-art placeholder
- song title
- artist
- progress bar
- elapsed / total time
- previous
- play/pause
- next
- full-screen-art toggle

Do not add Spotify yet.

### Milestone 3 — Wi-Fi

Goal:

- Establish stable Wi-Fi networking.
- Test reconnect behavior.
- Avoid hard-coded credentials in the final product.

Hard-coded temporary developer credentials are acceptable only for a short development test and must not be committed.

### Milestone 4 — HTTPS/API

Goal:

- Confirm outbound HTTPS/TLS.
- Validate certificate/time requirements.
- Perform a harmless test API request.
- Keep networking asynchronous enough that LVGL remains responsive.

### Milestone 5 — Spotify Authentication + Current Track

Use Spotify Web API.

Preferred auth:

- **Authorization Code with PKCE**

Do not use:
- Spotify username/password storage
- Implicit Grant
- embedded personal credentials
- committed client secrets/tokens

The final owner must authorize their own Spotify account.

Spotify platform details can change. Before implementing authentication or playback control, verify current Spotify developer rules from official Spotify documentation.

Expected capabilities:

- current track
- artist
- duration
- progress
- playback state
- active device information where available

### Milestone 6 — Album Artwork

Goal:

- Download and display album artwork.
- Preserve aspect ratio.
- Decode/scale efficiently for 480 x 480.
- Avoid repeated large allocations.
- Cache/reuse artwork where practical.

Album art should be the visual focus of the product.

### Milestone 7 — Playback Controls

Implement:

- Previous
- Play
- Pause
- Next
- Seek via progress bar if reliable

Gracefully handle:

- no active playback device
- paused playback
- API failure
- expired authorization
- rate limiting
- Wi-Fi loss

### Milestone 8 — Polished Main UI

Product-quality now-playing layout.

Design goals:

- minimal
- dark
- visually premium
- large album art
- highly readable song/artist text
- touch targets large enough for casual desk use
- subtle animation only where it improves the experience
- no clutter

Include full-screen album-art mode.

### Milestone 9 — Cyberpunk Idle Screen

When no active playback exists, switch to a custom idle interface inspired by futuristic Night City / cyberpunk UI.

Visual direction:

- dark background
- restrained yellow / cyan / red accent use
- digital clock
- subtle futuristic UI elements
- message such as `NO ACTIVE PLAYBACK`

Do not turn the interface into a cluttered anime collage.

Avoid embedding copyrighted art unless the user explicitly provides an asset they have the right to use. Original cyberpunk-inspired UI is preferred.

### Milestone 10 — Birthday Intro

One-time first-normal-boot intro.

Concept:

```
INITIALIZING...

[brief futuristic boot animation]

I REALLY WANT TO STAY AT YOUR HOUSE

HAPPY BIRTHDAY
<RECIPIENT_NAME>

WELCOME TO NIGHT CITY
```

Requirements:

- recipient name stored as an easy configuration value
- one-time flag stored in NVS
- easy developer method to replay/reset the intro
- do not embed/download the copyrighted song itself

### Milestone 11 — First-Run Setup

Final user onboarding should not require a PC.

Preferred flow:

1. Device starts setup mode if Wi-Fi / Spotify auth is missing.
2. Device creates a temporary setup AP.
3. User connects from phone.
4. Local/captive setup page opens.
5. User selects Wi-Fi and enters password.
6. Device stores Wi-Fi credentials securely in NVS.
7. Device presents a Spotify authorization flow.
8. User authorizes Spotify.
9. Device returns to normal Now Playing mode.
10. Future boots reconnect automatically.

Provide an easy reset/reconfiguration path.

---

## Product Behavior

Normal startup after setup:

```
Power
  ↓
Boot
  ↓
Connect Wi-Fi
  ↓
Restore / refresh Spotify authorization
  ↓
Fetch playback state
  ↓
Now Playing OR Idle
```

The display must not require the development PC during normal operation.

The board should recover gracefully from:

- router unavailable at boot
- temporary Wi-Fi loss
- internet loss
- Spotify temporarily unavailable
- token expiration
- no active Spotify device
- playback stops
- playback resumes
- malformed / missing album artwork
- reboot / power loss

---

## Main UI Requirements

Target resolution: **480 x 480**

Main screen should contain:

- large square album cover
- song title
- artist
- elapsed time
- total duration
- playback progress
- previous control
- play/pause control
- next control

Optional after core functionality is stable:

- current Spotify Connect device
- volume if reliable
- shuffle/repeat state

Do not prioritize optional features over reliability.

### Full-Screen Art Mode

Tap album art:

- controls/text disappear
- album art becomes dominant / nearly full-screen

Tap again:

- return to normal player UI

---

## Architecture

Keep modules separated. Exact naming may change, but preserve the separation of concerns.

Suggested structure:

```text
firmware/
  main/
    app_main.*
    app_state.*
    display/
    touch/
    ui/
      screens/
        now_playing.*
        idle.*
        setup.*
        birthday.*
    network/
      wifi.*
      http.*
    spotify/
      spotify_client.*
      spotify_auth.*
      spotify_models.*
    storage/
      settings.*
      nvs_store.*
    assets/
```

Avoid a single giant application file.

### Suggested State Model

Application-level states:

```text
BOOT
SETUP_REQUIRED
CONNECTING_WIFI
AUTH_REQUIRED
IDLE
NOW_PLAYING
NETWORK_ERROR
AUTH_ERROR
```

Playback model should include at least:

```text
track_id
track_name
artist_name
album_name
album_art_url
duration_ms
progress_ms
is_playing
device_id
device_name
```

UI must render from state rather than mixing API/network operations directly into button drawing logic.

---

## Concurrency / Performance

This is embedded hardware.

Rules:

- Keep LVGL work on the expected LVGL/UI task/thread.
- Do not perform long blocking HTTPS calls inside the UI callback path.
- Reuse buffers.
- Use PSRAM intentionally.
- Avoid repeated heap fragmentation from large JPEG allocations.
- Prefer state changes/messages between network and UI tasks.
- Measure free heap / PSRAM during major changes.
- Watch watchdog timing.
- Do not poll Spotify absurdly fast.
- Keep touch responsive during network requests.

---

## Security / Secrets

Never commit:

- Spotify access tokens
- Spotify refresh tokens
- OAuth authorization codes
- Wi-Fi passwords
- API credentials
- private keys
- factory flash dump
- local `.env` files containing credentials

Repository should contain templates/examples only.

Recommended patterns:

```text
secrets.local.h
.env
sdkconfig.local
```

These must be git-ignored.

If a Spotify client ID is safe to expose under the chosen PKCE flow, still keep environment-specific configuration separate from product source where practical.

Never print refresh/access tokens to the normal serial log.

---

## Git Rules

This repository exists specifically so development can continue from different machines and different Claude Code sessions.

### Commit strategy

Commit after each verified milestone or meaningful stable checkpoint.

Good examples:

```text
milestone 2: initial 480x480 now playing UI
milestone 3: add stable wifi connection and reconnect
milestone 5: add spotify PKCE auth and playback state
fix: prevent album art heap fragmentation
```

Avoid giant "everything" commits.

### Before major changes

Run:

```bash
git status
git log --oneline -10
```

Understand the current state before editing.

### Never overwrite verified work casually

If an experiment may be destructive:

- create a branch, or
- make a checkpoint commit first

### Main branch expectation

`main` should represent the latest known-good, bootable state whenever practical.

Experimental work may use feature branches.

---

## GitHub / Continue-Anywhere Requirements

The remote repository should preferably be **private** while development is active.

After cloning on another computer:

1. Read this `CLAUDE.md` fully.
2. Read `README.md`.
3. Read latest `docs/milestones/*`.
4. Run `git status`.
5. Run `git log --oneline -10`.
6. Do not assume COM5; detect the connected ESP32 port.
7. Do not assume ESP-IDF paths; detect/install the required version.
8. Recreate/download git-ignored dependencies such as Waveshare reference material.
9. Confirm the board still boots before flashing.
10. Continue from the latest completed milestone, not from the original plan blindly.

On a new Claude Code session, this file is the authoritative project orientation, but current source code and milestone docs override stale descriptions here.

---

## Flashing Safety

Before flashing:

1. confirm exact target (`esp32s3`)
2. confirm the intended serial device
3. stop any process holding the COM port
4. build successfully
5. inspect flash command / partition impact
6. avoid erase-all unless justified
7. capture serial output after reboot

If flashing appears to fail, do not repeatedly erase the board. Diagnose USB mode, BOOT behavior, serial-port ownership, esptool output, and boot logs first.

Physical intervention may be required for BOOT/PWR operations. Tell the user exactly what to press and when.

---

## Testing Philosophy

For every milestone:

1. build
2. flash
3. capture boot logs
4. verify no reset loop / fatal errors
5. verify memory health where relevant
6. perform automated checks that are possible
7. ask the user only for physical checks that cannot be observed from the computer
8. document result
9. commit verified state

Do not claim the screen or touch is visually correct unless the user physically confirms it or a real visual inspection source is available.

---

## User Experience Goal

The finished device should feel like:

- plug in USB-C
- boot quickly
- connect automatically
- show beautiful current music
- tap to control playback
- look good sitting on a desk

It should **not** feel like:

- an ESP32 demo
- a debug dashboard
- a tiny web browser
- a cluttered smart-home panel
- something requiring a laptop every time

---

## Enclosure Direction

Hardware currently has a white front bezel.

Final physical direction:

- keep front visually clean
- hide rear PCB completely
- leave required USB-C access
- integrate a stable desk stand
- approximately 10–15 degree rearward viewing angle if practical
- no exposed jumper wires
- no soldering required for the planned core build
- enclosure design comes after firmware/UI layout is stable enough to know orientation and port needs

Preferred final finish: simple, consumer-like, understated.

---

## Scope Guardrails

Do not add these until the primary product is complete:

- lyrics
- playlist browsing
- search
- queue editor
- Bluetooth audio output
- local speaker playback
- weather
- smart-home controls
- AI chatbot
- complex audio visualizers

They may be future enhancements, but they must not delay the core music-display gift.

---

## Immediate Next Task

Current next milestone:

**Milestone 2 — build the custom 480 x 480 non-Spotify Now Playing UI and verify touch behavior.**

Before starting:

1. inspect current branch/status
2. read Milestone 1 documentation
3. preserve the known-good display/touch configuration
4. build the custom UI on top of that verified hardware layer
5. do not begin Spotify integration until the UI prototype is stable


# CLAUDE.md

## Project: ESP32 Desk Media (Now Playing) Gift Display

### Purpose

Build a polished, standalone "Now Playing" touchscreen desk display (for the recipient's Windows PC) as a birthday gift.

The device should feel like a finished consumer product, not a development board. It shows what the recipient's **Windows PC** is currently playing - title, artist / channel, artwork, playback progress - and gives touch controls (play/pause, previous, next, seek). It will also include a Cyberpunk / Night City-inspired idle screen.

The device does **not** output audio. It is a status display and remote controller for the **active Windows media session**, whatever app produces it (Spotify desktop, YouTube / YouTube Music in a browser, VLC, Apple Music, ...).

---

## ROADMAP CHANGE (owner decision, 2026-10-12, later that day) - read this too

The **birthday intro is dropped** - do not work on it. Current priority order after the MVP:

A. Main player visual redesign (done, Milestone 8) -> B. Cyberpunk idle screen (M9) -> C. Full-screen album-art polish (M10) -> D. First-run Wi-Fi setup (M11) -> E. Companion installer (M12) -> F. Pairing / security (M13).

---

## ARCHITECTURE CHANGE (owner decision, 2026-10-12) - read this first

**Spotify OAuth / the Spotify Web API is no longer the architecture.** The device is only for the recipient's Windows PC, so:

- A small **Windows companion app** (`companion/`, Python + WinRT `Windows.Media.Control` / GlobalSystemMediaTransportControlsSessionManager) detects the active media session, and serves title / artist / album / playback state / position / duration / artwork over a small **HTTP API on the local network** (`GET /now-playing`, `GET /artwork`, `POST /control/{playpause,next,previous,seek}`).
- The **ESP32 firmware polls the companion** (it finds the PC automatically through a UDP beacon) and sends control commands back. No Spotify account, Client ID, Premium, tokens, TLS or internet access is needed.
- The old Spotify client is **obsolete**: it is kept, not built, in `legacy/` (commit `db51d85`). Do not revive it unless the owner asks.
- Authoritative design + protocol: `docs/ARCHITECTURE.md`; decision record: ADR-003 in `docs/DECISIONS.md`. Wherever the rest of this file still says "Spotify", read it as "the companion / Windows media session" - the sections below have been updated, and the milestone numbers below are the **current plan**.

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
- Physical buttons: **BOOT** (GPIO0) and **PWR** (AXP2101 PWRON, read by polling the PMIC over I2C) - the two side buttons are the **volume keys** (PWR = up, BOOT = down; see Milestone 8b and `docs/HARDWARE.md`)
- Two USB-C ports
- Current physical unit includes the white front shell/bezel; rear PCB remains exposed.

Do **not** substitute settings from a similar Waveshare board without verifying them against this exact model.

---

## Current Development Stack

Preferred stack:

- **ESP-IDF 5.5.5**
- **LVGL 9.5.0**
- Waveshare BSP **2.0.0**
- Windows development machine (also the machine that runs the companion during development)
- **Windows companion:** Python 3.12, `winrt-*` packages, Pillow, psutil (`companion/requirements.txt`); packaged later as a single `.exe` (PyInstaller)
- ESP-IDF installed under `C:\esp`
- Espressif tools installed under `C:\Espressif`
- esptool 5.5.0 available in the project-local environment

Why ESP-IDF instead of Arduino:

- 480 x 480 RGB framebuffer uses PSRAM.
- Stable RGB display + Wi-Fi/TLS benefits from explicit `sdkconfig` control.
- Waveshare's maintained ESP-IDF BSP path is preferred for this board.
- ESP-IDF provides first-class HTTP, NVS, networking, captive portal primitives, JPEG support, FreeRTOS task control, etc.

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
(See "Known issues" at the end: the M1 demo crashed on some touches. The M2 display config - 24 KB LVGL stack, one draw unit - is our answer; a pristine-demo gate on 2026-10-12 ran 7 minutes without a crash.)

### Milestone 2 - DONE (24 real touches over 941 s on the network build, no reset)

Own 480x480 UI in `firmware/components/ui` (placeholder art box, title, artist, seek bar, elapsed/total, prev/play-pause/next, tap-art full-screen toggle, status line). Evidence: `docs/milestones/M2-ui.md`.

### Milestones 3-7 + MVP gate - DONE (merged to `main`)

Wi-Fi + auto-reconnect (link dropped and recovered in ~2 s), the Windows companion, discovery, now-playing, artwork, commands. Verified with real Spotify desktop and Chrome (YouTube etc.) plus 24 physical touches. Evidence and open points: `docs/milestones/MVP-media-companion.md`. The owner has not yet given a visual confirmation of the screen - do not claim the layout is visually verified until they do.

---

## Remaining Milestones

### Milestone 2 - Custom 480 x 480 UI + Touch (done, see status above)

Required prototype screen: album-art placeholder, song title, artist, progress bar, elapsed / total time, previous, play/pause, next, full-screen-art toggle. Acceptance includes a multi-minute touch soak with zero resets (the health log line's `touches=` counter proves taps happened).

### Milestone 3 - Wi-Fi (done in firmware)

Stable Wi-Fi with automatic reconnect (exponential back-off). Credentials: temporary developer credentials only in the git-ignored `firmware/secrets.local.h` (`tools/set-dev-secrets.ps1`); never in the product, never committed, never logged.

### Milestone 4 - Windows companion (replaces "HTTPS/API")

`companion/`: GSMTC wrapper, HTTP API, UDP discovery beacon, per-user auto-start option. Runs quietly in the background. See `docs/ARCHITECTURE.md` for the protocol.

### Milestone 5 - ESP32 <-> companion: now-playing + progress (replaces "Spotify authentication")

`firmware/components/pc_media`: discovery, poll `/now-playing` about once per second, keep UI responsive (UI interpolates progress locally). Never block the LVGL task.

### Milestone 6 - Album artwork

The companion cleans any thumbnail (removes the Spotify logo composite and black letterbox bars) and renders a **baseline** JPEG at exactly the size the display draws it: a square cover, or - for 16:9 video thumbnails - a landscape card, aspect ratio always kept (`GET /artwork?size=312&maxw=432`); the ESP32 downloads it only when `art_id` changes and decodes it into two fixed PSRAM buffers (no allocation churn). Album art should be the visual focus of the product.

### Milestone 7 - Playback controls

Previous / play-pause / next / seek via `POST /control/...`, applied by the companion to the same media session. Gracefully handle: nothing playing, app refuses a control (`ok:false`), companion unreachable (PC asleep / app closed / firewall), Wi-Fi loss.

### MVP gate (before any polish)

Works with **Spotify desktop** and **YouTube in a supported browser**: real artwork, title + artist/channel, working progress bar, play/pause, next/previous where the media session supports them.

### Milestone 8 - Main player visual redesign (DONE 2026-10-12; evidence: `docs/milestones/M8-player-redesign.md`)

The owner found the MVP screen too prototype-like and asked for: a much bigger cover as the focal point, no card/border around it, the **whole
screen background derived from the cover** (blurred, darkened), no empty side borders, a clean readable foreground, and a finished-product look.

Built (`firmware/components/ui/ui.c`, `firmware/components/pc_media/pc_art.c`):

- cover 312x312 (was 260) with no frame - the box only rounds the corners; wide/tall art (16:9 video thumbnails) is kept whole and the rest of the square
  is filled with a blurred copy of itself (done in the companion), so there are no dead bars;
- background = one pre-baked RGB565 image per track: cover -> 40x40 colour grid -> saturate -> smooth -> smooth-step upscale to 480x480 -> darkened with a vertical
  gradient (extra dark for bright covers) -> soft shadow behind the cover -> ordered-dithered (no banding). LVGL only blits it (no overlays, no runtime blur);
- the cover is decoded to RGB888 and dithered down to RGB565 (smooth gradients in the art do not band);
- title (26 pt) and artist (20 pt) centred below, thin progress line as wide as the cover with the times at its ends (knob appears only while dragging),
  big white play/pause button, plain prev/next icons, dimmed when the session does not support them;
- the no-artwork look is a dithered dark gradient with a translucent placeholder square.

Rules for later work on this screen: keep the foreground clean; do not add LVGL shadows/blur/overlays over the full screen (they redraw every progress tick);
verify visually with `tools/screenshot.py` (see Testing Philosophy) instead of guessing.

### Milestone 8b - Refinement pass (DONE 2026-10-12; evidence: `docs/milestones/M8b-art-layout-gestures-volume.md`)

After the owner reviewed M8 on the real display:

- **Widescreen art** (YouTube) is a landscape card (432 px wide for 16:9, rounded, never stretched) instead of a letterboxed square; square covers are unchanged; the title / artist follow the card.
  Chromium hands Windows only ~150 px thumbnails, so cards are soft - a Windows limit.
- **The Spotify logo is gone**: Windows gives a 300x300 RGBA composite (opaque 234x234 cover, transparent gap, logo). `artwork.split_composite` splits it structurally (alpha bands), never by a fixed crop; anything
  ambiguous is left untouched. Black letterbox bars are trimmed.
- **Swipe gestures**: >= 72 px mostly horizontal, < 1 s; consumed so nothing is clicked; never from the progress bar; no-op when the app has no next / previous. *(M8b had right = next; reversed in M8c: left = next, right = previous.)*
- **Per-app volume** via Core Audio sessions (`companion/desk_media_companion/volume.py`): `GET /volume`, `POST /control/volume-up|volume-down[?step=5&app=...]`. Never the master volume (verified).
  Browsers: whole-browser volume (shared audio process); Firefox not matchable.
- **Side buttons = volume**: PWR (upper) = up, BOOT (lower) = down (`firmware/components/side_keys`), 5 % steps, hold-repeat capped at 3 s, transient "Volume 65%" pill.
  **Do not hold PWR > ~4 s (the PMIC powers the board off). BOOT at power-up is the ROM download-mode gesture, so Milestone 11's "hold BOOT at power-up to wipe Wi-Fi" must become a long press while running.**
- Rules learned: the display must name the app it shows in volume requests; never let a held key flood the command queue (poll is time-scheduled); overlays must be children of the screen (the screenshot tool does not render `lv_layer_top`).

### Milestone 8c - Media-source switching (DONE 2026-10-12; evidence: `docs/milestones/M8c-session-switching.md`; ADR-005)

Owner's second refinement request, after using M8b:

- **Horizontal swipes reversed**: finger LEFT = next track, RIGHT = previous (same thresholds / safeguards; seek-bar swipes still only seek; tap on the cover still toggles full screen).
- **Vertical swipes switch the media source**: finger UP = next, DOWN = previous source (= another app's Windows media session; playing **and paused** ones with a title / artist; dead sessions such as `PSExpressCore.exe`
  are left out; fixed alphabetical ring that wraps; nothing happens with a single source). The selection lives in the companion (`desk_media_companion/sources.py` = pure model + 43 unit tests; `media.py` applies it):
  **auto** follows the best session, a swipe **pins** the display to a session until that session disappears (another app becoming active does not take over). Feedback: a faint chevron disc + a translucent pill with the
  source name and position dots (~1.1 s, shared with the volume pill); no permanent widgets.
- **Every command names the session on screen** (`app=` on all `/control/*` and volume requests; `from=` on source switches). The companion sends it to exactly that session and refuses it if that session is gone - so controls,
  seek, play/pause and the side-button volume always follow what is displayed, immediately after a switch. A paused source shows its artwork, title, timeline and the play icon; play resumes that session.
- **Chrome / YouTube artwork**: investigated before accepting 150 px. GSMTC exposes one thumbnail stream and no URL / size variants; Windows passes an app's image through byte-identically (a 1280x720 PNG published by the fake
  session came back unchanged); Chromium itself requests artwork at `kMinImageSize = 71` / `kDesiredImageSize = 150` (`system_media_controls_notifier.cc`). So **no higher-resolution image exists without a browser extension**.
  PC-side up-scaling was improved (`artwork.enhance_upscale`: edge-only, halo-limited, strength ramps with the enlargement; Spotify-sized enlargements are bit-identical). An extension (reads the page's own MediaSession artwork list)
  is documented as an option in the M8c doc, deliberately not built.
- *(ADR-006 / M8d changed the "pin until it disappears" rule below into a temporary manual selection that auto-follow ends; see Milestone 8d.)*
- Rules learned: a dev action that only changes the *selection* (`swipe_up` / `swipe_down`) is safe on real sessions, everything else is guarded; end every test by `POST /control/source-auto` so the owner's display is not left pinned.

### Milestone 8d - Auto-follow restored + Spotify-only shuffle / repeat (DONE 2026-10-12; evidence: `docs/milestones/M8d-auto-follow-spotify-options.md`; ADR-006)

Owner's third refinement request, after using M8c ("a manual swipe must not disable auto-follow for good"):

- **Selection is an explicit state machine** (`companion/desk_media_companion/sources.py`, `Selector`, pure + unit-tested): `AUTO` follows the playing session that started playing **most recently** (a 0.4 s background watcher tracks the transitions);
  a vertical swipe enters `MANUAL_TEMP`, which ends - back to AUTO - when **another** session starts playing, when the selected session disappears, or (only if the selected session is not playing while another one is) after **20 s** without
  touching the selected session. A selected session that starts playing stays. No permanent pin any more; `mode` (auto / manual) replaces `pinned` in `/now-playing`, `/sources` and the swipe answer.
- **Commands unchanged in principle**: every request still names the displayed session (`app=`), so after an auto-switch the next poll (<= 1 s) moves the screen and controls / seek / volume follow it.
- **Spotify-only Shuffle and Repeat**: investigated first - Windows' GSMTC exposes both for Spotify (state + enabled flags + accepted change requests, three repeat modes: off / playlist / track, ~100 ms to settle), so **no OAuth and no Web API**.
  The companion reports `spotify`, `shuffle`, `repeat` (0/1/2), `can_shuffle`, `can_repeat` (Spotify identified by the session's **app id**, never by metadata) and offers `POST /control/shuffle` / `/control/repeat` (refused for every other app).
  On the display two small buttons flank the transport row only while Spotify is shown (shuffle left of previous, repeat right of next; green + dot when on, "1" on the icon for repeat-track, dimmed when Spotify refuses);
  previous / play / next did not move; Chrome / Edge / VLC / everything else looks exactly as before.
- Test tooling: `tools/auto_follow_test.py` (isolated: a companion that sees only two silent fake sessions - `python.exe` treated as Spotify via `--treat-as-spotify`, and `deskfakeb.exe`, a renamed copy of the interpreter made by
  `companion/tools/fakes.py` - so cases A-E and the Spotify buttons run through the real companion and the real Windows API without touching the owner's apps or display); dev flags `--ignore-apps`, `--only-apps`, `--treat-as-spotify`;
  `tools/session_test.py` is the board-in-the-loop version. Rule: while developing, restart the product companion with `--ignore-apps python.exe,deskfakeb.exe` so fake sessions never reach the owner's display.

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

### Milestone 10 - Full-screen album-art polish

The tap-the-cover full-screen mode works (cover grows to 456x456, chrome hides, tap again to return) but the cover is the 312 px image stretched, so it is soft.
Polish: request a larger cover from the companion for this mode (e.g. `/artwork?size=480`, needs a bigger decode buffer / scaling), smooth the transition, keep the blurred
background, make the tap target and a hint obvious, handle wide art, and keep the screen free of clutter.

*(The one-time birthday intro that used to be Milestone 10 was dropped by the owner on 2026-10-12 and is no longer part of the plan.)*

### Milestone 11 - First-Run Wi-Fi Setup

Final user onboarding should not require a PC.

Preferred flow:

1. Device starts setup mode if Wi-Fi is not configured (or the PC companion is not paired).
2. Device creates a temporary setup AP.
3. User connects from phone.
4. Local/captive setup page opens.
5. User selects Wi-Fi and enters password.
6. Device stores Wi-Fi credentials securely in NVS.
7. Device finds the PC companion (already automatic) and returns to normal Now Playing mode.
8. Future boots reconnect automatically.

Provide an easy reset/reconfiguration path. NOT "hold BOOT at power-up" (that is the ROM download-mode gesture, and BOOT is the volume-down key while running): use a long press of BOOT while the app is running, or a button on the setup / status page.

### Milestone 12 - Companion installer

Single `.exe` (PyInstaller, `companion/build.ps1` already builds it) for the recipient, auto-start with Windows (`--install-startup`, per-user Run key), a one-time Windows Firewall helper (the first run asks to allow the port; a script that adds a rule needs the owner's UAC click - Claude must not change system/firewall settings itself), tray/quit option, update story.

### Milestone 13 - Pairing / security

Display and companion pair once (shared token, shown on the display and typed/confirmed on the PC, or scanned) so the unauthenticated LAN API is closed to every other device; token stored in NVS on the board and in the companion's per-user config; requests carry it; wrong/missing token -> 401. Revisit the beacon (do not advertise to unpaired displays more than needed).

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
Find the Windows companion (UDP beacon) / reconnect
  ↓
Poll the PC's media session
  ↓
Now Playing OR Idle
```

The display must not require the development PC during normal operation.

The board should recover gracefully from:

- router unavailable at boot
- temporary Wi-Fi loss
- PC asleep / companion closed / PC firewall blocking (show "Can't reach your PC", keep retrying)
- the PC's IP address changing (re-discover)
- no active media session
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

- source app of the media session (already shown in the status line)
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

Actual structure (current):

```text
companion/                      Windows companion (Python)
  desk_media_companion/         media.py (GSMTC), server.py (HTTP API), discovery.py (UDP beacon), autostart.py, __main__.py
  tools/fake_media_session.py   silent test media session (--art-dir, --silent-audio for volume tests)
  tools/dump_sessions.py        read-only dump of every media session + raw thumbnail (how the Spotify composite was found)
  desk_media_companion/artwork.py  thumbnail clean-up + aspect-preserving rendering;  volume.py  per-app Core Audio volume;  sources.py  which sessions the display can switch between (pure model)
firmware/
  main/                         main.c (glue), app_config.h, health.c, demo_player.c (UI-only demo)
  components/
    ui/                         LVGL Now Playing screen (later: screens/{idle,setup})
    wifi_mgr/                   Wi-Fi STA + reconnect + SNTP
    pc_media/                   companion discovery + polling + artwork + commands
    side_keys/                  the two side buttons (BOOT GPIO0, PWR via AXP2101 over I2C) as volume down / up
legacy/                         obsolete Spotify client (not built)
```

Planned additions: `screens/idle`, `screens/setup`, `storage/` (NVS settings). Dev-only: `main/dev_tools.c` (screenshot dump + simulated taps, compiled only with `-DAPP_DEV_TOOLS=1`).

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

Playback model (`pc_media_state_t`) includes at least:

```text
has_session, playing, state, app (source app id)
title, artist (or channel), album
position_ms, duration_ms
track_id, art_id
can_play, can_pause, can_next, can_prev, can_seek
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
- Do not poll the companion absurdly fast (about once per second is plenty; the UI interpolates).
- Keep touch responsive during network requests.

---

## Security / Secrets

Never commit:

- Wi-Fi passwords
- any pairing token for the companion API (Milestone 13)
- Spotify tokens / client secrets (obsolete, but the rule stands if the legacy code is ever revived)
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

Keep environment-specific configuration separate from product source (`secrets.local.h`, NVS). The companion's LAN API is unauthenticated for the MVP, so it is fenced (LAN-only clients, IP-literal Host header, required `X-Desk-Client` header on POSTs) and exposes only media metadata plus four transport actions.

Never print credentials or tokens to the normal serial log.

---

## Git Rules

This repository exists specifically so development can continue from different machines and different Claude Code sessions.

### Commit strategy

Commit after each verified milestone or meaningful stable checkpoint.

Good examples:

```text
milestone 2: initial 480x480 now playing UI
milestone 3: add stable wifi connection and reconnect
milestone 5: esp32 polls the windows companion for now-playing
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

The remote repository was private during early development and **became public on 2026-10-12** (owner's decision). Everything committed - and the whole history - is therefore world-readable:
never commit Wi-Fi credentials, pairing tokens, the factory flash image, logs, device screenshots of the owner's real media or any third-party artwork (all committed images are synthetic; `firmware/secrets.local.h`, `backup/*.bin`, `logs/` stay git-ignored).
An audit on that day found no secrets, credentials, factory binaries or third-party artwork in the tree or in the history. Known harmless identifiers in the docs: the board's MAC address and private LAN addresses; the commit author e-mail is the owner's.

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

**Seeing the real screen:** (since M8b also scripted input: `--do swipe_right|swipe_left|swipe_up|swipe_down|swipe_diag|swipe_bar|swipe_bar_v|swipe_text|touch_cover|touch_play|touch_shuffle|touch_repeat|key_up|key_down|keyhold_*|<action>_shot` drives a virtual finger through the real LVGL input path or simulates a side-button press; all of these are refused unless the active session is the fake test session - except `swipe_up` / `swipe_down` (they only change WHICH session is shown). `tools/session_test.py` is the full hardware-in-the-loop test of gestures, source switching and per-app volume; `tools/soak_gestures.py` the long soak. `companion --prefer-app python.exe` pins the display to the fake session for such tests - but NEVER pin or send commands while the owner is testing by hand: their key presses and swipes would hit the wrong app.) build with `.\tools\idf.ps1 -DAPP_DEV_TOOLS=1 build`, run the companion with `--dev-tools`, then `tools\venv\Scripts\python.exe tools\screenshot.py --out logs\shot.png` - the board renders its LVGL screen to RAM and sends it over USB serial; the script turns it into a PNG (CRC-checked) that can be viewed. Use this to check layout, colours and artwork instead of guessing. `--tap art` presses the cover (UI only). `--tap play|next|prev` is refused unless the active session is the silent fake test session (`companion/tools/fake_media_session.py`), because it would press the real controls of whatever the owner is playing. Never ship a build with `APP_DEV_TOOLS`.

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

State on 2026-10-12: the MVP works, the Milestone 8 player redesign, the Milestone 8b refinement pass (landscape cards for widescreen art, Spotify logo removed, swipe gestures, per-app volume, side buttons as volume keys)
the Milestone 8c pass (reversed horizontal swipes, vertical media-source switching, every command bound to the displayed session, sharper browser thumbnails)
and the Milestone 8d pass (auto-follow restored after manual swipes, Spotify-only shuffle / repeat) are done
(see `docs/milestones/MVP-media-companion.md`, `M8-player-redesign.md`, `M8b-art-layout-gestures-volume.md`, `M8c-session-switching.md`, `M8d-auto-follow-spotify-options.md`). Next, in this order:

1. Collect the owner's feedback on the refined screen (spacing, sizes, gesture feel, the PWR-key check, fallback font for non-Latin titles - many YouTube titles have them) and fix what they point out.
2. B. M9 Cyberpunk idle screen (shown when nothing is playing; clock; `NO ACTIVE PLAYBACK`) - the owner asked not to start it before the 8b pass was complete.
3. C. M10 full-screen album-art polish.
4. D. M11 first-run Wi-Fi setup (reset gesture = long press of BOOT while running, NOT at power-up), E. M12 companion installer / auto-start / firewall helper, F. M13 pairing + security.
5. A targeted touch stress soak (top band, edges, rapid taps) before the product is finalised.

The birthday intro is dropped - do not work on it.

Before starting: `git status`, `git log --oneline -10`, read `docs/ARCHITECTURE.md` and the latest milestone doc.



---

## Known issues / corrections (added by Claude on 2026-10-12, after this file was provided)

These supersede the statements above they contradict.

- **Milestone 1 touch-test crash.** "No boot loops or reset errors observed" describes the *idle* board only. When the touchscreen was
  exercised, the official-demo + `m1_probe` build crashed and rebooted 4 times (2 memory-access panics, 2 `taskLVGL` stack overflows), always
  within ~0.25 s of a tap in the demo's top tab-bar band; 8 other taps were fine. Root cause is **not yet established**. Do **not** treat the
  demo's LVGL/task configuration as "known-good" for touch. In Milestone 2: own the display init (larger LVGL task stack - the esp_lvgl_port
  default is 7168 B; one draw unit instead of Waveshare's `CONFIG_LV_DRAW_SW_DRAW_UNIT_CNT=2` unless a soak test proves two are safe; no logging
  or relabelling inside LVGL-task timer callbacks), and make a multi-minute touch soak test (screen edges, top band, rapid taps; zero resets in
  the serial log) an acceptance criterion. Evidence, decoded backtraces and candidate causes: `docs/milestones/M1-lcd-touch.md`, "Touch-test addendum".
- Touch coordinate orientation and corner coverage were not independently verified (no corner taps were recorded).
- Never assume the serial port or the ESP-IDF location: use `tools\find-board.ps1`; `tools\idf.ps1` and `tools\serial_capture.py` auto-detect both.
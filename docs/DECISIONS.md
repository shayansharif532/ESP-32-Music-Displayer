# Decision records

## ADR-001 — Framework: **ESP-IDF 5.5.x + LVGL 9 (via `esp_lvgl_port`)**, not Arduino

Status: **recommended** (investigation, 2026-10-11) — becomes final when the owner says PROCEED.

The brief prefers Arduino + LVGL *if it supports everything cleanly*. Arduino **can** drive this board
(Waveshare ships 8 sketches and compiles them in CI with Arduino-ESP32 3.3.11), so this is a judgement about
"cleanly", not "possible". Reasons for ESP-IDF:

1. **The display is a 16-bit parallel RGB panel (ST7701) fed from PSRAM.** Staying stable and smooth while Wi-Fi,
   TLS, JPEG decoding and flash (NVS) writes run concurrently needs settings that live in `sdkconfig`: octal-PSRAM
   timing, cache line size, RGB bounce buffer + double frame buffers (`avoid_tearing`), IRAM-safe LCD ISR,
   optionally PSRAM-XIP and mbedTLS buffers in PSRAM. Arduino-ESP32 ships a *pre-compiled* core, so most of those
   cannot be changed. Waveshare's own Arduino LVGL sketch renders straight into the single live frame buffer
   (`DIRECT_RENDER_MODE`) and the wiki quotes "20~30 fps" — visible tearing is exactly what a product-feel UI must avoid.
2. **Waveshare's maintained, CI-tested path is ESP-IDF**: managed BSP `waveshare/esp32_s3_touch_lcd_4b` 2.0.0, examples built
   against ESP-IDF 5.5.5 and 6.0.2 with LVGL 9.5.0. The Arduino path depends on offline-only bundled copies of
   GFX-Library 1.6.0 / LVGL 9.3.0 / a board header, and the wiki's Arduino text is internally inconsistent (other boards' prose).
3. **Everything else we need is first-class in ESP-IDF**: `esp_http_client`/`esp-tls` with the CA bundle and keep-alive,
   `esp_http_server` + DNS (captive portal), `nvs_flash`, SNTP, mDNS, Wi-Fi events, task pinning (UI on core 1, network on core 0),
   `heap_caps` control for art buffers, and S3-optimised JPEG decoding via `esp_new_jpeg`.
4. Reproducible, pinned toolchain (`dependencies.lock`), headless install via Espressif's `eim` (available on winget).

Cost: bigger toolchain (several GB), slower first build. Accepted; it is automated.
Layout follows the brief: `main`, `board` (pins/hardware config), `display`, `touch`, `ui`, `screens/{now_playing,idle,setup}` (the birthday screen from the original brief was dropped on 2026-10-12),
`wifi`, `spotify`, `storage`.

Build settings carried over from Waveshare's tested example: QIO flash 16 MB, `CONFIG_SPIRAM_MODE_OCT`, 80 MHz PSRAM, 240 MHz CPU,
`CONFIG_ESP32S3_DATA_CACHE_LINE_64B`, FreeRTOS 1 kHz, custom partition table (own `factory` app + NVS, OTA-ready).

## ADR-003 — Windows media-session companion replaces the Spotify Web API (2026-10-12) — **current**

Status: **accepted** (owner decision). Supersedes ADR-002 as the primary architecture.

Context: the device will only ever be used with the recipient's Windows PC. The Spotify route needs a Spotify developer app, a **Premium**
owner, the 5-user cap, OAuth/PKCE with an awkward redirect on a screen-only device, token storage and a TLS client - and it only covers Spotify.

Decision: a lightweight **Windows companion** reads the active **media session** (`GlobalSystemMediaTransportControlsSessionManager` /
`Windows.Media.Control`) and exposes it over a small **HTTP API on the LAN**; the ESP32 polls it and sends control commands. Details:
[ARCHITECTURE.md](ARCHITECTURE.md).

Why: covers Spotify desktop, YouTube / YouTube Music in browsers, VLC, Apple Music, ...; no accounts / tokens / Premium; plain HTTP on the
LAN is simple and fast on the ESP32; the PC pre-normalises artwork to a baseline 300x300 JPEG; transport commands behave like media keys.

Consequences / accepted trade-offs: the PC must be on, on the same network and running the companion; the API is unauthenticated on the
LAN (mitigated by LAN-only / Host-header / custom-header rails now, pairing token later); the Windows Firewall will ask once to allow the
companion's port; non-Latin titles need a fallback font on the device (known issue). Companion language: Python + `winrt-*` + Pillow
(already available on the dev PC, quick to iterate; packaged as a single `.exe` with PyInstaller for the recipient - Milestone 12).

Old Spotify code is kept, **not built**, in [`legacy/`](../legacy/README.md) (commit `db51d85`).

## ADR-004 — Per-app volume through Core Audio sessions, side buttons as volume keys, art kept in its own shape (2026-10-12) — **current**

Status: **accepted** (owner request after reviewing the M8 redesign on the real display). Details and evidence: `docs/milestones/M8b-art-layout-gestures-volume.md`.

* **Volume is per application, never the master volume.** `Windows.Media.Control` has no volume, so the companion uses the Core Audio session API (`ISimpleAudioVolume`, via `pycaw`/`comtypes`) on the audio
  sessions owned by the displayed app's processes. The display names the app it shows in every volume request so the target always matches the screen. Chromium browsers mix all tabs in one session
  (whole-browser volume) and Firefox cannot be matched - both documented limits, accepted for a "best effort" browser story.
* **The side buttons are the volume keys**: PWR (AXP2101, polled over I2C because neither the key nor the PMIC IRQ reaches an ESP32 pin) = up, BOOT (GPIO0) = down. No permanent on-screen volume control.
  Consequence: "hold BOOT at power-up" can no longer be the Wi-Fi-reset gesture in Milestone 11 (it is the ROM download-mode gesture anyway); use a long press while running.
* **Artwork keeps its aspect ratio**: the companion renders the cover at the size it is drawn (square or landscape card) instead of letterboxing onto a square, the UI lays out around it.
* **App logos are removed structurally, never by fixed crops**: only alpha composites with exactly one solid block plus sparse bands are split (Spotify's 300x300 RGBA composite); everything else is untouched.
* **Gestures**: horizontal swipe with distance / direction / time rules and a guard that a travelled press is never a tap; the progress bar never starts a swipe. *(The direction mapping was reversed
  by ADR-005: left = next, right = previous.)*

## ADR-005 — Media-source switching by vertical swipe; the display, its controls and its volume always target one explicit session (2026-10-12) — **current, except the "pinned" rule (superseded by ADR-006)**

Status: **accepted** (owner request after using M8b). Details and evidence: `docs/milestones/M8c-session-switching.md`.

* **Gestures**: finger **left = next track, right = previous track** (reversed from M8b); finger **up = next media source, down = previous source** (a source = one Windows media session = one app).
  Same thresholds and safeguards as before (>= 72 px along the axis, a 44 px cross-axis limit at any time, 2:1 dominance, < 1 s, a travelled press is never a tap, the progress bar never starts a swipe).
  A diagonal strays on both axes and is neither. With fewer than two sources a vertical swipe does nothing at all.
* **The companion owns the session model** (`companion/desk_media_companion/sources.py`, pure logic with unit tests; `media.py` applies it to the live GSMTC sessions):
  * the *swipe list* ("ring") = sessions that are readable, not closed and carry a title or artist (playing **and paused** ones; dead / metadata-less sessions such as `PSExpressCore.exe` are left out),
    plus whatever is on screen; fixed alphabetical order by app name, wrapping at both ends, so repeated swipes walk the same ring regardless of playback state;
  * **auto** (initial): follow the best session - the system's current session when it plays, any playing session, the app that played last, the current session, the rest;
  * **pinned**: a swipe pins the display to that session. It stays there - another app starting to play does not take over - for as long as the session exists; when it disappears the pin is dropped and
    auto takes over (`POST /control/source-auto` and a companion restart also return to auto). Documented consequence: a pinned session that is paused keeps the screen even if something else plays.
* **Every command names the session on screen** (`app=` on every `/control/*`, volume and source request): it goes to exactly that session, and is *refused* ("that media session is gone") if it no longer
  exists - never redirected to whatever else is active. This closes the class of bug seen in the MVP (controls on a different session than the one displayed) for good, and it is what makes the side-button
  volume follow a source switch immediately. The source switch itself is relative to the app on screen (`from=`), and its answer carries the new `/now-playing` snapshot so the display switches at once.
* **UI**: no permanent widgets. A faint disc with an up / down chevron drifts in the swipe direction, then a translucent pill (shared with the volume pill) names the new source (`Spotify`, `Chrome`) with
  small position dots, ~1.1 s. The existing status line already shows the source name.
* **Artwork quality** (same pass): Chromium asks the OS for artwork at a fixed "desired 150 px" (`kMinImageSize = 71`, `kDesiredImageSize = 150` in `system_media_controls_notifier.cc`), Windows passes exactly one
  thumbnail stream through unchanged, and GSMTC exposes no URL / larger size / other image, so a browser thumbnail cannot be improved by asking more nicely. PC-side up-scaling was improved (edge-only, halo-limited,
  strength scaled by enlargement; Spotify-sized enlargements are bit-identical to before). **No browser extension**: it is the only route to a real high-resolution image (it can read the page's own
  `MediaSession` artwork list) but needs per-browser installation on the recipient's PC; recorded as an option in the M8c doc, deliberately not built.

## ADR-006 — Manual source selection is temporary (AUTO / MANUAL_TEMP); Spotify shuffle / repeat through Windows' own session controls (2026-10-12) — **current**

Status: **accepted** (owner request after using M8c). Details and evidence: `docs/milestones/M8d-auto-follow-spotify-options.md`. Supersedes the "pinned until the session disappears" rule of ADR-005.

* **Auto-follow is the long-term mode; a swipe never disables it.** The companion's selection is an explicit two-state machine (`sources.Selector`, pure, 43 unit tests in test_sources.py):
  * `AUTO`: follow the playing session that **started playing most recently** (ties: the system's current session); nothing playing: the app that played last, then the current session.
  * `MANUAL_TEMP` (entered by a vertical swipe): the chosen session is shown while the user deals with it, and the display returns to `AUTO` when (a) **another** session starts playing (a pause -> play transition or a new playing session appearing),
    (b) the selected session disappears, or (c) the selected session is *not* playing, another one is, and the user has not touched the selected session for **20 s** (`MANUAL_IDLE_RETURN_S`) - the only timeout, and only for the case
    that no event can ever arrive ("Spotify just keeps playing"). A selected session that starts playing (the user pressed play on it) stays; a swipe that lands on what AUTO would show anyway is simply AUTO.
    Brief `changing` / `opened` states (track changes) are not transitions. Interactions that restart the idle timer: any control, seek, shuffle / repeat or volume key aimed at the selected session, and another swipe.
  * A background watcher (every 0.4 s, a few ms per look) keeps the model current even when the display is not polling, so transitions are never missed.
* **Commands still always target the session on screen** (ADR-005): the display names it in every request, the companion refuses it if it is gone. When auto-follow switches the display, the next poll (<= 1 s) moves the screen and
  from then on every control, seek and volume key follows the new session.
* **Shuffle and repeat are Spotify-only and need no Spotify account.** Windows' GSMTC exposes them for Spotify desktop: `is_shuffle_active`, `auto_repeat_mode` (none / list / track), the enabled flags, and
  `try_change_shuffle_active_async` / `try_change_auto_repeat_mode_async` are accepted by Spotify (state read back ~100 ms later). All three repeat modes are real (off, playlist / album, track). No OAuth / Web API.
  Chrome, Edge, VLC ... report none of this. "Spotify" is decided by the session's **app identity** (`Spotify.exe` or the Store package `SpotifyAB.SpotifyMusic_...`), never by metadata, and the companion refuses the commands for every other app.
* **UI**: two small buttons flank the transport row only while Spotify is shown (shuffle left of previous, repeat right of next); subtle when off, Spotify green with a dot when on, a "1" on the repeat icon for repeat-track, dimmed when Spotify
  does not allow the change; previous / play / next are exactly where they were; hidden in full-screen mode and for every other app.

## ADR-007 — Customisable idle screen: the PC owns the configuration, the display caches it (2026-10-12) — **current**

Status: **accepted** (owner request). Details and evidence: `docs/milestones/M9-idle-screen.md`. It replaces the earlier plan for Milestone 9 (a Cyberpunk-styled idle screen) with a **generic, user-customisable product feature**; anything personal (a photo, a name, a fandom theme) is a local asset the user chooses on their own PC.

* **Three screens, decided by what the companion reports.** Boot / Wi-Fi / searching / PC unreachable -> the player screen with its connection message (never the idle screen).
  PC connected and something plays -> the player. PC connected and **nothing plays** (no session, or only paused / stopped ones) -> the idle screen.
  The companion adds `any_playing` to `/now-playing` (some *usable* session is playing - not just the displayed one, so a paused source swiped to manually while another plays does not blank the display).
* **Leaving idle is immediate** (the next poll, <= 1 s, normal auto-source-detection then picks the session); **entering idle waits** for the user's delay (default 4 s, 2-60 s) of continuous "nothing plays", so the gap between two tracks or a
  browser changing video never flashes it. Nothing has played since connecting -> idle at once. Two LVGL screens, a 350 ms fade between them (`lv_screen_load_anim`, no layers / blur / extra buffers).
* **Tap-to-peek.** A paused session would otherwise be unreachable (pause on the display, 4 s later it is idle, no play button). Tapping the idle screen while a session exists shows the player for 30 s (touching the player keeps it up the same way); with no session a tap does nothing.
* **The companion owns the configuration** (`companion/desk_media_companion/idle.py`): theme (Minimal / Dark / Neon / Photo), accent colour, show clock + 12/24 h, one custom text line, the delay, and for Photo a background picture with Fill / Fit / Blur Fill.
  It lives in `%LOCALAPPDATA%\DeskMediaCompanion\idle\` (outside any repository; `.gitignore` also blocks the file names should they land in a checkout). It is edited **only in-process** (tray icon -> Settings window, `ui.py`) - there is no HTTP route that changes it, so nothing on the LAN can.
  A single `version` number changes with every real change.
* **The display caches, the PC does not resend.** Every `/now-playing` answer carries `idle_v`. When it differs from the version stored on the display (`!=`, so a reset companion also resyncs) the display fetches `GET /idle` (settings JSON incl. the picture's size + CRC-32) and, only if the CRC changed, `GET /idle/image`.
  Persistence: settings in **NVS**, the picture in a new **`idle_img` 1 MB flash partition** (header sector written last, so an interrupted write means "no picture"; CRC re-checked on every load). A reboot of either side therefore needs no transfer.
  Partition table change (documented as required by CLAUDE.md): `idle_img` (1 MB, data/0x40) was carved out of the unused 7 MB `storage` (now 6 MB, nothing references it); `nvs` and `factory` keep their offsets, so Wi-Fi/NVS data survive a flash.
* **All picture work is on the PC.** JPG / PNG only (<= 25 MB, <= 89 MP, sides 16..20 000 px, format checked, EXIF rotation, transparency matted on black); cropped / fitted / blurred to exactly 480x480 and encoded as a **baseline JPEG <= 200 KB** (the ESP32's decoder is baseline-only). The display never sees an original.
  On the display the JPEG is decoded once to RGB888 and **dithered** to RGB565 in PSRAM (smooth blurred areas must not band), kept as one image LVGL only blits; buffers are swapped in under the LVGL lock, never rewritten on screen.
  Built-in themes are dithered gradients generated on the display (Neon adds an accent-coloured glow), so no per-theme image is stored or sent.
* **Fallbacks, never a broken screen.** A missing / corrupt / undecodable / wrongly-sized picture -> the built-in **Dark** theme; an undecodable picture is not downloaded again for the same CRC (no retry loop); a failed transfer is retried with a back-off (5 s, 15 s, then every minute); a corrupt stored picture is re-requested.
* **Clock.** `clock_ms` = the PC's local wall-clock time (UTC + offset, ms since the epoch) in every `/now-playing` answer; the display re-bases a software clock on each answer and ticks a 1 s timer only while the idle screen is visible, rewriting the label only when the minute changes. DST needs nothing on the display.
  The big digits are a generated font (`tools/gen_clock_font.py`: Montserrat Medium 128 px, digits + colon, tabular), since LVGL's built-in Montserrat stops at 48 px. The custom line is sanitised to printable ASCII on the PC (and again on the display): the fonts have nothing else.
* **Rejected:** storing the picture as raw RGB565 (460 KB, 4x the transfer and flash wear for no visible gain); decoding on every idle entry (a 150-300 ms hiccup at the moment of the fade); settings over an HTTP POST (an unauthenticated LAN could change them before pairing exists, Milestone 13); a full-screen LVGL layer / blur for photo legibility (a plain 30 % dim object is enough);
  animating the idle screen (it must be nearly free next to Wi-Fi and the player).

## ADR-002 — Spotify: rules that shape the design (verified against Spotify's own pages, 2026-10-11) — **OBSOLETE, superseded by ADR-003**

*Kept for the record in case a Spotify-API mode is ever revived.*

Sources: developer.spotify.com — *February 2026 Web API Dev Mode Changes (Migration Guide)* and *Redirect URIs*.

* **Premium.** Every **Development Mode** app needs its **owner** (the Spotify account that created the app on the developer
  dashboard) to have an active **Premium** subscription — if it lapses, the app stops working. Separately, the playback *control*
  endpoints (play / pause / next / previous / seek) only work for Premium **listeners**; reading "now playing" works for free accounts.
* **Users / Client IDs.** A Development Mode app is limited to **5 users per app** (still applies). The Feb 2026 change allowed only
  1 Client ID per developer, but Spotify's **July 2026 update raised that to 25 Client IDs per developer account** (confirmed by the
  owner), so creating one dedicated app per recipient is fine. The recipient's Spotify account must be added under *User Management*
  in that app (name + the e-mail of their Spotify account) before they can authorise.
* **Endpoints.** The February 2026 changes removed/changed library, batch-lookup, browse and playlist-item endpoints. The **Player**
  endpoints we use (`GET /me/player`, `GET /me/player/currently-playing`, play/pause/next/previous/seek, devices) are **not** in the
  removed/changed list. We re-verify live in Milestone 5.
* **OAuth.** Authorization Code **with PKCE** is the only flow we implement — **never Implicit Grant** (owner's explicit requirement;
  Spotify also removed it in Nov 2025). No client secret ever on the device. Redirect URIs must be
  **HTTPS**, except loopback IP literals (`http://127.0.0.1[:port]`, `http://[::1]`); `localhost` and plain-HTTP LAN addresses
  are rejected; matching is exact.
* **Consequences for the design**
  * The Spotify **Client ID is entered at setup time and stored in NVS** (never compiled in), so no secret and no shared credential
    lives in the firmware or the repo.
  * `http://<device-ip>/callback` is **not** an allowed redirect URI. Plan: a tiny static **HTTPS "bridge" page** (free static hosting,
    e.g. GitHub Pages) registered as the redirect URI. It receives `?code=…` and bounces the phone to the device's local setup page, which
    does the PKCE token exchange. Fallback with no hosting: register `http://127.0.0.1/callback`, and paste the resulting URL's `code`
    into the device's setup page.
  * Only the **refresh token** (+ Client ID) is persisted; access tokens live in RAM and are refreshed automatically; nothing sensitive is logged.
  * If the listener is not Premium, or no device is active, the UI shows a clear state (controls disabled / "No active device — open
    Spotify on your phone") instead of failing silently.
* **Open question for the owner (needed before Milestone 5):** whose Spotify developer account owns the app (needs Premium)?
  Either the gift-giver creates it and adds the recipient to the 5-user list, or the recipient creates their own.

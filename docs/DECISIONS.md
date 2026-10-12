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
Layout follows the brief: `main`, `board` (pins/hardware config), `display`, `touch`, `ui`, `screens/{now_playing,idle,setup,birthday}`,
`wifi`, `spotify`, `storage`.

Build settings carried over from Waveshare's tested example: QIO flash 16 MB, `CONFIG_SPIRAM_MODE_OCT`, 80 MHz PSRAM, 240 MHz CPU,
`CONFIG_ESP32S3_DATA_CACHE_LINE_64B`, FreeRTOS 1 kHz, custom partition table (own `factory` app + NVS, OTA-ready).

## ADR-002 — Spotify: rules that shape the design (verified against Spotify's own pages, 2026-10-11)

Sources: developer.spotify.com — *February 2026 Web API Dev Mode Changes (Migration Guide)* and *Redirect URIs*.

* **Premium.** Every **Development Mode** app needs its **owner** (the Spotify account that created the app on the developer
  dashboard) to have an active **Premium** subscription — if it lapses, the app stops working. Separately, the playback *control*
  endpoints (play / pause / next / previous / seek) only work for Premium **listeners**; reading "now playing" works for free accounts.
* **Users.** A Development Mode app is limited to **5 users** (new apps) and **1 Client ID per developer** (a July 2026 note raises it to 25).
  The friend's Spotify account must be added under *User Management* in the app (name + the e-mail of their Spotify account) before
  they can authorise.
* **Endpoints.** The February 2026 changes removed/changed library, batch-lookup, browse and playlist-item endpoints. The **Player**
  endpoints we use (`GET /me/player`, `GET /me/player/currently-playing`, play/pause/next/previous/seek, devices) are **not** in the
  removed/changed list. We re-verify live in Milestone 5.
* **OAuth.** Authorization Code **with PKCE** (no client secret ever on the device; implicit grant is gone). Redirect URIs must be
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

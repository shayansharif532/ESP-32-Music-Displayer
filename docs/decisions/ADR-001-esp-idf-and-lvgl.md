# ADR-001 — Framework: **ESP-IDF 5.5.x + LVGL 9 (via `esp_lvgl_port`)**, not Arduino

Status: **accepted** (recommended by the 2026-10-11 investigation; adopted when Milestone 1 was built and in use ever since).

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
Layout follows the brief: `main`, `board` (pins/hardware config), `display`, `touch`, `ui`, `screens/{now_playing,idle,setup}` (a one-off intro screen from the original brief was dropped on 2026-10-12),
`wifi`, `spotify`, `storage`.

Build settings carried over from Waveshare's tested example: QIO flash 16 MB, `CONFIG_SPIRAM_MODE_OCT`, 80 MHz PSRAM, 240 MHz CPU,
`CONFIG_ESP32S3_DATA_CACHE_LINE_64B`, FreeRTOS 1 kHz, custom partition table (own `factory` app + NVS, OTA-ready).

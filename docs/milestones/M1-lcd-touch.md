# Milestone 1 — official Waveshare LVGL demo on the real board

Goal: build Waveshare's official LVGL demo for *this* board, flash it, confirm LCD + touch, watch the serial console.
Date: 2026-10-11. Board: ESP32-S3-Touch-LCD-4B on `COM5` (native USB-Serial/JTAG). Scope limited to Milestone 1 by the owner.

**Status (updated 2026-10-12): display OK (owner-confirmed); touch input works; BUT the demo + sidecar build crashed on touches
in the demo's top tab-bar area — see [Touch-test addendum](#touch-test-addendum-2026-10-12). Open item for the start of Milestone 2.**
The "boots cleanly, no resets" statements below describe the *idle* board (100 s+ of observation) and remain true; they do not cover
touch interaction.

## What was done

| Step | Result |
|---|---|
| Install ESP-IDF | **v5.5.5** (the version Waveshare's CI validates) via Espressif `eim` 0.20.0; GCC 14.2.0 (`esp-14.2.0_20260121`), CMake 3.30.2, Ninja 1.12.1, ccache 4.12.1. Install took ~8 minutes, ~7 GB on `C:` |
| Configure | `set-target esp32s3`; component manager pulled LVGL **9.5.0**, Waveshare BSP **2.0.0**, `esp_lvgl_port` 2.9.0, `esp_lcd_st7701` 2.0.3, `esp_lcd_touch_gt911` 1.2.1, `esp_io_expander_tca9554` 2.0.3, `esp_lcd_panel_io_additions` 1.0.1~1 (pinned in `firmware/dependencies.lock`) |
| Build #1 — pristine demo (`-DM1_PROBE=0`) | 2061 build steps, **0 errors**; app **853,904 B** in an 8 MB slot (90 % free); bootloader 22,288 B |
| Flash #1 | bootloader @0x0, partition table @0x8000, app @0x10000 written, each **"Hash of data verified"**; no chip erase |
| Boot #1 | clean (see below) |
| Build #2 — demo + `m1_probe` sidecar | one compile error in my sidecar (wrong LVGL event name, fixed against the shipped header) then success; app 855,472 B |
| Flash #2 / boot #2 | verified the same way; sidecar reports `overlay active: display 480x480` |

Pre-flight before the first write: chip content re-verified equal to the factory backup (`verify-flash` digest matched).

## Boot log of the new firmware (from the ROM onward)

```
ESP-ROM:esp32s3-20210327   rst:0x15 (USB_UART_CHIP_RESET),boot:0x2b (SPI_FAST_FLASH_BOOT)
I boot: ESP-IDF v5.5.5 2nd stage bootloader      I qio_mode: Enabling default flash chip QIO
I boot.esp32s3: Boot SPI Speed : 80MHz   SPI Mode : QIO   SPI Flash Size : 16MB
I boot: ##  nvs  0x9000/0x6000 · phy_init 0xf000/0x1000 · factory 0x10000/0x800000 · storage(spiffs) 0x810000/0x700000
I boot: Loaded app from partition at offset 0x10000
I octal_psram: vendor 0x0d (AP) gen 3, 64 Mbit, good-die Pass, VCC 3V     I esp_psram: Found 8MB PSRAM device, Speed: 80MHz
I mmu_psram: Read only data copied and mapped to SPIRAM / Instructions copied and mapped to SPIRAM
I esp_psram: SPI SRAM memory test OK      I heap_init: 320 KiB + 21 KiB + 32 KiB internal; 7360K PSRAM added to heap
I LVGL: Starting LVGL task
I ESP32-S3-Touch-LCD-4B: Install 3-wire SPI panel IO      I st7701: version: 2.0.3
W st7701_rgb: The 36h / 3Ah command has been used and will be overwritten by external initialization sequence   (expected, per BSP)
I GT911: TouchPad_ID:0x39,0x31,0x31 ("911")   TouchPad_Config_Version:79
I main_task: Returned from app_main()          I m1_probe: overlay active: display 480x480, touch dot + serial trace enabled
```

Steady state (100 s observed, sampled every 5 s): **16-17 LVGL redraw cycles / 5 s**, internal RAM free **259.7 KB** (largest block 176 KB),
PSRAM free **6,896,136 B** — all flat, i.e. no leaks, no resets, no errors, no watchdog.

## What the serial log proves — and what it cannot

* Proven: the board-specific stack (3-wire-SPI ST7701 init through the TCA9554, RGB bus, bounce buffer, LVGL task, GT911 read) comes up
  without an error on this unit, in the exact flash/PSRAM configuration we will keep (QIO 80 MHz flash, octal 80 MHz PSRAM).
* Not provable from a PC: that the *pixels look right* and that *touch lands where the finger is*. That needs eyes and a finger.
  The sidecar makes it checkable: touch down / move / up with x,y go to the serial log, and a yellow dot with "x,y" follows the finger.

## Owner checklist (needs a human)

1. Screen shows the LVGL "widgets" dashboard (tabs, sliders, switches, small FPS/CPU counter bottom-right); colours correct
   (nothing red<->blue swapped), image stable (no shifting/flicker/snow), backlight on.
2. Touch: tap and drag; a yellow dot with x,y follows the finger; the four corners read about (0,0) (479,0) (0,479) (479,479).
   Widgets respond (switches toggle, sliders move, tabs change).

## Findings worth keeping

* Waveshare's `sdkconfig.defaults` references `LV_USE_DEMO_TRANSFORM`, which LVGL 9.5 no longer has — harmless warning.
* BSP defaults: **1 frame buffer, no tear avoidance** (`CONFIG_BSP_LCD_RGB_BUFFER_NUMS=1`) — fine for the demo; Milestone 2 switches to
  2 frame buffers + `avoid_tearing` for smooth animation.
* The factory image used XIP-from-PSRAM for 3.6 MB of assets; this build still copies code + rodata to PSRAM (Waveshare's settings) and leaves
  ~6.9 MB of PSRAM free — plenty for album art.
* Console on native USB needs DTR; reset/boot-mode selection is via RTS/DTR edges; see README "Notes learned the hard way".
* Windows PowerShell shell guard: scripts must not `Remove-Item "$var\file"` with an unresolved variable (blocked as a system-path delete) —
  use unique log names instead.

## Touch-test addendum (2026-10-12)

While the owner exercised the touchscreen, a read-only serial listener (`tools/serial_capture.py`, started 23:53 on 2026-10-11)
recorded the session. The build on the board was **official demo + `m1_probe` sidecar** (the pristine `-DM1_PROBE=0` build was
never touched by a finger, so it has **not** been observed under touch).

**Touch input works.** 46 events (12 presses, 25 drag samples, 9 releases) over a 27 s window; reported x spans 103–414 and
y spans 7–319 (of 0–479); drags produce smooth monotonic paths (e.g. (136,319) -> (192,131)). *Not proven:* corner coverage / axis orientation — no
corner touches were recorded; the owner reports the rendered display looks correct (CLAUDE.md, 2026-10-12).

**The board crashed on touches in the top tab-bar band.** 4 of the 12 presses were followed within 0.1–0.25 s by a crash + automatic reboot
(`rst:0xc RTC_SW_CPU_RST`); all 4 were in the top 16 px at x = 248–308 (the widgets demo's tab buttons). The 8 other presses — including
(222,24) and (414,9) and everything lower on the screen — did not crash.

| # | Listener time | Press | Crash |
|---|---|---|---|
| 1 | +1235.8 s | (264, 7) | `Guru Meditation Error: Core 0 panic'ed (StoreProhibited)`, EXCVADDR 0x000609e0 |
| 2 | +1239.6 s | (264,16), 0.2 s after a reboot | `Core 1 panic'ed (LoadProhibited)`, EXCVADDR 0x00000008; Core 0 shows the same state as #1 |
| 3 | +1244.5 s | (308,11) | `***ERROR*** A stack overflow in task taskLVGL has been detected` |
| 4 | +1251.3 s | (248,15) | `***ERROR*** A stack overflow in task taskLVGL has been detected` |

Backtraces decoded against the exact ELF (`xtensa-esp32s3-elf-addr2line`, ELF SHA-256 prefix `74a836b17`):

* #1 / core 0 of #2: `vPortYieldFromInt` <- `_frxt_int_exit` (FreeRTOS interrupt-exit context switch) — typical symptom of a corrupted task stack/TCB.
* #2 / core 1: `xTaskGenericNotify` <- `lv_thread_sync_signal` <- `lv_draw_dispatch_request` <- `render_thread_cb` <- `prvRunThread`
  (`lv_freertos.c:293`, `lv_draw.c:312`, `lv_draw_sw.c:374`): **LVGL's second software-render thread**.
* #3 / #4: `vApplicationStackOverflowHook` <- `vTaskSwitchContext` (canary check, `CONFIG_FREERTOS_CHECK_STACKOVERFLOW_CANARY=y`).

Relevant configuration: Waveshare's defaults set `CONFIG_LV_DRAW_SW_DRAW_UNIT_CNT=2` (LVGL renders on two threads) and `esp_lvgl_port` 2.9.0's
`ESP_LVGL_PORT_INIT_CONFIG()` gives `taskLVGL` a **7168-byte** stack in internal RAM; `CONFIG_ESP_MAIN_TASK_STACK_SIZE=3584`.

**Root cause: not yet established.** Candidates, most to least likely: (a) the demo's tab-switch animation + shadows need more than 7 KB of
LVGL-task stack, made worse by the sidecar logging and relabelling inside LVGL timers (they run on `taskLVGL`); (b) a race in LVGL 9.5's
two-thread software renderer with FreeRTOS notifications; (c) something specific to the official BSP's touch/LCD glue.
Not yet run (needs a flash, so deliberately not done during the GitHub-sync task): flash the pristine build and hammer the top band;
rebuild with `CONFIG_LV_DRAW_SW_DRAW_UNIT_CNT=1`; rebuild with a larger LVGL task stack.

**Consequence for Milestone 2.** Our own `display` layer must not inherit the demo's settings blindly: start LVGL through
`bsp_display_start_with_config()` (or our own init) with a larger `task_stack` (16 KB class), prefer one draw unit unless a soak test proves two are safe,
keep logging out of LVGL-task callbacks, and make a **touch soak test an acceptance criterion** (tap everywhere including the screen edges and
the top band, and rapid repeated taps, for several minutes with zero resets in the serial log).

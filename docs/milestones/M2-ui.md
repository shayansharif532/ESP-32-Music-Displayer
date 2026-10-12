# Milestone 2 — custom 480x480 Now Playing UI

Goal: replace the Waveshare demo with our own player screen (no Spotify yet) and make the display layer safe under touch.
Date: 2026-10-12. Commit: `d575896`.

## What was built

| Item | Where |
|---|---|
| Screen: album-art box (placeholder), title, artist, seek slider, elapsed/total, prev / play-pause / next, tap-art full-screen toggle, one status line | `firmware/components/ui/` |
| Thread-safe UI API (`ui_show_track`, `ui_set_progress`, `ui_set_art`, `ui_show_idle`, `ui_set_status`); callbacks only queue, never block | `ui.h` |
| Local progress interpolation between polls (LVGL timer, no logging/allocation) | `ui.c` `tick_cb` |
| Display start-up owned by us: 24 KB LVGL task stack, priority 4, pinned to core 1; backlight explicitly on | `main/main.c`, `main/app_config.h` |
| LVGL config: **one** software draw unit, no demos, no perf/sysmon overlay, fonts 12–48 | `firmware/sdkconfig.defaults` |
| LVGL partial-render buffer 50 rows (BSP default 100) to save internal RAM | `CONFIG_BSP_DISPLAY_LVGL_BUF_HEIGHT` |
| Health line every 10 s: heap, PSRAM, LVGL stack headroom, touch-press count | `main/health.c` |
| UI-only demo player (used when no developer credentials are built in) | `main/demo_player.c` |

## Answer to the Milestone 1 touch-crash question

Quick stability gate (owner request, 2026-10-12): the **pristine** Waveshare demo (no sidecar, `-DM1_PROBE=0`) was flashed and the serial log
recorded for 7 minutes (`logs/pristine-stress-1.log`, git-ignored): **no crash, no reboot** after boot. That build prints nothing on touch, so the
log cannot show how many taps happened - the owner ran the stress test and reported no problem. Conclusion used: the
sidecar's logging/relabelling inside LVGL-task timers on top of the demo's 7 KB stack was the likely trigger; not proven further, per the owner's
instruction not to spend more time on the demo.

Evidence that supports the stack theory: with a 24 KB stack the LVGL task's *minimum free* stack is ~20.2 KB, i.e. it uses ~4.3 KB at idle with this UI,
and the demo's widgets screen (shadows, tab animation) needs far more than the 7,168 B the stock config gave it.

Our config keeps the safer settings regardless (large stack, one draw unit, no overlays). The touch soak below is the acceptance test.

## Boot / resource numbers (M2 build, idle)

- boot clean; internal RAM free 273,935 B (largest block 192,512 B); PSRAM free 7,025,928 B
- LVGL task stack minimum free 20,244 B of 24,576 B
- 10 minutes (`logs/m2-soak.log`): heap numbers flat, no reset, no panic

## Touch soak - status

The serial soak recorded 10 minutes with **only 2 touch presses**: the heap/stack numbers are flat and nothing reset, but it is **not yet a
real touch soak** (it needs several minutes of varied taps: top band, edges, rapid taps, slider drags, art toggle). To be repeated on the
network build; the touch counter in the health line proves the taps happened.

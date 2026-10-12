# Provenance of the Milestone-1 firmware

`firmware/` starts from Waveshare's official ESP-IDF example **`02_lvgl_demo_v9`** for this exact board
(<https://github.com/waveshareteam/ESP32-S3-Touch-LCD-4B>, `examples/esp-idf/02_lvgl_demo_v9`, repo commit
`62c38908a15f1ce45f18b76caed7436cd9cbc22a`, Apache-2.0 per that repository's `LICENSE.txt`). It uses Waveshare's
official board-support component `waveshare/esp32_s3_touch_lcd_4b` 2.0.0 and LVGL 9.5.0, both fetched by the ESP-IDF
component manager (not vendored; exact versions are pinned in `dependencies.lock`).

| File | State | SHA-256 (first 16 hex) of Waveshare's original |
|---|---|---|
| `CMakeLists.txt` | **unchanged** | `ccfac0a0237de12f` |
| `partitions.csv` | **unchanged** | `4e9d732f910c8e4f` |
| `main/CMakeLists.txt` | **unchanged** | `22794eb6855536f9` |
| `main/idf_component.yml` | **unchanged** | `59ee892d34dd1a27` |
| `main/main.c` | **unchanged** | `24f4fcf6762933f7` |
| `sdkconfig.defaults` | Waveshare's lines unchanged (original `0ed988a06ca168b9`); one delta appended below a banner: console on the native USB-Serial/JTAG port | |
| `components/m1_probe/` | **ours** — diagnostic sidecar (touch trace + heap/redraw stats), removed at Milestone 2 | |

Build the pristine Waveshare demo with `-DM1_PROBE=0` (the sidecar becomes an empty component); the default build adds
the sidecar. Because the sidecar attaches itself through a constructor, Waveshare's `main/` never needs to change.

# Hardware reference — Waveshare ESP32-S3-Touch-LCD-4B (SKU 32052, "Smart 86 Box")

The reference (and currently only supported) board of the DeskMedia Display project. See [architecture.md](architecture.md) for how the
firmware uses it and [development.md](development.md) for building and flashing.

Everything below was cross-checked from independent sources. Evidence tags:

* **[board]** observed on *this* unit (esptool, eFuses, boot log of the factory firmware)
* **[audit]** Waveshare's schematic-backed `hardware/pin-audit.md` (official repo)
* **[bsp]** Waveshare ESP-IDF BSP `waveshare/esp32_s3_touch_lcd_4b` v2.0.0 source
* **[ard]** Waveshare Arduino board header `Mylibrary/pin_config.h`

> Waveshare's own repo says physical-board validation of the pin audit is still "pending". We therefore
> treat the map as *very likely correct* (three sources agree, and the factory firmware boots with it) and
> prove it on the real panel/touch in Milestone 1. Do not assume values from other Waveshare displays —
> the wiki's Arduino prose for this board contains copy-paste text from other boards (SH8601 / CO5300 /
> QSPI) that does **not** apply; the RGB + ST7701 + GT911 description below does.

## Identity & silicon [board]

| Item | Value |
|---|---|
| Module | ESP32-S3-WROOM-1-**N16R8** (wiki, schematic) |
| Chip | ESP32-S3 (QFN56) rev v0.2, dual-core LX7 240 MHz, Wi-Fi 4 + BLE 5, LP core |
| MAC | `xx:xx:xx:xx:xx:xx` (redacted; unique per unit) |
| Flash | 16 MB, **quad** (4 data lines), 3.3 V (VDD_SPI forced to 3.3 V by eFuse); JEDEC mfg 0x46 / dev 0x4018; app uses QIO |
| PSRAM | 8 MB **octal**, vendor AP_3v3, 85 °C grade; passes the SPI SRAM memory test at boot. Waveshare's official example runs it at 80 MHz (`CONFIG_SPIRAM_MODE_OCT` + `CONFIG_SPIRAM_SPEED_80M`) |
| Security | secure boot OFF, flash encryption OFF, JTAG/USB-JTAG/download mode all enabled, eFuses untouched |
| USB | two USB-C ports. **Native USB** (ESP32-S3 USB-Serial/JTAG, `VID_303A PID_1001`, enumerates as a COM port whose number differs per computer: `toolsind-board.ps1` prints it) is the one in use. The second port is a USB-to-UART bridge (UART0). Native port console only streams while the host asserts **DTR** — `idf.py monitor` does this; a raw serial tool must too |
| Buttons | **BOOT** (GPIO0) and **PWR** (AXP2101 PWRON). There is **no RESET button**. Both are the two "side buttons" on one board edge; this project uses them as volume down (BOOT, lower) / volume up (PWR, upper) - see "Side buttons" below. |

## Peripherals

| Function | Part | Bus / address | Notes |
|---|---|---|---|
| Display | 4" IPS 480x480, **ST7701**, 16-bit parallel **RGB565** | RGB (LCD_CAM) + init over 3-wire SPI bit-banged through the TCA9554 | 60 Hz, pixel clock 16 MHz [bsp] |
| Touch | **GT911**, 5-point | I2C **0x14** [board: "Touch 0x14 found"] | address 0x14 chosen by holding INT low while releasing RST |
| IO expander | **TCA9554PWR** | I2C 0x20 [audit/bsp/ard] | see EXIO table |
| PMIC | AXP2101 | I2C 0x34 [audit] | the BSP never touches it and Waveshare's BSP-only LVGL demo still lights the panel; the factory firmware does init it ("Init PMU SUCCESS"). To be confirmed in Milestone 1 |
| IMU | QMI8658 | I2C 0x6B [audit] | |
| RTC | PCF85063 | I2C (0x51 per datasheet) | battery-backed only if a cell is attached |
| Audio | ES8311 codec + ES7210 ADC + 8 ohm/2 W speaker header | I2S | **not used by this project** (the display makes no sound; audio plays on the PC) |
| Wireless | 2.4 GHz Wi-Fi + BLE 5 on-module antenna | | |

Shared I2C bus: **SDA = GPIO47, SCL = GPIO48** (400 kHz in the BSP, internal pull-ups enabled) [audit/bsp/ard].

## TCA9554 expander (0x20) [audit/bsp/ard]

| EXIO | Signal | | EXIO | Signal |
|---|---|---|---|---|
| 0 | LCD chip-select (3-wire SPI) | | 4 | gate of the BSS138 in the KEYS block (see "Side buttons"); do not drive |
| 1 | LCD serial data (3-wire SPI) | | 5 | GT911 reset |
| 2 | LCD serial clock (3-wire SPI) | | 6 | GT911 interrupt |
| 3 | Audio power-amp enable | | 7 | LCD reset |

## Side buttons (PWR and BOOT) [schematic, extracted 2026-10-12]

Source: the vector text of page 1 of the official schematic (`KEYS` block) and the PCB layout on page 2 (Key3 "PWRKEY" above Key1 "BOOT" on the left board edge, display FPC at
the bottom), plus Waveshare's wiki photo of the back of the board. The BSP declares `BSP_CAPS_BUTTONS 0` and defines no button pins.

| Key | Net | Notes |
|---|---|---|
| **Key1 "BOOT"** | `IO0` = **GPIO0**, other side GND; 10 k pull-up, 100 nF | strapping pin: held while the chip resets / powers up -> ROM download mode (hardware, unaffected by the app). A normal input while the app runs. |
| **Key3 "PWR"** | `PWRON` = **AXP2101 PWRON pin**, other side GND; 10 k pull-up | **not wired to an ESP32 pin.** The AXP2101 `IRQ` pin is not routed to the ESP32 either (net `AXP_IRQ` appears once). Read by polling the PMIC: I2C 0x34, `INTSTS2` (0x49) bit0 PWRON rising edge, bit1 falling edge, bit2 long press, bit3 short press (enable bits in `INTEN2` 0x41; chip id reg 0x03 = 0x4A). Bit map confirmed in Waveshare's bundled XPowersLib (`XPowersParams.hpp`). The PMIC powers the board off by itself after a long press (4-10 s, its own configuration): never hold PWR longer than ~4 s. |
| `EXIO4` (TCA9554) | gate of a BSS138 (T1) in the KEYS block | undocumented; the firmware never drives it. |

The firmware (`components/side_keys`) uses PWR = volume up, BOOT = volume down.

## RGB panel [audit/bsp/ard — all three agree]

| Signal | GPIO |
|---|---|
| DE / VSYNC / HSYNC / PCLK | 17 / 3 / 46 / 9 (no DISP pin) |
| `data_gpio_nums[0..15]` for `esp_lcd` RGB565 | B0..B4 = 40, 41, 42, 2, 1 · G0..G5 = 21, 8, 18, 45, 38, 39 · R0..R4 = 10, 11, 12, 13, 14 |
| **Backlight** | **GPIO4, LEDC PWM 5 kHz / 10-bit, polarity INVERTED** — duty 0 = full brightness, duty 1023 = off [bsp: `duty = (100 - percent) * 1023 / 100`] |

GPIO45 and GPIO46 are ESP32-S3 strapping pins but are used here as G3 / HSYNC exactly as the factory firmware
does; VDD_SPI is eFuse-forced so GPIO45 is not sampled for flash voltage.
Unused by the display and kept free: **GPIO19/20 (native USB)**, GPIO0 (BOOT).

## Panel bring-up recipe (from the BSP, reused in our own `board`/`display` layer)

1. I2C bus up (47/48), TCA9554 at 0x20.
2. Touch reset sequence: EXIO6 (INT) low -> EXIO5 (RST) low 200 ms -> high 200 ms -> EXIO6 back to input.
3. 3-wire SPI panel IO on EXIO0/1/2, ST7701 init table (see BSP `lcd_init_cmds`), `esp_lcd_new_panel_st7701`.
4. RGB config: 16-bit bus, PSRAM frame buffer(s), **bounce buffer** (BSP default 480x20 px), `psram_trans_align = 64`.
   For a smooth UI: 2 frame buffers + LVGL direct/full-refresh with `avoid_tearing`.
5. Backlight LEDC on GPIO4, inverted.

ESP-IDF components involved: `esp_lcd_st7701`, `esp_lcd_panel_io_additions` (3-wire SPI), `esp_io_expander_tca9554`,
`esp_lcd_touch_gt911`, `espressif/esp_lvgl_port` (^2), `lvgl/lvgl` (9.x).

## Factory-firmware boot log highlights [board]

```
app_init: Project name: phone_s3_box_3   App version: v0.4.2-92-g5c6be6c-dirty   ESP-IDF: v5.5.1-dirty
esp_psram: SPI SRAM memory test OK          spi_flash: flash io: qio
heap_init: ~258 KiB + 21 KiB + 32 KiB internal RAM available before Wi-Fi/LVGL
app_main: Switch to partition factory
lcd_panel.io.3wire_spi: Panel IO create success      st7701: version: 2.0.1
ESP32-S3-Touch-LCD-4B: Touch 0x14 found      GT911: TouchPad_ID:0x39,0x31,0x31 ("911")
ESP32-S3-Touch-LCD-4B: Setting flipped LCD backlight: 0% (original: 100%)
QMI8658 initialized successfully      BS:Settings: Init PMU SUCCESS!      wifi_init: WiFi IRAM OP enabled
```

## Sources

| Item | Where | SHA-256 / version |
|---|---|---|
| Product page (SKU 32052, "Smart 86 Box") | https://www.waveshare.com/esp32-s3-touch-lcd-4b.htm | fetched 2026-10-11 |
| Wiki | https://www.waveshare.com/wiki/ESP32-S3-Touch-LCD-4B | raw text saved to `docs/waveshare/wiki/` |
| Schematic (2 pages) | https://files.waveshare.com/wiki/ESP32-S3-Touch-LCD-4B/ESP32-S3-Touch-LCD-4B.pdf | `171d4d3a0041abe183746519dec6f7fa430bb5ae9acea11e02dda10b06014879` |
| Demo archive (Arduino 3.2.0 + ESP-IDF 5.4.2 + FirmWare) | https://files.waveshare.com/wiki/ESP32-S3-Touch-LCD-4B/ESP32-S3-Touch-LCD-4B-Demos.zip | `a9025815a1e51b81dcdc1936271c5bab528fbfc7ea37cd0a790dcb9a81e7e84f` |
| **Official GitHub repo** (examples, pin audit, factory image, CI) | https://github.com/waveshareteam/ESP32-S3-Touch-LCD-4B | commit `62c38908a15f1ce45f18b76caed7436cd9cbc22a` (2026-09-10) |
| ESP-IDF BSP | https://components.espressif.com/components/waveshare/esp32_s3_touch_lcd_4b | v2.0.0, zip `3d8fc515d9be5e8c8b3b4665abf95e2b9e28a910dd266398acfa948818784317` |
| Third-party LVGL firmware (not Waveshare-maintained) | https://github.com/alaltitov/Waveshare-ESP32-S3-Touch-LCD-4B | reference only |

Waveshare's CI builds its examples with ESP-IDF 5.5.5 and 6.0.2 (ESP-IDF baseline 5.4.2) and Arduino-ESP32 3.3.11;
the example pins LVGL **9.5.0** and the BSP 2.0.0. The downloads live in `docs/waveshare/` (git-ignored, ~700 MB, re-fetchable
from the URLs above).

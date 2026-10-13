# Milestone 0 — hardware investigation and factory flash backup

Goal: identify the exact board, confirm the hardware facts from independent sources, choose the framework
([ADR-001](../decisions/ADR-001-esp-idf-and-lvgl.md)) and, **before anything was flashed or erased**, take a verified,
byte-for-byte copy of the board's whole flash. Restoring it returns the board to its out-of-the-box state.

> **Make your own backup before you flash a board.** The image of the board used during development is device-specific
> (16 MiB of Waveshare's proprietary factory firmware that can contain data from earlier users), so it is deliberately **not** in this
> repository (`*.bin` is git-ignored; see [../../backup/README.md](../../backup/README.md)). The values below are the evidence
> from that backup; identifiers of the individual unit are redacted.

## The backup that was taken

| | |
|---|---|
| File | `factory_full_flash_16MB_<chip-id>_<timestamp>.bin` (kept outside the repository; `*.bin` is git-ignored) |
| Size | 16,777,216 bytes (0x1000000) = the whole flash |
| SHA-256 / MD5 | recorded privately next to the file (`<redacted>`); you need your own pair for your own dump |
| Captured | 2026-10-11, `esptool read-flash 0 ALL` (esptool 5.5.0), ~89 s over the native USB port |
| Verified | `esptool verify-flash 0x0 <file>` -> **"Verification successful (digest matched)"** (the chip's own MD5 over all 16 MiB equals the file) |
| Chip | ESP32-S3 (QFN56) rev v0.2, MAC `xx:xx:xx:xx:xx:xx`, 16 MB quad flash, 8 MB octal PSRAM |
| Security | Secure Boot **disabled**, Flash Encryption **disabled** (`SPI_BOOT_CRYPT_CNT=0`), no eFuse key blocks in use -> the dump is plain data and restores exactly |

> The factory image is the only exact copy of what shipped on that unit: Waveshare's published `FactoryXiaozhi_250807.bin` is an
> *older* build (the board shipped with Dec 2025 / Jan 2026 builds), so it is not a substitute. Keep a second copy of your own dump
> somewhere safe (cloud drive / USB stick).

## Contents (parsed from the dump with `tools/analyze_flash_dump.py`)

Bootloader at `0x0` (ESP32-S3, 3 segments), partition table at `0x8000`:

| Name | Type / subtype | Offset | Size | Contents |
|---|---|---|---|---|
| nvs | data / nvs | 0x009000 | 24 KiB | only Wi-Fi mode + PHY calibration keys — **no stored Wi-Fi SSID/password** (checked by key *names* only) |
| otadata | data / ota | 0x00F000 | 8 KiB | boot selector |
| phy_init | data / phy | 0x011000 | 4 KiB | |
| model | data / spiffs | 0x012000 | 952 KiB | ESP-SR / voice model data |
| **factory** | app / factory | 0x100000 | 5 MiB | `phone_s3_box_3` v0.4.2-92-g5c6be6c-dirty, ESP-IDF v5.5.1-dirty, built Dec 20 2025 14:03:23 |
| **ota_0** | app / ota_0 | 0x600000 | 6 MiB | `xiaozhi` 1.8.5, ESP-IDF v5.5.2-249-gf56bea3d1f-dirty, built Jan 23 2026 14:16:08 |
| storage | data / spiffs | 0xC00000 | 4052 KiB | assets (last non-erased byte at 0xFF40FD) |

Boot log of the untouched board (captured read-only over USB): boots from `factory`; PSRAM self-test OK;
ES8311 / ES7210 codecs OK; ST7701 panel init OK; GT911 touch found at I2C `0x14`; QMI8658 IMU OK;
AXP2101 PMU OK; Wi-Fi stack initialises.

## Restore the factory firmware

This **overwrites the whole flash** (including anything flashed later). Run from the project root in PowerShell, with the board
connected by the USB-C port that enumerates as `USB JTAG/serial debug unit` (the COM number differs per computer:
`tools\find-board.ps1` prints it):

```powershell
$port = .\tools\find-board.ps1
$bin  = "backup\factory\<your-backup-file>.bin"

# 1. (optional) check your copy of the file is intact against the hash you recorded when you made it
(Get-FileHash $bin -Algorithm SHA256).Hash

# 2. write the full image at offset 0 — "keep" = do not let esptool patch the image header
.\tools\venv\Scripts\python.exe -m esptool --port $port --flash-mode keep --flash-freq keep --flash-size keep write-flash 0x0 $bin

# 3. (optional) read the chip back and compare
.\tools\venv\Scripts\python.exe -m esptool --port $port verify-flash 0x0 $bin
```

The chip resets itself afterwards and boots the Waveshare demo again. If the port is not found or the
board is unresponsive (e.g. a bad firmware disabled USB), force the ROM bootloader: **unplug USB-C, hold
the BOOT button, plug USB-C back in, then release BOOT** (the board has only BOOT and PWR buttons — no
RESET button; if a battery is attached, long-press PWR to power the board off first). Then repeat step 2.
The ROM download mode lives in mask ROM and can not be overwritten, and no eFuses are ever burnt, so
flashing can not brick this board.

## How to take your own backup

```powershell
$port = .\tools\find-board.ps1
.\tools\venv\Scripts\python.exe -m esptool --port $port read-flash 0 ALL backup\factory\factory_full_flash_16MB_$(Get-Date -Format yyyyMMdd-HHmmss).bin
.\tools\venv\Scripts\python.exe -m esptool --port $port verify-flash 0x0 backup\factory\<that-file>.bin   # must report "digest matched"
(Get-FileHash backup\factory\<that-file>.bin -Algorithm SHA256).Hash                                   # write this down somewhere safe
```

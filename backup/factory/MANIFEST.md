# Factory flash backup — ESP32-S3-Touch-LCD-4B

A byte-for-byte copy of the **entire 16 MiB flash** of this board exactly as it arrived
(Waveshare factory firmware: ESP-Brookesia "phone" UI + Xiaozhi AI), taken **before
anything was flashed or erased**. Restoring it returns the board to its out-of-the-box state.

| | |
|---|---|
| File | `factory_full_flash_16MB_mac-2884-85a2-4860_20261011-230234.bin` (this folder; **git-ignored**) |
| Size | 16,777,216 bytes (0x1000000) = the whole flash |
| SHA-256 | `8b4afd76d598df61cb7fb0db086ffd895a1d1f1b798531e9d46eeb377791c6f5` |
| MD5 | `409b152f4ae21e8d88ee9b63c55a9750` |
| Captured | 2026-10-11 ~23:02 local, `esptool read-flash 0 ALL` (esptool 5.5.0), 88.9 s over native USB (COM5) |
| Verified | `esptool verify-flash 0x0 <file>` -> **"Verification successful (digest matched)"** (the chip's own MD5 over all 16 MiB equals the file) |
| Chip | ESP32-S3 (QFN56) rev v0.2, MAC `28:84:85:a2:48:60`, 16 MB quad flash, 8 MB octal PSRAM |
| Security | Secure Boot **disabled**, Flash Encryption **disabled** (`SPI_BOOT_CRYPT_CNT=0`), no eFuse key blocks in use -> the dump is plain data and restores exactly |

> **Copy this `.bin` somewhere safe** (cloud drive / USB stick). It is the only exact copy of the
> factory image: Waveshare's published `FactoryXiaozhi_250807.bin` is an *older* build
> (the board shipped with Dec 2025 / Jan 2026 builds), so it is not a substitute.
> The file is git-ignored on purpose (size, and it is device-specific); the hashes above are committed.

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

This **overwrites the whole flash** (including anything we flashed later). Run from the project root
in PowerShell, with the board connected by the USB-C port that enumerates as `USB JTAG/serial debug unit`
(currently `COM5` — check Device Manager > Ports if it changed):

```powershell
$bin = "backup\factory\factory_full_flash_16MB_mac-2884-85a2-4860_20261011-230234.bin"

# 1. (optional) check your copy of the file is intact
(Get-FileHash $bin -Algorithm SHA256).Hash   # must equal the SHA-256 above

# 2. write the full image at offset 0 — "keep" = do not let esptool patch the image header
.\tools\venv\Scripts\python.exe -m esptool --port COM5 --flash-mode keep --flash-freq keep --flash-size keep write-flash 0x0 $bin

# 3. (optional) read the chip back and compare
.\tools\venv\Scripts\python.exe -m esptool --port COM5 verify-flash 0x0 $bin
```

The chip resets itself afterwards and boots the Waveshare demo again. If the port is not found or the
board is unresponsive (e.g. a bad firmware disabled USB), force the ROM bootloader: **unplug USB-C, hold
the BOOT button, plug USB-C back in, then release BOOT** (the board has only BOOT and PWR buttons — no
RESET button; if a battery is attached, long-press PWR to power the board off first). Then repeat step 2.
The ROM download mode lives in mask ROM and can not be overwritten, and we never burn eFuses, so
flashing can not brick this board.

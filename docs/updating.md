# Updating

Three things have versions, and they are checked against each other every time the display connects:

| Part | Version today | Where to see it |
|---|---|---|
| DeskMedia Companion (the PC program) | 1.0.0 | tray icon > **About**; the installer's file name |
| Display firmware | 1.0.0 (`PROJECT_VER` in `firmware/CMakeLists.txt`) | tray icon > **About** (while the display is connected) |
| DMLINK, the USB link protocol | 1 | [usb-protocol.md](usb-protocol.md) |
| The media API both sides speak | 2 (accepted range: 2 to 2) | [protocol.md](protocol.md) |

When the two ends cannot work together the display says so, in words: **Update DeskMedia Companion** (the PC program is too old for the firmware)
or **Update the display** (the firmware is too old for the PC program); the tray icon's status says the same. Nothing is guessed and nothing
fails silently. The rule for the project's maintainers: a change that old peers cannot understand raises the API version (or the link version
for a framing change) and the `min_api` of the side that no longer supports the old one; a compatible addition (a new optional JSON field, a new
route that old peers never call) changes neither.

## Updating the companion (the PC program)

Run the newer `DeskMediaCompanion-Setup-<version>.exe` over the old installation. It closes the running copy, replaces the program, keeps your
settings, your idle-screen picture and the pairing (they live in `%LOCALAPPDATA%\DeskMediaCompanion`, outside the program folder), keeps the
*Start with Windows* choice and starts the new version. The display reconnects by itself and is **not** asked to pair again. Silent / scripted
installs: [../installer/README.md](../installer/README.md). Uninstalling asks whether to delete the settings and the pairing too (default: keep).

## Updating the display's firmware

You need the three firmware files of a release (`bootloader.bin`, `partition-table.bin`, `deskmedia_display.bin`; `tools\package_firmware.ps1` makes them
from a checkout) and a Python with `esptool` (`pip install esptool`). The display stays connected with its USB cable, on the **native USB** port.

1. **Exit the companion** (tray icon > **Exit**): it holds the display's COM port, and a flasher needs the port to itself.
2. Find the port: Device Manager > *Ports (COM & LPT)* > *USB Serial Device (COMn)*, or `tools\find-board.ps1` in a checkout.
3. Flash the three files **separately** (never as one merged image: a merged image starts at 0x0 and would overwrite the display's storage with
   blank data, which forgets the pairing and the idle-screen settings):

```
python -m esptool --chip esp32s3 -p COM5 -b 460800 --before default_reset --after hard_reset write_flash --flash_mode dio --flash_size 16MB --flash_freq 80m ^
    0x0 bootloader.bin 0x8000 partition-table.bin 0x10000 deskmedia_display.bin
```

   (replace COM5 with your port; in PowerShell use a backtick instead of `^` or put it on one line; these underscore spellings work in every esptool version,
   newer ones also accept hyphens and print a hint). The board resets by itself when it is done.
4. Start the companion again. The display comes up, finds it, and is already paired: the pairing key lives in a flash area (`nvs`, 0x9000) that
   this procedure does not touch. If a release changes the partition table's *layout* the release notes say so and what it costs.

What the three files are, and what is left alone:

| Address | File | Contents |
|---|---|---|
| 0x0 | `bootloader.bin` | second-stage bootloader |
| 0x8000 | `partition-table.bin` | partition table (unchanged between 1.0.0 builds: `nvs` 0x9000, `factory` 0x10000, `idle_img` 0x810000, `storage` 0x910000) |
| 0x10000 | `deskmedia_display.bin` | the application |
| *(untouched)* | `nvs`, `idle_img` | pairing key and display identity; idle-screen picture |

**Never erase the whole flash** (`erase-flash`, "erase all" in a flasher tool): that deletes the pairing and the idle picture, and the factory
firmware backup is the only way back to the original software. The ROM download mode cannot be damaged by flashing, so a failed update is always
recoverable: hold **BOOT** while plugging the cable in and flash again ([troubleshooting.md](troubleshooting.md)).

Not tested: flashing from a web browser (esptool-js) with the same three files and offsets should work the same way; this project verified
`esptool` / `idf.py flash` only.

## Maintainers: cutting a release

1. Raise the version where it changed: `desk_media_companion.__version__` (the installer, the exe's version resource and *About* follow it) and
   `PROJECT_VER` in `firmware/CMakeLists.txt`; raise `API_VERSION` / `MIN_API_VERSION` (companion) and the matching constants in the firmware only
   for a protocol change (see above).
2. `tools\build_release.ps1` builds the exe and the installer, `tools\package_firmware.ps1` the firmware files; run the automated tests and the
   board tests ([development.md](development.md)).
3. Publish the installer and the firmware files together at the address the display's QR code points to (`DM_SETUP_URL`).

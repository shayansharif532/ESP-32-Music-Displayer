# Installing DeskMedia Companion on your PC

DeskMedia Companion is the small program that runs on your Windows PC and tells the DeskMedia display what is playing (Spotify, YouTube
in your browser, VLC, ...). The display is connected with a **USB cable**; you install the program once, pair the display once, and after
that there is nothing to do.

You need: a PC with **Windows 10 (version 1809 or newer) or Windows 11**, 64-bit; the DeskMedia display; and a USB cable that carries
data (some thin cables are charge-only and will not work). You do **not** need Python, any other program, an administrator account or a
network connection for the display.

## First-time setup

1. **Plug the display into a USB port of this PC.** It is detected automatically; Windows' own driver is used, nothing needs to be installed
   for it. The display tells you how far it has got (the strip at its top shows *USB - Companion - Paired*):
   * **"USB connected - Looking for DeskMedia Companion on your PC..."**: wait a moment.
   * **"No USB data"**: this cable only supplies power, or it is not plugged into a PC. Use a data cable or another USB port.
   * **"Install DeskMedia Companion"**: the display cannot find the program on this PC. Do steps 2 to 4. If you have installed it already, start
     **DeskMedia Companion** from the Start menu and check that its icon is next to the clock.
   * **"Pair with your PC"**: see step 5.
2. **Download the installer.** On your PC open the project's GitHub page (the display prints its address: `github.com/shayansharif532/deskmedia-display`), click
   **Releases** and download `DeskMediaCompanion-Setup-<version>.exe` from the newest release (or go straight to
   [the latest release](https://github.com/shayansharif532/deskmedia-display/releases/latest)). Download only that one file: **not** "Code" > "Download ZIP" and not "Source code".
3. **Run the installer.** Double-click it. It installs just for you (no administrator password). If Windows says **"Windows protected your PC"**,
   click **More info**, then **Run anyway**: this appears because the installer does not carry a paid publisher signature (yet); it does not mean
   anything is wrong with it.
4. **Click Next** on the welcome page and on the page that lists the open-source software the program contains. On the **Options** page
   keep *Start DeskMedia Companion when I sign in to Windows* ticked (that is what makes everything "just work" every day) and click
   **Next**. Installing takes a few seconds. **Click Finish** (leave *Start DeskMedia Companion now* ticked). A small icon appears in the
   notification area at the bottom right, next to the clock (it may hide under the **^** arrow). That is DeskMedia Companion running.
5. **Pair the display and the PC, once.** The display and the PC both show a six-digit code. Make sure the two codes are the same, then
   confirm on the display and click **Pair** in the window on the PC. If no window appeared on the PC, right-click the icon next to the
   clock and choose **Pair a display...**.
6. **That is it.** Play something; the title, artist and cover appear on the display, and its buttons control the player on the PC.
   Wi-Fi is not needed, and nothing has to be done again after this.

If you chose not to use the USB cable: the optional network (LAN) mode is switched on in the companion's settings; USB is the normal way.
In USB mode the program opens no network port, so **Windows does not show a firewall question**.

## Everyday use

* **Plug in, nothing else.** The companion starts when you sign in to Windows and the display connects as soon as it is plugged in. Unplug
  it and plug it back in whenever you like; no pairing is needed again.
* **The icon next to the clock.** Its small dot shows the state (green: the display is connected; amber: something needs doing, e.g. pairing;
  red: needs attention, e.g. an update or a busy port; grey: looking for the display) and hovering shows it in words. Right-click it for the menu: the
  display's status, **Idle screen settings...** (what the display shows when nothing is playing - theme, 12 / 24 hour clock, date format or hidden,
  your own picture), **Pair a display...**, **Forget paired display**, **Start with Windows**, **Advanced** (the optional Wi-Fi / LAN mode and the
  automatic pairing window), **Open log folder**, **About** (the versions, handy for a bug report) and **Exit**.
  After **Exit** the display says it cannot find the program; start it again from the Start menu (*DeskMedia Companion*).
* **Updating.** Run the newer installer; it replaces the old version and keeps your settings and the pairing. A running copy is closed for you.
* **Uninstalling.** Windows **Settings > Apps > Installed apps > DeskMedia Companion > Uninstall**. Setup then asks whether to delete your
  settings and the pairing as well. The default answer, **No**, keeps them, so a later reinstall continues where you left off.

## If something does not work

| What you see | What to try |
|---|---|
| No icon next to the clock | Start *DeskMedia Companion* from the Start menu; look under the **^** arrow for hidden icons |
| The display shows "Install DeskMedia Companion" although it is installed | The program is not running: start it from the Start menu and look for its icon next to the clock |
| The display shows "No USB data" | Another cable (a data cable) or another USB port |
| The display shows "Pair again" | Open **Pair a display...** in the icon's menu and compare the codes |
| The display says "Update DeskMedia Companion" | Install the newer Setup on the PC (it keeps your settings and the pairing) |
| The display says "Update the display" | The PC program is newer than the display's own software: follow the project's firmware update instructions |
| Windows blocked the installer | Click **More info > Run anyway** (see step 2) |
| Still stuck | The program writes a log to `%LOCALAPPDATA%\DeskMediaCompanion\companion.log` (paste that into the File Explorer address bar). It lists what happened (connections, pairing, errors) but never the pairing key; the title of what was playing is only logged when the program is started with `--debug` |

## For whoever hands a display to somebody else

The display's *Install DeskMedia Companion* screen prints an address (no QR code, nothing is downloaded by the display) and tells the person to click
**Releases** there. The firmware's default is the project's GitHub page (`DM_SETUP_ADDRESS` in `firmware/main/app_config.h`); its newest release must carry
`DeskMediaCompanion-Setup-<version>.exe` and be reachable by the person you give the display to. To print another address, build the firmware with
`idf.py -DDM_SETUP_ADDRESS=example.org/deskmedia build` (or `tools\package_firmware.ps1 -SetupAddress example.org/deskmedia`).
Silent installs, signing and the packaging tests are described in [../installer/README.md](../installer/README.md).

# Installing DeskMedia Companion on your PC

> Draft for the final README: written for people who have never used a developer tool. No screenshots yet.
> Wording that depends on the USB link is marked **[check]** and should be confirmed once that part is final.

DeskMedia Companion is the small program that runs on your Windows PC and tells the DeskMedia display what is playing (Spotify, YouTube
in your browser, VLC, ...). You install it once; after that it starts by itself.

You need: a PC with **Windows 10 (version 1809 or newer) or Windows 11**, 64-bit, the DeskMedia display and a USB cable that carries data
(some thin cables are charge-only and will not work).

## First-time setup

1. **Run the installer.** Double-click `DeskMediaCompanion-Setup-<version>.exe`. You do not need to be an administrator, and you do not
   need Python or anything else installed.
2. **If Windows says "Windows protected your PC"**, click **More info**, then **Run anyway**. This happens because the installer does
   not carry a paid publisher signature yet; it does not mean anything is wrong with it.
3. **Click Next** on the welcome page and on the page that lists the open-source software the program uses.
4. On the **Options** page keep *Start DeskMedia Companion when I sign in to Windows* ticked (that is what makes it "just work" every
   day) and click **Next**. The installation takes a few seconds.
5. Click **Finish**. A small icon appears in the notification area at the bottom right, next to the clock (it may hide under the **^**
   arrow). That is DeskMedia Companion running.
6. **Plug the display into a USB port of this PC.** It is detected automatically; no driver needs to be installed. **[check]**
7. **The first time only, pair them:** the display and the PC both show a six-digit code. If the two codes are the same, confirm on the
   display and click **Pair** on the PC. (If the PC window did not open by itself, right-click the tray icon and choose
   **Pair a display...**.)
8. Play something. The title, artist and cover appear on the display.

Windows may ask once whether DeskMedia Companion is allowed to communicate on your network. **[check: only if the program still opens a
network port by default]** Choose **Allow** if you also want to use the display over Wi-Fi; the USB connection does not need it.

## Everyday use

* **There is nothing to start.** The companion starts when you sign in to Windows and the display connects when it is plugged in. Unplug
  and plug it back in whenever you like.
* **The tray icon** (notification area, next to the clock). Right-click it for:
  * **Settings...**: the idle screen (what the display shows when nothing is playing): theme, clock, your own picture.
  * **Pair a display...** and **Forget paired display**: pair a display (once), or remove the pairing.
  * **Start with Windows**: tick or untick whether it starts when you sign in.
  * **Quit**: stop the companion (the display then shows that it cannot find your PC). Start it again from the Start menu:
    *DeskMedia Companion*.
* **Updating.** Run the newer installer; it replaces the old version. Your settings and the pairing are kept.
* **Uninstalling.** Windows **Settings > Apps > Installed apps > DeskMedia Companion > Uninstall**. Setup then asks whether to delete your
  settings and the pairing as well; the answer **No** (the default) keeps them, so a reinstall picks up where you left off.

## If something does not work

| What you see | What to try |
|---|---|
| No icon next to the clock | Open the Start menu and start *DeskMedia Companion*; check the **^** arrow for hidden icons |
| The display does not find the PC | Is the companion running (tray icon)? Try another USB port and another data cable; unplug and replug the display. **[check]** |
| The display wants to pair again | Open **Pair a display...** in the tray menu and confirm the codes |
| Windows blocked the installer | Click **More info > Run anyway** (see step 2) |
| Still stuck | The program writes a log to `%LOCALAPPDATA%\DeskMediaCompanion\companion.log` (paste that path into the File Explorer address bar). It lists what happened, track titles included |

For administrators and for building the installer yourself (silent install, signing, tests) see [../installer/README.md](../installer/README.md).

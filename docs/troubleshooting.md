# Troubleshooting

Start with what the **display** says: it names the step that is missing. The strip at the top of its setup screens reads
*USB - Companion - Paired*; the step that needs attention is marked. The tray icon of the companion (next to the Windows clock) shows the same
from the PC's side: hover over it for a one-line status, right-click for the menu, **Open log folder** for the log.

## What the display shows

| Display | Meaning | What to do |
|---|---|---|
| **No USB data** | The board is powered but its native USB port sees no data connection from a PC: a charge-only cable, a charger or power bank, a PC that is off / asleep, or the cable is in the board's *other* USB-C connector (the one that goes to the serial bridge and only supplies power as far as the PC link is concerned) | Use a cable that carries data, plug it into the PC, try the display's other USB-C connector |
| **USB connected** - *Looking for DeskMedia Companion on your PC...* | Data is there, the display is asking for the companion. Normal for a few seconds after plugging in or after the companion starts | Wait about 5 seconds |
| **Install DeskMedia Companion** (QR code + address) | The PC does not answer: the companion is not installed, not running, or the USB port is held by another program | Install it ([installation.md](installation.md)); if installed, start it from the Start menu and look for its icon next to the clock; close programs that use the board's COM port (a serial terminal, an IDE monitor, a flashing tool) |
| **Pair with your PC** | Companion found; the pairing has not been confirmed yet | Compare the six-digit codes, click **Pair** on the PC, confirm on the display. No window on the PC: tray icon > **Pair a display...** |
| **Pair again** | The display has a stored pairing that this PC does not recognise (the pairing was forgotten on the PC, its settings folder was deleted, or the display was paired with another PC) | A pairing window opens on the PC by itself; confirm it there |
| **Update DeskMedia Companion** | The display's firmware needs a newer PC program | Install the newest Setup (it keeps your settings and the pairing) |
| **Update the display** | The PC program is newer than the display's firmware | Update the firmware: [updating.md](updating.md) |
| *Can't reach your PC* (or *Reconnecting to your PC...* on the idle screen) | The companion was closed or restarted, or the cable was unplugged, a moment ago | It reconnects by itself; if it stays, see the rows above |

Everything is detected again automatically: unplugging and re-plugging the cable, restarting the companion, rebooting the display or the PC,
the PC waking from sleep. You never choose a COM port.

## On the PC

| Symptom | Try |
|---|---|
| No tray icon | Start *DeskMedia Companion* from the Start menu; the icon may be under the **^** arrow of the notification area |
| Tray status says *The display's USB port is in use by another program* | Close the other program (Arduino / PlatformIO / PuTTY / `idf.py monitor`, another copy of the companion in another Windows session) or unplug and re-plug the display |
| Tray status says *Display found, but not paired with this PC* | **Pair a display...** in the menu |
| Tray status says *The display has a different pairing key* | **Forget paired display**, then pair again |
| The pairing window never appears for an unpaired display | **Advanced > Pair a new display automatically** is off, or you cancelled a pairing a few minutes ago (it is not offered again for 10 minutes); use **Pair a display...** |
| Windows blocked the installer | *More info > Run anyway*; the installer is not code-signed |
| A firewall prompt appeared | It should not in USB mode (the companion opens no port). It appears only if the optional Wi-Fi / LAN mode is switched on (**Advanced > Also connect over Wi-Fi (LAN)**) |
| Nothing is shown while a video plays in a browser | The browser must publish a Windows media session (Chrome / Edge do; some embedded players do not) |
| The display shows an old cover or title after the PC woke from sleep | Wait a second or two; if it persists, unplug and re-plug the display |

## Logs and what is in them

The companion writes `companion.log` to `%LOCALAPPDATA%\DeskMediaCompanion` (tray menu > **Open log folder**). It lists connection events (display
found, paired, session established / dropped, port busy), never the pairing key, and the display's warnings and errors; ordinary lines of the display
(which name the track that is playing) are only written when the companion is started with `--debug`. The file rotates (512 KB, two old copies).

For a bug report, include: the companion version (tray menu > **About**), the display's firmware version (also in **About**), what the display
showed, and the last 50 lines of `companion.log`. Look through them first: the log mentions your PC's name and the COM port.

## Recovery

* **The display shows nothing sensible / the firmware is damaged.** The ESP32-S3's ROM download mode cannot be overwritten, so the board cannot be
  bricked by flashing. Unplug the cable, **hold BOOT**, plug the cable in, release BOOT after a second: the board waits for a flasher
  (it enumerates as a USB serial device but shows nothing on screen). Then flash again as described in [updating.md](updating.md). Do not hold PWR for
  more than about four seconds: the power chip switches the board off.
* **Forget everything and pair from scratch.** Tray menu > **Forget paired display**; the display then says *Pair again* and the pairing window opens.
  The display's own storage (pairing key, idle-screen settings and picture) survives a firmware update and is cleared only by erasing the board's
  flash, which this project never does for you.
* **Reset the companion's settings.** Exit the companion and delete `%LOCALAPPDATA%\DeskMediaCompanion` (this also deletes the pairing key and your idle-screen
  picture). The next start creates a fresh identity; pair again.
* **The companion misbehaves after an update.** Run the previous Setup over it; settings and pairing are kept.

## Wi-Fi / LAN mode (optional, for development)

Only relevant if the display's firmware was built with Wi-Fi credentials and the companion's LAN mode is on. The display then also looks for the
companion on the local network (UDP beacon) when the USB link is not available. Typical problems: the PC's firewall profile is *Public* (the
companion serves only private / loopback / link-local clients), the router isolates Wi-Fi clients from the PC, or two companions listen on the
same port. USB always has priority when both are available. See [protocol.md](protocol.md) and [security.md](security.md).

# Acceptance tests: what is automated and what needs a person

The USB-first release was tested by machine as far as a machine can reach: unit tests, a simulated display, and tests on the real board that run inside the
companion process (the board is driven through its own LVGL input path and judged from the log and from screenshots). Some things can only be
judged by a person with the board, a cable and a PC. This page lists both, with the exact steps for the second kind. "Pass" means exactly what the
last column says; anything else is worth a note in an issue (what you did, what the display showed, the tray's **About** text).

## Automated

| Area | Tool | What it proves |
|---|---|---|
| Frame protocol, parser, session, API, tray logic, settings, date and layout | `python -m unittest discover -s companion/tests` (542 tests) | behaviour against the spec's vectors, a simulated display, damaged / truncated / glued / duplicated frames, disconnects, version mismatch, authentication |
| The firmware's own parts | `selftest` development action | on-target: DMLINK frame vectors, the idle date (103 vectors), cJSON's nesting limit |
| USB as a product | `tools/usb_test.py` | the display is found without a port setting, the pairing window opens by itself, restart of the companion and reboot of the display reconnect without pairing again, forgetting the pairing, heap / stack health |
| Versions | `tools/usb_versions_test.py` | an older companion and an older firmware each produce the right "update" screen on the glass; the real versions recover by themselves |
| Idle screen over USB | `tools/idle_test.py --transport usb` | transitions, themes, 12 h clock, picture persistence across a display reboot and a companion restart, **the date: four formats, hide / show, midnight rollover on the device, correct after reboot and restart** |
| Setup screens | `link_<n>` development action, `logs/usb/link-sheet.png` | every setup screen fits and reads correctly |
| The optional LAN transport | `tools/lan_smoke_test.py` | **written but never run to completion**: it should show that a Wi-Fi build still serves a paired display when USB is not available. See "Not re-verified" below |
| Installer | `tools/installer_smoke_test.py`, `installer_flow_test.py`, `quit_listener_test.py` | per-user install, upgrade, uninstall, autostart value, no personal paths, the exe's `--selftest` with no Python |

## Physical tests (need a person)

Do them in this order; each takes a few minutes.

1. **Re-plug the cable.** Unplug the USB-C cable from the board, wait three seconds, plug it back in (both ends if you like). The display should show the setup
   screens and then the player or idle screen **by itself** within about ten seconds, with the companion running and **without** a pairing prompt.
   *Pass:* it comes back, still paired. Repeat three times, once quickly (unplug for one second) and once with a longer pause (a minute).
2. **Charge-only cable.** Use a cable that only charges (or a USB charger / power bank) instead of the PC cable. *Pass:* the display says **No USB data** after a
   few seconds; with the data cable it moves on by itself.
3. **Which connector?** The board has two USB-C connectors. Plug the PC cable into each in turn. *Pass:* one of them reaches *USB connected* (the native port, the
   one this product uses); the other (a serial bridge) shows **No USB data**. Note which one is which on your enclosure.
4. **First-time setup as a recipient would do it.** On a Windows account that has never seen the companion (or after uninstalling it and deleting
   `%LOCALAPPDATA%\DeskMediaCompanion`): plug the display in **before** installing anything. *Pass:* **No USB data** is not shown (a data cable), the display says
   **Install DeskMedia Companion** with a QR code and an address; run the installer, accept the SmartScreen notice (the installer is unsigned: *More info > Run anyway*),
   leave *Start with Windows* ticked; the display switches to **Pair with your PC** by itself, a pairing window opens on the PC, compare the two codes, click **Pair** on the PC
   and tap **Pair** on the display. Play something: title, artist and artwork appear. Note: the QR code currently points to the project's GitHub releases page, which is not reachable
   while the repository is private (see the owner decisions below).
5. **Everyday use.** Sign out and in again (or reboot the PC) with the display connected. *Pass:* the tray icon is back, the display reconnects within about ten seconds, no pairing prompt.
   Also try: PC sleep and wake; closing the companion (tray > **Exit**: the display says it cannot reach the PC, then **Install DeskMedia Companion** after about a minute) and starting it
   again from the Start menu (the display recovers by itself).
6. **Recovery.** Hold **BOOT** while plugging the cable in; the board enumerates but shows nothing. Flash the firmware again as in [updating.md](updating.md)
   (the three files, separately). *Pass:* it boots, and is still paired (the pairing lives in a part of the flash that this procedure does not touch).
7. **No Wi-Fi at all, time and date.** With the product firmware (no Wi-Fi credentials): leave the idle screen up. *Pass:* the time matches the PC's clock; in
   the companion's *Idle screen settings* each of the four date formats shows the PC's date (`Oct 13, 2026`, `13 Oct 2026`, `10/13/2026`, `13/10/2026`), **Show the date** off hides it, the
   12 / 24 hour switch works, the Photo theme with a bright picture keeps the date and the clock readable; after unplugging and re-plugging the display, and after restarting the
   companion, the date is still right. Midnight: leave it running past 00:00; the date changes by itself.
8. **The player, by hand.** Swipes, taps, the seek bar, the side buttons (PWR = volume up, BOOT = volume down; never hold PWR longer than about four seconds), source switching, Spotify
   shuffle / repeat, per-app volume: the same checks as before the USB link, now over the cable. *Pass:* nothing feels different.

## Not re-verified: the optional Wi-Fi / LAN mode

**not re-verified after the USB changes.** A development firmware built *with* Wi-Fi credentials was flashed once during this work and the board's USB port stopped answering (the Wi-Fi regression test, `tools/lan_smoke_test.py`, therefore never ran). The suspected cause, a hardware random-number source left on while Wi-Fi starts, is fixed in the code but that fix has not been tested on the board; later the board turned out to be stuck in ROM download mode for an unrelated, hardware-level reason (GPIO0 held low), so the original hang was never cleanly isolated.

What exists: the LAN code (discovery beacon, HTTP server, signed requests, the display's LAN client) is unchanged in design and its companion side is covered by unit tests and the isolated
end-to-end tests (`auto_follow_test.py`, `idle_source_test.py` run against the HTTP transport). What is missing is a run of a Wi-Fi-enabled firmware on the board after the USB link was added.
To close the gap: attach the board's second USB-C connector (the serial console), build with `-DAPP_DEV_TOOLS=1` and Wi-Fi credentials (no `-DAPP_NO_WIFI=1`), flash, watch the boot log for the
Wi-Fi start and the USB link, then run `DM_TEST_DATA_DIR=<folder paired over USB> python tools/lan_smoke_test.py`.

## Decisions that are the project owner's

* **Where the installer is published** and therefore what the *Install DeskMedia Companion* QR code points to (`DM_SETUP_URL`; the default is the project's GitHub releases page).
* Code signing of the installer (it is unsigned now: SmartScreen asks once), a project licence, and whether to make the repository public.
* Whether to ship a firmware flasher for recipients (today: `esptool` and three files, [updating.md](updating.md)).

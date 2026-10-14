## DeskMedia Companion 1.0.0 for Windows

DeskMedia Companion is the small Windows program that lets your DeskMedia display show what your PC is playing (Spotify, YouTube in your browser, VLC, ...)
and lets you control it from the display. The display is connected with a **USB cable**.

**Download `DeskMediaCompanion-Setup-1.0.0.exe` from the Assets list below. It is the only file you need.**
Please do not download *Source code (zip)* or *Source code (tar.gz)*: that is for developers, not the app.

### What you need

* A PC with **Windows 10 (version 1809 or newer) or Windows 11**, 64-bit
* The DeskMedia display and a USB cable that carries data (some thin cables only charge)
* Nothing else: no Wi-Fi setup, no Python, no administrator account

### First-time setup

1. **Plug the DeskMedia display into a USB port** of your PC. It says **Install DeskMedia Companion**.
2. **Download `DeskMediaCompanion-Setup-1.0.0.exe`** below and **run it**. If Windows says "Windows protected your PC", click **More info**, then **Run anyway**
   (the installer is not code-signed yet). Click **Next** through the installer, keep *Start DeskMedia Companion when I sign in to Windows* ticked, and click **Finish**.
3. **Pair once.** The display and the PC both show a six-digit code. If the two codes match, click **Pair** on the PC and tap **Pair** on the display.
4. **Done.** Play something: title, artist and cover appear on the display.

### Every day after that

Nothing to do. DeskMedia Companion starts with Windows and the display reconnects by itself over the USB cable, with no pairing again.
**Wi-Fi is not needed for normal use.**

### Good to know

* The installer installs only for your own Windows account and needs no administrator rights. To remove it: Windows **Settings > Apps > Installed apps > DeskMedia Companion**.
* In USB mode the program opens no network port, so Windows shows no firewall question.
* Updating later: run the newer installer; your settings and the pairing are kept.
* An optional Wi-Fi / LAN mode exists for developers. It was **not re-verified** for this release.
* Help: see the [README](https://github.com/shayansharif532/deskmedia-display#readme) and `docs/troubleshooting.md` in the repository.

# DeskMedia Display: portfolio summary

Short wording for a CV, a portfolio page or a project listing. Everything here is backed by the code and the logs in this repository.

## Project summary

**DeskMedia Display: ESP32-S3 / Windows Media Controller.**
Designed and built an ESP32-S3 touchscreen media controller integrating Windows GSMTC and Core Audio APIs. Implemented
multi-application playback detection, gesture-driven source switching, per-application volume control, adaptive artwork rendering, persistent
customization, a framed and versioned USB link protocol with automatic port discovery and one-time pairing, a guided first-time setup on the
device itself, and a per-user Windows installer with a tray application, using ESP-IDF, LVGL and Python.

Stack: C (ESP-IDF 5.5.5, FreeRTOS, LVGL 9.5), Python 3.12 (WinRT GSMTC bindings, Core Audio via pycaw, pyserial, Pillow, PyInstaller), Inno Setup, PowerShell; USB CDC serial link (optional UDP / HTTP on the LAN).
Repository: [README.md](README.md).

## Résumé bullets

* **Multi-session selection state machine.** Designed the logic that decides which Windows media session the display shows and controls: a pure
  (no I/O) AUTO / MANUAL_TEMP state machine with a synthetic "Idle" item in a wrapping swipe ring, event-driven exits (another session starts
  playing, the selection disappears, a 20 s no-interaction rule) and a 0.4 s background watcher; every command carries the displayed session's id and is
  refused if that session is gone, which removed a class of "control hit the wrong app" bugs. Covered by 56 model tests on scripted timelines plus isolated
  end-to-end tests against the real Windows media API with silent fake sessions (48 checks) and board-in-the-loop tests.
* **Pairing and authenticated-request protocol.** Specified and implemented a one-time pairing between the display and the PC (ephemeral X25519
  with a commitment and a six-digit comparison code, HKDF key derivation) followed by HMAC-SHA256 signed requests with single-use nonces, replay
  protection, per-address failure limiting and signed responses, on both a Python HTTP server and an ESP32 client ([docs/protocol.md](docs/protocol.md),
  test vectors shared by both implementations).
* **USB link protocol and product onboarding.** Replaced the LAN-only link with a USB-first one after measuring the board's USB capabilities (native USB-Serial/JTAG, CDC, no custom driver): a
  versioned, length-prefixed, double-checksummed frame protocol (DMLINK v1) with a resynchronising streaming parser, chunked responses up to 160 KB, presence beacons, duplicate suppression
  and a mutual challenge-response session bound to the pairing key ([docs/usb-protocol.md](docs/usb-protocol.md), test vectors checked on the PC and on the target); automatic COM-port discovery that
  survives unplugging, reboots and restarts of either side; setup screens on the display (charge-only cable detection, where to download the installer, pairing, update-needed cases); a per-user
  Windows installer with no administrator rights and no firewall prompt in USB mode; an independent security review whose findings (a JSON nesting stack overflow among them) were fixed
  ([docs/security.md](docs/security.md)).
* **Image pipeline across PC and microcontroller.** Removed app logos structurally (Spotify's alpha composite is split by its alpha bands, never by
  a fixed crop), trimmed letterbox bars, rendered aspect-preserving covers, and benchmarked edge-only, halo-limited up-scaling against alternatives (PSNR and
  edge-energy scoring) for 150 px browser thumbnails; on the ESP32, decoded baseline JPEGs into fixed PSRAM buffers and baked a per-track blurred,
  ordered-dithered RGB565 background so LVGL only blits it.
* **Embedded reliability work.** Investigated touch-triggered crashes of a vendor demo (stack overflows and panics with a 7 KB LVGL stack and a two-thread software renderer; root cause not
  proven), moved to a 24 KB pinned LVGL task with one draw unit, and soak-tested with serial-log heap / stack accounting: 30 idle <-> playback cycles over 315 s with 0 resets and
  internal free heap 142,583 -> 142,595 B.

## Skills demonstrated

* Embedded C on ESP-IDF / FreeRTOS: task design and pinning, PSRAM buffer management, NVS and raw flash partitions with crash-safe writes, USB-Serial/JTAG device behaviour, framed binary protocols, Wi-Fi back-off recovery, UDP discovery, bounded parsing of untrusted input
* LVGL 9 UI engineering on an RGB panel: gestures, screen transitions, overlays, dithered RGB565 rendering, generated fonts
* Windows platform APIs from Python: WinRT GSMTC (metadata, thumbnails, transport, Spotify shuffle / repeat), Core Audio per-application sessions, tray UI (pystray, tkinter), PyInstaller and Inno Setup packaging, per-user auto-start, a tray application, pyserial
* Protocol and security design: authenticated key agreement, MAC-signed requests, replay protection, LAN threat modelling
* State-machine design and testing; test tooling (virtual finger and simulated key presses through the real LVGL input path, frame-buffer screenshots, soak analysis)
* Hardware bring-up from a schematic: the two side buttons were traced to a GPIO and to a power-management chip that is read over I2C
* Technical documentation: architecture decision records, per-milestone engineering logs, Mermaid diagrams

## Verification

* **212 unit tests** in `companion/tests/` (`python -m unittest discover -s companion/tests`, about 15 s): the selection state machine, the artwork pipeline, idle-screen
  picture processing, HTTP routing and hardening, the pairing state machine and request authentication (RFC 7748 and the protocol's published test vectors are
  reproduced by the Python objects *and* by the firmware on the real board), X25519.
* Isolated end-to-end tests with the real Windows media API and silent fake sessions: 48 checks for auto-follow and the Spotify controls, plus
  the idle-as-source API checks; 17 checks against real Spotify and browser sessions (with every play state and volume restored afterwards).
* A board-in-the-loop **pairing and authentication test** (`tools/pairing_test.py`, 58 checks against the real firmware over the real LAN): unpaired clients refused, prompt only while the
  pairing window is open, Cancel pairs nothing, pairing + signed traffic, replay / tamper / wrong-key refused, forget -> re-pair, pairing survives a reboot, a rogue beacon is ignored and a rogue
  server's unsigned or endless answers are discarded.
* Board-in-the-loop tests with the board's virtual finger and simulated key presses, judged from the serial and companion logs: 56 checks for gestures, source
  switching, auto-follow, shuffle / repeat and per-app volume; a 10-point idle-screen test; a source-carousel test.
* Soak runs with no reset, panic or watchdog: 24 real touches over 941 s; five-minute gesture soaks of 115-125 scripted actions; 30 idle <-> playback
  cycles over 315 s; 40 swipe cycles onto and off the idle item. Heap and stack stayed flat (for example the LVGL task's minimum free stack stayed at about 20 KB of 24 KB).
* A deliberately dropped Wi-Fi link recovered in about 2 s.
* Not claimed: the physical feel of gestures and of the upper side button, and how the idle screen looks on the glass, were checked mostly through
  the virtual finger and frame-buffer screenshots ("hardware-tested"), not by a user study.

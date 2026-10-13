# ADR-009 — USB is the primary transport; the LAN stays an optional fallback (2026-10-13) — **current**

Status: **accepted** (maintainer request: the display is plugged into the PC for power anyway, so it should be usable by someone who has never
seen the project: no Wi-Fi credentials, no firewall, no developer steps). Builds on ADR-003 (the Windows companion) and the pairing protocol of
[`protocol.md`](../protocol.md). The wire format is [`usb-protocol.md`](../usb-protocol.md).

## Context

Until now the display found the PC over Wi-Fi (UDP beacon, HTTP on 8765). That needs credentials compiled into the firmware, a Windows firewall
rule for the companion, and the display and the PC on the same network: three things a recipient cannot be expected to do. The board already sits
on a USB cable (power), and the PC is the only peer it has.

## What was verified on the board before deciding (reference unit, Windows 10, 2026-10-13)

| Question | Answer |
|---|---|
| Which USB interface can carry runtime traffic? | The **native USB** connector: the ESP32-S3's *USB-Serial/JTAG* controller, `VID 303A / PID 1001`. Windows enumerates a composite device: interface 0 = **CDC-ACM** (bound to the inbox `usbser` driver, appears as a COM port), interface 2 = the JTAG function (WinUSB). **No driver installation.** The second connector is a USB-to-UART bridge on UART0 (the board's debug console). |
| Is native USB CDC practical? | Yes. Measured with a throw-away firmware: **~900 KB/s display to PC, ~500 KB/s PC to display**, 0.1 ms median round trip, data intact (CRC-checked 1 MB transfers); a 160 KB cover takes ~0.2 s plus decoding. Opening and closing the port does **not** reset the chip (DTR and RTS kept low); a software reset keeps the port enumerated; a hard reset re-enumerates it. |
| Does it conflict with flashing and debugging? | No: `esptool` / `idf.py flash` use the same controller (auto-reset through RTS/DTR, verified again by flashing the test firmware repeatedly) and the BOOT-held-at-power-up download mode is hardware. The console is moved to UART0 so no log line can interleave with a binary frame (see below). |
| Composite USB (CDC + mass storage)? | **Not possible** with this controller: the SDK documents USB-Serial/JTAG as a fixed-function device that "cannot be reconfigured to perform any function other than a serial port and JTAG". Mass storage would need the USB-OTG controller with TinyUSB on the same two pins, which replaces the USB-Serial/JTAG function: no more auto-reset flashing from the host, recovery only through the BOOT gesture, and a firmware-side USB stack to maintain. Rejected. |
| Charge-only cable detection? | `usb_serial_jtag_is_connected()` is true only while the controller receives the host's start-of-frame packets; the SDK states a power bank never counts. A charge-only cable or charger therefore reads as "no host". |
| Entropy without Wi-Fi? | The hardware RNG is only a true random source while Wi-Fi/Bluetooth runs or the SAR-ADC entropy source is on. The USB-only firmware switches that source on at start-up (`bootloader_random_enable()`), because the pairing keys must not come from a pseudo-random stream. |

## Decision

* **Transport.** A CDC-ACM serial port over the native USB connector, carrying **DMLINK v1** frames (magic, version, type, flags, sequence, id, length, header CRC-16, payload, CRC-32) with a normative resynchronising receiver, bounded buffers and a
  presence handshake (BEACON / ACK). The companion finds the display by scanning for `303A:1001` and confirming with a valid BEACON; the user never selects a port.
* **Same API.** The display keeps being the only requester: the REQUEST / RESPONSE frames carry the *existing* routes and JSON, so the player, the idle-screen sync, the pairing handshake and every command run unchanged over either transport. A small transport layer in
  `pc_media` and a transport-independent `Api` class in the companion are the only code that knows the difference.
* **Authentication.** The cable is point to point, so the per-request nonce / MAC of the LAN transport is replaced by a **session**: the paired display and the companion prove to each other, once per link, that they hold the pairing key (mutual HMAC over fresh nonces). Pairing itself
  (X25519 key agreement, 6-digit code on both screens, click on the PC and tap on the display) is unchanged and happens once; the display identity is persistent so the companion's record names the display.
* **LAN becomes optional and off by default.** The companion opens no listening socket and the firmware never starts Wi-Fi unless a development build compiles credentials in (or a later release provisions them at run time). USB-only use therefore cannot trigger a firewall prompt.
* **Console on UART0.** The native port carries only frames; logs go to UART0 (the second connector) and, in addition, to the companion as LOG frames (so the companion's log file contains the display's side of a problem).
* **Setup delivery: a QR code on the display, not a mass-storage volume.** When USB data is present and no companion answers, the screen shows the install address as a QR code and in short form. Reliability over cleverness.

## Rejected

* **Wi-Fi only** (the status quo): needs credentials, the firewall and a shared network.
* **The UART-bridge connector as the runtime link**: a second, driver-dependent chip path (the bridge needs a vendor driver on some Windows versions), auto-reset lines wired to the chip that make opening the port a reset hazard, lower throughput.
* **USB HID / vendor-specific (WinUSB) interfaces**: not available on the fixed-function controller; a bespoke class would also need a Windows driver or a signed INF.
* **Bluetooth LE**: pairing UX on Windows, throughput for covers, and radio coexistence for nothing the cable does not already give.
* **Per-frame MACs / encryption on the cable**: the link is physical and point to point; the session proves the peer, and an attacker who can rewrite the cable has physical access. Documented as a limitation instead of costing latency and code.

## Consequences

* A display plugged into a PC with the companion installed needs no configuration; unplugging, rebooting either side or restarting the companion reconnects by itself and never re-pairs.
* The firmware has to survive an unread port (no host, closed port, crashed companion): every write is all-or-nothing into a bounded ring, beacons are only sent when the previous frame was collected, the poll task never blocks on the cable.
* Development tooling reads the firmware log through the companion (LOG frames) or the UART connector instead of the native port; `tools/serial_capture.py` decodes frames.
* The LAN transport and its tests remain (regression-tested) as the fallback.

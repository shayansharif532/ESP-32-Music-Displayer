# DeskMedia USB link protocol (DMLINK v1)

How the display and the companion talk over the USB cable. This is the **primary** transport since the USB-first release; the LAN
transport of [protocol.md](protocol.md) stays available as an optional fallback and shares its routes, JSON bodies and pairing
handshake with this document. Threat model and limitations: [security.md](security.md). Decision record:
[ADR-009](decisions/ADR-009-usb-first-transport.md).

| Version | Meaning | Value |
|---|---|---|
| **link** | the frame format of this document (`VER` byte) | **1** |
| **api** | the routes and JSON of [protocol.md](protocol.md) (the `v` of `/hello`) | **2** |

## 1. Physical layer

* The display is a **USB CDC-ACM serial device**: the ESP32-S3's fixed-function USB-Serial/JTAG controller, `VID 303A` / `PID 1001`
  (fixed by the silicon, shared by every ESP32-S3 board that uses its native USB). Windows binds its inbox `usbser` driver: **no driver
  install**, no driver signing question. The controller also exposes a JTAG function (WinUSB), which this protocol does not use.
* The companion opens the port as 8N1 (the baud rate is ignored by the hardware), **no flow control, and keeps DTR and RTS low for the
  whole time**. A reset-capable RTS/DTR sequence is what `esptool` uses to restart the chip; the link never produces one, so opening,
  closing and restarting the companion cannot reboot the display.
* Measured on the reference board (Windows 10, [hardware.md](hardware.md)): about **900 KB/s display to PC, 500 KB/s PC to display**,
  round trip 0.1 ms median, reliable with the port opened and closed repeatedly. A 160 KB cover therefore needs about 0.2 s.
* The controller can only be a serial port plus JTAG; it cannot also present a mass-storage volume. See ADR-009.
* Because the chip's USB identifiers are not unique to this product the companion **never trusts a VID/PID alone**: a port counts as a
  DeskMedia display only after it has produced a valid `BEACON` frame (section 3).
* Bytes outside frames (the ROM boot banner, stray text) are ignored by both sides.

## 2. Frame

All integers are **little endian**. Maximum payload `MAX_PAYLOAD = 4096`, so a frame is at most 4112 bytes.

```
offset size  field
0      2     MAGIC    0xFA 0xDE           (0xFA never occurs in UTF-8 text, so log text cannot fake a start)
2      1     VER      1                   (link version)
3      1     TYPE     see section 3
4      1     FLAGS    bit 0 = FIRST chunk of the message, bit 1 = LAST chunk (a single-frame message has both: 0x03)
5      1     SEQ      chunk number within the message, 0 for the first, +1 per chunk, wraps at 256
6      2     ID       message id; a RESPONSE carries the id of its REQUEST; 0 = not part of an exchange
8      2     LEN      payload length, 0..4096
10     2     HCRC     CRC-16/CCITT-FALSE of bytes 0..9
12     LEN   PAYLOAD
12+LEN 4     CRC      CRC-32 (IEEE, the zlib one) of bytes 0..11+LEN (header, HCRC and payload)
```

* CRC-16/CCITT-FALSE: polynomial `0x1021`, init `0xFFFF`, no reflection, no final xor; `"123456789"` gives `0x29B1`.
* CRC-32: IEEE 802.3 as in zlib; `"123456789"` gives `0xCBF43926` (`esp_rom_crc32_le(0, ...)` and `zlib.crc32` agree).
* The header has its own CRC so that a false start (random bytes that happen to contain `FA DE`) is rejected after 12 bytes instead
  of after a 4 KB payload.

### Receiving (normative, both sides)

The receiver keeps a byte buffer and loops:

1. Find the first `FA DE`. Everything before it is noise (a text log line, boot banner): hand it to the log sink, never to the parser.
2. Fewer than 12 bytes after it: wait for more.
3. `HCRC` wrong, or `LEN > 4096`: discard **one byte** (the `FA`) and go to 1. Nothing buffered is lost; the real frame behind a false
   start is found by the next scan.
4. `VER != 1` with a valid `HCRC`: the frame is skipped whole (`12 + LEN + 4` bytes) and counted; the link keeps working (future
   versions may add frames an old peer must ignore).
5. Fewer than `16 + LEN` bytes buffered: wait. A partial frame that does not complete within **1 s** is abandoned (discard one
   byte, go to 1): a sender that died mid-frame cannot wedge the receiver.
6. `CRC` wrong: discard one byte, go to 1.
7. Otherwise deliver the frame and consume it.

Consequences: a truncated frame, a frame cut by an unplug and continued after a re-plug, two glued frames, a corrupted byte or a
flood of garbage all cost at most the frames they touch; the next intact frame is always accepted. Nothing needs a reboot.

### Sending

A sender writes a frame with as few `write` calls as possible and never interleaves two frames (one writer lock). A write that
times out part-way leaves a truncated frame on the wire, which the receiver handles as above; the sender then simply continues with
its next frame. The display never blocks its UI or poll task on the cable: its writer drops a frame when nobody is reading.

## 3. Frame types

| TYPE | Name | From | Payload |
|---|---|---|---|
| `0x01` | `BEACON` | display | JSON, section 4 |
| `0x02` | `ACK` | companion | JSON, section 4 |
| `0x10` | `REQUEST` | display | `METHOD SP target LF [body]`, section 5 |
| `0x11` | `RESPONSE` | companion | `u16 status, u32 total` + data (first chunk), data (further chunks), section 5 |
| `0x20` | `LOG` | display | UTF-8 text (development builds: every log line; product builds: warnings and errors) |
| `0x30` | `PING` | either | up to 64 bytes, answered with the same bytes in a `PONG` |
| `0x31` | `PONG` | either | the echoed bytes |

Every other TYPE is **ignored** (and counted), so a newer peer may send frames an older one does not know.

## 4. Presence, versions and the session

### BEACON and ACK

The display announces itself and the companion answers; together they replace the UDP beacon of the LAN transport and double as a keep-alive.

```json
BEACON (display -> companion):  {"v":1,"api":2,"min_api":2,"dev":"<16 hex>","boot":"<8 hex>","fw":"1.0.0","paired":true}
ACK    (companion -> display):  {"v":1,"api":2,"min_api":2,"app":"1.0.0","name":"DESK-PC","id":"<16 hex>","compat":true}
```

* `dev` is the display's **persistent identity**: 8 random bytes created at the first boot and kept in NVS (it is not derived from
  the MAC address and is not secret). It is also the `dev` of the pairing handshake, so the companion's pairing record names the
  display and a different display can be recognised. `boot` is a fresh random value per boot: a new `boot` means "the display
  restarted", and the companion drops that display's session at once, even though the USB port stayed open (a software reset does
  not re-enumerate the device).
* `id` is the companion id of [protocol.md](protocol.md) (also persistent); `name` the PC name; `paired` / `compat` as below.
* Timing: the display sends a BEACON every **1 s** while it has no companion and every **2 s** otherwise, but only when its previous
  write has been collected by a reader (so an unplugged or unopened port never accumulates a backlog). The companion answers each
  BEACON, at most one ACK per 200 ms.
* The display treats the companion as present while an ACK (or any valid companion frame) arrived within the last **6 s**; the
  companion treats the display as present while any valid frame arrived within the last **8 s**, and otherwise closes the port and
  scans again.
* **Compatibility.** `api` is the highest API version a side speaks, `min_api` the lowest. The sides are compatible when
  `display.api >= companion.min_api` and `companion.api >= display.min_api`; the version used is the smaller `api`. The companion
  decides and reports it in `compat`. When it is `false` the companion answers every route except `/hello` with `426` and shows
  "the display firmware / this app needs an update"; the display shows the same on its screen (section 8). Nothing fails silently.

### Pairing and the session

Pairing is the handshake of [protocol.md](protocol.md) section 3 unchanged (`/pair/offer`, `/pair/reveal`, `/pair/confirm`,
`/pair/status`, `/pair/cancel`, carried as REQUESTs): a key `K` agreed with X25519, a 6-digit code compared on both screens, a click
on the PC **and** a tap on the display. The differences are:

* **One pairing, then never again.** The display stores `K` and the companion's id; the companion stores `K` and the display's `dev`
  (`pairing.bin`, DPAPI-protected). Rebooting either side, unplugging, restarting the companion or the PC does not pair again. A
  new pairing is needed only when the display's storage is erased, the companion's data is reset, or the user chooses to re-pair.
* **The pairing window opens by itself on USB.** When a display that is not paired with this PC appears on the cable and the
  companion has no paired display, the companion opens the 120 s window and shows its pairing dialog (a cancelled attempt is not
  offered again for 10 minutes; the tray menu can always start one). `GET /hello` reports it as `"pairing":true`.
* **A session instead of signed requests.** The cable is point to point, so the per-request nonce/MAC of the LAN transport is not
  used. Instead the paired display and the companion prove to each other, once per link, that they hold `K`:

```
1. display -> POST /link/open   {"dev":"<16 hex>","nd":"<32 hex: 16 random bytes>"}
   companion -> {"ok":true,"nc":"<32 hex: 16 random bytes>","proof":"<64 hex>"}
        proof = HMAC-SHA256(K, "DMLINK-PC\n"  || cid || dev || nd || nc)        (cid, dev: 8 bytes each; nd, nc: 16 bytes)
2. display verifies the proof in constant time, then
   display -> POST /link/auth   {"dev":"<16 hex>","proof":"<64 hex>"}
        proof = HMAC-SHA256(K, "DMLINK-DEV\n" || cid || dev || nd || nc)
   companion verifies -> {"ok":true}: the link is now AUTHENTICATED
```

  `/link/open` answers `401 {"error":"unpaired"}` when the companion has no key or its paired display is a different one (the
  display then offers to pair again), and `401 {"error":"auth"}` for a bad `/link/auth`. Both nonces are fresh per link, so a
  recorded session is useless. The session lasts until the link goes down, the display's `boot` changes, or the companion
  forgets the pairing. There is **no per-frame MAC**: an attacker who can inject bytes into the cable already has physical access
  (section 9).
* Routes that need the session: everything except `GET /hello`, `GET /health`, `/pair/*`, `/link/*`. Without a session they answer
  `401 {"error":"unpaired"}` (no key) or `401 {"error":"auth"}`. Ten failed `/link/auth` or `/link/open` attempts within 60 s make
  the companion answer `429` for the next 60 s.

## 5. Requests and responses

The display is the only requester (exactly as with HTTP polling), one request at a time.

### REQUEST (`0x10`, display to companion)

```
payload = METHOD SP target LF [body]          METHOD: GET | POST     target: path and query, at most 512 bytes
```

* A single frame: payload at most 4096 bytes; the optional body (the small JSON of the pairing and session routes) follows the LF.
* `ID` is the display's request counter (1..65535, skipping 0). On a timeout the display may repeat the request **with the same ID
  and the same bytes**.
* The companion rejects a malformed request line, an unknown METHOD or a target over 512 bytes with a RESPONSE `400`, and a body on a
  route that takes none with `400`. Request data is never echoed in an error.

### RESPONSE (`0x11`, companion to display)

* The first chunk (`FLAGS` bit 0) starts with `u16 status` (HTTP status codes of [protocol.md](protocol.md)) and `u32 total` (the
  body length), followed by the first body bytes; every further chunk carries only body bytes. The last chunk has bit 1 set.
  Chunks are at most 4096 payload bytes, `SEQ` counts them and all carry the request's `ID`. A body is at most 512 KB.
* The display checks `SEQ` (a gap or a repeat abandons the response), the running length against `total` and its own per-route size
  limit (16 KB JSON, 160 KB cover, 300 KB idle picture): a response that does not fit is dropped as a failure, never truncated.
* There are no headers: the display knows what it asked for. (`ETag`, `Content-Type`, the `X-DM-*` fields and `X-Desk-Client` of the
  LAN transport have no equivalent.)

### Retries and duplicates

The companion remembers the last 8 exchanges (ID, a CRC of the request bytes, the finished response) for 10 seconds. A REQUEST that
arrives again with the **same ID and the same CRC** is answered from that memory and **not executed again**: a "next track" whose
answer was lost is not applied twice. The same ID with different bytes is a new request (the counter wrapped).

## 6. Mapping to the API

The routes, parameters and JSON are those of [protocol.md](protocol.md) and the module docstring of `companion/.../server.py`:
`/now-playing`, `/artwork`, `/control/*`, `/volume`, `/sources`, `/idle`, `/idle/image`, `/hello`, `/health`, `/pair/*`.
Differences on the USB link: no `Host`, `X-Desk-Client`, `X-DM-*` or CORS handling (there is no browser, no other client), the session
instead of the signature, and `/hello` additionally returns `"transport":"usb"` and `"paired_with_you":bool` (the paired display is the
one that sent the BEACON). `/now-playing` carries the PC's local date and time exactly as on LAN (`clock_ms`, section 7 of protocol.md).

## 7. Resource limits

| What | Limit |
|---|---|
| payload per frame | 4096 bytes |
| request target / whole request | 512 / 4096 bytes |
| response body | 512 KB (the display accepts 16 KB JSON, 160 KB cover, 300 KB idle picture) |
| partial frame | abandoned after 1 s |
| response not completed | abandoned after 8 s without a chunk |
| LOG text per frame | 4096 bytes (longer lines are split) |
| display to companion keep-alive | BEACON every 1 s (no companion) / 2 s |
| companion receive buffer | at most 64 KB; beyond that the oldest bytes are dropped |
| failed session attempts | 10 per 60 s, then `429` for 60 s |

## 8. What each side does when things go wrong

| Event | Companion | Display |
|---|---|---|
| cable unplugged | the port read fails: closes, the display is "gone", scans every 2 s | no SOF for 3 ms: link down at once; after 6 s "no USB data" if it stays that way |
| data-less cable (charge only) | nothing to see | the USB controller never sees a host: **"no USB data"** screen (section 8.1) |
| replugged / display reboot | port reappears or a new `boot` in the BEACON: new link, session dropped | boots, beacons, handshakes again, no pairing |
| companion not running / not installed | - | USB present but no ACK for 5 s: **"DeskMedia Companion not found"** with the install QR code; switches by itself when the companion starts |
| companion crash or restart | - | requests time out (3 s), link marked down after 6 s, then as above; reconnects without a reboot |
| frame with a wrong CRC, bad header, oversize `LEN` | skipped, resynchronised, counted | same |
| unknown `TYPE` or newer `VER` | ignored | ignored |
| RESPONSE chunk lost or repeated | - | the response is abandoned; the poll repeats the request (same ID: answered from the companion's memory) |
| REQUEST lost or garbled | never answers; the display retries once with the same ID | after 3 s |
| display writes while nobody reads | - | drops the frame (never blocks) |
| `compat: false` | tray shows the incompatibility; every route except `/hello` answers 426 | "update needed" screen |

### 8.1 Telling a data cable from a charge-only cable

The USB-Serial/JTAG controller reports a connected host only while it receives the host's start-of-frame packets
(`usb_serial_jtag_is_connected()`; the SDK documents that a power bank never counts). The display therefore distinguishes
**powered, but no data** (charge-only cable, a phone charger, a hub without a data connection) from **data present, no companion**
(the PC sees the device but the app is not running). The first shows "No USB data connection: use a data cable..." with a hint to try
another cable or port; the second shows the install / start hint. After a 6 s grace period at power-up neither is shown while a Wi-Fi
fallback or an established link exists.

## 9. Security notes

* The cable is a trusted point-to-point link **after pairing**: the session proves both ends hold `K`, a different PC or a different
  display cannot join it, nothing sent is reusable. The link is **not encrypted** and has no per-frame MAC: someone who can tap or
  rewrite the cable can read the (low-value) now-playing data or inject frames. That is out of scope; see [security.md](security.md).
* Before pairing, only `/hello`, `/health`, `/pair/*` and `/link/*` are served. A program that opens the serial port before the
  companion can make an unpaired display *show* a pairing prompt but cannot pair it (the tap on the display and the click on the PC
  are both needed) and cannot control a paired one.
* Parsing is bounded on both sides: `LEN` is checked before any buffer is sized, the receive buffers are fixed, responses are checked
  against their announced `total` and per-route limits, JSON bodies are parsed with length-checked parsers and every string is copied into a bounded
  buffer. No frame can make either side write a file, run a command or open a path.

## 10. Test vectors

Computed independently of the product code; both implementations must reproduce them.

```
CRC-16/CCITT-FALSE("123456789") = 0x29B1        CRC-32("123456789") = 0xCBF43926

PING, id 7, payload "hello", FLAGS 0x03, SEQ 0:
  fa de 01 30 03 00 07 00 05 00 c9 f0 68 65 6c 6c 6f c0 c9 b3 e1

REQUEST, id 1, "GET /now-playing\n":
  fa de 01 10 03 00 01 00 11 00 11 76 47 45 54 20 2f 6e 6f 77 2d 70 6c 61 79 69 6e 67 0a 64 50 41 b9

RESPONSE, id 1, status 200, body {"ok":true} (single chunk):
  fa de 01 11 03 00 01 00 11 00 70 ce c8 00 0b 00 00 00 7b 22 6f 6b 22 3a 74 72 75 65 7d 48 39 aa 33

BEACON, id 0, payload {"v":1,"api":2}:
  fa de 01 01 03 00 00 00 0f 00 c3 af 7b 22 76 22 3a 31 2c 22 61 70 69 22 3a 32 7d 2e 27 e7 37

RESPONSE in two chunks, id 9, status 200, total 6, body "abcdef":
  chunk 1 (FLAGS 0x01, SEQ 0): fa de 01 11 01 00 09 00 09 00 29 4a c8 00 06 00 00 00 61 62 63 7a bc ca 83
  chunk 2 (FLAGS 0x02, SEQ 1): fa de 01 11 02 01 09 00 03 00 53 c1 64 65 66 3d 27 ed b5

PONG, id 0, empty payload:
  fa de 01 31 03 00 00 00 00 00 70 e6 50 85 17 b6 16
```

A stream that contains `fa de 01 30 ...` with one flipped byte, then the vector again, must yield exactly the second frame.

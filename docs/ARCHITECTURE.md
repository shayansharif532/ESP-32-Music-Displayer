# Architecture

Status: current as of 2026-10-12 (ADR-003). The earlier Spotify-Web-API architecture is **obsolete** (see [legacy/](../legacy/README.md)).

## One-paragraph summary

The display is used only with the recipient's **Windows PC**. A small **Windows companion** reads the PC's active *media session*
(the thing behind the media overlay / media keys, via `Windows.Media.Control`), so it sees Spotify desktop, YouTube / YouTube Music in
a browser, VLC, Apple Music, and anything else that exposes system media controls. It serves that over a tiny **HTTP API on the home
LAN**. The **ESP32-S3** finds the companion automatically, polls it about once a second, shows title / artist / artwork / progress on
the 480x480 LVGL screen, and sends play/pause/next/previous/seek back. No Spotify account, Client ID, Premium subscription, OAuth,
tokens or internet access is involved.

```
 Recipient's Windows PC                                   Home Wi-Fi / LAN            Desk display (this repo, firmware/)
┌──────────────────────────────────────────┐                                       ┌─────────────────────────────────────┐
│ any media app (Spotify, Chrome/Edge+YouTube,                                     │ ESP32-S3  (Waveshare 4B, 480x480)   │
│ VLC, Apple Music …)                      │                                       │                                     │
│        │  system media transport controls │                                       │  wifi_mgr ── Wi-Fi STA, reconnect   │
│        ▼                                  │   UDP beacon  DESKMEDIA1 :8766        │  pc_media ── discovery + client     │
│ Desk Media Companion (companion/)         │ ───────────────────────────────────▶ │     │  GET /now-playing  (1 Hz)      │
│   media.py   GSMTC wrapper (WinRT)        │   HTTP :8765                          │     │  GET /artwork      (on change) │
│   server.py  HTTP API                     │ ◀─────────────────────────────────── │     │  POST /control/…   (on tap)    │
│   discovery.py  UDP beacon                │                                       │  ui ── LVGL Now Playing screen      │
│   autostart.py  HKCU Run key              │                                       │  main ── glue (state → UI, taps → cmd)│
└──────────────────────────────────────────┘                                       └─────────────────────────────────────┘
```

## Why this design

* **Works with everything**: the media session is the one common denominator; Spotify desktop, browsers (YouTube, YouTube Music, any
  site that sets the Media Session API) and players all publish to it. Control goes through the same session, so next/previous behave
  exactly like the keyboard media keys.
* **No credentials to get wrong**: no OAuth, no tokens, no Premium requirement, no Spotify developer account, no 5-user cap.
* **Simple, fast firmware**: plain HTTP on the LAN (no TLS handshakes on the ESP32), small JSON, artwork pre-shrunk by the PC to a
  300x300 baseline JPEG that the ESP32's decoder handles without scaling or exotic formats.
* **Trade-off accepted**: the PC must be on, on the same network, and running the companion. If it is not, the display says so.

## Components

| Component | Where | Responsibility |
|---|---|---|
| Windows companion | `companion/desk_media_companion/` | session selection, now-playing snapshot with extrapolated position, artwork normalisation, controls, HTTP API, discovery beacon, auto-start |
| `wifi_mgr` | `firmware/components/wifi_mgr/` | station Wi-Fi, exponential-backoff reconnect, SNTP (for the future clock), credentials RAM-only, nothing sensitive logged |
| `pc_media` | `firmware/components/pc_media/` | listens for the beacon (or uses a fixed IP), polls `/now-playing`, fetches + decodes artwork into two fixed PSRAM buffers, queues commands, reports link status |
| `ui` | `firmware/components/ui/` | the LVGL Now Playing screen; thread-safe API; local progress interpolation; callbacks only enqueue |
| `main` | `firmware/main/` | display/LVGL start-up (24 KB LVGL stack on core 1, one draw unit), glue: companion events → UI calls, UI taps → commands, status line; UI-only demo when no Wi-Fi credentials are built in; health/soak logging |

Tasks (ESP32): LVGL task (core 1, prio 4); `pc_poll` and `pc_discovery` (core 0, prio 3); Wi-Fi/lwIP (core 0); health logger (prio 1).
Network code never calls LVGL directly except through the thread-safe `ui_*` functions; LVGL callbacks never block (they post to a queue).

## Protocol (companion HTTP API, version 1)

Base: `http://<pc-ip>:8765` (port configurable). All JSON is UTF-8.

| Request | Answer |
|---|---|
| `GET /now-playing` | JSON state (below). Always 200 while the companion runs; `has_session:false` when nothing is playing |
| `GET /artwork[?size=N]` | `image/jpeg`, square, **baseline** (non-progressive) 4:2:0, default 300x300 (64..640 allowed). Near-square art is centre-cropped; wide/tall art (16:9 video thumbnails) is letterboxed. Headers `ETag: "<art_id>-<size>"`, `X-Art-Id`. `404` when the session has no artwork. Honours `If-None-Match` (304) |
| `POST /control/playpause` · `/control/play` · `/control/pause` · `/control/next` · `/control/previous` | `{"ok":true,"action":"next","app":"Spotify.exe"}`; `ok:false` when the session refuses (control not enabled by that app) |
| `POST /control/seek?position_ms=N` | as above; `ok:false` if the session does not support seeking |
| `GET /health` | `{"ok":true,"service":"desk-media-companion","version":"0.1.0"}` |

`/now-playing` fields: `ok`, `version`, `has_session`, `app` (Windows app id, e.g. `Spotify.exe`, `Chrome`), `state`
(`playing|paused|stopped|changing|opened|closed|none`), `playing`, `title`, `artist` (artist, or channel for videos), `album`,
`position_ms` (already extrapolated to "now" from the session's timeline), `duration_ms` (0 = unknown/live), `track_id` (changes when
the track changes), `art_id` (changes when the artwork changes; `""` = none), `can_play`, `can_pause`, `can_next`, `can_prev`,
`can_seek`, `seq` (bumps on any visible change).

Session choice: the playing session wins (the system's current session if it is playing); otherwise the current session; broken
sessions are skipped. Commands go to the same choice.

**Discovery:** the companion broadcasts `DESKMEDIA1 {"port":8765,"name":"<pc name>"}` on UDP **8766** every 2 s from each real LAN
interface (VPN / virtual adapters skipped). The display uses the packet's source address as the PC's IP. `DEV_COMPANION_HOST` in
`secrets.local.h` pins a fixed IP instead. The first companion heard wins; another is only taken over when the current one stops answering.

**Safety rails (MVP):** the API is unauthenticated plain HTTP, so it is fenced: only private / loopback / link-local clients are served;
the `Host` header must be an IP literal (blocks DNS-rebinding); every `POST` must carry `X-Desk-Client` (a web page cannot add it
cross-origin without a CORS pre-flight, which is never approved - so websites cannot press the buttons); nothing but media metadata and
four transport actions is exposed. A pairing token (set up with the Milestone 11 flow) is the planned hardening.

## Failure behaviour

| Situation | Display |
|---|---|
| Wi-Fi down / reconnecting | status "Connecting to Wi-Fi..." (auto-reconnect with back-off 1→30 s) |
| Companion not found yet | "Looking for your PC" |
| Found earlier, now silent (PC asleep, companion closed, firewall) | last track stays, status "Can't reach your PC"; retries every 2 s; re-discovers if the PC's IP changes |
| Companion up, nothing playing | "Nothing playing - Play something on your PC" |
| Artwork missing / not decodable | placeholder art; retried 3x per image, then given up on |
| App refuses a control | command logged, UI corrects itself on the next poll |

## Memory / performance notes (ESP32)

* Artwork: two permanent 320x320 RGB565 PSRAM buffers (no per-track allocation); download buffer grows once; JPEG decode ~100-200 ms.
* Polling every 1 s of a ~400-byte JSON over a kept-alive HTTP connection; the UI interpolates progress locally (250 ms) so the bar is smooth.
* LVGL task stack 24 KB (measured use ~4 KB at idle); internal RAM stays > 200 KB free; PSRAM > 6 MB free.

## Security / secrets

No Spotify credentials exist anywhere. Wi-Fi credentials: development only, in the git-ignored `firmware/secrets.local.h` (made by
`tools/set-dev-secrets.ps1`); the product keeps them in NVS via the first-run portal (Milestone 11). Never commit credentials, never log them.

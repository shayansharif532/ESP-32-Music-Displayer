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
* **Simple, fast firmware**: plain HTTP on the LAN (no TLS handshakes on the ESP32), small JSON, artwork pre-rendered by the PC at exactly the
  size it is drawn (a square or a landscape card) as a baseline JPEG that the ESP32's decoder handles without scaling or exotic formats.
* **Trade-off accepted**: the PC must be on, on the same network, and running the companion. If it is not, the display says so.

## Components

| Component | Where | Responsibility |
|---|---|---|
| Windows companion | `companion/desk_media_companion/` | session selection, now-playing snapshot with extrapolated position, artwork clean-up and aspect-preserving rendering (`artwork.py`), controls, **per-app volume (`volume.py`, Core Audio sessions)**, HTTP API, discovery beacon, auto-start |
| `wifi_mgr` | `firmware/components/wifi_mgr/` | station Wi-Fi, exponential-backoff reconnect, SNTP (for the future clock), credentials RAM-only, nothing sensitive logged |
| `pc_media` | `firmware/components/pc_media/` | listens for the beacon (or uses a fixed IP), polls `/now-playing` (time-scheduled, never starved by commands), fetches + decodes artwork (square or landscape, up to 432x320) into two fixed PSRAM buffers, queues commands (volume commands coalesced), reports link status and volume results |
| `side_keys` | `firmware/components/side_keys/` | the two side buttons: BOOT (GPIO0) and PWR (AXP2101 key flags polled over I2C) as volume down / up, debounce, hold-repeat (capped at 3 s) |
| `ui` | `firmware/components/ui/` | the LVGL Now Playing screen (square cover or landscape card with the text following it); thread-safe API; local progress interpolation; swipe gestures (left = next track, right = previous; up = next media source, down = previous source); swipe-feedback disc, and the transient pill (the source name with position dots, or "Volume 65%"); callbacks only enqueue |
| `main` | `firmware/main/` | display/LVGL start-up (24 KB LVGL stack on core 1, one draw unit), glue: companion events → UI calls, UI taps → commands, status line; UI-only demo when no Wi-Fi credentials are built in; health/soak logging |

Tasks (ESP32): LVGL task (core 1, prio 4); `pc_poll` and `pc_discovery` (core 0, prio 3); `side_keys` (core 0, prio 4, 20 ms poll of GPIO0 and the AXP2101 key flags); Wi-Fi/lwIP (core 0); health logger (prio 1).
Network code never calls LVGL directly except through the thread-safe `ui_*` functions; LVGL callbacks never block (they post to a queue).

## Protocol (companion HTTP API, version 1)

Base: `http://<pc-ip>:8765` (port configurable). All JSON is UTF-8.

| Request | Answer |
|---|---|
| `GET /now-playing` | JSON state (below). Always 200 while the companion runs; `has_session:false` when nothing is playing |
| `GET /artwork[?size=N[&maxw=W]]` | `image/jpeg`, **baseline** (non-progressive) 4:2:0. Without `maxw`: a square `size`x`size` (default 300; 64..640; wide art letterboxed on a blurred copy - legacy). **With `maxw`: the artwork keeps its aspect ratio inside a `maxw`x`size` box** - square art stays `size`x`size`, 16:9 art becomes a landscape card (432x242 for `size=312&maxw=432`), never stretched; extreme ratios are centre-cropped to 2.4:1 / 0.7:1. Before rendering, app logos baked into the image (Spotify's alpha composite) and symmetric black letterbox bars are removed (`artwork.py`). Headers `ETag: "<art_id>-<size>-<maxw>"`, `X-Art-Id`. `404` when the session has no artwork. Honours `If-None-Match` (304) |
| `POST /control/playpause` · `/control/play` · `/control/pause` · `/control/next` · `/control/previous` (`&app=<app id>`) | `{"ok":true,"action":"next","app":"Spotify.exe"}`; `ok:false` when the session refuses (control not enabled by that app). **`app` = the app the display shows: the command goes to exactly that session and is refused (`"that media session is gone"`) if it no longer exists** - never to another session. Without `app`: the session the companion would show |
| `POST /control/seek?position_ms=N` (`&app=`) | as above; `ok:false` if the session does not support seeking |
| `POST /control/source-next` · `/control/source-prev` (`?from=<app id>`) | the vertical swipe: pins the display to the next / previous **usable media source** (playing or paused Windows media session with a title / artist; fixed alphabetical ring, wraps around) relative to `from`. `{"ok":true,"changed":true,"app":"chrome.exe","source":"Chrome","index":1,"count":2,"now":{<the new /now-playing>}}`; `changed:false` when there is only one source |
| `POST /control/source-auto` · `GET /sources` | back to automatic selection · the swipe list `{"mode":"auto"\|"pinned","displayed":"...","sources":[{app,name,state,title,artist,displayed}]}` |
| `GET /volume` | the **per-application** volume of the displayed app (its Windows audio-session volume, never the master volume): `{"ok":true,"app":"Spotify.exe","volume":65,"muted":false,"sessions":1}`; `ok:false` + `"error":"no audio session found for ..."` when the app has none |
| `POST /control/volume-up` · `/control/volume-down` (`?step=5`, `&app=<app id>`) · `POST /volume?level=0..100` | same answer with the new volume. `app` = the app the display is showing; the volume goes to that app's audio session and is refused if it no longer has a media session (no `app`: the session the companion would show). Core Audio session mapping and browser limits: `docs/milestones/M8b-art-layout-gestures-volume.md` |
| `GET /health` | `{"ok":true,"service":"desk-media-companion","version":"0.1.0"}` |

`/now-playing` fields: `ok`, `version`, `has_session`, `app` (Windows app id, e.g. `Spotify.exe`, `Chrome`), `state`
(`playing|paused|stopped|changing|opened|closed|none`), `playing`, `title`, `artist` (artist, or channel for videos), `album`,
`position_ms` (already extrapolated to "now" from the session's timeline), `duration_ms` (0 = unknown/live), `track_id` (changes when
the track changes), `art_id` (changes when the artwork changes; `""` = none), `can_play`, `can_pause`, `can_next`, `can_prev`,
`can_seek`, `seq` (bumps on any visible change), and the source fields `source` (short app name, "Spotify" / "Chrome" / "Edge"), `sources` (how many
sessions a vertical swipe can switch between), `source_index` (1-based position in that ring), `pinned` (the user selected this session).

Session choice (**auto**): the playing session wins (the system's current session if it is playing); otherwise the app that played last; otherwise the
system's current session; broken sessions are skipped. **Pinned** (after a vertical swipe, ADR-005): the display, its controls and its volume stay on the selected session for as long as it exists - another app
becoming active does not take over - and fall back to auto when it disappears. Commands go to exactly the session being shown (the display names it in every request).

**Discovery:** the companion broadcasts `DESKMEDIA1 {"port":8765,"name":"<pc name>"}` on UDP **8766** every 2 s from each real LAN
interface (VPN / virtual adapters skipped). The display uses the packet's source address as the PC's IP. `DEV_COMPANION_HOST` in
`secrets.local.h` pins a fixed IP instead. The first companion heard wins; another is only taken over when the current one stops answering.

**Safety rails (MVP):** the API is unauthenticated plain HTTP, so it is fenced: only private / loopback / link-local clients are served;
the `Host` header must be an IP literal (blocks DNS-rebinding); every `POST` must carry `X-Desk-Client` (a web page cannot add it
cross-origin without a CORS pre-flight, which is never approved - so websites cannot press the buttons); nothing but media metadata and
four transport actions is exposed. A pairing token (Milestone 13) is the planned hardening.

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

* Artwork: two permanent 432x320 RGB565 PSRAM buffers (no per-track allocation), one RGB888 decode scratch and two 480x480 background images; download buffer grows once; JPEG decode + background ~100-400 ms.
* Polling every 1 s of a ~400-byte JSON over a kept-alive HTTP connection; the UI interpolates progress locally (250 ms) so the bar is smooth.
* LVGL task stack 24 KB (measured use ~4 KB at idle); internal RAM stays > 200 KB free; PSRAM > 6 MB free.

## Security / secrets

No Spotify credentials exist anywhere. Wi-Fi credentials: development only, in the git-ignored `firmware/secrets.local.h` (made by
`tools/set-dev-secrets.ps1`); the product keeps them in NVS via the first-run Wi-Fi portal (Milestone 11). Never commit credentials, never log them.

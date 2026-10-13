# MVP: Windows media companion + ESP32 display (Milestones 3-7)

Date: 2026-10-12. Branch: `feature/mvp`. Architecture: [../architecture.md](../architecture.md) (ADR-003).
The maintainer replaced the Spotify Web API plan with a Windows media-session companion the same day; the Spotify code was removed from the tree later (it lives in git history under the tag `legacy-spotify-web-api`).

## What was built

* **Companion** (`companion/`): session selection (playing session first), snapshot with extrapolated position, artwork normalised to a square 300x300
  baseline JPEG (crop near-square, letterbox wide), controls, HTTP API, UDP discovery beacon, per-user auto-start flag, single-file exe build
  (`companion/build.ps1`, 16.7 MB).
* **Firmware** (`firmware/components/{wifi_mgr,pc_media}`, `main/main.c`): Wi-Fi with back-off reconnect; beacon discovery or fixed IP; 1 Hz poll over a
  kept-alive HTTP connection; artwork download + decode into two fixed PSRAM buffers; commands queued from the UI.

## Verified (evidence)

| Check | Result |
|---|---|
| Companion API, curl against a silent fake media session (`companion/tools/fake_media_session.py`) | `/health`, `/now-playing`, `/artwork` (300x300 **baseline** JPEG, ETag -> 304), `POST next/seek` change the session, POST without `X-Desk-Client` -> 403, bad `Host` header -> 403 |
| Real YouTube playback (Big Buck Bunny, muted, in the Claude desktop app's embedded browser = Chromium Media Session -> Windows) | companion reports title, channel ("Blender"), 634.6 s duration, real thumbnail (17.5 KB JPEG), `can_pause`/`can_seek`; `POST /control/pause` paused the video, `/control/play` resumed it, `/control/seek?position_ms=120000` moved it to 121 s |
| **Wi-Fi** (M3) on the board | joined at -51 dBm (IP via DHCP). Self-test (`-DAPP_WIFI_SELFTEST=1`): link dropped on purpose -> **recovered in 2048 ms**; `pc_media` resumed ~1.3 s later |
| **Discovery** (M5) | board found the PC by the UDP beacon ~1 s after getting an IP (no IP configured anywhere) |
| **Now-playing + artwork** (M5/M6) | board logged the track, then "artwork ready (300x300, 8,973 bytes JPEG) in 135 ms"; later 17,480 bytes in 160 ms (YouTube) |
| Session switching | board followed fake session -> YouTube -> fake session as the playing session changed, re-fetching artwork each time |
| Stability | 3+ minutes on the network build: no reset, no panic, LVGL stack minimum free 20,156 B of 24,576 B, internal RAM free ~158 KB (min 117 KB), PSRAM free ~5.9 MB |
| Bug found + fixed during testing | a `play` command reached a stale session (`PSExpressCore.exe`) instead of the one on screen; controls now use exactly the session the display shows (`MediaMonitor._select`). Re-tested: `play` -> `claude.exe` (YouTube) |
| Board keeps working unattended | after several minutes with no serial reader the board was still polling the companion once per second (seen in the companion's debug log) and re-attached to a restarted companion by itself |
| Packaged exe | `DeskMediaCompanion.exe` (windowed, no console) answered `/health`, `/now-playing`, `/artwork` correctly on a spare port |

## Maintainer test with real media (2026-10-12, 15-minute serial log `mvp-soak-final.log`, read by Claude afterwards)

The maintainer tapped the board and played real media while a serial reader was attached. From the log (not from looking at the screen):

| Check | Result |
|---|---|
| Physical touches | **24 presses**, zero resets / panics / stack overflows in 941 s (90 health lines); LVGL stack minimum free 20,188 B; internal RAM min 119 KB, flat; PSRAM free flat at 5.9 MB |
| **Spotify desktop** (`Spotify.exe`) | recognised: tracks, ads ("Apply now for grades K-6!"), artwork on every change (110-177 ms), play/pause + next executed (`control ... on Spotify.exe -> ok` in the companion log) |
| **YouTube / YouTube Music in Chrome** (`chrome.exe`) | recognised: titles + channel/artist, artwork, play/pause + next ok |
| Controls from the board | `/control/playpause` x14, `/control/next` x8 ok; `previous` and `next` on a single YouTube video correctly reported `ok:false` ("refused") and the UI stayed consistent |
| Session switching | followed Chrome <-> Spotify <-> embedded browser as the playing session changed |

Observed and handled afterwards: when everything is paused the display once jumped to a stray `Riot Client.exe` session -> the companion now prefers the app that played last.

## Not yet confirmed

1. **Visual check by the maintainer**: layout, colours, letterboxed 16:9 art, scrolling long titles - the logs prove the data path, not how it looks. No problem has been reported.
2. **Seek by dragging the bar** and the **art tap / full-screen toggle** are not distinguishable in the log (no command is logged for the toggle; no `seek` command appears in this log).
3. **Windows Firewall on the user's PC** (fine on the dev PC).

## (earlier list, kept for history) NOT yet verified before the maintainer test

1. **Touch on the network build**: taps on prev / play-pause / next / seek / art-toggle. The command path (UI -> `pc_media_command` -> companion -> session) is proven
   by curl and by the firmware code, but no physical tap has been logged yet (`touches=0` in every health line so far). A multi-minute touch soak is also still owed (M2 acceptance).
2. **Spotify desktop** (not running on the dev PC) and **YouTube in Chrome/Edge** (verified only through an embedded Chromium). Same Windows API; expected to work, to be confirmed.
3. **Visual check** of the layout with real artwork (colours, letterboxing, scrolling long titles) - only the maintainer can see the screen.
4. **Windows Firewall on the user's PC**: on the dev PC the board connected without a manual rule. A fresh PC will show the Windows prompt once; documented in `companion/README.md`.

## Known issues / limits

* **Dev-only:** after a long period with *no* serial reader attached (or a reader that stalls), the board's USB-Serial/JTAG console output can stop until the next reset. The firmware itself keeps running (verified by the companion log). Keep `serial_capture.py` attached - and use `--reset` - when you need logs; this does not affect normal use.
* Fonts: Montserrat has no CJK / Cyrillic / emoji glyphs - such titles render with missing characters (many YouTube titles). Needs a fallback font (polish milestone).
* `can_next` / `can_prev` are reported but the UI does not dim the buttons yet.
* Single companion, no pairing: any device on the LAN could press the buttons (fenced as described in architecture.md). Pairing token planned for Milestone 11.
* When one app is paused and another is playing, the display shows the playing one (by design).
* Wi-Fi password is compiled into the dev firmware from the git-ignored `secrets.local.h`; the product moves it to NVS (Milestone 11).

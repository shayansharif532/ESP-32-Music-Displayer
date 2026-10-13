# Project plan and status log (moved out of CLAUDE.md)

The original project brief, the milestone plan and the running status notes, kept for the record. They describe the project **as it was
planned and built, in order**; where they disagree with the current code or with [../architecture.md](../architecture.md),
[../protocol.md](../protocol.md) or the milestone logs, those win. The current state of the project is summarised in the
[root README](../../README.md) ("Project status"); the rules for working on the repository are in [../../CLAUDE.md](../../CLAUDE.md).

Important commits: baseline / Milestone 0 `95f724d`, Milestone 1 `d64ad06` (if these hashes are not present after cloning, inspect the
actual history). Waveshare reference material was downloaded under `docs/waveshare/` (git-ignored, ~700 MB, re-fetchable).

Two things changed after most of this was written: Milestone 13 (pairing / security) was implemented as protocol version 2
(see [../protocol.md](../protocol.md)), and the parked Spotify Web-API client was removed from the tree (it lives in git history under the tag
`legacy-spotify-web-api`; commit `db51d85` introduced it).

---

## Roadmap change (2026-10-12, later that day)

A one-off intro screen was dropped. Priority order after the MVP:

A. Main player visual redesign (done, Milestone 8) -> B. Customisable idle screen (M9, built; replaced an earlier theme-specific idle-screen plan - a personal theme is a local asset, not part of the generic product) -> C. Full-screen album-art polish (M10) -> D. First-run Wi-Fi setup (M11) -> E. Companion installer (M12) -> F. Pairing / security (M13).

---

## Architecture change (2026-10-12)

**Spotify OAuth / the Spotify Web API is no longer the architecture.** The device is built for a Windows PC, so:

- A small **Windows companion app** (`companion/`, Python + WinRT `Windows.Media.Control` / GlobalSystemMediaTransportControlsSessionManager) detects the active media session, and serves title / artist / album / playback state / position / duration / artwork over a small **HTTP API on the local network** (`GET /now-playing`, `GET /artwork`, `POST /control/{playpause,next,previous,seek}`).
- The **ESP32 firmware polls the companion** (it finds the PC automatically through a UDP beacon) and sends control commands back. No Spotify account, Client ID, Premium, tokens, TLS or internet access is needed.
- The old Spotify client is **obsolete** and was removed from the tree (git history, tag `legacy-spotify-web-api`).
- Design: [architecture.md](../architecture.md); decision record: [ADR-003](../decisions/ADR-003-windows-media-session-companion.md). Wherever the rest of this file still says "Spotify", read it as "the companion / Windows media session".

---

## Milestone Status

### Milestone 0 — DONE

Completed:

- Exact hardware detected.
- COM/USB connection verified.
- Flash and PSRAM identified.
- Factory boot log inspected.
- Factory image backed up and verified.
- ESP-IDF + LVGL chosen.
- Baseline committed.

### Milestone 1 — DONE / PHYSICALLY VERIFIED

Completed:

- ESP-IDF 5.5.5 installed.
- Official Waveshare LVGL demo built successfully.
- Display firmware flashed successfully.
- ST7701 display initialises.
- GT911 touch initialises.
- No boot loops or reset errors observed.
- PSRAM/internal RAM remained stable.
- User physically confirmed the LVGL demo is visible and properly rendered.
- Display appeared correctly aligned with normal colours and no visible tearing/flicker.
- Diagnostic touch support was added for physical validation.

Before assuming touch calibration is perfect on another build, preserve or rerun the touch diagnostic if needed.
(See "Known issues" at the end: the M1 demo crashed on some touches. The M2 display config - 24 KB LVGL stack, one draw unit - is our answer; a pristine-demo gate on 2026-10-12 ran 7 minutes without a crash.)

### Milestone 2 - DONE (24 real touches over 941 s on the network build, no reset)

Own 480x480 UI in `firmware/components/ui` (placeholder art box, title, artist, seek bar, elapsed/total, prev/play-pause/next, tap-art full-screen toggle, status line). Evidence: [`M2-ui.md`](M2-ui.md).

### Milestones 3-7 + MVP gate - DONE (merged to `main`)

Wi-Fi + auto-reconnect (link dropped and recovered in ~2 s), the Windows companion, discovery, now-playing, artwork, commands. Verified with real Spotify desktop and Chrome (YouTube etc.) plus 24 physical touches. Evidence and open points: [`MVP-media-companion.md`](MVP-media-companion.md). The maintainer has not yet given a visual confirmation of the screen - do not claim the layout is visually verified until they do.

---

## Remaining Milestones

### Milestone 2 - Custom 480 x 480 UI + Touch (done, see status above)

Required prototype screen: album-art placeholder, song title, artist, progress bar, elapsed / total time, previous, play/pause, next, full-screen-art toggle. Acceptance includes a multi-minute touch soak with zero resets (the health log line's `touches=` counter proves taps happened).

### Milestone 3 - Wi-Fi (done in firmware)

Stable Wi-Fi with automatic reconnect (exponential back-off). Credentials: temporary developer credentials only in the git-ignored `firmware/secrets.local.h` (`tools/set-dev-secrets.ps1`); never in the product, never committed, never logged.

### Milestone 4 - Windows companion (replaces "HTTPS/API")

`companion/`: GSMTC wrapper, HTTP API, UDP discovery beacon, per-user auto-start option. Runs quietly in the background. See [architecture.md](../architecture.md) and [protocol.md](../protocol.md) for the protocol.

### Milestone 5 - ESP32 <-> companion: now-playing + progress (replaces "Spotify authentication")

`firmware/components/pc_media`: discovery, poll `/now-playing` about once per second, keep UI responsive (UI interpolates progress locally). Never block the LVGL task.

### Milestone 6 - Album artwork

The companion cleans any thumbnail (removes the Spotify logo composite and black letterbox bars) and renders a **baseline** JPEG at exactly the size the display draws it: a square cover, or - for 16:9 video thumbnails - a landscape card, aspect ratio always kept (`GET /artwork?size=312&maxw=432`); the ESP32 downloads it only when `art_id` changes and decodes it into two fixed PSRAM buffers (no allocation churn). Album art should be the visual focus of the product.

### Milestone 7 - Playback controls

Previous / play-pause / next / seek via `POST /control/...`, applied by the companion to the same media session. Gracefully handle: nothing playing, app refuses a control (`ok:false`), companion unreachable (PC asleep / app closed / firewall), Wi-Fi loss.

### MVP gate (before any polish)

Works with **Spotify desktop** and **YouTube in a supported browser**: real artwork, title + artist/channel, working progress bar, play/pause, next/previous where the media session supports them.

### Milestone 8 - Main player visual redesign (DONE 2026-10-12; evidence: [`M8-player-redesign.md`](M8-player-redesign.md))

The maintainer found the MVP screen too prototype-like and asked for: a much bigger cover as the focal point, no card/border around it, the **whole
screen background derived from the cover** (blurred, darkened), no empty side borders, a clean readable foreground, and a finished-product look.

Built (`firmware/components/ui/ui.c`, `firmware/components/pc_media/pc_art.c`):

- cover 312x312 (was 260) with no frame - the box only rounds the corners; wide/tall art (16:9 video thumbnails) is kept whole and the rest of the square
  is filled with a blurred copy of itself (done in the companion), so there are no dead bars;
- background = one pre-baked RGB565 image per track: cover -> 40x40 colour grid -> saturate -> smooth -> smooth-step upscale to 480x480 -> darkened with a vertical
  gradient (extra dark for bright covers) -> soft shadow behind the cover -> ordered-dithered (no banding). LVGL only blits it (no overlays, no runtime blur);
- the cover is decoded to RGB888 and dithered down to RGB565 (smooth gradients in the art do not band);
- title (26 pt) and artist (20 pt) centred below, thin progress line as wide as the cover with the times at its ends (knob appears only while dragging),
  big white play/pause button, plain prev/next icons, dimmed when the session does not support them;
- the no-artwork look is a dithered dark gradient with a translucent placeholder square.

Rules for later work on this screen: keep the foreground clean; do not add LVGL shadows/blur/overlays over the full screen (they redraw every progress tick);
verify visually with `tools/screenshot.py` (see [development.md](../development.md#board-in-the-loop-and-developer-tools)) instead of guessing.

### Milestone 8b - Refinement pass (DONE 2026-10-12; evidence: [`M8b-art-layout-gestures-volume.md`](M8b-art-layout-gestures-volume.md))

After the maintainer reviewed M8 on the real display:

- **Widescreen art** (YouTube) is a landscape card (432 px wide for 16:9, rounded, never stretched) instead of a letterboxed square; square covers are unchanged; the title / artist follow the card.
  Chromium hands Windows only ~150 px thumbnails, so cards are soft - a Windows limit.
- **The Spotify logo is gone**: Windows gives a 300x300 RGBA composite (opaque 234x234 cover, transparent gap, logo). `artwork.split_composite` splits it structurally (alpha bands), never by a fixed crop; anything
  ambiguous is left untouched. Black letterbox bars are trimmed.
- **Swipe gestures**: >= 72 px mostly horizontal, < 1 s; consumed so nothing is clicked; never from the progress bar; no-op when the app has no next / previous. *(M8b had right = next; reversed in M8c: left = next, right = previous.)*
- **Per-app volume** via Core Audio sessions (`companion/desk_media_companion/volume.py`): `GET /volume`, `POST /control/volume-up|volume-down[?step=5&app=...]`. Never the master volume (verified).
  Browsers: whole-browser volume (shared audio process); Firefox not matchable.
- **Side buttons = volume**: PWR (upper) = up, BOOT (lower) = down (`firmware/components/side_keys`), 5 % steps, hold-repeat capped at 3 s, transient "Volume 65%" pill.
  **Do not hold PWR > ~4 s (the PMIC powers the board off). BOOT at power-up is the ROM download-mode gesture, so Milestone 11's "hold BOOT at power-up to wipe Wi-Fi" must become a long press while running.**
- Rules learned: the display must name the app it shows in volume requests; never let a held key flood the command queue (poll is time-scheduled); overlays must be children of the screen (the screenshot tool does not render `lv_layer_top`).

### Milestone 8c - Media-source switching (DONE 2026-10-12; evidence: [`M8c-session-switching.md`](M8c-session-switching.md); ADR-005)

Maintainer's second refinement request, after using M8b:

- **Horizontal swipes reversed**: finger LEFT = next track, RIGHT = previous (same thresholds / safeguards; seek-bar swipes still only seek; tap on the cover still toggles full screen).
- **Vertical swipes switch the media source**: finger UP = next, DOWN = previous source (= another app's Windows media session; playing **and paused** ones with a title / artist; dead sessions such as `PSExpressCore.exe`
  are left out; fixed alphabetical ring that wraps; nothing happens with a single source). The selection lives in the companion (`desk_media_companion/sources.py` = pure model + 43 unit tests; `media.py` applies it):
  **auto** follows the best session, a swipe **pins** the display to a session until that session disappears (another app becoming active does not take over). Feedback: a faint chevron disc + a translucent pill with the
  source name and position dots (~1.1 s, shared with the volume pill); no permanent widgets.
- **Every command names the session on screen** (`app=` on all `/control/*` and volume requests; `from=` on source switches). The companion sends it to exactly that session and refuses it if that session is gone - so controls,
  seek, play/pause and the side-button volume always follow what is displayed, immediately after a switch. A paused source shows its artwork, title, timeline and the play icon; play resumes that session.
- **Chrome / YouTube artwork**: investigated before accepting 150 px. GSMTC exposes one thumbnail stream and no URL / size variants; Windows passes an app's image through byte-identically (a 1280x720 PNG published by the fake
  session came back unchanged); Chromium itself requests artwork at `kMinImageSize = 71` / `kDesiredImageSize = 150` (`system_media_controls_notifier.cc`). So **no higher-resolution image exists without a browser extension**.
  PC-side up-scaling was improved (`artwork.enhance_upscale`: edge-only, halo-limited, strength ramps with the enlargement; Spotify-sized enlargements are bit-identical). An extension (reads the page's own MediaSession artwork list)
  is documented as an option in the M8c doc, deliberately not built.
- *(ADR-006 / M8d changed the "pin until it disappears" rule below into a temporary manual selection that auto-follow ends; see Milestone 8d.)*
- Rules learned: a dev action that only changes the *selection* (`swipe_up` / `swipe_down`) is safe on real sessions, everything else is guarded; end every test by `POST /control/source-auto` so the maintainer's display is not left pinned.

### Milestone 8d - Auto-follow restored + Spotify-only shuffle / repeat (DONE 2026-10-12; evidence: [`M8d-auto-follow-spotify-options.md`](M8d-auto-follow-spotify-options.md); ADR-006)

Maintainer's third refinement request, after using M8c ("a manual swipe must not disable auto-follow for good"):

- **Selection is an explicit state machine** (`companion/desk_media_companion/sources.py`, `Selector`, pure + unit-tested): `AUTO` follows the playing session that started playing **most recently** (a 0.4 s background watcher tracks the transitions);
  a vertical swipe enters `MANUAL_TEMP`, which ends - back to AUTO - when **another** session starts playing, when the selected session disappears, or (only if the selected session is not playing while another one is) after **20 s** without
  touching the selected session. A selected session that starts playing stays. No permanent pin any more; `mode` (auto / manual) replaces `pinned` in `/now-playing`, `/sources` and the swipe answer.
- **Commands unchanged in principle**: every request still names the displayed session (`app=`), so after an auto-switch the next poll (<= 1 s) moves the screen and controls / seek / volume follow it.
- **Spotify-only Shuffle and Repeat**: investigated first - Windows' GSMTC exposes both for Spotify (state + enabled flags + accepted change requests, three repeat modes: off / playlist / track, ~100 ms to settle), so **no OAuth and no Web API**.
  The companion reports `spotify`, `shuffle`, `repeat` (0/1/2), `can_shuffle`, `can_repeat` (Spotify identified by the session's **app id**, never by metadata) and offers `POST /control/shuffle` / `/control/repeat` (refused for every other app).
  On the display two small buttons flank the transport row only while Spotify is shown (shuffle left of previous, repeat right of next; green + dot when on, "1" on the icon for repeat-track, dimmed when Spotify refuses);
  previous / play / next did not move; Chrome / Edge / VLC / everything else looks exactly as before.
- Test tooling: `tools/auto_follow_test.py` (isolated: a companion that sees only two silent fake sessions - `python.exe` treated as Spotify via `--treat-as-spotify`, and `deskfakeb.exe`, a renamed copy of the interpreter made by
  `companion/tools/fakes.py` - so cases A-E and the Spotify buttons run through the real companion and the real Windows API without touching the maintainer's apps or display); dev flags `--ignore-apps`, `--only-apps`, `--treat-as-spotify`;
  `tools/session_test.py` is the board-in-the-loop version. Rule: while developing, restart the product companion with `--ignore-apps python.exe,deskfakeb.exe` so fake sessions never reach the maintainer's display.

### Milestone 9 - Customisable idle screen (BUILT 2026-10-12; evidence: [`M9-idle-screen.md`](M9-idle-screen.md); ADR-007)

Replaces the generic "Nothing playing" player placeholder when the PC companion is connected and nothing plays. A **generic product feature**: no theme is tied to a specific person or franchise; a user's photo / name / custom theme is local data chosen on their own PC and never committed.

- **Screens**: boot / Wi-Fi / searching / PC unreachable = the player screen with its connection message (never idle); connected + something plays = player; connected + nothing plays (no session or only paused ones) = **idle screen** after a configurable delay (default 4 s; at once when nothing has played since connecting), left **immediately** when anything plays; 350 ms fades; tap the idle screen to peek at a paused session's player for 30 s.
- **Companion** (`idle.py`, `ui.py`): tray icon + Settings window (tkinter, pystray): theme Minimal / Dark / Neon / Photo, accent, clock (12/24 h), one custom line, delay, Photo background with Fill / Fit / Blur Fill and a live preview; settings in `%LOCALAPPDATA%\DeskMediaCompanion\idle`; pictures validated and rendered to a 480x480 baseline JPEG <= 200 KB on the PC; `GET /idle`, `GET /idle/image`; `/now-playing` gains `any_playing`, `idle_v`, `clock_ms`. Settings are editable only in-process (no HTTP write route).
- **Firmware** (`components/idle_screen`): its own LVGL screen; settings in NVS, picture in the new `idle_img` flash partition (carved out of the unused `storage`); version-based sync from `pc_media` (only when `idle_v` differs); dithered gradients for the built-in themes, photo decoded once + dithered; generated large clock font (`tools/gen_clock_font.py`); fallbacks to the Dark theme for any picture problem.
- **Personal / generic separation (maintainer rule)**: the framework is committed; personal assets are not. `.gitignore` blocks `idle/settings.json`, `idle/background.jpg`, `idle/original.bin`, `idle-assets/`, `personal/`, `private/`. Before committing anything near this feature, grep the staged files for personal names and image file names.
- Test tooling: `tools/idle_test.py` (board-in-the-loop with an in-process companion that sees only two silent fake sessions: transitions, delay, picture + reboot persistence, companion restart, Fill / Fit / Blur Fill by pixel checks, the clock against the real typeface, corrupt pictures, and a repeated idle <-> playback soak with heap / stack / reset accounting).

### Milestone 9b - the idle screen as an item of the vertical source ring (BUILT 2026-10-12; evidence: [`M9b-idle-source.md`](M9b-idle-source.md); ADR-008)

Maintainer request after using M9: `Spotify <-> Chrome <-> Idle Screen`. The ring (`sources.ring(..., idle=True)`) always ends with the synthetic item `idle` (`IDLE_ID`; never a Windows session; offered whenever the companion is connected). Swiping onto it = a MANUAL_TEMP selection (`Selector.idle_selected`): a display choice only - media keeps playing, nothing is sent to any app; `/now-playing` has `idle_selected` and counts the item in `sources` / `source_index`;
it ends when **any session starts playing** (auto-follow), by `source-auto` or a tap, and it never times out (the 20 s return does not apply). While Idle is shown the side volume keys and horizontal swipes do nothing and every `app=idle` request is refused; vertical swipes work on the idle screen (`ui_attach_vertical_swipes`) and the source pill follows the visible screen. The display sends `from=idle` when it shows the idle screen (selected or automatic).
Tests: `tools/idle_source_test.py` (isolated, API level), the `carousel` phase of `tools/idle_test.py` (board: real swipes, keys, auto-follow, tap), 13 new model tests in `test_sources.py`.

### Milestone 10 - Full-screen album-art polish

The tap-the-cover full-screen mode works (cover grows to 456x456, chrome hides, tap again to return) but the cover is the 312 px image stretched, so it is soft.
Polish: request a larger cover from the companion for this mode (e.g. `/artwork?size=480`, needs a bigger decode buffer / scaling), smooth the transition, keep the blurred
background, make the tap target and a hint obvious, handle wide art, and keep the screen free of clutter.

*(A one-time intro screen that used to be Milestone 10 was dropped on 2026-10-12 and is no longer part of the plan.)*

### Milestone 11 - First-Run Wi-Fi Setup

Final user onboarding should not require a PC.

Preferred flow:

1. Device starts setup mode if Wi-Fi is not configured (or the PC companion is not paired).
2. Device creates a temporary setup AP.
3. User connects from phone.
4. Local/captive setup page opens.
5. User selects Wi-Fi and enters password.
6. Device stores Wi-Fi credentials securely in NVS.
7. Device finds the PC companion (already automatic) and returns to normal Now Playing mode.
8. Future boots reconnect automatically.

Provide an easy reset/reconfiguration path. NOT "hold BOOT at power-up" (that is the ROM download-mode gesture, and BOOT is the volume-down key while running): use a long press of BOOT while the app is running, or a button on the setup / status page.

### Milestone 12 - Companion installer

Single `.exe` (PyInstaller, `companion/build.ps1` already builds it) for end users, auto-start with Windows (`--install-startup`, per-user Run key), a one-time Windows Firewall helper (the first run asks to allow the port; a script that adds a rule needs the maintainer's UAC click - Claude must not change system/firewall settings itself), tray/quit option, update story.

### Milestone 13 - Pairing / security

*(Implemented later as protocol version 2: see [protocol.md](../protocol.md). The text below is the original plan.)*

Display and companion pair once (shared token, shown on the display and typed/confirmed on the PC, or scanned) so the unauthenticated LAN API is closed to every other device; token stored in NVS on the board and in the companion's per-user config; requests carry it; wrong/missing token -> 401. Revisit the beacon (do not advertise to unpaired displays more than needed).

---

## Product Behavior

Normal startup after setup:

```
Power
  ↓
Boot
  ↓
Connect Wi-Fi
  ↓
Find the Windows companion (UDP beacon) / reconnect
  ↓
Poll the PC's media session
  ↓
Now Playing OR Idle
```

The display must not require the development PC during normal operation.

The board should recover gracefully from:

- router unavailable at boot
- temporary Wi-Fi loss
- PC asleep / companion closed / PC firewall blocking (show "Can't reach your PC", keep retrying)
- the PC's IP address changing (re-discover)
- no active media session
- playback stops
- playback resumes
- malformed / missing album artwork
- reboot / power loss

---

## Main UI Requirements

Target resolution: **480 x 480**

Main screen should contain:

- large square album cover
- song title
- artist
- elapsed time
- total duration
- playback progress
- previous control
- play/pause control
- next control

Optional after core functionality is stable:

- source app of the media session (already shown in the status line)
- volume if reliable
- shuffle/repeat state

Do not prioritize optional features over reliability.

### Full-Screen Art Mode

Tap album art:

- controls/text disappear
- album art becomes dominant / nearly full-screen

Tap again:

- return to normal player UI

---

## User Experience Goal

The finished device should feel like:

- plug in USB-C
- boot quickly
- connect automatically
- show beautiful current music
- tap to control playback
- look good sitting on a desk

It should **not** feel like:

- an ESP32 demo
- a debug dashboard
- a tiny web browser
- a cluttered smart-home panel
- something requiring a laptop every time

---

## Enclosure Direction

Hardware currently has a white front bezel.

Final physical direction:

- keep front visually clean
- hide rear PCB completely
- leave required USB-C access
- integrate a stable desk stand
- approximately 10–15 degree rearward viewing angle if practical
- no exposed jumper wires
- no soldering required for the planned core build
- enclosure design comes after firmware/UI layout is stable enough to know orientation and port needs

Preferred final finish: simple, consumer-like, understated.

---

## Scope Guardrails

Do not add these until the primary product is complete:

- lyrics
- playlist browsing
- search
- queue editor
- Bluetooth audio output
- local speaker playback
- weather
- smart-home controls
- AI chatbot
- complex audio visualizers

They may be future enhancements, but they must not delay the core music display.

---

## Known issues / corrections (added 2026-10-12)

These supersede the statements above they contradict.

- **Milestone 1 touch-test crash.** "No boot loops or reset errors observed" describes the *idle* board only. When the touchscreen was
  exercised, the official-demo + `m1_probe` build crashed and rebooted 4 times (2 memory-access panics, 2 `taskLVGL` stack overflows), always
  within ~0.25 s of a tap in the demo's top tab-bar band; 8 other taps were fine. Root cause is **not yet established**. Do **not** treat the
  demo's LVGL/task configuration as "known-good" for touch. In Milestone 2: own the display init (larger LVGL task stack - the esp_lvgl_port
  default is 7168 B; one draw unit instead of Waveshare's `CONFIG_LV_DRAW_SW_DRAW_UNIT_CNT=2` unless a soak test proves two are safe; no logging
  or relabelling inside LVGL-task timer callbacks), and make a multi-minute touch soak test (screen edges, top band, rapid taps; zero resets in
  the serial log) an acceptance criterion. Evidence, decoded backtraces and candidate causes: [`M1-lcd-touch.md`](M1-lcd-touch.md), "Touch-test addendum".
- Touch coordinate orientation and corner coverage were not independently verified (no corner taps were recorded).
- Never assume the serial port or the ESP-IDF location: use `tools\find-board.ps1`; `tools\idf.ps1` and `tools\serial_capture.py` auto-detect both.

# Milestone 8b — widescreen art layout, no app logos, swipe gestures, per-app volume, side buttons

Date: 2026-10-12. Branch: `feature/art-layout-gestures-volume`. Request (owner, after physically reviewing M8): fix how 16:9 YouTube thumbnails look,
get the Spotify logo off the screen, add swipe gestures, per-application volume through Windows, and use the two side buttons for volume.

![Square and widescreen artwork on the real display](img/m8b-art-layouts.jpg)

*Real screenshots from the board (`tools/screenshot.py`), left to right / top to bottom: square cover (unchanged layout), 16:9 HD frame as a landscape card, 4:3 frame with baked-in black bars (bars trimmed),
5:1 banner (clamped to 2.4:1), a Spotify-style composite (only the cover block is shown, no logo), the swipe-feedback disc, the "Volume 65%" pill, and full-screen mode of a landscape cover.
All covers here are synthetic (`companion/tools/make_test_covers.py`); no third-party artwork is committed. The checks with the owner's real Spotify and Chrome / YouTube Music sessions were done on the real
display too (screenshots kept locally in the git-ignored `logs/`): Spotify showed only the cover with a tiny "Spotify" label; the Chrome / YouTube Music session showed its real 16:9 thumbnail as a landscape card.*

## 1. Widescreen artwork: a landscape card instead of a letterboxed square

Before, the companion put any non-square image on a blurred square canvas, so a 16:9 thumbnail looked like a small picture inside a square "card" with
soft grey strips above and below. Now the cover keeps its own shape:

| Art | Shown as |
|---|---|
| square (aspect 0.88 - 1.14) | large square 312x312, exactly as before (layout unchanged, verified pixel-for-pixel by eye) |
| wider (16:9, 4:3, ...) | **landscape card** up to 432 px wide (16:9 -> 432x242), rounded corners, never stretched; very wide banners are centre-cropped to 2.4:1 |
| taller than 0.88 | narrower portrait card (min. 0.7:1, centre-cropped beyond that) |

* The companion renders exactly the size the display draws (`GET /artwork?size=312&maxw=432` -> a JPEG of that shape); the ESP32 decodes it without scaling.
* The layout adapts: for a landscape card the card + title + artist are centred as a group between the status line and the progress bar
  (`ui_art_area()` in `ui.c`), so nothing is cramped and nothing floats. The progress bar and controls never move.
* The baked background shadow follows the card (the decoder asks `ui_art_area()` where the cover will be drawn).
* Full-screen mode (tap the cover) keeps the aspect ratio: longest side 456 px, centred.
* **YouTube thumbnails with baked-in black bars** (4:3 frames from old uploads) have the symmetric pure-black bars removed (`artwork.trim_black_bars`) so the
  card is the real picture. Conservative: both bars must exist, be roughly equal, >= 4 % of the side, and leave >= 35 % picture.
* Limitation, stated honestly: **Windows hands over Chromium thumbnails at ~150 px** (measured: 150x83 for a YouTube video, 150x31 for a wide banner), so a 432-px card is
  an ~2.9x enlargement (Lanczos + light unsharp mask). It is clean but soft; there is no larger source in the media session.

## 2. The Spotify logo: what Windows really supplies, and how it is removed

Measured with `companion/tools/dump_sessions.py` (read-only dump of every media session + raw thumbnail) and `analyze_art.py`:

* **Spotify desktop** thumbnail = a **300x300 RGBA PNG composite**: the real cover is a **fully opaque 234x234 block at x 33..266, y 0..233**, then **13 fully
  transparent rows**, then the **Spotify logo + wordmark** (53 rows, sparse glyph shapes, black on transparent). There is no other thumbnail property
  (`GlobalSystemMediaTransportControlsSessionMediaProperties` has only `Thumbnail`; no URL, no second image). The old pipeline composited this on a dark matte, which is why
  the logo showed.
* **Chrome / Edge / Electron**: a plain RGB PNG at <= 150 px in the video's aspect ratio, no branding.

Fix (`companion/desk_media_companion/artwork.py`, `split_composite`) - a structural detector, **no hard-coded crop**:

1. only for RGBA images with real transparent pixels;
2. split into bands of visible pixels separated by transparent rows (gap >= 1.5 % of the height);
3. accept only if there is **exactly one solid block** (>= 90 % of its bounding box fully opaque), every other band is sparse (< 75 % solid, logo-like), the other
   bands are not larger than the cover, the cover is >= 25 % of the image and its aspect is 0.5 - 2.0;
4. crop to the solid block (also drops the semi-transparent anti-aliased rim).

Anything else is left untouched: an opaque cover, a cover with transparent rounded corners (a single band), two solid blocks (ambiguous). Covered by
`companion/tests/test_artwork.py` (synthetic images with Spotify's exact structure, plus the "must not crop" cases). On the live session the log says
`app-branded composite 300x300: kept the 233x233 cover block, dropped 1 logo band(s)`.

If Spotify ever delivers an *opaque* composite (no alpha), it cannot be separated safely and the logo would show again; that case does not occur in the current Spotify build.
The source app stays as a tiny 12 px label at the top of the screen ("Spotify", "Chrome").

## 3. Swipe gestures

Horizontal swipe on the Now Playing screen: **swipe right = next track, swipe left = previous track** (as requested, regardless of convention).

* Rules (`ui.c`, constants `SWIPE_*`): >= 72 px horizontal travel, vertical drift never above 44 px and at most half the horizontal travel, within 1 s. A tap moves < 10 px.
* On success the touch is consumed (`lv_indev_wait_release`), so the finger's start object does not also get a click; a press that travelled more than 24 px is never treated as a tap
  (no accidental full-screen toggle / button press).
* Works from the cover, the text, the background and the buttons. **Does not start on the progress bar** (the slider does not bubble its events): dragging there seeks and never changes the track.
* Feedback: a translucent disc with the next / previous symbol on the cover that drifts in the swipe direction and fades (420 ms).
* If the session reports `can_next` / `can_prev` = false, the swipe does nothing (no command, no feedback).
* Tap on the cover still toggles full-screen; play / prev / next buttons unchanged.

## 4. Per-application volume (Windows Core Audio sessions, never the master volume)

`companion/desk_media_companion/volume.py` uses `ISimpleAudioVolume` of the audio sessions that belong to the displayed app - the same per-app sliders as the Windows Volume Mixer.

* **Mapping**: GSMTC app id (`Spotify.exe`, `chrome.exe`, `msedge.exe`, Store apps `Family_hash!App`) -> every audio session, on every active playback device, whose owning process has that
  executable name (or lives in that package folder). Process lookups are cached per pid.
* **API**: `GET /volume`; `POST /control/volume-up|volume-down[?step=5][&app=<id>]`; `POST /volume?level=0..100`. Answer: `{"ok":true,"app":"Spotify.exe","volume":65,"muted":false,"sessions":1}`.
  The display passes `app=` (the app it is showing); the companion honours it only if that app really has a media session now, otherwise it falls back to the displayed session. Nothing outside
  media apps can be addressed. Pressing "up" on a muted app un-mutes it.
* **Verified** (`companion/tools/check_master_volume.py`): the Windows master volume of all four playback devices was identical before / during / after, and only the displayed app's session changed.
  Real mapping on the owner's PC: `Spotify.exe` -> 1 session (the main process), `chrome.exe` -> 1 session (Chrome's audio process). Call latency ~6 ms (first call ~120 ms).
* **Limitations (honest)**
  * **Chrome / Edge / any Chromium browser**: all tabs share one audio process, hence one session. The volume of "the YouTube tab" cannot be addressed separately; **the whole browser's volume changes**.
  * **Firefox** uses an opaque app id (a hash), so it cannot be matched: the display shows "No volume control".
  * Spotify has its own internal volume slider; the Windows session volume multiplies with it.
  * Apps with no audio session yet (nothing has played) report "No volume control" until they make sound.
  * If two apps share an executable name with the displayed one (several Chrome profiles), they are changed together.

## 5. The two side buttons = volume up / down

Identified from the schematic (page 1 "KEYS" block, vector text extracted from the PDF, and the PCB layout on page 2) and Waveshare's XPowersLib - not guessed:

| Button | Schematic | Reads as | Role now |
|---|---|---|---|
| **PWR** (Key3, upper) | one side to GND, other side to net **PWRON** (10 k pull-up) = the **AXP2101 PWRON pin** | **not connected to any ESP32 pin**; the AXP2101 `IRQ` pin is not routed to the ESP32 either (the net name occurs once in the schematic) -> read by **polling the PMIC over I2C** (0x34, `INTSTS2` 0x49: bit0 PWRON rising edge, bit1 falling edge, bit2 long, bit3 short press; enabled in `INTEN2` 0x41) | **volume up** |
| **BOOT** (Key1, lower) | one side to GND, other side to net **IO0 = GPIO0** (10 k pull-up, 100 nF) | plain GPIO input with debounce | **volume down** |

* "Upper / lower": the PCB layout and Waveshare's board photo (display FPC at the bottom edge) show PWRKEY above BOOT on the same edge, so with the display upright PWR is the upper button.
  `SIDE_KEYS_UPPER_IS_PWR` in `side_keys.h` swaps them in one line if a unit is mounted differently.
* 5 % per press; holding repeats after 0.5 s at ~4 steps/s and **stops after 3 s**. A brief translucent pill ("Volume 65%" + level line) appears for ~1 s and fades; nothing permanent is on screen.
  Other pill texts: "Muted", "No volume control", "PC not reachable".
* **What I checked before repurposing them**
  * BOOT / GPIO0 is a strapping pin: holding it while the chip *resets or powers up* selects the ROM download mode. That is hardware and is unaffected; once the app runs, GPIO0 is a normal input.
    Flashing with esptool (RTS/DTR) is unaffected. Consequence for the roadmap: the old idea "hold BOOT at power-up to wipe the stored Wi-Fi" (Milestone 11) is **not possible** - it would enter
    download mode. The reset path must be a long press *while running* (or on the setup page).
  * PWR / PWRON: the AXP2101 itself turns the board off after a long press (4-10 s depending on its configuration). The driver never changes that configuration, only reads the key flags and
    enables the key-interrupt bits (they were already all enabled: `INTEN2 0xFF`). Repeat is capped at 3 s so normal use never gets near the power-off threshold; **do not hold PWR for more than about 4 s**.
    With the USB cable connected, replugging powers the board up again.
  * `EXIO4` of the TCA9554 drives a BSS138 in the KEYS block; the firmware never touches it.

## 6. Bugs found and fixed while testing

* **The volume keys could hit the wrong app**: the PC's idea of the "current" session can change between the display's last poll and the key press. Fix: the display names the app it shows (`app=`), the companion validates it.
* **A held key flooded the command queue** (20 queued POSTs, ~5 s to drain) and starved the 1 Hz poll, so the screen went stale. Fix: volume commands are dropped while two are already queued, the
  poll is now scheduled by time (a due poll always runs first), repeat slowed to ~4 steps/s and capped at 3 s.
* The swipe feedback / volume pill were invisible in screenshots because LVGL's top layer is not part of the screen tree; they are now top-most children of the screen (and still never clickable).
* `winsound` cannot loop a WAV from memory (test helper); `Image.getdata` is deprecated in Pillow 12 (wrapped).

## 7. Verification

| What | How | Result |
|---|---|---|
| Unit tests | `python -m unittest discover -s companion/tests` (14 tests: composite split incl. negative cases, letterbox trim, size rules, JPEG dimensions) | pass |
| Square art unchanged | real-display screenshot | same layout as M8 |
| Landscape card | real-display screenshots: 150x84 video thumbnail, 1280x720 HD frame, letterboxed 4:3 (bars trimmed), 5:1 banner (clamped 2.4:1), real Chrome / YouTube Music 16:9 | pass |
| Spotify logo | real Spotify session: log "kept the 233x233 cover block, dropped 1 logo band(s)", real-display screenshot shows the cover only | pass |
| Swipes | virtual finger through the real LVGL input path (`tools/screenshot.py --do swipe_*`): right -> `control next ok`, left -> `control previous ok`, from the text area -> next, **vertical and 45-degree swipes -> no command (twice)**, **swipe from the progress bar -> `control seek` only, track unchanged**, tap on the cover -> full-screen toggle, tap on play -> `control playpause` | pass |
| Owner's real finger | the owner swiped and pressed on the real touchscreen during testing; the companion log shows matching alternating `next` / `previous` | observed |
| Volume API | fake session with a silent audio stream: 100 -> 95 -> 90 -> set 100; master volume unchanged on all 4 devices; no unrelated session changed | pass |
| Volume keys | BOOT (lower) physically pressed and held by the owner: companion log shows `volume-down` steps on the displayed app (this also exposed the two bugs in section 6). PWR (upper): see the final report | BOOT observed |
| Hold-repeat | simulated 3 s hold: 11 steps, ~4/s, stops at 3 s, no backlog, poll unaffected | pass |
| Overlays | real-display screenshots of "Volume 100%", "No volume control", "PC not reachable", "Muted" and the swipe disc | pass |
| Soak | scripted swipes / taps / key presses / track changes (square <-> landscape) for 5 minutes with the board's serial log | see section 8 |

## 8. Soak result

Dev firmware + companion `--dev-tools --prefer-app python.exe` (silent fake session cycling through square / 16:9 / letterboxed / banner / composite covers), driver `tools/soak_gestures.py`:
**125 scripted actions in ~5 minutes** (swipes in all directions, swipes from the bar and the text, taps on cover and play, 26 simulated key presses, 11 simulated 3-second key holds, track changes with square <-> landscape switching),
board log captured over the last 95 actions / 300 s (`logs/soak.log`, not committed):

* uptime rose continuously (`up=81s` ... `up=382s`): **no reset**; no panic, abort, assert, stack overflow or `E (` error line;
* internal heap free steady at ~154 KB (transient dip to 147 KB during an artwork decode), minimum ever 96.9 KB and **constant** (no leak); PSRAM free 3.9 MB constant; `lvgl_stack_free_min` 20.1 KB constant (24 KB stack);
* commands the board sent in the captured window (from its own log), each explained by the scripted actions: `next` 30 (13 swipe-right + 12 swipe-from-text = 25, plus 5 "bar" swipes that ran while the soak had
  full-screen mode on - the bar is hidden there, so they were ordinary cover swipes), `previous` 11 (= the 11 swipe-lefts), `seek` 5 (the other 5 bar swipes), `playpause` 2 (only 2 of 8 scripted play taps landed on a visible button
  because the soak's own full-screen toggles hide the buttons; a direct check of 6 play taps from the normal layout produced 6 commands), per-app volume steps 95 (18 single presses + 7 simulated 3-second holds).
  No vertical or diagonal swipe produced a command.

Not covered by this soak (needs fingers): the real GT911 touch path under swipes (the owner's own swipes were seen in the log: alternating `next` / `previous`), and the PWR key.

## Known issues / not yet done

* CJK / Cyrillic / emoji glyphs still show as boxes (Montserrat only has Latin): fallback font still to do (visible in the real Chrome screenshot: a Japanese title).
* Chromium thumbnails are 150 px, so landscape cards are soft (Windows limit, see section 1).
* Chrome / Edge volume is whole-browser (section 4).
* No fade between tracks.

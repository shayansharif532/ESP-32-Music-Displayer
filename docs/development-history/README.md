# Development history

These are the **engineering logs of how the project was built**, one file per milestone, oldest first, newest last. Each one states the
goal, what was built, how it was verified (numbers, test runs, serial-log evidence), what was found along the way (bugs, dead ends,
measurements) and what was left open. They are kept as written, apart from a neutral-wording and redaction pass (no personal identifiers,
addresses or device ids), so some statements are superseded by later milestones; each file says so where it matters. The current design is
in [../architecture.md](../architecture.md), the reasons for the big choices are in [../decisions/](../decisions/README.md).

Wording used in these logs: **hardware-tested** means driven through the board's real LVGL input path with the test tools (virtual finger,
simulated key presses) and judged from the serial log, the companion's log or screenshots of the board's frame buffer; **observed** means a
person used the real touchscreen or buttons while the logs were captured. "The maintainer" is the person who ran the physical checks.

| # | Log | Summary |
|---|---|---|
| plan | [project-plan.md](project-plan.md) | The original brief, the milestone plan and the running status notes (moved out of `CLAUDE.md`) |
| M0 | [M0-factory-backup.md](M0-factory-backup.md) | Hardware investigation; a verified byte-for-byte backup of the factory flash, its partition map and the restore procedure (identifiers redacted) |
| M1 | [M1-lcd-touch.md](M1-lcd-touch.md) | Waveshare's official LVGL demo built and flashed on the real board; display and touch confirmed; the touch-crash findings (7 KB LVGL stack, two draw units) that shaped the later display configuration |
| M2 | [M2-ui.md](M2-ui.md) | The first own 480x480 UI (placeholder art, title, progress, transport buttons, full-screen toggle); 24 KB LVGL task, one draw unit; first touch soak without a reset |
| MVP (M3-M7) | [MVP-media-companion.md](MVP-media-companion.md) | Wi-Fi with automatic reconnect (recovered in about 2 s), the Windows companion, discovery, now-playing, artwork, play / pause / next / previous / seek; real Spotify desktop and Chrome / YouTube working; the architecture change away from the Spotify Web API |
| M8 | [M8-player-redesign.md](M8-player-redesign.md) | Visual redesign: a large frameless cover, a pre-baked dithered background derived from the cover, a clean foreground; fixed-size PSRAM buffers |
| M8b | [M8b-art-layout-gestures-volume.md](M8b-art-layout-gestures-volume.md) | Landscape cards for 16:9 art, structural removal of the Spotify logo, swipe gestures, per-application Core Audio volume, the two side buttons as volume keys (schematic analysis), 5-minute gesture soak |
| M8c | [M8c-session-switching.md](M8c-session-switching.md) | Vertical swipes switch the media source; every command names the displayed session; the investigation of why browser thumbnails are about 150 px and what was done about it |
| M8d | [M8d-auto-follow-spotify-options.md](M8d-auto-follow-spotify-options.md) | The AUTO / MANUAL_TEMP selection state machine (auto-follow restored), Spotify-only shuffle and repeat through Windows' own controls, isolated end-to-end tests |
| M9 | [M9-idle-screen.md](M9-idle-screen.md) | The customisable idle screen: four themes, clock, custom line, photo background; the companion's tray Settings window; NVS plus a dedicated flash partition cache; 30-cycle soak |
| M9b | [M9b-idle-source.md](M9b-idle-source.md) | The idle screen as a selectable item of the vertical source ring; nothing reaches any app while it is shown; 40-cycle ring soak |

After M9b the work continued with the pairing and authenticated-request protocol (see [../protocol.md](../protocol.md)) and the
documentation and licensing clean-up; their reasoning is in [../security.md](../security.md) and [../licensing.md](../licensing.md).

Screenshots used by the logs are in [img/](img/) (all synthetic: generated covers, mock sessions and UI chrome; nothing from a real library
or desktop). The curated set used by the root README is in [../screenshots/](../screenshots/).

# The idle screen: clock, date and layout

What the display shows when nothing is playing (and when you swipe onto the *Idle Screen* source). Configured in the companion's tray menu >
**Idle screen settings...**; stored on the display (NVS plus the `idle_img` flash partition), so a reboot of the display, the companion or Windows
needs no transfer. Background and decisions: [ADR-007](decisions/ADR-007-customisable-idle-screen.md), [ADR-008](decisions/ADR-008-idle-screen-as-source-ring-item.md).

## Settings

| Setting | Values |
|---|---|
| Theme | Minimal (near-black), Dark (default), Neon, Photo (your own picture; Fill / Fit / Blur Fill) |
| Accent colour | presets or a custom colour |
| Clock | shown / hidden; 12 hour (AM / PM) or 24 hour |
| **Date** | shown / hidden, independent of the clock; four formats |
| Custom line | one line of ASCII text |
| Delay | 2 to 60 s of nothing playing before the idle screen appears (default 4 s) |

### Date formats

The date is the PC's local date, shown in addition to the clock (or alone, if the clock is hidden). Month names are fixed English abbreviations
(the UI font is ASCII Montserrat); the day has no leading zero in the two month-name formats and two digits in the numeric ones.

| Setting (`date_format`) | Example for 13 October 2026 | Device index (`date_fmt`) |
|---|---|---|
| `mon_d_y` (default) | `Oct 13, 2026` | 0 |
| `d_mon_y` | `13 Oct 2026` | 1 |
| `mdy` | `10/13/2026` | 2 |
| `dmy` | `13/10/2026` | 3 |

### Where the time comes from (no Wi-Fi, no internet)

Every answer the companion gives carries `clock_ms`: the PC's local wall-clock time as milliseconds since 1970 *including the local UTC offset*
(`idle.local_clock_ms`; daylight saving and odd offsets such as +5:30 or +5:45 are inside that number and need nothing on the display). The
display re-synchronises its clock with every answer (about once a second) and in between counts with its own timer, so:

* the time and date are right with **no Wi-Fi at all** (the USB link is the time source; the LAN mode's SNTP is not used for the idle clock);
* the date rolls over at local midnight by itself, without a reboot or a new setting (the display derives the date from the running clock with the civil-from-days algorithm);
* after a **reboot of the display** the first answer sets the clock again; after a **restart of the companion** nothing changes visibly;
* a change of time zone or a DST switch on the PC shows within a second.

## Layout contract

The firmware (`firmware/components/idle_screen/idle_screen.c`) and the companion's preview (`companion/desk_media_companion/idle_layout.py`,
`idle.render_preview`) draw the same numbers; the table is in `idle_layout.py` (480 x 480 px, everything centred horizontally):

| Clock | Custom line | Date | Clock centre y | Date centre y (px) | Accent line y | Line centre y (px) |
|---|---|---|---|---|---|---|
| yes | yes | no | 168 | - | 262 | 306 (32) |
| yes | no | no | 205 | - | 300 | - |
| no | yes | no | - | - | 268 | 215 (48) |
| yes | no | **yes** | 172 | 252 (26) | 296 | - |
| yes | yes | **yes** | 138 | 214 (26) | 252 | 300 (32) |
| no | no | **yes** | - | 215 (48) | 262 | - |
| no | yes | **yes** | - | 178 (48) | 236 | 290 (32) |

Contrast: the clock, the date and the custom line get an outline so they stay readable on a picture: **Photo** = four black copies at +-2 px with
opacity 110/255 behind the text, over the photo's 30 % dim; **Neon** = four copies in the accent colour (opacity 46/255) as a glow;
**Minimal** and **Dark** have no outline.

## How it is tested

* The pure date code is tested on the companion (`companion/tests/test_idle_date.py`: formats, month ends, leap years, midnight, DST, odd
  offsets, settings persistence, the layout contract, preview boxes) and **on the target**: `firmware/components/idle_screen/idle_date_vectors.inc`
  is generated from the companion's vectors (`tools/gen_date_vectors.py`) and checked in the firmware's self-test (103 vectors).
* On the board (`tools/idle_test.py --only date`, any transport): all four formats, hide / show, the Minimal / Neon themes, a faked clock that
  crosses midnight (Dec 31 to Jan 1) without a reboot, and the date after a display reboot and a companion restart; the screenshot of the display is compared with the companion's
  preview of the same settings.
* The preview and the device use different text renderers (Pillow and LVGL), so glyph anti-aliasing differs by a sub-pixel; positions, sizes and
  strings agree.

"""Where everything sits on the 480 x 480 idle screen. The firmware (`firmware/components/idle_screen`) draws the same numbers and
the settings window's preview (`idle.render_preview`) reads them from here, so the preview and the device cannot drift apart.
Pure data, no imports beyond the standard library.

Coordinates are pixels, origin top-left. `clock_cy` is the vertical centre of the big clock digits, `date_cy` and `text_cy` the
vertical centre of the date's / custom line's label (a label's height is its font's line height, so the glyph baseline is
`label top + line_height - base_line`), `line_y` the top of the 64 x 3 accent line. Everything is centred horizontally.

    clock  text  date | clock_cy  date_cy  date px  line_y  text_cy (font px)
    yes    yes   no   |   168       -         -       262     306 (32)
    yes    no    no   |   205       -         -       300      -
    no     yes   no   |    -        -         -       268     215 (48)
    no     no    no   |    -        -         -       240      -
    yes    no    yes  |   172      252       26       296      -
    yes    yes   yes  |   138      214       26       252     300 (32)
    no     no    yes  |    -       215       48       262      -
    no     yes   yes  |    -       178       48       236     290 (32)

Contrast, like the clock's: Photo = four black copies of the text at (+-2, 0) and (0, +-2) with opacity 110/255 behind it, on top
of the photo's 30 % black dim; Neon = four copies in the accent colour at the same offsets with opacity 46/255; Minimal and Dark
have no outline. The date, the custom line and the status never wrap and are never wider than LABEL_WIDTH.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

SCREEN = 480                                  # the display is 480 x 480
CENTER_X = SCREEN // 2
LABEL_WIDTH = 440                             # widest a text label may be (it is clipped / shortened beyond that)

# LVGL's built-in Montserrat fonts (and the generated clock font) as the firmware has them: pixel size -> (line_height, base_line).
# The values are from lv_font_montserrat_<n>.c and firmware/components/idle_screen/font_clock.c.
FONT_METRICS = {12: (15, 3), 14: (16, 3), 26: (29, 5), 32: (35, 6), 48: (52, 9), 128: (157, 33)}
CLOCK_PX = 128                                # the clock's digits are Montserrat Medium at this size, made tabular
CLOCK_BASELINE_BELOW_CENTRE = 46              # the digits' vertical middle is this far above their baseline
AMPM_PX = 26                                  # the "AM" / "PM" of the 12-hour clock sits right of the clock ...
AMPM_GAP = 6                                  # ... this far from it ...
AMPM_BASELINE_BELOW_CENTRE = 49               # ... with its baseline here (clock_cy + 49)
STATUS_PX, STATUS_TOP = 14, 436               # "Nothing playing" (a label's top edge)
CONNECTED_PX, CONNECTED_POS = 12, (200, 16)   # "Connected" (top-left corner of the label); its dot is 6 x 6 at (188, 20)
CONNECTED_DOT = (188, 20, 6)                  # x, y, diameter
ACCENT_LINE = (64, 3)                         # width, height

# colours (RGB)
CLOCK_COLOUR = (0xFF, 0xFF, 0xFF)
AMPM_COLOUR = (0xDA, 0xDA, 0xE4)
DATE_COLOUR = (0xE6, 0xE6, 0xEE)
TEXT_COLOUR = (0xF2, 0xF2, 0xF7)
STATUS_COLOUR = (0x8C, 0x90, 0xAA)

# contrast
CLOCK_RING = ((-3, 0), (3, 0), (0, -3), (0, 3))     # offsets of the four outline / glow copies of the clock digits
DATE_RING = ((-2, 0), (2, 0), (0, -2), (0, 2))      # ... and of the date
PHOTO_RING_OPACITY = 110                            # black copies, Photo theme
NEON_RING_OPACITY = 46                              # accent-coloured copies, Neon theme
PHOTO_TEXT_SHADOW = (0, 2, 130)                     # the custom line in the Photo theme: one black copy, offset x, y, opacity
PHOTO_DIM = 0.30                                    # how much black is laid over a photo
NEON_LINE_GLOW = (16, 153)                          # the accent line's glow in the Neon theme: width, opacity (60 %)


@dataclass(frozen=True)
class IdleLayout:
    clock_cy: Optional[int]                   # None = no clock
    date_cy: Optional[int]                    # None = no date
    date_px: int
    line_y: int
    text_cy: Optional[int]                    # None = no custom line
    text_px: int

    @property
    def clock_baseline(self) -> Optional[int]:
        return None if self.clock_cy is None else self.clock_cy + CLOCK_BASELINE_BELOW_CENTRE

    @property
    def ampm_baseline(self) -> Optional[int]:
        return None if self.clock_cy is None else self.clock_cy + AMPM_BASELINE_BELOW_CENTRE


# (clock, text, date) -> (clock_cy, date_cy, line_y, text_cy)
_POSITIONS = {
    (True, True, False): (168, None, 262, 306),
    (True, False, False): (205, None, 300, None),
    (False, True, False): (None, None, 268, 215),
    (False, False, False): (None, None, 240, None),
    (True, False, True): (172, 252, 296, None),
    (True, True, True): (138, 214, 252, 300),
    (False, False, True): (None, 215, 262, None),
    (False, True, True): (None, 178, 236, 290),
}


def idle_layout(show_clock: bool, has_text: bool, show_date: bool) -> IdleLayout:
    clock_cy, date_cy, line_y, text_cy = _POSITIONS[(bool(show_clock), bool(has_text), bool(show_date))]
    return IdleLayout(clock_cy=clock_cy, date_cy=date_cy, date_px=26 if show_clock else 48, line_y=line_y, text_cy=text_cy,
                      text_px=32 if (show_clock or show_date) else 48)       # the 48 px date is the big element; the line under it is 32 px


def label_top(centre_y: int, font_px: int) -> int:
    """Top edge of a label whose line box is centred on `centre_y` (integer division, like the firmware's `cy - height / 2`)."""
    return centre_y - FONT_METRICS[font_px][0] // 2


def label_baseline(top: int, font_px: int) -> int:
    """Where the glyph baseline of a label with that top edge lies."""
    line_height, base_line = FONT_METRICS[font_px]
    return top + line_height - base_line

"""Idle screen settings: what the display shows when the PC is connected but nothing is playing.

The companion owns the whole configuration (a settings file and, for the Photo theme, the user's picture) and the display caches
what it needs in its own flash, so the idle screen survives a reboot of either side without the PC resending anything:

* `IdleStore` keeps the settings (JSON, written atomically) and the picture, and a monotonically changing `version`. The display
  sees that version in every /now-playing answer; when it differs from the one it has stored, it fetches `GET /idle` (the
  settings) and, when the picture changed, `GET /idle/image` (a ready-made 480x480 baseline JPEG, <= 200 KB).
* All picture work happens here, never on the ESP32: the chosen JPG / PNG is validated (size, dimensions, format), cropped /
  fitted / blurred to exactly the screen size and re-encoded. A malformed or huge file is rejected with a readable message
  and the previous setup stays active.
* Nothing in this module (or the data it writes) belongs in Git: it lives in the user's profile (%LOCALAPPDATA%), never in the
  repository.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import functools
import io
import json
import logging
import os
import re
import tempfile
import threading
import unicodedata
import zlib
from dataclasses import dataclass
from typing import Any, Optional

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from . import idle_layout as lay
from .idle_date import (DATE_FORMAT_DEFAULT, DATE_FORMAT_LABELS, DATE_FORMATS, civil_from_clock_ms,  # noqa: F401 (re-exported)
                        date_format_choices, date_format_index, format_clock_ms, format_date)
from .idle_layout import SCREEN
from .resources import resource_path

log = logging.getLogger("idle")

THEMES = ("minimal", "dark", "neon", "photo")
BG_MODES = ("fill", "fit", "blur")
THEME_LABELS = {"minimal": "Minimal", "dark": "Dark", "neon": "Neon", "photo": "Photo"}
BG_MODE_LABELS = {"fill": "Fill", "fit": "Fit", "blur": "Blur Fill"}
ACCENT_PRESETS = {                    # name -> #RRGGBB
    "Blue": "#4DA3FF", "Cyan": "#00E5FF", "Green": "#1ED760", "Amber": "#FFB020",
    "Pink": "#FF4FA3", "Violet": "#9B6BFF", "White": "#F2F2F7",
}
DEFAULT_ACCENT = ACCENT_PRESETS["Blue"]

TEXT_MAX = 40                         # characters of the custom line (the display font is ASCII only, see sanitize_text)
DELAY_MIN, DELAY_MAX, DELAY_DEFAULT = 2, 60, 4

SOURCE_MAX_BYTES = 25 * 1024 * 1024   # largest picture file we are willing to open
SOURCE_MAX_PIXELS = 89_000_000        # below Pillow's own decompression-bomb warning
SOURCE_MAX_SIDE = 20_000
SOURCE_MIN_SIDE = 16
JPEG_MAX_BYTES = 200 * 1024           # what is sent to the display (its flash slot is bigger; this keeps the transfer short)
SOURCE_FORMATS = {"JPEG", "MPO", "PNG"}   # MPO = what many phones write for a JPEG

_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


class ImageError(ValueError):
    """The chosen picture cannot be used; the message is meant for the user."""


@dataclass
class IdleSettings:
    theme: str = "dark"
    accent: str = DEFAULT_ACCENT
    show_clock: bool = True
    clock_24h: bool = True
    show_date: bool = True            # independent of show_clock: a date without a clock is a valid setup
    date_format: str = DATE_FORMAT_DEFAULT
    text: str = ""
    bg_mode: str = "fill"
    delay_s: int = DELAY_DEFAULT
    image_name: str = ""              # file name of the chosen picture (display only)

    def validated(self) -> "IdleSettings":
        s = dataclasses.replace(self)
        if s.theme not in THEMES:
            s.theme = "dark"
        if not isinstance(s.accent, str) or not _HEX.match(s.accent):
            s.accent = DEFAULT_ACCENT
        s.accent = s.accent.upper()
        s.show_clock = bool(s.show_clock)
        s.clock_24h = bool(s.clock_24h)
        if not isinstance(s.show_date, bool):                       # anything but a real boolean is a damaged file: show it
            s.show_date = True
        if s.date_format not in DATE_FORMATS:
            s.date_format = DATE_FORMAT_DEFAULT
        s.text = sanitize_text(s.text)
        if s.bg_mode not in BG_MODES:
            s.bg_mode = "fill"
        try:
            s.delay_s = min(DELAY_MAX, max(DELAY_MIN, int(s.delay_s)))
        except (TypeError, ValueError):
            s.delay_s = DELAY_DEFAULT
        s.image_name = os.path.basename(str(s.image_name or ""))[:80]
        return s


def sanitize_text(text: Any) -> str:
    """The display's fonts cover ASCII only (anything else would show as empty boxes): accents are folded to their base
    letters, everything else outside printable ASCII is dropped, whitespace is collapsed, the length is capped."""
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = "".join(c if 32 <= ord(c) < 127 else (" " if c in "\t\r\n" else "") for c in s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:TEXT_MAX].rstrip()


def default_data_dir() -> str:
    base = os.environ.get("DESK_MEDIA_DATA_DIR") or os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "DeskMediaCompanion")
    return os.path.join(base, "idle")


# ---- picture processing ------------------------------------------------------------------------------------------------

def open_picture(raw: bytes) -> Image.Image:
    """Validates and decodes a user's picture (JPG / PNG): returns an RGB image, upright, composited on black when it has
    transparency. Raises ImageError with a readable reason for anything unsafe or unsupported."""
    if len(raw) > SOURCE_MAX_BYTES:
        raise ImageError(f"This file is too large ({len(raw) // (1024 * 1024)} MB, the limit is {SOURCE_MAX_BYTES // (1024 * 1024)} MB).")
    if len(raw) < 32:
        raise ImageError("This file is empty or not a picture.")
    try:
        img = Image.open(io.BytesIO(raw))
        fmt = (img.format or "").upper()
        if fmt not in SOURCE_FORMATS:
            raise ImageError("Only JPG and PNG pictures are supported.")
        w, h = img.size
        if min(w, h) < SOURCE_MIN_SIDE or max(w, h) > SOURCE_MAX_SIDE or w * h > SOURCE_MAX_PIXELS:
            raise ImageError(f"This picture has an unusable size ({w} x {h} pixels).")
        if fmt in ("JPEG", "MPO"):
            img.draft("RGB", (SCREEN * 2, SCREEN * 2))     # decode a big photo at reduced size (much faster, less memory)
        img.load()
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            matte = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
            img = Image.alpha_composite(matte, rgba).convert("RGB")
        else:
            img = img.convert("RGB")
    except ImageError:
        raise
    except (OSError, ValueError, SyntaxError, MemoryError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        log.info("rejected picture: %s", exc)
        raise ImageError("This file could not be read as a JPG or PNG picture.") from exc
    return img


def _rounded_mask(size: tuple[int, int], radius: int, scale: int = 4) -> Image.Image:
    big = Image.new("L", (size[0] * scale, size[1] * scale), 0)
    ImageDraw.Draw(big).rounded_rectangle((0, 0, big.width - 1, big.height - 1), radius * scale, fill=255)
    return big.resize(size, Image.LANCZOS)


def fit_background(img: Image.Image, mode: str, size: int = SCREEN) -> Image.Image:
    """The picture as the screen shows it, size x size:
    fill = cover the whole screen (centre crop); fit = the whole picture, dark bars where it does not reach; blur = a blurred, darkened
    copy fills the screen and the sharp picture sits in the foreground with a soft shadow (the same idea as the player's cover)."""
    if mode == "fit":
        canvas = Image.new("RGB", (size, size), (8, 8, 12))
        fg = ImageOps.contain(img, (size, size), Image.LANCZOS)
        canvas.paste(fg, ((size - fg.width) // 2, (size - fg.height) // 2))
        return canvas
    if mode == "blur":
        bg = ImageOps.fit(img, (size, size), Image.LANCZOS)
        bg = bg.resize((48, 48), Image.BILINEAR).resize((size, size), Image.BICUBIC).filter(ImageFilter.GaussianBlur(12))
        bg = ImageEnhance.Color(ImageEnhance.Brightness(bg).enhance(0.5)).enhance(1.25)
        box = int(size * 0.84)
        fg = ImageOps.contain(img, (box, box), Image.LANCZOS)
        fx, fy = (size - fg.width) // 2, (size - fg.height) // 2
        shadow = Image.new("L", (size, size), 0)
        ImageDraw.Draw(shadow).rounded_rectangle((fx, fy + 8, fx + fg.width, fy + fg.height + 8), 16, fill=150)
        bg = Image.composite(Image.new("RGB", (size, size), (0, 0, 0)), bg, shadow.filter(ImageFilter.GaussianBlur(16)))
        bg.paste(fg, (fx, fy), _rounded_mask(fg.size, 16))
        return bg
    return ImageOps.fit(img, (size, size), Image.LANCZOS)


def encode_jpeg(img: Image.Image) -> bytes:
    """A baseline JPEG (what the ESP32's decoder reads) of at most JPEG_MAX_BYTES."""
    for quality in (88, 82, 76, 70, 64, 58):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality, optimize=False, progressive=False, subsampling=2)
        if buf.tell() <= JPEG_MAX_BYTES:
            return buf.getvalue()
    raise ImageError("This picture cannot be made small enough for the display.")


def render_background(raw: bytes, mode: str) -> bytes:
    """User's file -> the 480x480 JPEG for the display (raises ImageError)."""
    return encode_jpeg(fit_background(open_picture(raw), mode))


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


# ---- preview (what the settings window shows; an approximation of the display's own drawing) -------------------------------

def _rgb(hex_colour: str) -> tuple[int, int, int]:
    return int(hex_colour[1:3], 16), int(hex_colour[3:5], 16), int(hex_colour[5:7], 16)


def _gradient(top: tuple[int, int, int], bottom: tuple[int, int, int], size: int) -> Image.Image:
    col = Image.linear_gradient("L").resize((size, size))            # black (top) -> white (bottom)
    return Image.composite(Image.new("RGB", (size, size), bottom), Image.new("RGB", (size, size), top), col)


def theme_background(s: IdleSettings, size: int = SCREEN) -> Image.Image:
    accent = _rgb(s.accent)
    if s.theme == "minimal":
        return _gradient((6, 6, 9), (0, 0, 0), size)
    if s.theme == "neon":
        img = _gradient((16, 8, 42), (4, 3, 14), size)
        glow = Image.new("L", (size, size), 0)
        ImageDraw.Draw(glow).ellipse((-size // 2, int(size * 0.80), size + size // 2, int(size * 1.5)), fill=120)
        glow = glow.filter(ImageFilter.GaussianBlur(size // 8))
        return Image.composite(Image.new("RGB", (size, size), tuple(c * 3 // 4 for c in accent)), img, glow)
    return _gradient((29, 29, 54), (7, 7, 12), size)                  # dark


@functools.lru_cache(maxsize=32)
def _montserrat(px: float) -> ImageFont.FreeTypeFont:
    """The firmware's typeface (LVGL's built-in fonts are Montserrat Medium) at `px` pixels. Without the font file - an exe built
    without its data folder - the preview still works with Pillow's stand-in, only the letter shapes differ."""
    try:
        return ImageFont.truetype(resource_path("Montserrat-Medium.ttf"), px)
    except OSError:
        log.warning("Montserrat-Medium.ttf not found: the settings preview uses a stand-in font")
        return ImageFont.load_default(size=px)


def _put_text(img: Image.Image, items: list[tuple[tuple[float, float], str]], font: ImageFont.FreeTypeFont, colour: tuple[int, int, int],
              opacity: int = 255, anchor: str = "ls") -> Optional[tuple[int, int, int, int]]:
    """Draws the strings in `items` ((x, y), text) over `img` in `colour`, blended with `opacity` (0-255) the way LVGL blends a label
    with text_opa; returns the box (left, top, right, bottom) of the pixels that got ink, or None when nothing was drawn."""
    mask = Image.new("L", img.size, 0)
    d = ImageDraw.Draw(mask)
    for xy, text in items:
        d.text(xy, text, font=font, fill=opacity, anchor=anchor)
    img.paste(colour, (0, 0), mask)
    return mask.getbbox()


def _shorten(text: str, font: ImageFont.FreeTypeFont, max_width: float) -> str:
    """The text, cut with "..." when it is wider than `max_width` (a label on the display never wraps)."""
    if font.getlength(text) <= max_width:
        return text
    while text and font.getlength(text + "...") > max_width:
        text = text[:-1]
    return text.rstrip() + "..."


def clock_text(now: dt.datetime, clock_24h: bool) -> tuple[str, str]:
    """(digits, "AM" / "PM" or "") exactly as the display writes the clock: `14:05`, or `2:05` + `PM` (no leading zero)."""
    if clock_24h:
        return f"{now.hour:02d}:{now.minute:02d}", ""
    return f"{now.hour % 12 or 12}:{now.minute:02d}", "AM" if now.hour < 12 else "PM"


def render_preview(s: IdleSettings, background_jpeg: Optional[bytes], size: int = 240, now: Optional[dt.datetime] = None,
                   boxes: Optional[dict[str, tuple[int, int, int, int]]] = None) -> Image.Image:
    """A picture of the idle screen for the settings window: the display's own geometry (idle_layout.py), fonts, strings and per-theme
    contrast, scaled by size / 480. `now` is the PC's local time (its own year / month / day / hour / minute are shown, whatever
    its tzinfo). When `boxes` is a dict it is filled with the ink box (left, top, right, bottom, in pixels of the returned image) of
    every element drawn - clock, ampm, date, text, line, status, connected, dot - so a test can render at size 480 and check where
    each one landed."""
    s = s.validated()
    k = size / SCREEN
    bg: Optional[Image.Image] = None
    photo = False
    if s.theme == "photo" and background_jpeg:
        try:
            bg = Image.open(io.BytesIO(background_jpeg)).convert("RGB").resize((size, size), Image.LANCZOS)
            bg = Image.blend(bg, Image.new("RGB", (size, size), (0, 0, 0)), lay.PHOTO_DIM)    # the display dims a photo for legibility
            photo = True
        except (OSError, ValueError):
            bg = None
    theme = "photo" if photo else ("dark" if s.theme == "photo" else s.theme)       # a Photo theme without a usable picture is Dark
    if bg is None:
        bg = theme_background(dataclasses.replace(s, theme=theme), size)
    accent = _rgb(s.accent)
    now = now or dt.datetime.now()
    layout = lay.idle_layout(s.show_clock, bool(s.text), s.show_date)
    ring = ((0, 0, 0), lay.PHOTO_RING_OPACITY) if theme == "photo" else (accent, lay.NEON_RING_OPACITY) if theme == "neon" else None
    centre = lay.CENTER_X * k
    found: dict[str, tuple[int, int, int, int]] = {}

    def font(device_px: int) -> ImageFont.FreeTypeFont:
        return _montserrat(device_px * k)

    def baseline(centre_y: int, device_px: int) -> float:
        return lay.label_baseline(lay.label_top(centre_y, device_px), device_px) * k

    def record(name: str, box: Optional[tuple[int, int, int, int]]) -> None:
        if box is not None:
            found[name] = box

    if layout.clock_cy is not None:
        digits, suffix = clock_text(now, s.clock_24h)
        f = font(lay.CLOCK_PX)
        digit_width = max(f.getlength(c) for c in "0123456789")                    # tabular digits, as in the generated clock font
        advances = [digit_width if c.isdigit() else f.getlength(c) for c in digits]
        left = centre - sum(advances) / 2

        def glyphs(dx: float, dy: float) -> list[tuple[tuple[float, float], str]]:
            items, x = [], left
            for c, advance in zip(digits, advances):
                items.append(((x + (advance - f.getlength(c)) / 2 + dx, layout.clock_baseline * k + dy), c))
                x += advance
            return items

        if ring:
            for dx, dy in lay.CLOCK_RING:
                _put_text(bg, glyphs(dx * k, dy * k), f, ring[0], ring[1])
        record("clock", _put_text(bg, glyphs(0, 0), f, lay.CLOCK_COLOUR))
        if suffix:
            at = ((left + sum(advances) + lay.AMPM_GAP * k, layout.ampm_baseline * k), suffix)
            record("ampm", _put_text(bg, [at], font(lay.AMPM_PX), lay.AMPM_COLOUR))

    if layout.date_cy is not None:
        text = format_date(now.year, now.month, now.day, s.date_format)
        f, y = font(layout.date_px), baseline(layout.date_cy, layout.date_px)
        if ring:
            for dx, dy in lay.DATE_RING:
                _put_text(bg, [((centre + dx * k, y + dy * k), text)], f, ring[0], ring[1], anchor="ms")
        record("date", _put_text(bg, [((centre, y), text)], f, lay.DATE_COLOUR, anchor="ms"))

    if theme != "minimal":
        w, h = max(2, round(lay.ACCENT_LINE[0] * k)), max(1, round(lay.ACCENT_LINE[1] * k))
        x, y = round((lay.CENTER_X - lay.ACCENT_LINE[0] / 2) * k), round(layout.line_y * k)
        shape = _rounded_mask((w, h), h / 2)
        if theme == "neon":                                                           # the line glows (LVGL shadow, 60 %)
            glow_width, glow_opacity = lay.NEON_LINE_GLOW
            pad = int(glow_width * k * 1.5) + 2
            glow = Image.new("L", (w + 2 * pad, h + 2 * pad), 0)
            glow.paste(shape, (pad, pad))
            glow = glow.filter(ImageFilter.GaussianBlur(glow_width * k / 2)).point(lambda v: v * glow_opacity // 255)
            bg.paste(accent, (x - pad, y - pad), glow)
        bg.paste(accent, (x, y), shape)
        found["line"] = (x, y, x + w, y + h)

    if layout.text_cy is not None:
        f, y = font(layout.text_px), baseline(layout.text_cy, layout.text_px)
        shown = _shorten(s.text, f, lay.LABEL_WIDTH * k)
        if theme == "photo":
            dx, dy, opacity = lay.PHOTO_TEXT_SHADOW
            _put_text(bg, [((centre + dx * k, y + dy * k), shown)], f, (0, 0, 0), opacity, anchor="ms")
        record("text", _put_text(bg, [((centre, y), shown)], f, lay.TEXT_COLOUR, anchor="ms"))

    status_y = lay.label_baseline(lay.STATUS_TOP, lay.STATUS_PX) * k
    record("status", _put_text(bg, [((centre, status_y), "Nothing playing")], font(lay.STATUS_PX), lay.STATUS_COLOUR, anchor="ms"))
    dot_x, dot_y, dot_size = round(lay.CONNECTED_DOT[0] * k), round(lay.CONNECTED_DOT[1] * k), max(2, round(lay.CONNECTED_DOT[2] * k))
    bg.paste(accent, (dot_x, dot_y), _rounded_mask((dot_size, dot_size), dot_size / 2))
    found["dot"] = (dot_x, dot_y, dot_x + dot_size, dot_y + dot_size)
    label_x, label_top_y = lay.CONNECTED_POS
    connected_y = lay.label_baseline(label_top_y, lay.CONNECTED_PX) * k
    record("connected", _put_text(bg, [((label_x * k, connected_y), "Connected")], font(lay.CONNECTED_PX), lay.STATUS_COLOUR))
    if boxes is not None:
        boxes.update(found)
    return bg


# ---- the store --------------------------------------------------------------------------------------------------------------

class IdleStore:
    """Thread-safe holder of the idle configuration: settings + the processed picture + a version number the display polls."""

    def __init__(self, data_dir: Optional[str] = None) -> None:
        self.dir = data_dir or default_data_dir()
        self._lock = threading.RLock()
        self._settings = IdleSettings()
        self._version = 1
        self._image: Optional[bytes] = None          # the processed JPEG that goes to the display
        self._image_crc = 0
        self._image_v = 0
        self._load()

    # -- files --
    def _path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def _atomic_write(self, name: str, data: bytes) -> None:
        os.makedirs(self.dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, self._path(name))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _save(self) -> None:
        doc = {"version": self._version, "settings": dataclasses.asdict(self._settings),
               "image": {"v": self._image_v} if self._image else None}
        self._atomic_write("settings.json", json.dumps(doc, indent=2).encode("utf-8"))

    def _load(self) -> None:
        try:
            with open(self._path("settings.json"), "rb") as f:
                doc = json.loads(f.read().decode("utf-8"))
            fields = {f.name for f in dataclasses.fields(IdleSettings)}
            self._settings = IdleSettings(**{k: v for k, v in (doc.get("settings") or {}).items() if k in fields}).validated()
            self._version = max(1, int(doc.get("version") or 1))
            self._image_v = int(((doc.get("image") or {}).get("v")) or 0)
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            log.warning("idle settings unreadable (%s): starting with the defaults", exc)
            self._settings, self._version, self._image_v = IdleSettings(), 1, 0
            return
        try:
            with open(self._path("background.jpg"), "rb") as f:
                data = f.read()
            if 100 < len(data) <= JPEG_MAX_BYTES * 2:
                self._image, self._image_crc = data, crc32(data)
        except OSError:
            pass
        if self._settings.image_name and self._image is None:             # processed picture lost: rebuild it from the original
            try:
                self._rebuild_image()
            except ImageError as exc:
                log.warning("cannot rebuild the idle picture: %s", exc)
                self._settings.image_name = ""

    def _rebuild_image(self) -> None:
        with open(self._path("original.bin"), "rb") as f:
            raw = f.read()
        data = render_background(raw, self._settings.bg_mode)
        self._set_image(data)

    def _set_image(self, data: Optional[bytes]) -> None:
        if data is None:
            self._image, self._image_crc = None, 0
            for name in ("background.jpg", "original.bin"):
                try:
                    os.unlink(self._path(name))
                except OSError:
                    pass
            return
        self._atomic_write("background.jpg", data)
        self._image, self._image_crc = data, crc32(data)
        self._image_v += 1

    # -- queries --
    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def settings(self) -> IdleSettings:
        with self._lock:
            return dataclasses.replace(self._settings)

    def image(self) -> Optional[bytes]:
        with self._lock:
            return self._image

    def has_image(self) -> bool:
        with self._lock:
            return self._image is not None

    def config(self) -> dict[str, Any]:
        """The answer to GET /idle (also what the display stores)."""
        with self._lock:
            s = self._settings
            return {
                "ok": True, "version": self._version, "theme": s.theme, "accent": s.accent, "clock": s.show_clock,
                "clock_24h": s.clock_24h, "date": s.show_date, "date_fmt": date_format_index(s.date_format),
                "text": s.text, "bg_mode": s.bg_mode, "delay_s": s.delay_s,
                "image": ({"v": self._image_v, "size": len(self._image), "crc": self._image_crc, "w": SCREEN, "h": SCREEN}
                          if self._image else None),
            }

    # -- changes (settings window) --
    def update(self, **changes: Any) -> IdleSettings:
        """Applies settings changes (unknown names are an error), re-renders the picture when its fitting mode changed, stores
        everything and bumps the version if anything really changed."""
        with self._lock:
            names = {f.name for f in dataclasses.fields(IdleSettings)} - {"image_name"}
            bad = set(changes) - names
            if bad:
                raise ValueError(f"unknown idle setting(s): {', '.join(sorted(bad))}")
            new = dataclasses.replace(self._settings, **changes).validated()
            if new == self._settings:
                return dataclasses.replace(self._settings)
            if new.bg_mode != self._settings.bg_mode and self._image is not None:
                old = self._settings, self._image, self._image_crc, self._image_v
                self._settings = new
                try:
                    self._rebuild_image()
                except (ImageError, OSError) as exc:
                    self._settings, self._image, self._image_crc, self._image_v = old
                    raise ImageError(str(exc)) from exc
            self._settings = new
            self._version += 1
            self._save()
            log.info("idle settings changed -> version %d (%s)", self._version, new.theme)
            return dataclasses.replace(new)

    def set_picture(self, raw: bytes, name: str = "", bg_mode: Optional[str] = None) -> IdleSettings:
        """Uses `raw` (JPG / PNG bytes) as the background picture. Raises ImageError and changes nothing when it is not usable."""
        with self._lock:
            mode = bg_mode if bg_mode in BG_MODES else self._settings.bg_mode
            data = render_background(raw, mode)                          # may raise: nothing has been touched yet
            self._atomic_write("original.bin", raw)
            self._set_image(data)
            self._settings = dataclasses.replace(self._settings, bg_mode=mode, image_name=os.path.basename(name)[:80] or "picture").validated()
            self._version += 1
            self._save()
            log.info("idle picture set (%d KB JPEG, mode %s) -> version %d", len(data) // 1024, mode, self._version)
            return dataclasses.replace(self._settings)

    def set_picture_file(self, path: str, bg_mode: Optional[str] = None) -> IdleSettings:
        try:
            size = os.path.getsize(path)
            if size > SOURCE_MAX_BYTES:
                raise ImageError(f"This file is too large ({size // (1024 * 1024)} MB, the limit is {SOURCE_MAX_BYTES // (1024 * 1024)} MB).")
            with open(path, "rb") as f:
                raw = f.read()
        except OSError as exc:
            raise ImageError(f"The file could not be opened: {exc.strerror or exc}") from exc
        return self.set_picture(raw, os.path.basename(path), bg_mode)

    def clear_picture(self) -> IdleSettings:
        with self._lock:
            if self._image is None and not self._settings.image_name:
                return dataclasses.replace(self._settings)
            self._set_image(None)
            self._settings = dataclasses.replace(self._settings, image_name="").validated()
            self._version += 1
            self._save()
            return dataclasses.replace(self._settings)

    def reset(self) -> IdleSettings:
        """Back to the built-in defaults (picture removed)."""
        with self._lock:
            self._set_image(None)
            self._settings = IdleSettings()
            self._version += 1
            self._save()
            return dataclasses.replace(self._settings)

    def preview_source(self, mode: str) -> Optional[bytes]:
        """The picture as it would look in `mode` (for the settings window), or None when there is no picture."""
        with self._lock:
            if self._image is None:
                return None
            try:
                with open(self._path("original.bin"), "rb") as f:
                    return render_background(f.read(), mode)
            except (OSError, ImageError):
                return self._image


_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


def local_clock_ms(now: Optional[dt.datetime] = None) -> int:
    """The PC's local wall-clock time in milliseconds since the Unix epoch (UTC + the local offset): the display shows
    (ms / 1000) % 86400 as its time of day and floor(ms / 86400000) days after 1970-01-01 as its date, so daylight-saving changes
    and odd offsets (+5:30, +5:45, -9:30) need nothing on its side - only the offset inside this number matters.

    `now` is for tests: an aware datetime is used with the offset it carries (any tzinfo, no conversion to the PC's zone); a naive
    one is read as the PC's local time; None = the current time. The arithmetic is on integers (no float timestamps), so a
    millisecond at midnight is exact."""
    if now is None:
        now = dt.datetime.now()
    offset = now.utcoffset()
    if offset is None:                                  # naive: the PC's own zone (also resolves a repeated hour like the OS does)
        now = now.astimezone()
        offset = now.utcoffset() or dt.timedelta(0)
    since_epoch = now - _EPOCH                          # aware - aware: the real elapsed time, in UTC
    utc_ms = (since_epoch.days * 86400 + since_epoch.seconds) * 1000 + since_epoch.microseconds // 1000
    return utc_ms + offset // dt.timedelta(milliseconds=1)

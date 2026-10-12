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

log = logging.getLogger("idle")

SCREEN = 480                          # the display is 480 x 480
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


def render_preview(s: IdleSettings, background_jpeg: Optional[bytes], size: int = 240, now: Optional[dt.datetime] = None) -> Image.Image:
    """A small picture of the idle screen for the settings window."""
    s = s.validated()
    k = size / SCREEN
    if s.theme == "photo" and background_jpeg:
        try:
            bg = Image.open(io.BytesIO(background_jpeg)).convert("RGB").resize((size, size), Image.LANCZOS)
            bg = Image.blend(bg, Image.new("RGB", (size, size), (0, 0, 0)), 0.30)      # the display dims a photo a little for legibility
        except (OSError, ValueError):
            bg = theme_background(dataclasses.replace(s, theme="dark"), size)
    else:
        bg = theme_background(s if s.theme != "photo" else dataclasses.replace(s, theme="dark"), size)
    d = ImageDraw.Draw(bg)
    accent = _rgb(s.accent)
    now = now or dt.datetime.now()
    clock = now.strftime("%H:%M") if s.clock_24h else now.strftime("%I:%M").lstrip("0")
    big = ImageFont.load_default(size=max(12, int(112 * k)))
    mid = ImageFont.load_default(size=max(8, int(30 * k)))
    small = ImageFont.load_default(size=max(7, int(14 * k)))
    if s.show_clock:
        y = int((120 if s.text else 150) * k)
        if s.theme == "neon":                                                      # a cheap glow
            for dx, dy in ((-3, 0), (3, 0), (0, -3), (0, 3)):
                d.text((size // 2 + dx * k, y + dy * k), clock, font=big, fill=tuple(c // 3 for c in accent), anchor="mt")
        d.text((size // 2, y), clock, font=big, fill=(255, 255, 255), anchor="mt")
        line_y = int((300 if s.text else 330) * k)
    else:
        line_y = int(250 * k)
    if s.theme != "minimal":
        w = int(64 * k)
        d.rectangle((size // 2 - w // 2, line_y, size // 2 + w // 2, line_y + max(1, int(3 * k))), fill=accent)
    if s.text:
        d.text((size // 2, line_y + int(14 * k)), s.text, font=mid, fill=(235, 235, 240), anchor="mt")
    d.text((size // 2, size - int(26 * k)), "Nothing playing", font=small, fill=(150, 152, 170), anchor="mt")
    d.ellipse((size // 2 - int(3 * k) - int(48 * k), int(16 * k), size // 2 + int(3 * k) - int(48 * k), int(22 * k)), fill=accent)
    d.text((size // 2 - int(38 * k), int(13 * k)), "Connected", font=small, fill=(130, 132, 150), anchor="lt")
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
                "clock_24h": s.clock_24h, "text": s.text, "bg_mode": s.bg_mode, "delay_s": s.delay_s,
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


def local_clock_ms() -> int:
    """The PC's local wall-clock time in milliseconds since the Unix epoch (UTC + the local offset): the display shows
    (ms / 1000) % 86400 as its time of day, so daylight-saving changes need nothing on its side."""
    now = dt.datetime.now().astimezone()
    offset = now.utcoffset() or dt.timedelta(0)
    return int((now.timestamp() + offset.total_seconds()) * 1000)

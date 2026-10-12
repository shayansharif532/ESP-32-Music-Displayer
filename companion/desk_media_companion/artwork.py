"""Artwork clean-up and rendering for the display (Pillow only).

What Windows really hands over (measured with companion/tools/dump_sessions.py):

* Spotify desktop: a 300x300 RGBA PNG that is a *composite*: the real cover as a fully opaque square block, then a fully
  transparent gap, then the Spotify logo + wordmark. Showing it as it is puts the logo on screen.
* Chromium apps (Chrome, Edge, the Claude / Electron apps): a thumbnail of at most ~150 px on its longest side, in the video's
  own aspect ratio (16:9 for YouTube, sometimes a very wide banner or a 4:3 frame with black bars baked in).

clean() therefore
  1. splits an alpha composite when - and only when - its structure is unmistakable: two or more bands of visible pixels
     separated by transparent rows, exactly one of which is a solid (opaque) block and the rest sparse (logo-like). Plain
     covers, covers with transparent rounded corners, and anything opaque are left alone: nothing is ever cropped by fixed
     coordinates;
  2. trims symmetrical pure-black bars (letterboxed thumbnails).
render() then produces the JPEG the ESP32 draws: a square cover stays square, wide art keeps its aspect ratio (never stretched).
"""
from __future__ import annotations

import io
import logging
from typing import Optional

from PIL import Image, ImageFilter, ImageEnhance, ImageOps

log = logging.getLogger("art")

ART_BG = (16, 16, 22)              # matte colour behind transparent pixels

SQUARE_MIN, SQUARE_MAX = 0.88, 1.14   # aspect ratios (w/h) still treated as "a square cover" (centre-cropped to square)
WIDE_MAX = 2.4                        # wider art is centre-cropped to this ratio
TALL_MIN = 0.7                        # taller art is centre-cropped to this ratio

_VISIBLE = [255 if a > 16 else 0 for a in range(256)]     # alpha -> mask of visible pixels
_SOLID = [255 if a >= 250 else 0 for a in range(256)]     # alpha -> mask of (nearly) opaque pixels
_BRIGHT = [255 if v > 28 else 0 for v in range(256)]      # luminance -> mask of non-black pixels


def decode(raw: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(raw))
    img.load()
    img = ImageOps.exif_transpose(img)
    if img.mode in ("P", "PA", "LA", "L", "I;16", "CMYK"):
        img = img.convert("RGBA" if (img.mode in ("P", "PA", "LA") or "transparency" in img.info) else "RGB")
    return img


def _runs(flags) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    start: Optional[int] = None
    for i, v in enumerate(flags):
        if v and start is None:
            start = i
        elif not v and start is not None:
            out.append((start, i - 1))
            start = None
    if start is not None:
        out.append((start, len(flags) - 1))
    return out


def _pixels(img: Image.Image) -> list[int]:
    """Pixel values of a single-band image as a list (getdata() is deprecated in newer Pillow)."""
    flat = getattr(img, "get_flattened_data", None) or img.getdata
    return list(flat())


def _mean(mask: Image.Image) -> float:
    """Fraction (0..1) of set pixels of an 'L' mask."""
    return mask.resize((1, 1), Image.BOX).getpixel((0, 0)) / 255.0


# ---- 1. app-branded alpha composites ---------------------------------------------------------------------------------

def split_composite(img: Image.Image) -> Optional[tuple[Image.Image, str]]:
    """If `img` is an RGBA composite "opaque cover block + transparent gap + sparse logo", return (cover as RGB, note)."""
    if img.mode != "RGBA":
        return None
    w, h = img.size
    if w < 64 or h < 64:
        return None
    alpha = img.getchannel("A")
    if alpha.getextrema()[0] > 16:                      # no transparent pixels at all: not a composite
        return None
    visible = alpha.point(_VISIBLE)
    row_cov = _pixels(visible.resize((1, h), Image.BOX))
    min_gap = max(3, round(h * 0.015))
    bands: list[tuple[int, int]] = []
    for y0, y1 in _runs([c > 0 for c in row_cov]):
        if bands and y0 - bands[-1][1] - 1 < min_gap:    # a hairline gap does not split a band
            bands[-1] = (bands[-1][0], y1)
        else:
            bands.append((y0, y1))
    if len(bands) < 2:
        return None

    solid = alpha.point(_SOLID)
    blocks = []                                          # (box, area, solid fill)
    for y0, y1 in bands:
        col_cov = _pixels(visible.crop((0, y0, w, y1 + 1)).resize((w, 1), Image.BOX))
        xs = [i for i, c in enumerate(col_cov) if c > 0]
        if not xs:
            continue
        box = (xs[0], y0, xs[-1] + 1, y1 + 1)
        blocks.append((box, (box[2] - box[0]) * (box[3] - box[1]), _mean(solid.crop(box))))
    covers = [b for b in blocks if b[2] >= 0.90]
    if len(covers) != 1:
        return None                                      # no solid block, or several: ambiguous, leave it alone
    cover = covers[0]
    others = [b for b in blocks if b is not cover]
    cw, ch = cover[0][2] - cover[0][0], cover[0][3] - cover[0][1]
    if (any(b[2] >= 0.75 for b in others)                # another solid block is artwork, not a logo
            or cover[1] < 0.25 * w * h                    # the cover must be the main thing in the picture
            or sum(b[1] for b in others) > cover[1]       # the logo band is never bigger than the cover
            or not 0.5 <= cw / ch <= 2.0):
        return None
    tight = solid.crop(cover[0]).getbbox() or (0, 0, cw, ch)    # drop the semi-transparent anti-aliased rim
    box = (cover[0][0] + tight[0], cover[0][1] + tight[1], cover[0][0] + tight[2], cover[0][1] + tight[3])
    note = f"app-branded composite {w}x{h}: kept the {box[2] - box[0]}x{box[3] - box[1]} cover block, dropped {len(others)} logo band(s)"
    return img.crop(box).convert("RGB"), note


# ---- 2. letterbox / pillarbox ----------------------------------------------------------------------------------------

def _lead(values: list[int], limit: float) -> int:
    n = 0
    for v in values:
        if v > limit:
            break
        n += 1
    return n


def trim_black_bars(img: Image.Image) -> tuple[Image.Image, str]:
    """Crop symmetrical, pure-black bars (e.g. a 4:3 video thumbnail with baked-in letterboxing). Conservative: both bars must
    exist, be roughly equal and at least 4 % of the side, and the rest must still be a real picture."""
    w, h = img.size
    if w < 64 or h < 64:
        return img, ""
    bright = img.convert("L").point(_BRIGHT)
    noise = 255 * 0.012                                  # up to ~1 % stray bright pixels (compression noise) in a "black" line
    box = [0, 0, w, h]
    notes = []
    for axis, size in ((1, h), (0, w)):
        cov = _pixels(bright.resize((1, h), Image.BOX) if axis == 1 else bright.resize((w, 1), Image.BOX))
        a, b = _lead(cov, noise), _lead(cov[::-1], noise)
        need = max(3, round(size * 0.04))
        if a >= need and b >= need and abs(a - b) <= max(3, 0.3 * max(a, b)) and size - a - b >= 0.35 * size:
            if axis == 1:
                box[1], box[3] = a, size - b
                notes.append(f"{a}+{b}px black bars (top/bottom)")
            else:
                box[0], box[2] = a, size - b
                notes.append(f"{a}+{b}px black bars (left/right)")
    if not notes:
        return img, ""
    return img.crop(tuple(box)), "trimmed " + ", ".join(notes)


def clean(img: Image.Image) -> tuple[Image.Image, list[str]]:
    """Decoded thumbnail -> the artwork itself as an RGB image, plus notes about what was removed (for the log)."""
    notes: list[str] = []
    matted = False
    split = split_composite(img)
    if split is not None:
        img, note = split
        notes.append(note)
    elif img.mode == "RGBA":                             # ordinary art with transparency: lay it on a dark matte
        matte = Image.new("RGBA", img.size, ART_BG + (255,))
        matte.alpha_composite(img)
        img = matte
        matted = True                                    # the dark areas are our own matte, not letterbox bars
    img = img.convert("RGB")
    if not matted:
        img, note = trim_black_bars(img)
        if note:
            notes.append(note)
    return img, notes


# ---- rendering -------------------------------------------------------------------------------------------------------

def target_size(src_w: int, src_h: int, size: int, max_w: Optional[int]) -> tuple[int, int, bool]:
    """(width, height, cropped) the cover will be drawn at. size = square side / maximum height, max_w = maximum width."""
    ratio = src_w / src_h if src_h else 1.0
    if max_w is None or SQUARE_MIN <= ratio <= SQUARE_MAX:
        return size, size, True
    ratio = min(max(ratio, TALL_MIN), WIDE_MAX)
    th = size
    tw = round(th * ratio)
    if tw > max_w:
        tw = max_w
        th = round(tw / ratio)
    return tw & ~1, th & ~1, False


def render_jpeg(img: Image.Image, size: int, max_w: Optional[int] = None) -> bytes:
    """Baseline 4:2:0 JPEG (what the ESP32's decoder handles). Without max_w the result is the legacy square (wide art is
    letterboxed on a blurred copy of itself); with max_w the aspect ratio is kept inside a max_w x size box."""
    w, h = img.size
    tw, th, _crop = target_size(w, h, size, max_w)
    if max_w is None and not (SQUARE_MIN <= w / h <= SQUARE_MAX):
        out_img = _blurred_letterbox(img, size)
    else:
        out_img = ImageOps.fit(img, (tw, th), Image.LANCZOS)
        if tw > w * 1.35:                                # small source (Chromium thumbnails): crisp up-scale, not mush
            out_img = out_img.filter(ImageFilter.UnsharpMask(radius=1.4, percent=55, threshold=2))
    buf = io.BytesIO()
    out_img.save(buf, format="JPEG", quality=88, optimize=False, progressive=False, subsampling=2)
    return buf.getvalue()


def _blurred_letterbox(img: Image.Image, size: int) -> Image.Image:
    fitted = ImageOps.contain(img, (size, size), Image.LANCZOS)
    canvas = ImageOps.fit(img, (size, size), Image.BILINEAR).filter(ImageFilter.GaussianBlur(max(4, size // 14)))
    canvas = ImageEnhance.Brightness(canvas).enhance(0.6)
    canvas.paste(fitted, ((size - fitted.width) // 2, (size - fitted.height) // 2))
    return canvas

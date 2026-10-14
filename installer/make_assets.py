"""Generates the installer's artwork: the application icon and the Setup wizard images (synthetic, drawn with Pillow; no photo, no
third-party artwork, no text, so no font is needed).

    python installer\\make_assets.py            # rewrites installer\\assets\\*  (the results are committed, so nobody needs to run this)

Output (deterministic: same Pillow, same bytes):
    assets/DeskMediaCompanion.ico   16 / 24 / 32 / 48 px as 32-bit DIB entries, 64 / 128 / 256 px as PNG entries
    assets/wizard-large-100.bmp ... the left panel of the Welcome / Finish pages (164x314 at 100 %, also 150 %, 200 %)
    assets/wizard-small-100.bmp ... the top-right logo of the other pages (55x55 at 100 %, also 150 %, 200 %)

The motif matches the tray icon drawn by desk_media_companion/ui.py: a dark rounded square with the accent blue (#4DA3FF), here
with a play symbol and a progress line.
"""
from __future__ import annotations

import io
import os
import random
import struct
import sys

from PIL import Image, ImageChops, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "assets")

ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
ACCENT = (77, 163, 255)
ACCENT_LIGHT = (150, 208, 255)
NAVY_TOP = (44, 52, 96)
NAVY_BOTTOM = (15, 17, 32)


def _lerp(a, b, t: float):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(len(a)))


def _vgradient(size: tuple[int, int], top, bottom) -> Image.Image:
    w, h = size
    col = Image.new("RGB", (1, h))
    px = col.load()
    for y in range(h):
        px[0, y] = _lerp(top, bottom, y / max(1, h - 1))
    return col.resize((w, h), Image.NEAREST)


def _rounded_triangle(d: ImageDraw.ImageDraw, pts, radius: float, fill) -> None:
    """A triangle with rounded corners: the polygon pulled in by `radius`, then stroked with a round pen of that radius."""
    cx = sum(p[0] for p in pts) / 3
    cy = sum(p[1] for p in pts) / 3
    inner = []
    for x, y in pts:
        dx, dy = x - cx, y - cy
        n = (dx * dx + dy * dy) ** 0.5
        k = max(0.0, (n - radius * 1.6) / n)        # pull each corner towards the centre (an equilateral-ish triangle)
        inner.append((cx + dx * k, cy + dy * k))
    d.polygon(inner, fill=fill)
    d.line(inner + [inner[0]], fill=fill, width=int(radius * 2), joint="curve")
    for x, y in inner:
        d.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill)


def draw_icon(size: int, background: bool = True) -> Image.Image:
    """The logo at `size` px as RGBA (transparent outside the rounded square). Small sizes drop the fine detail."""
    ss = 8 if size <= 64 else 4                      # supersampling: draw big, shrink with LANCZOS
    s = size * ss
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    radius = int(s * 0.22)
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, s - 1, s - 1), radius, fill=255)
    if background:
        img.paste(_vgradient((s, s), NAVY_TOP, NAVY_BOTTOM), (0, 0), mask)
        # a soft highlight along the top edge
        hl = Image.new("L", (s, s), 0)
        ImageDraw.Draw(hl).rounded_rectangle((int(s * 0.04), int(s * 0.03), int(s * 0.96), int(s * 0.40)), int(radius * 0.8), fill=12)
        img.paste((255, 255, 255, 255), (0, 0), ImageChops.multiply(hl, mask))

    detail = size >= 32
    tri = Image.new("L", (s, s), 0)
    td = ImageDraw.Draw(tri)
    if detail:
        pts = [(0.37 * s, 0.22 * s), (0.37 * s, 0.66 * s), (0.75 * s, 0.44 * s)]
        rr = 0.035 * s
    else:                                            # 16 / 24 px: a bigger symbol, no progress line
        pts = [(0.31 * s, 0.17 * s), (0.31 * s, 0.83 * s), (0.82 * s, 0.50 * s)]
        rr = 0.06 * s
    _rounded_triangle(td, pts, rr, 255)
    img.paste(_vgradient((s, s), ACCENT_LIGHT, ACCENT).convert("RGBA"), (0, 0), tri)

    if detail:
        d = ImageDraw.Draw(img)
        y0, y1 = int(0.79 * s), int(0.79 * s + max(2, 0.055 * s))
        x0, x1, xp = int(0.22 * s), int(0.78 * s), int(0.54 * s)
        r = (y1 - y0) // 2
        d.rounded_rectangle((x0, y0, x1, y1), r, fill=(255, 255, 255, 70))
        d.rounded_rectangle((x0, y0, xp, y1), r, fill=(255, 255, 255, 235))
    return img.resize((size, size), Image.LANCZOS)


# ---- ICO ----------------------------------------------------------------------------------------------------------------------

def _dib_entry(img: Image.Image) -> bytes:
    """A classic 32-bit icon image: BITMAPINFOHEADER + bottom-up BGRA pixels + an all-zero 1-bit AND mask."""
    w, h = img.size
    bgra = bytearray()
    px = img.load()
    for y in range(h - 1, -1, -1):
        for x in range(w):
            r, g, b, a = px[x, y]
            bgra += bytes((b, g, r, a))
    row_bytes = ((w + 31) // 32) * 4
    mask = bytes(row_bytes * h)
    header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, len(bgra) + len(mask), 0, 0, 0, 0)
    return header + bytes(bgra) + mask


def make_ico(images: dict[int, Image.Image]) -> bytes:
    entries = []
    for size in sorted(images):
        img = images[size]
        if size >= 64:
            buf = io.BytesIO()
            img.save(buf, "PNG", optimize=True)
            entries.append((size, buf.getvalue()))
        else:
            entries.append((size, _dib_entry(img)))
    out = bytearray(struct.pack("<HHH", 0, 1, len(entries)))
    offset = 6 + 16 * len(entries)
    for size, data in entries:
        out += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)     # 256 is stored as 0
        offset += len(data)
    for _, data in entries:
        out += data
    return bytes(out)


# ---- wizard images ------------------------------------------------------------------------------------------------------------

def _dither(img: Image.Image, seed: int) -> Image.Image:
    """+-1 level of fixed-seed noise so a smooth gradient does not band in 24-bit BMP (deterministic)."""
    rnd = random.Random(seed)
    w, h = img.size
    noise = Image.new("L", (w, h))
    noise.putdata([rnd.choice((126, 128, 128, 130)) for _ in range(w * h)])
    n = Image.merge("RGB", (noise, noise, noise))
    # img + (noise - 128), clamped
    return ImageChops.add(img, n, 1, -128)


def make_wizard_large(w: int, h: int) -> Image.Image:
    """Left panel of the Welcome / Finish pages: dark gradient, a big logo, faint equaliser bars rising from the bottom."""
    ss = 3
    W, H = w * ss, h * ss
    img = _vgradient((W, H), (38, 46, 92), (12, 14, 28))          # RGB, so the translucent bars below really blend
    d = ImageDraw.Draw(img, "RGBA")
    heights = (0.20, 0.34, 0.26, 0.42, 0.30, 0.38, 0.24)
    n = len(heights)
    gap = W * 0.045
    bw = (W - gap * (n + 1)) / n
    for i, k in enumerate(heights):
        x = gap + i * (bw + gap)
        top = H * (1.0 - k)
        d.rounded_rectangle((x, top, x + bw, H * 1.02), bw * 0.28, fill=(77, 163, 255, 40))
    logo = draw_icon(int(W * 0.62))
    img = img.convert("RGBA")
    img.alpha_composite(logo, (int((W - logo.width) / 2), int(H * 0.13)))
    small = img.convert("RGB").resize((w, h), Image.LANCZOS)
    return _dither(small, 11)


def make_wizard_small(size: int) -> Image.Image:
    """Top-right logo of the inner pages (the page header is white)."""
    ss = 4
    s = size * ss
    img = Image.new("RGBA", (s, s), (255, 255, 255, 255))
    logo = draw_icon(int(s * 0.9))
    img.alpha_composite(logo, ((s - logo.width) // 2, (s - logo.height) // 2))
    return img.convert("RGB").resize((size, size), Image.LANCZOS)


def save_bmp(img: Image.Image, name: str) -> None:
    img.save(os.path.join(OUT, name), "BMP")


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    icons = {s: draw_icon(s) for s in ICON_SIZES}
    with open(os.path.join(OUT, "DeskMediaCompanion.ico"), "wb") as f:
        f.write(make_ico(icons))
    for pct in (100, 150, 200):
        k = pct / 100
        save_bmp(make_wizard_large(round(164 * k), round(314 * k)), f"wizard-large-{pct}.bmp")
        save_bmp(make_wizard_small(round(55 * k)), f"wizard-small-{pct}.bmp")
    for name in sorted(os.listdir(OUT)):
        print(f"{name:28s} {os.path.getsize(os.path.join(OUT, name)):>9,d} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())

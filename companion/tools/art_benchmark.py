"""Developer tool: compares up-scaling pipelines for the ~150 px thumbnails Chromium hands Windows (YouTube in Chrome / Edge).

Ground truth = local high-resolution pictures (Windows wallpapers + synthetic text thumbnails). Each is shrunk to 150 px the way
Chromium delivers it, then every candidate pipeline scales it back to the card size the display draws (432 x 242) and is scored
against the original picture reduced to that size:

    psnr      higher = closer to the original
    edges     gradient energy relative to the original (1.0 = as crisp as the original; < 0.7 soft; > 1.2 over-sharpened)
    ring      mean over/undershoot beyond the original's local min/max (halos); lower is better

    companion\\.venv\\Scripts\\python.exe companion\\tools\\art_benchmark.py logs\\art-bench
"""
import io
import math
import os
import sys

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageStat

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from desk_media_companion import artwork  # noqa: E402

SRC_W = 150
OUT = (432, 242)
PHOTOS = [r"C:\Windows\Web\Screen\img100.jpg", r"C:\Windows\Web\Wallpaper\Theme1\img1.jpg", r"C:\Windows\Web\Wallpaper\Theme1\img3.jpg",
          r"C:\Windows\Web\Wallpaper\Theme1\img4.jpg", r"C:\Windows\Web\Wallpaper\Windows\img0.jpg", r"C:\Windows\Web\Wallpaper\Theme2\img7.jpg"]


def load_ground_truth() -> list[tuple[str, Image.Image]]:
    out = []
    for p in PHOTOS:
        if os.path.exists(p):
            im = Image.open(p).convert("RGB")
            out.append((os.path.basename(os.path.dirname(p)) + "_" + os.path.basename(p), ImageOps.fit(im, (1280, 720), Image.LANCZOS)))
    out.append(("text_thumb", make_text_thumbnail()))
    return out


def make_text_thumbnail() -> Image.Image:
    base = None
    for p in PHOTOS:
        if os.path.exists(p):
            base = ImageOps.fit(Image.open(p).convert("RGB"), (1280, 720), Image.LANCZOS)
            break
    base = base or Image.new("RGB", (1280, 720), (40, 60, 120))
    d = ImageDraw.Draw(base)
    try:
        big = ImageFont.truetype(r"C:\Windows\Fonts\impact.ttf", 150)
        small = ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", 56)
    except OSError:
        big = small = ImageFont.load_default()
    d.rectangle([60, 70, 1220, 330], fill=(0, 0, 0))
    d.text((90, 90), "TOP 10 TRACKS", font=big, fill=(255, 220, 0))
    d.text((90, 420), "Official Music Video - 4K", font=small, fill=(255, 255, 255), stroke_width=4, stroke_fill=(0, 0, 0))
    d.ellipse([930, 400, 1190, 660], outline=(255, 40, 40), width=14)
    return base


def chromium_like(gt: Image.Image) -> Image.Image:
    """What Windows delivers for a Chrome tab: the picture shrunk so the longest side is ~150 px (then PNG, lossless)."""
    h = round(SRC_W * gt.height / gt.width)
    small = gt.resize((SRC_W, h), Image.LANCZOS)
    buf = io.BytesIO()
    small.save(buf, "PNG")
    return Image.open(io.BytesIO(buf.getvalue())).convert("RGB")


# ---- candidate pipelines -------------------------------------------------------------------------------------------------

def fit(img: Image.Image, resample=Image.LANCZOS) -> Image.Image:
    return ImageOps.fit(img, OUT, resample)


def p_lanczos(img): return fit(img)
def p_bicubic(img): return fit(img, Image.BICUBIC)
def p_current(img): return fit(img).filter(ImageFilter.UnsharpMask(radius=1.4, percent=55, threshold=2))


def p_two_step(img):
    up = ImageOps.fit(img, (img.width * 2, round(img.height * 2)), Image.LANCZOS).filter(ImageFilter.UnsharpMask(0.8, 40, 1))
    return ImageOps.fit(up, OUT, Image.LANCZOS).filter(ImageFilter.UnsharpMask(1.0, 35, 2))


def _limit(sharp: Image.Image, base: Image.Image, slack: int = 6) -> Image.Image:
    """Clip a sharpened picture to the local min/max of the unsharpened one (+ slack): no halos / ringing."""
    hi = base.filter(ImageFilter.MaxFilter(5)).point(lambda v: min(255, v + slack))
    lo = base.filter(ImageFilter.MinFilter(5)).point(lambda v: max(0, v - slack))
    return ImageChops.lighter(ImageChops.darker(sharp, hi), lo)


def p_halo_limited(img):
    base = fit(img)
    return _limit(base.filter(ImageFilter.UnsharpMask(radius=1.6, percent=120, threshold=1)), base)


def _edge_mask(base: Image.Image, lo: int = 10, hi: int = 60) -> Image.Image:
    g = base.convert("L")
    sx = g.filter(ImageFilter.Kernel((3, 3), (-1, 0, 1, -2, 0, 2, -1, 0, 1), scale=4, offset=128))
    sy = g.filter(ImageFilter.Kernel((3, 3), (-1, -2, -1, 0, 0, 0, 1, 2, 1), scale=4, offset=128))
    mag = ImageChops.add(sx.point(lambda v: abs(v - 128)), sy.point(lambda v: abs(v - 128)))
    mask = mag.point(lambda v: 0 if v <= lo else (255 if v >= hi else (v - lo) * 255 // (hi - lo)))
    return mask.filter(ImageFilter.GaussianBlur(1.2))


def p_edge_adaptive(img):
    base = fit(img)
    sharp = _limit(base.filter(ImageFilter.UnsharpMask(radius=1.8, percent=170, threshold=0)), base, 10)
    return Image.composite(sharp, base, _edge_mask(base))


def p_denoise_edge(img):
    smooth = Image.blend(img, img.filter(ImageFilter.MedianFilter(3)), 0.35)
    base = fit(smooth)
    sharp = _limit(base.filter(ImageFilter.UnsharpMask(radius=1.8, percent=170, threshold=0)), base, 10)
    return Image.composite(sharp, base, _edge_mask(base))


def p_two_step_edge(img):
    up = ImageOps.fit(img, (img.width * 2, img.height * 2), Image.LANCZOS)
    up = Image.composite(_limit(up.filter(ImageFilter.UnsharpMask(1.0, 130, 0)), up, 8), up, _edge_mask(up, 10, 60))
    base = ImageOps.fit(up, OUT, Image.LANCZOS)
    sharp = _limit(base.filter(ImageFilter.UnsharpMask(radius=1.4, percent=110, threshold=0)), base, 8)
    return Image.composite(sharp, base, _edge_mask(base))


def edge_variant(radius, percent, slack=10, lo=10, hi=60):
    def fn(img):
        base = fit(img)
        sharp = _limit(base.filter(ImageFilter.UnsharpMask(radius=radius, percent=percent, threshold=0)), base, slack)
        return Image.composite(sharp, base, _edge_mask(base, lo, hi))
    return fn


PIPELINES = {"lanczos": p_lanczos, "current": p_current, "halo_limited": p_halo_limited, "edge_adaptive": p_edge_adaptive,
             "edge_r2_250": edge_variant(2.0, 250), "edge_r2_350": edge_variant(2.0, 350, 14), "edge_r1.4_300": edge_variant(1.4, 300, 12),
             "edge_r2.4_300": edge_variant(2.4, 300, 14, 8, 50)}


# ---- metrics -------------------------------------------------------------------------------------------------------------

def psnr(a: Image.Image, b: Image.Image) -> float:
    rms = ImageStat.Stat(ImageChops.difference(a, b)).rms
    mse = sum(v * v for v in rms) / len(rms)
    return 99.0 if mse == 0 else 10 * math.log10(255 * 255 / mse)


def grad_energy(img: Image.Image) -> float:
    g = img.convert("L")
    gx = ImageChops.difference(g, ImageChops.offset(g, 1, 0))
    gy = ImageChops.difference(g, ImageChops.offset(g, 0, 1))
    return ImageStat.Stat(gx).mean[0] + ImageStat.Stat(gy).mean[0]


def ring(cand: Image.Image, gt: Image.Image) -> float:
    c, g = cand.convert("L"), gt.convert("L")
    hi = g.filter(ImageFilter.MaxFilter(5)).point(lambda v: min(255, v + 6))
    lo = g.filter(ImageFilter.MinFilter(5)).point(lambda v: max(0, v - 6))
    over = ImageChops.subtract(c, hi)
    under = ImageChops.subtract(lo, c)
    return ImageStat.Stat(over).mean[0] + ImageStat.Stat(under).mean[0]


def main(out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    totals = {k: [0.0, 0.0, 0.0] for k in PIPELINES}
    gts = load_ground_truth()
    sheet_rows = []
    for name, gt in gts:
        ref = ImageOps.fit(gt, OUT, Image.LANCZOS)
        ref_edges = grad_energy(ref)
        small = chromium_like(gt)
        cands = {}
        for pname, fn in PIPELINES.items():
            out = fn(small)
            cands[pname] = out
            totals[pname][0] += psnr(out, ref)
            totals[pname][1] += grad_energy(out) / ref_edges
            totals[pname][2] += ring(out, ref)
        # a zoomed crop of the centre of each candidate: 2x nearest-neighbour of a 144x80 window
        crop = (144, 81, 288, 161)
        tiles = [("original", ref)] + list(cands.items())
        row = Image.new("RGB", (len(tiles) * 292, 168), (20, 20, 20))
        for i, (tn, im) in enumerate(tiles):
            z = im.crop(crop).resize((288, 160), Image.NEAREST)
            row.paste(z, (i * 292, 4))
        sheet_rows.append((name, row))
        print(f"done {name}")
    sheet = Image.new("RGB", (sheet_rows[0][1].width, sum(r.height for _, r in sheet_rows)), (0, 0, 0))
    y = 0
    for _, r in sheet_rows:
        sheet.paste(r, (0, y))
        y += r.height
    sheet.save(os.path.join(out_dir, "zoom_sheet.png"))
    n = len(gts)
    print("\ncolumns of the sheet: original | " + " | ".join(PIPELINES))
    print(f"{'pipeline':16s} {'psnr':>7s} {'edges':>7s} {'ring':>7s}")
    for pname, (p, e, r) in totals.items():
        print(f"{pname:16s} {p / n:7.2f} {e / n:7.2f} {r / n:7.2f}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "art-bench")

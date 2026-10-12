"""Writes a handful of synthetic album covers (bright, dark, photo-like, flat, grayscale) for visual tests of the display.

    companion\\.venv\\Scripts\\python.exe companion\\tools\\make_test_covers.py logs\\test-art
    companion\\.venv\\Scripts\\python.exe companion\\tools\\fake_media_session.py --art-dir logs\\test-art
"""
import os
import random
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

SIZE = 1000


def noise(img: Image.Image, amount: int = 10) -> Image.Image:
    n = Image.effect_noise(img.size, amount).convert("RGB")
    return Image.blend(img, n, 0.06)


def blobs(seed: int, palette: list[tuple[int, int, int]], base: tuple[int, int, int]) -> Image.Image:
    rnd = random.Random(seed)
    img = Image.new("RGB", (SIZE, SIZE), base)
    d = ImageDraw.Draw(img)
    for _ in range(9):
        r = rnd.randint(150, 380)
        x, y = rnd.randint(0, SIZE), rnd.randint(0, SIZE)
        d.ellipse([x - r, y - r, x + r, y + r], fill=rnd.choice(palette))
    return noise(img.filter(ImageFilter.GaussianBlur(110)))


def text_cover(bg, fg, title: str) -> Image.Image:
    img = Image.new("RGB", (SIZE, SIZE), bg)
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arialbd.ttf", 150)
    except OSError:
        font = ImageFont.load_default()
    d.text((70, 90), title, fill=fg, font=font)
    d.rectangle([70, 700, 930, 730], fill=fg)
    d.ellipse([700, 760, 930, 990], outline=fg, width=14)
    return img


def widescreen(w: int, h: int, seed: int, title: str) -> Image.Image:
    """A video-thumbnail-like frame: colour blobs, a bold caption and a 'play' marker."""
    rnd = random.Random(seed)
    img = Image.new("RGB", (w, h), (24, 60, 110))
    d = ImageDraw.Draw(img)
    for _ in range(8):
        r = rnd.randint(h // 5, h // 2)
        x, y = rnd.randint(0, w), rnd.randint(0, h)
        d.ellipse([x - r, y - r, x + r, y + r], fill=rnd.choice([(250, 190, 40), (230, 70, 90), (40, 190, 200), (120, 90, 220)]))
    img = img.filter(ImageFilter.GaussianBlur(h // 14))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arialbd.ttf", h // 5)
    except OSError:
        font = ImageFont.load_default()
    d.text((w // 14, h // 10), title, fill=(255, 255, 255), font=font, stroke_width=max(2, h // 90), stroke_fill=(0, 0, 0))
    d.rounded_rectangle([w // 14, h - h // 4, w // 14 + w // 3, h - h // 8], radius=h // 24, fill=(255, 255, 255))
    return img


def spotify_like_composite() -> Image.Image:
    """Same structure as the real Spotify thumbnail: opaque cover block, transparent gap, sparse logo band (all synthetic)."""
    canvas = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
    cover = blobs(21, [(250, 120, 60), (60, 40, 120), (240, 220, 90)], (30, 20, 60)).resize((234, 234)).convert("RGBA")
    canvas.paste(cover, (33, 0))
    d = ImageDraw.Draw(canvas)
    d.ellipse([50, 250, 100, 298], fill=(0, 0, 0, 255))
    for x in range(115, 260, 24):
        d.rectangle([x, 262, x + 12, 292], fill=(0, 0, 0, 255))
    return canvas


def main() -> None:
    out = sys.argv[1] if len(sys.argv) > 1 else "test-art"
    os.makedirs(out, exist_ok=True)
    letterboxed = Image.new("RGB", (640, 480), (0, 0, 0))                     # 4:3 frame with black bars baked in (old YouTube style)
    letterboxed.paste(widescreen(640, 360, 7, "FOUR\nTHREE"), (0, 60))
    wide_dir = {
        "7 wide 16x9 video": widescreen(150, 84, 3, "VIDEO").resize((150, 84)),
        "8 wide 16x9 hd": widescreen(1280, 720, 5, "HD TRAILER"),
        "9 letterboxed 4x3": letterboxed,
        "9b banner 5x1": widescreen(1000, 200, 9, "BANNER"),
    }
    for name, img in wide_dir.items():
        img.save(os.path.join(out, name + ".png"))
    spotify_like_composite().save(os.path.join(out, "9c spotify-like composite.png"))
    covers = {
        "1 white minimal": text_cover((246, 246, 244), (20, 20, 20), "LIGHT\nHOURS"),
        "2 near black": text_cover((10, 10, 12), (230, 60, 60), "NIGHT\nDRIVE"),
        "3 teal pink blobs": blobs(3, [(30, 200, 190), (240, 90, 160), (250, 200, 120), (60, 80, 200)], (20, 40, 80)),
        "4 flat yellow": text_cover((250, 214, 30), (30, 30, 30), "SUN\nSTATE"),
        "5 warm photo-like": blobs(11, [(230, 120, 60), (250, 190, 90), (150, 50, 40), (60, 30, 50)], (90, 40, 30)),
        "6 grayscale": blobs(5, [(240, 240, 240), (120, 120, 120), (20, 20, 20)], (60, 60, 60)).convert("L").convert("RGB"),
    }
    for name, img in covers.items():
        img.save(os.path.join(out, name + ".jpg"), quality=92)
    print("wrote", len(covers), "covers to", out)


if __name__ == "__main__":
    main()

"""Developer tool: before / after of the artwork pipeline for raw thumbnails (the .bin files written by dump_sessions.py).

    companion\\.venv\\Scripts\\python.exe companion\\tools\\art_compare.py logs\\art-dump2\\2_chrome_exe.bin logs\\art-compare.png

"before" = the M8b pipeline (Lanczos + a global unsharp mask), "after" = the current one (edge-only, halo-limited sharpening).
Both are shown at 1:1 (the size the display draws) and as a 2x close-up of the centre.
"""
import io
import os
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageOps

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from desk_media_companion import artwork  # noqa: E402


def before_jpeg(img: Image.Image, size: int, max_w: int) -> Image.Image:
    tw, th, _ = artwork.target_size(img.width, img.height, size, max_w)
    out = ImageOps.fit(img, (tw, th), Image.LANCZOS)
    if tw > img.width * 1.35:
        out = out.filter(ImageFilter.UnsharpMask(radius=1.4, percent=55, threshold=2))
    buf = io.BytesIO()
    out.save(buf, format="JPEG", quality=88, subsampling=2)
    return Image.open(io.BytesIO(buf.getvalue())).convert("RGB")


def main(src: str, out_path: str, size: int = 312, max_w: int = 432) -> None:
    img, notes = artwork.clean(artwork.decode(open(src, "rb").read()))
    a = before_jpeg(img, size, max_w)
    b = Image.open(io.BytesIO(artwork.render_jpeg(img, size, max_w))).convert("RGB")
    w, h = a.size
    pad = 10
    sheet = Image.new("RGB", (w * 2 + pad * 3, h + pad * 2 + h * 2 + pad), (24, 24, 28))
    sheet.paste(a, (pad, pad))
    sheet.paste(b, (w + pad * 2, pad))
    box = (w // 4, h // 4, w // 4 + w // 2, h // 4 + h // 2)
    for i, im in enumerate((a, b)):
        sheet.paste(im.crop(box).resize((w, h), Image.NEAREST), (pad + i * (w + pad), h + pad * 2))
    d = ImageDraw.Draw(sheet)
    d.text((pad + 4, pad + 4), "before (M8b)", fill=(255, 255, 0))
    d.text((w + pad * 2 + 4, pad + 4), "after", fill=(255, 255, 0))
    sheet.save(out_path)
    print(f"source {img.size}, drawn at {a.size}; notes {notes}; saved {out_path}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])

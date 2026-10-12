"""Developer tool: puts two display screenshots next to each other (full card + a 2x close-up of the same region) for before / after
comparisons.

    companion\\.venv\\Scripts\\python.exe companion\\tools\\side_by_side.py before.png after.png out.png [x0 y0 x1 y1]

The optional box is the region (in display pixels) that is shown as the close-up; default: the middle of the landscape card.
"""
import sys

from PIL import Image, ImageDraw


def main() -> None:
    a, b, out = Image.open(sys.argv[1]).convert("RGB"), Image.open(sys.argv[2]).convert("RGB"), sys.argv[3]
    box = tuple(int(v) for v in sys.argv[4:8]) if len(sys.argv) >= 8 else (130, 90, 346, 211)
    card = (20, 44, 460, 300)
    pad = 12
    cw, ch = card[2] - card[0], card[3] - card[1]
    zw, zh = (box[2] - box[0]) * 2, (box[3] - box[1]) * 2
    width = max(cw, zw) * 2 + pad * 3
    sheet = Image.new("RGB", (width, ch + zh + pad * 3 + 16), (20, 20, 24))
    d = ImageDraw.Draw(sheet)
    for i, (img, label) in enumerate(((a, "before"), (b, "after"))):
        x = pad + i * (max(cw, zw) + pad)
        d.text((x, 4), label, fill=(255, 255, 0))
        sheet.paste(img.crop(card), (x, 16))
        sheet.paste(img.crop(box).resize((zw, zh), Image.NEAREST), (x, 16 + ch + pad))
    sheet.save(out)
    print("saved", out, sheet.size)


if __name__ == "__main__":
    main()

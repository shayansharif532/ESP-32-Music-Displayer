#!/usr/bin/env python3
"""Tile several screenshots into one picture (for docs / quick review).

    tools\\venv\\Scripts\\python.exe tools\\contact_sheet.py OUT.jpg IMG1.png IMG2.png ... [--cols 3] [--scale 0.5] [--gap 8]
"""
import argparse

from PIL import Image


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("images", nargs="+")
    ap.add_argument("--cols", type=int, default=3)
    ap.add_argument("--scale", type=float, default=0.5)
    ap.add_argument("--gap", type=int, default=8)
    args = ap.parse_args()
    imgs = [Image.open(p).convert("RGB") for p in args.images]
    w, h = int(imgs[0].width * args.scale), int(imgs[0].height * args.scale)
    cols = min(args.cols, len(imgs))
    rows = (len(imgs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * w + (cols + 1) * args.gap, rows * h + (rows + 1) * args.gap), (18, 18, 22))
    for i, img in enumerate(imgs):
        x = args.gap + (i % cols) * (w + args.gap)
        y = args.gap + (i // cols) * (h + args.gap)
        sheet.paste(img.resize((w, h), Image.LANCZOS), (x, y))
    sheet.save(args.out, quality=90)
    print("wrote", args.out, sheet.size)


if __name__ == "__main__":
    main()

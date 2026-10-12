"""Developer tool: prints the structure of a raw thumbnail (alpha usage, visible bands, flat margins) so the composite-artwork
detector in media.py can be designed from facts rather than guesses. Pillow only (the companion has no numpy).

    companion\\.venv\\Scripts\\python.exe companion\\tools\\analyze_art.py logs\\art-dump\\3_Spotify_exe.bin
"""
import sys

from PIL import Image


def runs(flags) -> list[tuple[int, int]]:
    out, start = [], None
    for i, v in enumerate(flags):
        if v and start is None:
            start = i
        elif not v and start is not None:
            out.append((start, i - 1))
            start = None
    if start is not None:
        out.append((start, len(flags) - 1))
    return out


def main(path: str) -> None:
    img = Image.open(path)
    print(path, img.format, img.size, img.mode)
    rgba = img.convert("RGBA")
    w, h = rgba.size
    px = rgba.load()
    alpha_hist = {}
    for y in range(h):
        for x in range(w):
            a = px[x, y][3]
            alpha_hist[a] = alpha_hist.get(a, 0) + 1
    print("alpha values:", len(alpha_hist), "  fully opaque:", alpha_hist.get(255, 0), "  fully transparent:", alpha_hist.get(0, 0), "  of", w * h)
    row_vis = [sum(1 for x in range(w) if px[x, y][3] > 8) for y in range(h)]
    col_vis = [sum(1 for y in range(h) if px[x, y][3] > 8) for x in range(w)]
    print("rows with visible px:", runs([v > 0 for v in row_vis]))
    print("cols with visible px:", runs([v > 0 for v in col_vis]))
    print("rows where >50% of the width is fully opaque:", runs([sum(1 for x in range(w) if px[x, y][3] == 255) > w * 0.5 for y in range(h)]))
    print("per-row visible px (every 8th row):", [(y, row_vis[y]) for y in range(0, h, 8)])
    # colour of the margin pixels (what is hidden behind alpha 0)
    corner = px[0, 0]
    print("pixel (0,0):", corner, " pixel (w-1,h-1):", px[w - 1, h - 1])


if __name__ == "__main__":
    main(sys.argv[1])

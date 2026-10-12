"""Developer tool: runs raw thumbnails through the same clean-up + rendering the companion uses and writes the JPEGs the display
would receive, so the result can be looked at.

    companion\\.venv\\Scripts\\python.exe companion\\tools\\render_art_preview.py logs\\art-dump logs\\art-preview
"""
import io
import os
import sys

from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from desk_media_companion import artwork  # noqa: E402


def main(src_dir: str, out_dir: str, size: int = 312, max_w: int = 432) -> None:
    os.makedirs(out_dir, exist_ok=True)
    for name in sorted(os.listdir(src_dir)):
        if not name.endswith(".bin"):
            continue
        raw = open(os.path.join(src_dir, name), "rb").read()
        img = artwork.decode(raw)
        clean, notes = artwork.clean(img)
        jpeg = artwork.render_jpeg(clean, size, max_w)
        out = Image.open(io.BytesIO(jpeg))
        path = os.path.join(out_dir, name.replace(".bin", ".jpg"))
        with open(path, "wb") as f:
            f.write(jpeg)
        print(f"{name}: source {img.size} {img.mode} -> clean {clean.size} -> sent {out.size}, {len(jpeg)} bytes; {notes or 'no changes'}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])

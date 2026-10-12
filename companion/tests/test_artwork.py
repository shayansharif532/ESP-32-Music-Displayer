"""Unit tests for the artwork clean-up (synthetic images only - no third-party artwork is committed).

    companion\\.venv\\Scripts\\python.exe -m unittest discover -s companion\\tests -v
"""
import io
import os
import sys
import unittest

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from desk_media_companion import artwork  # noqa: E402


def gradient_cover(size: int) -> Image.Image:
    img = Image.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            px[x, y] = (40 + x * 200 // size, 60 + y * 150 // size, 120 + (x + y) * 100 // (2 * size))
    return img


def spotify_like_composite() -> Image.Image:
    """The structure measured from the real Spotify thumbnail: opaque cover block, transparent gap, sparse logo band."""
    canvas = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
    canvas.paste(gradient_cover(234).convert("RGBA"), (33, 0))
    d = ImageDraw.Draw(canvas)
    d.ellipse([50, 250, 100, 298], fill=(0, 0, 0, 255))                      # logo disc
    for x in range(115, 260, 24):                                             # wordmark-ish glyphs
        d.rectangle([x, 262, x + 12, 292], fill=(0, 0, 0, 255))
    return canvas


class Composite(unittest.TestCase):
    def test_spotify_like_composite_keeps_only_the_cover(self):
        out, notes = artwork.clean(spotify_like_composite())
        self.assertEqual(out.size, (234, 234), notes)
        self.assertTrue(any("composite" in n for n in notes))

    def test_opaque_square_cover_untouched(self):
        out, notes = artwork.clean(gradient_cover(300))
        self.assertEqual(out.size, (300, 300))
        self.assertEqual(notes, [])

    def test_transparent_rounded_corners_are_not_a_composite(self):
        img = gradient_cover(300).convert("RGBA")
        mask = Image.new("L", (300, 300), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, 299, 299], 40, fill=255)
        img.putalpha(mask)
        out, notes = artwork.clean(img)
        self.assertEqual(out.size, (300, 300), notes)

    def test_two_solid_blocks_are_left_alone(self):
        canvas = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
        canvas.paste(gradient_cover(120).convert("RGBA"), (90, 10))
        canvas.paste(gradient_cover(120).convert("RGBA"), (90, 170))
        out, notes = artwork.clean(canvas)
        self.assertEqual(out.size, (300, 300), "ambiguous art must not be cropped")

    def test_sparse_band_above_cover_is_also_split(self):
        canvas = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
        d = ImageDraw.Draw(canvas)
        for x in range(100, 200, 12):                                                      # sparse, text-like glyphs above the cover
            d.rectangle([x, 8, x + 4, 28], fill=(0, 0, 0, 255))
        canvas.paste(gradient_cover(240).convert("RGBA"), (30, 50))
        out, notes = artwork.clean(canvas)
        self.assertEqual(out.size, (240, 240), notes)


class Bars(unittest.TestCase):
    def test_letterboxed_4_3_thumbnail_is_trimmed_to_16_9(self):
        img = Image.new("RGB", (640, 480), (0, 0, 0))
        img.paste(gradient_cover(640).crop((0, 0, 640, 360)), (0, 60))
        out, notes = artwork.clean(img)
        self.assertEqual(out.size, (640, 360), notes)

    def test_dark_cover_without_bars_untouched(self):
        img = Image.new("RGB", (300, 300), (8, 8, 10))
        out, notes = artwork.clean(img)
        self.assertEqual(out.size, (300, 300))

    def test_single_bar_is_content_not_a_letterbox(self):
        img = gradient_cover(300)
        ImageDraw.Draw(img).rectangle([0, 0, 299, 40], fill=(0, 0, 0))
        out, _ = artwork.clean(img)
        self.assertEqual(out.size, (300, 300))


class Sizes(unittest.TestCase):
    def test_square_stays_square(self):
        self.assertEqual(artwork.target_size(300, 300, 312, 432)[:2], (312, 312))

    def test_16_9_becomes_a_landscape_card(self):
        w, h, _ = artwork.target_size(150, 83, 312, 432)
        self.assertEqual(w, 432)
        self.assertAlmostEqual(w / h, 150 / 83, delta=0.03)

    def test_banner_is_clamped(self):
        w, h, _ = artwork.target_size(150, 31, 312, 432)
        self.assertEqual((w, h), (432, 180))

    def test_tall_art_is_narrower_than_the_box(self):
        w, h, _ = artwork.target_size(200, 300, 312, 432)
        self.assertEqual(h, 312)
        self.assertLess(w, 312)

    def test_legacy_square_mode(self):
        self.assertEqual(artwork.target_size(160, 90, 300, None)[:2], (300, 300))

    def test_render_jpeg_has_the_requested_dimensions(self):
        for src, want in (((300, 300), (312, 312)), ((160, 90), (432, 242)), ((150, 31), (432, 180))):
            jpeg = artwork.render_jpeg(Image.new("RGB", src, (120, 80, 200)), 312, 432)
            self.assertEqual(Image.open(io.BytesIO(jpeg)).size, want)


if __name__ == "__main__":
    unittest.main()

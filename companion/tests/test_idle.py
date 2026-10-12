"""Idle screen settings, picture processing and storage (no WinRT, no network).

    companion\\.venv\\Scripts\\python.exe -m unittest discover -s companion/tests
"""
import datetime as dt
import io
import json
import os
import random
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PIL import Image  # noqa: E402

from desk_media_companion import idle  # noqa: E402
from desk_media_companion.idle import IdleSettings, IdleStore, ImageError  # noqa: E402


def png_bytes(size=(64, 48), colour=(200, 30, 30), mode="RGB") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, colour).save(buf, "PNG")
    return buf.getvalue()


def jpeg_bytes(size=(640, 480), colour=(30, 90, 200), quality=90) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def split_png(size=(400, 200)) -> bytes:
    """Left half red, right half blue (so cropping / fitting can be told apart)."""
    img = Image.new("RGB", size, (0, 0, 255))
    img.paste((255, 0, 0), (0, 0, size[0] // 2, size[1]))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class Text(unittest.TestCase):
    def test_accents_fold_and_junk_is_dropped(self):
        self.assertEqual(idle.sanitize_text("Caf\u00e9 \u00dcber"), "Cafe Uber")
        self.assertEqual(idle.sanitize_text("  hello \t\n world  "), "hello world")
        self.assertEqual(idle.sanitize_text("Hi \U0001F600 there"), "Hi there")        # emoji: the display font has no glyph
        self.assertEqual(idle.sanitize_text("\u65e5\u672c\u8a9e"), "")                 # no ASCII fallback for CJK: dropped
        self.assertEqual(idle.sanitize_text(None), "")

    def test_length_is_capped(self):
        self.assertEqual(len(idle.sanitize_text("x" * 200)), idle.TEXT_MAX)


class Settings(unittest.TestCase):
    def test_defaults_are_generic(self):
        s = IdleSettings().validated()
        self.assertEqual((s.theme, s.text, s.image_name), ("dark", "", ""))           # nothing personal built in
        self.assertTrue(s.show_clock)

    def test_bad_values_are_replaced(self):
        s = IdleSettings(theme="cyberpunk", accent="red", delay_s=9999, bg_mode="stretch", text="a" * 100).validated()
        self.assertEqual(s.theme, "dark")
        self.assertEqual(s.accent, idle.DEFAULT_ACCENT)
        self.assertEqual(s.delay_s, idle.DELAY_MAX)
        self.assertEqual(s.bg_mode, "fill")
        self.assertEqual(len(s.text), idle.TEXT_MAX)
        self.assertEqual(IdleSettings(delay_s=-5).validated().delay_s, idle.DELAY_MIN)
        self.assertEqual(IdleSettings(delay_s="abc").validated().delay_s, idle.DELAY_DEFAULT)
        self.assertEqual(IdleSettings(accent="#4da3ff").validated().accent, "#4DA3FF")

    def test_image_name_is_just_a_file_name(self):
        self.assertEqual(IdleSettings(image_name=r"C:\Users\x\Pictures\me.png").validated().image_name, "me.png")


class Pictures(unittest.TestCase):
    def test_rejects_garbage_and_wrong_formats(self):
        for raw in (b"", b"not a picture at all, just some text bytes....", os.urandom(4000)):
            with self.assertRaises(ImageError):
                idle.open_picture(raw)
        gif = io.BytesIO()
        Image.new("RGB", (40, 40), (1, 2, 3)).save(gif, "GIF")
        with self.assertRaises(ImageError) as cm:
            idle.open_picture(gif.getvalue())
        self.assertIn("JPG and PNG", str(cm.exception))

    def test_rejects_truncated_jpeg(self):
        data = jpeg_bytes((800, 600))
        with self.assertRaises(ImageError):
            idle.open_picture(data[: len(data) // 3])

    def test_rejects_oversized_file_and_extreme_dimensions(self):
        with self.assertRaises(ImageError) as cm:
            idle.open_picture(b"\x89PNG" + b"0" * (idle.SOURCE_MAX_BYTES + 1))
        self.assertIn("too large", str(cm.exception))
        for size in ((idle.SOURCE_MAX_SIDE + 1, 20), (8, 400)):
            with self.assertRaises(ImageError):
                idle.open_picture(png_bytes(size, mode="L", colour=0))

    def test_transparency_goes_onto_black(self):
        img = idle.open_picture(png_bytes((40, 40), (255, 255, 255, 0), mode="RGBA"))
        self.assertEqual(img.mode, "RGB")
        self.assertEqual(img.getpixel((20, 20)), (0, 0, 0))

    def test_fill_crops_to_the_centre(self):
        out = idle.fit_background(idle.open_picture(split_png()), "fill")
        self.assertEqual(out.size, (480, 480))
        self.assertGreater(out.getpixel((10, 240))[0], 200)       # left edge: red
        self.assertGreater(out.getpixel((470, 240))[2], 200)      # right edge: blue

    def test_fit_keeps_the_whole_picture_with_bars(self):
        out = idle.fit_background(idle.open_picture(split_png((400, 200))), "fit")
        self.assertEqual(out.size, (480, 480))
        self.assertLess(sum(out.getpixel((240, 5))), 60)          # top bar is dark
        self.assertGreater(out.getpixel((5, 240))[0], 200)        # picture reaches the left edge
        self.assertGreater(out.getpixel((475, 240))[2], 200)

    def test_blur_fill_has_blurred_edges_and_a_sharp_foreground(self):
        out = idle.fit_background(idle.open_picture(split_png((400, 200))), "blur")
        self.assertEqual(out.size, (480, 480))
        top = out.getpixel((30, 8))                                # background: tinted by the picture, not black bars
        self.assertGreater(top[0] + top[2], 40)
        self.assertGreater(out.getpixel((120, 240))[0], 200)       # foreground: bright red, sharp

    def test_output_is_a_small_baseline_jpeg(self):
        rnd = random.Random(1)
        noisy = Image.frombytes("RGB", (900, 900), bytes(rnd.getrandbits(8) for _ in range(900 * 900 * 3)))
        buf = io.BytesIO()
        noisy.save(buf, "PNG")
        data = idle.render_background(buf.getvalue(), "fill")
        self.assertLessEqual(len(data), idle.JPEG_MAX_BYTES)
        out = Image.open(io.BytesIO(data))
        self.assertEqual((out.format, out.size), ("JPEG", (480, 480)))
        self.assertFalse(out.info.get("progressive") or out.info.get("progression"))        # the display's decoder is baseline only

    def test_phone_style_jpeg_orientation_is_respected(self):
        img = Image.new("RGB", (200, 100), (255, 0, 0))
        exif = Image.Exif()
        exif[0x0112] = 6                                          # rotated: stored sideways
        buf = io.BytesIO()
        img.save(buf, "JPEG", exif=exif)
        self.assertEqual(idle.open_picture(buf.getvalue()).size, (100, 200))


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "idle")

    def tearDown(self):
        self.tmp.cleanup()

    def test_fresh_store_is_generic(self):
        s = IdleStore(self.dir)
        cfg = s.config()
        self.assertEqual((cfg["theme"], cfg["text"], cfg["image"], cfg["version"]), ("dark", "", None, 1))

    def test_update_bumps_the_version_only_on_a_real_change(self):
        s = IdleStore(self.dir)
        v = s.version
        s.update(theme="dark")                                    # same value
        self.assertEqual(s.version, v)
        s.update(theme="neon", text="Welcome back")
        self.assertEqual(s.version, v + 1)
        self.assertEqual((s.settings().theme, s.settings().text), ("neon", "Welcome back"))
        with self.assertRaises(ValueError):
            s.update(banana=1)

    def test_settings_survive_a_restart(self):
        a = IdleStore(self.dir)
        a.update(theme="minimal", accent="#FF4FA3", clock_24h=False, text="Hello", delay_s=7)
        a.set_picture(jpeg_bytes(), "holiday.jpg", "blur")
        b = IdleStore(self.dir)                                    # "companion restart"
        self.assertEqual(b.settings(), a.settings())
        self.assertEqual(b.version, a.version)
        self.assertEqual(b.image(), a.image())
        self.assertEqual(b.config()["image"]["crc"], a.config()["image"]["crc"])
        self.assertEqual(b.settings().image_name, "holiday.jpg")

    def test_picture_flow(self):
        s = IdleStore(self.dir)
        v = s.version
        s.set_picture(split_png(), "wide.png", "fill")
        info = s.config()["image"]
        self.assertEqual((info["w"], info["h"]), (480, 480))
        self.assertEqual(info["size"], len(s.image()))
        self.assertEqual(info["crc"], idle.crc32(s.image()))
        self.assertGreater(s.version, v)
        crc_fill = info["crc"]
        s.update(bg_mode="blur")                                   # a different fitting re-renders the picture
        self.assertNotEqual(s.config()["image"]["crc"], crc_fill)
        self.assertEqual(s.settings().bg_mode, "blur")
        s.clear_picture()
        self.assertIsNone(s.image())
        self.assertIsNone(s.config()["image"])
        self.assertEqual(s.settings().image_name, "")

    def test_a_bad_picture_changes_nothing(self):
        s = IdleStore(self.dir)
        s.set_picture(png_bytes((300, 300)), "ok.png")
        before = (s.version, s.image(), s.settings())
        for raw in (b"garbage" * 50, jpeg_bytes()[:500]):
            with self.assertRaises(ImageError):
                s.set_picture(raw, "bad.png")
        self.assertEqual((s.version, s.image(), s.settings()), before)
        self.assertEqual(IdleStore(self.dir).image(), before[1])  # nor on disk

    def test_missing_processed_picture_is_rebuilt_from_the_original(self):
        s = IdleStore(self.dir)
        s.set_picture(split_png(), "wide.png", "fit")
        os.unlink(os.path.join(self.dir, "background.jpg"))
        self.assertIsNotNone(IdleStore(self.dir).image())

    def test_corrupt_settings_fall_back_to_defaults(self):
        os.makedirs(self.dir)
        with open(os.path.join(self.dir, "settings.json"), "wb") as f:
            f.write(b"{ this is not json")
        s = IdleStore(self.dir)
        self.assertEqual(s.settings(), IdleSettings())
        s.update(theme="neon")                                     # and it keeps working
        self.assertEqual(IdleStore(self.dir).settings().theme, "neon")

    def test_unknown_or_hostile_values_in_the_file_are_cleaned(self):
        os.makedirs(self.dir)
        doc = {"version": 4, "settings": {"theme": "evil", "accent": "<script>", "text": "x" * 500, "delay_s": 10 ** 9, "future_field": 1}}
        with open(os.path.join(self.dir, "settings.json"), "w") as f:
            json.dump(doc, f)
        s = IdleStore(self.dir).settings()
        self.assertEqual((s.theme, s.accent, len(s.text), s.delay_s), ("dark", idle.DEFAULT_ACCENT, idle.TEXT_MAX, idle.DELAY_MAX))

    def test_reset_removes_everything_personal(self):
        s = IdleStore(self.dir)
        s.update(text="Someone")
        s.set_picture(png_bytes(), "me.png")
        s.reset()
        self.assertEqual(s.settings(), IdleSettings())
        self.assertIsNone(s.image())
        self.assertFalse(os.path.exists(os.path.join(self.dir, "original.bin")))


class Misc(unittest.TestCase):
    def test_every_theme_previews(self):
        jpeg = idle.render_background(png_bytes((300, 200)), "blur")
        for theme in idle.THEMES:
            for clock in (True, False):
                img = idle.render_preview(IdleSettings(theme=theme, show_clock=clock, text="Hi"), jpeg, 240)
                self.assertEqual(img.size, (240, 240))
        self.assertEqual(idle.render_preview(IdleSettings(theme="photo"), None, 120).size, (120, 120))      # no picture: falls back

    def test_clock_is_the_local_wall_clock(self):
        ms = idle.local_clock_ms()
        expected = (dt.datetime.now().astimezone().replace(tzinfo=None) - dt.datetime(1970, 1, 1)).total_seconds() * 1000
        self.assertLess(abs(ms - expected), 2000)
        self.assertLess(abs(ms / 1000 % 86400 - (time.localtime().tm_hour * 3600 + time.localtime().tm_min * 60 + time.localtime().tm_sec)), 5)


if __name__ == "__main__":
    unittest.main()

"""The idle screen's date: calendar arithmetic, the four formats, local-time handling, persistence, the preview and the settings window.

    companion\\.venv\\Scripts\\python.exe -m unittest discover -s companion/tests

The expected values come from `datetime` / `calendar` and from tests/data/idle_date_vectors.json (made by
tools/make_date_vectors.py, which does not use the code under test); the firmware's self-tests load the same file.
"""
import datetime as dt
import gc
import importlib.util
import json
import os
import random
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from PIL import Image, ImageFont  # noqa: E402

from desk_media_companion import idle, idle_date, idle_layout, resources  # noqa: E402
from desk_media_companion.idle import IdleSettings, IdleStore  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
TOOLS = os.path.join(HERE, "..", "tools")
EPOCH = dt.datetime(1970, 1, 1)
MS_DAY = 86_400_000
NOW = dt.datetime(2026, 10, 13, 14, 5)                                # what the box tests pretend the PC's clock shows


def load_vectors() -> list[dict]:
    with open(os.path.join(DATA, "idle_date_vectors.json"), encoding="utf-8") as f:
        return json.load(f)


def local_ms(y, mo, d, h=0, mi=0, s=0, ms=0) -> int:
    """clock_ms of a local wall-clock moment, from datetime (not from the code under test)."""
    delta = dt.datetime(y, mo, d, h, mi, s, ms * 1000) - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def load_tool(name: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(TOOLS, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Zone(dt.tzinfo):
    """A time zone given by its UTC offsets over time, with PEP 495 `fold` handling: `periods` = [(first UTC instant as a naive
    datetime, offset in minutes), ...] in order. Enough to build daylight-saving zones without a time-zone database (Windows has none)."""

    def __init__(self, periods):
        self.periods = [(start, dt.timedelta(minutes=minutes)) for start, minutes in periods]

    def _fits(self, wall):
        """The periods during which the wall-clock time `wall` happens: one, two in a repeated hour, none in a skipped one."""
        hits = []
        for i, (start, off) in enumerate(self.periods):
            if wall < start + off or (i + 1 < len(self.periods) and wall >= self.periods[i + 1][0] + off):
                continue
            hits.append(i)
        return hits

    def utcoffset(self, d):
        wall = d.replace(tzinfo=None)
        hits = self._fits(wall)
        if hits:
            return self.periods[hits[min(d.fold, len(hits) - 1)]][1]
        before = max(i for i, (start, off) in enumerate(self.periods) if start + off <= wall)       # an hour that never happened
        return self.periods[min(before + d.fold, len(self.periods) - 1)][1]

    def fromutc(self, d):
        utc = d.replace(tzinfo=None)
        i = max(j for j, (start, _off) in enumerate(self.periods) if start <= utc)
        wall = utc + self.periods[i][1]
        hits = self._fits(wall)
        return wall.replace(tzinfo=self, fold=1 if len(hits) == 2 and hits[1] == i else 0)

    def dst(self, d):
        return None

    def tzname(self, d):
        return "test"


LONG_AGO = dt.datetime(1900, 1, 1)
US_EASTERN = Zone([(LONG_AGO, -300), (dt.datetime(2026, 3, 8, 7, 0), -240), (dt.datetime(2026, 11, 1, 6, 0), -300)])
SYDNEY = Zone([(LONG_AGO, 660), (dt.datetime(2026, 4, 4, 16, 0), 600), (dt.datetime(2026, 10, 3, 16, 0), 660)])
LORD_HOWE = Zone([(LONG_AGO, 660), (dt.datetime(2026, 4, 4, 15, 0), 630), (dt.datetime(2026, 10, 3, 15, 30), 660)])    # 30-minute DST
SAO_PAULO = Zone([(LONG_AGO, -120), (dt.datetime(2018, 2, 18, 2, 0), -180)])         # DST ended at midnight: 00:00 became 23:00 the day before
MIDNIGHT_FORWARD = Zone([(LONG_AGO, 120), (dt.datetime(2026, 3, 28, 22, 0), 180)])   # DST starts at local midnight: 00:00 -> 01:00


def utc(y, mo, d, h=0, mi=0, s=0, ms=0) -> dt.datetime:
    return dt.datetime(y, mo, d, h, mi, s, ms * 1000, tzinfo=dt.timezone.utc)


class Device:
    """What the firmware does with clock_ms: the date comes from (base + its own monotonic clock); a sync only replaces the base."""

    def __init__(self) -> None:
        self.base = None

    def sync(self, clock_ms: int, mono_ms: int) -> None:
        self.base = clock_ms - mono_ms

    def ymd(self, mono_ms: int):
        return idle.civil_from_clock_ms(self.base + mono_ms)

    def text(self, mono_ms: int, fmt: str = "mon_d_y") -> str:
        return idle.format_clock_ms(self.base + mono_ms, fmt)


# ---- calendar arithmetic -----------------------------------------------------------------------------------------------------

class CivilFromDays(unittest.TestCase):
    def test_every_day_of_four_centuries_matches_datetime(self):
        first, last = dt.date(1600, 1, 1).toordinal(), dt.date(2500, 12, 31).toordinal()
        epoch = dt.date(1970, 1, 1).toordinal()
        for ordinal in range(first, last + 1):
            d = dt.date.fromordinal(ordinal)
            self.assertEqual(idle_date.civil_from_days(ordinal - epoch), (d.year, d.month, d.day), d)

    def test_the_whole_range_the_formats_accept_matches_datetime(self):
        rnd = random.Random(20261013)
        epoch = dt.date(1970, 1, 1).toordinal()
        for _ in range(20000):
            ordinal = rnd.randint(1, dt.date(9999, 12, 31).toordinal())
            d = dt.date.fromordinal(ordinal)
            self.assertEqual(idle.civil_from_clock_ms((ordinal - epoch) * MS_DAY + rnd.randrange(MS_DAY)), (d.year, d.month, d.day))

    def test_negative_clock_values_floor_to_the_previous_day(self):
        self.assertEqual(idle.civil_from_clock_ms(0), (1970, 1, 1))
        self.assertEqual(idle.civil_from_clock_ms(-1), (1969, 12, 31))
        self.assertEqual(idle.civil_from_clock_ms(-1000), (1969, 12, 31))
        self.assertEqual(idle.civil_from_clock_ms(-MS_DAY), (1969, 12, 31))
        self.assertEqual(idle.civil_from_clock_ms(-MS_DAY - 1), (1969, 12, 30))
        self.assertEqual(idle.civil_from_clock_ms(local_ms(1900, 3, 1) - 1), (1900, 2, 28))

    def test_day_boundaries_to_the_millisecond(self):
        rnd = random.Random(7)
        for _ in range(500):
            day = rnd.randint(-40000, 90000)                       # about 1860 .. 2216
            before, after = dt.date(1970, 1, 1) + dt.timedelta(days=day - 1), dt.date(1970, 1, 1) + dt.timedelta(days=day)
            self.assertEqual(idle.civil_from_clock_ms(day * MS_DAY - 1), (before.year, before.month, before.day))
            self.assertEqual(idle.civil_from_clock_ms(day * MS_DAY), (after.year, after.month, after.day))
            self.assertEqual(idle.civil_from_clock_ms(day * MS_DAY + MS_DAY - 1), (after.year, after.month, after.day))

    def test_the_32_bit_limits_are_just_days(self):
        self.assertEqual(idle.civil_from_clock_ms(2**31 * 1000), (2038, 1, 19))               # 2038-01-19 03:14:08 UTC
        self.assertEqual(idle.civil_from_clock_ms((2**31 - 1) * 1000), (2038, 1, 19))
        self.assertEqual(idle.civil_from_clock_ms(2**32 * 1000), (2106, 2, 7))                # 2106-02-07 06:28:16 UTC
        self.assertEqual(idle.civil_from_clock_ms(-(2**31) * 1000), (1901, 12, 13))

    def test_leap_years(self):
        for year, leap in ((1900, False), (2000, True), (2024, True), (2026, False), (2100, False), (2400, True)):
            self.assertEqual(idle.civil_from_clock_ms(local_ms(year, 2, 28) + MS_DAY), (year, 2, 29) if leap else (year, 3, 1), year)


class IdleDateModule(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(idle.DATE_FORMATS, ("mon_d_y", "d_mon_y", "mdy", "dmy"))
        self.assertEqual(idle.DATE_FORMAT_DEFAULT, "mon_d_y")
        self.assertEqual(set(idle.DATE_FORMAT_LABELS), set(idle.DATE_FORMATS))
        self.assertEqual(idle_date.MONTHS, ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"))

    def test_the_wire_numbers(self):
        self.assertEqual([idle.date_format_index(f) for f in idle.DATE_FORMATS], [0, 1, 2, 3])
        self.assertEqual(idle.date_format_index("bogus"), 0)
        self.assertEqual(idle.date_format_index(None), 0)


# ---- the four formats ----------------------------------------------------------------------------------------------------------

class Formats(unittest.TestCase):
    def test_the_examples_of_the_specification(self):
        self.assertEqual([idle.format_date(2026, 10, 13, f) for f in idle.DATE_FORMATS],
                         ["Oct 13, 2026", "13 Oct 2026", "10/13/2026", "13/10/2026"])
        self.assertEqual([idle.format_date(2027, 1, 5, f) for f in idle.DATE_FORMATS],
                         ["Jan 5, 2027", "5 Jan 2027", "01/05/2027", "05/01/2027"])

    def test_default_format(self):
        self.assertEqual(idle.format_date(2026, 10, 13), "Oct 13, 2026")

    def test_every_vector_in_every_format(self):
        vectors = load_vectors()
        self.assertGreaterEqual(len(vectors), 90)
        for v in vectors:
            self.assertEqual(idle.civil_from_clock_ms(v["clock_ms"]), (v["year"], v["month"], v["day"]), v)
            self.assertEqual(set(v["formats"]), set(idle.DATE_FORMATS))
            for fmt, expected in v["formats"].items():
                self.assertEqual(idle.format_clock_ms(v["clock_ms"], fmt), expected, (v["clock_ms"], fmt))
                self.assertEqual(idle.format_date(v["year"], v["month"], v["day"], fmt), expected)

    def test_month_names_are_fixed_english_and_ascii(self):
        names = [idle.format_date(2026, m, 1, "d_mon_y").split()[1] for m in range(1, 13)]
        self.assertEqual(names, list(idle_date.MONTHS))
        for name in names:
            self.assertTrue(name.isascii() and name.isalpha() and len(name) == 3, name)

    def test_the_system_language_does_not_matter(self):
        import locale
        before = locale.setlocale(locale.LC_TIME)
        tried = 0
        try:
            for name in ("de_DE.UTF-8", "German_Germany.1252", "fr_FR.UTF-8", "French_France.1252", "ja_JP.UTF-8", "Japanese_Japan.932"):
                try:
                    locale.setlocale(locale.LC_TIME, name)
                except locale.Error:
                    continue
                tried += 1
                self.assertEqual(idle.format_date(2026, 3, 4, "mon_d_y"), "Mar 4, 2026", name)
                self.assertEqual(idle.format_date(2026, 5, 4, "d_mon_y"), "4 May 2026", name)
        finally:
            locale.setlocale(locale.LC_TIME, before)
        if not tried:
            self.skipTest("no other locale is installed here")

    def test_unusable_values_are_refused(self):
        for args in ((2026, 0, 1), (2026, 13, 1), (2026, 1, 0), (2026, 1, 32), (-1, 1, 1), (10000, 1, 1)):
            with self.assertRaises(ValueError, msg=args):
                idle.format_date(*args)
        with self.assertRaises(ValueError):
            idle.format_date(2026, 1, 1, "iso")

    def test_numeric_formats_are_zero_padded_and_textual_ones_are_not(self):
        self.assertEqual(idle.format_date(2026, 3, 4, "mdy"), "03/04/2026")
        self.assertEqual(idle.format_date(2026, 3, 4, "dmy"), "04/03/2026")
        self.assertEqual(idle.format_date(2026, 3, 4, "mon_d_y"), "Mar 4, 2026")
        self.assertEqual(idle.format_date(2026, 3, 4, "d_mon_y"), "4 Mar 2026")

    def test_the_dropdown_entries_show_the_given_day_in_each_format(self):
        choices = idle.date_format_choices(2026, 10, 5)
        self.assertEqual([fmt for fmt, _text in choices], list(idle.DATE_FORMATS))
        self.assertEqual([text.split("  (")[0] for _fmt, text in choices], ["Oct 5, 2026", "5 Oct 2026", "10/05/2026", "05/10/2026"])
        self.assertTrue(all(text.endswith(")") for _fmt, text in choices))                      # each says how to read it

    def test_the_widest_date_fits_the_screen_at_every_size(self):
        """Montserrat Medium, the firmware's typeface (the TTF that LVGL's own fonts are made from), measured with Pillow."""
        widest = {26: 0.0, 48: 0.0}
        for px in widest:
            font = ImageFont.truetype(resources.resource_path("Montserrat-Medium.ttf"), px)
            for year in (1970, 1999, 2026, 2088, 2111, 9999):
                for month in range(1, 13):
                    for day in range(1, 32):
                        for fmt in idle.DATE_FORMATS:
                            widest[px] = max(widest[px], font.getlength(idle.format_date(year, month, day, fmt)))
            self.assertLessEqual(widest[px], idle_layout.LABEL_WIDTH, px)
        self.assertLessEqual(widest[48], 440)
        for text in ("Sep 30, 2026", "30 Sep 2026"):
            self.assertLessEqual(ImageFont.truetype(resources.resource_path("Montserrat-Medium.ttf"), 48).getlength(text), 440)


# ---- the running clock: midnight, month ends, year ends, leap days ------------------------------------------------------------------

class Rollover(unittest.TestCase):
    CASES = [
        ((2026, 10, 13), "an ordinary day"), ((2026, 1, 31), "month end into February"), ((2026, 2, 28), "February end, not a leap year"),
        ((2024, 2, 28), "into the leap day"), ((2024, 2, 29), "the leap day into March"), ((2026, 4, 30), "a 30-day month"),
        ((2026, 12, 31), "year end"), ((1999, 12, 31), "century end 1999"), ((2099, 12, 31), "into 2100"),
        ((2100, 2, 28), "2100 has no leap day"), ((2000, 2, 28), "2000 has one"), ((2038, 1, 18), "next to the 32-bit limit"),
    ]

    def test_the_date_changes_exactly_at_local_midnight(self):
        for (y, m, d), what in self.CASES:
            midnight = local_ms(y, m, d) + MS_DAY
            today, tomorrow = dt.date(y, m, d), dt.date(y, m, d) + dt.timedelta(days=1)
            device = Device()
            device.sync(midnight - 5000, 1000)                      # synced five seconds before midnight, then left alone
            for t, expected in ((midnight - 5000, today), (midnight - 1, today), (midnight, tomorrow), (midnight + 1, tomorrow)):
                self.assertEqual(device.ymd(1000 + t - (midnight - 5000)), (expected.year, expected.month, expected.day), (what, t - midnight))

    def test_the_string_changes_once_at_midnight_in_every_format(self):
        for (y, m, d), what in self.CASES:
            midnight = local_ms(y, m, d) + MS_DAY
            for fmt in idle.DATE_FORMATS:
                self.assertNotEqual(idle.format_clock_ms(midnight - 1, fmt), idle.format_clock_ms(midnight, fmt), (what, fmt))
                self.assertEqual(idle.format_clock_ms(midnight, fmt), idle.format_clock_ms(midnight + MS_DAY - 1, fmt), (what, fmt))
                self.assertEqual(idle.format_clock_ms(midnight - MS_DAY, fmt), idle.format_clock_ms(midnight - 1, fmt), (what, fmt))

    def test_a_clock_that_runs_for_days_without_the_companion_keeps_the_right_date(self):
        """Synced once, never again (the companion went away): the display's own timer carries the date over several midnights."""
        start_ms = local_ms(2026, 12, 30, 20, 0, 0)
        device = Device()
        device.sync(start_ms, 123456)
        changes, last = 0, None
        for second in range(0, 96 * 3600, 7):                           # four days in 7-second steps
            when = EPOCH + dt.timedelta(milliseconds=start_ms + second * 1000)
            ymd = device.ymd(123456 + second * 1000)
            self.assertEqual(ymd, (when.year, when.month, when.day), second)
            if last is not None and ymd != last:
                changes += 1
            last = ymd
        self.assertEqual(changes, 4)                                    # Dec 31 -> Jan 1 -> 2 -> 3 -> 4
        self.assertEqual(device.text(123456 + 96 * 3600 * 1000 - 1), "Jan 3, 2027")

    def test_a_later_sync_corrects_the_date_and_its_offset(self):
        device = Device()
        device.sync(local_ms(2026, 10, 13, 23, 59, 59), 5000)
        self.assertEqual(device.text(5000), "Oct 13, 2026")
        self.assertEqual(device.text(6000), "Oct 14, 2026")
        device.sync(local_ms(2026, 10, 13, 18, 0, 0), 9000)             # the PC's offset changed (travel): the wall clock is earlier now
        self.assertEqual(device.text(9000), "Oct 13, 2026")
        self.assertEqual(device.text(9000 + 6 * 3600 * 1000), "Oct 14, 2026")


# ---- local time: only the offset inside clock_ms matters ------------------------------------------------------------------------

class LocalClock(unittest.TestCase):
    OFFSETS = (0, 330, 345, 765, 840, -570, -720, -210, 60, -300)

    def test_fixed_offsets_give_the_local_calendar_day_never_the_utc_one(self):
        rnd = random.Random(99)
        for offset in self.OFFSETS:
            tz = dt.timezone(dt.timedelta(minutes=offset))
            for _ in range(300):
                instant = utc(2026, 1, 1) + dt.timedelta(milliseconds=rnd.randrange(365 * MS_DAY))
                local = instant.astimezone(tz)
                ms = idle.local_clock_ms(local)
                self.assertEqual(ms, local_ms(local.year, local.month, local.day, local.hour, local.minute, local.second,
                                              local.microsecond // 1000), (offset, instant))
                self.assertEqual(idle.civil_from_clock_ms(ms), (local.year, local.month, local.day), (offset, instant))

    def test_moments_where_local_and_utc_dates_differ(self):
        cases = (                                       # (UTC instant, offset in minutes, local date, is the local date different from UTC's?)
            (utc(2026, 10, 12, 18, 15), 345, (2026, 10, 13), True),                # +05:45: already the 13th
            (utc(2026, 10, 12, 18, 14, 59, 999), 345, (2026, 10, 12), False),
            (utc(2026, 10, 14, 9, 0), -570, (2026, 10, 13), True),                 # -09:30: 09:00 UTC is 23:30 on the 13th
            (utc(2026, 10, 14, 9, 29, 59, 999), -570, (2026, 10, 13), True),
            (utc(2026, 10, 14, 9, 30), -570, (2026, 10, 14), False),
            (utc(2026, 10, 13, 10, 10), 840, (2026, 10, 14), True),                # +14:00 is up to a day ahead
            (utc(2027, 1, 1, 11, 59, 59, 999), -720, (2026, 12, 31), True),        # -12:00: still last year
            (utc(2027, 1, 1, 12, 0, 0), -720, (2027, 1, 1), False),
            (utc(2026, 10, 12, 21, 59, 59, 999), 330, (2026, 10, 13), True),       # +05:30
            (utc(2026, 10, 12, 18, 29, 59, 999), 330, (2026, 10, 12), False),
        )
        for instant, offset, expected, differs in cases:
            local = instant.astimezone(dt.timezone(dt.timedelta(minutes=offset)))
            self.assertEqual(idle.civil_from_clock_ms(idle.local_clock_ms(local)), expected, (instant, offset))
            self.assertEqual((instant.year, instant.month, instant.day) != expected, differs, (instant, offset))

    def test_every_vector_with_an_offset_round_trips_through_local_clock_ms(self):
        checked = 0
        for v in load_vectors():
            if v["utc_offset_min"] is None:
                continue
            tz = dt.timezone(dt.timedelta(minutes=v["utc_offset_min"]))
            instant = utc(1970, 1, 1) + dt.timedelta(milliseconds=v["clock_ms"] - v["utc_offset_min"] * 60000)
            local = instant.astimezone(tz)
            self.assertEqual(idle.local_clock_ms(local), v["clock_ms"], v)
            self.assertEqual(idle.format_clock_ms(idle.local_clock_ms(local), "mon_d_y"), v["formats"]["mon_d_y"])
            self.assertEqual((local.year, local.month, local.day), (v["year"], v["month"], v["day"]))
            checked += 1
        self.assertGreater(checked, 30)

    def test_extreme_offsets_and_instants(self):
        for offset in (-720, 840):
            tz = dt.timezone(dt.timedelta(minutes=offset))
            self.assertEqual(idle.local_clock_ms(utc(1970, 1, 1).astimezone(tz)), offset * 60000)
            self.assertEqual(idle.local_clock_ms(utc(2106, 2, 7, 6, 28, 16).astimezone(tz)), 2**32 * 1000 + offset * 60000)

    def test_a_naive_time_is_the_pcs_own_wall_clock(self):
        self.assertEqual(idle.local_clock_ms(dt.datetime(2026, 10, 13, 12, 0, 0, 500000)), local_ms(2026, 10, 13, 12, 0, 0, 500))
        self.assertEqual(idle.local_clock_ms(dt.datetime(2026, 1, 5, 12)), local_ms(2026, 1, 5, 12))

    def test_it_is_exact_to_the_millisecond(self):
        tz = dt.timezone(dt.timedelta(hours=-5))
        edge = dt.datetime(2026, 12, 31, 23, 59, 59, 999999, tzinfo=tz)                 # 999.999 ms: the millisecond is floored
        self.assertEqual(idle.local_clock_ms(edge), local_ms(2026, 12, 31, 23, 59, 59, 999))
        self.assertEqual(idle.civil_from_clock_ms(idle.local_clock_ms(edge)), (2026, 12, 31))
        self.assertEqual(idle.civil_from_clock_ms(idle.local_clock_ms(edge + dt.timedelta(microseconds=1))), (2027, 1, 1))

    def test_the_current_time_is_still_the_pcs_local_time(self):
        now = idle.local_clock_ms()
        expected = (dt.datetime.now().astimezone().replace(tzinfo=None) - EPOCH).total_seconds() * 1000
        self.assertLess(abs(now - expected), 2000)
        self.assertLess(abs(now - idle.local_clock_ms(dt.datetime.now())), 2000)

    # -- daylight saving, with zones that change their offset on the day itself
    def assert_local_date(self, aware):
        ms = idle.local_clock_ms(aware)
        self.assertEqual(idle.civil_from_clock_ms(ms), (aware.year, aware.month, aware.day), aware)
        for fmt in idle.DATE_FORMATS:
            self.assertEqual(idle.format_clock_ms(ms, fmt), idle.format_date(aware.year, aware.month, aware.day, fmt))
        return ms

    def test_us_spring_forward(self):
        before = self.assert_local_date(utc(2026, 3, 8, 6, 59, 59).astimezone(US_EASTERN))          # 01:59:59 EST
        after = self.assert_local_date(utc(2026, 3, 8, 7, 0, 0).astimezone(US_EASTERN))             # 03:00:00 EDT
        self.assertEqual((before, after), (local_ms(2026, 3, 8, 1, 59, 59), local_ms(2026, 3, 8, 3, 0, 0)))
        self.assertEqual(after - before, 3_601_000)                                                 # one real second, one lost hour
        self.assertEqual(idle.format_clock_ms(after), "Mar 8, 2026")
        late = utc(2026, 3, 9, 3, 59, 59, 999).astimezone(US_EASTERN)                               # 23:59:59.999 EDT on the 8th
        self.assertEqual(idle.format_clock_ms(self.assert_local_date(late)), "Mar 8, 2026")
        self.assertEqual(idle.format_clock_ms(self.assert_local_date(late + dt.timedelta(milliseconds=1))), "Mar 9, 2026")

    def test_us_fall_back_repeats_an_hour_and_the_date_does_not_move(self):
        first = utc(2026, 11, 1, 5, 30).astimezone(US_EASTERN)                                      # 01:30 EDT
        second = utc(2026, 11, 1, 6, 30).astimezone(US_EASTERN)                                     # 01:30 EST, an hour later
        self.assertEqual((first.fold, second.fold), (0, 1))
        self.assertEqual(self.assert_local_date(first), self.assert_local_date(second))             # the wall clock shows the same time twice
        self.assertEqual(idle.format_clock_ms(self.assert_local_date(second)), "Nov 1, 2026")
        late = utc(2026, 11, 2, 4, 59, 59, 999).astimezone(US_EASTERN)                              # 23:59:59.999 EST
        self.assertEqual(idle.format_clock_ms(self.assert_local_date(late)), "Nov 1, 2026")
        self.assertEqual(idle.format_clock_ms(self.assert_local_date(late + dt.timedelta(milliseconds=1))), "Nov 2, 2026")

    def test_southern_hemisphere_and_a_half_hour_dst(self):
        for zone, instants in ((SYDNEY, ((2026, 4, 4, 15, 59, 59), (2026, 4, 4, 16, 0, 0), (2026, 10, 3, 15, 59, 59), (2026, 10, 3, 16, 0, 0))),
                               (LORD_HOWE, ((2026, 4, 4, 14, 59, 59), (2026, 4, 4, 15, 0, 0), (2026, 10, 3, 15, 29, 59), (2026, 10, 3, 15, 30, 0)))):
            for parts in instants:
                local = utc(*parts).astimezone(zone)
                self.assert_local_date(local)
                self.assertNotEqual((local.day, local.hour), (parts[2], parts[3]))                  # (their local day / hour is not UTC's)
        lord_howe = utc(2026, 10, 3, 15, 30).astimezone(LORD_HOWE)                                  # 02:30, not 02:00: the half hour jumped
        self.assertEqual((lord_howe.hour, lord_howe.minute), (2, 30))
        self.assertEqual(idle.local_clock_ms(lord_howe), local_ms(2026, 10, 4, 2, 30))

    def test_a_dst_jump_at_midnight_forward(self):
        before = utc(2026, 3, 28, 21, 59, 59, 999).astimezone(MIDNIGHT_FORWARD)                     # 23:59:59.999 on the 28th
        after = utc(2026, 3, 28, 22, 0, 0).astimezone(MIDNIGHT_FORWARD)                             # 01:00:00 on the 29th: there was no 00:xx
        self.assertEqual(idle.format_clock_ms(self.assert_local_date(before)), "Mar 28, 2026")
        self.assertEqual(idle.format_clock_ms(self.assert_local_date(after)), "Mar 29, 2026")
        self.assertEqual(idle.local_clock_ms(after), local_ms(2026, 3, 29, 1, 0, 0))

    def test_a_dst_end_at_midnight_repeats_the_last_hour_of_the_day(self):
        """Sao Paulo until 2019: at 00:00 on 18 Feb 2018 the clocks went back to 23:00 on the 17th. The date follows the wall clock, so
        the display shows the 17th for two hours and changes to the 18th when the repeated hour has run."""
        cases = ((utc(2018, 2, 18, 1, 59, 59, 999), (2018, 2, 17), 23), (utc(2018, 2, 18, 2, 0, 0), (2018, 2, 17), 23),
                 (utc(2018, 2, 18, 2, 59, 59, 999), (2018, 2, 17), 23), (utc(2018, 2, 18, 3, 0, 0), (2018, 2, 18), 0))
        for instant, ymd, hour in cases:
            local = instant.astimezone(SAO_PAULO)
            self.assertEqual((local.year, local.month, local.day, local.hour), ymd + (hour,), instant)
            self.assertEqual(idle.civil_from_clock_ms(self.assert_local_date(local)), ymd)

    def test_the_zone_helper_itself_is_a_real_pep495_zone(self):
        a, b = utc(2026, 11, 1, 5, 30).astimezone(US_EASTERN), utc(2026, 11, 1, 6, 30).astimezone(US_EASTERN)
        self.assertEqual((a.utcoffset(), b.utcoffset()), (dt.timedelta(hours=-4), dt.timedelta(hours=-5)))
        self.assertEqual(a.astimezone(dt.timezone.utc), utc(2026, 11, 1, 5, 30))
        self.assertEqual(b.astimezone(dt.timezone.utc), utc(2026, 11, 1, 6, 30))


# ---- settings: fields, validation, persistence, version, /idle ------------------------------------------------------------------

class SettingsFields(unittest.TestCase):
    def test_defaults(self):
        s = IdleSettings().validated()
        self.assertEqual((s.show_date, s.date_format), (True, "mon_d_y"))

    def test_every_format_is_kept(self):
        for fmt in idle.DATE_FORMATS:
            self.assertEqual(IdleSettings(date_format=fmt).validated().date_format, fmt)

    def test_unknown_formats_become_the_default(self):
        for bad in ("iso", "", "MDY", None, 3, 1.5, ["mdy"], {"a": 1}, b"mdy"):
            self.assertEqual(IdleSettings(date_format=bad).validated().date_format, "mon_d_y", bad)

    def test_show_date_must_be_a_real_boolean(self):
        self.assertFalse(IdleSettings(show_date=False).validated().show_date)
        self.assertTrue(IdleSettings(show_date=True).validated().show_date)
        for bad in ("no", "false", 0, 1, None, [], 2.5):                    # a damaged file: the date is shown (the default)
            self.assertTrue(IdleSettings(show_date=bad).validated().show_date, bad)

    def test_the_date_is_independent_of_the_clock(self):
        s = IdleSettings(show_clock=False, show_date=True, clock_24h=False).validated()
        self.assertEqual((s.show_clock, s.show_date, s.clock_24h), (False, True, False))
        s = IdleSettings(show_clock=True, show_date=False).validated()
        self.assertEqual((s.show_clock, s.show_date), (True, False))


class Persistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "idle")

    def tearDown(self):
        self.tmp.cleanup()

    def write_file(self, settings, version=7):
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, "settings.json"), "w") as f:
            json.dump({"version": version, "settings": settings, "image": None}, f)

    def test_round_trip_through_settings_json_for_all_formats(self):
        for fmt in idle.DATE_FORMATS:
            for show in (True, False):
                with tempfile.TemporaryDirectory() as d:
                    a = IdleStore(os.path.join(d, "idle"))
                    a.update(date_format=fmt, show_date=show, text="x")                     # (a change, so that a file is written)
                    with open(os.path.join(d, "idle", "settings.json"), encoding="utf-8") as f:
                        saved = json.load(f)["settings"]
                    self.assertEqual((saved["show_date"], saved["date_format"]), (show, fmt))
                    b = IdleStore(os.path.join(d, "idle"))
                    self.assertEqual((b.settings().show_date, b.settings().date_format), (show, fmt))
                    self.assertEqual(b.settings(), a.settings())
                    self.assertEqual(b.version, a.version)

    def test_a_file_from_the_previous_version_loads_with_the_defaults(self):
        self.write_file({"theme": "neon", "accent": "#FF4FA3", "show_clock": False, "clock_24h": False, "text": "Hi", "bg_mode": "blur",
                         "delay_s": 9, "image_name": ""})
        s = IdleStore(self.dir)
        self.assertEqual((s.settings().show_date, s.settings().date_format), (True, "mon_d_y"))
        self.assertEqual((s.settings().theme, s.settings().show_clock, s.settings().text, s.settings().delay_s), ("neon", False, "Hi", 9))
        self.assertEqual(s.version, 7)                                         # nothing changed, so nothing is bumped
        cfg = s.config()
        self.assertEqual((cfg["date"], cfg["date_fmt"]), (True, 0))
        s.update(date_format="dmy")                                           # and the new keys are written from now on
        with open(os.path.join(self.dir, "settings.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["settings"]["date_format"], "dmy")
        self.assertEqual(IdleStore(self.dir).settings().date_format, "dmy")

    def test_corrupt_values_in_the_file_become_the_defaults(self):
        for bad in ({"show_date": "maybe", "date_format": 12}, {"show_date": None, "date_format": None},
                    {"show_date": [], "date_format": {"x": 1}}, {"show_date": 7, "date_format": "yyyy-mm-dd"}):
            self.write_file(dict(bad, theme="minimal"))
            s = IdleStore(self.dir).settings()
            self.assertEqual((s.show_date, s.date_format, s.theme), (True, "mon_d_y", "minimal"), bad)

    def test_valid_values_in_a_hand_edited_file_are_used(self):
        self.write_file({"show_date": False, "date_format": "mdy"})
        s = IdleStore(self.dir).settings()
        self.assertEqual((s.show_date, s.date_format), (False, "mdy"))

    def test_a_damaged_file_falls_back_to_the_defaults_including_the_date(self):
        os.makedirs(self.dir)
        with open(os.path.join(self.dir, "settings.json"), "wb") as f:
            f.write(b"{ not json")
        s = IdleStore(self.dir).settings()
        self.assertEqual((s.show_date, s.date_format), (True, "mon_d_y"))

    def test_the_version_is_bumped_by_a_change_and_only_by_a_change(self):
        s = IdleStore(self.dir)
        v = s.version
        s.update(show_date=True, date_format="mon_d_y")                       # the same values
        self.assertEqual(s.version, v)
        s.update(show_date=False)
        self.assertEqual(s.version, v + 1)
        s.update(date_format="d_mon_y")
        self.assertEqual(s.version, v + 2)
        s.update(show_date=False, date_format="d_mon_y")
        self.assertEqual(s.version, v + 2)
        s.update(show_date=True, date_format="dmy")                           # two changes in one call: one new version
        self.assertEqual(s.version, v + 3)
        self.assertEqual(IdleStore(self.dir).version, v + 3)

    def test_an_invalid_update_is_cleaned_like_a_damaged_file(self):
        s = IdleStore(self.dir)
        s.update(date_format="dmy")
        s.update(date_format="bogus")
        self.assertEqual(s.settings().date_format, "mon_d_y")
        s.update(show_date="nope")
        self.assertTrue(s.settings().show_date)

    def test_reset_brings_the_date_back(self):
        s = IdleStore(self.dir)
        s.update(show_date=False, date_format="mdy")
        v = s.version
        s.reset()
        self.assertEqual((s.settings().show_date, s.settings().date_format), (True, "mon_d_y"))
        self.assertEqual(s.version, v + 1)

    def test_idle_config_json(self):
        s = IdleStore(self.dir)
        self.assertEqual(s.config(), {"ok": True, "version": 1, "theme": "dark", "accent": "#4DA3FF", "clock": True, "clock_24h": True,
                                      "date": True, "date_fmt": 0, "text": "", "bg_mode": "fill", "delay_s": 4, "image": None})
        for number, fmt in enumerate(idle.DATE_FORMATS):
            s.update(date_format=fmt)
            self.assertEqual(s.config()["date_fmt"], number)
        s.update(show_date=False)
        cfg = s.config()
        self.assertEqual((cfg["date"], cfg["date_fmt"]), (False, 3))
        json.dumps(cfg)                                                       # plain JSON types only

    def test_update_rejects_unknown_names_but_knows_the_new_ones(self):
        s = IdleStore(self.dir)
        s.update(show_date=False, date_format="dmy")
        with self.assertRaises(ValueError):
            s.update(date_fmt="dmy")                                          # the wire name is not a setting name


# ---- the preview ---------------------------------------------------------------------------------------------------------------

class Recorder:
    """Wraps idle._put_text: remembers every draw call (and optionally skips the drawing, which makes many renders cheap)."""

    def __init__(self, draw=True):
        self.calls = []
        self.draw = draw
        self.original = idle._put_text

    def __call__(self, img, items, font, colour, opacity=255, anchor="ls"):
        self.calls.append({"items": list(items), "colour": colour, "opacity": opacity, "size": font.size, "anchor": anchor})
        return self.original(img, items, font, colour, opacity, anchor) if self.draw else (0, 0, 1, 1)

    def __enter__(self):
        self.patch = mock.patch.object(idle, "_put_text", self)
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()

    def texts(self):
        """What each draw call wrote, in order (the clock's digits are drawn one by one: their call counts as one string)."""
        return ["".join(item[1] for item in call["items"]) for call in self.calls]


def render(settings, size=idle.SCREEN, now=NOW, jpeg=None):
    boxes = {}
    img = idle.render_preview(settings, jpeg, size, now, boxes)
    return img, boxes


def overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def grey_jpeg(level=200, size=480):
    import io
    buf = io.BytesIO()
    Image.new("RGB", (size, size), (level, level, level)).save(buf, "JPEG", quality=95)
    return buf.getvalue()


class Layout(unittest.TestCase):
    TABLE = (                                                                   # clock, text, date -> clock_cy, date_cy, date px, line_y, text_cy
        (True, True, False, 168, None, 26, 262, 306), (True, False, False, 205, None, 26, 300, None),
        (False, True, False, None, None, 48, 268, 215), (False, False, False, None, None, 48, 240, None),
        (True, False, True, 172, 252, 26, 296, None), (True, True, True, 138, 214, 26, 252, 300),
        (False, False, True, None, 215, 48, 262, None), (False, True, True, None, 178, 48, 236, 290),
    )

    def test_the_device_layout_contract(self):
        for clock, text, date, clock_cy, date_cy, date_px, line_y, text_cy in self.TABLE:
            lay = idle_layout.idle_layout(clock, text, date)
            self.assertEqual((lay.clock_cy, lay.date_cy, lay.line_y, lay.text_cy), (clock_cy, date_cy, line_y, text_cy), (clock, text, date))
            self.assertEqual(lay.date_px, date_px)
            self.assertEqual(lay.text_px, 32 if clock else 48)

    def test_hiding_the_date_keeps_the_old_positions(self):
        self.assertEqual(idle_layout.idle_layout(True, True, False).line_y, 262)
        self.assertEqual(idle_layout.idle_layout(False, False, False).line_y, 240)

    def test_font_metrics_match_the_ones_in_lvgl(self):
        self.assertEqual(idle_layout.FONT_METRICS[26], (29, 5))
        self.assertEqual(idle_layout.FONT_METRICS[48], (52, 9))
        self.assertEqual(idle_layout.FONT_METRICS[128], (157, 33))
        self.assertEqual(idle_layout.label_top(252, 26), 252 - 14)               # the line box is centred: integer half of 29
        self.assertEqual(idle_layout.label_baseline(238, 26), 238 + 29 - 5)


class PreviewDate(unittest.TestCase):
    def test_the_date_is_drawn_only_when_enabled(self):
        for theme in idle.THEMES:
            for clock in (True, False):
                for text in ("", "Hi"):
                    _img, on = render(IdleSettings(theme=theme, show_clock=clock, text=text, show_date=True))
                    _img, off = render(IdleSettings(theme=theme, show_clock=clock, text=text, show_date=False))
                    self.assertIn("date", on)
                    self.assertNotIn("date", off)

    def test_pixels_follow_the_setting(self):
        s = IdleSettings(theme="minimal", show_clock=False, text="", show_date=True)
        on, boxes = render(s)
        off, _ = render(IdleSettings(theme="minimal", show_clock=False, text="", show_date=False))
        region = (100, 180, 380, 236)                                           # where the date sits when there is neither clock nor line
        self.assertGreater(max(on.crop(region).convert("L").tobytes()), 180)
        self.assertLess(max(off.crop(region).convert("L").tobytes()), 60)
        self.assertTrue(boxes["date"][1] >= 180 and boxes["date"][3] <= 245)

    def test_the_text_is_exactly_format_date_for_every_format_and_vector(self):
        vectors = load_vectors()
        with Recorder(draw=False) as rec:
            for v in vectors:
                local = dt.datetime(v["year"], v["month"], v["day"], 14, 5)
                for fmt in idle.DATE_FORMATS:
                    rec.calls.clear()
                    idle.render_preview(IdleSettings(show_date=True, date_format=fmt), None, 120, local)
                    self.assertIn(v["formats"][fmt], rec.texts(), (v["clock_ms"], fmt))
                    self.assertEqual(sum(1 for t in rec.texts() if t == v["formats"][fmt]), 1)

    def test_the_text_comes_from_the_wall_clock_of_now_whatever_its_zone(self):
        for tz in (dt.timezone(dt.timedelta(hours=14)), dt.timezone(dt.timedelta(minutes=-570)), US_EASTERN):
            now = dt.datetime(2026, 12, 31, 23, 59, 59, 999000, tzinfo=tz)
            with Recorder(draw=False) as rec:
                idle.render_preview(IdleSettings(show_date=True, date_format="mdy"), None, 120, now)
            self.assertIn("12/31/2026", rec.texts())
            self.assertIn("23:59", rec.texts())

    def test_the_default_now_is_the_pcs_current_date(self):
        before = dt.datetime.now()
        with Recorder(draw=False) as rec:
            idle.render_preview(IdleSettings(), None, 120)
        after = dt.datetime.now()
        possible = {idle.format_date(t.year, t.month, t.day) for t in (before, after)}              # (a render can straddle midnight)
        self.assertTrue(possible & set(rec.texts()), (possible, rec.texts()))

    def test_clock_and_date_are_independent(self):
        _img, boxes = render(IdleSettings(show_clock=False, show_date=True))
        self.assertIn("date", boxes)
        self.assertNotIn("clock", boxes)
        _img, boxes = render(IdleSettings(show_clock=True, show_date=False))
        self.assertIn("clock", boxes)
        self.assertNotIn("date", boxes)
        _img, boxes = render(IdleSettings(show_clock=False, show_date=False, theme="dark"))
        self.assertNotIn("clock", boxes)
        self.assertNotIn("date", boxes)

    def test_am_pm_only_with_the_12_hour_clock(self):
        for clock, h24, expected in ((True, False, True), (True, True, False), (False, False, False)):
            _img, boxes = render(IdleSettings(show_clock=clock, clock_24h=h24))
            self.assertEqual("ampm" in boxes, expected, (clock, h24))
        with Recorder(draw=False) as rec:
            idle.render_preview(IdleSettings(clock_24h=False), None, 120, dt.datetime(2026, 10, 13, 0, 7))
            idle.render_preview(IdleSettings(clock_24h=False), None, 120, dt.datetime(2026, 10, 13, 12, 0))
            idle.render_preview(IdleSettings(clock_24h=False), None, 120, dt.datetime(2026, 10, 13, 23, 59))
        self.assertEqual([t for t in rec.texts() if t in ("AM", "PM") or ":" in t], ["12:07", "AM", "12:00", "PM", "11:59", "PM"])

    def test_scaled_previews_keep_the_geometry(self):
        for clock, text in ((True, True), (True, False), (False, True), (False, False)):
            s = IdleSettings(show_clock=clock, text="Hello" if text else "", show_date=True)
            _img, full = render(s, 480)
            img, half = render(s, 240)
            self.assertEqual(img.size, (240, 240))
            for name, box in half.items():
                self.assertTrue(all(abs(a * 2 - b) <= 4 for a, b in zip(box, full[name])), (name, box, full[name]))


class PreviewBoxes(unittest.TestCase):
    def test_every_element_stays_inside_the_480_box_and_the_date_overlaps_nothing(self):
        times = (dt.datetime(2026, 10, 13, 14, 5), dt.datetime(2026, 9, 30, 8, 8), dt.datetime(2026, 12, 31, 23, 59), dt.datetime(2027, 1, 5, 12, 0),
                 dt.datetime(2026, 5, 20, 20, 20))
        cases = [(theme, clock, text, h24, "mon_d_y", NOW)                                  # every theme x clock x custom line x 12/24 h ...
                 for theme in idle.THEMES for clock in (True, False) for text in ("", "Hello world", "W" * 40) for h24 in (True, False)]
        cases += [("neon", clock, text, h24, fmt, now)                                      # ... and every format at several dates / times
                  for clock in (True, False) for text in ("", "Hello") for h24 in (True, False) for fmt in idle.DATE_FORMATS for now in times]
        for theme, clock, text, h24, fmt, now in cases:
            s = IdleSettings(theme=theme, show_clock=clock, text=text, clock_24h=h24, date_format=fmt, show_date=True)
            _img, boxes = render(s, now=now)
            where = (theme, clock, text, h24, fmt, now)
            for name, box in boxes.items():
                self.assertTrue(0 <= box[0] < box[2] <= 480 and 0 <= box[1] < box[3] <= 480, (where, name, box))
            date = boxes["date"]
            self.assertLessEqual(date[2] - date[0], idle_layout.LABEL_WIDTH, where)
            self.assertTrue(date[0] >= 20 and date[2] <= 460, (where, date))
            for name, box in boxes.items():
                if name != "date":
                    self.assertFalse(overlap(date, box), (where, name, box, date))
        self.assertGreater(len(cases), 150)

    def test_a_clear_gap_between_the_date_and_its_neighbours(self):
        """Not just 'no overlap': at least 10 px between the date and the clock, the accent line and the custom line (the contrast
        rings reach 3 px beyond the glyphs)."""
        for clock, text in ((True, False), (True, True), (False, False), (False, True)):
            s = IdleSettings(theme="neon", show_clock=clock, text="Hello" if text else "", show_date=True)
            _img, boxes = render(s)
            for name in ("clock", "line", "text"):
                if name in boxes:
                    a, b = sorted((boxes["date"], boxes[name]), key=lambda box: box[1])
                    self.assertGreaterEqual(b[1] - a[3], 10, (clock, text, name, boxes))

    def test_the_stored_boxes_of_a_480_render(self):
        with open(os.path.join(DATA, "idle_preview_boxes.json"), encoding="utf-8") as f:
            stored = json.load(f)
        self.assertEqual(len(stored), 12)
        for entry in stored:
            s = IdleSettings(theme="dark", show_clock=entry["clock"], clock_24h=entry["clock_24h"], show_date=entry["date"],
                             text="Hello world" if entry["text"] else "")
            _img, boxes = render(s, idle.SCREEN, NOW)
            self.assertEqual(set(boxes), set(entry["boxes"]), entry)
            for name, expected in entry["boxes"].items():
                for got, want in zip(boxes[name], expected):
                    self.assertLessEqual(abs(got - want), 2, (entry, name, boxes[name], expected))

    def test_the_date_is_centred_in_its_label_box(self):
        """The label's line box is centred on date_cy (like the device) and the glyphs sit inside it, horizontally centred."""
        for clock, text, date, _clock_cy, date_cy, date_px, _line, _text_cy in Layout.TABLE:
            if not date:
                continue
            s = IdleSettings(show_clock=clock, text="Hello" if text else "", show_date=True)
            _img, boxes = render(s)
            top = idle_layout.label_top(date_cy, date_px)
            bottom = top + idle_layout.FONT_METRICS[date_px][0]
            left, t, right, b = boxes["date"]
            self.assertTrue(top <= t and b <= bottom, (boxes["date"], top, bottom))
            self.assertLessEqual(abs((left + right) / 2 - 240), 3)
            self.assertLessEqual(abs((t + b) / 2 - date_cy), date_px * 0.2)

    def test_the_clock_digits_are_centred_on_clock_cy(self):
        for clock, text, date, clock_cy, *_ in Layout.TABLE:
            if clock_cy is None:
                continue
            _img, boxes = render(IdleSettings(show_clock=True, text="Hello" if text else "", show_date=date))
            self.assertLessEqual(abs((boxes["clock"][1] + boxes["clock"][3]) / 2 - clock_cy), 3, (text, date))


class PreviewContrast(unittest.TestCase):
    def date_calls(self, theme, fmt="mon_d_y"):
        with Recorder() as rec:
            idle.render_preview(IdleSettings(theme=theme, show_clock=False, text="", date_format=fmt, show_date=True),
                                grey_jpeg() if theme == "photo" else None, 480, NOW)
        text = idle.format_date(2026, 10, 13, fmt)
        return [c for c in rec.calls if text in [i[1] for i in c["items"]]]

    def test_photo_has_four_black_copies_behind_the_date(self):
        calls = self.date_calls("photo")
        self.assertEqual(len(calls), 5)
        main = calls[-1]
        self.assertEqual((main["colour"], main["opacity"]), (idle_layout.DATE_COLOUR, 255))
        mx, my = main["items"][0][0]
        offsets = set()
        for c in calls[:-1]:
            self.assertEqual((c["colour"], c["opacity"]), ((0, 0, 0), 110))
            (x, y), _ = c["items"][0]
            offsets.add((round(x - mx), round(y - my)))
        self.assertEqual(offsets, {(-2, 0), (2, 0), (0, -2), (0, 2)})

    def test_neon_has_four_accent_copies_behind_the_date(self):
        accent = "#FF4FA3"
        with Recorder() as rec:
            idle.render_preview(IdleSettings(theme="neon", accent=accent, show_clock=False, show_date=True), None, 480, NOW)
        calls = [c for c in rec.calls if "Oct 13, 2026" in [i[1] for i in c["items"]]]
        self.assertEqual(len(calls), 5)
        for c in calls[:-1]:
            self.assertEqual((c["colour"], c["opacity"]), ((0xFF, 0x4F, 0xA3), 46))
        self.assertEqual((calls[-1]["colour"], calls[-1]["opacity"]), (idle_layout.DATE_COLOUR, 255))

    def test_minimal_and_dark_have_no_outline(self):
        for theme in ("minimal", "dark"):
            self.assertEqual(len(self.date_calls(theme)), 1, theme)

    def test_a_photo_theme_without_a_usable_picture_is_dark_and_has_no_outline(self):
        for jpeg in (None, b"not a jpeg"):
            with Recorder() as rec:
                idle.render_preview(IdleSettings(theme="photo", show_clock=False, show_date=True), jpeg, 480, NOW)
            self.assertEqual(len([c for c in rec.calls if "Oct 13, 2026" in [i[1] for i in c["items"]]]), 1)

    def test_the_photo_outline_darkens_the_pixels_around_the_glyphs(self):
        """Uniform light photo: far from the text every pixel is the same dimmed grey; right next to the glyphs the black copies
        make some darker than that (and the date text itself is brighter)."""
        img, boxes = render(IdleSettings(theme="photo", show_clock=False, text="", show_date=True), jpeg=grey_jpeg(200))
        base = img.getpixel((20, 300))
        self.assertEqual(base, img.getpixel((460, 300)))
        left, top, right, bottom = boxes["date"]
        ring = img.crop((left - 3, top - 3, right + 3, bottom + 3)).convert("L")
        values = list(ring.tobytes())
        self.assertLess(min(values), base[0] - 25)
        self.assertGreater(max(values), base[0] + 30)
        self.assertEqual(img.getpixel((left - 12, (top + bottom) // 2)), base)                # the outline is only 2 px wide

    def test_the_photo_is_dimmed_by_30_percent_under_everything(self):
        img, _ = render(IdleSettings(theme="photo", show_clock=False, show_date=False, text=""), jpeg=grey_jpeg(200))
        self.assertTrue(all(abs(c - 140) <= 2 for c in img.getpixel((20, 300))))

    def test_neon_pixels_next_to_the_date_pick_up_the_accent(self):
        """Just left of the date's first glyph (where the 2 px shifted copy reaches and the glyph itself does not) the red of a pink
        accent shows up over the dark blue background."""
        img, boxes = render(IdleSettings(theme="neon", accent="#FF4FA3", show_clock=False, show_date=True, text=""))
        plain, _ = render(IdleSettings(theme="neon", accent="#FF4FA3", show_clock=False, show_date=False, text=""))
        left, top, _right, bottom = boxes["date"]
        strip = (left - 2, top + (bottom - top) // 3, left, bottom - (bottom - top) // 3)
        change = max(a - b for a, b in zip(img.crop(strip).getchannel("R").tobytes(), plain.crop(strip).getchannel("R").tobytes()))
        self.assertGreater(change, 25)

    def test_custom_line_shadow_is_photo_only(self):
        for theme, copies in (("photo", 2), ("neon", 1), ("dark", 1), ("minimal", 1)):
            with Recorder() as rec:
                idle.render_preview(IdleSettings(theme=theme, show_clock=False, text="Hello", show_date=False),
                                    grey_jpeg() if theme == "photo" else None, 480, NOW)
            self.assertEqual(len([c for c in rec.calls if "Hello" in [i[1] for i in c["items"]]]), copies, theme)


class PreviewFonts(unittest.TestCase):
    def test_the_bundled_font_is_montserrat_medium(self):
        font = ImageFont.truetype(resources.resource_path("Montserrat-Medium.ttf"), 48)
        self.assertEqual(font.getname(), ("Montserrat", "Medium"))

    def test_a_missing_font_does_not_break_the_preview(self):
        idle._montserrat.cache_clear()
        try:
            with mock.patch.object(idle, "resource_path", lambda name: os.path.join(HERE, "no-such-font.ttf")):
                with self.assertLogs("idle", level="WARNING"):
                    img, boxes = render(IdleSettings(show_date=True), size=240)
            self.assertEqual(img.size, (240, 240))
            self.assertIn("date", boxes)
        finally:
            idle._montserrat.cache_clear()

    def test_long_custom_lines_are_shortened_not_wrapped(self):
        _img, boxes = render(IdleSettings(show_clock=False, text="W" * 40, show_date=True))
        self.assertLessEqual(boxes["text"][2] - boxes["text"][0], idle_layout.LABEL_WIDTH)
        with Recorder(draw=False) as rec:
            idle.render_preview(IdleSettings(show_clock=False, text="W" * 40), None, 480, NOW)
        shown = [t for t in rec.texts() if t.startswith("W")]
        self.assertTrue(shown and shown[0].endswith("..."))


class ResourcePath(unittest.TestCase):
    def test_the_font_ships_inside_the_package(self):
        path = resources.resource_path("Montserrat-Medium.ttf")
        self.assertTrue(os.path.isfile(path), path)
        self.assertTrue(path.replace("\\", "/").endswith("desk_media_companion/assets/Montserrat-Medium.ttf"))
        self.assertTrue(os.path.isfile(resources.resource_path("OFL-1.1-Montserrat.txt")))        # its licence travels with it

    def test_a_pyinstaller_bundle_is_found_under_meipass(self):
        with mock.patch.object(sys, "_MEIPASS", os.path.join("C:\\", "bundle"), create=True):
            self.assertEqual(resources.resource_path("Montserrat-Medium.ttf"),
                             os.path.join("C:\\", "bundle", "desk_media_companion", "assets", "Montserrat-Medium.ttf"))

    def test_only_plain_file_names_are_accepted(self):
        for bad in ("", ".", "..", "../x.ttf", "sub/x.ttf", "sub\\x.ttf", "C:\\x.ttf"):
            with self.assertRaises(ValueError, msg=bad):
                resources.resource_path(bad)


class ToolFiles(unittest.TestCase):
    def test_the_committed_vectors_are_what_the_tool_produces(self):
        tool = load_tool("make_date_vectors")
        self.assertEqual(load_vectors(), tool.build_vectors())

    def test_the_vector_file_is_plain(self):
        with open(os.path.join(DATA, "idle_date_vectors.json"), "rb") as f:
            raw = f.read()
        self.assertNotIn(b"\r", raw)
        for v in json.loads(raw):
            self.assertEqual(list(v), ["clock_ms", "utc_offset_min", "year", "month", "day", "formats"])
            self.assertIsInstance(v["clock_ms"], int)
            self.assertTrue(v["utc_offset_min"] is None or isinstance(v["utc_offset_min"], int))
            self.assertEqual(list(v["formats"]), list(idle.DATE_FORMATS))

    def test_the_vectors_cover_what_the_specification_asks_for(self):
        by_ms = {v["clock_ms"]: v for v in load_vectors() if v["utc_offset_min"] is None}
        self.assertIn(0, by_ms)                                                                  # the epoch
        self.assertEqual(by_ms[-1000]["formats"]["mon_d_y"], "Dec 31, 1969")                     # 1969-12-31 23:59:59
        self.assertEqual(by_ms[local_ms(2024, 2, 29)]["formats"]["mdy"], "02/29/2024")           # a leap day
        self.assertEqual(by_ms[local_ms(2000, 2, 29)]["formats"]["dmy"], "29/02/2000")
        self.assertEqual(by_ms[local_ms(2100, 2, 28, 23, 59, 59, 999)]["formats"]["d_mon_y"], "28 Feb 2100")
        self.assertEqual(by_ms[local_ms(2100, 3, 1)]["formats"]["d_mon_y"], "1 Mar 2100")       # 2100 is not a leap year
        self.assertEqual(by_ms[local_ms(2026, 12, 31, 23, 59, 59, 999)]["formats"]["mon_d_y"], "Dec 31, 2026")
        self.assertEqual(by_ms[local_ms(2027, 1, 1)]["formats"]["mon_d_y"], "Jan 1, 2027")
        self.assertEqual(by_ms[local_ms(2027, 1, 5, 9, 5)]["formats"]["mdy"], "01/05/2027")
        self.assertIn(local_ms(2038, 1, 19, 3, 14, 8), by_ms)
        self.assertIn(local_ms(2106, 2, 7, 6, 28, 16), by_ms)
        offsets = {v["utc_offset_min"] for v in load_vectors()}
        self.assertTrue({345, -570, 840, -720, 330, 0} <= offsets, offsets)


# ---- the server ------------------------------------------------------------------------------------------------------------------

class ServerIdle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from desk_media_companion.server import make_server
        from test_server import StubMonitor
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = IdleStore(os.path.join(cls.tmp.name, "idle"))
        cls.server = make_server(StubMonitor(), 0, idle=cls.store)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        self.store.reset()                                                                        # the store is shared by the tests of this class

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
            return json.loads(r.read())

    def test_idle_exposes_date_and_date_fmt(self):
        cfg = self.get("/idle")
        self.assertEqual((cfg["date"], cfg["date_fmt"]), (True, 0))
        for number, fmt in enumerate(idle.DATE_FORMATS):
            before = self.get("/idle")["version"]
            self.store.update(date_format=fmt)
            cfg = self.get("/idle")
            self.assertEqual((cfg["date"], cfg["date_fmt"]), (True, number))
            self.assertEqual(cfg["version"], before + (1 if number else 0))                           # the first one is the default: no change
        self.store.update(show_date=False)
        cfg = self.get("/idle")
        self.assertEqual((cfg["date"], cfg["date_fmt"]), (False, 3))
        self.assertIn("clock_24h", cfg)                                                           # nothing else moved
        self.assertIn("image", cfg)

    def test_a_change_moves_the_version_the_display_polls(self):
        before = self.get("/now-playing")["idle_v"]
        self.store.update(show_date=not self.store.settings().show_date)
        self.assertEqual(self.get("/now-playing")["idle_v"], before + 1)
        self.assertEqual(self.get("/idle")["version"], before + 1)

    def test_now_playing_clock_ms_decodes_to_todays_local_date(self):
        for _ in range(3):
            lo = dt.datetime.now()
            ms = self.get("/now-playing")["clock_ms"]
            hi = dt.datetime.now()
            self.assertIn(idle.civil_from_clock_ms(ms), {(lo.year, lo.month, lo.day), (hi.year, hi.month, hi.day)})


# ---- the settings window -------------------------------------------------------------------------------------------------------

class SettingsWindowDate(unittest.TestCase):
    def setUp(self):
        try:
            import tkinter as tk
            self.root = tk.Tk()
        except Exception as exc:                                                                  # no Tk, or no desktop to open a window on
            self.skipTest(f"Tk is not usable here: {exc}")
        self.root.withdraw()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = IdleStore(os.path.join(self.tmp.name, "idle"))
        from desk_media_companion.ui import SettingsWindow
        self.window = SettingsWindow(self.store)
        self.today = dt.date(2026, 10, 13)
        self.window._today = lambda: self.today
        self.ui = self.window.attach(self.root)

    def tearDown(self):
        try:
            for after_id in self.root.tk.splitlist(self.root.tk.call("after", "info")):          # no timer may outlive the window
                self.root.after_cancel(after_id)
            self.root.destroy()
        except Exception:
            pass
        self.ui = self.window = self.root = None
        gc.collect()                  # Tk variables must be finalised in this thread: from another one each makes that thread wait a second
        self.tmp.cleanup()

    def entries(self):
        return list(self.ui.date_box.cget("values"))

    def test_the_dropdown_shows_todays_date_in_each_format(self):
        entries = self.entries()
        self.assertEqual([e.split("  (")[0] for e in entries], ["Oct 13, 2026", "13 Oct 2026", "10/13/2026", "13/10/2026"])
        self.assertEqual(self.ui.date_box.current(), 0)
        self.assertEqual(str(self.ui.date_box.cget("state")), "readonly")
        self.assertTrue(self.ui.show_date.get())

    def test_selecting_a_format_updates_the_preview_settings_and_the_store(self):
        self.ui.date_box.current(3)
        self.ui.select_date()
        self.assertEqual(self.ui.date_format.get(), "dmy")
        self.assertEqual(self.ui.collect().date_format, "dmy")
        self.assertEqual(self.store.settings().date_format, "mon_d_y")                            # nothing applied yet
        v = self.store.version
        self.ui.apply()
        self.assertEqual(self.store.settings().date_format, "dmy")
        self.assertEqual(self.store.version, v + 1)
        self.assertEqual(self.store.config()["date_fmt"], 3)

    def test_hiding_the_date_disables_the_dropdown_and_is_applied(self):
        self.ui.show_date.set(False)
        self.ui.toggle_date()
        self.assertEqual(str(self.ui.date_box.cget("state")), "disabled")
        self.assertFalse(self.ui.collect().show_date)
        self.ui.apply()
        self.assertFalse(self.store.settings().show_date)
        self.assertFalse(self.store.config()["date"])
        self.ui.show_date.set(True)
        self.ui.toggle_date()
        self.assertEqual(str(self.ui.date_box.cget("state")), "readonly")
        self.ui.apply()
        self.assertTrue(self.store.settings().show_date)

    def test_the_window_opens_with_the_stored_values(self):
        self.store.update(show_date=False, date_format="d_mon_y")
        import tkinter as tk
        from desk_media_companion.ui import SettingsWindow
        second = tk.Toplevel(self.root)                                                           # (one Tk root per process: images need it)
        window = SettingsWindow(self.store)
        window._today = lambda: self.today
        ui = window.attach(second)
        self.assertEqual(ui.date_box.current(), 1)
        self.assertEqual(str(ui.date_box.cget("state")), "disabled")
        self.assertFalse(ui.show_date.get())
        self.assertEqual(ui.collect().date_format, "d_mon_y")

    def test_the_entries_follow_the_day_while_the_window_stays_open(self):
        self.ui.date_box.current(2)
        self.ui.select_date()
        self.today = dt.date(2026, 10, 14)
        self.ui.poll()
        self.assertEqual([e.split("  (")[0] for e in self.entries()], ["Oct 14, 2026", "14 Oct 2026", "10/14/2026", "14/10/2026"])
        self.assertEqual(self.ui.date_box.current(), 2)                                           # the choice is kept
        self.assertEqual(self.ui.date_format.get(), "mdy")

    def test_reset_restores_the_date_settings(self):
        self.store.update(show_date=False, date_format="dmy")
        self.ui.show_date.set(False)
        self.ui.date_box.current(3)
        self.ui.select_date()
        with mock.patch("tkinter.messagebox.askyesno", return_value=True):
            self.ui.reset()
        self.assertEqual((self.store.settings().show_date, self.store.settings().date_format), (True, "mon_d_y"))
        self.assertTrue(self.ui.show_date.get())
        self.assertEqual(self.ui.date_box.current(), 0)
        self.assertEqual(str(self.ui.date_box.cget("state")), "readonly")

    def test_the_live_preview_is_redrawn_with_the_date(self):
        with Recorder(draw=False) as rec:
            self.ui.date_box.current(2)
            self.ui.select_date()                                                                 # asks for a redraw in 120 ms
            deadline = time.time() + 3
            while time.time() < deadline and not any(len(t) == 10 and t[2] == "/" for t in rec.texts()):
                self.root.update()
                time.sleep(0.02)
        today = dt.date.today()
        self.assertIn(idle.format_date(today.year, today.month, today.day, "mdy"), rec.texts())


if __name__ == "__main__":
    unittest.main()

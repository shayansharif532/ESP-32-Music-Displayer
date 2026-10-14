#!/usr/bin/env python3
"""Generates companion/tests/data/idle_date_vectors.json: test vectors for the idle screen's date, shared by the companion's unit
tests and the firmware's self-tests.

Each vector says: the display was told `clock_ms` (the PC's LOCAL wall-clock time as milliseconds since 1970-01-01 00:00, i.e. UTC
plus the PC's UTC offset), and it must show this calendar day in each of the four formats. `utc_offset_min` is the offset that was
baked into `clock_ms` (so the UTC instant is `clock_ms - utc_offset_min * 60000`) or null when the vector is about the calendar
only. The expected values come from Python's `datetime` / `calendar` and from the tables below, NOT from the companion's own
date helpers, so the helpers are really checked against something else.

    python companion/tools/make_date_vectors.py           # (re)write the file
    python companion/tools/make_date_vectors.py --check   # exit 1 when the committed file is not what this script produces

The output is plain and deterministic (one vector per line, no timestamps, "\\n" line ends).
"""
import argparse
import calendar
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(HERE, "..", "tests", "data", "idle_date_vectors.json")

# English, fixed: calendar.month_abbr and strftime("%b") follow the system language, which the display must never do.
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
EPOCH = dt.datetime(1970, 1, 1)


def vector(local: dt.datetime, offset_min=None) -> dict:
    """The vector for the local wall-clock moment `local` (a naive datetime = what the PC's clock shows)."""
    delta = local - EPOCH                                             # exact integer arithmetic, no time zone involved
    clock_ms = (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000
    # a second, independent derivation of the same number (calendar.timegm reads a time tuple as if it were UTC)
    assert clock_ms == calendar.timegm(local.timetuple()) * 1000 + local.microsecond // 1000, local
    day = local.date()
    assert 1 <= day.day <= calendar.monthrange(day.year, day.month)[1]
    return {
        "clock_ms": clock_ms,
        "utc_offset_min": offset_min,
        "year": day.year, "month": day.month, "day": day.day,
        "formats": {
            "mon_d_y": f"{MONTHS[day.month - 1]} {day.day}, {day.year}",
            "d_mon_y": f"{day.day} {MONTHS[day.month - 1]} {day.year}",
            "mdy": day.strftime("%m/%d/%Y"),
            "dmy": day.strftime("%d/%m/%Y"),
        },
    }


def build_vectors() -> list[dict]:
    cases: list[tuple[dt.datetime, object]] = []

    def add(y, mo, d, h=0, mi=0, s=0, ms=0, off=None) -> None:
        cases.append((dt.datetime(y, mo, d, h, mi, s, ms * 1000), off))

    # the epoch and the days just before it (negative clock_ms: the day must be floored, not truncated)
    add(1970, 1, 1)
    add(1970, 1, 1, 23, 59, 59, 999)
    add(1970, 1, 2)
    add(1969, 12, 31, 23, 59, 59)
    add(1969, 12, 31, 23, 59, 59, 999)
    add(1969, 12, 31)
    add(1960, 2, 29, 12)
    add(1900, 1, 1)
    add(1900, 2, 28, 23, 59, 59, 999)                                 # 1900 is not a leap year
    add(1900, 3, 1)

    # leap days: every 4th year, but not 1900 / 2100, and again 2000 / 2400
    add(2000, 2, 28, 23, 59, 59, 999)
    add(2000, 2, 29)
    add(2000, 2, 29, 23, 59, 59, 999)
    add(2000, 3, 1)
    add(2024, 2, 28, 23, 59, 59, 999)
    add(2024, 2, 29)
    add(2024, 2, 29, 12, 30)
    add(2024, 2, 29, 23, 59, 59, 999)
    add(2024, 3, 1)
    add(2100, 2, 28)
    add(2100, 2, 28, 23, 59, 59, 999)                                 # 2100 is not a leap year: the next day is 1 March
    add(2100, 3, 1)
    add(2400, 2, 29)

    # one 2026 year end and every month end of 2026 (last millisecond of the month, then the first one of the next)
    add(2025, 12, 31, 23, 59, 59, 999)
    add(2026, 1, 1)
    for month in range(1, 13):
        add(2026, month, calendar.monthrange(2026, month)[1], 23, 59, 59, 999)
        if month < 12:
            add(2026, month + 1, 1)
    add(2027, 1, 1)
    add(2028, 2, 28, 23, 59, 59, 999)
    add(2028, 2, 29)
    add(2028, 3, 1)

    # single-digit days and months, today's date, the widest strings
    add(2027, 1, 5, 9, 5)
    add(2026, 3, 4, 8)
    add(2026, 9, 9, 9, 9, 9)
    add(2026, 10, 13, 12)
    add(2026, 9, 30, 17)
    add(2026, 12, 8)
    add(2026, 11, 29)

    # past the 32-bit limits (2038-01-19 03:14:07 and 2106-02-07 06:28:15 are 2**31 - 1 and 2**32 - 1 seconds), far in the future
    add(2037, 12, 31, 23, 59, 59, 999)
    add(2038, 1, 19, 3, 14, 7)
    add(2038, 1, 19, 3, 14, 8)
    add(2038, 1, 19, 23, 59, 59, 999)
    add(2106, 2, 6, 23, 59, 59, 999)
    add(2106, 2, 7, 6, 28, 15)
    add(2106, 2, 7, 6, 28, 16)
    add(2106, 12, 31, 23, 59, 59, 999)
    add(9999, 12, 31, 23, 59, 59, 999)

    # odd offsets: the local date is NOT the UTC date at these moments
    add(2026, 10, 13, 0, 0, 0, off=345)                               # +05:45 (Nepal): UTC is still 12 October, 18:15
    add(2026, 10, 13, 5, 44, 59, 999, off=345)
    add(2026, 10, 13, 3, 29, 59, 999, off=330)                        # +05:30 (India): UTC is 12 October, 21:59
    add(2026, 10, 13, 0, 0, 0, off=765)                               # +12:45 (Chatham Islands)
    add(2026, 10, 14, 0, 10, 0, off=840)                              # +14:00 (Line Islands): UTC is 13 October, 10:10
    add(2026, 12, 31, 23, 59, 59, 999, off=840)
    add(2026, 10, 13, 23, 30, 0, off=-570)                            # -09:30 (Marquesas): UTC is already 14 October, 09:00
    add(2026, 10, 13, 23, 59, 59, 999, off=-570)
    add(2026, 10, 14, 0, 0, 0, off=-570)
    add(2026, 1, 1, 0, 0, 0, off=-720)                                # -12:00: UTC is 1 January, 12:00
    add(2026, 12, 31, 23, 59, 59, 999, off=-720)                      # UTC is already 2027
    add(2026, 1, 1, 0, 0, 0, off=-210)                                # -03:30 (Newfoundland)
    add(2026, 10, 13, 0, 0, 0, off=0)
    add(2026, 10, 13, 23, 59, 59, 999, off=0)

    # daylight-saving changes: the wall clock jumps, `clock_ms` jumps with it, the date follows the wall clock
    add(2026, 3, 8, 1, 59, 59, off=-300)                              # US Eastern, 8 March 2026: 02:00 EST -> 03:00 EDT
    add(2026, 3, 8, 3, 0, 0, off=-240)
    add(2026, 3, 8, 23, 59, 59, 999, off=-240)
    add(2026, 3, 9, 0, 0, 0, off=-240)
    add(2026, 11, 1, 0, 30, 0, off=-240)                              # US Eastern, 1 November 2026: 02:00 EDT -> 01:00 EST
    add(2026, 11, 1, 1, 30, 0, off=-240)                              # (the hour 01:00-02:00 happens twice)
    add(2026, 11, 1, 1, 30, 0, off=-300)
    add(2026, 11, 1, 23, 59, 59, 999, off=-300)
    add(2026, 11, 2, 0, 0, 0, off=-300)
    add(2026, 3, 29, 1, 59, 59, off=60)                               # Central Europe, 29 March 2026: 02:00 CET -> 03:00 CEST
    add(2026, 3, 29, 3, 0, 0, off=120)
    add(2026, 10, 25, 2, 30, 0, off=120)                              # 25 October 2026: 03:00 CEST -> 02:00 CET
    add(2026, 10, 25, 2, 30, 0, off=60)
    add(2026, 10, 25, 23, 59, 59, 999, off=60)
    add(2026, 10, 26, 0, 0, 0, off=60)
    add(2026, 4, 5, 2, 30, 0, off=660)                                # Sydney, 5 April 2026: 03:00 AEDT -> 02:00 AEST
    add(2026, 4, 5, 2, 30, 0, off=600)
    add(2026, 10, 4, 1, 59, 59, off=600)                              # 4 October 2026: 02:00 AEST -> 03:00 AEDT
    add(2026, 10, 4, 3, 0, 0, off=660)
    add(2026, 10, 4, 1, 59, 59, off=630)                              # Lord Howe Island: UTC+10:30, 30-minute DST: 02:00 -> 02:30
    add(2026, 10, 4, 2, 30, 0, off=660)

    vectors = [vector(local, off) for local, off in cases]
    seen = set()
    for v in vectors:                                                 # a duplicated (clock_ms, offset) pair is a typo, not a test
        key = (v["clock_ms"], v["utc_offset_min"])
        assert key not in seen, key
        seen.add(key)
    return vectors


def render(vectors: list[dict]) -> str:
    return "[\n" + ",\n".join("  " + json.dumps(v, separators=(", ", ": ")) for v in vectors) + "\n]\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--check", action="store_true", help="do not write: fail when the file differs from what this script produces")
    args = ap.parse_args()
    vectors = build_vectors()
    out = os.path.abspath(args.out)
    if args.check:
        try:
            with open(out, encoding="utf-8") as f:
                current = json.load(f)
        except (OSError, ValueError) as exc:
            print(f"{out}: unreadable ({exc})")
            return 1
        if current != vectors:
            print(f"{out} is out of date: run companion/tools/make_date_vectors.py")
            return 1
        print(f"{out}: {len(vectors)} vectors, up to date")
        return 0
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(render(vectors))
    print(f"wrote {out}: {len(vectors)} vectors")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""The idle screen's date: the four display formats and the calendar arithmetic behind them. Pure Python: no Windows API, no locale.

The display has no calendar of its own. The companion sends `clock_ms` (the PC's LOCAL wall-clock time as milliseconds since
1970-01-01 00:00, i.e. UTC plus the PC's UTC offset at that moment) and the display counts on from it with its own timer. So the
date is simply "which calendar day is floor(clock_ms / 86_400_000) days after 1970-01-01", with no time zone, daylight-saving rule
or locale involved: an offset such as +5:30, +5:45 or -9:30 is already inside the number, and the date rolls over at local
midnight by itself.

The firmware computes it with the same integer algorithm (`civil_from_days`, Howard Hinnant's public-domain "chrono-compatible
low-level date algorithms") and prints the same English month names, so the settings window's preview and the device show the
same text. Never use strftime here: `%b` follows the system language.
"""
from __future__ import annotations

DATE_FORMATS = ("mon_d_y", "d_mon_y", "mdy", "dmy")        # the numbering is also the wire / NVS one: 0 .. 3
DATE_FORMAT_DEFAULT = "mon_d_y"
DATE_FORMAT_LABELS = {                                     # what each format looks like, for the settings window
    "mon_d_y": "Mon D, YYYY", "d_mon_y": "D Mon YYYY", "mdy": "MM/DD/YYYY", "dmy": "DD/MM/YYYY",
}
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
MS_PER_DAY = 86_400_000
YEAR_MIN, YEAR_MAX = 0, 9999                                # what format_date accepts (4 digits at most)


def date_format_index(fmt: object) -> int:
    """The number of a format on the wire (`date_fmt` of GET /idle); an unknown name counts as the default."""
    return DATE_FORMATS.index(fmt) if fmt in DATE_FORMATS else DATE_FORMATS.index(DATE_FORMAT_DEFAULT)


def civil_from_days(days: int) -> tuple[int, int, int]:
    """(year, month, day) of the proleptic Gregorian calendar day `days` after 1970-01-01 (negative = before).

    Howard Hinnant's algorithm: 400-year "eras" of 146097 days, the year counted from 1 March so the leap day is the last day of
    its year. C truncates integer division towards zero and needs an adjustment for negative numbers; Python's `//` already
    floors, so the line for the era is the one place where this differs from the firmware's text."""
    z = days + 719468                                       # days since 0000-03-01
    era = z // 146097
    doe = z - era * 146097                                  # day of era, 0 .. 146096
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365      # year of era, 0 .. 399
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)         # day of year from 1 March, 0 .. 365
    mp = (5 * doy + 2) // 153                               # month from March, 0 .. 11
    day = doy - (153 * mp + 2) // 5 + 1
    month = mp + 3 if mp < 10 else mp - 9
    year = yoe + era * 400 + (1 if month <= 2 else 0)
    return year, month, day


def civil_from_clock_ms(clock_ms: int) -> tuple[int, int, int]:
    """(year, month, day) of a LOCAL `clock_ms` (milliseconds since 1970-01-01 00:00 local time; may be negative)."""
    return civil_from_days(int(clock_ms) // MS_PER_DAY)


def format_date(year: int, month: int, day: int, fmt: str = DATE_FORMAT_DEFAULT) -> str:
    """The date as the display prints it. English month names, never localised:
    mon_d_y `Oct 13, 2026` / `Jan 5, 2027`, d_mon_y `13 Oct 2026` / `5 Jan 2027`, mdy `10/13/2026` / `01/05/2027`,
    dmy `13/10/2026` / `05/01/2027`. Raises ValueError for an unknown format or a field that cannot be part of a date."""
    if fmt not in DATE_FORMATS:
        raise ValueError(f"unknown date format {fmt!r}")
    if not (YEAR_MIN <= year <= YEAR_MAX and 1 <= month <= 12 and 1 <= day <= 31):
        raise ValueError(f"not a date: {year}-{month}-{day}")
    name = MONTHS[month - 1]
    if fmt == "mon_d_y":
        return f"{name} {day}, {year}"
    if fmt == "d_mon_y":
        return f"{day} {name} {year}"
    if fmt == "mdy":
        return f"{month:02d}/{day:02d}/{year:04d}"
    return f"{day:02d}/{month:02d}/{year:04d}"


def format_clock_ms(clock_ms: int, fmt: str = DATE_FORMAT_DEFAULT) -> str:
    """The date of a local `clock_ms`, formatted."""
    return format_date(*civil_from_clock_ms(clock_ms), fmt)


def date_format_choices(year: int, month: int, day: int) -> list[tuple[str, str]]:
    """(format, text for a drop-down entry) for each format: the given date as that format prints it, then the pattern, because a
    numeric date such as 05/10/2026 is ambiguous on its own."""
    return [(fmt, f"{format_date(year, month, day, fmt)}  ({DATE_FORMAT_LABELS[fmt]})") for fmt in DATE_FORMATS]

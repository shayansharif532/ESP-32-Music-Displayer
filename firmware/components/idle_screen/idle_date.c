/* The idle screen's date: see idle_date.h. */
#include "idle_date.h"

#include <stdio.h>
#include <string.h>

static const char *const MONTHS[12] = { "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec" };   /* fixed English, never locale */

static int64_t floor_div(int64_t a, int64_t b)
{
    int64_t q = a / b;
    return (a % b != 0 && ((a < 0) != (b < 0))) ? q - 1 : q;
}

void idle_civil_from_clock_ms(int64_t clock_ms, int *year, int *month, int *day)
{
    int64_t z = floor_div(clock_ms, 86400000) + 719468;            /* days since 0000-03-01 */
    const int64_t era = floor_div(z, 146097);
    const int64_t doe = z - era * 146097;                           /* [0, 146096] */
    const int64_t yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    const int64_t y = yoe + era * 400;
    const int64_t doy = doe - (365 * yoe + yoe / 4 - yoe / 100);    /* [0, 365] */
    const int64_t mp = (5 * doy + 2) / 153;                         /* [0, 11], March = 0 */
    const int d = (int)(doy - (153 * mp + 2) / 5 + 1);
    const int m = (int)(mp < 10 ? mp + 3 : mp - 9);
    *year = (int)(y + (m <= 2));
    *month = m;
    *day = d;
}

size_t idle_format_date(char *out, size_t cap, int year, int month, int day, int fmt)
{
    if (!out || cap == 0 || month < 1 || month > 12 || day < 1 || day > 31) {
        return 0;
    }
    int n;
    switch (fmt) {
    case IDLE_DATE_FMT_MON_D_Y:
        n = snprintf(out, cap, "%s %d, %d", MONTHS[month - 1], day, year);
        break;
    case IDLE_DATE_FMT_D_MON_Y:
        n = snprintf(out, cap, "%d %s %d", day, MONTHS[month - 1], year);
        break;
    case IDLE_DATE_FMT_MDY:
        n = snprintf(out, cap, "%02d/%02d/%04d", month, day, year);
        break;
    case IDLE_DATE_FMT_DMY:
        n = snprintf(out, cap, "%02d/%02d/%04d", day, month, year);
        break;
    default:
        out[0] = '\0';
        return 0;
    }
    if (n <= 0 || (size_t)n >= cap) {
        out[0] = '\0';
        return 0;
    }
    return (size_t)n;
}

size_t idle_format_clock_date(char *out, size_t cap, int64_t clock_ms, int fmt)
{
    if (!out || cap == 0) {
        return 0;
    }
    out[0] = '\0';
    if (clock_ms <= 0) {
        return 0;
    }
    int y, m, d;
    idle_civil_from_clock_ms(clock_ms, &y, &m, &d);
    return idle_format_date(out, cap, y, m, d, fmt);
}

/* ---- self-test ---------------------------------------------------------------------------------------------------------------- */

typedef struct {
    int64_t clock_ms;
    int year, month, day;
    const char *fmt[IDLE_DATE_FMT_COUNT];
} vec_t;

#include "idle_date_vectors.inc"          /* generated from companion/tests/data/idle_date_vectors.json by tools/gen_date_vectors.py */

const char *idle_date_selftest(void)
{
    char buf[24];
    for (size_t i = 0; i < sizeof VECTORS / sizeof VECTORS[0]; i++) {              /* all shared vectors, negative values included */
        int vy, vm, vd;
        idle_civil_from_clock_ms(VECTORS[i].clock_ms, &vy, &vm, &vd);
        if (vy != VECTORS[i].year || vm != VECTORS[i].month || vd != VECTORS[i].day) {
            return "civil date vector";
        }
        for (int f = 0; f < IDLE_DATE_FMT_COUNT; f++) {
            if (idle_format_date(buf, sizeof buf, vy, vm, vd, f) == 0 || strcmp(buf, VECTORS[i].fmt[f]) != 0) {
                return "date format vector";
            }
        }
    }
    int y, m, d;
    idle_civil_from_clock_ms(-1, &y, &m, &d);                      /* 1969-12-31 23:59:59.999 */
    if (y != 1969 || m != 12 || d != 31) return "negative clock";
    idle_civil_from_clock_ms(951782400000LL, &y, &m, &d);          /* 2000-02-29 00:00:00 */
    if (y != 2000 || m != 2 || d != 29) return "leap day 2000";
    idle_civil_from_clock_ms(4107542400000LL - 1, &y, &m, &d);     /* 2100-02-28 23:59:59.999 (2100 is not a leap year) */
    if (y != 2100 || m != 2 || d != 28) return "2100-02-28";
    idle_civil_from_clock_ms(4107542400000LL, &y, &m, &d);         /* the next millisecond is 2100-03-01 */
    if (y != 2100 || m != 3 || d != 1) return "2100-03-01";
    if (idle_format_date(buf, 8, 2026, 10, 13, IDLE_DATE_FMT_MON_D_Y) != 0) return "small buffer";
    if (idle_format_date(buf, sizeof buf, 2026, 13, 1, IDLE_DATE_FMT_MDY) != 0) return "bad month";
    if (idle_format_date(buf, sizeof buf, 2026, 10, 13, 9) != 0) return "bad format";
    return NULL;
}

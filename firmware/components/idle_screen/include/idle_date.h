#pragma once
/*
 * The idle screen's date (docs/idle-screen.md): the PC's LOCAL calendar date, derived from the same millisecond value that drives the
 * clock (`clock_ms` = UTC milliseconds + the PC's local UTC offset, so daylight-saving changes and odd offsets need nothing here).
 * Pure C, no ESP-IDF dependency; the companion's idle.py implements the same functions and both are checked against the shared test
 * vectors (companion/tests/data/idle_date_vectors.json).
 */
#include <stddef.h>
#include <stdint.h>

#define IDLE_DATE_FMT_MON_D_Y   0       /* "Oct 13, 2026"   (day without leading zero) */
#define IDLE_DATE_FMT_D_MON_Y   1       /* "13 Oct 2026" */
#define IDLE_DATE_FMT_MDY       2       /* "10/13/2026"     (zero padded) */
#define IDLE_DATE_FMT_DMY       3       /* "13/10/2026" */
#define IDLE_DATE_FMT_COUNT     4

/* Calendar date of the day that contains `clock_ms` (proleptic Gregorian, correct for negative values). Howard Hinnant's algorithm. */
void idle_civil_from_clock_ms(int64_t clock_ms, int *year, int *month, int *day);

/* "Oct 13, 2026" etc. into out (at most 16 bytes are needed). Returns the length, 0 for an unknown format or a buffer that is too small. */
size_t idle_format_date(char *out, size_t cap, int year, int month, int day, int fmt);

/* Both steps; "" when clock_ms <= 0 (the clock has not been synchronised yet). */
size_t idle_format_clock_date(char *out, size_t cap, int64_t clock_ms, int fmt);

/* On-target self-test against hand-checked and generated vectors; NULL when everything passes, else a description of the failure. */
const char *idle_date_selftest(void);

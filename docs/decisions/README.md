# Architecture decision records

One file per decision, in the order they were taken. Each record keeps its context, the options that were rejected and the
consequences that were accepted. Evidence (measurements, test runs) lives in the matching
[development-history](../development-history/README.md) entry.

| ADR | Title | Status | Date |
|---|---|---|---|
| [ADR-001](ADR-001-esp-idf-and-lvgl.md) | Framework: ESP-IDF 5.5.x + LVGL 9, not Arduino | Accepted | 2026-10-11 |
| [ADR-002](ADR-002-spotify-web-api-rules-obsolete.md) | Spotify Web API rules that shaped the first design | Obsolete (superseded by ADR-003) | 2026-10-11 |
| [ADR-003](ADR-003-windows-media-session-companion.md) | A Windows media-session companion replaces the Spotify Web API | Accepted (current) | 2026-10-12 |
| [ADR-004](ADR-004-per-app-volume-side-buttons-art-shape.md) | Per-app volume through Core Audio sessions, side buttons as volume keys, artwork kept in its own shape | Accepted (current) | 2026-10-12 |
| [ADR-005](ADR-005-source-switching-by-vertical-swipe.md) | Media-source switching by vertical swipe; every command names the displayed session | Accepted (the "pinned" rule was superseded by ADR-006) | 2026-10-12 |
| [ADR-006](ADR-006-temporary-manual-selection-and-spotify-options.md) | Manual source selection is temporary (AUTO / MANUAL_TEMP); Spotify shuffle / repeat through Windows' own session controls | Accepted (current) | 2026-10-12 |
| [ADR-007](ADR-007-customisable-idle-screen.md) | Customisable idle screen: the PC owns the configuration, the display caches it | Accepted (current) | 2026-10-12 |
| [ADR-008](ADR-008-idle-screen-as-source-ring-item.md) | The idle screen is a synthetic item of the vertical source ring | Accepted (current) | 2026-10-12 |
| [ADR-009](ADR-009-usb-first-transport.md) | USB (CDC-ACM on the native port) is the primary transport; the LAN stays an optional fallback | Accepted (current) | 2026-10-13 |

The pairing / authenticated-request protocol is specified in [../protocol.md](../protocol.md), its threat model in
[../security.md](../security.md).

The record numbering is historical: ADR-002 was written during the first investigation and is kept last in the original
single-file log, which is why ADR-001 and ADR-003 are not adjacent in time.

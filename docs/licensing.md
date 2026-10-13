# Licensing

**Status: the licence for this project's own code has not been chosen yet.** Until a `LICENSE` file is added, the original code is
"all rights reserved" by default. This page records what was found in a licence review and the recommended structure, so
the decision can be made with the facts in front of you. It is not legal advice.

What the project *redistributes or builds on* is documented separately and is already complete:
[`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md) (per-component licences and credit lines), [`LICENSES/`](../LICENSES/) (the licence
texts that have to travel with binaries) and [`firmware/NOTICE.md`](../firmware/NOTICE.md) (what is left of Waveshare's example).

## What is original and what is not

| Part | Origin | Licence |
|---|---|---|
| Windows companion (`companion/`), tools (`tools/`), documentation, firmware components `ui`, `pc_media`, `side_keys`, `wifi_mgr`, `idle_screen` (except the font), `main/` | written for this project | not yet chosen (see below) |
| Remnants of Waveshare's example (`firmware/main/idf_component.yml` verbatim; a few boilerplate lines of `CMakeLists.txt`, `partitions.csv`, `sdkconfig.defaults` section A) | Waveshare `02_lvgl_demo_v9` | Apache-2.0 (verified in Waveshare's repository `LICENSE.txt`); keeps its notice, modifications are stated in `firmware/NOTICE.md` |
| `firmware/components/idle_screen/font_clock.c` | bitmap subset of Montserrat Medium | **SIL OFL 1.1**: a derived font, it stays under the OFL (SPDX header in the file) |
| Dependencies downloaded at build time (LVGL MIT; Espressif components and Waveshare's board package Apache-2.0; ESP-IDF Apache-2.0) | upstream | not vendored; listed in `THIRD_PARTY_NOTICES.md` |
| Python packages inside the companion executable | PyPI | MIT / BSD / PSF / HPND; **pystray is LGPL-3.0-or-later** |

Nothing in the repository is copied from Waveshare's wiki, schematic, demo archive or factory firmware; those stay out
(`docs/waveshare/` and `*.bin` are git-ignored).

## Recommended structure

1. **One permissive licence for all original code: Apache-2.0** (alternative: MIT). The platform stack is Apache-2.0 (ESP-IDF, Waveshare, Espressif
   components) plus MIT (LVGL), so nothing conflicts; the Waveshare-derived lines are already Apache-2.0 and need no isolation or rewrite;
   Apache-2.0's explicit patent grant suits a project that links vendor hardware code; it is one-way compatible with GPL-3.0 / LGPL-3.0, which
   covers the pystray case. MIT is also safe if brevity matters more.
2. Files to add when the choice is made: root `LICENSE`, a short `NOTICE`, `SPDX-License-Identifier` headers in the original sources.
   `font_clock.c` keeps `OFL-1.1`. Documentation and screenshots can follow the same licence (CC-BY-4.0 for `docs/` is optional; no dual licensing by default).
3. **Binary releases are what trigger notice duties**: ship `THIRD_PARTY_NOTICES.md` and `LICENSES/` with any firmware image or companion executable.
4. **pystray (LGPL-3.0)** is used unmodified and only for the tray icon; the import is lazy and optional (`ui.py`). Options: (A) keep it and comply
   - notice + licence texts in the release, public source and `companion/build.ps1` so anyone can rebuild with another pystray (done in the notices);
   (B) ship without a tray icon; (C) replace it by about 100 lines of `ctypes` `Shell_NotifyIcon` code, which removes the LGPL dependency entirely.
   Recommendation: A now, C only if a licence-clean executable matters.
5. **Trademarks**: "Spotify", "Chrome", "YouTube", "Windows", "Waveshare", "Espressif", "ESP32" and "LVGL" are used only to say what the project works with, with a
   non-affiliation statement (see `THIRD_PARTY_NOTICES.md`). No logos or third-party artwork are in the repository; all screenshots use synthetic covers.
   Do a name search for "DeskMedia Display" before relying on it commercially.

## Open decisions for the maintainer

* The name for the copyright line, and Apache-2.0 vs MIT.
* Whether built binaries (firmware image, `DeskMediaCompanion.exe`) will be published, and which pystray option applies.
* Whether the obsolete Spotify Web API client that is no longer in the tree (kept in history) should ever be revived: Spotify's developer terms apply to it.
* A licence applies from the day it is added: copies taken earlier stay "all rights reserved".

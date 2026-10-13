#!/usr/bin/env python3
"""Generates companion/tests/data/idle_preview_boxes.json: where the settings preview puts every element of the idle screen when it
is rendered at the display's own size (480 x 480), for each clock / custom line / date combination.

The numbers are ink bounding boxes (left, top, right, bottom) in pixels, as `idle.render_preview(..., boxes=...)` reports them. The
test (tests/test_idle_date.py) renders the same settings again and compares within a tolerance of a few pixels, so a change of the
layout contract or of the font handling is noticed; regenerate the file only after changing the layout on purpose (and change the
firmware with it). Only numbers are stored: no pictures.

    python companion/tools/make_preview_boxes.py
"""
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from desk_media_companion import idle  # noqa: E402

DEFAULT_OUT = os.path.join(HERE, "..", "tests", "data", "idle_preview_boxes.json")
NOW = dt.datetime(2026, 10, 13, 14, 5)                               # the preview's clock: 14:05 / 2:05 PM; its date: Oct 13, 2026
TEXT = "Hello world"


def build() -> list[dict]:
    entries = []
    for clock in (True, False):
        for text in (True, False):
            for date in (True, False):
                for h24 in ((True, False) if clock else (True,)):
                    settings = idle.IdleSettings(theme="dark", show_clock=clock, clock_24h=h24, show_date=date, text=TEXT if text else "")
                    boxes: dict = {}
                    idle.render_preview(settings, None, idle.SCREEN, NOW, boxes)
                    entries.append({"clock": clock, "clock_24h": h24, "date": date, "text": text,
                                    "boxes": {name: list(box) for name, box in sorted(boxes.items())}})
    return entries


def main() -> int:
    out = os.path.abspath(DEFAULT_OUT)
    entries = build()
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write("[\n" + ",\n".join("  " + json.dumps(e, separators=(", ", ": ")) for e in entries) + "\n]\n")
    print(f"wrote {out}: {len(entries)} layouts")
    return 0


if __name__ == "__main__":
    sys.exit(main())

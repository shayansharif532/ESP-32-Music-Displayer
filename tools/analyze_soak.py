#!/usr/bin/env python3
"""Reads a serial log captured during a soak test (tools/serial_capture.py) and reports resets, panics and heap / stack drift.

    tools\\venv\\Scripts\\python.exe tools\\analyze_soak.py logs\\soak.log
"""
import re
import sys

BAD = re.compile(r"(Guru Meditation|abort\(\)|panic|Backtrace|rst:0x|ets Jul|stack overflow|assert failed|LoadProhibited|StoreProhibited|"
                 r"E \(\d+\) task_wdt|watchdog)", re.I)
HEALTH = re.compile(r"health: up=(\d+)s touches=(\d+) int_free=(\d+) int_min=(\d+) int_big=(\d+) psram_free=(\d+) lvgl_stack_free_min=(\d+)")


def main(path: str) -> int:
    text = open(path, encoding="utf-8", errors="ignore").read().splitlines()
    bad = [ln for ln in text if BAD.search(ln)]
    health = [tuple(int(v) for v in m.groups()) for m in (HEALTH.search(ln) for ln in text) if m]
    actions = sum(1 for ln in text if "dev_tools: debug action" in ln)
    sources = sum(1 for ln in text if "-> source " in ln)
    print(f"{len(text)} log lines, {actions} board-side actions, {sources} source switches applied")
    print(f"resets / panics / watchdogs: {len(bad)}")
    for ln in bad[:10]:
        print("   ", ln[:160])
    if health:
        ups = [h[0] for h in health]
        print(f"health lines: {len(health)}; uptime {ups[0]} -> {ups[-1]} s (monotonic: {ups == sorted(ups)})")
        for name, idx in (("internal heap free", 2), ("internal heap min ever", 3), ("largest internal block", 4), ("PSRAM free", 5), ("LVGL stack free min", 6)):
            vals = [h[idx] for h in health]
            print(f"   {name:24s} first {vals[0]:>8d}   last {vals[-1]:>8d}   min {min(vals):>8d}   max {max(vals):>8d}")
        print(f"touches counter {health[0][1]} -> {health[-1][1]}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))

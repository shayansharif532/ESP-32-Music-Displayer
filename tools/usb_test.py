#!/usr/bin/env python3
"""Board-in-the-loop test of the USB link as a PRODUCT: discovery, pairing once, and every way the connection can be interrupted.

    companion\\.venv\\Scripts\\python.exe tools\\usb_test.py [--port COM5] [--quick]

Needs: the board on its native USB port, flashed with a development build of this checkout (-DAPP_DEV_TOOLS=1 -DAPP_NO_WIFI=1: no Wi-Fi, so the
only way the display can reach the companion is the cable), no other program holding the COM port (the real companion must be stopped), nobody
using the display by hand. The companion under test runs in this process with a scratch data folder (never the real one) and sees only two
silent fake media sessions.

Checks:  1 the display is found without any port setting and pairs (the pairing window opens by itself), 2 a companion restart reconnects
without asking for a pairing again, 3 a reboot of the display does too, 4 a simulated unplug / replug of the cable (the display lets go of the
USB data lines, then restarts as a power-up does: for the PC this is what pulling the cable looks like) of 1.5 s, 3 s, 9 s and 20 s is
recovered by itself, with the same pairing, 5 forgetting the pairing on the PC makes the display ask again and the pairing window opens by itself, 6 data keeps flowing
afterwards (poll counters, artwork) and the display never reset except where this test rebooted it.
"""
import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import idle_test as it  # noqa: E402
import usb_harness as uh  # noqa: E402

import fakes  # noqa: E402

check, wait_for, LOGS = it.check, it.wait_for, it.LOGS
RESET_RX = r"ESP-ROM|rst:0x|Guru Meditation|abort\(\)|Backtrace"


def link_counters(comp) -> dict:
    return dict(comp.runtime.usb_status().get("link", {}))


def served(comp) -> bool:
    """True when requests of the display are being answered: the request counter moves."""
    before = link_counters(comp).get("requests", 0)
    time.sleep(3.5)
    return link_counters(comp).get("requests", 0) > before


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="auto", help="only probe this COM port (default: discover it)")
    ap.add_argument("--quick", action="store_true", help="skip the 20 s detach")
    args = ap.parse_args()
    os.makedirs(LOGS, exist_ok=True)
    kept = os.environ.get("DM_TEST_DATA_DIR", "")             # keep the pairing between runs (never the real data folder)
    data_dir = kept or tempfile.mkdtemp(prefix="usb-test-")
    os.makedirs(data_dir, exist_ok=True)
    procs: dict = {}
    board = uh.UsbBoard()
    comp = uh.UsbCompanion(os.path.join(data_dir, "idle"), board, usb_port="" if args.port == "auto" else args.port)
    events = comp.events
    results: dict = {}
    try:
        print("== 1. discovery and the first pairing")
        t0 = time.time()
        found = wait_for(lambda: comp.usb_state() in ("connected", "unpaired", "pairing", "probing"), 40.0, 0.2)
        check("1 the display is found on its own (no port given)", found, f"state {comp.usb_state()}, ports {comp.runtime.usb_status().get('ports')}")
        pair_requests = [e for e in events if e.get("type") == "pair-request"]
        check("1 an unpaired display makes the companion open the pairing window by itself", bool(pair_requests) or comp.pairing.window_open or comp.pairing.paired)
        ok = uh.ensure_paired(board, comp)
        check("1 the display pairs (code on both ends, confirm on the PC, tap on the display) and the session opens", ok)
        results["time_to_paired_s"] = round(time.time() - t0, 1)
        if not ok:
            return 2
        procs["A"] = fakes.start_fake(it.A, paused=False, log_path=os.path.join(LOGS, "usb-fake-A.log"))
        comp.monitor.only_apps = {it.A, it.B}
        check("1 the display shows what is playing (requests are answered)", wait_for(lambda: board.find(r"pc_media: now (playing|paused)") is not None, 20.0) and served(comp))
        resets0 = board.count(RESET_RX)

        print("\n== 2. the companion restarts: reconnect, no pairing again")
        mark = board.mark()
        comp.stop_http()
        time.sleep(3.0)
        t0 = time.time()
        comp.start_http()
        ok = wait_for(lambda: comp.usb_state() == "connected", 30.0, 0.1)
        results["companion_restart_reconnect_s"] = round(time.time() - t0, 1)
        check("2 reconnected by itself", ok, f"{results['companion_restart_reconnect_s']} s")
        check("2 the same pairing was used (the display was not asked to pair)", comp.pairing.paired and board.count(r"pairing code shown", mark) == 0)
        check("2 the display did not reset", board.count(RESET_RX, mark) == 0)

        print("\n== 3. the display reboots")
        mark = board.mark()
        t0 = time.time()
        board.reset()
        ok = wait_for(lambda: comp.usb_state() == "connected" and board.find(r"pc_media: status 2", mark) is not None, 60.0, 0.2)
        results["board_reboot_reconnect_s"] = round(time.time() - t0, 1)
        check("3 reconnected by itself after the reboot", ok, f"{results['board_reboot_reconnect_s']} s")
        check("3 without a pairing prompt", board.count(r"pairing code shown", mark) == 0 and comp.pairing.paired)
        resets0 = board.count(RESET_RX)

        print("\n== 4. the cable is pulled and plugged back in (simulated: the display lets go of the USB data lines, then restarts as a power-up does)")
        for ms in (1500, 3000, 9000) + (() if args.quick else (20000,)):
            mark = board.mark()
            comp.act(f"usb_replug_{ms}", gap=0.0)
            went = wait_for(lambda: comp.usb_state() != "connected", 15.0, 0.1)
            t_gone = time.time()
            check(f"4 {ms / 1000:g} s unplugged: the companion notices", went, f"state {comp.usb_state()}")
            back = wait_for(lambda: comp.usb_state() == "connected", ms / 1000 + 45.0, 0.1)
            results[f"replug_{ms}_back_after_s"] = round(time.time() - t_gone, 1)
            check(f"4 {ms / 1000:g} s unplugged: the link is back by itself, same pairing", back and comp.pairing.paired, f"{results[f'replug_{ms}_back_after_s']} s after it went away")
            check(f"4 {ms / 1000:g} s unplugged: no pairing prompt", board.count(r"pairing code shown", mark) == 0)
            check(f"4 {ms / 1000:g} s unplugged: the display is served again", served(comp))
            time.sleep(2.0)
        resets0 = board.count(RESET_RX)

        print("\n== 5. the pairing is forgotten on the PC: the display asks again, the window opens by itself")
        mark = board.mark()
        n_events = len(events)
        comp.pairing.forget()
        ok = wait_for(lambda: board.find(r"pairing lost|status 7", mark) is not None, 30.0, 0.2)
        check("5 the display says the pairing is lost", ok)
        ok = wait_for(lambda: any(e.get("type") == "pair-request" for e in events[n_events:]) or comp.pairing.window_open, 30.0, 0.2)
        check("5 the companion opens the pairing window by itself", ok)
        check("5 and the display pairs again", uh.ensure_paired(board, comp))
        check("5 and is served", served(comp))

        print("\n== 6. health")
        counters = link_counters(comp)
        bad = {k: v for k, v in counters.items() if k.startswith("parser_") and k not in ("parser_frames",) and v}
        check("6 no malformed frames on the cable in normal operation", not bad, str(bad))
        health = [m for _t, m in board.since(0) if "health:" in m]
        if len(health) >= 2:
            def field(line, name):
                m = re.search(name + r"=(\d+)", line)
                return int(m.group(1)) if m else 0
            check("6 internal heap steady", field(health[-1], "int_free") >= field(health[0], "int_free") - 4096, f"{field(health[0], 'int_free')} -> {field(health[-1], 'int_free')}")
            check("6 PSRAM did not leak", field(health[-1], "psram_free") >= field(health[0], "psram_free") - 131072, f"{field(health[0], 'psram_free')} -> {field(health[-1], 'psram_free')}")
            check("6 LVGL stack headroom healthy", field(health[-1], "lvgl_stack_free_min") >= 8000, str(field(health[-1], "lvgl_stack_free_min")))
        results["link_counters"] = counters
    finally:
        for p in procs.values():
            fakes.stop_fake(p)
        try:
            comp.stop_http()
        except Exception:
            pass
        board.stop = True
        with open(os.path.join(LOGS, "usb-test-serial.log"), "w", encoding="utf-8") as f:
            for t, ln in board.lines:
                if not re.match(r"^S\d+ ", ln):
                    f.write(f"{t:.2f} {ln}\n")
        if not kept:
            shutil.rmtree(data_dir, ignore_errors=True)
    print(f"\n{len(it.failures)} failure(s)" + (": " + "; ".join(it.failures) if it.failures else ""))
    print(json.dumps(results, indent=1))
    with open(os.path.join(LOGS, "usb-test-result.json"), "w", encoding="utf-8") as f:
        json.dump({"checks": it.results, "measurements": results}, f, indent=1)
    return 1 if it.failures else 0


if __name__ == "__main__":
    sys.exit(main())

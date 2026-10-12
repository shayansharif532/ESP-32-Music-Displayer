#!/usr/bin/env python3
"""Passive serial capture for the ESP32-S3's native USB-Serial/JTAG console.

Why not `idf.py monitor`?  It needs an interactive terminal.  This script only reads and writes a
log file, so it works from automation and can run in the background while someone taps the screen.

USB-Serial/JTAG line behaviour (observed on this board, matches esptool's reset code):
  * the console only streams while the host asserts DTR;
  * a RTS rising edge resets the chip, and the DTR level at that edge latches GPIO0
    (DTR=1 -> ROM download mode, DTR=0 -> normal boot).
So: while listening keep RTS low and DTR high; for a clean app reset use DTR=0, RTS 0->1->0.
pyserial's default of DTR=1/RTS=1 on open would produce a RTS edge with DTR high, so we never use it.

    python tools/serial_capture.py --port COM5 --seconds 20 --reset
    python tools/serial_capture.py --port COM5 --seconds 600 --out build/serial.log    (background)
"""
import argparse
import datetime
import re
import sys
import time

import serial

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# Safety net: never let anything that looks like a credential reach a log file or the terminal.
SECRET = re.compile(r"(pass(word)?|pwd|token|secret|psk|bearer|authorization|refresh_token|access_token|client_secret|code_verifier)(\s*[=:]\s*|\"\s*:\s*\")\S+", re.I)


def set_dtr(ser: serial.Serial, state: bool) -> None:
    ser.dtr = state
    ser.rts = ser.rts          # Windows usbser.sys only sends a control-line update when the *other* line is written too


def set_rts(ser: serial.Serial, state: bool) -> None:
    ser.rts = state
    ser.dtr = ser.dtr          # same workaround as esptool's reset code ("dummy change to DTR")


def open_port(port: str, reset: bool) -> serial.Serial:
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = 115200
    ser.timeout = 0.1
    ser.dtr = False
    ser.rts = False
    ser.open()
    if reset:
        set_dtr(ser, False)    # normal boot, not download mode
        set_rts(ser, False)
        time.sleep(0.1)
        set_rts(ser, True)     # RTS rising edge -> chip reset
        time.sleep(0.1)
        set_rts(ser, False)    # release: app boots
        time.sleep(0.05)
    set_dtr(ser, True)         # start streaming; RTS stays low so no further reset edge
    return ser


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="COM5")
    ap.add_argument("--seconds", type=float, default=15.0, help="how long to listen")
    ap.add_argument("--reset", action="store_true", help="reset into the app first (normal boot)")
    ap.add_argument("--out", help="append every line to this file as well")
    ap.add_argument("--until", help="stop early once a line matches this regex (exit 2 if it never does)")
    ap.add_argument("--max-lines", type=int, default=300, help="max lines echoed to stdout (the file gets everything)")
    args = ap.parse_args()

    until = re.compile(args.until) if args.until else None
    fout = open(args.out, "a", encoding="utf-8", buffering=1) if args.out else None
    t0 = time.time()
    deadline = t0 + args.seconds
    echoed = 0
    matched = False
    pending = b""
    ser = None
    reset = args.reset

    def emit(line: str) -> None:
        nonlocal echoed, matched
        line = SECRET.sub(lambda m: m.group(1) + m.group(3) + "<masked>", ANSI.sub("", line)).rstrip("\r")
        if not line.strip():
            return
        stamped = f"[+{time.time() - t0:7.2f}s] {line}"
        if fout:
            fout.write(stamped + "\n")
        if echoed < args.max_lines:
            print(stamped, flush=True)
            echoed += 1
            if echoed == args.max_lines:
                print("... (further lines only in the log file)", flush=True)
        if until and until.search(line):
            matched = True

    while time.time() < deadline and not matched:
        if ser is None:
            try:
                ser = open_port(args.port, reset)
                reset = False                      # only the first open may reset
                emit(f"[host] listening on {args.port}" + (" after reset" if args.reset and echoed == 0 else ""))
            except serial.SerialException as exc:
                time.sleep(0.3)                    # port may be re-enumerating after a crash/reset
                if time.time() >= deadline:
                    print(f"[host] could not open {args.port}: {exc}", file=sys.stderr)
                    return 3
                continue
        try:
            chunk = ser.read(4096)
        except (serial.SerialException, OSError):
            emit("[host] port dropped (device re-enumerating?) - reopening")
            try:
                ser.close()
            except Exception:
                pass
            ser = None
            continue
        if chunk:
            pending += chunk
            *lines, pending = pending.split(b"\n")
            for raw in lines:
                emit(raw.decode("utf-8", errors="replace"))

    if pending.strip():
        emit(pending.decode("utf-8", errors="replace"))
    if ser is not None:
        try:
            ser.close()
        except Exception:
            pass
    if fout:
        fout.close()
    if until and not matched:
        print(f"[host] pattern {args.until!r} not seen within {args.seconds:.0f} s", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

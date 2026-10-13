"""Board-in-the-loop test of pairing and request authentication (docs/protocol.md, docs/security.md).

    companion\\.venv\\Scripts\\python.exe tools\\pairing_test.py [--port COM5]

Needs: a board running a development firmware build (-DAPP_DEV_TOOLS=1), the normal companion STOPPED (this test brings its own on port 8765,
on the default pairing store, so the board and the normal companion share one pairing afterwards) and no one touching the display.
The test pairs, un-pairs and re-pairs the board several times; it ends with the board paired.

What it proves, against the real firmware and the real network stack (LAN address of this PC, not loopback):
  unpaired LAN clients are refused everywhere except /hello and /health; the display shows the code prompt only while the pairing window
  is open; Cancel on the display (a real tap through the touch path) pairs nothing; Pair + the click on the PC pairs; signed requests of the
  display work, a replayed / tampered / wrongly-keyed request does not; "Forget" on the PC makes the display say "Pairing lost" and the
  old key stops working; re-pairing replaces the key; the pairing survives a reboot of the display; a rogue beacon with another companion
  id is ignored; a rogue server that spoofs the beacon of the paired companion cannot feed the display anything (answers without a valid
  proof are discarded, an oversized answer does not crash it); the real companion is picked up again afterwards; no reset except the
  deliberate one.
"""
import argparse
import http.server
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import shutil

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import idle_test as it  # noqa: E402  (Board, Companion, check, wait_for: the same harness the idle-screen test uses)
import screenshot as shot  # noqa: E402
import serial_capture as sc  # noqa: E402
from display_client import DisplayClient  # noqa: E402
from desk_media_companion.discovery import lan_broadcasts  # noqa: E402

check, wait_for, PORT, DISCOVERY, LOGS = it.check, it.wait_for, it.PORT, it.DISCOVERY, it.LOGS
ST_CONNECTED, ST_NO_SESSION, ST_UNREACHABLE, ST_NEEDS, ST_PAIRING, ST_LOST = 2, 3, 4, 5, 6, 7


def last_status(board, mark=0):
    last = None
    for _t, ln in board.since(mark):
        m = re.search(r"pc_media: status (\d+)", ln)
        if m:
            last = int(m.group(1))
    return last


def wait_status(board, mark, wanted, timeout):
    return wait_for(lambda: last_status(board, mark) in wanted, timeout, 0.3)


def grab(board, comp, name, action="pair_shot", timeout=60.0):
    out = os.path.join(LOGS, name)
    for _ in range(3):
        mark = board.mark()
        comp.monitor.request_debug(action)
        if not board.wait(r"SHOT_BEGIN", mark, 15.0) or not board.wait(r"SHOT_END", mark, timeout):
            continue
        if shot.assemble([ln for _t, ln in board.since(mark)], out) == 0:
            from PIL import Image
            return Image.open(out).convert("RGB")
    return None


def tap(comp, action):
    comp.act(action)


def reply_status(client, method, target, **kw):
    try:
        return client.raw_request(method, target, **kw)
    except Exception as exc:                                   # a refused / reset connection is a refusal too
        class R:
            status, headers, body = 0, {}, str(exc).encode()
        return R()


def run_pairing(board, comp, via_tap=True):
    """Opens the window and pairs: the display prompts, the PC confirms first, then the display's Pair button is tapped. True when paired."""
    pairing = comp.pairing
    mark = board.mark()
    pairing.open_window()
    if not board.wait(r"pairing code shown", mark, 25.0):
        return False
    wait_for(lambda: pairing.snapshot()["code"], 5.0)
    pairing.pc_confirm()
    time.sleep(0.5)
    if via_tap:
        tap(comp, "touch_pair")
    else:
        tap(comp, "pair_accept")
    return wait_for(lambda: pairing.paired, 20.0)


class Rogue:
    """A hostile 'companion': answers anything with plausible JSON but no X-DM-Proof, or with a huge body."""

    def __init__(self, port, mode="plain"):
        self.hits, self.mode = 0, mode
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _answer(self):
                outer.hits += 1
                if outer.mode == "huge":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(50_000_000))
                    self.send_header("X-DM-Next", "0" * 32)
                    self.end_headers()
                    try:
                        for _ in range(120):
                            self.wfile.write(b"x" * 1024)
                    except OSError:
                        pass
                    self.close_connection = True
                    return
                body = json.dumps({"ok": True, "has_session": True, "playing": True, "state": "playing", "app": "Spotify.exe", "source": "Spotify",
                                   "title": "HACKED", "artist": "rogue", "track_id": "evil", "art_id": "", "any_playing": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-DM-Next", "1" * 32)
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _answer

        self.server = http.server.ThreadingHTTPServer(("0.0.0.0", port), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def beacon(lan_bcast, port, cid_hex, name="rogue", pairing=False):
    payload = b"DESKMEDIA2 " + json.dumps({"v": 2, "port": port, "name": name, "id": cid_hex, "paired": False, "pairing": pairing}).encode()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(payload, (lan_bcast, DISCOVERY))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="auto")
    args = ap.parse_args()
    import urllib.request
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2)
        print(f"something already listens on port {PORT}: stop the normal companion first (this test brings its own).")
        return 2
    except OSError:
        pass
    os.makedirs(LOGS, exist_ok=True)
    port = sc.detect_port() if args.port == "auto" else args.port
    lan_ip, lan_bcast = lan_broadcasts()[0]
    data_dir = tempfile.mkdtemp(prefix="pairing-test-")
    board = it.Board(port)
    comp = it.Companion(os.path.join(data_dir, "idle"))
    pairing = comp.pairing
    resets0 = board.count(r"ESP-ROM|rst:0x|Guru Meditation|abort\(\)|Backtrace")
    try:
        print("\n== 0. start: not paired")
        old_key = pairing.key()
        pairing.forget()
        ok = wait_status(board, 0, (ST_NEEDS, ST_LOST), 40.0)
        check("0 the display reports 'not paired' / 'pairing lost' once the PC has no key", ok, f"status {last_status(board)}")

        print("\n== 1. an unpaired LAN client (this PC's LAN address, not loopback) can do nothing")
        c = DisplayClient(lan_ip, PORT)
        for method, target in (("GET", "/now-playing"), ("GET", "/idle"), ("GET", "/idle/image"), ("GET", "/sources"), ("GET", "/volume"),
                               ("GET", "/artwork?size=300"), ("POST", "/control/playpause?app=x"), ("POST", "/control/seek?position_ms=5"),
                               ("POST", "/control/volume-up?step=5"), ("POST", "/volume?level=10"), ("POST", "/control/source-next")):
            r = reply_status(c, method, target)
            check(f"1 {method} {target.split('?')[0]} -> 401 unpaired", r.status == 401 and b"unpaired" in r.body, f"HTTP {r.status}")
        check("1 /hello and /health stay open", reply_status(c, "GET", "/hello").status == 200 and reply_status(c, "GET", "/health").status == 200)
        check("1 this PC itself (loopback) is exempt", DisplayClient("127.0.0.1", PORT).raw_request("GET", "/now-playing").status == 200)

        print("\n== 2. window closed: no prompt on the display")
        mark = board.mark()
        time.sleep(7.0)
        check("2 no pairing prompt without an open window", board.count(r"pairing code shown", mark) == 0)

        print("\n== 3. window open: the display shows the code prompt")
        mark = board.mark()
        pairing.open_window()
        shown = board.wait(r"pairing code shown", mark, 25.0)
        check("3 the display shows the pairing prompt", bool(shown))
        code = None
        wait_for(lambda: pairing.snapshot()["code"], 5.0)
        snap = pairing.snapshot()
        code = snap["code"]
        check("3 the PC has the 6-digit code for the same attempt", bool(code and re.fullmatch(r"\d{6}", code)), f"state {snap['state']}")
        check("3 status says PAIRING", last_status(board, mark) == ST_PAIRING, f"status {last_status(board, mark)}")
        img = grab(board, comp, "pairing-prompt.png")
        check("3 screenshot of the prompt received (480x480)", img is not None and img.size == (480, 480))
        if img is not None:
            print("      (compare the code in logs/pairing-prompt.png with the PC's: %s)" % code)

        print("\n== 4. Cancel on the display (a real tap) pairs nothing")
        mark = board.mark()
        tap(comp, "touch_paircancel")
        check("4 the display logs the cancel", bool(board.wait(r"cancelled on the display", mark, 10.0)))
        check("4 the PC's attempt is cancelled / closed", wait_for(lambda: pairing.snapshot()["state"] in ("cancelled", "idle"), 8.0))
        check("4 nothing was paired", not pairing.paired)
        check("4 the display is back at 'not paired'", wait_status(board, mark, (ST_NEEDS, ST_LOST), 20.0), f"status {last_status(board, mark)}")

        print("\n== 5. pairing: the PC confirms, then Pair is tapped on the display")
        mark = board.mark()
        paired = run_pairing(board, comp)
        check("5 paired", paired)
        check("5 the display logged the pairing", bool(board.wait(r"paired with the companion", mark, 10.0)))
        check("5 the display is connected (signed requests work)", wait_status(board, mark, (ST_CONNECTED, ST_NO_SESSION), 25.0), f"status {last_status(board, mark)}")
        check("5 the prompt is gone and no answer was discarded", board.count(r"without a valid proof", mark) == 0)
        key1 = pairing.key()

        print("\n== 6. a LAN client with the key works; replay, tampering, wrong key and missing headers do not")
        good = DisplayClient(lan_ip, PORT, key=key1)
        r = good.get("/now-playing")
        check("6 signed GET /now-playing -> 200 with a valid proof", r.status == 200 and r.proof_ok is True, f"HTTP {r.status}")
        r = good.post("/control/source-auto")
        check("6 signed POST /control/source-auto -> 200 with a valid proof", r.status == 200 and r.proof_ok is True, f"HTTP {r.status}")
        nonce = good.fresh_nonce()
        hdrs = good.auth_headers("GET", "/sources", nonce)
        check("6 a captured signed request is accepted once ...", reply_status(good, "GET", "/sources", headers=hdrs).status == 200)
        check("6 ... and refused when replayed", reply_status(good, "GET", "/sources", headers=hdrs).status == 401)
        nonce = good.fresh_nonce()
        hdrs = good.auth_headers("GET", "/sources", nonce)
        check("6 the same signature on another target is refused", reply_status(good, "GET", "/idle", headers=hdrs).status == 401)
        nonce = good.fresh_nonce()
        hdrs = good.auth_headers("GET", "/sources", nonce)
        check("6 the same signature with another method is refused", reply_status(good, "POST", "/sources", headers=hdrs).status == 401)
        bad = DisplayClient(lan_ip, PORT, key=bytes(32))
        check("6 a wrong key is refused", bad.request("GET", "/sources", verify=False).status == 401)
        check("6 a request without the headers is refused", reply_status(DisplayClient(lan_ip, PORT), "GET", "/sources").status == 401)

        print("\n== 7. 'Forget paired display' on the PC")
        mark = board.mark()
        pairing.forget()
        check("7 the display reports 'pairing lost'", wait_status(board, mark, (ST_LOST,), 25.0), f"status {last_status(board, mark)}")
        r = reply_status(good, "GET", "/now-playing")
        check("7 the old key stops working at once", r.status == 401, f"HTTP {r.status}")
        time.sleep(4.0)
        check("7 no prompt without a window", board.count(r"pairing code shown", mark) == 0)

        print("\n== 8. re-pairing while 'lost' replaces the key")
        mark = board.mark()
        check("8 paired again", run_pairing(board, comp, via_tap=True))
        key2 = pairing.key()
        check("8 the new key differs from the old one", key2 is not None and key2 != key1)
        check("8 the display is connected again", wait_status(board, mark, (ST_CONNECTED, ST_NO_SESSION), 30.0), f"status {last_status(board, mark)}")
        check("8 the old key stays dead", reply_status(good, "GET", "/now-playing").status == 401)
        check("8 the new key works", DisplayClient(lan_ip, PORT, key=key2).get("/now-playing").status == 200)

        print("\n== 9. the pairing survives a reboot of the display")
        mark = board.mark()
        board.reset()
        check("9 connected again without a prompt", wait_status(board, mark, (ST_CONNECTED, ST_NO_SESSION), 60.0) and board.count(r"pairing code shown", mark) == 0,
              f"status {last_status(board, mark)}")
        check("9 the key was found in NVS", bool(board.find(r"paired display \(key found\)", mark)))
        resets_expected = 1

        print("\n== 10. a rogue beacon with another companion id is ignored")
        mark = board.mark()
        for _ in range(8):
            beacon(lan_bcast, 8999, "ffeeddccbbaa9988", pairing=True)
            time.sleep(1.0)
        check("10 the display did not follow it", board.count(r"found companion at", mark) == 0 and last_status(board, mark) in (None, ST_CONNECTED, ST_NO_SESSION),
              f"status {last_status(board, mark)}")
        check("10 and showed no prompt", board.count(r"pairing code shown", mark) == 0)

        print("\n== 11. the paired companion is gone and a rogue server spoofs its beacon")
        mark = board.mark()
        comp.stop_http()
        check("11 the display notices the PC is away", wait_status(board, mark, (ST_UNREACHABLE,), 40.0), f"status {last_status(board, mark)}")
        rogue = Rogue(8999)
        mark = board.mark()
        end = time.time() + 30
        while time.time() < end:
            beacon(lan_bcast, 8999, pairing.cid_hex)
            time.sleep(1.0)
        check("11 the display talked to the rogue (it followed the spoofed beacon) ...", rogue.hits > 0, f"{rogue.hits} requests")
        check("11 ... and discarded its answers (no valid proof)", board.count(r"without a valid proof", mark) > 0)
        check("11 the rogue's text never reached the screen", board.count(r"HACKED", mark) == 0)
        rogue.stop()
        rogue = Rogue(8999, "huge")
        mark = board.mark()
        end = time.time() + 20
        while time.time() < end:
            beacon(lan_bcast, 8999, pairing.cid_hex)
            time.sleep(1.0)
        check("11 an oversized / endless answer is cut off safely", rogue.hits > 0 and board.count(r"ESP-ROM|rst:0x|Guru Meditation|abort\(\)|Backtrace", mark) == 0,
              f"{rogue.hits} requests")
        rogue.stop()
        mark = board.mark()
        comp.start_http()
        check("11 the real companion is picked up again", wait_status(board, mark, (ST_CONNECTED, ST_NO_SESSION), 60.0), f"status {last_status(board, mark)}")
        check("11 still paired, no prompt", pairing.paired and board.count(r"pairing code shown", mark) == 0)

        print("\n== 12. the companion's failure limiter")
        flood = DisplayClient(lan_ip, PORT, key=bytes(32))
        codes = [reply_status(flood, "GET", "/sources", headers={"X-DM-Nonce": "0" * 32, "X-DM-Auth": "0" * 64}).status for _ in range(13)]
        check("12 repeated bad signatures end in 429", 429 in codes, f"{codes}")
        check("12 /hello still answers a blocked address", reply_status(flood, "GET", "/hello").status == 200)
        check("12 the display is not affected (different address)", last_status(board, mark) in (ST_CONNECTED, ST_NO_SESSION))

        print("\n== 13. no reset except the deliberate one")
        resets = board.count(r"ESP-ROM|rst:0x|Guru Meditation|abort\(\)|Backtrace") - resets0
        check("13 resets", resets <= resets_expected * 2, f"{resets} reset markers (one reboot = up to 2 lines)")
        comp.monitor.auto_source()
    finally:
        try:
            comp.stop_http()
        except Exception:
            pass
        board.stop = True
        with open(os.path.join(LOGS, "pairing-test-serial.log"), "w", encoding="utf-8") as f:
            for t, ln in board.lines:
                if not re.match(r"^S\d+ ", ln):
                    f.write(f"{t:.2f} {ln}\n")
        shutil.rmtree(data_dir, ignore_errors=True)
    print(f"\n{len(it.failures)} failure(s)" + (": " + "; ".join(it.failures) if it.failures else ""))
    return 1 if it.failures else 0


if __name__ == "__main__":
    sys.exit(main())

"""Pairing, request authentication and the secure-by-default server (docs/protocol.md sections 2 and 3), without any sleeping:
every time limit runs on an injected clock. The HTTP tests use a real server on a loopback test port and the reference display client
(companion/tools/display_client.py); because that client IS loopback, the server's loopback test is patched to say "another LAN host"
there (the loopback exemption and the decision for real LAN addresses are tested separately).

    companion\\.venv\\Scripts\\python.exe -m unittest discover -s companion/tests
"""
import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from desk_media_companion import pairing as pairing_mod  # noqa: E402
from desk_media_companion import server as server_mod  # noqa: E402
from desk_media_companion import ui as ui_mod  # noqa: E402
from desk_media_companion import x25519  # noqa: E402
from desk_media_companion.auth import Authenticator, NonceStore, RateLimiter, request_mac, response_proof  # noqa: E402
from desk_media_companion.discovery import Beacon  # noqa: E402
from desk_media_companion.pairing import (Pairing, PairingError, PairingStore, commitment, derive, hkdf_expand,  # noqa: E402
                                          hkdf_extract)
from desk_media_companion.server import make_server, requires_signature  # noqa: E402
from display_client import DisplayAttempt, DisplayClient, PairingFailed, ProofError  # noqa: E402

H = bytes.fromhex

# ---- the test vectors of docs/protocol.md (appendix) -----------------------------------------------------------------------------
E_PRIV = H("e77c4f598df3914465af5303551ac58efc71bd617ca5877c8cbc590cf2d62a78")
PUB_E = H("d86f406d8e9dfd35ba4b3a63c4f40c4076be2fd20d29f653d6258f2c014c1e53")
C_PRIV = H("00bd2f1bc098fe7a86b8ec025568f65e03c2992c9fef9af1d9cd2c40f76bc5ce")
PUB_C = H("0e7f912e8edb5f58030b926447b3789b122f1ba7dde136ec86a483cb27b2546c")
CID, DEV, N_E, N_C = H("0102030405060708"), H("11" * 8), H("aa" * 16), H("bb" * 16)
CE = H("8238badc7f8e71df9aaa6bbce41c1340f898bd7be3e150a301e182f488710ac7")
Z = H("226406b45f50ac0e954dd7682c9ff42620d4680c334afe6621329d02b8dda434")
T = H("d3448c8c90e680cc95666ef536e80b30b429022d72afff163a39c1e29bbdf628")
K = H("a388cfc735774ba3e13991eadd90a4ffa2020becaf2d78a70541f26db7a09821")
CODE = "959079"
PROOF_DEV = H("fc1bace96c591d3072b8b2bfb2007e2ce9db361c84746ff7c871210016910dac")
PROOF_PC = H("4c2dee3dfc1e9e3f213bb1243186408e285ec92b0034b76b4e31a3ac65fbce95")
NONCE = "00112233445566778899aabbccddeeff"
AUTH_VECTOR = "53a8bdfd2b22c9daef7645c4c96d3a61214f63f3811fdc4f5a04be4d1595f8c8"
PROOF_VECTOR = "3ec39b8f361d53cf98d7f1df2384653ebe137c037ae8b95844b7b16c6c5f26d2"


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class StubMonitor:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.art_calls: list[tuple] = []
        self.debug: list[str] = []

    def now_playing(self):
        return {"ok": True, "has_session": True, "app": "Spotify.exe"}

    def control(self, action, position_ms=None, app=""):
        self.calls.append(("control", action, position_ms, app))
        return {"ok": True, "action": action, "app": app}

    def volume(self, action, step=5, level=None, app=""):
        self.calls.append(("volume", action, step, level, app))
        return {"ok": True, "action": action, "volume": 50}

    def switch_source(self, direction, shown_app=""):
        self.calls.append(("source", direction, shown_app))
        return {"ok": True, "changed": False}

    def auto_source(self):
        self.calls.append(("auto",))
        return {"ok": True}

    def list_sources(self):
        return {"ok": True, "sources": []}

    def artwork(self, size=300, max_w=None):
        self.art_calls.append((size, max_w))
        return "art1", b"\xff\xd8\xff\xe0" + bytes(range(256)) * 4

    def request_debug(self, action):
        self.debug.append(action)

    def take_debug(self):
        return []


def new_pairing(directory: str, clock: FakeClock, **kw) -> Pairing:
    return Pairing(PairingStore(directory), name="TEST-PC", clock=clock, **kw)


def display_offer(p: Pairing, a: DisplayAttempt) -> dict:
    """The display's offer + reveal against a Pairing object (no HTTP); the attempt is completed with the companion's answer."""
    offer = p.offer(a.dev.hex(), a.pub_e.hex(), a.ce.hex())
    a.complete(bytes.fromhex(offer["id"]), bytes.fromhex(offer["pub"]), bytes.fromhex(offer["nc"]))
    return offer


def to_reveal(p: Pairing, a: DisplayAttempt) -> None:
    display_offer(p, a)
    p.reveal(a.dev.hex(), a.n_e.hex())


# ---- 1. the protocol vectors -------------------------------------------------------------------------------------------------------

class Vectors(unittest.TestCase):
    def test_hkdf_matches_rfc_5869_case_1(self):
        prk = hkdf_extract(bytes(range(13)), H("0b" * 22))
        self.assertEqual(prk.hex(), "077709362c2e32df0ddc3f0dc47bba6390b6c73bb50f9c3122ec844ad7c2b3e5")
        self.assertEqual(hkdf_expand(prk, bytes(range(0xf0, 0xfa)), 42).hex(),
                         "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865")

    def test_display_side_reproduces_the_appendix(self):
        a = DisplayAttempt(dev=DEV, e_priv=E_PRIV, n_e=N_E)
        self.assertEqual((a.pub_e, a.ce), (PUB_E, CE))
        self.assertEqual(x25519.x25519(C_PRIV, x25519.BASE_POINT), PUB_C)
        self.assertEqual(x25519.shared(E_PRIV, PUB_C), Z)
        self.assertEqual(x25519.shared(C_PRIV, PUB_E), Z)                       # both sides agree
        d = a.complete(CID, PUB_C, N_C)
        self.assertEqual((d.transcript, d.key, d.code, d.proof_dev, d.proof_pc), (T, K, CODE, PROOF_DEV, PROOF_PC))
        self.assertEqual(commitment(PUB_E, N_E), CE)
        self.assertEqual(derive(CID, DEV, PUB_E, PUB_C, N_E, N_C, Z), d)
        self.assertTrue(a.check_pc_proof(PROOF_PC.hex()))
        self.assertFalse(a.check_pc_proof(PROOF_DEV.hex()))
        self.assertEqual(a.confirm_body()["proof"], PROOF_DEV.hex())

    def test_request_and_response_signatures_match_the_appendix(self):
        self.assertEqual(request_mac(K, NONCE, "GET", "/now-playing"), AUTH_VECTOR)
        self.assertEqual(response_proof(K, NONCE, 200, b'{"ok":true}'), PROOF_VECTOR)

    def test_the_companion_side_runs_the_appendix_through_the_real_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock()
            p = Pairing(PairingStore(tmp, cid=CID), name="TEST-PC", clock=clock, rand=lambda n: N_C[:n] if n == 16 else bytes(n),
                        keygen=lambda: (C_PRIV, PUB_C))
            self.assertEqual(p.cid, CID)
            p.open_window()
            a = DisplayAttempt(dev=DEV, e_priv=E_PRIV, n_e=N_E)
            offer = p.offer(DEV.hex(), PUB_E.hex(), CE.hex())
            self.assertEqual((offer["pub"], offer["nc"], offer["id"], offer["name"]), (PUB_C.hex(), N_C.hex(), CID.hex(), "TEST-PC"))
            self.assertIsNone(p.snapshot()["code"])                          # no code before the reveal
            p.reveal(DEV.hex(), N_E.hex())
            self.assertEqual(p.snapshot()["code"], CODE)
            self.assertEqual(p.confirm(DEV.hex(), a.complete(CID, PUB_C, N_C).proof_dev.hex())["state"], "waiting_pc")
            self.assertTrue(p.pc_confirm())
            self.assertEqual(p.status(DEV.hex()), {"ok": True, "state": "paired", "proof": PROOF_PC.hex()})
            self.assertEqual(p.key(), K)


# ---- 2. the state machine ------------------------------------------------------------------------------------------------------------

class StateMachine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = FakeClock()
        self.p = new_pairing(self.tmp.name, self.clock)

    def refused(self, status, error, fn, *args):
        with self.assertRaises(PairingError) as ctx:
            fn(*args)
        self.assertEqual((ctx.exception.status, ctx.exception.error), (status, error))

    def pair_fully(self, pc_first: bool) -> DisplayAttempt:
        a = DisplayAttempt()
        to_reveal(self.p, a)
        if pc_first:
            self.assertTrue(self.p.pc_confirm())
            self.assertEqual(self.p.snapshot()["state"], "waiting_device")
            self.assertNotEqual(self.p.key(), a.derived.key)                  # not yet: only one of the two confirmations is in
            self.assertEqual(self.p.confirm(a.dev.hex(), a.derived.proof_dev.hex())["state"], "paired")
        else:
            self.assertEqual(self.p.confirm(a.dev.hex(), a.derived.proof_dev.hex())["state"], "waiting_pc")
            self.assertNotEqual(self.p.key(), a.derived.key)
            self.assertTrue(self.p.pc_confirm())
        return a

    def test_offers_need_an_open_window(self):
        a = DisplayAttempt()
        self.refused(403, "pairing_closed", self.p.offer, a.dev.hex(), a.pub_e.hex(), a.ce.hex())
        self.p.open_window()
        self.assertTrue(self.p.window_open)
        self.clock.advance(119)
        self.assertTrue(self.p.window_open)
        self.clock.advance(2)
        self.assertFalse(self.p.window_open)
        self.refused(403, "pairing_closed", self.p.offer, a.dev.hex(), a.pub_e.hex(), a.ce.hex())

    def test_one_attempt_at_a_time(self):
        self.p.open_window()
        a, b = DisplayAttempt(), DisplayAttempt()
        first = display_offer(self.p, a)
        self.refused(409, "busy", self.p.offer, b.dev.hex(), b.pub_e.hex(), b.ce.hex())
        self.assertEqual(self.p.offer(a.dev.hex(), a.pub_e.hex(), a.ce.hex()), first)     # a repeated offer (lost answer) is answered again
        self.p.reveal(a.dev.hex(), a.n_e.hex())
        self.refused(409, "busy", self.p.offer, b.dev.hex(), b.pub_e.hex(), b.ce.hex())   # still busy while revealed

    def test_a_finished_attempt_frees_the_slot_for_the_next_one(self):
        self.p.open_window()
        a = DisplayAttempt()
        to_reveal(self.p, a)
        self.p.cancel(a.dev.hex())
        self.p.open_window()                                                  # a cancel closes the window: the user opens it again
        b = DisplayAttempt()
        display_offer(self.p, b)
        self.assertEqual(self.p.status(b.dev.hex())["state"], "offered")
        self.refused(404, "unknown", self.p.status, a.dev.hex())              # the old attempt is gone

    def test_each_attempt_uses_fresh_keys_and_nonces(self):
        self.p.open_window()
        offers = []
        for _ in range(3):
            a = DisplayAttempt()
            offers.append(display_offer(self.p, a))
            self.p.cancel(a.dev.hex())
            self.p.open_window()
        self.assertEqual(len({o["pub"] for o in offers}), 3)
        self.assertEqual(len({o["nc"] for o in offers}), 3)

    def test_the_reveal_must_follow_the_offer_within_15_seconds(self):
        self.p.open_window()
        a = DisplayAttempt()
        display_offer(self.p, a)
        self.clock.advance(14)
        self.p.reveal(a.dev.hex(), a.n_e.hex())                               # in time
        self.assertEqual(self.p.snapshot()["state"], "revealed")
        b = DisplayAttempt()
        self.p.pc_cancel()
        self.p.open_window()
        display_offer(self.p, b)
        self.clock.advance(16)
        self.refused(409, "expired", self.p.reveal, b.dev.hex(), b.n_e.hex())
        self.assertEqual(self.p.status(b.dev.hex()), {"ok": True, "state": "expired"})
        self.assertIsNone(self.p.snapshot()["code"])

    def test_the_window_limits_both_confirmations(self):
        self.p.open_window()
        a = DisplayAttempt()
        to_reveal(self.p, a)
        self.clock.advance(121)                                               # the offer extended the window to offer + 120 s only
        self.assertEqual(self.p.status(a.dev.hex())["state"], "expired")
        self.refused(409, "expired", self.p.confirm, a.dev.hex(), a.derived.proof_dev.hex())
        self.assertFalse(self.p.pc_confirm())
        self.assertFalse(self.p.paired)

    def test_an_offer_extends_the_window(self):
        self.p.open_window()
        self.clock.advance(100)
        a = DisplayAttempt()
        to_reveal(self.p, a)
        self.clock.advance(100)                                               # t = 200: past the original 120 s, inside offer + 120 s
        self.assertEqual(self.p.confirm(a.dev.hex(), a.derived.proof_dev.hex())["state"], "waiting_pc")
        self.assertTrue(self.p.pc_confirm())
        self.assertTrue(self.p.paired)

    def test_both_confirmations_are_needed_in_either_order(self):
        for pc_first in (True, False):
            with self.subTest(pc_first=pc_first):
                self.tmp2 = tempfile.TemporaryDirectory()
                self.addCleanup(self.tmp2.cleanup)
                self.p = new_pairing(self.tmp2.name, self.clock)
                self.p.open_window()
                a = self.pair_fully(pc_first)
                self.assertTrue(self.p.paired)
                self.assertEqual(self.p.key(), a.derived.key)
                self.assertFalse(self.p.window_open)                          # the window closes when the pairing completes
                self.assertEqual(self.p.status(a.dev.hex()), {"ok": True, "state": "paired", "proof": a.derived.proof_pc.hex()})
                self.assertTrue(a.check_pc_proof(self.p.status(a.dev.hex())["proof"]))

    def test_the_pc_click_alone_or_the_proof_alone_does_not_pair(self):
        self.p.open_window()
        a = DisplayAttempt()
        self.assertFalse(self.p.pc_confirm())                                 # nothing to confirm yet
        to_reveal(self.p, a)
        self.assertTrue(self.p.pc_confirm())
        self.assertFalse(self.p.paired)
        self.assertIsNone(self.p.key())
        self.assertEqual(self.p.status(a.dev.hex()), {"ok": True, "state": "waiting_device"})   # no proof before the pairing is complete
        self.p.pc_cancel()
        self.p.open_window()
        b = DisplayAttempt()
        to_reveal(self.p, b)
        self.assertEqual(self.p.confirm(b.dev.hex(), b.derived.proof_dev.hex())["state"], "waiting_pc")
        self.assertFalse(self.p.paired)
        self.assertNotIn("proof", self.p.status(b.dev.hex()))

    def test_a_wrong_commitment_drops_the_attempt(self):
        self.p.open_window()
        a = DisplayAttempt()
        display_offer(self.p, a)
        self.refused(403, "bad_commit", self.p.reveal, a.dev.hex(), ("cc" * 16))      # not the nonce that was committed to
        self.assertEqual(self.p.status(a.dev.hex())["state"], "cancelled")
        self.assertIsNone(self.p.snapshot()["code"])
        self.assertFalse(self.p.window_open)
        self.refused(409, "bad_state", self.p.reveal, a.dev.hex(), a.n_e.hex())      # and it stays dropped

    def test_a_low_order_display_key_is_refused(self):
        self.p.open_window()
        zero_pub, n_e = bytes(32), H("dd" * 16)
        self.p.offer(DEV.hex(), zero_pub.hex(), commitment(zero_pub, n_e).hex())
        self.refused(400, "bad_key", self.p.reveal, DEV.hex(), n_e.hex())
        self.assertEqual(self.p.status(DEV.hex())["state"], "cancelled")

    def test_a_wrong_device_proof_drops_the_attempt(self):
        self.p.open_window()
        a = DisplayAttempt()
        to_reveal(self.p, a)
        self.p.pc_confirm()
        self.refused(403, "bad_proof", self.p.confirm, a.dev.hex(), PROOF_DEV.hex())
        self.assertEqual(self.p.status(a.dev.hex())["state"], "cancelled")
        self.assertFalse(self.p.paired)
        self.assertFalse(self.p.window_open)

    def test_either_side_can_cancel(self):
        self.p.open_window()
        a = DisplayAttempt()
        to_reveal(self.p, a)
        self.assertEqual(self.p.cancel(a.dev.hex()), {"ok": True, "state": "cancelled"})
        self.assertFalse(self.p.window_open)
        self.assertEqual(self.p.snapshot()["state"], "cancelled")
        self.p.open_window()
        b = DisplayAttempt()
        to_reveal(self.p, b)
        self.assertTrue(self.p.pc_cancel())
        self.assertEqual(self.p.status(b.dev.hex())["state"], "cancelled")
        self.assertIsNone(self.p.snapshot()["code"])
        self.assertFalse(self.p.window_open)
        self.p.open_window()
        self.assertTrue(self.p.pc_cancel())                                    # cancelling an open window without an attempt closes it
        self.assertFalse(self.p.window_open)
        self.assertFalse(self.p.pc_cancel())

    def test_unknown_attempts_and_bad_input(self):
        self.p.open_window()
        a = DisplayAttempt()
        display_offer(self.p, a)
        other = ("22" * 8)
        for fn, args in ((self.p.reveal, (other, a.n_e.hex())), (self.p.confirm, (other, "00" * 32)), (self.p.status, (other,)),
                         (self.p.cancel, (other,))):
            self.refused(404, "unknown", fn, *args)
        bad = [None, 5, [], "", "zz" * 8, "11" * 7, "11" * 9, "1" * 15 + "é", " " + "1" * 15]
        for dev in bad:
            self.refused(400, "bad_request", self.p.status, dev)
            self.refused(400, "bad_request", self.p.cancel, dev)
        for args in ((None, a.pub_e.hex(), a.ce.hex()), (a.dev.hex(), "00" * 31, a.ce.hex()), (a.dev.hex(), a.pub_e.hex(), None),
                     (a.dev.hex(), a.pub_e.hex(), "g" * 64)):
            self.refused(400, "bad_request", self.p.offer, *args)
        self.refused(400, "bad_request", self.p.reveal, a.dev.hex(), "aa" * 15)
        self.refused(400, "bad_request", self.p.confirm, a.dev.hex(), "aa" * 31)

    def test_forget_removes_the_key_at_once_and_keeps_the_companion_id(self):
        self.p.open_window()
        self.pair_fully(False)
        cid = self.p.cid_hex
        self.assertTrue(self.p.paired)
        self.assertTrue(self.p.forget())
        self.assertFalse(self.p.paired)
        self.assertIsNone(self.p.key())
        self.assertFalse(self.p.forget())
        self.assertEqual(self.p.cid_hex, cid)
        self.assertFalse(new_pairing(self.tmp.name, self.clock).paired)        # also gone from disk
        self.assertEqual(new_pairing(self.tmp.name, self.clock).cid_hex, cid)

    def test_a_new_pairing_replaces_the_old_key(self):
        self.p.open_window()
        first = self.pair_fully(False)
        self.p.open_window()
        second = self.pair_fully(True)
        self.assertNotEqual(first.derived.key, second.derived.key)
        self.assertEqual(self.p.key(), second.derived.key)
        self.assertEqual(new_pairing(self.tmp.name, self.clock).key(), second.derived.key)

    def test_snapshot_for_the_dialog(self):
        s = self.p.snapshot()
        self.assertEqual((s["state"], s["code"], s["window_open"], s["seconds_left"], s["name"], s["paired"]),
                         ("idle", None, False, 0, "TEST-PC", False))
        self.p.open_window()
        self.clock.advance(20.5)
        s = self.p.snapshot()
        self.assertEqual((s["window_open"], s["seconds_left"]), (True, 100))
        a = DisplayAttempt()
        display_offer(self.p, a)
        s = self.p.snapshot()
        self.assertEqual((s["state"], s["code"], s["device"]), ("offered", None, a.dev.hex()[:8]))
        self.p.reveal(a.dev.hex(), a.n_e.hex())
        s = self.p.snapshot()
        self.assertEqual((s["state"], s["code"]), ("revealed", a.code))
        self.assertRegex(s["code"], r"^\d{6}$")

    def test_the_code_is_six_digits_with_leading_zeros(self):
        seen = set()
        for i in range(40):
            a = DisplayAttempt(rand=lambda n, i=i: bytes([(i * 7 + j) % 256 for j in range(n)]))
            seen.add(a.complete(CID, PUB_C, N_C).code)
        self.assertTrue(all(len(c) == 6 and c.isdigit() for c in seen))


class Mitm(unittest.TestCase):
    """A man in the middle runs one handshake with the display and one with the companion: the codes cannot match (except with
    probability 10^-6), and the display's proof is worthless on the other leg."""

    def test_two_different_keys_on_the_two_legs_give_different_codes_and_no_pairing(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock()
            atk_priv2, atk_pub2 = H("11" * 32), x25519.x25519(H("11" * 32), x25519.BASE_POINT)
            # leg 2: the attacker plays "display" towards the real companion
            pc = Pairing(PairingStore(tmp, cid=CID), name="PC", clock=clock, rand=lambda n: H("dd") * n, keygen=lambda: (C_PRIV, PUB_C))
            pc.open_window()
            atk = DisplayAttempt(dev=H("22" * 8), e_priv=atk_priv2, n_e=H("cc" * 16))
            offer = pc.offer(atk.dev.hex(), atk.pub_e.hex(), atk.ce.hex())
            atk.complete(CID, bytes.fromhex(offer["pub"]), bytes.fromhex(offer["nc"]))
            pc.reveal(atk.dev.hex(), atk.n_e.hex())
            pc_code = pc.snapshot()["code"]
            # leg 1: the attacker plays "companion" towards the real display (its own key and nonce, the real companion id)
            display = DisplayAttempt(dev=DEV, e_priv=E_PRIV, n_e=N_E)
            display.complete(CID, atk_pub2, H("ee" * 16))
            self.assertNotEqual(display.code, pc_code)
            self.assertNotEqual(display.derived.key, atk.derived.key)
            # the display's proof (made under its leg's key) is rejected by the companion; nothing gets paired
            pc.pc_confirm()
            with self.assertRaises(PairingError) as ctx:
                pc.confirm(atk.dev.hex(), display.derived.proof_dev.hex())
            self.assertEqual(ctx.exception.error, "bad_proof")
            self.assertFalse(pc.paired)


# ---- 3. the store --------------------------------------------------------------------------------------------------------------------

class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "data")                        # does not exist yet: the store creates it

    def test_round_trip_and_cid_survives_forget(self):
        s = PairingStore(self.dir)
        self.assertEqual(len(s.cid), 8)
        self.assertIsNone(s.device)
        s.set_device(H("a1" * 8), K)
        s2 = PairingStore(self.dir)
        self.assertEqual((s2.cid, s2.device.id, s2.device.key), (s.cid, H("a1" * 8), K))
        self.assertGreater(s2.device.paired_at, 1_700_000_000)
        s2.clear_device()
        s3 = PairingStore(self.dir)
        self.assertEqual((s3.cid, s3.device), (s.cid, None))

    @unittest.skipUnless(os.name == "nt", "DPAPI is Windows only")
    def test_the_file_is_not_plaintext(self):
        s = PairingStore(self.dir)
        s.set_device(H("a1" * 8), K)
        with open(s.path, "rb") as f:
            blob = f.read()
        self.assertEqual(blob[:5], b"DMP1D")                                  # the DPAPI scheme
        for secret in (K, K.hex().encode(), b'"key"', b'"device"', s.cid.hex().encode(), H("a1" * 8).hex().encode()):
            self.assertNotIn(secret, blob)

    @unittest.skipUnless(os.name == "nt", "DPAPI is Windows only")
    def test_dpapi_needs_the_applications_entropy(self):
        blob = pairing_mod._dpapi(b"secret", True)
        self.assertEqual(pairing_mod._dpapi(blob, False), b"secret")
        with mock.patch.object(pairing_mod, "_ENTROPY", b"another application"):
            with self.assertRaises(OSError):
                pairing_mod._dpapi(blob, False)

    def test_the_plain_fallback_for_other_platforms(self):
        with mock.patch.object(pairing_mod, "_use_dpapi", return_value=False):
            s = PairingStore(self.dir)
            s.set_device(H("a1" * 8), K)
            with open(s.path, "rb") as f:
                self.assertEqual(f.read(5), b"DMP1P")
            self.assertEqual(PairingStore(self.dir).device.key, K)
            if os.name != "nt":
                self.assertEqual(os.stat(s.path).st_mode & 0o777, 0o600)

    def test_a_corrupt_or_foreign_file_means_unpaired_and_never_crashes(self):
        good = PairingStore(self.dir)
        good.set_device(H("a1" * 8), K)
        with open(good.path, "rb") as f:
            blob = f.read()
        bad = [b"", b"not a pairing file", b"DMP1", b"DMP1D" + os.urandom(120), b"DMP1P" + b'{"v":1,"cid":"zz"}',
               b"DMP1P" + b"[1,2,3]", blob[:-5], blob[:5] + bytes(len(blob) - 5), b"\xff" * 5000,
               b"DMP1D" + b"x" * 20000]
        for content in bad:
            with self.subTest(size=len(content), head=content[:6]):
                with open(good.path, "wb") as f:
                    f.write(content)
                with self.assertLogs("pairing", "WARNING") as cm:
                    s = PairingStore(self.dir)
                self.assertIsNone(s.device)
                self.assertEqual(len(s.cid), 8)
                self.assertNotIn(K.hex(), "\n".join(cm.output))               # the content is never logged
                self.assertEqual(PairingStore(self.dir).cid, s.cid)           # a fresh, valid file replaced it

    def test_an_unwritable_location_does_not_stop_the_store(self):
        blocker = os.path.join(self.tmp.name, "file")
        with open(blocker, "wb") as f:
            f.write(b"x")
        with self.assertLogs("pairing", "WARNING"):
            s = PairingStore(os.path.join(blocker, "sub"))                    # a directory cannot be created below a file
        self.assertEqual(len(s.cid), 8)
        with self.assertRaises(OSError):
            s.set_device(H("a1" * 8), K)
        self.assertEqual(s.device.key, K)                                     # still paired for this run

    def test_no_temporary_files_are_left_behind(self):
        s = PairingStore(self.dir)
        s.set_device(H("a1" * 8), K)
        s.clear_device()
        self.assertEqual(os.listdir(self.dir), [pairing_mod.FILE_NAME])

    def test_default_directory_honours_the_data_dir_override(self):
        with mock.patch.dict(os.environ, {"DESK_MEDIA_DATA_DIR": self.dir}):
            self.assertEqual(PairingStore().dir, self.dir)
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": self.tmp.name}):
            os.environ.pop("DESK_MEDIA_DATA_DIR", None)
            self.assertEqual(pairing_mod.default_data_dir(), os.path.join(self.tmp.name, "DeskMediaCompanion"))


# ---- 4. nonces, signatures, rate limiting ---------------------------------------------------------------------------------------------

class AuthUnits(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.auth = Authenticator(clock=self.clock)

    def sign(self, method="GET", target="/now-playing", key=K):
        nonce = self.auth.next_nonce()
        return nonce, request_mac(key, nonce, method, target)

    def test_nonces_are_32_hex_single_use_and_expire_after_60_seconds(self):
        n = self.auth.nonces
        a = n.issue()
        self.assertRegex(a, r"^[0-9a-f]{32}$")
        self.assertTrue(n.consume(a))
        self.assertFalse(n.consume(a))
        b = n.issue()
        self.clock.advance(59)
        self.assertTrue(n.consume(b))
        c = n.issue()
        self.clock.advance(61)
        self.assertFalse(n.consume(c))
        self.assertFalse(n.consume("00" * 16))
        self.assertEqual(len({n.issue() for _ in range(20)}), 20)

    def test_at_most_32_nonces_are_outstanding(self):
        n = NonceStore(clock=self.clock)
        issued = [n.issue(f"10.0.0.{i}") for i in range(40)]                   # (from 40 different hosts: the per-host cap is separate)
        self.assertEqual(len(n), 32)
        self.assertFalse(n.consume(issued[0]))                                # the oldest ones made room
        self.assertTrue(n.consume(issued[-1]))

    def test_one_host_cannot_push_anothers_nonce_out_of_the_pool(self):
        n = NonceStore(clock=self.clock)
        display_nonce = n.issue("192.168.1.50")
        flood = [n.issue("192.168.1.66") for _ in range(100)]                 # a host polling /health as fast as it can
        self.assertEqual(len(n), 1 + 8)                                       # it only ever holds its own 8
        self.assertTrue(n.consume(display_nonce))
        self.assertFalse(n.consume(flood[0]))
        self.assertTrue(n.consume(flood[-1]))
        many = NonceStore(clock=self.clock)
        for i in range(60):                                                   # many hosts together still cannot exceed 32
            many.issue(f"10.0.0.{i}")
        self.assertEqual(len(many), 32)

    def test_verify_accepts_one_correct_request_once(self):
        nonce, mac = self.sign()
        self.assertTrue(self.auth.verify("GET", "/now-playing", nonce, mac, K))
        self.assertFalse(self.auth.verify("GET", "/now-playing", nonce, mac, K))        # replay

    def test_a_failed_check_still_consumes_the_nonce(self):
        nonce, mac = self.sign()
        self.assertFalse(self.auth.verify("GET", "/now-playing", nonce, "00" * 32, K))
        self.assertFalse(self.auth.verify("GET", "/now-playing", nonce, mac, K))        # the right MAC no longer helps

    def test_method_target_key_and_nonce_are_all_bound(self):
        for kwargs in ({"method": "POST"}, {"target": "/now-playing?x=1"}, {"target": "/now-playing/"}, {"key": H("00" * 32)}):
            with self.subTest(kwargs=kwargs):
                nonce, mac = self.sign()
                args = {"method": "GET", "target": "/now-playing", "key": K}
                args.update(kwargs)
                self.assertFalse(self.auth.verify(args["method"], args["target"], nonce, mac, args["key"]))
        nonce, mac = self.sign()
        other = self.auth.next_nonce()
        self.assertFalse(self.auth.verify("GET", "/now-playing", other, mac, K))

    def test_garbage_input_is_refused_not_raised(self):
        nonce, mac = self.sign()
        for n, m in ((None, None), ("", ""), (nonce, "é" * 64), (nonce, mac[:-1]), (nonce[:-1], mac), ("é" * 32, mac), (nonce, "zz" * 32)):
            self.assertFalse(self.auth.verify("GET", "/now-playing", n, m, K))

    def test_expired_nonce_is_refused(self):
        nonce, mac = self.sign()
        self.clock.advance(61)
        self.assertFalse(self.auth.verify("GET", "/now-playing", nonce, mac, K))

    def test_limiter_blocks_after_ten_failures_and_recovers(self):
        lim = self.auth.limiter
        for i in range(9):
            lim.fail("192.168.1.50")
            self.assertEqual(lim.blocked_for("192.168.1.50"), 0)
        lim.fail("192.168.1.50")
        self.assertTrue(1 <= lim.blocked_for("192.168.1.50") <= 60)
        self.assertEqual(lim.blocked_for("192.168.1.51"), 0)                  # per address
        self.clock.advance(30)
        self.assertTrue(1 <= lim.blocked_for("192.168.1.50") <= 30)
        self.clock.advance(31)
        self.assertEqual(lim.blocked_for("192.168.1.50"), 0)
        for i in range(9):                                                    # the old failures do not count any more
            lim.fail("192.168.1.50")
        self.assertEqual(lim.blocked_for("192.168.1.50"), 0)

    def test_failures_older_than_a_minute_age_out(self):
        lim = self.auth.limiter
        for _ in range(9):
            lim.fail("10.0.0.2")
        self.clock.advance(61)
        lim.fail("10.0.0.2")
        self.assertEqual(lim.blocked_for("10.0.0.2"), 0)

    def test_limiter_memory_is_bounded(self):
        lim = RateLimiter(clock=self.clock, max_tracked=64)
        for i in range(2000):
            lim.fail(f"10.{i // 250}.{i % 250}.1")
            self.clock.advance(0.001)
        for i in range(2000):                                                 # and a flood that trips the block for every address
            for _ in range(10):
                lim.fail(f"172.16.{i // 250}.{i % 250}")
        self.assertLessEqual(len(lim._fails), 64)
        self.assertLessEqual(len(lim._blocked), 64)


# ---- 5. the real server ------------------------------------------------------------------------------------------------------------------

def http_request(port: int, method: str, target: str, headers: dict | None = None, body: bytes | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, target, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


def raw_status(port: int, data: bytes) -> int:
    """Sends raw bytes, returns the status of the answer (the request must make the server close, or say Connection: close)."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(data)
        buf = b""
        while b"\r\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    return int(buf.split(b" ", 2)[1])


class HttpCase(unittest.TestCase):
    dev_tools = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = FakeClock()
        self.pairing = new_pairing(self.tmp.name, self.clock)
        self.auth = Authenticator(clock=self.clock)
        self.mon = StubMonitor()
        self.server = make_server(self.mon, 0, dev_tools=self.dev_tools, pairing=self.pairing, bind="127.0.0.1", auth=self.auth)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.clients: list[DisplayClient] = []
        patcher = mock.patch.object(server_mod, "is_loopback", return_value=False)   # the test client is loopback: act as "another LAN host"
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.stop_server)

    def stop_server(self):
        for c in self.clients:
            c.close()
        self.server.shutdown()
        self.server.server_close()

    def client(self, key=None) -> DisplayClient:
        c = DisplayClient("127.0.0.1", self.port, key=key)
        self.clients.append(c)
        return c

    def pair_client(self) -> DisplayClient:
        """A full pairing as the display does it; the user's side on the PC is pc_confirm() after checking that the codes match."""
        self.pairing.open_window()
        c = self.client()

        def on_code(code):
            self.assertEqual(self.pairing.snapshot()["code"], code)            # the same code on both screens
            return True
        c.pair(on_code=on_code, after_confirm=lambda: self.assertTrue(self.pairing.pc_confirm()))
        return c

    def post_json(self, c: DisplayClient, route: str, body):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        r = c.raw_request("POST", route, {"Content-Type": "application/json"}, raw)
        return r.status, r.json()


class OpenRoutesAndUnpaired(HttpCase):
    def test_hello_and_health_are_open_and_every_answer_carries_a_nonce(self):
        c = self.client()
        r = c.raw_request("GET", "/hello")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.json(), {"ok": True, "v": 2, "id": self.pairing.cid_hex, "name": "TEST-PC", "paired": False, "pairing": False})
        self.assertRegex(r.headers["x-dm-next"], r"^[0-9a-f]{32}$")
        self.assertNotIn("x-dm-proof", r.headers)
        r = c.raw_request("GET", "/health")
        self.assertEqual((r.status, r.json()["service"]), (200, "desk-media-companion"))
        self.assertIn("x-dm-next", r.headers)
        self.pairing.open_window()
        self.assertTrue(c.hello()["pairing"])

    def test_nothing_else_is_served_to_an_unpaired_lan_client(self):
        c = self.client()
        for method, target in (("GET", "/now-playing"), ("GET", "/artwork?size=100"), ("GET", "/volume"), ("GET", "/sources"),
                               ("GET", "/idle"), ("GET", "/nonexistent"), ("POST", "/control/next"), ("POST", "/control/seek?position_ms=5"),
                               ("POST", "/volume?level=5"), ("POST", "/control/source-auto"), ("POST", "/debug/screenshot")):
            r = c.raw_request(method, target)
            self.assertEqual((r.status, r.json()), (401, {"ok": False, "error": "unpaired"}), (method, target))
            self.assertIn("x-dm-next", r.headers)
            self.assertNotIn("x-dm-proof", r.headers)
        self.assertEqual((self.mon.calls, self.mon.art_calls, self.mon.debug), ([], [], []))

    def test_unpaired_answers_are_not_counted_as_failures(self):
        c = self.client()
        for _ in range(30):
            self.assertEqual(c.raw_request("GET", "/now-playing").status, 401)

    def test_the_server_without_a_pairing_object_is_closed_too(self):
        srv = make_server(self.mon, 0, bind="127.0.0.1")
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        status, _h, body = http_request(srv.server_address[1], "GET", "/now-playing")
        self.assertEqual((status, json.loads(body)["error"]), (401, "unpaired"))
        self.assertEqual(http_request(srv.server_address[1], "GET", "/hello")[0], 200)
        self.assertEqual(http_request(srv.server_address[1], "POST", "/pair/offer", {"X-Desk-Client": "t"}, b"{}")[0], 403)

    def test_the_existing_guards_come_first(self):
        port = self.port
        self.assertEqual(http_request(port, "GET", "/hello", {"Host": "example.com"})[0], 403)             # DNS rebinding guard
        self.assertEqual(http_request(port, "GET", "/now-playing", {"Host": "example.com"})[0], 403)
        self.assertEqual(http_request(port, "POST", "/pair/offer", {}, b"{}")[0], 403)                      # POST needs X-Desk-Client
        self.assertEqual(http_request(port, "POST", "/pair/offer", {"X-Desk-Client": "t"}, b"{}")[0], 400)  # guards passed: the body is judged
        with mock.patch.object(server_mod, "_client_allowed", return_value=False):                         # a public address
            status, headers, _b = http_request(port, "GET", "/hello")
        self.assertEqual(status, 403)
        self.assertNotIn("x-dm-next", headers)

    def test_no_cors_headers_and_preflights_fail_cleanly(self):
        c = self.client()
        status, headers, _b = http_request(self.port, "OPTIONS", "/now-playing",
                                           {"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"})
        self.assertEqual(status, 405)
        self.assertFalse([h for h in headers if h.startswith("access-control")])
        for target in ("/hello", "/health"):
            _s, headers, _b = http_request(self.port, "GET", target, {"Origin": "http://evil.example"})
            self.assertFalse([h for h in headers if h.startswith("access-control")])
        self.assertEqual(c.raw_request("PUT", "/now-playing").status, 501)


class PairingOverHttp(HttpCase):
    def test_the_full_pairing_and_the_first_signed_request(self):
        with self.assertLogs("pairing", "DEBUG") as logs:
            c = self.pair_client()
        self.assertTrue(self.pairing.paired)
        self.assertEqual(self.pairing.key(), c.key)
        self.assertEqual(c.cid, self.pairing.cid)
        self.assertFalse(self.pairing.window_open)
        hello = c.hello()
        self.assertEqual((hello["paired"], hello["pairing"]), (True, False))
        r = c.get("/now-playing")
        self.assertEqual((r.status, r.json()["app"], r.proof_ok), (200, "Spotify.exe", True))
        self.assertIn("x-dm-next", r.headers)
        self.assertIn("x-dm-proof", r.headers)
        text = "\n".join(logs.output)
        self.assertNotIn(c.key.hex(), text)
        self.assertIn("display paired", text)

    def test_the_secrets_of_one_pairing_never_appear_in_the_log(self):
        self.pairing.open_window()
        c = self.client()
        seen: list[DisplayAttempt] = []
        with self.assertLogs("pairing", "DEBUG") as logs:
            a = c.pair(on_code=lambda code: True, after_confirm=self.pairing.pc_confirm)
            seen.append(a)
        text = "\n".join(logs.output)
        for secret in (a.derived.key.hex(), a.derived.code, a.n_e.hex(), a.derived.proof_dev.hex(), a.derived.proof_pc.hex(),
                       a.pub_e.hex(), a.dev.hex()):
            self.assertNotIn(secret, text)

    def test_signed_requests_work_repeatedly_and_every_answer_is_proven(self):
        c = self.pair_client()
        for _ in range(5):
            r = c.get("/now-playing")
            self.assertEqual((r.status, r.proof_ok), (200, True))
        r = c.post("/control/next?app=Spotify.exe")
        self.assertEqual((r.status, r.proof_ok), (200, True))
        self.assertEqual(self.mon.calls, [("control", "next", None, "Spotify.exe")])
        r = c.get("/artwork?size=312&maxw=432")                                 # a binary body is proven too
        self.assertEqual((r.status, r.proof_ok, r.headers["content-type"]), (200, True, "image/jpeg"))
        self.assertEqual(r.headers["x-art-id"], "art1")

    def test_errors_of_authenticated_requests_carry_a_proof_but_401_and_429_do_not(self):
        c = self.pair_client()
        r = c.get("/nonexistent")
        self.assertEqual((r.status, r.proof_ok), (404, True))
        r = c.post("/volume")                                                   # level missing: 400
        self.assertEqual((r.status, r.proof_ok), (400, True))
        r = c.post("/control/explode")
        self.assertEqual((r.status, r.proof_ok), (404, True))
        r = c.raw_request("GET", "/now-playing")                                # no signature
        self.assertEqual(r.status, 401)
        self.assertNotIn("x-dm-proof", r.headers)
        self.assertIn("x-dm-next", r.headers)

    def test_the_response_proof_binds_body_status_request_and_key(self):
        c = self.pair_client()
        nonce = c.fresh_nonce()
        reply = c.raw_request("GET", "/now-playing", c.auth_headers("GET", "/now-playing", nonce))
        self.assertEqual(reply.status, 200)
        good = response_proof(c.key, nonce, 200, reply.body)
        self.assertEqual(reply.headers["x-dm-proof"], good)
        self.assertNotEqual(response_proof(c.key, nonce, 200, reply.body + b" "), good)       # body bound
        self.assertNotEqual(response_proof(c.key, nonce, 404, reply.body), good)              # status bound
        self.assertNotEqual(response_proof(c.key, "11" * 16, 200, reply.body), good)          # request (nonce) bound
        self.assertNotEqual(response_proof(H("00" * 32), nonce, 200, reply.body), good)

    def test_missing_or_bad_signatures_are_401_with_a_fresh_nonce_to_retry_with(self):
        c = self.pair_client()
        r = c.raw_request("GET", "/now-playing")
        self.assertEqual((r.status, r.json()), (401, {"ok": False, "error": "auth"}))
        nonce = r.headers["x-dm-next"]
        r = c.raw_request("GET", "/now-playing", {"X-DM-Nonce": nonce})                      # nonce without MAC
        self.assertEqual(r.status, 401)
        r = c.raw_request("GET", "/now-playing", {"X-DM-Nonce": r.headers["x-dm-next"], "X-DM-Auth": "00" * 32})
        self.assertEqual(r.status, 401)
        retry = c.raw_request("GET", "/now-playing", c.auth_headers("GET", "/now-playing", r.headers["x-dm-next"]))
        self.assertEqual(retry.status, 200)
        self.assertEqual(self.mon.calls, [])

    def test_the_client_retries_once_after_a_401(self):
        c = self.pair_client()
        c._nonce = "ab" * 16                                                   # a nonce the companion never issued
        r = c.get("/now-playing")
        self.assertEqual((r.status, r.proof_ok), (200, True))

    def test_a_replayed_request_is_refused(self):
        c = self.pair_client()
        nonce = c.fresh_nonce()
        headers = c.auth_headers("POST", "/control/next", nonce)
        self.assertEqual(c.raw_request("POST", "/control/next", headers).status, 200)
        self.assertEqual(c.raw_request("POST", "/control/next", headers).status, 401)
        self.assertEqual(self.mon.calls, [("control", "next", None, "")])           # the second one never reached the monitor

    def test_unknown_and_expired_nonces_are_refused(self):
        c = self.pair_client()
        headers = c.auth_headers("GET", "/now-playing", "ab" * 16)
        self.assertEqual(c.raw_request("GET", "/now-playing", headers).status, 401)
        nonce = c.fresh_nonce()
        headers = c.auth_headers("GET", "/now-playing", nonce)
        self.clock.advance(61)
        self.assertEqual(c.raw_request("GET", "/now-playing", headers).status, 401)

    def test_a_tampered_target_method_or_mac_is_refused(self):
        c = self.pair_client()
        cases = [("GET", "/now-playing", "GET", "/now-playing?x=1"), ("GET", "/now-playing", "GET", "/sources"),
                 ("POST", "/control/next?app=a", "POST", "/control/next?app=b"), ("POST", "/control/next", "GET", "/control/next"),
                 ("POST", "/control/playpause", "POST", "/control/next")]
        for sign_m, sign_t, send_m, send_t in cases:
            with self.subTest(signed=(sign_m, sign_t), sent=(send_m, send_t)):
                headers = c.auth_headers(sign_m, sign_t, c.fresh_nonce())
                self.assertEqual(c.raw_request(send_m, send_t, headers).status, 401)
        headers = c.auth_headers("GET", "/now-playing", c.fresh_nonce())
        headers["X-DM-Auth"] = headers["X-DM-Auth"][:-1] + ("0" if headers["X-DM-Auth"][-1] != "0" else "1")
        self.assertEqual(c.raw_request("GET", "/now-playing", headers).status, 401)
        self.assertEqual(self.mon.calls, [])

    def test_another_key_is_refused(self):
        self.pair_client()
        stranger = self.client(key=H("5a" * 32))
        r = stranger.get("/now-playing", verify=False)
        self.assertEqual(r.status, 401)

    def test_rate_limit_after_ten_failures_and_recovery(self):
        c = self.pair_client()
        bad = {"X-DM-Nonce": "00" * 16, "X-DM-Auth": "00" * 32}
        for _ in range(10):
            self.assertEqual(c.raw_request("GET", "/now-playing", bad).status, 401)
        r = c.raw_request("GET", "/now-playing", bad)
        self.assertEqual(r.status, 429)
        self.assertTrue(1 <= int(r.headers["retry-after"]) <= 60)
        self.assertNotIn("x-dm-proof", r.headers)
        self.assertEqual(r.json()["ok"], False)
        valid = c.auth_headers("GET", "/now-playing", c.fresh_nonce())
        self.assertEqual(c.raw_request("GET", "/now-playing", valid).status, 429)                  # even a good request waits
        self.assertEqual(c.hello()["paired"], True)                                                  # the open routes still answer
        self.clock.advance(30)
        self.assertEqual(c.raw_request("GET", "/now-playing", bad).status, 429)
        self.clock.advance(31)
        r = c.get("/now-playing")                                                                    # recovered (retry covers the old nonce)
        self.assertEqual((r.status, r.proof_ok), (200, True))

    def test_the_failure_count_is_per_address_not_global(self):
        c = self.pair_client()
        for _ in range(12):
            self.auth.limiter.fail("192.168.1.99")
        self.assertGreater(self.auth.limiter.blocked_for("192.168.1.99"), 0)
        self.assertEqual(c.get("/now-playing").status, 200)                                          # this client is 127.0.0.1

    def test_forget_locks_the_display_out_immediately(self):
        c = self.pair_client()
        self.assertEqual(c.get("/now-playing").status, 200)
        self.pairing.forget()
        r = c.get("/now-playing", verify=False)
        self.assertEqual((r.status, r.json()), (401, {"ok": False, "error": "unpaired"}))
        self.assertEqual(c.hello()["paired"], False)
        self.assertEqual(self.mon.calls, [])

    def test_re_pairing_replaces_the_key_and_the_old_key_stops_working(self):
        first = self.pair_client()
        old_key = first.key
        self.assertEqual(first.get("/now-playing").status, 200)
        second = self.pair_client()
        self.assertNotEqual(second.key, old_key)
        self.assertEqual(second.get("/now-playing").status, 200)
        r = first.get("/now-playing", verify=False)
        self.assertEqual((r.status, r.json()["error"]), (401, "auth"))
        self.assertEqual(self.pairing.key(), second.key)

    def test_the_pairing_survives_a_restart(self):
        c = self.pair_client()
        again = new_pairing(self.tmp.name, self.clock)
        self.assertTrue(again.paired)
        self.assertEqual((again.key(), again.cid), (c.key, self.pairing.cid))

    def test_a_pairing_window_must_be_open_and_one_attempt_runs_at_a_time(self):
        c = self.client()
        a = DisplayAttempt()
        self.assertEqual(self.post_json(c, "/pair/offer", a.offer_body()), (403, {"ok": False, "error": "pairing_closed"}))
        self.pairing.open_window()
        status, offer = self.post_json(c, "/pair/offer", a.offer_body())
        self.assertEqual((status, offer["ok"], offer["name"], offer["id"]), (200, True, "TEST-PC", self.pairing.cid_hex))
        other = self.client()
        b = DisplayAttempt()
        self.assertEqual(self.post_json(other, "/pair/offer", b.offer_body()), (409, {"ok": False, "error": "busy"}))
        self.assertEqual(self.post_json(c, "/pair/cancel", {"dev": a.dev.hex()}), (200, {"ok": True, "state": "cancelled"}))

    def test_http_expiry_of_the_reveal_and_the_window(self):
        c = self.client()
        self.pairing.open_window()
        a = DisplayAttempt()
        self.post_json(c, "/pair/offer", a.offer_body())
        self.clock.advance(16)
        self.assertEqual(self.post_json(c, "/pair/reveal", a.reveal_body()), (409, {"ok": False, "error": "expired"}))
        r = c.raw_request("GET", f"/pair/status?dev={a.dev.hex()}")
        self.assertEqual(r.json(), {"ok": True, "state": "expired"})
        self.clock.advance(200)
        b = DisplayAttempt()
        self.assertEqual(self.post_json(c, "/pair/offer", b.offer_body())[0], 403)

    def test_http_wrong_commitment_and_wrong_proof(self):
        c = self.client()
        self.pairing.open_window()
        a = DisplayAttempt()
        status, offer = self.post_json(c, "/pair/offer", a.offer_body())
        self.assertEqual(self.post_json(c, "/pair/reveal", {"dev": a.dev.hex(), "ne": "cc" * 16}), (403, {"ok": False, "error": "bad_commit"}))
        self.assertEqual(c.raw_request("GET", f"/pair/status?dev={a.dev.hex()}").json()["state"], "cancelled")
        self.pairing.open_window()
        b = DisplayAttempt()
        status, offer = self.post_json(c, "/pair/offer", b.offer_body())
        b.complete(bytes.fromhex(offer["id"]), bytes.fromhex(offer["pub"]), bytes.fromhex(offer["nc"]))
        self.assertEqual(self.post_json(c, "/pair/reveal", b.reveal_body())[1]["state"], "revealed")
        self.pairing.pc_confirm()
        self.assertEqual(self.post_json(c, "/pair/confirm", {"dev": b.dev.hex(), "proof": "00" * 32}),
                         (403, {"ok": False, "error": "bad_proof"}))
        self.assertFalse(self.pairing.paired)

    def test_pairing_requests_are_validated(self):
        c = self.client()
        self.pairing.open_window()
        for body in (b"", b"not json", b"[]", b'"x"', b"5", b"null", b'{"dev": 5}', b'{"dev":"11","pub":"00","ce":"00"}', b"{" * 500,
                     b'{"dev":"' + b"1" * 200 + b'"}'):
            status, data = self.post_json(c, "/pair/offer", body)
            self.assertEqual((status, data), (400, {"ok": False, "error": "bad_request"}), body[:20])
        for route in ("/pair/reveal", "/pair/confirm", "/pair/cancel"):
            self.assertEqual(self.post_json(c, route, {"dev": "11" * 8, "ne": "aa" * 16, "proof": "aa" * 32})[0], 404)
        self.assertEqual(c.raw_request("GET", "/pair/status").json(), {"ok": False, "error": "bad_request"})
        self.assertEqual(c.raw_request("GET", "/pair/status?dev=zz").status, 400)
        self.assertEqual(c.raw_request("GET", "/pair/offer").status, 404)
        self.assertEqual(self.post_json(c, "/pair/explode", {})[0], 404)

    def test_pairing_bodies_are_limited_to_one_kilobyte(self):
        self.pairing.open_window()
        for length, expected in ((1024, 400), (1025, 413)):                     # (400: within the limit, but not a valid offer)
            head = (f"POST /pair/offer HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nX-Desk-Client: t\r\nConnection: close\r\n"
                    f"Content-Length: {length}\r\n\r\n").encode()
            body = b"x" * length if expected == 400 else b""                   # an oversize body is refused before it is read
            self.assertEqual(raw_status(self.port, head + body), expected)


class DevToolsRoutes(HttpCase):
    dev_tools = True

    def test_debug_routes_need_the_same_authentication(self):
        c = self.client()
        r = c.raw_request("POST", "/debug/pair_accept")
        self.assertEqual((r.status, r.json()["error"]), (401, "unpaired"))
        c = self.pair_client()
        self.assertEqual(c.raw_request("POST", "/debug/pair_accept").status, 401)                     # paired, but unsigned
        for action in ("pair_accept", "pair_cancel", "pair_shot"):
            r = c.post(f"/debug/{action}")
            self.assertEqual((r.status, r.json(), r.proof_ok), (200, {"ok": True, "debug": action}, True))
        self.assertEqual(self.mon.debug, ["pair_accept", "pair_cancel", "pair_shot"])
        self.assertEqual(c.post("/debug/not_an_action").status, 404)
        self.assertEqual(self.mon.debug, ["pair_accept", "pair_cancel", "pair_shot"])


class NoDevToolsRoutes(HttpCase):
    def test_debug_routes_do_not_exist_without_the_flag(self):
        c = self.pair_client()
        r = c.post("/debug/pair_accept")
        self.assertEqual((r.status, r.proof_ok), (404, True))
        self.assertEqual(self.mon.debug, [])


class LoopbackExemption(unittest.TestCase):
    """The PC itself (tray UI, development tools) needs no signature - with the real, unpatched decision."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pairing = new_pairing(self.tmp.name, FakeClock())
        self.mon = StubMonitor()
        self.server = make_server(self.mon, 0, pairing=self.pairing, bind="127.0.0.1")
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))

    def test_loopback_needs_no_signature_paired_or_not(self):
        for paired in (False, True):
            if paired:
                self.pairing._store.set_device(H("a1" * 8), K)
            status, headers, body = http_request(self.port, "GET", "/now-playing")
            self.assertEqual((status, json.loads(body)["app"]), (200, "Spotify.exe"))
            self.assertNotIn("x-dm-proof", headers)                                  # nothing to prove: no key was used
            status, _h, body = http_request(self.port, "POST", "/control/next?app=x", {"X-Desk-Client": "t"})
            self.assertEqual(status, 200)
        self.assertEqual(self.mon.calls, [("control", "next", None, "x")] * 2)

    def test_the_decision_for_real_lan_addresses(self):
        lan = ["192.168.1.50", "10.0.0.7", "172.16.4.2", "169.254.10.20", "fe80::1%12", "fd00::5"]
        for addr in lan:
            self.assertTrue(server_mod._client_allowed(addr), addr)                  # served ...
            self.assertFalse(server_mod.is_loopback(addr), addr)
            self.assertTrue(requires_signature(addr, "GET", "/now-playing"), addr)   # ... but only with a signature
            self.assertTrue(requires_signature(addr, "POST", "/control/next"), addr)
            self.assertTrue(requires_signature(addr, "GET", "/artwork"), addr)
            self.assertTrue(requires_signature(addr, "POST", "/hello"), addr)        # only GET /hello is open
            self.assertTrue(requires_signature(addr, "GET", "/pair"), addr)
            for open_route in (("GET", "/hello"), ("GET", "/health"), ("HEAD", "/hello"), ("POST", "/pair/offer"), ("POST", "/pair/reveal"),
                               ("POST", "/pair/confirm"), ("GET", "/pair/status"), ("POST", "/pair/cancel")):
                self.assertFalse(requires_signature(addr, *open_route), (addr, open_route))
        for addr in ("127.0.0.1", "127.1.2.3", "::1", "::ffff:127.0.0.1"):
            self.assertTrue(server_mod.is_loopback(addr), addr)
            self.assertFalse(requires_signature(addr, "GET", "/now-playing"), addr)
            self.assertFalse(requires_signature(addr, "POST", "/debug/screenshot"), addr)
        for addr in ("8.8.8.8", "2001:db8::1", "not an address", ""):
            self.assertFalse(server_mod.is_loopback(addr), addr)
        for addr in ("8.8.8.8", "93.184.216.34", "not an address"):
            self.assertFalse(server_mod._client_allowed(addr), addr)                 # public addresses are not served at all


# ---- 6. discovery and the tray dialog ----------------------------------------------------------------------------------------------------

class BeaconPayload(unittest.TestCase):
    def test_the_beacon_reads_paired_and_pairing_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock()
            p = new_pairing(tmp, clock)
            b = Beacon(8765, 8766, p)                                           # never started: no packet leaves this machine

            def doc():
                raw = b.payload
                self.assertTrue(raw.startswith(b"DESKMEDIA2 "))
                return json.loads(raw[len(b"DESKMEDIA2 "):])
            d = doc()
            self.assertEqual(d, {"v": 2, "port": 8765, "name": "TEST-PC", "id": p.cid_hex, "paired": False, "pairing": False})
            self.assertEqual(list(d), ["v", "port", "name", "id", "paired", "pairing"])
            p.open_window()
            self.assertTrue(doc()["pairing"])
            a = DisplayAttempt()
            to_reveal(p, a)
            p.pc_confirm()
            p.confirm(a.dev.hex(), a.derived.proof_dev.hex())
            d = doc()
            self.assertEqual((d["paired"], d["pairing"]), (True, False))
            p.forget()
            self.assertEqual(doc()["paired"], False)
            clock.advance(1)
            p.open_window(5)
            self.assertTrue(doc()["pairing"])
            clock.advance(6)
            self.assertFalse(doc()["pairing"])

    def test_without_a_pairing_object(self):
        d = json.loads(Beacon(8765, 8766).payload[len(b"DESKMEDIA2 "):])
        self.assertEqual((d["v"], d["paired"], d["pairing"], len(d["id"])), (2, False, False, 16))


class FakePairing:
    def __init__(self) -> None:
        self.snap = {"state": "idle", "code": None, "window_open": True, "seconds_left": 120, "paired": False}
        self.calls: list[str] = []

    def open_window(self, *a):
        self.calls.append("open")

    def snapshot(self):
        return dict(self.snap)

    def pc_confirm(self):
        self.calls.append("confirm")
        return True

    def pc_cancel(self):
        self.calls.append("cancel")
        return True


class PairDialog(unittest.TestCase):
    def test_the_view_for_every_state(self):
        view = ui_mod.pairing_view
        waiting = view({"state": "idle", "window_open": True})
        self.assertEqual((waiting["headline"], waiting["code"], waiting["can_pair"], waiting["done"]),
                         ("Waiting for the display...", "", False, None))
        self.assertIn("pairing prompt", waiting["detail"])
        shown = view({"state": "revealed", "code": "042017", "window_open": True})
        self.assertEqual((shown["code"], shown["can_pair"], shown["done"]), ("042017", True, None))
        self.assertEqual(shown["headline"], "Check that the display shows the same code")
        self.assertTrue(view({"state": "waiting_pc", "code": "1", "window_open": True})["can_pair"])
        self.assertFalse(view({"state": "waiting_device", "code": "1", "window_open": True})["can_pair"])
        self.assertFalse(view({"state": "offered", "window_open": True})["can_pair"])
        for state, message in (("paired", "Paired"), ("cancelled", "Cancelled"), ("expired", "Timed out")):
            self.assertEqual(view({"state": state, "window_open": False})["done"], message)
        self.assertEqual(view({"state": "idle", "window_open": False})["done"], "Timed out")

    def test_the_dialog_drives_a_pairing(self):
        try:
            import tkinter as tk
            root = tk.Tk()
        except Exception as exc:                                                 # no Tk / no desktop in this environment
            self.skipTest(f"tkinter cannot run here: {exc}")
        root.withdraw()
        try:
            fake = FakePairing()
            ui = ui_mod.PairDialog(fake).attach(root)
            self.assertEqual((ui.head.get(), ui.code.get()), ("Waiting for the display...", ""))
            self.assertTrue(ui.pair_button.instate(["disabled"]))
            fake.snap.update(state="revealed", code="123456")
            ui.poll()
            self.assertEqual((ui.head.get(), ui.code.get()), ("Check that the display shows the same code", "123456"))
            self.assertTrue(ui.pair_button.instate(["!disabled"]))
            ui.pair_button.invoke()
            self.assertEqual(fake.calls, ["confirm"])
            fake.snap.update(state="paired", code=None)
            ui.poll()
            self.assertEqual((ui.head.get(), ui.code.get(), ui.state["closing"]), ("Paired", "", True))
        finally:
            root.destroy()

    def test_cancel_and_timeout_close_the_dialog_with_a_message(self):
        try:
            import tkinter as tk
            root = tk.Tk()
        except Exception as exc:
            self.skipTest(f"tkinter cannot run here: {exc}")
        root.withdraw()
        try:
            fake = FakePairing()
            ui = ui_mod.PairDialog(fake).attach(root)
            ui.cancel()
            self.assertEqual((ui.head.get(), fake.calls), ("Cancelled", ["cancel"]))
        finally:
            root.destroy()
        root = tk.Tk()
        root.withdraw()
        try:
            fake = FakePairing()
            ui = ui_mod.PairDialog(fake).attach(root)
            fake.snap.update(window_open=False)
            ui.poll()
            self.assertEqual(ui.head.get(), "Timed out")
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()

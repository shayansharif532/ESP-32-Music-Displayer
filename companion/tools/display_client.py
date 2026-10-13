"""Reference DISPLAY client: talks to the companion exactly as the ESP32 firmware does (docs/protocol.md), over plain http.client.

    from display_client import DisplayClient
    c = DisplayClient("192.168.1.20", 8765)
    c.pair(on_code=lambda code: input(f"code {code} - same on the PC? [y/N] ").lower() == "y")   # the user then clicks Pair on the PC
    c.get_json("/now-playing")                                    # signed, the answer's X-DM-Proof is verified

* pairing: X25519 keys, commitment, offer / reveal, the code, confirm, status polling and the check of the companion's `proof_pc`;
* afterwards every request carries X-DM-Nonce / X-DM-Auth, the nonce comes from the previous answer's X-DM-Next (or GET /hello), one retry
  follows a 401 (its answer carries a fresh nonce), and the X-DM-Proof of each answer is verified (a failing answer raises ProofError,
  as the display would discard it).

Used by the unit tests (tests/test_pairing.py) and by development tools that must talk to the companion like the board does.
`DisplayAttempt` is the network-free half of one pairing attempt, so the protocol test vectors can be checked directly.
Never prints keys, codes or nonces.
"""
from __future__ import annotations

import hmac
import http.client
import json
import os
import secrets
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from desk_media_companion import auth, x25519  # noqa: E402
from desk_media_companion.pairing import Derived, commitment, derive  # noqa: E402


class ProofError(Exception):
    """An answer to a signed request did not carry a valid X-DM-Proof (the firmware discards such answers)."""


class PairingFailed(Exception):
    """The companion refused a pairing step (`status` / `error` as answered) or the attempt ended without a pairing."""

    def __init__(self, status: int, error: str) -> None:
        super().__init__(f"{status} {error}")
        self.status = status
        self.error = error


@dataclass
class Reply:
    status: int
    headers: dict                  # lower-case names
    body: bytes
    proof_ok: Optional[bool] = None    # None: not checked (unsigned request, or a 401 / 429 which carries no proof)

    def json(self) -> dict:
        return json.loads(self.body.decode("utf-8"))


class DisplayAttempt:
    """The display's half of one pairing attempt (no network): fresh X25519 key, nonce and `dev` per attempt unless given (test vectors)."""

    def __init__(self, dev: Optional[bytes] = None, e_priv: Optional[bytes] = None, n_e: Optional[bytes] = None,
                 rand: Callable[[int], bytes] = secrets.token_bytes) -> None:
        self.dev = dev if dev is not None else rand(8)
        self.e_priv = e_priv if e_priv is not None else rand(32)
        self.pub_e = x25519.x25519(self.e_priv, x25519.BASE_POINT)
        self.n_e = n_e if n_e is not None else rand(16)
        self.ce = commitment(self.pub_e, self.n_e)
        self.cid = b""
        self.derived: Optional[Derived] = None

    def offer_body(self) -> dict:
        return {"dev": self.dev.hex(), "pub": self.pub_e.hex(), "ce": self.ce.hex()}

    def reveal_body(self) -> dict:
        return {"dev": self.dev.hex(), "ne": self.n_e.hex()}

    def complete(self, cid: bytes, pub_c: bytes, n_c: bytes) -> Derived:
        """After the companion's offer answer: Z, T, K, the code and both proofs."""
        self.cid = cid
        z = x25519.shared(self.e_priv, pub_c)
        self.derived = derive(cid, self.dev, self.pub_e, pub_c, self.n_e, n_c, z)
        return self.derived

    @property
    def code(self) -> str:
        return self.derived.code if self.derived else ""

    def confirm_body(self) -> dict:
        if self.derived is None:
            raise RuntimeError("complete() first")
        return {"dev": self.dev.hex(), "proof": self.derived.proof_dev.hex()}

    def check_pc_proof(self, proof_hex: str) -> bool:
        """Is `proof_pc` from the companion that holds the same K (constant-time compare)?"""
        return self.derived is not None and isinstance(proof_hex, str) and hmac.compare_digest(
            proof_hex.encode("ascii", "replace"), self.derived.proof_pc.hex().encode("ascii"))


class DisplayClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 8765, key: Optional[bytes] = None, timeout: float = 5.0,
                 rand: Callable[[int], bytes] = secrets.token_bytes) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.key = key                       # K once paired
        self.cid: Optional[bytes] = None
        self._rand = rand
        self._nonce: Optional[str] = None    # the X-DM-Next of the last answer
        self._conn: Optional[http.client.HTTPConnection] = None

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- transport ------------------------------------------------------------------------------------------------------------
    def raw_request(self, method: str, target: str, headers: Optional[dict] = None, body: Optional[bytes] = None) -> Reply:
        """One request exactly as given (no signing); the nonce of the answer is remembered. Reconnects once when a kept-alive
        connection was dropped by the server."""
        hdrs = {"X-Desk-Client": "display"}
        hdrs.update(headers or {})
        for attempt in (1, 2):
            if self._conn is None:
                self._conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
            try:
                self._conn.request(method, target, body=body, headers=hdrs)
                resp = self._conn.getresponse()
                data = resp.read()
                break
            except (http.client.HTTPException, ConnectionError):
                self.close()
                if attempt == 2:
                    raise
        out = {k.lower(): v for k, v in resp.getheaders()}
        if out.get("connection", "").lower() == "close":
            self.close()
        self._nonce = out.get("x-dm-next")
        return Reply(resp.status, out, data)

    def auth_headers(self, method: str, target: str, nonce: str) -> dict:
        if self.key is None:
            raise RuntimeError("not paired")
        return {"X-DM-Nonce": nonce, "X-DM-Auth": auth.request_mac(self.key, nonce, method, target)}

    def fresh_nonce(self) -> str:
        """The nonce for the next signed request: the one the last answer carried, else one from GET /hello."""
        if self._nonce is None:
            self.raw_request("GET", "/hello")
        if self._nonce is None:
            raise RuntimeError("the companion sent no X-DM-Next")
        nonce, self._nonce = self._nonce, None
        return nonce

    def request(self, method: str, target: str, body: Optional[bytes] = None, *, signed: bool = True, retry: bool = True,
                verify: bool = True, headers: Optional[dict] = None) -> Reply:
        """A signed request (signed=False: plain, like a pairing route). One retry after a 401. The answer's X-DM-Proof is checked:
        `verify=True` raises ProofError for a bad / missing proof, `verify=False` only reports it in Reply.proof_ok."""
        hdrs = {"Content-Type": "application/json"} if body is not None else {}
        hdrs.update(headers or {})
        nonce = None
        if signed:
            nonce = self.fresh_nonce()
            hdrs.update(self.auth_headers(method, target, nonce))
        reply = self.raw_request(method, target, hdrs, body)
        if signed and reply.status == 401 and retry:
            return self.request(method, target, body, signed=True, retry=False, verify=verify, headers=headers)
        if signed and self.key is not None and reply.status not in (401, 429):
            expected = auth.response_proof(self.key, nonce or "", reply.status, reply.body)
            got = reply.headers.get("x-dm-proof", "")
            reply.proof_ok = hmac.compare_digest(got.encode("ascii", "replace"), expected.encode("ascii"))
            if verify and not reply.proof_ok:
                raise ProofError(f"answer to {method} {target.split('?')[0]} has no valid X-DM-Proof")
        return reply

    def get(self, target: str, **kw) -> Reply:
        return self.request("GET", target, **kw)

    def post(self, target: str, body: Optional[dict] = None, **kw) -> Reply:
        return self.request("POST", target, None if body is None else json.dumps(body).encode("utf-8"), **kw)

    def get_json(self, target: str, **kw) -> dict:
        return self.get(target, **kw).json()

    def hello(self) -> dict:
        return self.raw_request("GET", "/hello").json()

    # -- pairing -----------------------------------------------------------------------------------------------------------------
    def _pair_post(self, route: str, body: dict) -> dict:
        reply = self.raw_request("POST", route, {"Content-Type": "application/json"}, json.dumps(body).encode("utf-8"))
        data = reply.json()
        if reply.status != 200:
            raise PairingFailed(reply.status, str(data.get("error", "")))
        return data

    def pair(self, on_code: Callable[[str], bool] = lambda code: True, after_confirm: Optional[Callable[[], None]] = None,
             attempt: Optional[DisplayAttempt] = None, timeout_s: float = 10.0, poll_s: float = 0.02) -> DisplayAttempt:
        """The display's side of section 3: offer, reveal, show the code (`on_code(code)` returns True when the user taps Pair), confirm,
        then poll until the companion says "paired" and check its proof. On success K is stored in self.key. Returns the attempt."""
        a = attempt or DisplayAttempt(rand=self._rand)
        offer = self._pair_post("/pair/offer", a.offer_body())
        a.complete(bytes.fromhex(offer["id"]), bytes.fromhex(offer["pub"]), bytes.fromhex(offer["nc"]))
        self._pair_post("/pair/reveal", a.reveal_body())
        if not on_code(a.code):
            self._pair_post("/pair/cancel", {"dev": a.dev.hex()})
            raise PairingFailed(0, "cancelled_by_user")
        self._pair_post("/pair/confirm", a.confirm_body())
        if after_confirm is not None:
            after_confirm()
        deadline = time.monotonic() + timeout_s
        while True:
            reply = self.raw_request("GET", f"/pair/status?dev={a.dev.hex()}")
            data = reply.json()
            if reply.status != 200:
                raise PairingFailed(reply.status, str(data.get("error", "")))
            if data["state"] == "paired":
                if not a.check_pc_proof(data.get("proof", "")):
                    raise ProofError("the companion's pairing proof is wrong")
                if a.derived is None:
                    raise RuntimeError("complete() first")
                self.key, self.cid = a.derived.key, a.cid
                self._nonce = None
                return a
            if data["state"] in ("cancelled", "expired"):
                raise PairingFailed(0, data["state"])
            if time.monotonic() > deadline:
                raise PairingFailed(0, "timeout")
            time.sleep(poll_s)

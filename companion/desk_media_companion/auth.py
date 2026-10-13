"""Request authentication of the HTTP API (docs/protocol.md section 2): single-use nonces, request MACs, response proofs, and a small
per-address failure limiter.

The paired display signs every request with the shared key K over a nonce the companion handed out in its previous answer
(`X-DM-Next`), the method and the request target; the companion signs its answers back (`X-DM-Proof`) so a rogue host cannot feed the
display fake data. Nothing in this module logs: nonces, MACs and keys are never written anywhere.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import re
import secrets
import threading
import time
from collections import OrderedDict, deque
from typing import Callable

NONCE_TTL_S = 60.0
NONCE_MAX_OUTSTANDING = 32
NONCE_PER_OWNER = 8
LIMIT_FAILURES = 10
LIMIT_WINDOW_S = 60.0
LIMIT_BLOCK_S = 60.0
LIMIT_MAX_TRACKED = 256          # addresses remembered by the limiter: memory stays bounded however many hosts probe us

_NONCE = re.compile(r"[0-9a-f]{32}")
_MAC = re.compile(r"[0-9a-f]{64}")


def request_mac(key: bytes, nonce: str, method: str, target: str) -> str:
    """hex(HMAC-SHA256(K, "DMREQ1\\n" + nonce + "\\n" + METHOD + "\\n" + target)); the target is what the request line carries."""
    msg = b"DMREQ1\n" + nonce.encode("ascii") + b"\n" + method.encode("ascii") + b"\n" + target.encode("latin-1", "replace")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def response_proof(key: bytes, nonce: str, status: int, body: bytes) -> str:
    """hex(HMAC-SHA256(K, "DMRESP1\\n" + nonce + "\\n" + status + "\\n" + hex(SHA-256(body)))): binds the answer to its request."""
    msg = b"DMRESP1\n" + nonce.encode("ascii") + b"\n" + str(int(status)).encode("ascii") + b"\n" + hashlib.sha256(body).hexdigest().encode("ascii")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


class NonceStore:
    """Single-use nonces: 16 random bytes as 32 hex characters, valid for `ttl` seconds, at most `limit` outstanding at a time.

    Every answer hands out a nonce, so a host that merely polls /health could push the display's nonce out of a shared pool. Each
    `owner` (the client address) therefore may hold at most `per_owner` of them: a flood only recycles its own."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, ttl: float = NONCE_TTL_S, limit: int = NONCE_MAX_OUTSTANDING,
                 per_owner: int = NONCE_PER_OWNER) -> None:
        self._clock = clock
        self._ttl = ttl
        self._limit = limit
        self._per_owner = per_owner
        self._lock = threading.Lock()
        self._items: OrderedDict[str, tuple[float, str]] = OrderedDict()   # nonce -> (expiry, owner); insertion order = expiry order

    def _purge(self, now: float) -> None:
        while self._items:
            nonce, (expires, _owner) = next(iter(self._items.items()))
            if expires > now:
                break
            del self._items[nonce]

    def issue(self, owner: str = "") -> str:
        with self._lock:
            now = self._clock()
            self._purge(now)
            mine = [n for n, (_e, o) in self._items.items() if o == owner]
            for old in mine[:max(0, len(mine) - self._per_owner + 1)]:
                del self._items[old]                              # this owner's oldest ones make room first
            while len(self._items) >= self._limit:
                self._items.popitem(last=False)                   # then the oldest outstanding nonce of all
            nonce = secrets.token_bytes(16).hex()
            self._items[nonce] = (now + self._ttl, owner)
            return nonce

    def consume(self, nonce: str) -> bool:
        """True exactly once for an issued, unexpired nonce."""
        with self._lock:
            self._purge(self._clock())
            return self._items.pop(nonce, None) is not None

    def __len__(self) -> int:
        with self._lock:
            self._purge(self._clock())
            return len(self._items)


class RateLimiter:
    """Per-address failure limiter: `max_failures` within `window` seconds block that address for `block` seconds."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, max_failures: int = LIMIT_FAILURES, window: float = LIMIT_WINDOW_S,
                 block: float = LIMIT_BLOCK_S, max_tracked: int = LIMIT_MAX_TRACKED) -> None:
        self._clock = clock
        self._max = max_failures
        self._window = window
        self._block = block
        self._cap = max_tracked
        self._lock = threading.Lock()
        self._fails: OrderedDict[str, deque] = OrderedDict()
        self._blocked: OrderedDict[str, float] = OrderedDict()

    def blocked_for(self, addr: str) -> int:
        """Seconds until `addr` may try again (0 = not blocked)."""
        with self._lock:
            until = self._blocked.get(addr)
            if until is None:
                return 0
            left = until - self._clock()
            if left <= 0:
                del self._blocked[addr]
                return 0
            return max(1, math.ceil(left))

    def fail(self, addr: str) -> None:
        with self._lock:
            now = self._clock()
            times = self._fails.pop(addr, None) or deque()
            while times and now - times[0] > self._window:
                times.popleft()
            times.append(now)
            if len(times) >= self._max:
                self._blocked[addr] = now + self._block
                self._blocked.move_to_end(addr)
                times.clear()
            if times:
                self._fails[addr] = times                         # re-inserted last: the OrderedDict stays oldest-first
            self._trim(now)

    def _trim(self, now: float) -> None:
        for addr in [a for a, until in self._blocked.items() if until <= now]:
            del self._blocked[addr]
        while len(self._blocked) > self._cap:
            self._blocked.popitem(last=False)
        for addr in [a for a, t in self._fails.items() if not t or now - t[-1] > self._window]:
            del self._fails[addr]
        while len(self._fails) > self._cap:
            self._fails.popitem(last=False)


class Authenticator:
    """The nonce store and the limiter of one server, plus the check of a signed request."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.nonces = NonceStore(clock=clock)
        self.limiter = RateLimiter(clock=clock)

    def next_nonce(self, owner: str = "") -> str:
        return self.nonces.issue(owner)

    def verify(self, method: str, target: str, nonce: str, mac: str, key: bytes) -> bool:
        """True for a fresh nonce with the right MAC over (nonce, method, target). The nonce is consumed whatever the outcome, so a
        wrong MAC cannot be retried with the same nonce and a captured request cannot be replayed."""
        nonce, mac = (nonce or "").strip().lower(), (mac or "").strip().lower()
        known = _NONCE.fullmatch(nonce) is not None and self.nonces.consume(nonce)
        if not known or _MAC.fullmatch(mac) is None:
            return False
        expected = request_mac(key, nonce, method, target)
        return hmac.compare_digest(expected.encode("ascii"), mac.encode("ascii"))

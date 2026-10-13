"""X25519 (RFC 7748) in pure Python - used once per pairing, never on the request path.

The companion needs a Diffie-Hellman primitive for the pairing handshake (docs/protocol.md). Python's standard library has none and
the project avoids a native dependency for one scalar multiplication per pairing, so this is the textbook Montgomery ladder over
Python integers. It is checked against the RFC 7748 test vectors in tests/test_x25519.py. Not constant-time: the private scalar is
ephemeral (one pairing) and the code runs on the user's own PC, so timing side channels are not part of the threat model.
"""
from __future__ import annotations

import secrets

P = 2 ** 255 - 19
A24 = 121665
BASE_POINT = (9).to_bytes(32, "little")


def _clamp(k: bytes) -> int:
    n = bytearray(k)
    n[0] &= 248
    n[31] &= 127
    n[31] |= 64
    return int.from_bytes(n, "little")


def x25519(scalar: bytes, u: bytes) -> bytes:
    """RFC 7748 X25519(k, u): both arguments and the result are 32 little-endian bytes."""
    if len(scalar) != 32 or len(u) != 32:
        raise ValueError("X25519 needs 32-byte inputs")
    k = _clamp(scalar)
    x1 = int.from_bytes(u, "little") & ((1 << 255) - 1)
    x2, z2, x3, z3, swap = 1, 0, x1, 1, 0
    for t in range(254, -1, -1):
        kt = (k >> t) & 1
        swap ^= kt
        if swap:
            x2, x3 = x3, x2
            z2, z3 = z3, z2
        swap = kt
        a = (x2 + z2) % P
        aa = a * a % P
        b = (x2 - z2) % P
        bb = b * b % P
        e = (aa - bb) % P
        c = (x3 + z3) % P
        d = (x3 - z3) % P
        da = d * a % P
        cb = c * b % P
        x3 = pow(da + cb, 2, P)
        z3 = x1 * pow(da - cb, 2, P) % P
        x2 = aa * bb % P
        z2 = e * (aa + A24 * e) % P
    if swap:
        x2, x3 = x3, x2
        z2, z3 = z3, z2
    return (x2 * pow(z2, P - 2, P) % P).to_bytes(32, "little")


def generate() -> tuple[bytes, bytes]:
    """A fresh ephemeral key pair: (private 32 bytes, public 32 bytes)."""
    private = secrets.token_bytes(32)
    return private, x25519(private, BASE_POINT)


def shared(private: bytes, peer_public: bytes) -> bytes:
    """The DH shared secret. Raises ValueError for a low-order peer key (all-zero result), which would give an attacker a known secret."""
    z = x25519(private, peer_public)
    if z == bytes(32):
        raise ValueError("low-order public key")
    return z

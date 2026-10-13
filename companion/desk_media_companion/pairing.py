"""Pairing of one display with this PC (docs/protocol.md section 3): the key store and the runtime state machine.

* `PairingStore` keeps the companion id (`cid`, 8 random bytes, created at the first start, never secret) and, once a display is
  paired, its id plus the shared 256-bit key. On Windows the file is protected with DPAPI (per-user scope, fixed application entropy):
  it is useless on another account or another PC. Elsewhere (the code stays importable, e.g. for tooling) it is a plain 0600 file.
  A missing, corrupt or undecryptable file simply means "not paired" - it never crashes the companion, and its content is never logged.
* `Pairing` is the thread-safe state machine behind the `/pair/*` routes and the tray dialog: an authenticated key agreement (ephemeral
  X25519 plus a commitment by the display, "numeric comparison" style). It completes only when the user has confirmed on the PC
  *and* the display has sent a correct proof. Time comes from an injectable clock so tests need no sleeping.

Nothing secret (key, code, nonces, proofs) is ever logged: only "window opened/closed", "display paired", "cancelled/expired".
"""
from __future__ import annotations

import ctypes
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import socket
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Callable, NamedTuple, Optional

from . import x25519

log = logging.getLogger("pairing")

WINDOW_SECONDS = 120.0           # how long the window stays open after "Pair a display..." (and after an offer)
REVEAL_SECONDS = 15.0            # the display must reveal its nonce this soon after its offer
FILE_NAME = "pairing.bin"
_MAGIC = b"DMP1"
_SCHEME_DPAPI, _SCHEME_PLAIN = b"D", b"P"
_ENTROPY = b"DeskMediaCompanion/pairing/v1"       # app-specific DPAPI entropy: another program cannot unprotect the file by accident
_FILE_MAX = 4096

# attempt states (the strings are the ones /pair/status reports; "idle" only appears in snapshot())
OFFERED, REVEALED, WAITING_PC, WAITING_DEVICE, PAIRED, CANCELLED, EXPIRED = (
    "offered", "revealed", "waiting_pc", "waiting_device", "paired", "cancelled", "expired")
IN_PROGRESS = (OFFERED, REVEALED, WAITING_PC, WAITING_DEVICE)
HAS_CODE = (REVEALED, WAITING_PC, WAITING_DEVICE)


class PairingError(Exception):
    """A refused pairing request: `status` is the HTTP status, `error` a short fixed code (never request data)."""

    def __init__(self, status: int, error: str) -> None:
        super().__init__(error)
        self.status = status
        self.error = error


# ---- the cryptography of section 3 (shared with the reference display client) ------------------------------------------------

def hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    """RFC 5869 HKDF-Extract with HMAC-SHA256."""
    return hmac.new(salt, ikm, hashlib.sha256).digest()


def hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    """RFC 5869 HKDF-Expand with HMAC-SHA256."""
    if not 0 < length <= 255 * 32:
        raise ValueError("bad HKDF length")
    out, block, counter = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def commitment(pub_e: bytes, n_e: bytes) -> bytes:
    """ce = SHA-256(pubE || nE): the display commits to its nonce before it sees the companion's."""
    return hashlib.sha256(pub_e + n_e).digest()


class Derived(NamedTuple):
    transcript: bytes      # T
    key: bytes             # K
    code: str              # the 6 digits both screens show
    proof_dev: bytes
    proof_pc: bytes


def derive(cid: bytes, dev: bytes, pub_e: bytes, pub_c: bytes, n_e: bytes, n_c: bytes, z: bytes) -> Derived:
    """T, K, the 6-digit code and both proofs from the exchanged values and the DH secret Z (protocol section 3)."""
    t = hashlib.sha256(b"DMPAIR1" + cid + dev + pub_e + pub_c + n_e + n_c).digest()
    prk = hkdf_extract(t, z)
    key = hkdf_expand(prk, b"DMKEY1", 32)
    code = int.from_bytes(hkdf_expand(prk, b"DMSAS1", 4), "big") % 1_000_000
    t_hex = t.hex().encode("ascii")
    return Derived(t, key, f"{code:06d}",
                   hmac.new(key, b"DMPAIR-DEV\n" + t_hex, hashlib.sha256).digest(),
                   hmac.new(key, b"DMPAIR-PC\n" + t_hex, hashlib.sha256).digest())


_HEX = re.compile(r"[0-9a-fA-F]+")


def parse_hex(value: object, nbytes: int) -> bytes:
    """A hex string of exactly nbytes bytes, else PairingError(400): types, length and alphabet are all checked."""
    if not isinstance(value, str) or len(value) != nbytes * 2 or not _HEX.fullmatch(value):
        raise PairingError(400, "bad_request")
    return bytes.fromhex(value)


# ---- DPAPI (Windows) -------------------------------------------------------------------------------------------------------

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    """CryptProtectData / CryptUnprotectData with the user's own scope (no CRYPTPROTECT_LOCAL_MACHINE) and no UI."""
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)      # type: ignore[attr-defined]  (Windows only)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)    # type: ignore[attr-defined]
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    fn.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_uint32, ctypes.POINTER(_Blob)]
    fn.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buf = ctypes.create_string_buffer(data, len(data))
    entropy = ctypes.create_string_buffer(_ENTROPY, len(_ENTROPY))
    blob_in = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_ent = _Blob(len(_ENTROPY), ctypes.cast(entropy, ctypes.POINTER(ctypes.c_char)))
    blob_out = _Blob()
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    if not fn(ctypes.byref(blob_in), None, ctypes.byref(blob_ent), None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out)):
        raise OSError(ctypes.get_last_error(), "DPAPI call failed")      # type: ignore[attr-defined]
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        if not protect:
            ctypes.memset(blob_out.pbData, 0, blob_out.cbData)           # the plaintext key must not linger in freed memory
        kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def _use_dpapi() -> bool:
    return os.name == "nt"


# ---- the store ------------------------------------------------------------------------------------------------------------------

class PairedDevice(NamedTuple):
    id: bytes              # the display's persistent `dev` id (8 bytes): the same value in its USB BEACON and in its /pair/offer
    key: bytes             # K (32 bytes)
    paired_at: int         # unix time


def default_data_dir() -> str:
    return os.environ.get("DESK_MEDIA_DATA_DIR") or os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "DeskMediaCompanion")


class PairingStore:
    """The companion id and the paired display's key, persisted (DPAPI-protected on Windows). Thread-safe."""

    def __init__(self, directory: Optional[str] = None, *, cid: Optional[bytes] = None) -> None:
        """`cid` only seeds a brand-new store (tests); an existing file always wins."""
        self.dir = directory or default_data_dir()
        self.path = os.path.join(self.dir, FILE_NAME)
        self._lock = threading.Lock()
        self._cid = b""
        self._device: Optional[PairedDevice] = None
        self._load(cid)

    @property
    def cid(self) -> bytes:
        return self._cid

    @property
    def device(self) -> Optional[PairedDevice]:
        return self._device

    def set_device(self, dev: bytes, key: bytes) -> None:
        """Replaces the paired display. The in-memory state changes even when the file cannot be written (OSError is raised then)."""
        with self._lock:
            self._device = PairedDevice(bytes(dev), bytes(key), int(time.time()))
            self._save()

    def clear_device(self) -> None:
        """Forgets the paired display (the companion id survives). If the file cannot be rewritten it is removed: a forgotten key
        must not come back after a restart."""
        with self._lock:
            self._device = None
            try:
                self._save()
            except OSError as exc:
                log.warning("could not rewrite the pairing file (%s): removing it", type(exc).__name__)
                try:
                    os.unlink(self.path)
                except FileNotFoundError:
                    pass

    # -- file -----------------------------------------------------------------------------------------------------------------
    def _encode(self) -> bytes:
        doc = {"v": 1, "cid": self._cid.hex(),
               "device": None if self._device is None else
               {"id": self._device.id.hex(), "key": self._device.key.hex(), "paired_at": self._device.paired_at}}
        raw = json.dumps(doc, separators=(",", ":")).encode("ascii")
        if _use_dpapi():
            return _MAGIC + _SCHEME_DPAPI + _dpapi(raw, True)
        return _MAGIC + _SCHEME_PLAIN + raw

    def _decode(self, blob: bytes) -> tuple[bytes, Optional[PairedDevice]]:
        if blob[:4] != _MAGIC:
            raise ValueError("not a pairing file")
        scheme, payload = blob[4:5], blob[5:]
        if scheme == _SCHEME_DPAPI and _use_dpapi():
            raw = _dpapi(payload, False)
        elif scheme == _SCHEME_PLAIN and not _use_dpapi():
            raw = payload
        else:
            raise ValueError("unsupported protection scheme")
        doc = json.loads(raw.decode("ascii"))
        cid = parse_hex(doc["cid"], 8)
        dev = doc.get("device")
        if dev is None:
            return cid, None
        paired_at = dev["paired_at"]
        if not isinstance(paired_at, int) or isinstance(paired_at, bool):
            raise ValueError("bad timestamp")
        return cid, PairedDevice(parse_hex(dev["id"], 8), parse_hex(dev["key"], 32), paired_at)

    def _save(self) -> None:
        data = self._encode()
        os.makedirs(self.dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=FILE_NAME + ".", suffix=".tmp")      # mkstemp: owner-only permissions
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _load(self, seed_cid: Optional[bytes]) -> None:
        fresh = True
        try:
            with open(self.path, "rb") as f:
                blob = f.read(_FILE_MAX + 1)
            fresh = False
            if len(blob) > _FILE_MAX:
                raise ValueError("file too large")
            self._cid, self._device = self._decode(blob)
            return
        except FileNotFoundError:
            pass
        except Exception as exc:                  # corrupt / foreign-account / unreadable: start unpaired, never crash, never log content
            log.warning("pairing data unreadable (%s): starting unpaired", type(exc).__name__)
        self._cid = bytes(seed_cid) if seed_cid is not None else secrets.token_bytes(8)
        self._device = None
        try:
            self._save()
        except OSError as exc:
            log.warning("could not write the pairing file (%s): the companion id is not persisted", type(exc).__name__)
        if fresh:
            log.info("new companion id created")


# ---- the runtime state machine ---------------------------------------------------------------------------------------------------

@dataclass
class _Attempt:
    dev: bytes
    pub_e: bytes
    ce: bytes
    priv_c: Optional[bytes]            # dropped as soon as Z is derived
    pub_c: bytes
    n_c: bytes
    offered_at: float
    state: str = OFFERED
    n_e: bytes = b""
    derived: Optional[Derived] = None  # dropped when the attempt ends (a paired attempt keeps only proof_pc)
    pc_confirmed: bool = False
    proof_pc: bytes = b""


class Pairing:
    """One lock guards everything. The window is opened by the user (`open_window`), one attempt runs at a time."""

    def __init__(self, store: PairingStore, name: Optional[str] = None, clock: Callable[[], float] = time.monotonic,
                 rand: Callable[[int], bytes] = secrets.token_bytes,
                 keygen: Callable[[], tuple[bytes, bytes]] = x25519.generate) -> None:
        self._store = store
        self.name = (name if name is not None else socket.gethostname())[:63]
        self._clock = clock
        self._rand = rand
        self._keygen = keygen            # (private, public); injectable so the protocol test vectors can run through the real object
        self._lock = threading.RLock()
        self._window_until = -math.inf
        self._window_len = WINDOW_SECONDS
        self._window_logged = False
        self._attempt: Optional[_Attempt] = None

    # -- read access -------------------------------------------------------------------------------------------------------------
    @property
    def cid(self) -> bytes:
        return self._store.cid

    @property
    def cid_hex(self) -> str:
        return self._store.cid.hex()

    @property
    def paired(self) -> bool:
        return self._store.device is not None

    def key(self) -> Optional[bytes]:
        """The paired display's key (for request authentication), or None."""
        dev = self._store.device
        return dev.key if dev is not None else None

    @property
    def device_id(self) -> Optional[bytes]:
        """The paired display's persistent `dev` id (8 bytes; the one its USB BEACON and its /pair/offer carry), or None."""
        dev = self._store.device
        return dev.id if dev is not None else None

    def paired_with(self, dev_id: bytes) -> bool:
        """Is the paired display the one with this `dev` id? (constant-time compare; False when nothing is paired)"""
        mine = self.device_id
        return mine is not None and bool(dev_id) and hmac.compare_digest(mine, bytes(dev_id))

    @property
    def window_open(self) -> bool:
        with self._lock:
            self._tick()
            return self._clock() < self._window_until

    def snapshot(self) -> dict:
        """For the tray dialog: state, the code (only once it exists), the display's short id, seconds left, the PC's name."""
        with self._lock:
            self._tick()
            now = self._clock()
            a = self._attempt
            dev = self._store.device
            short = a.dev.hex()[:8] if a is not None else (dev.id.hex()[:8] if dev is not None else "")
            return {"state": a.state if a is not None else "idle",
                    "code": a.derived.code if (a is not None and a.state in HAS_CODE and a.derived is not None) else None,
                    "device": short, "name": self.name, "paired": dev is not None,
                    "window_open": now < self._window_until,
                    "seconds_left": max(0, math.ceil(self._window_until - now)) if now < self._window_until else 0}

    # -- the user's side ---------------------------------------------------------------------------------------------------------
    def open_window(self, seconds: float = WINDOW_SECONDS) -> None:
        """"Pair a display...": accept pairing offers for `seconds` (a new window ends a finished attempt, never one in progress)."""
        with self._lock:
            self._tick()
            a = self._attempt
            running = a is not None and a.state in IN_PROGRESS
            if a is not None and not running:
                self._attempt = None
            self._window_len = float(seconds)
            until = self._clock() + self._window_len
            self._window_until = max(self._window_until, until) if running else until
            if not self._window_logged:
                self._window_logged = True
                log.info("pairing window opened (%d s)", int(seconds))

    def pc_confirm(self) -> bool:
        """The user clicked Pair on the PC. True when the confirmation is recorded (the pairing completes once the display's proof is in)."""
        with self._lock:
            self._tick()
            a = self._attempt
            if a is None or a.state not in (REVEALED, WAITING_PC, WAITING_DEVICE):
                return False
            a.pc_confirmed = True
            if a.state == WAITING_PC:
                self._complete(a)
            else:
                a.state = WAITING_DEVICE
            return True

    def pc_cancel(self) -> bool:
        """The user clicked Cancel (or closed the dialog): drops the attempt and closes the window. True if something was running/open."""
        with self._lock:
            self._tick()
            a = self._attempt
            was_open = self._clock() < self._window_until
            if a is not None and a.state in IN_PROGRESS:
                self._end(a, CANCELLED)
                return True
            if was_open:
                self._close_window()
            return was_open

    def forget(self) -> bool:
        """"Forget paired display": the key is deleted at once, so the old display is locked out immediately. True if one was paired."""
        with self._lock:
            was = self._store.device is not None
            self._store.clear_device()
            a = self._attempt
            if a is not None and a.state in IN_PROGRESS:
                self._end(a, CANCELLED)
            self._attempt = None
            self._close_window()
            if was:
                log.info("paired display forgotten")
            return was

    # -- the display's side (HTTP routes) --------------------------------------------------------------------------------------
    def offer(self, dev_hex: object, pub_hex: object, ce_hex: object) -> dict:
        dev, pub_e, ce = parse_hex(dev_hex, 8), parse_hex(pub_hex, 32), parse_hex(ce_hex, 32)
        with self._lock:
            self._tick()
            now = self._clock()
            if now >= self._window_until:
                raise PairingError(403, "pairing_closed")
            a = self._attempt
            if a is not None and a.state in IN_PROGRESS:
                if a.state == OFFERED and hmac.compare_digest(a.dev, dev) and a.pub_e == pub_e and a.ce == ce:
                    return self._offer_reply(a)                  # the display repeats an offer whose answer got lost
                raise PairingError(409, "busy")
            priv_c, pub_c = self._keygen()
            a = _Attempt(dev=dev, pub_e=pub_e, ce=ce, priv_c=priv_c, pub_c=pub_c, n_c=self._rand(16), offered_at=now)
            self._attempt = a
            self._window_until = max(self._window_until, now + self._window_len)
            return self._offer_reply(a)

    def reveal(self, dev_hex: object, ne_hex: object) -> dict:
        dev, n_e = parse_hex(dev_hex, 8), parse_hex(ne_hex, 16)
        with self._lock:
            self._tick()
            a = self._attempt_for(dev)
            if a.state in HAS_CODE and a.n_e == n_e:
                return {"ok": True, "state": a.state}            # repeated reveal
            if a.state != OFFERED:
                raise PairingError(409, EXPIRED if a.state == EXPIRED else "bad_state")
            if not hmac.compare_digest(commitment(a.pub_e, n_e), a.ce):
                self._end(a, CANCELLED)
                raise PairingError(403, "bad_commit")
            try:
                z = x25519.shared(a.priv_c or b"", a.pub_e)
            except ValueError:                                   # all-zero secret: a low-order key, never a real display
                self._end(a, CANCELLED)
                raise PairingError(400, "bad_key") from None
            a.n_e = n_e
            a.derived = derive(self.cid, a.dev, a.pub_e, a.pub_c, n_e, a.n_c, z)
            a.priv_c = None
            a.state = REVEALED
            return {"ok": True, "state": REVEALED}

    def confirm(self, dev_hex: object, proof_hex: object) -> dict:
        dev, proof = parse_hex(dev_hex, 8), parse_hex(proof_hex, 32)
        with self._lock:
            self._tick()
            a = self._attempt_for(dev)
            if a.state in (WAITING_PC, PAIRED):
                return {"ok": True, "state": a.state}            # repeated confirm
            if a.state not in (REVEALED, WAITING_DEVICE) or a.derived is None:
                raise PairingError(409, EXPIRED if a.state == EXPIRED else "bad_state")
            if not hmac.compare_digest(proof, a.derived.proof_dev):
                self._end(a, CANCELLED)
                raise PairingError(403, "bad_proof")
            if a.pc_confirmed:
                self._complete(a)
            else:
                a.state = WAITING_PC
            return {"ok": True, "state": a.state}

    def status(self, dev_hex: object) -> dict:
        dev = parse_hex(dev_hex, 8)
        with self._lock:
            self._tick()
            a = self._attempt_for(dev)
            out: dict = {"ok": True, "state": a.state}
            if a.state == PAIRED:
                out["proof"] = a.proof_pc.hex()
            return out

    def cancel(self, dev_hex: object) -> dict:
        dev = parse_hex(dev_hex, 8)
        with self._lock:
            self._tick()
            a = self._attempt_for(dev)
            if a.state in IN_PROGRESS:
                self._end(a, CANCELLED)
            return {"ok": True, "state": a.state}

    # -- internals (lock held) -----------------------------------------------------------------------------------------------
    def _offer_reply(self, a: _Attempt) -> dict:
        return {"ok": True, "pub": a.pub_c.hex(), "nc": a.n_c.hex(), "name": self.name, "id": self.cid_hex}

    def _attempt_for(self, dev: bytes) -> _Attempt:
        a = self._attempt
        if a is None or not hmac.compare_digest(a.dev, dev):
            raise PairingError(404, "unknown")
        return a

    def _tick(self) -> None:
        """Applies the time limits (lazily: every public call and the beacon's poll run it)."""
        now = self._clock()
        a = self._attempt
        if a is not None:
            if a.state == OFFERED and now - a.offered_at > REVEAL_SECONDS:
                self._end(a, EXPIRED)
            elif a.state in IN_PROGRESS and now >= self._window_until:
                self._end(a, EXPIRED)
        if self._window_logged and now >= self._window_until:
            self._window_logged = False
            log.info("pairing window closed")

    def _close_window(self) -> None:
        self._window_until = -math.inf
        if self._window_logged:
            self._window_logged = False
            log.info("pairing window closed")

    def _end(self, a: _Attempt, state: str) -> None:
        """Drops an attempt: secrets go, a cancel (either side, or a failed check) also closes the window - a retry needs the user again."""
        a.state = state
        a.priv_c = None
        a.derived = None
        if state == CANCELLED:
            log.info("pairing cancelled")
            self._close_window()
        else:
            log.info("pairing expired")

    def _complete(self, a: _Attempt) -> None:
        if a.derived is None:
            return
        try:
            self._store.set_device(a.dev, a.derived.key)
        except OSError as exc:                                   # paired for this run anyway; the user can re-pair after a restart
            log.warning("could not store the pairing (%s): it will not survive a restart", type(exc).__name__)
        a.proof_pc = a.derived.proof_pc
        a.derived = None
        a.priv_c = None
        a.state = PAIRED
        self._close_window()
        log.info("display paired")

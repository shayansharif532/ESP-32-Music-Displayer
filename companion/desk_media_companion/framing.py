"""DMLINK v1 frame codec (docs/usb-protocol.md sections 2 and 3): pure functions and a streaming parser, no I/O, no threads.

    frame = MAGIC(2) VER TYPE FLAGS SEQ ID(2) LEN(2) HCRC(2) PAYLOAD(LEN) CRC32(4)        all integers little endian

* `encode_frame` / `encode_response` / `encode_request` build frames; a response body is cut into chunks of at most 4096 payload bytes.
* `FrameParser` is the NORMATIVE receiver of the specification: it finds frames in a byte stream that may contain boot-banner text,
  truncated frames, corrupted bytes and floods of garbage. It never raises, never grows beyond a fixed buffer and counts every error
  class. A false start costs ONE byte (never the data behind it), so the next intact frame is always found.
* Time (the 1 s limit for a partial frame) comes from an injectable clock, so tests never sleep.

The module is independent of everything else in the companion (and of WinRT / pyserial), so it can be tested exhaustively.
"""
from __future__ import annotations

import binascii
import struct
import time
import zlib
from dataclasses import dataclass, asdict
from typing import Callable, NamedTuple, Optional

MAGIC = b"\xfa\xde"
VER = 1
HEADER_LEN = 12                         # MAGIC .. HCRC
CRC_LEN = 4
MAX_PAYLOAD = 4096
MAX_FRAME = HEADER_LEN + MAX_PAYLOAD + CRC_LEN
MAX_BUFFER = 64 * 1024                  # receive buffer cap (section 7)
PARTIAL_TIMEOUT_S = 1.0                 # a partial frame that does not complete within this is abandoned (section 2, rule 5)
MAX_RESPONSE_BODY = 512 * 1024          # section 5: the largest body a RESPONSE may announce

FLAG_FIRST = 0x01
FLAG_LAST = 0x02
FLAG_SINGLE = FLAG_FIRST | FLAG_LAST

TYPE_BEACON = 0x01
TYPE_ACK = 0x02
TYPE_REQUEST = 0x10
TYPE_RESPONSE = 0x11
TYPE_LOG = 0x20
TYPE_PING = 0x30
TYPE_PONG = 0x31
KNOWN_TYPES = frozenset((TYPE_BEACON, TYPE_ACK, TYPE_REQUEST, TYPE_RESPONSE, TYPE_LOG, TYPE_PING, TYPE_PONG))

_HEAD_NO_CRC = struct.Struct("<2sBBBBHH")        # MAGIC VER TYPE FLAGS SEQ ID LEN (10 bytes), HCRC follows
_HEAD_FIELDS = struct.Struct("<BBBBHHH")         # VER TYPE FLAGS SEQ ID LEN HCRC, read from just after the magic
_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")
_RESPONSE_HEAD = struct.Struct("<HI")            # u16 status, u32 total: the first six bytes of a response's first chunk
RESPONSE_HEAD_LEN = _RESPONSE_HEAD.size


def crc16_ccitt(data: bytes, init: int = 0xFFFF) -> int:
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflection, no final xor); b"123456789" -> 0x29B1.
    `binascii.crc_hqx` is exactly this algorithm with a chosen start value, and it runs in C."""
    return binascii.crc_hqx(data, init)


def crc32(data: bytes) -> int:
    """CRC-32 / IEEE 802.3 (the zlib one); b"123456789" -> 0xCBF43926."""
    return zlib.crc32(data) & 0xFFFFFFFF


class Frame(NamedTuple):
    """A frame the parser accepted (VER 1, both CRCs correct)."""
    type: int
    flags: int
    seq: int
    msg_id: int
    payload: bytes

    @property
    def first(self) -> bool:
        return bool(self.flags & FLAG_FIRST)

    @property
    def last(self) -> bool:
        return bool(self.flags & FLAG_LAST)

    @property
    def single(self) -> bool:
        return (self.flags & FLAG_SINGLE) == FLAG_SINGLE


# ---- encoding ---------------------------------------------------------------------------------------------------------------------

def encode_frame(ftype: int, payload: bytes = b"", *, msg_id: int = 0, flags: int = FLAG_SINGLE, seq: int = 0, ver: int = VER) -> bytes:
    """One frame as bytes (a sender writes it with a single `write` call). ValueError for arguments that cannot be encoded: this is
    the sender's own programming contract, not untrusted input."""
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("payload longer than 4096 bytes")
    if not (0 <= ftype <= 0xFF and 0 <= flags <= 0xFF and 0 <= ver <= 0xFF and 0 <= msg_id <= 0xFFFF):
        raise ValueError("field out of range")
    head = _HEAD_NO_CRC.pack(MAGIC, ver, ftype, flags, seq & 0xFF, msg_id, len(payload))
    head += _U16.pack(crc16_ccitt(head))
    body = head + bytes(payload)
    return body + _U32.pack(crc32(body))


def encode_request(msg_id: int, method: str, target: str, body: bytes = b"") -> bytes:
    """A REQUEST frame: `METHOD SP target LF [body]` in one frame (section 5)."""
    payload = method.encode("ascii") + b" " + target.encode("latin-1") + b"\n" + bytes(body)
    return encode_frame(TYPE_REQUEST, payload, msg_id=msg_id)


def encode_response(msg_id: int, status: int, body: bytes, *, max_payload: int = MAX_PAYLOAD) -> list[bytes]:
    """The frames of one RESPONSE (section 5): the first carries `u16 status, u32 total` and the first body bytes, the following ones
    only body bytes; FIRST on the first chunk, LAST on the last, SEQ counts the chunks (wrapping at 256), every chunk has the request's
    ID. `max_payload` (default 4096) exists so the specification's two-chunk test vector can be reproduced with tiny chunks.
    ValueError for a body over 512 KB: the caller decides what to answer instead (an error response)."""
    if not (0 <= status <= 0xFFFF):
        raise ValueError("status out of range")
    if len(body) > MAX_RESPONSE_BODY:
        raise ValueError("response body over 512 KB")
    if not (RESPONSE_HEAD_LEN < max_payload <= MAX_PAYLOAD):
        raise ValueError("max_payload out of range")
    frames: list[bytes] = []
    first_room = max_payload - RESPONSE_HEAD_LEN
    chunks = [bytes(body[:first_room])]
    pos = first_room
    while pos < len(body):
        chunks.append(bytes(body[pos:pos + max_payload]))
        pos += max_payload
    last = len(chunks) - 1
    for seq, data in enumerate(chunks):
        flags = (FLAG_FIRST if seq == 0 else 0) | (FLAG_LAST if seq == last else 0)
        payload = (_RESPONSE_HEAD.pack(status, len(body)) + data) if seq == 0 else data
        frames.append(encode_frame(TYPE_RESPONSE, payload, msg_id=msg_id, flags=flags, seq=seq))
    return frames


def split_response_head(payload: bytes) -> Optional[tuple[int, int, bytes]]:
    """(status, total, first body bytes) of a RESPONSE's first chunk, or None when it is shorter than six bytes."""
    if len(payload) < RESPONSE_HEAD_LEN:
        return None
    status, total = _RESPONSE_HEAD.unpack_from(payload, 0)
    return status, total, payload[RESPONSE_HEAD_LEN:]


# ---- the receiver --------------------------------------------------------------------------------------------------------------------

@dataclass
class ParserStats:
    """One counter per error class (docs/usb-protocol.md section 2); `frames` counts accepted frames."""
    frames: int = 0
    noise_bytes: int = 0          # bytes before a magic, handed to the noise callback
    bad_hcrc: int = 0             # false starts: header CRC wrong
    bad_len: int = 0              # header CRC right but LEN > 4096
    bad_crc: int = 0              # whole-frame CRC-32 wrong
    unknown_ver: int = 0          # valid header with VER != 1: skipped whole
    partial_timeouts: int = 0     # an incomplete frame abandoned after 1 s
    overflow_dropped: int = 0     # oldest bytes dropped because the buffer cap was reached
    callback_errors: int = 0      # the noise callback raised (swallowed)
    internal_errors: int = 0      # should stay 0: the parser's own safety net fired

    def as_dict(self) -> dict:
        return asdict(self)


class FrameParser:
    """Streaming receiver of docs/usb-protocol.md section 2. Feed it whatever the port returned; it returns the frames completed by that
    data. Not thread-safe: one reader owns it.

    Why the rules look the way they do: a frame boundary is only a hint (`FA DE` may occur in garbage), so a candidate that fails a
    check never costs more than its first byte, and the data behind it is scanned again. Nothing is ever lost by a false start.
    A candidate that waits for bytes that never come (the sender died mid-frame) is abandoned after `partial_timeout` seconds; the
    timer is evaluated whenever `feed` is called (call `feed(b"")` on a read timeout), so a silent line cannot keep it from firing.
    """

    def __init__(self, on_noise: Optional[Callable[[bytes], None]] = None, clock: Callable[[], float] = time.monotonic, *,
                 max_buffer: int = MAX_BUFFER, partial_timeout: float = PARTIAL_TIMEOUT_S) -> None:
        self.stats = ParserStats()
        self._on_noise = on_noise
        self._clock = clock
        self._max_buffer = max(1, int(max_buffer))
        self._timeout = float(partial_timeout)
        self._buf = bytearray()
        self._wait_since: Optional[float] = None     # when the candidate at the head of the buffer started waiting for bytes

    def __len__(self) -> int:
        """Bytes currently buffered (a partial frame, or a trailing 0xFA that may begin a magic)."""
        return len(self._buf)

    def reset(self) -> None:
        self._buf.clear()
        self._wait_since = None

    def feed(self, data: bytes = b"") -> list[Frame]:
        """Consume `data`; return the frames it completed. Never raises, whatever `data` is."""
        out: list[Frame] = []
        try:
            raw = bytes(data)
        except (TypeError, ValueError):                     # not bytes-like at all: nothing to parse, the buffered bytes are untouched
            self.stats.internal_errors += 1
            raw = b""
        try:
            self._feed(raw, out)
        except Exception:                                   # a safety net, not a feature: counted, state reset, parsing continues
            self.stats.internal_errors += 1
            self._buf.clear()
            self._wait_since = None
        return out

    # -- internals -------------------------------------------------------------------------------------------------------------------
    def _feed(self, data: bytes, out: list[Frame]) -> None:
        buf = self._buf
        now = self._clock()
        if self._wait_since is not None and now - self._wait_since > self._timeout:
            self.stats.partial_timeouts += 1                # rule 5: abandon the stale candidate (discard one byte), then rescan
            del buf[:1]
            self._wait_since = None
        pos = 0
        while True:
            if pos < len(data):
                room = self._max_buffer - len(buf)
                if room <= 0:                               # cannot happen with the 64 KB cap (a candidate needs at most 4112 bytes),
                    self.stats.overflow_dropped += 1        # but the rule is: the oldest byte goes
                    del buf[:1]
                    self._wait_since = None
                    continue
                chunk = data[pos:pos + room]
                pos += len(chunk)
                buf += chunk
            self._scan(out, now)
            if pos >= len(data):
                return

    def _noise(self, data: bytes) -> None:
        self.stats.noise_bytes += len(data)
        if self._on_noise is not None:
            try:
                self._on_noise(data)
            except Exception:
                self.stats.callback_errors += 1

    def _scan(self, out: list[Frame], now: float) -> None:
        buf = self._buf
        stats = self.stats
        n = len(buf)
        pos = 0
        while True:
            i = buf.find(MAGIC, pos)
            if i < 0:                                       # rule 1: no magic left; a trailing 0xFA may be the start of the next one
                end = n - 1 if (n > pos and buf[n - 1] == MAGIC[0]) else n
                if end > pos:
                    self._noise(bytes(buf[pos:end]))
                pos = end
                self._wait_since = None
                break
            if i > pos:
                self._noise(bytes(buf[pos:i]))
                pos = i
            avail = n - pos
            if avail < HEADER_LEN:                          # rule 2: wait for the rest of the header
                self._wait(now)
                break
            ver, ftype, flags, seq, msg_id, length, hcrc = _HEAD_FIELDS.unpack_from(buf, pos + 2)
            if crc16_ccitt(buf[pos:pos + 10]) != hcrc:        # rule 3: a false start costs one byte
                stats.bad_hcrc += 1
                pos += 1
                self._wait_since = None
                continue
            if length > MAX_PAYLOAD:
                stats.bad_len += 1
                pos += 1
                self._wait_since = None
                continue
            total = HEADER_LEN + length + CRC_LEN
            if ver != VER:                                  # rule 4: a newer link version: skip the whole frame, keep going
                if avail < total:
                    self._wait(now)
                    break
                stats.unknown_ver += 1
                pos += total
                self._wait_since = None
                continue
            if avail < total:                               # rule 5: wait for the payload (the timer is checked at the next feed)
                self._wait(now)
                break
            body_end = pos + HEADER_LEN + length
            if _U32.unpack_from(buf, body_end)[0] != crc32(buf[pos:body_end]):      # rule 6
                stats.bad_crc += 1
                pos += 1
                self._wait_since = None
                continue
            out.append(Frame(ftype, flags, seq, msg_id, bytes(buf[pos + HEADER_LEN:body_end])))     # rule 7
            stats.frames += 1
            pos += total
            self._wait_since = None
        del buf[:pos]

    def _wait(self, now: float) -> None:
        if self._wait_since is None:
            self._wait_since = now

"""DMLINK v1 frame codec: the specification's test vectors byte for byte, the normative receiver rule by rule, and a seeded fuzz run.

    companion\\.venv\\Scripts\\python.exe -m unittest discover -s companion/tests

The fuzz size can be changed with DM_FUZZ_STREAMS (default 200000).
"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from desk_media_companion import framing as fr  # noqa: E402
from desk_media_companion.framing import (FLAG_FIRST, FLAG_LAST, FLAG_SINGLE, HEADER_LEN, MAX_BUFFER, MAX_PAYLOAD,  # noqa: E402
                                          TYPE_ACK, TYPE_BEACON, TYPE_LOG, TYPE_PING, TYPE_PONG, TYPE_REQUEST, TYPE_RESPONSE,
                                          Frame, FrameParser, crc16_ccitt, crc32, encode_frame, encode_request, encode_response)

H = bytes.fromhex

# ---- docs/usb-protocol.md section 10 (the PONG line there carries a stray trailing "16", the frame is 16 bytes: see test_the_pong_vector) --
V_PING = H("fa de 01 30 03 00 07 00 05 00 c9 f0 68 65 6c 6c 6f c0 c9 b3 e1".replace(" ", ""))
V_REQUEST = H("fa de 01 10 03 00 01 00 11 00 11 76 47 45 54 20 2f 6e 6f 77 2d 70 6c 61 79 69 6e 67 0a 64 50 41 b9".replace(" ", ""))
V_RESPONSE = H("fa de 01 11 03 00 01 00 11 00 70 ce c8 00 0b 00 00 00 7b 22 6f 6b 22 3a 74 72 75 65 7d 48 39 aa 33".replace(" ", ""))
V_BEACON = H("fa de 01 01 03 00 00 00 0f 00 c3 af 7b 22 76 22 3a 31 2c 22 61 70 69 22 3a 32 7d 2e 27 e7 37".replace(" ", ""))
V_CHUNK1 = H("fa de 01 11 01 00 09 00 09 00 29 4a c8 00 06 00 00 00 61 62 63 7a bc ca 83".replace(" ", ""))
V_CHUNK2 = H("fa de 01 11 02 01 09 00 03 00 53 c1 64 65 66 3d 27 ed b5".replace(" ", ""))
V_PONG = H("fa de 01 31 03 00 00 00 00 00 70 e6 50 85 17 b6".replace(" ", ""))

PING = Frame(TYPE_PING, 3, 0, 7, b"hello")


class FakeClock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def feed_all(parser: FrameParser, data: bytes, chunk: int = 0) -> list:
    """Frames from feeding `data` in one piece (chunk=0) or in pieces of `chunk` bytes."""
    if not chunk:
        return parser.feed(data)
    out = []
    for i in range(0, len(data), chunk):
        out += parser.feed(data[i:i + chunk])
    return out


class Vectors(unittest.TestCase):
    def test_crc_check_values(self):
        self.assertEqual(crc16_ccitt(b"123456789"), 0x29B1)
        self.assertEqual(crc32(b"123456789"), 0xCBF43926)
        self.assertEqual(crc16_ccitt(b""), 0xFFFF)
        self.assertEqual(crc32(b""), 0)

    def test_encoder_reproduces_every_vector_byte_for_byte(self):
        self.assertEqual(encode_frame(TYPE_PING, b"hello", msg_id=7), V_PING)
        self.assertEqual(encode_request(1, "GET", "/now-playing"), V_REQUEST)
        self.assertEqual(encode_response(1, 200, b'{"ok":true}'), [V_RESPONSE])
        self.assertEqual(encode_frame(TYPE_BEACON, b'{"v":1,"api":2}'), V_BEACON)
        self.assertEqual(encode_response(9, 200, b"abcdef", max_payload=9), [V_CHUNK1, V_CHUNK2])
        self.assertEqual(encode_frame(TYPE_PONG), V_PONG)

    def test_parser_decodes_every_vector(self):
        expected = [PING,
                    Frame(TYPE_REQUEST, 3, 0, 1, b"GET /now-playing\n"),
                    Frame(TYPE_RESPONSE, 3, 0, 1, b"\xc8\x00\x0b\x00\x00\x00" + b'{"ok":true}'),
                    Frame(TYPE_BEACON, 3, 0, 0, b'{"v":1,"api":2}'),
                    Frame(TYPE_RESPONSE, FLAG_FIRST, 0, 9, b"\xc8\x00\x06\x00\x00\x00abc"),
                    Frame(TYPE_RESPONSE, FLAG_LAST, 1, 9, b"def"),
                    Frame(TYPE_PONG, 3, 0, 0, b"")]
        stream = V_PING + V_REQUEST + V_RESPONSE + V_BEACON + V_CHUNK1 + V_CHUNK2 + V_PONG
        p = FrameParser()
        self.assertEqual(p.feed(stream), expected)
        self.assertEqual((p.stats.frames, p.stats.noise_bytes, len(p)), (7, 0, 0))
        for one in (V_PING, V_REQUEST, V_RESPONSE, V_BEACON, V_CHUNK1, V_CHUNK2, V_PONG):       # each alone, and one byte at a time
            self.assertEqual(len(FrameParser().feed(one)), 1)
            self.assertEqual(len(feed_all(FrameParser(), one, 1)), 1)

    def test_the_pong_vector(self):
        """The doc prints the PONG as 17 bytes: the final `16` is the frame length (16) pasted after the frame. The real frame is the
        first 16 bytes, which is what the encoder produces; read literally, the stray byte is one byte of noise after a valid frame."""
        self.assertEqual(len(V_PONG), 16)
        noise = []
        p = FrameParser(on_noise=noise.append)
        self.assertEqual(p.feed(V_PONG + b"\x16"), [Frame(TYPE_PONG, 3, 0, 0, b"")])
        self.assertEqual(b"".join(noise), b"\x16")

    def test_frame_properties(self):
        f = FrameParser().feed(V_CHUNK1)[0]
        self.assertEqual((f.first, f.last, f.single), (True, False, False))
        f = FrameParser().feed(V_CHUNK2)[0]
        self.assertEqual((f.first, f.last, f.single), (False, True, False))
        self.assertTrue(FrameParser().feed(V_PING)[0].single)


class CorruptionAndResync(unittest.TestCase):
    def test_the_spec_sentence_one_flipped_byte_then_the_vector_again_yields_exactly_the_second_frame(self):
        """docs section 10: a stream with one flipped byte of the vector, then the vector again, must yield exactly the second frame
        - for EVERY position and EVERY bit of the first copy."""
        for pos in range(len(V_PING)):
            for bit in range(8):
                bad = bytearray(V_PING)
                bad[pos] ^= 1 << bit
                self.assertEqual(FrameParser().feed(bytes(bad) + V_PING), [PING], (pos, bit))
        for pos in range(len(V_PING)):                      # and whole-byte replacements, fed byte by byte
            for value in (0x00, 0xFF, 0xFA, 0xDE):
                bad = bytearray(V_PING)
                bad[pos] = value
                if bytes(bad) != V_PING:
                    self.assertEqual(feed_all(FrameParser(), bytes(bad) + V_PING, 1), [PING], (pos, value))

    def test_counters_name_the_error_class(self):
        p = FrameParser()
        bad_payload = bytearray(V_PING)
        bad_payload[13] ^= 0x01                              # payload byte: header fine, CRC-32 wrong
        p.feed(bytes(bad_payload))
        self.assertEqual((p.stats.bad_crc, p.stats.bad_hcrc, p.stats.frames), (1, 0, 0))
        bad_header = bytearray(V_PING)
        bad_header[4] ^= 0x01                                # FLAGS: the header CRC no longer matches
        p.feed(bytes(bad_header))
        self.assertEqual(p.stats.bad_hcrc, 1)
        self.assertEqual(p.feed(V_PING), [PING])

    def test_glued_frames(self):
        frames = [encode_frame(TYPE_LOG, bytes([i]) * i, msg_id=i) for i in range(1, 40)]
        got = FrameParser().feed(b"".join(frames))
        self.assertEqual([f.msg_id for f in got], list(range(1, 40)))
        got = feed_all(FrameParser(), b"".join(frames), 7)
        self.assertEqual([f.payload for f in got], [bytes([i]) * i for i in range(1, 40)])

    def test_byte_at_a_time_feeding_of_a_noisy_stream(self):
        banner = b"ESP-ROM:esp32s3-20210327\r\nBuild:Mar 27 2021\r\n"
        stream = banner + V_BEACON + b"junk\xfa" + V_PING + b"\xfa\xfa\xde\x00" + V_RESPONSE
        noise = []
        p = FrameParser(on_noise=noise.append)
        got = feed_all(p, stream, 1)
        self.assertEqual([f.type for f in got], [TYPE_BEACON, TYPE_PING, TYPE_RESPONSE])
        self.assertTrue(b"".join(noise).startswith(banner))

    def test_truncated_frame_then_valid_frames_costs_only_the_truncated_one(self):
        """Cut the RESPONSE vector after every possible byte and glue valid frames behind it: all of them are found, nothing is lost
        (a false start discards one byte and the data behind it is scanned again)."""
        want = [PING, PING, Frame(TYPE_BEACON, 3, 0, 0, b'{"v":1,"api":2}'), PING]
        for cut in range(1, len(V_RESPONSE)):
            stream = V_RESPONSE[:cut] + V_PING + V_PING + V_BEACON + V_PING
            self.assertEqual(FrameParser().feed(stream), want, cut)
            self.assertEqual(feed_all(FrameParser(), stream, 5), want, cut)

    def test_a_truncated_frame_followed_by_only_one_valid_frame_is_recovered_after_the_one_second_limit(self):
        clock = FakeClock()
        p = FrameParser(clock=clock)
        long_cut = encode_frame(TYPE_LOG, b"x" * 400)[:100]          # claims 400 bytes, only 88 payload bytes were sent
        self.assertEqual(p.feed(long_cut + V_PING), [])               # the candidate is still waiting for its payload
        clock.advance(1.5)
        self.assertEqual(p.feed(b""), [PING])
        self.assertEqual(p.stats.partial_timeouts, 1)

    def test_the_partial_frame_limit_is_one_second(self):
        clock = FakeClock()
        p = FrameParser(clock=clock)
        self.assertEqual(p.feed(V_RESPONSE[:20]), [])
        clock.advance(0.9)
        self.assertEqual(p.feed(V_RESPONSE[20:])[0].type, TYPE_RESPONSE)        # in time: delivered
        self.assertEqual(p.stats.partial_timeouts, 0)
        self.assertEqual(p.feed(V_RESPONSE[:20]), [])
        clock.advance(1.2)
        self.assertEqual(p.feed(V_RESPONSE[20:]), [])                             # too late: the first part was abandoned
        self.assertEqual(p.stats.partial_timeouts, 1)
        self.assertEqual(len(p.feed(V_PING)), 1)                                  # and the receiver is not wedged
        self.assertEqual(len(p), 0)

    def test_the_timer_fires_on_a_silent_line_too(self):
        clock = FakeClock()
        p = FrameParser(clock=clock)
        p.feed(V_PING[:5])
        self.assertEqual(len(p), 5)
        clock.advance(2.0)
        p.feed(b"")                                                                 # a read timeout: nothing arrived, the limit still applies
        self.assertEqual(p.stats.partial_timeouts, 1)
        self.assertLess(len(p), 5)

    def test_slow_trickle_that_stays_within_the_limit_is_accepted_and_a_slower_one_is_not(self):
        clock = FakeClock()
        p = FrameParser(clock=clock)
        got = []
        for b in V_PING:                                      # 21 bytes, 40 ms apart: 0.8 s in total
            got += p.feed(bytes([b]))
            clock.advance(0.04)
        self.assertEqual(len(got), 1)
        got = []
        for b in V_PING:                                      # 21 bytes, 60 ms apart: 1.2 s in total
            got += p.feed(bytes([b]))
            clock.advance(0.06)
        self.assertEqual(got, [])
        self.assertGreaterEqual(p.stats.partial_timeouts, 1)
        self.assertEqual(len(p.feed(V_PING)), 1)

    def test_oversize_len_is_a_false_start(self):
        head = bytearray(fr._HEAD_NO_CRC.pack(fr.MAGIC, 1, TYPE_LOG, 3, 0, 5, MAX_PAYLOAD + 1))
        head += fr._U16.pack(crc16_ccitt(bytes(head)))        # a valid header CRC, but LEN > 4096
        p = FrameParser()
        self.assertEqual(p.feed(bytes(head) + b"\x00" * 100 + V_PING), [PING])
        self.assertEqual(p.stats.bad_len, 1)
        edge = encode_frame(TYPE_LOG, b"e" * MAX_PAYLOAD)      # exactly 4096 is fine
        self.assertEqual(len(FrameParser().feed(edge)), 1)
        with self.assertRaises(ValueError):
            encode_frame(TYPE_LOG, b"e" * (MAX_PAYLOAD + 1))

    def test_a_newer_link_version_is_skipped_whole_and_counted(self):
        v2 = encode_frame(TYPE_LOG, b"future " * 50, ver=2)
        for chunk in (0, 1, 13):
            p = FrameParser()
            got = feed_all(p, V_PING + v2 + V_BEACON, chunk)
            self.assertEqual([f.type for f in got], [TYPE_PING, TYPE_BEACON], chunk)
            self.assertEqual((p.stats.unknown_ver, p.stats.bad_hcrc, p.stats.bad_crc, p.stats.noise_bytes), (1, 0, 0, 0), chunk)
        # frames inside the skipped one are not looked at (they belong to the newer frame)
        self.assertEqual(FrameParser().feed(encode_frame(TYPE_LOG, V_PING, ver=2)), [])
        # a version-2 frame that is still incomplete waits (and is abandoned after 1 s like any partial frame)
        clock = FakeClock()
        p = FrameParser(clock=clock)
        self.assertEqual(p.feed(v2[:50] + V_PING), [])
        clock.advance(1.5)
        self.assertEqual(len(p.feed(b"")), 1)

    def test_noise_goes_to_the_callback_and_never_to_the_parser(self):
        noise = []
        p = FrameParser(on_noise=noise.append)
        text = b"I (123) boot: hello\r\n"
        got = p.feed(text + V_PING + b"tail text")
        self.assertEqual(len(got), 1)
        self.assertEqual(b"".join(noise), text + b"tail text")
        self.assertEqual(p.stats.noise_bytes, len(text) + len(b"tail text"))
        self.assertEqual(p.stats.frames, 1)

    def test_a_trailing_fa_is_kept_for_the_next_read_and_a_split_magic_is_found(self):
        p = FrameParser()
        stream = b"zz" + V_PING
        cut = stream.index(b"\xfa") + 1
        self.assertEqual(p.feed(stream[:cut]), [])
        self.assertEqual(len(p), 1)                           # the 0xFA waits: it may begin a magic
        self.assertEqual(len(p.feed(stream[cut:])), 1)

    def test_a_raising_noise_callback_does_not_break_the_parser(self):
        def boom(_data):
            raise RuntimeError("sink broken")
        p = FrameParser(on_noise=boom)
        self.assertEqual(len(p.feed(b"text" + V_PING)), 1)
        self.assertEqual(p.stats.callback_errors, 1)

    def test_a_flood_of_garbage_is_bounded_and_the_next_frame_is_found(self):
        rng = random.Random(7)
        p = FrameParser()
        for _ in range(40):
            junk = bytes(rng.choice((0xFA, 0xDE, 0x01, 0x00, 0xFF, rng.randrange(256))) for _ in range(6000))
            p.feed(junk)
            self.assertLessEqual(len(p), MAX_BUFFER)
        p.feed(b"\x00" * 5000)                                # random bytes may look like a header and leave a candidate waiting: flush it
        self.assertEqual(p.feed(V_PING), [PING])

    def test_a_flood_of_magics_is_bounded_and_the_frame_behind_it_is_found(self):
        p = FrameParser()
        got = p.feed(b"\xfa\xde" * 100_000 + V_PING)
        self.assertLessEqual(len(p), MAX_BUFFER)
        self.assertIn(PING, got)
        self.assertGreater(p.stats.bad_hcrc, 50_000)

    def test_one_huge_read_is_processed_in_slices_without_losing_frames(self):
        frames = [encode_frame(TYPE_LOG, bytes([i % 251]) * (i % 90), msg_id=i + 1) for i in range(3000)]
        stream = b"".join(frames)
        self.assertGreater(len(stream), 2 * MAX_BUFFER)
        p = FrameParser()
        self.assertEqual(len(p.feed(stream)), 3000)
        self.assertEqual(p.stats.overflow_dropped, 0)

    def test_the_buffer_cap_drops_the_oldest_bytes(self):
        p = FrameParser(max_buffer=20)                         # a frame that needs 40 bytes can never be assembled in 20
        self.assertEqual(p.feed(encode_frame(TYPE_LOG, b"z" * 24)), [])
        self.assertLessEqual(len(p), 20)
        self.assertGreater(p.stats.overflow_dropped, 0)
        self.assertEqual(p.feed(V_PONG), [Frame(TYPE_PONG, 3, 0, 0, b"")])      # a frame that fits is still found

    def test_the_parser_never_raises(self):
        p = FrameParser()
        for bad in (None, "text", 12.5, object(), [300], {1: 2}):
            self.assertEqual(p.feed(bad), [])
        self.assertGreaterEqual(p.stats.internal_errors, 5)
        self.assertEqual(p.feed(bytearray(V_PING)), [PING])
        self.assertEqual(p.feed(memoryview(V_PING)), [PING])

    def test_reset_forgets_a_partial_frame(self):
        p = FrameParser()
        p.feed(V_PING[:10])
        p.reset()
        self.assertEqual((len(p), p.feed(V_PING)[0].msg_id), (0, 7))


class RoundTrips(unittest.TestCase):
    def test_every_payload_length_round_trips(self):
        """0..4096, whole and (for the lengths whose total frame length is a multiple of 64, i.e. that end exactly on a USB packet
        boundary) in 64-byte pieces."""
        multiples = 0
        for n in range(0, MAX_PAYLOAD + 1):
            payload = bytes((n * 7 + i) & 0xFF for i in range(n))
            frame = encode_frame(TYPE_LOG, payload, msg_id=(n % 65535) + 1, seq=n & 0xFF)
            self.assertEqual(len(frame), HEADER_LEN + n + 4)
            want = [Frame(TYPE_LOG, 3, n & 0xFF, (n % 65535) + 1, payload)]
            self.assertEqual(FrameParser().feed(frame), want, n)
            if len(frame) % 64 == 0:
                multiples += 1
                self.assertEqual(feed_all(FrameParser(), frame, 64), want, n)
                self.assertEqual(feed_all(FrameParser(), frame + frame, 64)[0:1], want, n)
        self.assertEqual(multiples, len([n for n in range(MAX_PAYLOAD + 1) if (n + 16) % 64 == 0]))
        self.assertGreater(multiples, 60)

    def test_streams_of_lengths_around_the_packet_size_in_usb_sized_pieces(self):
        rng = random.Random(1)
        lengths = [n for n in range(0, MAX_PAYLOAD + 1) if (n + 16) % 64 in (0, 1, 63)]
        stream = b"".join(encode_frame(TYPE_RESPONSE, bytes(rng.randrange(256) for _ in range(n)), msg_id=1 + (i % 9))
                          for i, n in enumerate(lengths))
        self.assertEqual([len(f.payload) for f in feed_all(FrameParser(), stream, 64)], lengths)

    def test_msg_id_range_seq_wraps_and_errors(self):
        for msg_id in (0, 1, 255, 256, 65535):
            self.assertEqual(FrameParser().feed(encode_frame(TYPE_PING, b"", msg_id=msg_id))[0].msg_id, msg_id)
        self.assertEqual(FrameParser().feed(encode_frame(TYPE_PING, b"", seq=300))[0].seq, 300 & 0xFF)
        for bad in (-1, 65536):
            with self.assertRaises(ValueError):
                encode_frame(TYPE_PING, b"", msg_id=bad)

    def test_unknown_types_are_delivered_for_the_caller_to_ignore(self):
        got = FrameParser().feed(encode_frame(0x7F, b"future") + V_PING)
        self.assertEqual([f.type for f in got], [0x7F, TYPE_PING])
        self.assertNotIn(0x7F, fr.KNOWN_TYPES)


def reassemble(frames: list[bytes]) -> tuple[int, bytes]:
    """The display's view of a chunked RESPONSE (section 5): status, body; asserts the chunk rules."""
    parsed = FrameParser().feed(b"".join(frames))
    assert len(parsed) == len(frames)
    status, total, body = None, None, b""
    for i, f in enumerate(parsed):
        assert f.type == TYPE_RESPONSE and f.seq == (i & 0xFF) and len(f.payload) <= MAX_PAYLOAD
        assert f.first == (i == 0) and f.last == (i == len(parsed) - 1)
        if i == 0:
            status, total, data = fr.split_response_head(f.payload)
        else:
            data = f.payload
        body += data
    assert len(body) == total
    return status, body


class ResponseChunking(unittest.TestCase):
    def test_bodies_of_many_sizes_reassemble(self):
        sizes = [0, 1, 5, 4089, 4090, 4091, 4096, 4097, 8186, 8187, 8192, 12288, 160_000, 163_840, 300_000, fr.MAX_RESPONSE_BODY]
        for n in sizes:
            body = bytes((i * 31) & 0xFF for i in range(n))
            frames = encode_response(0xBEEF, 404 if n % 2 else 200, body)
            self.assertEqual(reassemble(frames), (404 if n % 2 else 200, body), n)
            self.assertTrue(all(len(f) <= fr.MAX_FRAME for f in frames))
            self.assertEqual(len(frames), max(1, -(-(n + 6) // MAX_PAYLOAD)), n)

    def test_the_first_chunk_is_full_and_the_flags_mark_first_and_last(self):
        parsed = FrameParser().feed(b"".join(encode_response(3, 200, b"x" * 10_000)))
        self.assertEqual([(f.flags, f.seq, len(f.payload)) for f in parsed],
                         [(FLAG_FIRST, 0, 4096), (0, 1, 4096), (FLAG_LAST, 2, 10_000 + 6 - 8192)])
        self.assertEqual(encode_response(3, 200, b"")[0][4], FLAG_SINGLE)

    def test_seq_wraps_at_256(self):
        frames = encode_response(2, 200, b"y" * 400_000, max_payload=1024)
        self.assertGreater(len(frames), 300)
        seqs = [f.seq for f in FrameParser().feed(b"".join(frames))]
        self.assertEqual(seqs[254:258], [254, 255, 0, 1])

    def test_limits(self):
        with self.assertRaises(ValueError):
            encode_response(1, 200, b"z" * (fr.MAX_RESPONSE_BODY + 1))
        with self.assertRaises(ValueError):
            encode_response(1, 70000, b"")
        for bad in (0, 6, MAX_PAYLOAD + 1):
            with self.assertRaises(ValueError):
                encode_response(1, 200, b"abc", max_payload=bad)

    def test_split_response_head(self):
        self.assertEqual(fr.split_response_head(b"\xc8\x00\x03\x00\x00\x00abc"), (200, 3, b"abc"))
        self.assertIsNone(fr.split_response_head(b"\x00\x01\x02"))


# ---- seeded fuzz ----------------------------------------------------------------------------------------------------------------------

def _random_frame(rng: random.Random) -> bytes:
    kind = rng.choice((TYPE_BEACON, TYPE_ACK, TYPE_REQUEST, TYPE_RESPONSE, TYPE_LOG, TYPE_PING, TYPE_PONG, 0x44))
    n = rng.choice((0, 1, 5, 17, 40, 64, 100, 250, rng.randrange(0, 700)))
    payload = bytes(rng.getrandbits(8) for _ in range(n)) if rng.random() < 0.5 else (b"\xfa\xde" * (n // 2))
    flags = rng.choice((3, 3, 3, 1, 2, 0))
    return encode_frame(kind, payload, msg_id=rng.randrange(65536), flags=flags, seq=rng.randrange(256))


def _noise(rng: random.Random) -> bytes:
    style = rng.randrange(5)
    n = rng.randrange(1, 60)
    if style == 0:
        return bytes(rng.getrandbits(8) for _ in range(n))
    if style == 1:
        return b"I (1234) boot: some log text\r\n"[:n]
    if style == 2:
        return rng.choice((b"\xfa", b"\xde", b"\xfa\xde", b"\xfa\xde\x01", b"\xfa\xfa\xfa\xde")) * rng.randrange(1, 4)
    if style == 3:                                          # looks like the start of a header, but the rest is random
        return bytes([0xFA, 0xDE, 1] + [rng.randrange(256) for _ in range(9)])[:n]
    return b"\x00" * n


def _mutate(rng: random.Random, frame: bytes) -> bytes:
    """A frame damaged in a way that (practically) cannot yield another valid frame."""
    how = rng.randrange(5)
    b = bytearray(frame)
    if how == 0:
        b[rng.randrange(len(b))] ^= 1 << rng.randrange(8)
    elif how == 1:
        return bytes(b[:rng.randrange(1, len(b))])             # truncated
    elif how == 2:
        del b[rng.randrange(len(b))]                           # a byte lost
    elif how == 3:
        b.insert(rng.randrange(len(b)), rng.randrange(256))     # a byte inserted
    else:
        for _ in range(rng.randrange(1, 4)):
            b[rng.randrange(len(b))] = rng.randrange(256)
    return bytes(b)


def _decode(raw: bytes) -> Frame:
    got = FrameParser().feed(raw)
    assert len(got) == 1
    return got[0]


class Fuzz(unittest.TestCase):
    def test_seeded_random_streams(self):
        """Random streams of intact frames, damaged frames, future-version frames and noise, fed in random read sizes: never an exception,
        a bounded buffer, every intact frame recovered in order, and no frame that was not put there."""
        streams = int(os.environ.get("DM_FUZZ_STREAMS", "200000"))
        rng = random.Random(20261013)
        decoded = {raw: _decode(raw) for raw in (_random_frame(rng) for _ in range(64))}
        pool = list(decoded)
        known = set(decoded.values())
        clock = FakeClock()
        intact_total = 0
        for n in range(streams):
            parts, expected = [], []
            for _ in range(rng.randrange(1, 6)):
                r = rng.random()
                if r < 0.35:
                    raw = rng.choice(pool) if rng.random() < 0.9 else _random_frame(rng)
                    if raw not in decoded:
                        decoded[raw] = _decode(raw)
                        known.add(decoded[raw])
                    parts.append(raw)
                    expected.append(decoded[raw])
                elif r < 0.65:
                    parts.append(_mutate(rng, rng.choice(pool)))
                elif r < 0.75:
                    parts.append(encode_frame(TYPE_LOG, b"future", ver=2))
                else:
                    parts.append(_noise(rng))
            stream = b"".join(parts)
            p = FrameParser(clock=clock)
            got = []
            i = 0
            while i < len(stream):                                    # random read sizes, like a serial port
                step = rng.choice((1, 3, 7, 64, 4096)) if rng.random() < 0.7 else rng.randrange(1, 200)
                got += p.feed(stream[i:i + step])
                i += step
                self.assertLessEqual(len(p), MAX_BUFFER)
            for _ in range(100):                                      # the line went quiet: waiting candidates are abandoned one by one
                if len(p) <= 1:
                    break
                clock.advance(2.0)
                got += p.feed(b"")
            else:
                self.fail((n, "the receiver stayed wedged"))
            self.assertEqual(p.stats.internal_errors, 0, n)
            it = iter(got)
            for want in expected:
                self.assertTrue(any(f == want for f in it), (n, "an intact frame was lost"))
            self.assertTrue(all(f in known for f in got), (n, "a frame appeared that was never sent"))
            intact_total += len(expected)
        self.assertGreater(intact_total, streams)

    def test_no_phantom_frames_from_damaged_streams(self):
        """Damaged copies of frames never turn into frames that were not sent (a damaged frame has a wrong CRC-32)."""
        rng = random.Random(99)
        raws = [_random_frame(rng) for _ in range(32)]
        known = {_decode(raw) for raw in raws}
        for _ in range(20000):
            stream = b"".join(_mutate(rng, rng.choice(raws)) for _ in range(rng.randrange(1, 5)))
            for f in FrameParser().feed(stream):
                self.assertIn(f, known)


if __name__ == "__main__":
    unittest.main()

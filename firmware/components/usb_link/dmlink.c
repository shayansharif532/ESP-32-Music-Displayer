/*
 * DMLINK v1 frame codec and resynchronising parser (docs/usb-protocol.md sections 2 and 3). Pure C, bounded, no allocation.
 */
#include "dmlink.h"

#include <string.h>

static const uint32_t CRC32_TABLE[256] = {
    0x00000000, 0x77073096, 0xEE0E612C, 0x990951BA, 0x076DC419, 0x706AF48F,
    0xE963A535, 0x9E6495A3, 0x0EDB8832, 0x79DCB8A4, 0xE0D5E91E, 0x97D2D988,
    0x09B64C2B, 0x7EB17CBD, 0xE7B82D07, 0x90BF1D91, 0x1DB71064, 0x6AB020F2,
    0xF3B97148, 0x84BE41DE, 0x1ADAD47D, 0x6DDDE4EB, 0xF4D4B551, 0x83D385C7,
    0x136C9856, 0x646BA8C0, 0xFD62F97A, 0x8A65C9EC, 0x14015C4F, 0x63066CD9,
    0xFA0F3D63, 0x8D080DF5, 0x3B6E20C8, 0x4C69105E, 0xD56041E4, 0xA2677172,
    0x3C03E4D1, 0x4B04D447, 0xD20D85FD, 0xA50AB56B, 0x35B5A8FA, 0x42B2986C,
    0xDBBBC9D6, 0xACBCF940, 0x32D86CE3, 0x45DF5C75, 0xDCD60DCF, 0xABD13D59,
    0x26D930AC, 0x51DE003A, 0xC8D75180, 0xBFD06116, 0x21B4F4B5, 0x56B3C423,
    0xCFBA9599, 0xB8BDA50F, 0x2802B89E, 0x5F058808, 0xC60CD9B2, 0xB10BE924,
    0x2F6F7C87, 0x58684C11, 0xC1611DAB, 0xB6662D3D, 0x76DC4190, 0x01DB7106,
    0x98D220BC, 0xEFD5102A, 0x71B18589, 0x06B6B51F, 0x9FBFE4A5, 0xE8B8D433,
    0x7807C9A2, 0x0F00F934, 0x9609A88E, 0xE10E9818, 0x7F6A0DBB, 0x086D3D2D,
    0x91646C97, 0xE6635C01, 0x6B6B51F4, 0x1C6C6162, 0x856530D8, 0xF262004E,
    0x6C0695ED, 0x1B01A57B, 0x8208F4C1, 0xF50FC457, 0x65B0D9C6, 0x12B7E950,
    0x8BBEB8EA, 0xFCB9887C, 0x62DD1DDF, 0x15DA2D49, 0x8CD37CF3, 0xFBD44C65,
    0x4DB26158, 0x3AB551CE, 0xA3BC0074, 0xD4BB30E2, 0x4ADFA541, 0x3DD895D7,
    0xA4D1C46D, 0xD3D6F4FB, 0x4369E96A, 0x346ED9FC, 0xAD678846, 0xDA60B8D0,
    0x44042D73, 0x33031DE5, 0xAA0A4C5F, 0xDD0D7CC9, 0x5005713C, 0x270241AA,
    0xBE0B1010, 0xC90C2086, 0x5768B525, 0x206F85B3, 0xB966D409, 0xCE61E49F,
    0x5EDEF90E, 0x29D9C998, 0xB0D09822, 0xC7D7A8B4, 0x59B33D17, 0x2EB40D81,
    0xB7BD5C3B, 0xC0BA6CAD, 0xEDB88320, 0x9ABFB3B6, 0x03B6E20C, 0x74B1D29A,
    0xEAD54739, 0x9DD277AF, 0x04DB2615, 0x73DC1683, 0xE3630B12, 0x94643B84,
    0x0D6D6A3E, 0x7A6A5AA8, 0xE40ECF0B, 0x9309FF9D, 0x0A00AE27, 0x7D079EB1,
    0xF00F9344, 0x8708A3D2, 0x1E01F268, 0x6906C2FE, 0xF762575D, 0x806567CB,
    0x196C3671, 0x6E6B06E7, 0xFED41B76, 0x89D32BE0, 0x10DA7A5A, 0x67DD4ACC,
    0xF9B9DF6F, 0x8EBEEFF9, 0x17B7BE43, 0x60B08ED5, 0xD6D6A3E8, 0xA1D1937E,
    0x38D8C2C4, 0x4FDFF252, 0xD1BB67F1, 0xA6BC5767, 0x3FB506DD, 0x48B2364B,
    0xD80D2BDA, 0xAF0A1B4C, 0x36034AF6, 0x41047A60, 0xDF60EFC3, 0xA867DF55,
    0x316E8EEF, 0x4669BE79, 0xCB61B38C, 0xBC66831A, 0x256FD2A0, 0x5268E236,
    0xCC0C7795, 0xBB0B4703, 0x220216B9, 0x5505262F, 0xC5BA3BBE, 0xB2BD0B28,
    0x2BB45A92, 0x5CB36A04, 0xC2D7FFA7, 0xB5D0CF31, 0x2CD99E8B, 0x5BDEAE1D,
    0x9B64C2B0, 0xEC63F226, 0x756AA39C, 0x026D930A, 0x9C0906A9, 0xEB0E363F,
    0x72076785, 0x05005713, 0x95BF4A82, 0xE2B87A14, 0x7BB12BAE, 0x0CB61B38,
    0x92D28E9B, 0xE5D5BE0D, 0x7CDCEFB7, 0x0BDBDF21, 0x86D3D2D4, 0xF1D4E242,
    0x68DDB3F8, 0x1FDA836E, 0x81BE16CD, 0xF6B9265B, 0x6FB077E1, 0x18B74777,
    0x88085AE6, 0xFF0F6A70, 0x66063BCA, 0x11010B5C, 0x8F659EFF, 0xF862AE69,
    0x616BFFD3, 0x166CCF45, 0xA00AE278, 0xD70DD2EE, 0x4E048354, 0x3903B3C2,
    0xA7672661, 0xD06016F7, 0x4969474D, 0x3E6E77DB, 0xAED16A4A, 0xD9D65ADC,
    0x40DF0B66, 0x37D83BF0, 0xA9BCAE53, 0xDEBB9EC5, 0x47B2CF7F, 0x30B5FFE9,
    0xBDBDF21C, 0xCABAC28A, 0x53B39330, 0x24B4A3A6, 0xBAD03605, 0xCDD70693,
    0x54DE5729, 0x23D967BF, 0xB3667A2E, 0xC4614AB8, 0x5D681B02, 0x2A6F2B94,
    0xB40BBE37, 0xC30C8EA1, 0x5A05DF1B, 0x2D02EF8D,
};

uint32_t dm_crc32(uint32_t crc, const void *data, size_t n)
{
    const uint8_t *p = data;
    crc = ~crc;
    while (n--) {
        crc = CRC32_TABLE[(crc ^ *p++) & 0xFF] ^ (crc >> 8);
    }
    return ~crc;
}

uint16_t dm_crc16(const void *data, size_t n)
{
    const uint8_t *p = data;
    uint16_t crc = 0xFFFF;
    while (n--) {
        crc ^= (uint16_t)(*p++) << 8;
        for (int i = 0; i < 8; i++) {
            crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
        }
    }
    return crc;
}

static inline uint16_t ld16(const uint8_t *p)
{
    return (uint16_t)(p[0] | (p[1] << 8));
}

static inline uint32_t ld32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

size_t dm_frame_encode(uint8_t *out, size_t cap, uint8_t type, uint8_t flags, uint8_t seq, uint16_t id, const void *payload, size_t len)
{
    if (!out || len > DM_MAX_PAYLOAD || cap < len + DM_OVERHEAD_BYTES || (len && !payload)) {
        return 0;
    }
    out[0] = DM_MAGIC0;
    out[1] = DM_MAGIC1;
    out[2] = DM_VERSION;
    out[3] = type;
    out[4] = flags;
    out[5] = seq;
    out[6] = (uint8_t)(id & 0xFF);
    out[7] = (uint8_t)(id >> 8);
    out[8] = (uint8_t)(len & 0xFF);
    out[9] = (uint8_t)(len >> 8);
    const uint16_t hc = dm_crc16(out, 10);
    out[10] = (uint8_t)(hc & 0xFF);
    out[11] = (uint8_t)(hc >> 8);
    if (len) {
        memcpy(out + DM_HEADER_BYTES, payload, len);
    }
    const uint32_t c = dm_crc32(0, out, DM_HEADER_BYTES + len);
    uint8_t *t = out + DM_HEADER_BYTES + len;
    t[0] = (uint8_t)(c & 0xFF);
    t[1] = (uint8_t)((c >> 8) & 0xFF);
    t[2] = (uint8_t)((c >> 16) & 0xFF);
    t[3] = (uint8_t)(c >> 24);
    return len + DM_OVERHEAD_BYTES;
}

void dm_parser_init(dm_parser_t *p)
{
    memset(p, 0, sizeof *p);
}

/* The normative receiver of the spec: find a magic, check the header, wait for the rest, check the CRC; on any doubt discard ONE byte and
 * look again inside what is already buffered, so a frame that starts behind a false start is never lost. */
static void process(dm_parser_t *p, uint32_t now_ms, dm_frame_cb_t cb, void *ctx)
{
    size_t pos = 0;
    for (;;) {
        size_t i = pos;
        while (i < p->len && !(p->buf[i] == DM_MAGIC0 && (i + 1 >= p->len || p->buf[i + 1] == DM_MAGIC1))) {
            i++;
        }
        p->noise_bytes += (uint32_t)(i - pos);
        pos = i;
        if (pos >= p->len) {
            break;
        }
        const uint8_t *h = p->buf + pos;
        const size_t avail = p->len - pos;
        if (avail < DM_HEADER_BYTES) {
            break;                                           /* a lone magic byte or a partial header: wait for more */
        }
        if (dm_crc16(h, 10) != ld16(h + 10)) {
            p->bad_header++;
            pos++;
            continue;
        }
        const uint16_t len = ld16(h + 8);
        if (len > DM_MAX_PAYLOAD) {
            p->oversize++;
            pos++;
            continue;
        }
        const size_t total = (size_t)len + DM_OVERHEAD_BYTES;
        if (avail < total) {
            break;                                           /* the rest is still on its way (or never comes: dm_parser_poll) */
        }
        if (h[2] != DM_VERSION) {                            /* an intact frame of another link version: skip it whole */
            p->skipped_version++;
            pos += total;
            continue;
        }
        if (dm_crc32(0, h, total - 4) != ld32(h + total - 4)) {
            p->bad_crc++;
            pos++;
            continue;
        }
        const dm_frame_t f = { .type = h[3], .flags = h[4], .seq = h[5], .id = ld16(h + 6), .len = len, .payload = h + DM_HEADER_BYTES };
        p->frames++;
        if (cb) {
            cb(&f, ctx);
        }
        pos += total;
    }
    if (pos > 0) {
        memmove(p->buf, p->buf + pos, p->len - pos);
        p->len -= pos;
        p->started_ms = now_ms;
    }
}

void dm_parser_feed(dm_parser_t *p, const uint8_t *data, size_t n, uint32_t now_ms, dm_frame_cb_t cb, void *ctx)
{
    while (n > 0) {
        if (p->len == 0) {
            p->started_ms = now_ms;
        }
        size_t room = sizeof p->buf - p->len;
        size_t take = n < room ? n : room;
        memcpy(p->buf + p->len, data, take);
        p->len += take;
        data += take;
        n -= take;
        process(p, now_ms, cb, ctx);
    }
}

void dm_parser_poll(dm_parser_t *p, uint32_t now_ms, dm_frame_cb_t cb, void *ctx)
{
    if (p->len > 0 && (uint32_t)(now_ms - p->started_ms) > DM_PARTIAL_MS) {
        p->partial_dropped++;
        memmove(p->buf, p->buf + 1, p->len - 1);             /* the sender died mid-frame: give up on this start, look at what follows */
        p->len--;
        p->started_ms = now_ms;
        process(p, now_ms, cb, ctx);
    }
}

/* ---- self-test (development builds) ------------------------------------------------------------------------------------------- */

static const uint8_t V_PING[] = { 0xfa, 0xde, 0x01, 0x30, 0x03, 0x00, 0x07, 0x00, 0x05, 0x00, 0xc9, 0xf0, 0x68, 0x65, 0x6c, 0x6c, 0x6f, 0xc0, 0xc9, 0xb3, 0xe1 };
static const uint8_t V_REQUEST[] = { 0xfa, 0xde, 0x01, 0x10, 0x03, 0x00, 0x01, 0x00, 0x11, 0x00, 0x11, 0x76, 0x47, 0x45, 0x54, 0x20, 0x2f, 0x6e, 0x6f, 0x77, 0x2d,
                                     0x70, 0x6c, 0x61, 0x79, 0x69, 0x6e, 0x67, 0x0a, 0x64, 0x50, 0x41, 0xb9 };
static const uint8_t V_PONG[] = { 0xfa, 0xde, 0x01, 0x31, 0x03, 0x00, 0x00, 0x00, 0x00, 0x00, 0x70, 0xe6, 0x50, 0x85, 0x17, 0xb6, 0x16 };

typedef struct {
    int      n;
    uint8_t  type[16];
    uint16_t id[16], len[16];
    uint8_t  first_byte[16];
} st_seen_t;

static void st_cb(const dm_frame_t *f, void *ctx)
{
    st_seen_t *s = ctx;
    if (s->n < 16) {
        s->type[s->n] = f->type;
        s->id[s->n] = f->id;
        s->len[s->n] = f->len;
        s->first_byte[s->n] = f->len ? f->payload[0] : 0;
    }
    s->n++;
}

static dm_parser_t s_test_parser;                              /* 4.4 KB: static, not on a task stack */

const char *dm_selftest(void)
{
    st_seen_t seen;
    static const uint8_t check[] = "123456789";
    if (dm_crc16(check, 9) != 0x29B1) return "crc16";
    if (dm_crc32(0, check, 9) != 0xCBF43926u) return "crc32";
    if (dm_crc32(dm_crc32(0, check, 4), check + 4, 5) != 0xCBF43926u) return "crc32 pieces";

    uint8_t enc[64];                                             /* our encoder must reproduce the published vectors */
    if (dm_frame_encode(enc, sizeof enc, DM_T_PING, 3, 0, 7, "hello", 5) != sizeof V_PING || memcmp(enc, V_PING, sizeof V_PING) != 0) return "encode ping";
    if (dm_frame_encode(enc, sizeof enc, DM_T_PONG, 3, 0, 0, NULL, 0) != sizeof V_PONG || memcmp(enc, V_PONG, sizeof V_PONG) != 0) return "encode pong";
    if (dm_frame_encode(enc, 10, DM_T_PING, 3, 0, 7, "hello", 5) != 0) return "encode must refuse a small buffer";

    dm_parser_init(&s_test_parser);                              /* the vectors, whole */
    memset(&seen, 0, sizeof seen);
    dm_parser_feed(&s_test_parser, V_PING, sizeof V_PING, 0, st_cb, &seen);
    dm_parser_feed(&s_test_parser, V_REQUEST, sizeof V_REQUEST, 0, st_cb, &seen);
    dm_parser_feed(&s_test_parser, V_PONG, sizeof V_PONG, 0, st_cb, &seen);
    if (seen.n != 3 || seen.type[0] != DM_T_PING || seen.id[0] != 7 || seen.type[1] != DM_T_REQUEST || seen.id[1] != 1 || seen.type[2] != DM_T_PONG) return "vectors";

    memset(&seen, 0, sizeof seen);                               /* byte by byte, with noise (a text line) in front */
    static const uint8_t noise[] = "ESP-ROM boot banner\r\n\xFA";
    dm_parser_feed(&s_test_parser, noise, sizeof noise - 1, 10, st_cb, &seen);
    for (size_t i = 0; i < sizeof V_REQUEST; i++) {
        dm_parser_feed(&s_test_parser, &V_REQUEST[i], 1, 11, st_cb, &seen);
    }
    if (seen.n != 1 || seen.type[0] != DM_T_REQUEST) return "byte-wise + noise";

    memset(&seen, 0, sizeof seen);                               /* one flipped byte, then the same frame again: exactly the second */
    uint8_t bad[sizeof V_PING];
    memcpy(bad, V_PING, sizeof bad);
    bad[14] ^= 0x40;
    dm_parser_feed(&s_test_parser, bad, sizeof bad, 20, st_cb, &seen);
    dm_parser_feed(&s_test_parser, V_PING, sizeof V_PING, 20, st_cb, &seen);
    if (seen.n != 1 || seen.id[0] != 7 || s_test_parser.bad_crc == 0) return "flipped byte";

    memset(&seen, 0, sizeof seen);                               /* a truncated frame, then a whole one glued behind it: the whole one is found */
    dm_parser_feed(&s_test_parser, V_REQUEST, 20, 30, st_cb, &seen);
    dm_parser_feed(&s_test_parser, V_PING, sizeof V_PING, 30, st_cb, &seen);
    dm_parser_feed(&s_test_parser, V_PONG, sizeof V_PONG, 30, st_cb, &seen);
    if (seen.n != 2 || seen.type[0] != DM_T_PING || seen.type[1] != DM_T_PONG) return "truncated then glued";

    memset(&seen, 0, sizeof seen);                               /* a header that claims 60000 bytes: skipped after 12 bytes, nothing lost behind it */
    uint8_t big[DM_HEADER_BYTES + sizeof V_PING];
    memcpy(big, V_PING, DM_HEADER_BYTES);
    big[8] = 0x60; big[9] = 0xEA;                                /* LEN = 60000 */
    const uint16_t hc = dm_crc16(big, 10);
    big[10] = (uint8_t)hc; big[11] = (uint8_t)(hc >> 8);
    memcpy(big + DM_HEADER_BYTES, V_PING, sizeof V_PING);
    dm_parser_feed(&s_test_parser, big, sizeof big, 40, st_cb, &seen);
    if (seen.n != 1 || seen.type[0] != DM_T_PING || s_test_parser.oversize == 0) return "oversize header";

    memset(&seen, 0, sizeof seen);                               /* a partial frame is abandoned after 1 s and the next frame is accepted */
    dm_parser_feed(&s_test_parser, V_REQUEST, 15, 100, st_cb, &seen);
    dm_parser_poll(&s_test_parser, 1200, st_cb, &seen);
    dm_parser_feed(&s_test_parser, V_PING, sizeof V_PING, 1200, st_cb, &seen);
    if (seen.n != 1 || seen.type[0] != DM_T_PING) return "partial timeout";

    static uint8_t body[DM_MAX_FRAME];                           /* every payload length of interest round-trips, also frames of 64 * k bytes */
    static const uint16_t lens[] = { 0, 1, 47, 48, 49, 111, 112, 4095, 4096 };
    for (size_t k = 0; k < sizeof lens / sizeof lens[0]; k++) {
        static uint8_t pay[DM_MAX_PAYLOAD];
        for (size_t i = 0; i < lens[k]; i++) pay[i] = (uint8_t)(i * 31 + k);
        const size_t n = dm_frame_encode(body, sizeof body, DM_T_RESPONSE, 3, 0, 1234, pay, lens[k]);
        if (n != (size_t)lens[k] + DM_OVERHEAD_BYTES) return "encode length";
        memset(&seen, 0, sizeof seen);
        for (size_t off = 0; off < n; off += 61) {
            dm_parser_feed(&s_test_parser, body + off, n - off < 61 ? n - off : 61, 200, st_cb, &seen);
        }
        if (seen.n != 1 || seen.len[0] != lens[k] || seen.id[0] != 1234) return "round trip";
    }
    if (dm_frame_encode(body, sizeof body, DM_T_RESPONSE, 3, 0, 1, body, DM_MAX_PAYLOAD + 1) != 0) return "encode must refuse an oversize payload";

    memset(&seen, 0, sizeof seen);                               /* random garbage never produces a frame, never overruns, and does not hide a real one */
    uint32_t x = 12345;
    uint32_t before = s_test_parser.frames;
    for (int round = 0; round < 300; round++) {
        uint8_t junk[97];
        for (size_t i = 0; i < sizeof junk; i++) {
            x = x * 1664525u + 1013904223u;
            junk[i] = (uint8_t)(x >> 24);
            if ((x & 0x1F) == 0) junk[i] = DM_MAGIC0;
            if ((x & 0x1F) == 1) junk[i] = DM_MAGIC1;
        }
        dm_parser_feed(&s_test_parser, junk, sizeof junk, 300 + (uint32_t)round, st_cb, &seen);
    }
    if (s_test_parser.frames != before || s_test_parser.len >= DM_MAX_FRAME) return "garbage";
    dm_parser_feed(&s_test_parser, V_PING, sizeof V_PING, 700, st_cb, &seen);
    dm_parser_poll(&s_test_parser, 2000, st_cb, &seen);
    dm_parser_poll(&s_test_parser, 4000, st_cb, &seen);
    dm_parser_feed(&s_test_parser, V_PING, sizeof V_PING, 4000, st_cb, &seen);
    return seen.n >= 1 ? NULL : "frame after garbage";
}

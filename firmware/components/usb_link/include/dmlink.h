#pragma once
/*
 * DMLINK v1: the frame format and the resynchronising stream parser of the USB link (docs/usb-protocol.md, sections 2 and 3).
 *
 * Pure C with no ESP-IDF dependency, so the same code can be exercised by the on-target self-test (dmlink_selftest.c, development
 * builds) with the protocol's published test vectors. Everything is bounded: the parser owns one fixed buffer and never allocates.
 */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define DM_MAGIC0           0xFA
#define DM_MAGIC1           0xDE
#define DM_VERSION          1
#define DM_HEADER_BYTES     12
#define DM_OVERHEAD_BYTES   16                      /* header + trailing CRC-32 */
#define DM_MAX_PAYLOAD      4096
#define DM_MAX_FRAME        (DM_OVERHEAD_BYTES + DM_MAX_PAYLOAD)
#define DM_PARTIAL_MS       1000                    /* a frame that does not complete within this long is abandoned */

enum {
    DM_T_BEACON   = 0x01,
    DM_T_ACK      = 0x02,
    DM_T_REQUEST  = 0x10,
    DM_T_RESPONSE = 0x11,
    DM_T_LOG      = 0x20,
    DM_T_PING     = 0x30,
    DM_T_PONG     = 0x31,
    DM_T_DEBUG    = 0x7E,           /* development builds only: the host asks the display to run a development action (payload: its name) */
};

#define DM_F_FIRST  0x01
#define DM_F_LAST   0x02

uint16_t dm_crc16(const void *data, size_t n);                        /* CRC-16/CCITT-FALSE */
uint32_t dm_crc32(uint32_t crc, const void *data, size_t n);          /* zlib's CRC-32; start with crc = 0, feed in pieces */

/* Writes one frame into out (capacity cap); returns its length (len + 16) or 0 when it does not fit / len is too big. */
size_t dm_frame_encode(uint8_t *out, size_t cap, uint8_t type, uint8_t flags, uint8_t seq, uint16_t id, const void *payload, size_t len);

typedef struct {
    uint8_t        type, flags, seq;
    uint16_t       id;
    uint16_t       len;
    const uint8_t *payload;          /* points into the parser's buffer: valid only during the callback */
} dm_frame_t;

typedef void (*dm_frame_cb_t)(const dm_frame_t *f, void *ctx);

typedef struct {
    uint8_t  buf[DM_MAX_FRAME + 256];
    size_t   len;
    uint32_t started_ms;             /* when the frame at the start of buf began to arrive (for the partial-frame timeout) */
    /* counters, for the status line and the tests */
    uint32_t frames, bad_header, bad_crc, oversize, skipped_version, noise_bytes, partial_dropped;
} dm_parser_t;

void dm_parser_init(dm_parser_t *p);
/* Feeds received bytes (any amount, any split) and calls cb for every intact frame, in order. Never fails. */
void dm_parser_feed(dm_parser_t *p, const uint8_t *data, size_t n, uint32_t now_ms, dm_frame_cb_t cb, void *ctx);
/* Call now and then while idle: abandons a partial frame that has been waiting longer than DM_PARTIAL_MS. */
void dm_parser_poll(dm_parser_t *p, uint32_t now_ms, dm_frame_cb_t cb, void *ctx);

/* Development self-test: the published vectors of docs/usb-protocol.md section 10, resynchronisation, byte-wise feeding and random
 * garbage. Returns NULL when everything passes, else the name of the first failing check. */
const char *dm_selftest(void);

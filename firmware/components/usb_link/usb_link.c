/*
 * usb_link: the display's end of the DMLINK v1 USB link. See usb_link.h and docs/usb-protocol.md.
 *
 * Tasks: `usb_rx` reads the port and feeds the frame parser; `usb_tx` keeps the presence handshake (BEACON) and forwards log text. The
 * poll task of pc_media sends REQUEST frames itself (under the same TX lock) and sleeps on a semaphore until the RX task has collected
 * the whole RESPONSE.
 *
 * Why every write is all-or-nothing: usb_serial_jtag_write_bytes() queues the whole buffer in its ring or nothing, so a frame is never
 * half on the wire because of this side; the peer's parser resynchronises on anything else (cable pulled, host crashed, partial reads).
 */
#include "usb_link.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "cJSON.h"
#include "dmlink.h"
#include "driver/usb_serial_jtag.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/ringbuf.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

static const char *TAG = "usb_link";

#define DRIVER_RX_BYTES     16384                   /* a frame is at most 4112 bytes; the rx task drains the ring at once */
#define DRIVER_TX_BYTES     16384                   /* ... and a write is all-or-nothing into the ring: it must hold the biggest frame */
#define COMPANION_STALE_MS  6000            /* no frame from the companion for this long: it is gone */
#define BEACON_FAST_MS      1000            /* no companion yet */
#define BEACON_SLOW_MS      2000            /* keep-alive while linked */
#define TX_WAIT_MS          60              /* how long a frame may wait for room in the driver's ring */
#define LOG_RING_BYTES      4096
#define LOG_FRAME_BYTES     1024
#define LOG_LINE_MAX        256
#define REQ_MAX_BYTES       DM_MAX_PAYLOAD
#define PONG_MAX            64

static struct {
    bool started;
    usb_link_config_t cfg;
    uint32_t boot_id;
    dm_parser_t parser;                     /* RX task only */
    uint8_t tx_buf[DM_MAX_FRAME];           /* guarded by tx_lock */
    SemaphoreHandle_t tx_lock, req_lock, pend_lock, done_sem;
    portMUX_TYPE mux;                       /* peer / timestamps / epoch / stats that are read from other tasks */
    int64_t last_rx_ms;                     /* any valid frame from the companion; 0 = never */
    usb_link_peer_t peer;
    bool peer_valid;
    uint32_t epoch;
    uint16_t next_id;
    struct {
        bool active;
        uint16_t id;
        usb_link_buf_t *buf;
        int *status;
        bool got_first, finished, ok;
        uint8_t next_seq;
        uint32_t total, received;
    } pend;                                 /* guarded by pend_lock */
    RingbufHandle_t log_rb;
    void (*debug_cb)(const char *action, void *ctx);
    void *debug_ctx;
    vprintf_like_t old_vprintf;
    bool log_installed;
    usb_link_stats_t st;
} u = { .mux = portMUX_INITIALIZER_UNLOCKED };

static int64_t now_ms(void)
{
    return esp_timer_get_time() / 1000;
}

/* The SDK's connection monitor (SOF packets seen from a tick hook) can read "disconnected" for a moment when the CPU was held up, e.g. by a
 * flash write: ride such a blip out instead of dropping frames or flipping the transport. A real unplug is reported after HOST_GRACE_MS. */
#define HOST_GRACE_MS   400
static volatile int64_t s_host_seen_ms;

static bool host_present(void)
{
    const int64_t t = now_ms();
    if (usb_serial_jtag_is_connected()) {
        s_host_seen_ms = t;
        return true;
    }
    return s_host_seen_ms != 0 && t - s_host_seen_ms < HOST_GRACE_MS;
}

/* ---- response buffer (PSRAM, grows on demand up to `max`) ---------------------------------------------------------------- */

bool usb_link_buf_reserve(usb_link_buf_t *b, size_t total)
{
    if (total + 1 > b->max) {
        b->truncated = true;
        return false;
    }
    if (total + 1 <= b->cap) {
        return true;
    }
    char *p = heap_caps_realloc(b->data, total + 1, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!p) {
        b->truncated = true;
        return false;
    }
    b->data = p;
    b->cap = total + 1;
    return true;
}

bool usb_link_buf_append(usb_link_buf_t *b, const void *data, size_t n)
{
    if (b->len + n + 1 > b->max) {
        b->truncated = true;
        return false;
    }
    if (b->len + n + 1 > b->cap) {
        size_t cap = b->cap ? b->cap : 2048;
        while (cap < b->len + n + 1) {
            cap *= 2;
        }
        if (cap > b->max) {
            cap = b->max;
        }
        if (!usb_link_buf_reserve(b, cap - 1)) {
            return false;
        }
    }
    memcpy(b->data + b->len, data, n);
    b->len += n;
    b->data[b->len] = '\0';
    return true;
}

/* ---- sending ---------------------------------------------------------------------------------------------------------------- */

/* One frame, atomically with respect to every other sender. Not while no host is attached (nothing would ever read it). */
static bool tx_frame(uint8_t type, uint8_t flags, uint8_t seq, uint16_t id, const void *payload, size_t len, int wait_ms)
{
    if (!host_present()) {
        return false;
    }
    if (xSemaphoreTake(u.tx_lock, pdMS_TO_TICKS(wait_ms)) != pdTRUE) {
        u.st.tx_dropped++;
        return false;
    }
    size_t n = dm_frame_encode(u.tx_buf, sizeof u.tx_buf, type, flags, seq, id, payload, len);
    bool ok = n > 0 && usb_serial_jtag_write_bytes(u.tx_buf, n, pdMS_TO_TICKS(wait_ms)) == (int)n;
    xSemaphoreGive(u.tx_lock);
    if (ok) {
        u.st.tx_frames++;
    } else {
        u.st.tx_dropped++;
    }
    return ok;
}

static bool tx_idle(void)
{
    return usb_serial_jtag_wait_tx_done(0) == ESP_OK;
}

static void send_beacon(void)
{
    char hex[17], json[200];
    static const char *H = "0123456789abcdef";
    for (int i = 0; i < 8; i++) {
        hex[2 * i] = H[u.cfg.dev_id[i] >> 4];
        hex[2 * i + 1] = H[u.cfg.dev_id[i] & 15];
    }
    hex[16] = '\0';
    int n = snprintf(json, sizeof json, "{\"v\":1,\"api\":%d,\"min_api\":%d,\"dev\":\"%s\",\"boot\":\"%08x\",\"fw\":\"%s\",\"paired\":%s}",
                     USB_LINK_API, USB_LINK_MIN_API, hex, (unsigned)u.boot_id, u.cfg.fw_version ? u.cfg.fw_version : "0",
                     (u.cfg.is_paired && u.cfg.is_paired()) ? "true" : "false");
    if (n > 0 && n < (int)sizeof json) {
        tx_frame(DM_T_BEACON, DM_F_FIRST | DM_F_LAST, 0, 0, json, (size_t)n, TX_WAIT_MS);
    }
}

/* ---- receiving -------------------------------------------------------------------------------------------------------------- */

static bool hex8(const char *s, uint8_t out[8])
{
    if (!s || strlen(s) != 16) {
        return false;
    }
    for (int i = 0; i < 8; i++) {
        int v = 0;
        for (int k = 0; k < 2; k++) {
            char c = s[2 * i + k];
            int d = (c >= '0' && c <= '9') ? c - '0' : (c >= 'a' && c <= 'f') ? c - 'a' + 10 : (c >= 'A' && c <= 'F') ? c - 'A' + 10 : -1;
            if (d < 0) {
                return false;
            }
            v = v * 16 + d;
        }
        out[i] = (uint8_t)v;
    }
    return true;
}

static void copy_clean(char *dst, size_t n, const cJSON *v)
{
    dst[0] = '\0';
    if (!cJSON_IsString(v) || !v->valuestring) {
        return;
    }
    size_t o = 0;
    for (const unsigned char *p = (const unsigned char *)v->valuestring; *p && o + 1 < n; p++) {
        dst[o++] = (*p < 0x20 || *p >= 0x7F) ? '?' : (char)*p;            /* the name goes on the screen: printable ASCII only */
    }
    dst[o] = '\0';
}

static void on_ack(const dm_frame_t *f)
{
    cJSON *j = cJSON_ParseWithLength((const char *)f->payload, f->len);
    if (!j) {
        return;
    }
    usb_link_peer_t p;
    memset(&p, 0, sizeof p);
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(j, "v"), *api = cJSON_GetObjectItemCaseSensitive(j, "api"),
                *min = cJSON_GetObjectItemCaseSensitive(j, "min_api"), *id = cJSON_GetObjectItemCaseSensitive(j, "id");
    const bool ok = cJSON_IsNumber(v) && v->valuedouble == 1.0 && cJSON_IsNumber(api) && api->valuedouble >= 0 && api->valuedouble < 256 &&
                    cJSON_IsNumber(min) && min->valuedouble >= 0 && min->valuedouble < 256;
    if (ok) {
        p.api = (int)api->valuedouble;
        p.min_api = (int)min->valuedouble;
        p.cid_valid = cJSON_IsString(id) && hex8(id->valuestring, p.cid);
        copy_clean(p.name, sizeof p.name, cJSON_GetObjectItemCaseSensitive(j, "name"));
        copy_clean(p.app, sizeof p.app, cJSON_GetObjectItemCaseSensitive(j, "app"));
        const cJSON *c = cJSON_GetObjectItemCaseSensitive(j, "compat");
        p.compat = !cJSON_IsFalse(c) && p.api >= USB_LINK_MIN_API && USB_LINK_API >= p.min_api;
        const int64_t t = now_ms();
        portENTER_CRITICAL(&u.mux);
        const bool fresh = !u.peer_valid || u.last_rx_ms == 0 || t - u.last_rx_ms > COMPANION_STALE_MS ||
                           (p.cid_valid != u.peer.cid_valid) || (p.cid_valid && memcmp(p.cid, u.peer.cid, 8) != 0);
        u.peer = p;
        u.peer_valid = true;
        if (fresh) {
            u.epoch++;
        }
        portEXIT_CRITICAL(&u.mux);
    }
    cJSON_Delete(j);
}

static void pend_finish(bool ok)                     /* pend_lock held */
{
    u.pend.finished = true;
    u.pend.ok = ok;
    xSemaphoreGive(u.done_sem);
}

static void on_response(const dm_frame_t *f)
{
    xSemaphoreTake(u.pend_lock, portMAX_DELAY);
    if (u.pend.active && !u.pend.finished && u.pend.id == f->id) {
        const uint8_t *data = f->payload;
        size_t n = f->len;
        bool ok = true;
        if (f->flags & DM_F_FIRST) {
            if (u.pend.got_first || f->seq != 0 || n < 6) {
                ok = false;                                           /* a repeated first chunk, or one without its status / length */
            } else {
                *u.pend.status = (int)(data[0] | (data[1] << 8));
                u.pend.total = (uint32_t)data[2] | ((uint32_t)data[3] << 8) | ((uint32_t)data[4] << 16) | ((uint32_t)data[5] << 24);
                data += 6;
                n -= 6;
                u.pend.got_first = true;
                u.pend.next_seq = 1;
                if (u.pend.total > u.pend.buf->max || !usb_link_buf_reserve(u.pend.buf, u.pend.total)) {
                    u.pend.buf->truncated = true;                      /* does not fit: never read half of it */
                    ok = false;
                }
            }
        } else if (!u.pend.got_first || f->seq != u.pend.next_seq) {
            ok = false;                                               /* a chunk went missing or repeated */
        } else {
            u.pend.next_seq++;
        }
        if (ok) {
            if (u.pend.received + n > u.pend.total || !usb_link_buf_append(u.pend.buf, data, n)) {
                ok = false;
            } else {
                u.pend.received += (uint32_t)n;
                if (f->flags & DM_F_LAST) {
                    ok = u.pend.received == u.pend.total;
                    pend_finish(ok);
                }
            }
        }
        if (!ok && !u.pend.finished) {
            pend_finish(false);
        }
    }
    xSemaphoreGive(u.pend_lock);
}

static void on_frame(const dm_frame_t *f, void *ctx)
{
    (void)ctx;
    switch (f->type) {
    case DM_T_ACK:
        on_ack(f);
        break;
    case DM_T_RESPONSE:
        on_response(f);
        break;
    case DM_T_PING:
        if (f->len <= PONG_MAX) {
            tx_frame(DM_T_PONG, DM_F_FIRST | DM_F_LAST, 0, f->id, f->payload, f->len, 20);
        }
        break;
    case DM_T_PONG:
        break;
    case DM_T_DEBUG:
        if (u.debug_cb && f->len > 0 && f->len < 400) {             /* development builds: an action name, optionally followed by a space and arguments */
            char name[400];
            bool ok = true;
            for (uint16_t i = 0; i < f->len; i++) {
                const char c = (char)f->payload[i];
                ok = ok && c >= 0x20 && c < 0x7F;                    /* printable ASCII only */
                name[i] = c;
            }
            name[f->len] = '\0';
            if (ok) {
                u.debug_cb(name, u.debug_ctx);
            }
        }
        return;                                                   /* a development request is not proof that a companion is there */
    default:
        return;                                                       /* unknown: ignored, and it does not prove a companion is there */
    }
    const int64_t t = now_ms();
    portENTER_CRITICAL(&u.mux);
    u.last_rx_ms = t ? t : 1;
    portEXIT_CRITICAL(&u.mux);
}

/* A hostile stream of valid-looking headers (each claiming 4096 bytes of payload, 12 bytes apart) makes the parser check a 4 KB CRC for every 12
 * bytes that arrive: more than this core can do, which would starve the poll and tx tasks for as long as the stream lasts. Normal operation has
 * no CRC failure at all (hundreds of thousands of frames on the bench: 0), so a burst of them is treated as an attack or a broken cable: the
 * input is thrown away for a short while and parsing starts again from the next byte (the companion's retry logic repeats what was lost). */
#define RX_BAD_CRC_BURST    40                                      /* CRC failures within RX_BURST_WINDOW_MS ... */
#define RX_BURST_WINDOW_MS  1000
#define RX_COOLDOWN_MS      500                                     /* ... make the receiver deaf for this long */

static void rx_task(void *arg)
{
    (void)arg;
    static uint8_t chunk[1024];
    dm_parser_init(&u.parser);
    int64_t window_start = 0, cooldown_until = 0;
    uint32_t window_base = 0;
    for (;;) {
        int n = usb_serial_jtag_read_bytes(chunk, sizeof chunk, pdMS_TO_TICKS(25));
        const int64_t now = now_ms();
        const uint32_t t = (uint32_t)now;
        if (cooldown_until) {
            if (now < cooldown_until) {
                continue;                                           /* deaf: read and drop */
            }
            cooldown_until = 0;
            window_start = 0;
        }
        if (n > 0) {
            dm_parser_feed(&u.parser, chunk, (size_t)n, t, on_frame, NULL);
            if (now - window_start > RX_BURST_WINDOW_MS) {
                window_start = now;
                window_base = u.parser.bad_crc;
            } else if (u.parser.bad_crc - window_base > RX_BAD_CRC_BURST) {
                ESP_LOGW(TAG, "burst of damaged frames: input ignored for %d ms", RX_COOLDOWN_MS);
                u.parser.len = 0;
                cooldown_until = now + RX_COOLDOWN_MS;
            }
        } else {
            dm_parser_poll(&u.parser, t, on_frame, NULL);
        }
        u.st.rx_frames = u.parser.frames;
        u.st.rx_bad = u.parser.bad_header + u.parser.bad_crc + u.parser.oversize;
        u.st.rx_noise_bytes = u.parser.noise_bytes;
        u.st.rx_partial = u.parser.partial_dropped;
    }
}

/* ---- presence and log forwarding ------------------------------------------------------------------------------------------- */

static void tx_task(void *arg)
{
    (void)arg;
    int64_t next_beacon = 0;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(40));
        if (!host_present()) {
            size_t n;                                       /* nobody can read it: do not keep stale log text for later */
            void *p;
            while ((p = xRingbufferReceiveUpTo(u.log_rb, &n, 0, LOG_FRAME_BYTES)) != NULL) {
                vRingbufferReturnItem(u.log_rb, p);
            }
            next_beacon = 0;
            continue;
        }
        const int64_t t = now_ms();
        if (t >= next_beacon && tx_idle()) {                /* only when the previous frame was collected: nothing piles up while unread */
            send_beacon();
            next_beacon = t + (usb_link_state() == USB_LINK_COMPANION ? BEACON_SLOW_MS : BEACON_FAST_MS);
        }
        if (tx_idle()) {
            size_t n = 0;
            void *p = xRingbufferReceiveUpTo(u.log_rb, &n, 0, LOG_FRAME_BYTES);
            if (p) {
                tx_frame(DM_T_LOG, DM_F_FIRST | DM_F_LAST, 0, 0, p, n, 20);
                vRingbufferReturnItem(u.log_rb, p);
            }
        }
    }
}

/* esp_log sink: format, strip colour codes, hand to the TX task through a ring (never waits: a logging task must not stall on the
 * cable), and still pass the line on to the previous sink (the UART console). */
static int link_vprintf(const char *fmt, va_list ap)
{
    char line[LOG_LINE_MAX];
    va_list copy;
    va_copy(copy, ap);
    int n = vsnprintf(line, sizeof line, fmt, copy);
    va_end(copy);
    int r = u.old_vprintf ? u.old_vprintf(fmt, ap) : n;
    if (n > 0 && u.log_rb && host_present()) {
        if (n >= (int)sizeof line) {
            n = (int)sizeof line - 1;
        }
        char clean[LOG_LINE_MAX];
        size_t o = 0;
        for (int i = 0; i < n; i++) {
            if (line[i] == 0x1B) {                           /* ESC [ ... m */
                while (i < n && line[i] != 'm') {
                    i++;
                }
                continue;
            }
            clean[o++] = line[i];
        }
#ifndef APP_DEV_TOOLS
        if (o == 0 || (clean[0] != 'W' && clean[0] != 'E')) {
            return r;                                        /* product builds mirror warnings and errors only: ordinary lines name the playing track */
        }
#endif
        if (o && xRingbufferSend(u.log_rb, clean, o, 0) != pdTRUE) {
            u.st.log_dropped++;
        }
    }
    return r;
}

void usb_link_log_install(void)
{
    if (u.started && !u.log_installed) {
        u.log_installed = true;
        u.old_vprintf = esp_log_set_vprintf(link_vprintf);
    }
}

bool usb_link_log_line(const char *line)
{
    if (!u.started || !host_present()) {
        return false;
    }
    const size_t n = strlen(line);
    for (size_t off = 0; off < n || off == 0; off += LOG_FRAME_BYTES) {
        size_t m = n - off < LOG_FRAME_BYTES ? n - off : LOG_FRAME_BYTES;
        bool sent = false;
        for (int attempt = 0; attempt < 40 && !sent; attempt++) {
            sent = tx_frame(DM_T_LOG, DM_F_FIRST | DM_F_LAST, 0, 0, line + off, m, 100);
            if (!sent) {
                vTaskDelay(pdMS_TO_TICKS(20));                      /* the host is slow or just blipped: wait instead of burning the attempts */
            }
        }
        if (!sent) {
            return false;
        }
        if (n == 0) {
            break;
        }
    }
    return true;
}

/* ---- queries ----------------------------------------------------------------------------------------------------------------- */

usb_link_state_t usb_link_state(void)
{
    if (!u.started || !host_present()) {
        return USB_LINK_NO_HOST;
    }
    portENTER_CRITICAL(&u.mux);
    const int64_t last = u.last_rx_ms;
    portEXIT_CRITICAL(&u.mux);
    return (last != 0 && now_ms() - last <= COMPANION_STALE_MS) ? USB_LINK_COMPANION : USB_LINK_NO_COMPANION;
}

bool usb_link_peer(usb_link_peer_t *out)
{
    portENTER_CRITICAL(&u.mux);
    const bool ok = u.peer_valid;
    if (ok) {
        *out = u.peer;
    }
    portEXIT_CRITICAL(&u.mux);
    return ok;
}

uint32_t usb_link_epoch(void)
{
    portENTER_CRITICAL(&u.mux);
    const uint32_t e = u.epoch;
    portEXIT_CRITICAL(&u.mux);
    return e;
}

void usb_link_set_debug_handler(void (*cb)(const char *action, void *ctx), void *ctx)
{
    u.debug_ctx = ctx;
    u.debug_cb = cb;
}

void usb_link_stats(usb_link_stats_t *out)
{
    *out = u.st;
}

/* ---- the request / response exchange ------------------------------------------------------------------------------------------ */

esp_err_t usb_link_request(const char *method, const char *target, const char *body, int timeout_ms, usb_link_buf_t *resp, int *status)
{
    if (!u.started || !method || !target || !resp || !status) {
        return ESP_ERR_INVALID_ARG;
    }
    *status = 0;
    resp->len = 0;
    resp->truncated = false;
    if (resp->data) {
        resp->data[0] = '\0';
    }
    static char req[REQ_MAX_BYTES];                      /* single caller (req_lock); the payload is built once and re-sent as it is */
    xSemaphoreTake(u.req_lock, portMAX_DELAY);
    esp_err_t result = ESP_FAIL;
    const size_t blen = body ? strlen(body) : 0;
    int n = snprintf(req, sizeof req, "%s %s\n", method, target);
    if (n <= 0 || (size_t)n + blen > sizeof req || strlen(target) > 512) {
        xSemaphoreGive(u.req_lock);
        return ESP_ERR_INVALID_SIZE;
    }
    memcpy(req + n, body ? body : "", blen);
    const size_t rlen = (size_t)n + blen;
    if (!host_present()) {
        xSemaphoreGive(u.req_lock);
        return ESP_ERR_INVALID_STATE;
    }
    uint16_t id = ++u.next_id;
    if (id == 0) {
        id = ++u.next_id;
    }
    xSemaphoreTake(u.pend_lock, portMAX_DELAY);
    memset(&u.pend, 0, sizeof u.pend);
    u.pend.active = true;
    u.pend.id = id;
    u.pend.buf = resp;
    u.pend.status = status;
    xSemaphoreTake(u.done_sem, 0);                       /* clear a stale give */
    xSemaphoreGive(u.pend_lock);

    const int64_t deadline = now_ms() + timeout_ms;
    for (int attempt = 0; attempt < 2; attempt++) {
        if (!tx_frame(DM_T_REQUEST, DM_F_FIRST | DM_F_LAST, 0, id, req, rlen, TX_WAIT_MS)) {
            result = ESP_ERR_TIMEOUT;                    /* nobody is collecting what we write */
            break;
        }
        int64_t wait = attempt == 0 ? (int64_t)timeout_ms * 6 / 10 : deadline - now_ms();
        if (wait < 1) {
            wait = 1;
        }
        const bool got = xSemaphoreTake(u.done_sem, pdMS_TO_TICKS(wait)) == pdTRUE;
        xSemaphoreTake(u.pend_lock, portMAX_DELAY);
        const bool finished = u.pend.finished, ok = u.pend.ok, started = u.pend.got_first;
        xSemaphoreGive(u.pend_lock);
        if (got && finished) {
            result = ok ? ESP_OK : (resp->truncated ? ESP_ERR_INVALID_SIZE : ESP_ERR_INVALID_RESPONSE);
            break;
        }
        if (started) {                                   /* chunks are arriving: keep waiting for the rest instead of repeating */
            const int64_t left = deadline - now_ms();
            if (xSemaphoreTake(u.done_sem, pdMS_TO_TICKS(left > 1 ? left : 1)) == pdTRUE) {
                xSemaphoreTake(u.pend_lock, portMAX_DELAY);
                result = u.pend.ok ? ESP_OK : (resp->truncated ? ESP_ERR_INVALID_SIZE : ESP_ERR_INVALID_RESPONSE);
                xSemaphoreGive(u.pend_lock);
            } else {
                result = ESP_ERR_TIMEOUT;
            }
            break;
        }
        result = ESP_ERR_TIMEOUT;
        if (attempt == 0) {
            u.st.req_retry++;
        }
    }
    xSemaphoreTake(u.pend_lock, portMAX_DELAY);
    u.pend.active = false;
    xSemaphoreGive(u.pend_lock);
    if (result == ESP_OK) {
        u.st.req_ok++;
    } else {
        u.st.req_fail++;
        *status = 0;
    }
    xSemaphoreGive(u.req_lock);
    return result;
}

/* ---- start --------------------------------------------------------------------------------------------------------------------- */

esp_err_t usb_link_start(const usb_link_config_t *cfg)
{
    if (u.started || !cfg) {
        return ESP_ERR_INVALID_STATE;
    }
    u.cfg = *cfg;
    u.boot_id = esp_random();
    u.tx_lock = xSemaphoreCreateMutex();
    u.req_lock = xSemaphoreCreateMutex();
    u.pend_lock = xSemaphoreCreateMutex();
    u.done_sem = xSemaphoreCreateBinary();
    u.log_rb = xRingbufferCreate(LOG_RING_BYTES, RINGBUF_TYPE_BYTEBUF);
    if (!u.tx_lock || !u.req_lock || !u.pend_lock || !u.done_sem || !u.log_rb) {
        return ESP_ERR_NO_MEM;
    }
    usb_serial_jtag_driver_config_t dcfg = { .tx_buffer_size = DRIVER_TX_BYTES, .rx_buffer_size = DRIVER_RX_BYTES };
    esp_err_t err = usb_serial_jtag_driver_install(&dcfg);
    if (err != ESP_OK) {
        return err;
    }
    u.started = true;
#ifdef APP_DEV_TOOLS
    const char *bad = dm_selftest();
    if (bad) {
        ESP_LOGE(TAG, "dmlink selftest FAILED: %s", bad);
    } else {
        ESP_LOGI(TAG, "dmlink selftest ok");
    }
#endif
    if (xTaskCreatePinnedToCore(rx_task, "usb_rx", 4096, NULL, 4, NULL, 0) != pdPASS ||
        xTaskCreatePinnedToCore(tx_task, "usb_tx", 4096, NULL, 3, NULL, 0) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    ESP_LOGI(TAG, "USB link up (DMLINK v%d, api %d)", DM_VERSION, USB_LINK_API);
    return ESP_OK;
}

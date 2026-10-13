/*
 * pc_media: finding the Windows companion (USB cable first, the local network as an optional fallback), pairing with it, polling of
 * /now-playing, artwork download, control commands. Never touches LVGL - results go out through the callbacks in pc_media_config_t.
 *
 * Transports. Every exchange goes through xfer(): over USB it is a DMLINK request (usb_link), over the LAN an HTTP request (pc_http).
 * USB is used whenever a companion answers on the cable; otherwise the LAN, when Wi-Fi is configured and a companion was found. The
 * routes, the JSON and the pairing handshake are identical; only the proof of identity differs: a USB link opens one session with the
 * key (usb_session_open), a LAN request is signed one by one (pc_http.c).
 *
 * Pairing (docs/protocol.md sections 2 and 3). The poll task is in one of three states:
 *   UNPAIRED  no key stored: only GET /hello is polled (every 2 s); when the companion has its pairing window open the handshake runs;
 *   PAIRED    key stored and accepted: the normal poll, every request signed, every answer verified (pc_http.c);
 *   LOST      a key is stored but the companion refuses it ("unpaired" 401, three failed signatures in a row, or no valid answer for
 *             120 s): like UNPAIRED, plus a rare signed probe so a merely sleeping PC is picked up again without any user action.
 */
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>

#include "cJSON.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "lwip/sockets.h"

#include "esp_app_desc.h"
#include "pc_internal.h"
#include "pc_media.h"
#include "usb_link.h"
#include "wifi_mgr.h"

static const char *TAG = "pc_media";

#define POLL_MS             1000
#define POLL_AFTER_CMD_MS   200
#define POLL_FAILING_MS     2000
#define HTTP_TIMEOUT_MS     3000
#define FAILS_BEFORE_LOST   3
#define ART_MAX_BYTES       (160 * 1024)
#define JSON_MAX_BYTES      (16 * 1024)
#define ART_RETRIES         3
#define IDLE_IMG_MAX_BYTES  (300 * 1024)        /* the companion keeps the idle picture under 200 KB */
#define BEACON_MAGIC        "DESKMEDIA2 "
#define BEACON_MAX_BYTES    320
#define NAME_BYTES          64                  /* a PC name as the beacon / hello carries it, bounded and printable */
#define HELLO_MS            2000                /* unpaired / lost: how often GET /hello asks whether a pairing window is open */
#define HELLO_TIMEOUT_MS    2000
#define PAIR_RETRY_MS       10000               /* after a cancelled / failed attempt: no new offer for this long */
#define PAIR_USER_MS        120000              /* the user has this long to compare the codes and tap Pair / Cancel */
#define PAIR_PC_MS          120000              /* ... and this long after tapping Pair for the click on the PC */
#define PAIR_STATUS_MS      2000                /* while the prompt waits: how often the PC is asked whether the attempt is still alive */
#define LOST_AFTER_MS       120000              /* paired, but no valid answer for this long: accept a new pairing offer */
#define AUTH_FAILS_LOST     3                   /* paired: this many auth failures in a row (401 / no valid proof): same */
#define PROBE_MS            20000               /* lost: how often a signed request tries whether the old key still works */
#define BLOCKED_MS          30000               /* the companion answered 429 (this address is blocked for up to 60 s): stay quiet this long */
#define USB_GRACE_MS        6000                /* after power-up: this long without USB data is normal (the PC is still enumerating us) */
#define USB_ANSWER_MS       5000                /* USB data for this long without a companion answering: it is not installed / not running */
#define USB_TIMEOUT_MS      3000
#define COMPANION_GONE_MS   60000           /* a companion that was there and is silent for this long is "not running", not "restarting" */

typedef struct {
    pc_media_cmd_t cmd;
    uint32_t arg;
} cmd_msg_t;

typedef enum { X_NONE, X_USB, X_LAN } xport_t;

static struct {
    pc_media_callbacks_t cbs;
    QueueHandle_t q;
    bool lan_enabled;                   /* Wi-Fi is configured: the LAN fallback exists */
    xport_t xport;                      /* the transport in use right now (poll task) */
    int64_t boot_ms, usb_since_ms;      /* start-up time; when USB data first appeared (continuously) */
    int64_t usb_seen_ms;                /* the last time there was USB data */
    int64_t companion_seen_ms;          /* the last time a companion answered on USB (0 = never since boot) */
    bool session_ok;                    /* USB: the key was proved on this link */
    uint32_t session_epoch;             /* usb_link_epoch() when it was proved: a changed epoch = the companion restarted */
    pc_auth_result_t usb_last;          /* what the last USB exchange learned about authentication (the LAN keeps its own in pc_http.c) */
    SemaphoreHandle_t lock;             /* guards host / port / host_valid */
    char host[16];
    uint16_t port;
    bool host_valid;
    bool host_static;
    unsigned failures;
    pc_media_state_t st;
    uint16_t art_max_w, art_max_h;
    char art_shown[16];
    int art_failures;
    pc_media_status_t status;
    char status_detail[64];
    bool status_valid;
    /* pairing (poll task unless noted; `cid_valid`, `cid`, `lost`, `host_*` are also read by the discovery task, under `lock`) */
    bool cid_valid;                     /* the paired companion's id is known (a key is stored) */
    uint8_t cid[8];
    bool lost;                          /* a key is stored but the companion no longer accepts it */
    uint8_t host_id[8];                 /* id / name / "window open" flag of the current host, from its beacon or its /hello */
    bool host_id_valid, host_pairing;
    char host_name[NAME_BYTES];
    unsigned auth_fails;                /* consecutive signed requests refused (401 "auth" after the retry, or no valid proof) */
    int64_t last_valid_ms;              /* when a signed request was last answered with a valid proof */
    int64_t pair_retry_at;              /* no new pairing offer before this time */
    int64_t probe_at;                   /* lost: no signed probe before this time */
    int64_t blocked_until;              /* no request at all before this time (after a 429) */
    atomic_int pair_answer;             /* 0 = waiting, 1 = Pair, 2 = Cancel (written by pc_media_pair_answer, from the LVGL task) */
    atomic_bool prompt_active;          /* the pairing prompt is showing: answers are only accepted then */
    SemaphoreHandle_t answer_sem;       /* given by pc_media_pair_answer(): wakes the waits in pair_attempt() at once */
    pc_pair_t pair;                     /* the attempt in progress */
    pc_resp_t resp;                     /* JSON / small replies */
    pc_resp_t art;                      /* JPEG download */
    pc_resp_t idle_img;                 /* idle screen picture (allocated only while it is downloaded) */
    unsigned idle_fails;                /* consecutive failed idle syncs (back-off) */
    int64_t idle_retry_at;              /* no new sync attempt before this time */
    bool started;
} s;

static int64_t now_ms(void);

/* ---- helpers ------------------------------------------------------------------------------------ */

static void emit_status(pc_media_status_t st, const char *detail)
{
    detail = detail ? detail : "";
    if (s.status_valid && s.status == st && strcmp(s.status_detail, detail) == 0) {
        return;
    }
    s.status = st;
    s.status_valid = true;
    strlcpy(s.status_detail, detail, sizeof s.status_detail);
    ESP_LOGI(TAG, "status %d%s%s", (int)st, detail[0] ? ": " : "", detail);
    if (s.cbs.on_status) {
        s.cbs.on_status(st, detail, s.cbs.ctx);
    }
}

/* strlcpy that never leaves a half UTF-8 character at the end (LVGL would draw garbage for it). */
static void copy_utf8(char *dst, size_t n, const char *src)
{
    size_t len = strlcpy(dst, src, n);
    if (len >= n) {
        size_t i = n - 1;
        while (i > 0 && ((unsigned char)dst[i] & 0xC0) == 0x80) {
            i--;                                   /* back up over continuation bytes */
        }
        if (i > 0 && (unsigned char)dst[i] >= 0xC0) {
            dst[i] = '\0';                         /* drop the lead byte of the cut character */
        }
    }
}

static void jstr(const cJSON *obj, const char *key, char *out, size_t n)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(obj, key);
    out[0] = '\0';
    if (cJSON_IsString(v)) {
        copy_utf8(out, n, v->valuestring);
    }
}

static int64_t jint64(const cJSON *obj, const char *key)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(obj, key);
    return (cJSON_IsNumber(v) && v->valuedouble > 0) ? (int64_t)v->valuedouble : 0;
}

static uint32_t jnum(const cJSON *obj, const char *key)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(obj, key);
    return (cJSON_IsNumber(v) && v->valuedouble > 0) ? (uint32_t)v->valuedouble : 0;
}

static uint8_t jnum8(const cJSON *obj, const char *key)
{
    uint32_t v = jnum(obj, key);
    return v > 255 ? 255 : (uint8_t)v;
}

static bool jbool(const cJSON *obj, const char *key)
{
    return cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(obj, key));
}

static void wipe_buf(void *p, size_t n)
{
    volatile uint8_t *b = p;
    while (n--) {
        *b++ = 0;
    }
}

/* ---- pairing state --------------------------------------------------------------------------------- */

static bool is_lost(void)
{
    xSemaphoreTake(s.lock, portMAX_DELAY);
    const bool lost = s.lost;
    xSemaphoreGive(s.lock);
    return lost;
}

static void set_lost(bool lost)
{
    xSemaphoreTake(s.lock, portMAX_DELAY);
    s.lost = lost;
    xSemaphoreGive(s.lock);
}

/* The companion no longer accepts our key. The key stays stored (the PC may merely have been asleep): the poll task falls back to
 * /hello polling, accepts a pairing offer and probes the old key now and then. */
static void enter_lost(const char *why)
{
    if (is_lost()) {
        return;
    }
    set_lost(true);
    s.auth_fails = 0;
    s.probe_at = 0;
    ESP_LOGW(TAG, "pairing lost: %s", why);
}

static pc_auth_result_t last_auth(void)
{
    return s.xport == X_USB ? s.usb_last : pc_http_last_auth();
}

/* A 429 means the companion's failure limiter has blocked this address: stop talking for a while instead of hammering it. */
static void note_limited(void)
{
    if (last_auth() == PC_AUTH_LIMITED) {
        s.blocked_until = now_ms() + BLOCKED_MS;
        ESP_LOGW(TAG, "the companion is limiting this display (HTTP 429): backing off");
    }
}

/* ---- the two transports ---------------------------------------------------------------------------------------------------- */

static bool route_is_open(const char *pq)               /* the routes that need no proof of identity (docs/protocol.md section 2) */
{
    const size_t n = strcspn(pq, "?");
    return (n == 6 && strncmp(pq, "/hello", 6) == 0) || (n == 7 && strncmp(pq, "/health", 7) == 0) || strncmp(pq, "/pair/", 6) == 0 ||
           strncmp(pq, "/link/", 6) == 0;
}

static bool lan_host(char *host, size_t n, uint16_t *port)
{
    bool ok;
    xSemaphoreTake(s.lock, portMAX_DELAY);
    ok = s.host_valid;
    if (ok) {
        strlcpy(host, s.host, n);
        *port = s.port;
    }
    xSemaphoreGive(s.lock);
    return ok;
}

/* One unauthenticated-or-session exchange over USB; status and body land in resp like an HTTP answer. */
static esp_err_t usb_call(const char *path_query, bool post, const char *json, int timeout_ms, pc_resp_t *resp)
{
    int status = 0;
    esp_err_t err = usb_link_request(post ? "POST" : "GET", path_query, json, timeout_ms, &resp->body, &status);
    resp->status = err == ESP_OK ? status : 0;
    return err;
}

/* The key is proved once per link (docs/usb-protocol.md section 4): two exchanges, both sides bind both nonces. */
static esp_err_t usb_session_open(int timeout_ms)
{
    uint8_t dev[8], cid[8], nd[16], nc[16], want[32], got[32];
    char body[160], dev_hex[17], nd_hex[33];
    if (!pc_auth_has_key() || !pc_auth_cid(cid) || !pc_auth_dev_id(dev)) {
        s.usb_last = PC_AUTH_NO_KEY;
        return ESP_ERR_INVALID_STATE;
    }
    pc_auth_random(nd, sizeof nd);
    pc_hex_encode(dev, sizeof dev, dev_hex);
    pc_hex_encode(nd, sizeof nd, nd_hex);
    snprintf(body, sizeof body, "{\"dev\":\"%s\",\"nd\":\"%s\"}", dev_hex, nd_hex);
    pc_resp_t r;
    pc_resp_init(&r, 1024);
    esp_err_t err = usb_call("/link/open", true, body, timeout_ms, &r);
    cJSON *j = (err == ESP_OK && r.status == 200 && r.body.data) ? cJSON_ParseWithLength(r.body.data, r.body.len) : NULL;
    if (err == ESP_OK && r.status == 401) {
        s.usb_last = (r.body.data && strstr(r.body.data, "\"unpaired\"")) ? PC_AUTH_UNPAIRED : PC_AUTH_FAILED;
        err = ESP_ERR_INVALID_RESPONSE;
    } else if (err == ESP_OK && r.status == 429) {
        s.usb_last = PC_AUTH_LIMITED;
        err = ESP_ERR_INVALID_RESPONSE;
    } else if (err == ESP_OK) {
        const cJSON *nc_v = j ? cJSON_GetObjectItemCaseSensitive(j, "nc") : NULL, *pr_v = j ? cJSON_GetObjectItemCaseSensitive(j, "proof") : NULL;
        const bool fields = jbool(j, "ok") && cJSON_IsString(nc_v) && cJSON_IsString(pr_v) &&
                            pc_hex_decode(nc_v->valuestring, strlen(nc_v->valuestring), nc, sizeof nc) &&
                            pc_hex_decode(pr_v->valuestring, strlen(pr_v->valuestring), got, sizeof got);
        if (!fields || !pc_link_proof(true, dev, nd, nc, want) || !pc_auth_ct_equal(want, got, sizeof want)) {
            ESP_LOGW(TAG, "USB session: the companion's proof is missing or wrong");
            s.usb_last = PC_AUTH_BAD_PROOF;
            err = ESP_ERR_INVALID_RESPONSE;
        }
    }
    cJSON_Delete(j);
    pc_resp_free(&r);
    if (err != ESP_OK) {
        return err;
    }
    uint8_t mine[32];
    char proof_hex[65];
    if (!pc_link_proof(false, dev, nd, nc, mine)) {
        return ESP_FAIL;
    }
    pc_hex_encode(mine, sizeof mine, proof_hex);
    snprintf(body, sizeof body, "{\"dev\":\"%s\",\"proof\":\"%s\"}", dev_hex, proof_hex);
    pc_resp_init(&r, 1024);
    err = usb_call("/link/auth", true, body, timeout_ms, &r);
    if (err == ESP_OK && r.status == 200) {
        s.session_ok = true;
        s.session_epoch = usb_link_epoch();
    } else if (err == ESP_OK) {
        s.usb_last = r.status == 429 ? PC_AUTH_LIMITED : PC_AUTH_FAILED;
        err = ESP_ERR_INVALID_RESPONSE;
    }
    wipe_buf(proof_hex, sizeof proof_hex);
    pc_resp_free(&r);
    return err;
}

/* An exchange over the transport in use. Authenticated routes need the key (USB: a session; LAN: signed by pc_http.c); the open
 * routes (hello, health, the pairing and session routes) need nothing. `json` = POST body (only the pairing and session routes take one). */
static esp_err_t xfer(const char *path_query, bool post, const char *json, int timeout_ms, pc_resp_t *resp)
{
    if (s.xport == X_USB) {
        s.usb_last = PC_AUTH_NONE;
        const bool open = route_is_open(path_query);
        if (!open) {
            if (!pc_auth_has_key()) {
                s.usb_last = PC_AUTH_NO_KEY;
                return ESP_ERR_INVALID_STATE;
            }
            if (s.session_ok && s.session_epoch != usb_link_epoch()) {
                s.session_ok = false;                               /* the companion restarted: its session is gone */
            }
        }
        for (int attempt = 0; attempt < 2; attempt++) {
            if (!open && !s.session_ok) {
                esp_err_t e = usb_session_open(timeout_ms);
                if (e != ESP_OK) {
                    return e;
                }
            }
            esp_err_t err = usb_call(path_query, post, json, timeout_ms, resp);
            if (err != ESP_OK || open) {
                if (err == ESP_OK && resp->status == 429) {
                    s.usb_last = PC_AUTH_LIMITED;
                }
                return err;
            }
            if (resp->status == 401) {
                const bool unpaired = resp->body.data && strstr(resp->body.data, "\"unpaired\"") != NULL;
                s.session_ok = false;
                s.usb_last = unpaired ? PC_AUTH_UNPAIRED : PC_AUTH_FAILED;
                if (!unpaired && attempt == 0) {
                    continue;                                       /* the session was lost (companion restarted): open a new one, once */
                }
                return ESP_OK;
            }
            if (resp->status == 429) {
                s.usb_last = PC_AUTH_LIMITED;
            } else if (resp->status == 426) {
                s.usb_last = PC_AUTH_FAILED;                        /* "update needed": the status line explains it (see select_transport) */
            } else {
                s.usb_last = PC_AUTH_OK;
            }
            return ESP_OK;
        }
        return ESP_FAIL;
    }
    char host[16];
    uint16_t port;
    if (s.xport != X_LAN || !lan_host(host, sizeof host, &port)) {
        return ESP_ERR_INVALID_STATE;
    }
    return json ? pc_http_post_json(host, port, path_query, json, timeout_ms, resp) : pc_http_request(host, port, path_query, post, timeout_ms, resp);
}

/* Every authenticated request to the companion goes through here: besides sending it, it keeps the authentication bookkeeping. */
static esp_err_t req(const char *path_query, bool post, int timeout_ms, pc_resp_t *resp)
{
    esp_err_t err = xfer(path_query, post, NULL, timeout_ms, resp);
    switch (last_auth()) {
    case PC_AUTH_OK:
        s.auth_fails = 0;
        s.last_valid_ms = now_ms();
        break;
    case PC_AUTH_UNPAIRED:
        enter_lost("the PC does not know this display");
        break;
    case PC_AUTH_FAILED:
    case PC_AUTH_BAD_PROOF:
        if (++s.auth_fails >= AUTH_FAILS_LOST) {
            enter_lost("signed requests keep being refused");
        }
        break;
    case PC_AUTH_LIMITED:
        note_limited();
        break;
    default:
        break;
    }
    return err;
}

/* ---- discovery ------------------------------------------------------------------------------------ */

typedef struct {
    uint16_t port;
    uint8_t id[8];
    char name[NAME_BYTES];
    bool paired, pairing;
} beacon_t;

/* A name from the network ends up in the log and on the pairing prompt: keep printable ASCII-ish bytes only. */
static void clean_name(char *str)
{
    for (; *str; str++) {
        if ((unsigned char)*str < 0x20 || *str == 0x7F) {
            *str = ' ';
        }
    }
}

/* A string member that must be exactly 2 * n hex digits, decoded. */
static bool jhex(const cJSON *obj, const char *key, uint8_t *out, size_t n)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(obj, key);
    return cJSON_IsString(v) && v->valuestring && pc_hex_decode(v->valuestring, strlen(v->valuestring), out, n);
}

/* {"v":2,"port":8765,"name":"...","id":"<16 hex>","paired":bool,"pairing":bool}: everything is validated, nothing is trusted. */
static bool parse_beacon(const char *json, beacon_t *b)
{
    cJSON *j = cJSON_Parse(json);
    if (!j) {
        return false;
    }
    memset(b, 0, sizeof *b);
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(j, "v");
    const cJSON *p = cJSON_GetObjectItemCaseSensitive(j, "port");
    const bool ok = cJSON_IsNumber(v) && v->valuedouble == 2.0 && cJSON_IsNumber(p) && p->valuedouble >= 1.0 && p->valuedouble <= 65535.0 &&
                    p->valuedouble == (double)(int)p->valuedouble && jhex(j, "id", b->id, sizeof b->id);
    if (ok) {
        b->port = (uint16_t)p->valuedouble;
        jstr(j, "name", b->name, sizeof b->name);
        clean_name(b->name);
        b->paired = jbool(j, "paired");
        b->pairing = jbool(j, "pairing");
    }
    cJSON_Delete(j);
    return ok;
}

static void note_host(const uint8_t id[8], const char *name, bool pairing)   /* `lock` held */
{
    memcpy(s.host_id, id, sizeof s.host_id);
    s.host_id_valid = true;
    s.host_pairing = pairing;
    strlcpy(s.host_name, name, sizeof s.host_name);
}

/* Host selection. A display that holds a key listens only to its own companion (and takes a new address of it at once); while it
 * is unpaired or lost the first companion wins, as before, and another one takes over only after failures - or when it announces
 * an open pairing window while the current one does not (the user is pairing with that PC). */
static void handle_beacon(const char *json, const char *ip)
{
    beacon_t b;
    if (!parse_beacon(json, &b)) {
        return;
    }
    xSemaphoreTake(s.lock, portMAX_DELAY);
    const bool same = s.host_valid && strcmp(s.host, ip) == 0 && s.port == b.port;
    const bool from_paired = s.cid_valid && memcmp(s.cid, b.id, sizeof b.id) == 0;
    bool accept;
    if (s.cid_valid && (!s.lost || from_paired)) {
        /* paired (or lost, and its companion is back): only that one, and only when the current host is not answering - a
         * beacon is unauthenticated, so a look-alike announcing the same id must not pull the display away from a host that works
         * (a rogue that wins the race at start-up is dropped again by its own failed proofs: failures > 0) */
        accept = from_paired && !same && (!s.host_valid || s.failures > 0);
    } else {
        accept = !s.host_valid || (!same && (s.failures > 0 || (b.pairing && !s.host_pairing)));
    }
    if (accept) {
        strlcpy(s.host, ip, sizeof s.host);
        s.port = b.port;
        s.host_valid = true;
        s.failures = 0;
    }
    if (accept || same) {
        note_host(b.id, b.name, b.pairing);
    }
    xSemaphoreGive(s.lock);
    if (accept) {
        ESP_LOGI(TAG, "found companion at %s:%u (%s)", ip, (unsigned)b.port, b.name);
    }
}

static void discovery_task(void *arg)
{
    (void)arg;
    for (;;) {
        if (!wifi_mgr_is_connected()) {
            wifi_mgr_wait_connected(2000);
            continue;
        }
        int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
        if (sock < 0) {
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }
        int one = 1;
        setsockopt(sock, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
        struct sockaddr_in addr = { .sin_family = AF_INET, .sin_port = htons(PC_MEDIA_DISCOVERY_PORT) };
        addr.sin_addr.s_addr = htonl(INADDR_ANY);
        struct timeval tv = { .tv_sec = 1 };
        setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
        if (bind(sock, (struct sockaddr *)&addr, sizeof addr) != 0) {
            ESP_LOGW(TAG, "discovery bind failed");
            close(sock);
            vTaskDelay(pdMS_TO_TICKS(2000));
            continue;
        }
        while (wifi_mgr_is_connected()) {
            char buf[BEACON_MAX_BYTES];
            struct sockaddr_in from;
            socklen_t fl = sizeof from;
            int n = recvfrom(sock, buf, sizeof buf - 1, 0, (struct sockaddr *)&from, &fl);
            if (n <= (int)strlen(BEACON_MAGIC)) {
                continue;                                    /* timeout or junk */
            }
            buf[n] = '\0';
            if (strncmp(buf, BEACON_MAGIC, strlen(BEACON_MAGIC)) != 0) {
                continue;
            }
            char ip[16];
            inet_ntoa_r(from.sin_addr, ip, sizeof ip);
            handle_beacon(buf + strlen(BEACON_MAGIC), ip);
        }
        close(sock);
    }
}

/* ---- artwork ------------------------------------------------------------------------------------ */

static void update_art(void)
{
    const char *want = s.st.has_session ? s.st.art_id : "";
    if (strcmp(want, s.art_shown) == 0) {
        return;
    }
    if (!want[0]) {
        s.art_shown[0] = '\0';
        s.art_failures = 0;
        if (s.cbs.on_art) {
            s.cbs.on_art(NULL, NULL, s.cbs.ctx);
        }
        return;
    }
    int64_t t0 = esp_timer_get_time();
    const lv_image_dsc_t *dsc = NULL, *bg = NULL;
    char path[56];
    snprintf(path, sizeof path, "/artwork?size=%u&maxw=%u", (unsigned)s.art_max_h, (unsigned)s.art_max_w);
    esp_err_t err = req(path, false, 6000, &s.art);
    if (err == ESP_OK && s.art.status == 200 && s.art.body.len > 100 && !s.art.body.truncated) {
        err = pc_art_decode((const uint8_t *)s.art.body.data, s.art.body.len, &dsc, &bg);
    } else {
        err = ESP_FAIL;
    }
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "artwork ready (%ux%u, %u bytes JPEG) in %d ms", (unsigned)dsc->header.w, (unsigned)dsc->header.h,
                 (unsigned)s.art.body.len, (int)((esp_timer_get_time() - t0) / 1000));
        strlcpy(s.art_shown, want, sizeof s.art_shown);
        s.art_failures = 0;
        if (s.cbs.on_art) {
            s.cbs.on_art(dsc, bg, s.cbs.ctx);
        }
    } else if (++s.art_failures >= ART_RETRIES) {
        ESP_LOGW(TAG, "artwork failed %d times (HTTP %d), giving up on this image", s.art_failures, s.art.status);
        strlcpy(s.art_shown, want, sizeof s.art_shown);
        s.art_failures = 0;
        if (s.cbs.on_art) {
            s.cbs.on_art(NULL, NULL, s.cbs.ctx);
        }
    }
}

/* ---- idle screen settings --------------------------------------------------------------------------- */

/* The companion's idle-screen settings changed (its version is in every /now-playing answer): fetch them (and the picture, if it
 * changed) and hand them to the idle screen, which stores them in its own flash. Runs in the poll task: a settings change blocks the
 * poll for a moment (the picture is under 200 KB), and a failure is retried with a back-off (5 s, 15 s, then every minute). */
static void idle_sync(uint32_t remote_v)
{
    if (!remote_v || !s.cbs.idle_config || (s.cbs.idle_have && s.cbs.idle_have(remote_v, s.cbs.ctx))) {
        s.idle_fails = 0;
        return;
    }
    int64_t now = now_ms();
    if (now < s.idle_retry_at) {
        return;
    }
    bool ok = false;
    esp_err_t err = req("/idle", false, 4000, &s.resp);
    if (err == ESP_OK && s.resp.status == 200 && s.resp.body.data && !s.resp.body.truncated) {
        int need = s.cbs.idle_config(s.resp.body.data, s.resp.body.len, s.cbs.ctx);
        if (need == 0) {
            ok = true;
        } else if (need == 1 && s.cbs.idle_picture) {
            pc_resp_init(&s.idle_img, IDLE_IMG_MAX_BYTES);
            err = req("/idle/image", false, 10000, &s.idle_img);
            if (err == ESP_OK && s.idle_img.status == 200 && !s.idle_img.body.truncated && s.idle_img.body.len > 100) {
                ok = s.cbs.idle_picture((const uint8_t *)s.idle_img.body.data, s.idle_img.body.len, s.cbs.ctx);
            } else {
                ESP_LOGW(TAG, "idle picture download failed (%s, HTTP %d)", esp_err_to_name(err), s.idle_img.status);
            }
            pc_resp_free(&s.idle_img);
        }
    } else {
        ESP_LOGW(TAG, "idle settings download failed (%s, HTTP %d)", esp_err_to_name(err), s.resp.status);
    }
    if (ok) {
        s.idle_fails = 0;
    } else {
        s.idle_fails++;
        s.idle_retry_at = now_ms() + (s.idle_fails <= 1 ? 5000 : (s.idle_fails == 2 ? 15000 : 60000));
    }
}

/* ---- polling -------------------------------------------------------------------------------------- */

/* A /now-playing JSON object -> state. Also used for the "now" object inside the answer to a source switch. */
static void parse_state(const cJSON *root, pc_media_state_t *ns)
{
    memset(ns, 0, sizeof *ns);
    ns->has_session = jbool(root, "has_session");
    ns->playing = jbool(root, "playing");
    jstr(root, "state", ns->state, sizeof ns->state);
    jstr(root, "app", ns->app, sizeof ns->app);
    jstr(root, "source", ns->source, sizeof ns->source);
    ns->sources = jnum8(root, "sources");
    ns->source_index = jnum8(root, "source_index");
    jstr(root, "title", ns->title, sizeof ns->title);
    jstr(root, "artist", ns->artist, sizeof ns->artist);
    jstr(root, "album", ns->album, sizeof ns->album);
    jstr(root, "track_id", ns->track_id, sizeof ns->track_id);
    jstr(root, "art_id", ns->art_id, sizeof ns->art_id);
    ns->position_ms = jnum(root, "position_ms");
    ns->duration_ms = jnum(root, "duration_ms");
    ns->can_play = jbool(root, "can_play");
    ns->can_pause = jbool(root, "can_pause");
    ns->can_next = jbool(root, "can_next");
    ns->can_prev = jbool(root, "can_prev");
    ns->can_seek = jbool(root, "can_seek");
    ns->spotify = jbool(root, "spotify");
    ns->shuffle = ns->spotify && jbool(root, "shuffle");
    ns->repeat = ns->spotify ? (jnum8(root, "repeat") > 2 ? 0 : jnum8(root, "repeat")) : 0;
    ns->can_shuffle = ns->spotify && jbool(root, "can_shuffle");
    ns->can_repeat = ns->spotify && jbool(root, "can_repeat");
    ns->any_playing = cJSON_GetObjectItemCaseSensitive(root, "any_playing") ? jbool(root, "any_playing") : ns->playing;
    ns->idle_v = jnum(root, "idle_v");
    ns->idle_selected = jbool(root, "idle_selected");
    ns->clock_ms = jint64(root, "clock_ms");
}

/* Makes `ns` the current state: status line and UI callback. (The artwork download follows separately: it takes a moment, and
 * the text should not wait for it.) */
static void apply_state(const pc_media_state_t *ns)
{
    if (ns->has_session && strcmp(ns->track_id, s.st.track_id) != 0) {
        ESP_LOGI(TAG, "now %s [%s]: \"%s\" - %s (%u ms) source %u/%u", ns->playing ? "playing" : ns->state, ns->app, ns->title,
                 ns->artist, (unsigned)ns->duration_ms, (unsigned)ns->source_index, (unsigned)ns->sources);
    }
    s.failures = 0;
    s.st = *ns;
    emit_status(ns->has_session ? PC_MEDIA_CONNECTED : PC_MEDIA_NO_SESSION,
                ns->has_session ? (ns->source[0] ? ns->source : ns->app) : "");
    if (s.cbs.on_state) {
        s.cbs.on_state(&s.st, s.cbs.ctx);
    }
}

static bool poll_once(void)
{
    esp_err_t err = req("/now-playing", false, HTTP_TIMEOUT_MS, &s.resp);
    cJSON *root = NULL;
    if (err == ESP_OK && s.resp.status == 200 && s.resp.body.data) {
        root = cJSON_ParseWithLength(s.resp.body.data, s.resp.body.len);
    }
    if (!root) {
        if (is_lost()) {
            return false;                               /* the PC refused our key: the pairing logic shows it, not "unreachable" */
        }
        s.failures++;
        if (s.failures >= FAILS_BEFORE_LOST) {
            const bool refused = last_auth() == PC_AUTH_BAD_PROOF || (err == ESP_OK && s.resp.status);
            emit_status(PC_MEDIA_UNREACHABLE, refused ? "PC refused the request" : "PC not answering");
        }
        return false;
    }

    pc_media_state_t ns;
    parse_state(root, &ns);
    char dbg[24];
    jstr(root, "debug", dbg, sizeof dbg);
    cJSON_Delete(root);

    apply_state(&ns);
    update_art();
    idle_sync(ns.idle_v);
    if (dbg[0] && s.cbs.on_debug) {
        s.cbs.on_debug(dbg, s.cbs.ctx);
    }
    return true;
}

/* ---- commands ------------------------------------------------------------------------------------- */

/* Percent-encodes everything except unreserved characters (app ids can contain '!' and '_'). */
static void url_encode(char *dst, size_t n, const char *src)
{
    static const char HEX[] = "0123456789ABCDEF";
    size_t o = 0;
    for (; *src && o + 4 < n; src++) {
        unsigned char c = (unsigned char)*src;
        if ((c >= '0' && c <= '9') || (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || c == '.' || c == '-' || c == '_') {
            dst[o++] = (char)c;
        } else {
            dst[o++] = '%';
            dst[o++] = HEX[c >> 4];
            dst[o++] = HEX[c & 15];
        }
    }
    dst[o] = '\0';
}

/* The answer to a source switch: {"ok":true,"changed":true,"source":"Chrome","index":1,"count":2,"now":{...}}. Applies the new
 * session's state at once (no waiting for the next poll) and tells the UI which source it is now showing. */
static void apply_source_answer(const char *path)
{
    cJSON *j = (s.resp.status == 200 && s.resp.body.data) ? cJSON_ParseWithLength(s.resp.body.data, s.resp.body.len) : NULL;
    const cJSON *now = j ? cJSON_GetObjectItemCaseSensitive(j, "now") : NULL;
    if (!j || !jbool(j, "ok") || !jbool(j, "changed") || !cJSON_IsObject(now)) {
        ESP_LOGI(TAG, "command %s: nothing to switch", path);
        cJSON_Delete(j);
        return;
    }
    pc_media_state_t ns;
    parse_state(now, &ns);
    char name[sizeof ns.source];
    jstr(j, "source", name, sizeof name);
    int index = (int)jnum(j, "index"), count = (int)jnum(j, "count");
    ESP_LOGI(TAG, "command %s -> source \"%s\" (%d of %d)", path, name, index, count);
    cJSON_Delete(j);
    apply_state(&ns);                                   /* new text, controls and progress now ... */
    if (s.cbs.on_source) {
        s.cbs.on_source(name, index, count, s.cbs.ctx);  /* ... the source pill with them ... */
    }
    update_art();                                       /* ... and the cover as soon as it has been downloaded */
}

/* The answer to a shuffle / repeat press: {"ok":true,"shuffle":true,"repeat":1,...,"now":{...}}. The state Windows reports afterwards
 * replaces the optimistic toggle at once (no waiting for the next poll); a refusal just lets the next poll put the truth back. */
static void apply_transport_answer(const char *path)
{
    cJSON *j = (s.resp.status == 200 && s.resp.body.data) ? cJSON_ParseWithLength(s.resp.body.data, s.resp.body.len) : NULL;
    const cJSON *now = j ? cJSON_GetObjectItemCaseSensitive(j, "now") : NULL;
    if (!j || !cJSON_IsObject(now)) {
        ESP_LOGW(TAG, "command %s: no usable answer (HTTP %d)", path, s.resp.status);
        cJSON_Delete(j);
        return;
    }
    const bool ok = jbool(j, "ok");
    pc_media_state_t ns;
    parse_state(now, &ns);
    ESP_LOGI(TAG, "command %s -> %s%s", path, ok ? "ok" : "refused", ns.idle_selected ? " (idle screen selected)" : "");
    cJSON_Delete(j);
    apply_state(&ns);
}

static void do_command(const cmd_msg_t *m)
{
    char path[224];
    if (s.xport == X_NONE) {
        return;
    }
    /* Every command names the app the display is showing, so it goes to exactly that session even if the PC's own idea of the
     * "current" session changed since the last poll (the PC refuses it when that session no longer exists). */
    char app[sizeof s.st.app * 3 + 1];
    url_encode(app, sizeof app, s.st.has_session ? s.st.app : "");
    switch (m->cmd) {
    case PC_MEDIA_CMD_PLAY_PAUSE:
        snprintf(path, sizeof path, "/control/playpause?app=%s", app);
        if (s.st.has_session) {                         /* optimistic: flip the icon now, the next poll confirms */
            s.st.playing = !s.st.playing;
            if (s.cbs.on_state) {
                s.cbs.on_state(&s.st, s.cbs.ctx);
            }
        }
        break;
    case PC_MEDIA_CMD_NEXT:
        snprintf(path, sizeof path, "/control/next?app=%s", app);
        break;
    case PC_MEDIA_CMD_PREV:
        snprintf(path, sizeof path, "/control/previous?app=%s", app);
        break;
    case PC_MEDIA_CMD_SEEK:
        snprintf(path, sizeof path, "/control/seek?position_ms=%u&app=%s", (unsigned)m->arg, app);
        break;
    case PC_MEDIA_CMD_VOLUME_UP:
    case PC_MEDIA_CMD_VOLUME_DOWN:
        snprintf(path, sizeof path, "/control/volume-%s?step=%u&app=%s", m->cmd == PC_MEDIA_CMD_VOLUME_UP ? "up" : "down",
                 (unsigned)(m->arg ? m->arg : 5), app);
        break;
    case PC_MEDIA_CMD_SHUFFLE:
    case PC_MEDIA_CMD_REPEAT:
        if (!s.st.has_session || !s.st.spotify) {
            return;                                     /* the buttons are not even shown for other apps */
        }
        snprintf(path, sizeof path, "/control/%s?app=%s", m->cmd == PC_MEDIA_CMD_SHUFFLE ? "shuffle" : "repeat", app);
        if (m->cmd == PC_MEDIA_CMD_SHUFFLE) {           /* optimistic: show the new state now, the PC's answer confirms it */
            s.st.shuffle = !s.st.shuffle;
        } else {
            s.st.repeat = (uint8_t)((s.st.repeat + 1) % 3);
        }
        if (s.cbs.on_state) {
            s.cbs.on_state(&s.st, s.cbs.ctx);
        }
        break;
    case PC_MEDIA_CMD_SOURCE_NEXT:
    case PC_MEDIA_CMD_SOURCE_PREV:
        /* the display names what it shows: the idle screen has the identity "idle" (it is not a Windows session) */
        snprintf(path, sizeof path, "/control/source-%s?from=%s", m->cmd == PC_MEDIA_CMD_SOURCE_NEXT ? "next" : "prev",
                 m->arg == 1 ? "idle" : app);
        break;
    case PC_MEDIA_CMD_SOURCE_AUTO:
        snprintf(path, sizeof path, "/control/source-auto");
        break;
    default:
        return;
    }
    const bool is_volume = (m->cmd == PC_MEDIA_CMD_VOLUME_UP || m->cmd == PC_MEDIA_CMD_VOLUME_DOWN);
    const bool is_source = (m->cmd == PC_MEDIA_CMD_SOURCE_NEXT || m->cmd == PC_MEDIA_CMD_SOURCE_PREV);
    const bool is_transport = (m->cmd == PC_MEDIA_CMD_SHUFFLE || m->cmd == PC_MEDIA_CMD_REPEAT || m->cmd == PC_MEDIA_CMD_SOURCE_AUTO);   /* answer carries a fresh "now" */
    esp_err_t err = req(path, true, HTTP_TIMEOUT_MS, &s.resp);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "command %s failed: %s", path, esp_err_to_name(err));
        if (is_volume && s.cbs.on_volume) {
            s.cbs.on_volume(-2, false, s.cbs.ctx);             /* the PC did not answer */
        }
    } else if (is_source) {
        apply_source_answer(path);
    } else if (is_transport) {
        apply_transport_answer(path);
    } else if (is_volume) {
        /* {"ok":true,"volume":65,"muted":false,...} or {"ok":false,"error":"no audio session found for ..."} */
        cJSON *j = (s.resp.status == 200 && s.resp.body.data) ? cJSON_ParseWithLength(s.resp.body.data, s.resp.body.len) : NULL;
        const cJSON *v = j ? cJSON_GetObjectItemCaseSensitive(j, "volume") : NULL;
        int pct = (j && jbool(j, "ok") && cJSON_IsNumber(v)) ? (int)v->valuedouble : -1;
        ESP_LOGI(TAG, "command %s -> volume %d%%%s", path, pct, pct < 0 ? " (unavailable)" : "");
        if (s.cbs.on_volume) {
            s.cbs.on_volume(pct, j ? jbool(j, "muted") : false, s.cbs.ctx);
        }
        cJSON_Delete(j);
    } else if (s.resp.status != 200 || (s.resp.body.data && strstr(s.resp.body.data, "\"ok\":false"))) {
        ESP_LOGW(TAG, "command %s refused: HTTP %d %s", path, s.resp.status, s.resp.body.data ? s.resp.body.data : "");
    } else {
        ESP_LOGI(TAG, "command %s ok", path);
    }
}

static void drain_commands(void)
{
    cmd_msg_t m;
    while (xQueueReceive(s.q, &m, 0) == pdTRUE) {
    }
}

static int64_t now_ms(void)
{
    return esp_timer_get_time() / 1000;
}

/* ---- pairing ---------------------------------------------------------------------------------------- */

#define PAIR_HTTP_MS        3000

/* Is the transport in use still there? (A pairing attempt ends when it vanishes.) */
static bool link_ok(void)
{
    return s.xport == X_USB ? usb_link_state() == USB_LINK_COMPANION : (s.xport == X_LAN && wifi_mgr_is_connected());
}

typedef struct {
    bool paired, pairing;
    uint8_t id[8];
    char name[NAME_BYTES];
} hello_t;

#ifdef APP_DEV_TOOLS
/* The companion's one-shot /debug request normally rides on /now-playing, which is not polled while the display is unpaired: the
 * development firmware also reads it from /hello and /pair/status, so the pairing screen can be driven from the PC. */
static void check_debug(const cJSON *j)
{
    char dbg[24];
    jstr(j, "debug", dbg, sizeof dbg);
    if (dbg[0] && s.cbs.on_debug) {
        s.cbs.on_debug(dbg, s.cbs.ctx);
    }
}
#else
#define check_debug(j) ((void)(j))
#endif

/* GET /hello (unauthenticated): who answers, and is its pairing window open. */
static bool fetch_hello(hello_t *h)
{
    esp_err_t err = xfer("/hello", false, NULL, HELLO_TIMEOUT_MS, &s.resp);
    note_limited();
    cJSON *j = (err == ESP_OK && s.resp.status == 200 && s.resp.body.data && !s.resp.body.truncated)
                   ? cJSON_ParseWithLength(s.resp.body.data, s.resp.body.len) : NULL;
    if (!j) {
        return false;
    }
    memset(h, 0, sizeof *h);
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(j, "v");
    const bool ok = jbool(j, "ok") && cJSON_IsNumber(v) && v->valuedouble == 2.0 && jhex(j, "id", h->id, sizeof h->id);
    if (ok) {
        h->paired = jbool(j, "paired");
        h->pairing = jbool(j, "pairing");
        jstr(j, "name", h->name, sizeof h->name);
        clean_name(h->name);
        check_debug(j);
    }
    cJSON_Delete(j);
    return ok;
}

/* One pairing call: the parsed answer when it is HTTP 200 with "ok":true (caller frees), else NULL. */
static cJSON *pair_call(const char *label, const char *path, const char *json_body)
{
    esp_err_t err = xfer(path, json_body != NULL, json_body, PAIR_HTTP_MS, &s.resp);
    note_limited();
    cJSON *j = (err == ESP_OK && s.resp.body.data && !s.resp.body.truncated) ? cJSON_ParseWithLength(s.resp.body.data, s.resp.body.len) : NULL;
    if (err == ESP_OK && s.resp.status == 200 && j && jbool(j, "ok")) {
        return j;
    }
    char why[24] = "";
    if (j) {
        jstr(j, "error", why, sizeof why);
        clean_name(why);
    }
    ESP_LOGW(TAG, "pairing: %s failed (%s, HTTP %d%s%s)", label, esp_err_to_name(err), s.resp.status, why[0] ? ", " : "", why);
    cJSON_Delete(j);
    return NULL;
}

typedef enum { PS_UNKNOWN, PS_PENDING, PS_PAIRED, PS_ENDED } pair_state_t;

/* GET /pair/status?dev=...: UNKNOWN = no usable answer, ENDED = cancelled / expired (or "paired" without a usable proof). */
static pair_state_t pair_status(const char *dev_hex, uint8_t proof[32])
{
    char path[48];
    snprintf(path, sizeof path, "/pair/status?dev=%s", dev_hex);
    cJSON *j = pair_call("status", path, NULL);
    if (!j) {
        return PS_UNKNOWN;
    }
    char state[16];
    jstr(j, "state", state, sizeof state);
    pair_state_t r = PS_PENDING;
    if (strcmp(state, "paired") == 0) {
        r = jhex(j, "proof", proof, 32) ? PS_PAIRED : PS_ENDED;
    } else if (strcmp(state, "cancelled") == 0 || strcmp(state, "expired") == 0) {
        r = PS_ENDED;
    }
    check_debug(j);
    cJSON_Delete(j);
    return r;
}

/* One pairing attempt (protocol section 3). true = the key was verified and stored. Every other outcome (the user's Cancel, a
 * timeout, any error) hides the prompt, tells the PC (best effort), stores nothing and starts the 10 s pause before a new offer. */
static bool pair_attempt(const hello_t *hello)
{
    pc_pair_t *p = &s.pair;
    char dev[17], body[256], name[NAME_BYTES], pc_name[NAME_BYTES];
    uint8_t pub_c[32], nc[16], cid[8] = { 0 }, proof[32];
    bool offered = false, shown = false, ok = false, fields;
    int64_t deadline, next_status;
    unsigned miss;
    int answer;
    int n;
    cJSON *j;

    strlcpy(name, hello->name, sizeof name);
    if (!pc_pair_begin(p)) {
        ESP_LOGW(TAG, "pairing: no key pair (crypto error)");
        goto done;
    }
    pc_pair_dev_hex(p, dev);

    /* 1. offer: our public key and the commitment to our nonce */
    n = pc_pair_offer_json(p, body, sizeof body);
    j = n > 0 ? pair_call("offer", "/pair/offer", body) : NULL;
    if (!j) {
        goto done;
    }
    offered = true;
    fields = jhex(j, "pub", pub_c, sizeof pub_c) && jhex(j, "nc", nc, sizeof nc) && jhex(j, "id", cid, sizeof cid);
    jstr(j, "name", pc_name, sizeof pc_name);
    clean_name(pc_name);
    cJSON_Delete(j);
    if (!fields || memcmp(cid, hello->id, sizeof cid) != 0) {
        ESP_LOGW(TAG, "pairing: unusable answer to the offer");
        goto done;
    }
    if (pc_name[0]) {
        strlcpy(name, pc_name, sizeof name);
    }
    pc_pair_set_peer(p, pub_c, nc, cid);

    /* 2. reveal our nonce (the PC checks it against the commitment), then both sides derive the same code */
    n = pc_pair_reveal_json(p, body, sizeof body);
    j = n > 0 ? pair_call("reveal", "/pair/reveal", body) : NULL;
    if (!j) {
        goto done;
    }
    cJSON_Delete(j);
    if (!pc_pair_derive(p)) {
        ESP_LOGW(TAG, "pairing: key agreement failed");
        goto done;
    }

    /* 3. show the code; the user compares it with the PC's and answers (pc_media_pair_answer) */
    atomic_store(&s.pair_answer, 0);
    atomic_store(&s.prompt_active, true);
    shown = true;
    s.cbs.on_pair_prompt(name, p->code, s.cbs.ctx);
    emit_status(PC_MEDIA_PAIRING, name);
    ESP_LOGI(TAG, "pairing code shown");
    deadline = now_ms() + PAIR_USER_MS;
    next_status = now_ms() + PAIR_STATUS_MS;
    miss = 0;
    while ((answer = atomic_load(&s.pair_answer)) == 0) {
        const int64_t t = now_ms();
        if (t >= deadline || !link_ok()) {
            ESP_LOGI(TAG, "pairing: no answer in time");
            goto done;
        }
        if (t >= next_status) {                         /* is the attempt still alive on the PC (not cancelled / expired there)? */
            next_status = t + PAIR_STATUS_MS;
            const pair_state_t st = pair_status(dev, proof);
            if (st == PS_ENDED || (st == PS_UNKNOWN && ++miss >= 3)) {
                ESP_LOGI(TAG, "pairing: the attempt ended on the PC");
                goto done;
            }
            if (st != PS_UNKNOWN) {
                miss = 0;
            }
            continue;
        }
        drain_commands();
        xSemaphoreTake(s.answer_sem, pdMS_TO_TICKS(500));         /* (or the user answers sooner) */
    }
    if (answer != 1) {
        ESP_LOGI(TAG, "pairing: cancelled on the display");
        goto done;
    }

    /* 4. confirm with our proof, then wait for the click on the PC and its proof */
    n = pc_pair_confirm_json(p, body, sizeof body);
    j = n > 0 ? pair_call("confirm", "/pair/confirm", body) : NULL;
    if (!j) {
        goto done;
    }
    cJSON_Delete(j);
    deadline = now_ms() + PAIR_PC_MS;
    miss = 0;
    for (;;) {
        const pair_state_t st = pair_status(dev, proof);
        if (st == PS_PAIRED) {
            break;
        }
        if (st == PS_ENDED || (st == PS_UNKNOWN && ++miss >= 5) || now_ms() >= deadline || !link_ok() ||
            atomic_load(&s.pair_answer) == 2) {         /* (the user may still tap Cancel while the PC has not been clicked) */
            ESP_LOGI(TAG, "pairing: not completed");
            goto done;
        }
        if (st != PS_UNKNOWN) {
            miss = 0;
        }
        drain_commands();
        xSemaphoreTake(s.answer_sem, pdMS_TO_TICKS(1000));
    }
    if (!pc_pair_verify_pc_proof(p, proof)) {
        ESP_LOGW(TAG, "pairing: the PC's proof does not match, nothing stored");
        goto done;
    }
    ok = pc_pair_commit(p);                              /* the key is stored only now, complete and verified */

done:
    if (shown) {
        atomic_store(&s.prompt_active, false);
        s.cbs.on_pair_prompt(name, NULL, s.cbs.ctx);    /* hide the prompt before the (possibly slow) cancel below */
    }
    if (!ok && offered) {
        n = pc_pair_cancel_json(p, body, sizeof body);
        j = n > 0 ? pair_call("cancel", "/pair/cancel", body) : NULL;
        cJSON_Delete(j);
    }
    pc_pair_wipe(p);
    if (ok) {
        xSemaphoreTake(s.lock, portMAX_DELAY);
        memcpy(s.cid, cid, sizeof s.cid);
        s.cid_valid = true;
        s.lost = false;
        xSemaphoreGive(s.lock);
        s.auth_fails = 0;
        s.failures = 0;
        s.last_valid_ms = now_ms();
        s.pair_retry_at = 0;
        ESP_LOGI(TAG, "paired with the companion");
    } else {
        s.pair_retry_at = now_ms() + PAIR_RETRY_MS;
    }
    return ok;
}

/* Unpaired or lost: ask GET /hello every 2 s (never /now-playing), show what is wrong, and start the handshake when the companion's
 * pairing window is open. While lost, a signed request is tried now and then so a PC that was merely asleep is picked up again. */
static void unpaired_cycle(int64_t *next_poll)
{
    drain_commands();
    int64_t now = now_ms();
    if (now < *next_poll) {
        const int64_t wait = *next_poll - now;
        vTaskDelay(pdMS_TO_TICKS(wait < 250 ? wait : 250));
        return;
    }
    *next_poll = now + HELLO_MS;
    hello_t h;
    if (!fetch_hello(&h)) {
        s.failures++;
        if (s.failures >= FAILS_BEFORE_LOST) {
            emit_status(PC_MEDIA_UNREACHABLE, "PC not answering");
        }
        return;
    }
    s.failures = 0;
    xSemaphoreTake(s.lock, portMAX_DELAY);
    note_host(h.id, h.name, h.pairing);
    xSemaphoreGive(s.lock);

    const bool has_key = pc_auth_has_key();
    if (has_key && h.paired && now >= s.probe_at) {      /* lost, but it knows a paired display: is it still us? */
        req("/sources", false, 2500, &s.resp);
        if (last_auth() == PC_AUTH_OK) {
            set_lost(false);
            s.auth_fails = 0;
            ESP_LOGI(TAG, "pairing restored: the companion accepts the stored key again");
            return;
        }
        s.probe_at = now_ms() + PROBE_MS;                /* (a few probes a minute stay far below the PC's failure limiter) */
    }
    emit_status(has_key ? PC_MEDIA_PAIRING_LOST : PC_MEDIA_NEEDS_PAIRING, h.name);
    if (h.pairing && s.cbs.on_pair_prompt && now_ms() >= s.pair_retry_at) {
        *next_poll = pair_attempt(&h) ? 0 : now_ms() + HELLO_MS;
    }
}

/* ---- the poll task --------------------------------------------------------------------------------- */

/* Which transport to use now, and what to tell the user when there is none. Runs at the top of every poll cycle. USB wins whenever a
 * companion answers on the cable; otherwise the LAN, when Wi-Fi is configured and a companion was found there. */
static void select_transport(void)
{
    const int64_t now = now_ms();
    const usb_link_state_t us = usb_link_state();
    if (us == USB_LINK_NO_HOST) {
        s.usb_since_ms = 0;
    } else {
        s.usb_seen_ms = now;
        if (s.usb_since_ms == 0) {
            s.usb_since_ms = now;
        }
        if (us == USB_LINK_COMPANION) {
            s.companion_seen_ms = now;
        }
    }
    xport_t want = X_NONE;
    if (us == USB_LINK_COMPANION) {
        usb_link_peer_t peer;
        if (usb_link_peer(&peer) && !peer.compat) {
            emit_status(PC_MEDIA_INCOMPATIBLE, peer.api < USB_LINK_MIN_API ? "companion" : "firmware");
        } else {
            want = X_USB;
        }
    } else if (s.lan_enabled && wifi_mgr_is_connected() && s.host_valid) {
        want = X_LAN;
    }
    if (want != s.xport) {
        ESP_LOGI(TAG, "transport: %s", want == X_USB ? "USB" : want == X_LAN ? "LAN" : "none");
        s.xport = want;
        s.failures = 0;
        s.auth_fails = 0;
        s.session_ok = false;
        s.last_valid_ms = now;                           /* the "no valid answer for 120 s" clock only runs while a companion is being asked */
        pc_http_drop();
    }
    if (want != X_NONE || (us == USB_LINK_COMPANION)) {
        return;
    }
    /* nothing to talk to: say why */
    const int64_t quiet_since = s.usb_seen_ms > s.boot_ms ? s.usb_seen_ms : s.boot_ms;
    if (us == USB_LINK_NO_COMPANION) {
        const bool restarting = s.companion_seen_ms != 0 && now - s.companion_seen_ms < COMPANION_GONE_MS;
        if (now - s.usb_since_ms < USB_ANSWER_MS || restarting) {
            emit_status(PC_MEDIA_USB_CONNECTED, "");         /* the first seconds, or the companion is restarting: not the install screen */
        } else if (!s.lan_enabled || !wifi_mgr_is_connected()) {
            emit_status(PC_MEDIA_COMPANION_MISSING, "");
        } else {
            emit_status(PC_MEDIA_SEARCHING, "");
        }
    } else if (s.lan_enabled) {
        if (!wifi_mgr_is_connected()) {
            emit_status(now - quiet_since < USB_GRACE_MS ? PC_MEDIA_WAITING_NETWORK : PC_MEDIA_NO_USB_DATA, "");
        } else {
            emit_status(PC_MEDIA_SEARCHING, "");
        }
    } else if (now - quiet_since >= USB_GRACE_MS) {
        emit_status(PC_MEDIA_NO_USB_DATA, "");              /* a cable that was just bumped does not flash this: it needs 6 s of silence */
    }
}

#ifdef APP_DEV_TOOLS
static void usb_debug(const char *action, void *ctx)      /* a DEBUG frame from the host (development action): same hook the /debug route uses */
{
    (void)ctx;
    if (s.cbs.on_debug) {
        s.cbs.on_debug(action, s.cbs.ctx);
    }
}
#endif

static void poll_task(void *arg)
{
    (void)arg;
#ifdef APP_DEV_TOOLS
    pc_auth_selftest();                                 /* logs "dmauth selftest ok" (or the failing vector) once at start-up */
#endif
    int64_t next_poll = 0;                              /* when the next /now-playing poll is due */
    for (;;) {
        select_transport();
        if (s.xport == X_NONE) {
            drain_commands();
            vTaskDelay(pdMS_TO_TICKS(250));
            continue;
        }
        if (now_ms() < s.blocked_until) {               /* limited by the companion (429): wait it out quietly */
            drain_commands();
            vTaskDelay(pdMS_TO_TICKS(250));
            continue;
        }
        if (!pc_auth_has_key() || is_lost()) {
            unpaired_cycle(&next_poll);
            continue;
        }
        int64_t now = now_ms();
        if (now - s.last_valid_ms > LOST_AFTER_MS) {
            enter_lost("no valid answer for 120 s");
            continue;
        }
        if (now >= next_poll) {                         /* a poll is due: it goes first, even if commands are waiting */
            bool ok = poll_once();
            next_poll = now_ms() + (ok ? POLL_MS : (s.failures >= FAILS_BEFORE_LOST ? POLL_FAILING_MS : POLL_MS));
            continue;
        }
        cmd_msg_t m;
        if (xQueueReceive(s.q, &m, pdMS_TO_TICKS(next_poll - now)) == pdTRUE) {
            do_command(&m);
            int64_t soon = now_ms() + POLL_AFTER_CMD_MS;    /* look at the effect shortly, but never starve the poll */
            if (soon < next_poll) {
                next_poll = soon;
            }
        }
    }
}

/* ---- public ----------------------------------------------------------------------------------------- */

esp_err_t pc_media_start(const pc_media_config_t *cfg)
{
    if (s.started || !cfg) {
        return ESP_ERR_INVALID_STATE;
    }
    s.cbs = cfg->cbs;
    s.lock = xSemaphoreCreateMutex();
    s.q = xQueueCreate(8, sizeof(cmd_msg_t));
    s.answer_sem = xSemaphoreCreateBinary();
    if (!s.lock || !s.q || !s.answer_sem) {
        return ESP_ERR_NO_MEM;
    }
    pc_auth_init();                                     /* the stored pairing key (if any) is read once, here; the display id exists after this */
    s.lan_enabled = cfg->lan_enabled;
    s.boot_ms = now_ms();
    {
        usb_link_config_t uc = { .fw_version = esp_app_get_description()->version, .is_paired = pc_media_is_paired };
        pc_auth_dev_id(uc.dev_id);
        esp_err_t ue = usb_link_start(&uc);
        if (ue != ESP_OK) {
            ESP_LOGE(TAG, "USB link could not start: %s", esp_err_to_name(ue));
        }
        usb_link_log_install();                         /* the firmware log goes to the companion as well (it keeps a log file for support) */
#ifdef APP_DEV_TOOLS
        usb_link_set_debug_handler(usb_debug, NULL);
#endif
    }
    uint8_t cid[8];
    if (pc_auth_cid(cid)) {
        memcpy(s.cid, cid, sizeof s.cid);
        s.cid_valid = true;
    }
    s.last_valid_ms = now_ms();
    pc_resp_init(&s.resp, JSON_MAX_BYTES);
    pc_resp_init(&s.art, ART_MAX_BYTES);
    s.port = cfg->port ? cfg->port : PC_MEDIA_DEFAULT_PORT;
    s.art_max_h = cfg->art_max_h ? cfg->art_max_h : 300;
    s.art_max_w = cfg->art_max_w ? cfg->art_max_w : s.art_max_h;
    pc_art_set_cover_area_fn(cfg->cover_area);
    if (cfg->lan_enabled && cfg->static_host && cfg->static_host[0]) {
        strlcpy(s.host, cfg->static_host, sizeof s.host);
        s.host_valid = true;
        s.host_static = true;
        ESP_LOGI(TAG, "using fixed companion address %s:%u", s.host, (unsigned)s.port);
    }
    s.started = true;
    if (cfg->lan_enabled && !s.host_static) {
        xTaskCreatePinnedToCore(discovery_task, "pc_discovery", 4096, NULL, 3, NULL, 0);
    }
    if (xTaskCreatePinnedToCore(poll_task, "pc_poll", 12288, NULL, 3, NULL, 0) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

void pc_media_command(pc_media_cmd_t cmd, uint32_t arg)
{
    if (!s.q) {
        return;
    }
    if ((cmd == PC_MEDIA_CMD_VOLUME_UP || cmd == PC_MEDIA_CMD_VOLUME_DOWN) && uxQueueMessagesWaiting(s.q) >= 2) {
        return;                                         /* a held key must not build up a backlog: skip, the next repeat follows */
    }
    if ((cmd == PC_MEDIA_CMD_SOURCE_NEXT || cmd == PC_MEDIA_CMD_SOURCE_PREV) && uxQueueMessagesWaiting(s.q) >= 3) {
        return;                                         /* swipes in a flurry: each one is relative to the source on screen */
    }
    cmd_msg_t m = { cmd, arg };
    xQueueSend(s.q, &m, 0);
}

void pc_media_pair_answer(bool accept)
{
    if (!atomic_load(&s.prompt_active)) {
        return;                                         /* no prompt: a stale tap */
    }
    atomic_store(&s.pair_answer, accept ? 1 : 2);
    xSemaphoreGive(s.answer_sem);                       /* wakes the wait in pair_attempt() at once */
}

bool pc_media_is_paired(void)
{
    return pc_auth_has_key();
}

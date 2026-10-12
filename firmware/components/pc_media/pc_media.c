/*
 * pc_media: discovery of the Windows companion, polling of /now-playing, artwork download, control commands.
 * Never touches LVGL - results go out through the callbacks in pc_media_config_t.
 */
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

#include "pc_internal.h"
#include "pc_media.h"
#include "wifi_mgr.h"

static const char *TAG = "pc_media";

#define POLL_MS             1000
#define POLL_AFTER_CMD_MS   200
#define POLL_FAILING_MS     2000
#define HTTP_TIMEOUT_MS     3000
#define FAILS_BEFORE_LOST   3
#define ART_MAX_BYTES       (160 * 1024)
#define ART_RETRIES         3
#define BEACON_MAGIC        "DESKMEDIA1 "

typedef struct {
    pc_media_cmd_t cmd;
    uint32_t arg;
} cmd_msg_t;

static struct {
    pc_media_callbacks_t cbs;
    QueueHandle_t q;
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
    pc_resp_t resp;                     /* JSON / small replies */
    pc_resp_t art;                      /* JPEG download */
    bool started;
} s;

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

static uint32_t jnum(const cJSON *obj, const char *key)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(obj, key);
    return (cJSON_IsNumber(v) && v->valuedouble > 0) ? (uint32_t)v->valuedouble : 0;
}

static bool jbool(const cJSON *obj, const char *key)
{
    return cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(obj, key));
}

static bool get_host(char *host, size_t n, uint16_t *port)
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

/* ---- discovery ------------------------------------------------------------------------------------ */

static void handle_beacon(const char *json, const char *ip)
{
    cJSON *j = cJSON_Parse(json);
    uint16_t port = PC_MEDIA_DEFAULT_PORT;
    if (j) {
        uint32_t p = jnum(j, "port");
        if (p > 0 && p < 65536) {
            port = (uint16_t)p;
        }
    }
    xSemaphoreTake(s.lock, portMAX_DELAY);
    bool same = s.host_valid && strcmp(s.host, ip) == 0 && s.port == port;
    /* The first companion wins; another one is only taken over when the current one has stopped answering. */
    bool accept = !s.host_valid || (!same && s.failures > 0);
    if (accept) {
        strlcpy(s.host, ip, sizeof s.host);
        s.port = port;
        s.host_valid = true;
        s.failures = 0;
    }
    xSemaphoreGive(s.lock);
    if (accept) {
        char name[48] = "";
        if (j) {
            jstr(j, "name", name, sizeof name);
        }
        ESP_LOGI(TAG, "found companion at %s:%u (%s)", ip, (unsigned)port, name);
    }
    cJSON_Delete(j);
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
            char buf[200];
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
    char host[16];
    uint16_t port;
    if (!get_host(host, sizeof host, &port)) {
        return;
    }
    int64_t t0 = esp_timer_get_time();
    const lv_image_dsc_t *dsc = NULL, *bg = NULL;
    char path[56];
    snprintf(path, sizeof path, "/artwork?size=%u&maxw=%u", (unsigned)s.art_max_h, (unsigned)s.art_max_w);
    esp_err_t err = pc_http_request(host, port, path, false, 6000, &s.art);
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

/* ---- polling -------------------------------------------------------------------------------------- */

static bool poll_once(void)
{
    char host[16];
    uint16_t port;
    if (!get_host(host, sizeof host, &port)) {
        return false;
    }
    esp_err_t err = pc_http_request(host, port, "/now-playing", false, HTTP_TIMEOUT_MS, &s.resp);
    cJSON *root = NULL;
    if (err == ESP_OK && s.resp.status == 200 && s.resp.body.data) {
        root = cJSON_ParseWithLength(s.resp.body.data, s.resp.body.len);
    }
    if (!root) {
        s.failures++;
        if (s.failures >= FAILS_BEFORE_LOST) {
            emit_status(PC_MEDIA_UNREACHABLE, err == ESP_OK && s.resp.status ? "PC refused the request" : "PC not answering");
        }
        return false;
    }

    pc_media_state_t ns;
    memset(&ns, 0, sizeof ns);
    ns.has_session = jbool(root, "has_session");
    ns.playing = jbool(root, "playing");
    jstr(root, "state", ns.state, sizeof ns.state);
    jstr(root, "app", ns.app, sizeof ns.app);
    jstr(root, "title", ns.title, sizeof ns.title);
    jstr(root, "artist", ns.artist, sizeof ns.artist);
    jstr(root, "album", ns.album, sizeof ns.album);
    jstr(root, "track_id", ns.track_id, sizeof ns.track_id);
    jstr(root, "art_id", ns.art_id, sizeof ns.art_id);
    ns.position_ms = jnum(root, "position_ms");
    ns.duration_ms = jnum(root, "duration_ms");
    ns.can_play = jbool(root, "can_play");
    ns.can_pause = jbool(root, "can_pause");
    ns.can_next = jbool(root, "can_next");
    ns.can_prev = jbool(root, "can_prev");
    ns.can_seek = jbool(root, "can_seek");
    char dbg[24];
    jstr(root, "debug", dbg, sizeof dbg);
    cJSON_Delete(root);

    if (ns.has_session && strcmp(ns.track_id, s.st.track_id) != 0) {
        ESP_LOGI(TAG, "now %s [%s]: \"%s\" - %s (%u ms)", ns.playing ? "playing" : ns.state, ns.app, ns.title, ns.artist,
                 (unsigned)ns.duration_ms);
    }
    s.failures = 0;
    s.st = ns;
    emit_status(ns.has_session ? PC_MEDIA_CONNECTED : PC_MEDIA_NO_SESSION, ns.has_session ? ns.app : "");
    if (s.cbs.on_state) {
        s.cbs.on_state(&s.st, s.cbs.ctx);
    }
    update_art();
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

static void do_command(const cmd_msg_t *m)
{
    char host[16], path[192];
    uint16_t port;
    if (!get_host(host, sizeof host, &port)) {
        return;
    }
    switch (m->cmd) {
    case PC_MEDIA_CMD_PLAY_PAUSE:
        strlcpy(path, "/control/playpause", sizeof path);
        if (s.st.has_session) {                         /* optimistic: flip the icon now, the next poll confirms */
            s.st.playing = !s.st.playing;
            if (s.cbs.on_state) {
                s.cbs.on_state(&s.st, s.cbs.ctx);
            }
        }
        break;
    case PC_MEDIA_CMD_NEXT:
        strlcpy(path, "/control/next", sizeof path);
        break;
    case PC_MEDIA_CMD_PREV:
        strlcpy(path, "/control/previous", sizeof path);
        break;
    case PC_MEDIA_CMD_SEEK:
        snprintf(path, sizeof path, "/control/seek?position_ms=%u", (unsigned)m->arg);
        break;
    case PC_MEDIA_CMD_VOLUME_UP:
    case PC_MEDIA_CMD_VOLUME_DOWN: {
        /* Name the app the display is showing, so the volume goes to exactly that app even if the PC's own idea of the
         * "current" session changed since the last poll. */
        char app[sizeof s.st.app * 3 + 1];
        url_encode(app, sizeof app, s.st.has_session ? s.st.app : "");
        snprintf(path, sizeof path, "/control/volume-%s?step=%u&app=%s", m->cmd == PC_MEDIA_CMD_VOLUME_UP ? "up" : "down",
                 (unsigned)(m->arg ? m->arg : 5), app);
        break;
    }
    default:
        return;
    }
    const bool is_volume = (m->cmd == PC_MEDIA_CMD_VOLUME_UP || m->cmd == PC_MEDIA_CMD_VOLUME_DOWN);
    esp_err_t err = pc_http_request(host, port, path, true, HTTP_TIMEOUT_MS, &s.resp);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "command %s failed: %s", path, esp_err_to_name(err));
        if (is_volume && s.cbs.on_volume) {
            s.cbs.on_volume(-2, false, s.cbs.ctx);             /* the PC did not answer */
        }
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

static void poll_task(void *arg)
{
    (void)arg;
    int64_t next_poll = 0;                              /* when the next /now-playing poll is due */
    for (;;) {
        if (!wifi_mgr_is_connected()) {
            emit_status(PC_MEDIA_WAITING_NETWORK, "");
            drain_commands();
            wifi_mgr_wait_connected(1000);
            continue;
        }
        char host[16];
        uint16_t port;
        if (!get_host(host, sizeof host, &port)) {
            emit_status(PC_MEDIA_SEARCHING, "");
            drain_commands();
            vTaskDelay(pdMS_TO_TICKS(300));
            continue;
        }
        int64_t now = now_ms();
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
    if (!s.lock || !s.q) {
        return ESP_ERR_NO_MEM;
    }
    pc_resp_init(&s.resp, 16 * 1024);
    pc_resp_init(&s.art, ART_MAX_BYTES);
    s.port = cfg->port ? cfg->port : PC_MEDIA_DEFAULT_PORT;
    s.art_max_h = cfg->art_max_h ? cfg->art_max_h : 300;
    s.art_max_w = cfg->art_max_w ? cfg->art_max_w : s.art_max_h;
    pc_art_set_cover_area_fn(cfg->cover_area);
    if (cfg->static_host && cfg->static_host[0]) {
        strlcpy(s.host, cfg->static_host, sizeof s.host);
        s.host_valid = true;
        s.host_static = true;
        ESP_LOGI(TAG, "using fixed companion address %s:%u", s.host, (unsigned)s.port);
    }
    s.started = true;
    if (!s.host_static) {
        xTaskCreatePinnedToCore(discovery_task, "pc_discovery", 4096, NULL, 3, NULL, 0);
    }
    if (xTaskCreatePinnedToCore(poll_task, "pc_poll", 8192, NULL, 3, NULL, 0) != pdPASS) {
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
    cmd_msg_t m = { cmd, arg };
    xQueueSend(s.q, &m, 0);
}

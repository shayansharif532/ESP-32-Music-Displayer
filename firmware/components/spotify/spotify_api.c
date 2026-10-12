/*
 * Spotify task: waits for network + clock, makes sure we are authorized, then polls /v1/me/player and executes
 * transport commands. Never touches LVGL - results go out through the callbacks in spotify_config_t.
 */
#include <stdio.h>
#include <string.h>

#include "cJSON.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

#include "spotify_internal.h"
#include "wifi_mgr.h"

static const char *TAG = "spotify";

#define API_BASE            "https://api.spotify.com/v1"
#define POLL_PLAYING_MS     3000
#define POLL_PAUSED_MS      4000
#define POLL_IDLE_MS        6000
#define POLL_AFTER_CMD_MS   450          /* Spotify needs a moment before /me/player reflects a command */
#define POLL_RESP_MAX       (128 * 1024)
#define ART_RETRIES         3

#define TASK_STACK          (12 * 1024)
#define TASK_PRIORITY       3            /* below the LVGL task (4): the UI always wins */

typedef struct {
    spotify_cmd_t cmd;
    uint32_t arg;
} cmd_msg_t;

static struct {
    spotify_callbacks_t cbs;
    QueueHandle_t q;
    spotify_playback_t last;
    char token[768];
    char art_url[200];                   /* URL whose art is on screen ("" = none) */
    int art_failures;
    spotify_status_t status;
    char status_detail[128];
    bool status_valid;
    unsigned net_errors;
    uint32_t next_poll_ms;
    sp_resp_t poll_resp;                 /* reused for every poll: PSRAM, grows once, no churn */
    int64_t linked_at_us;
    bool started;
} s;

/* ---- status / callbacks ------------------------------------------------------------------------ */

static void emit_status(spotify_status_t st, const char *detail)
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

static void emit_playback(void)
{
    if (s.cbs.on_playback) {
        s.cbs.on_playback(&s.last, s.cbs.ctx);
    }
}

/* ---- JSON -------------------------------------------------------------------------------------- */

static void pick_image(const cJSON *images, char *out, size_t out_len)
{
    /* Smallest image that is at least 300 px wide (that is the one we decode); else the largest available. */
    const cJSON *best = NULL;
    int best_w = 0;
    const cJSON *img;
    cJSON_ArrayForEach(img, images) {
        const cJSON *url = cJSON_GetObjectItemCaseSensitive(img, "url");
        const cJSON *w = cJSON_GetObjectItemCaseSensitive(img, "width");
        if (!cJSON_IsString(url)) {
            continue;
        }
        int width = cJSON_IsNumber(w) ? (int)w->valuedouble : 0;
        bool better;
        if (!best) {
            better = true;
        } else if (best_w >= 300) {
            better = (width >= 300 && width < best_w);
        } else {
            better = (width > best_w);
        }
        if (better) {
            best = url;
            best_w = width;
        }
    }
    if (best) {
        strlcpy(out, best->valuestring, out_len);
    }
}

static void join_artists(const cJSON *artists, char *out, size_t out_len)
{
    out[0] = '\0';
    const cJSON *a;
    cJSON_ArrayForEach(a, artists) {
        const cJSON *name = cJSON_GetObjectItemCaseSensitive(a, "name");
        if (!cJSON_IsString(name)) {
            continue;
        }
        if (out[0]) {
            strlcat(out, ", ", out_len);
        }
        strlcat(out, name->valuestring, out_len);
    }
}

static void copy_str(const cJSON *obj, const char *key, char *out, size_t out_len)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(obj, key);
    if (cJSON_IsString(v)) {
        strlcpy(out, v->valuestring, out_len);
    }
}

static bool parse_player(const char *json, size_t len, spotify_playback_t *pb)
{
    cJSON *root = cJSON_ParseWithLength(json, len);
    if (!root) {
        return false;
    }
    memset(pb, 0, sizeof *pb);

    const cJSON *dev = cJSON_GetObjectItemCaseSensitive(root, "device");
    if (cJSON_IsObject(dev)) {
        pb->device_active = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(dev, "is_active"));
        copy_str(dev, "name", pb->device_name, sizeof pb->device_name);
        copy_str(dev, "type", pb->device_type, sizeof pb->device_type);
    }
    pb->is_playing = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(root, "is_playing"));
    const cJSON *prog = cJSON_GetObjectItemCaseSensitive(root, "progress_ms");
    if (cJSON_IsNumber(prog) && prog->valuedouble > 0) {
        pb->progress_ms = (uint32_t)prog->valuedouble;
    }

    const cJSON *type = cJSON_GetObjectItemCaseSensitive(root, "currently_playing_type");
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(root, "item");
    if (cJSON_IsObject(item)) {
        pb->has_item = true;
        copy_str(item, "id", pb->track_id, sizeof pb->track_id);
        copy_str(item, "name", pb->title, sizeof pb->title);
        const cJSON *dur = cJSON_GetObjectItemCaseSensitive(item, "duration_ms");
        if (cJSON_IsNumber(dur) && dur->valuedouble > 0) {
            pb->duration_ms = (uint32_t)dur->valuedouble;
        }
        const cJSON *album = cJSON_GetObjectItemCaseSensitive(item, "album");
        const cJSON *show = cJSON_GetObjectItemCaseSensitive(item, "show");
        if (cJSON_IsObject(album)) {                                  /* a music track */
            join_artists(cJSON_GetObjectItemCaseSensitive(item, "artists"), pb->artist, sizeof pb->artist);
            copy_str(album, "name", pb->album, sizeof pb->album);
            pick_image(cJSON_GetObjectItemCaseSensitive(album, "images"), pb->art_url, sizeof pb->art_url);
        } else if (cJSON_IsObject(show)) {                            /* a podcast episode */
            copy_str(show, "name", pb->artist, sizeof pb->artist);
            copy_str(show, "name", pb->album, sizeof pb->album);
            pick_image(cJSON_GetObjectItemCaseSensitive(item, "images"), pb->art_url, sizeof pb->art_url);
            if (!pb->art_url[0]) {
                pick_image(cJSON_GetObjectItemCaseSensitive(show, "images"), pb->art_url, sizeof pb->art_url);
            }
        }
        if (!pb->track_id[0]) {                                       /* local files have no id */
            snprintf(pb->track_id, sizeof pb->track_id, "local:%08x", (unsigned)esp_timer_get_time());
        }
    } else if (cJSON_IsString(type) && strcmp(type->valuestring, "ad") == 0) {
        pb->has_item = true;
        pb->is_ad = true;
        strlcpy(pb->track_id, "ad", sizeof pb->track_id);
        strlcpy(pb->title, "Advertisement", sizeof pb->title);
    }
    cJSON_Delete(root);
    return true;
}

/* ---- authorized API call ----------------------------------------------------------------------- */

#define API_NET_ERROR   (-1)
#define API_AUTH_LOST   (-2)

/* Returns the HTTP status, or API_NET_ERROR / API_AUTH_LOST. Refreshes the token once on 401. */
static int api_call(esp_http_client_method_t method, const char *path, sp_resp_t *resp)
{
    char url[256];
    snprintf(url, sizeof url, API_BASE "%s", path);
    for (int attempt = 0; attempt < 2; attempt++) {
        sp_auth_result_t ar = sp_auth_access_token(s.token, sizeof s.token, attempt > 0);
        if (ar == SP_AUTH_REVOKED || ar == SP_AUTH_NO_TOKEN) {
            return API_AUTH_LOST;
        }
        if (ar != SP_AUTH_OK) {
            return API_NET_ERROR;
        }
        sp_req_t req = { .url = url, .method = method, .bearer = s.token, .persistent = true };
        esp_err_t err = sp_http_request(&req, resp);
        if (err != ESP_OK) {
            return API_NET_ERROR;
        }
        if (resp->status == 401 && attempt == 0) {
            continue;
        }
        return resp->status;
    }
    return API_AUTH_LOST;
}

static void api_error_message(const sp_resp_t *resp, char *out, size_t out_len)
{
    out[0] = '\0';
    if (!resp->body.data) {
        return;
    }
    cJSON *root = cJSON_ParseWithLength(resp->body.data, resp->body.len);
    const cJSON *err = cJSON_GetObjectItemCaseSensitive(root, "error");
    if (cJSON_IsObject(err)) {
        copy_str(err, "message", out, out_len);
    }
    cJSON_Delete(root);
}

/* ---- album art --------------------------------------------------------------------------------- */

static void update_art(void)
{
    const char *want = s.last.has_item ? s.last.art_url : "";
    if (strcmp(want, s.art_url) == 0) {
        return;
    }
    if (!want[0]) {
        s.art_url[0] = '\0';
        if (s.cbs.on_art) {
            s.cbs.on_art(NULL, s.cbs.ctx);
        }
        return;
    }
    const lv_image_dsc_t *dsc = NULL;
    int64_t t0 = esp_timer_get_time();
    esp_err_t err = sp_art_load(want, &dsc);
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "album art ready (%ux%u) in %d ms", (unsigned)dsc->header.w, (unsigned)dsc->header.h,
                 (int)((esp_timer_get_time() - t0) / 1000));
        strlcpy(s.art_url, want, sizeof s.art_url);
        s.art_failures = 0;
        if (s.cbs.on_art) {
            s.cbs.on_art(dsc, s.cbs.ctx);
        }
    } else if (++s.art_failures >= ART_RETRIES) {
        ESP_LOGW(TAG, "album art failed %d times (%s), giving up on this image", s.art_failures, esp_err_to_name(err));
        strlcpy(s.art_url, want, sizeof s.art_url);              /* stop retrying this URL */
        s.art_failures = 0;
        if (s.cbs.on_art) {
            s.cbs.on_art(NULL, s.cbs.ctx);
        }
    }
}

/* ---- polling ----------------------------------------------------------------------------------- */

static void poll_once(void)
{
    int64_t t0 = esp_timer_get_time();
    int st = api_call(HTTP_METHOD_GET, "/me/player?additional_types=episode", &s.poll_resp);
    int rtt_ms = (int)((esp_timer_get_time() - t0) / 1000);

    if (st == API_NET_ERROR) {
        sp_http_drop_persistent();
        s.net_errors++;
        s.next_poll_ms = s.net_errors < 3 ? 3000 : (s.net_errors < 6 ? 8000 : 20000);
        emit_status(SPOTIFY_STATUS_NET_ERROR, "no connection to Spotify");
        return;
    }
    s.net_errors = 0;

    if (st == API_AUTH_LOST || st == 401) {
        sp_auth_forget();
        emit_status(SPOTIFY_STATUS_AUTH_ERROR, "Spotify authorization expired");
        s.next_poll_ms = 1500;
        return;
    }
    if (st == 429) {
        int wait_s = s.poll_resp.retry_after_s > 0 ? s.poll_resp.retry_after_s : 5;
        emit_status(SPOTIFY_STATUS_RATE_LIMITED, "Spotify rate limit - waiting");
        s.next_poll_ms = (uint32_t)wait_s * 1000 + 500;
        return;
    }
    if (st == 403) {
        char msg[128];
        api_error_message(&s.poll_resp, msg, sizeof msg);
        ESP_LOGW(TAG, "player request forbidden: %s", msg);
        emit_status(SPOTIFY_STATUS_AUTH_ERROR, msg[0] ? msg : "Spotify refused access (is this account added to the app?)");
        s.next_poll_ms = 10000;
        return;
    }
    if (st == 204 || (st == 200 && s.poll_resp.body.len == 0)) {
        memset(&s.last, 0, sizeof s.last);
        emit_status(SPOTIFY_STATUS_NO_DEVICE, "");
        emit_playback();
        update_art();
        s.next_poll_ms = POLL_IDLE_MS;
        return;
    }
    if (st != 200) {
        ESP_LOGW(TAG, "player request: HTTP %d", st);
        emit_status(SPOTIFY_STATUS_NET_ERROR, "Spotify is not answering properly");
        s.next_poll_ms = 8000;
        return;
    }

    spotify_playback_t pb;
    if (!parse_player(s.poll_resp.body.data, s.poll_resp.body.len, &pb)) {
        ESP_LOGW(TAG, "could not parse player JSON (%u bytes%s)", (unsigned)s.poll_resp.body.len,
                 s.poll_resp.body.truncated ? ", truncated" : "");
        emit_status(SPOTIFY_STATUS_NET_ERROR, "unreadable answer from Spotify");
        s.next_poll_ms = 8000;
        return;
    }
    if (pb.is_playing) {
        pb.progress_ms += (uint32_t)(rtt_ms / 2);             /* the state is a little old by the time we see it */
        if (pb.duration_ms && pb.progress_ms > pb.duration_ms) {
            pb.progress_ms = pb.duration_ms;
        }
    }
    if (strcmp(pb.track_id, s.last.track_id) != 0 && pb.has_item) {
        ESP_LOGI(TAG, "now %s: \"%s\" - %s (%u ms) on %s", pb.is_playing ? "playing" : "paused", pb.title, pb.artist,
                 (unsigned)pb.duration_ms, pb.device_name);
    }
    s.last = pb;
    emit_status(pb.has_item ? SPOTIFY_STATUS_OK : SPOTIFY_STATUS_NO_DEVICE, "");
    emit_playback();
    update_art();
    s.next_poll_ms = !pb.has_item ? POLL_IDLE_MS : (pb.is_playing ? POLL_PLAYING_MS : POLL_PAUSED_MS);
}

/* ---- commands ---------------------------------------------------------------------------------- */

static void do_command(const cmd_msg_t *m)
{
    esp_http_client_method_t method = HTTP_METHOD_PUT;
    char path[96];
    switch (m->cmd) {
    case SPOTIFY_CMD_PLAY_PAUSE:
        if (s.last.is_playing) {
            strlcpy(path, "/me/player/pause", sizeof path);
        } else {
            strlcpy(path, "/me/player/play", sizeof path);
        }
        /* optimistic: flip the icon now, the next poll confirms or corrects it */
        s.last.is_playing = !s.last.is_playing;
        emit_playback();
        break;
    case SPOTIFY_CMD_NEXT:
        method = HTTP_METHOD_POST;
        strlcpy(path, "/me/player/next", sizeof path);
        break;
    case SPOTIFY_CMD_PREV:
        method = HTTP_METHOD_POST;
        strlcpy(path, "/me/player/previous", sizeof path);
        break;
    case SPOTIFY_CMD_SEEK:
        snprintf(path, sizeof path, "/me/player/seek?position_ms=%u", (unsigned)m->arg);
        break;
    default:
        return;
    }

    sp_resp_t resp;
    sp_resp_init(&resp, 4096);
    int st = api_call(method, path, &resp);
    if (st == API_NET_ERROR) {
        sp_http_drop_persistent();
        emit_status(SPOTIFY_STATUS_NET_ERROR, "command not delivered - check connection");
    } else if (st == API_AUTH_LOST) {
        sp_auth_forget();
        emit_status(SPOTIFY_STATUS_AUTH_ERROR, "Spotify authorization expired");
    } else if (st >= 200 && st < 300) {
        ESP_LOGI(TAG, "command %d ok", (int)m->cmd);
    } else {
        char msg[128];
        api_error_message(&resp, msg, sizeof msg);
        ESP_LOGW(TAG, "command %d -> HTTP %d %s", (int)m->cmd, st, msg);
        if (st == 404) {
            emit_status(SPOTIFY_STATUS_NO_DEVICE, "no active Spotify device");
        } else if (st == 403) {
            emit_status(SPOTIFY_STATUS_PREMIUM_REQUIRED, msg[0] ? msg : "playback control needs Spotify Premium");
        } else if (st == 429) {
            emit_status(SPOTIFY_STATUS_RATE_LIMITED, "Spotify rate limit - slow down");
        }
    }
    sp_resp_free(&resp);
    s.next_poll_ms = POLL_AFTER_CMD_MS;
}

/* ---- task -------------------------------------------------------------------------------------- */

static void drain_commands(void)
{
    cmd_msg_t m;
    while (xQueueReceive(s.q, &m, 0) == pdTRUE) {
    }
}

static void spotify_task(void *arg)
{
    (void)arg;
    s.next_poll_ms = 0;
    emit_status(SPOTIFY_STATUS_STARTING, "");

    for (;;) {
        if (!wifi_mgr_is_connected()) {
            emit_status(SPOTIFY_STATUS_WAITING_NETWORK, "Wi-Fi");
            drain_commands();
            wifi_mgr_wait_connected(1000);
            continue;
        }
        if (!wifi_mgr_time_synced()) {
            emit_status(SPOTIFY_STATUS_WAITING_NETWORK, "clock");
            drain_commands();
            wifi_mgr_wait_time(1000);
            continue;
        }
        if (!sp_auth_has_client_id()) {
            emit_status(SPOTIFY_STATUS_NO_CLIENT_ID, "");
            vTaskDelay(pdMS_TO_TICKS(2000));
            continue;
        }

        if (sp_setup_take_linked_flag()) {
            s.linked_at_us = esp_timer_get_time();
            s.next_poll_ms = 0;
            s.status_valid = false;
        }
        if (!sp_auth_has_refresh_token()) {
            if (sp_setup_start() == ESP_OK) {
                char url[48];
                snprintf(url, sizeof url, "http://%s/", wifi_mgr_ip());
                emit_status(SPOTIFY_STATUS_NEED_AUTH, url);
            }
            drain_commands();
            vTaskDelay(pdMS_TO_TICKS(500));
            continue;
        }
        if (s.linked_at_us && (esp_timer_get_time() - s.linked_at_us) > 4000000) {
            sp_setup_stop();                         /* let the "linked" page finish loading first */
            s.linked_at_us = 0;
        }

        cmd_msg_t m;
        if (xQueueReceive(s.q, &m, pdMS_TO_TICKS(s.next_poll_ms)) == pdTRUE) {
            do_command(&m);
        } else {
            poll_once();
        }
    }
}

/* ---- public ------------------------------------------------------------------------------------ */

esp_err_t spotify_start(const spotify_config_t *cfg)
{
    if (s.started || !cfg) {
        return ESP_ERR_INVALID_STATE;
    }
    s.cbs = cfg->cbs;
    s.q = xQueueCreate(8, sizeof(cmd_msg_t));
    if (!s.q) {
        return ESP_ERR_NO_MEM;
    }
    sp_http_init();
    sp_resp_init(&s.poll_resp, POLL_RESP_MAX);
    ESP_ERROR_CHECK(sp_auth_init(cfg->client_id, cfg->redirect_uri));
    s.started = true;
    if (xTaskCreatePinnedToCore(spotify_task, "spotify", TASK_STACK, NULL, TASK_PRIORITY, NULL, 0) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

void spotify_command(spotify_cmd_t cmd, uint32_t arg)
{
    if (!s.q) {
        return;
    }
    cmd_msg_t m = { cmd, arg };
    xQueueSend(s.q, &m, 0);
}

void spotify_forget_authorization(void)
{
    sp_auth_forget();
}

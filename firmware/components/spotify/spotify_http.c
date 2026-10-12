#include <stdlib.h>
#include <string.h>
#include <strings.h>

#include "esp_crt_bundle.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

#include "spotify_internal.h"

static const char *TAG = "sp_http";

#define DEFAULT_TIMEOUT_MS      12000
#define STALE_CONNECTION_US     (20LL * 1000 * 1000)    /* idle longer than this: do not trust a kept-alive socket */

static SemaphoreHandle_t s_lock;
static esp_http_client_handle_t s_api;       /* persistent api.spotify.com client */
static int64_t s_api_last_ok_us;
static sp_resp_t *s_cur;                     /* response being filled; guarded by s_lock */

void sp_http_init(void)
{
    if (!s_lock) {
        s_lock = xSemaphoreCreateMutex();
    }
}

/* ---- growable PSRAM response buffer ------------------------------------------------------------ */

void sp_resp_init(sp_resp_t *r, size_t max_body)
{
    memset(r, 0, sizeof *r);
    r->body.max = max_body;
}

void sp_resp_free(sp_resp_t *r)
{
    heap_caps_free(r->body.data);
    memset(&r->body, 0, sizeof r->body);
}

static bool buf_append(sp_buf_t *b, const void *data, size_t n)
{
    if (b->len + n + 1 > b->max) {
        b->truncated = true;
        return false;
    }
    if (b->len + n + 1 > b->cap) {
        size_t cap = b->cap ? b->cap : 4096;
        while (cap < b->len + n + 1) {
            cap *= 2;
        }
        if (cap > b->max) {
            cap = b->max;
        }
        char *p = heap_caps_realloc(b->data, cap, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (!p) {
            b->truncated = true;
            return false;
        }
        b->data = p;
        b->cap = cap;
    }
    memcpy(b->data + b->len, data, n);
    b->len += n;
    b->data[b->len] = '\0';
    return true;
}

static esp_err_t on_http_event(esp_http_client_event_t *evt)
{
    if (!s_cur) {
        return ESP_OK;
    }
    switch (evt->event_id) {
    case HTTP_EVENT_ON_HEADER:
        if (evt->header_key && strcasecmp(evt->header_key, "Retry-After") == 0 && evt->header_value) {
            s_cur->retry_after_s = atoi(evt->header_value);
        }
        break;
    case HTTP_EVENT_ON_DATA:
        if (evt->data_len > 0) {
            buf_append(&s_cur->body, evt->data, (size_t)evt->data_len);
        }
        break;
    default:
        break;
    }
    return ESP_OK;
}

static esp_http_client_handle_t new_client(const char *url, int timeout_ms, bool keep_alive)
{
    esp_http_client_config_t cfg = {
        .url = url,
        .event_handler = on_http_event,
        .crt_bundle_attach = esp_crt_bundle_attach,
        .timeout_ms = timeout_ms,
        .keep_alive_enable = keep_alive,
        .buffer_size = 2048,
        .buffer_size_tx = 1536,
    };
    return esp_http_client_init(&cfg);
}

void sp_http_drop_persistent(void)
{
    if (s_lock) {
        xSemaphoreTake(s_lock, portMAX_DELAY);
    }
    if (s_api) {
        esp_http_client_cleanup(s_api);
        s_api = NULL;
    }
    if (s_lock) {
        xSemaphoreGive(s_lock);
    }
}

esp_err_t sp_http_request(const sp_req_t *r, sp_resp_t *resp)
{
    sp_http_init();
    xSemaphoreTake(s_lock, portMAX_DELAY);

    resp->status = 0;
    resp->retry_after_s = 0;
    resp->body.len = 0;
    resp->body.truncated = false;
    if (resp->body.data) {
        resp->body.data[0] = '\0';
    }

    int timeout = r->timeout_ms > 0 ? r->timeout_ms : DEFAULT_TIMEOUT_MS;
    bool is_get = (r->method == HTTP_METHOD_GET);
    esp_err_t err = ESP_FAIL;

    for (int attempt = 0; attempt < 2; attempt++) {
        esp_http_client_handle_t h = NULL;
        if (r->persistent) {
            /* A kept-alive socket the server may already have closed is only retried for idempotent GETs;
             * before a command (POST/PUT) we reconnect up front instead of risking a double skip. */
            if (s_api && !is_get && (esp_timer_get_time() - s_api_last_ok_us) > STALE_CONNECTION_US) {
                esp_http_client_cleanup(s_api);
                s_api = NULL;
            }
            if (!s_api) {
                s_api = new_client(r->url, timeout, true);
            }
            h = s_api;
        } else {
            h = new_client(r->url, timeout, false);
        }
        if (!h) {
            err = ESP_ERR_NO_MEM;
            break;
        }

        s_cur = resp;
        resp->body.len = 0;
        if (resp->body.data) {
            resp->body.data[0] = '\0';
        }
        esp_http_client_set_url(h, r->url);
        esp_http_client_set_method(h, r->method);
        esp_http_client_set_timeout_ms(h, timeout);

        char hdr[640];
        if (r->bearer) {
            snprintf(hdr, sizeof hdr, "Bearer %s", r->bearer);
            esp_http_client_set_header(h, "Authorization", hdr);
            memset(hdr, 0, sizeof hdr);                       /* token must not linger on the stack */
        } else {
            esp_http_client_delete_header(h, "Authorization");
        }
        if (r->content_type) {
            esp_http_client_set_header(h, "Content-Type", r->content_type);
        } else {
            esp_http_client_delete_header(h, "Content-Type");
        }
        if (r->body && r->body_len) {
            esp_http_client_set_post_field(h, r->body, (int)r->body_len);
        } else {
            esp_http_client_set_post_field(h, NULL, 0);
            if (r->method == HTTP_METHOD_PUT || r->method == HTTP_METHOD_POST) {
                esp_http_client_set_header(h, "Content-Length", "0");   /* Spotify answers 411 without it */
            } else {
                esp_http_client_delete_header(h, "Content-Length");
            }
        }

        err = esp_http_client_perform(h);
        s_cur = NULL;

        if (err == ESP_OK) {
            resp->status = esp_http_client_get_status_code(h);
            if (r->persistent) {
                s_api_last_ok_us = esp_timer_get_time();
            }
        }
        if (!r->persistent) {
            esp_http_client_cleanup(h);
        } else if (err != ESP_OK) {
            esp_http_client_cleanup(s_api);
            s_api = NULL;
        }
        if (err == ESP_OK || !(r->persistent && is_get)) {
            break;                                           /* success, or not safe to retry blindly */
        }
        ESP_LOGW(TAG, "GET failed (%s), retrying on a fresh connection", esp_err_to_name(err));
    }

    xSemaphoreGive(s_lock);
    return err;
}

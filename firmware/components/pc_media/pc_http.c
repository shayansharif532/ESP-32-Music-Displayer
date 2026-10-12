#include <stdio.h>
#include <string.h>

#include "esp_heap_caps.h"
#include "esp_log.h"

#include "pc_internal.h"

static const char *TAG = "pc_http";

static esp_http_client_handle_t s_client;
static char s_client_host[24];
static uint16_t s_client_port;
static pc_resp_t *s_cur;               /* response being filled; only the poll task makes requests */

void pc_resp_init(pc_resp_t *r, size_t max_body)
{
    memset(r, 0, sizeof *r);
    r->body.max = max_body;
}

void pc_resp_free(pc_resp_t *r)
{
    heap_caps_free(r->body.data);
    memset(&r->body, 0, sizeof r->body);
}

static bool buf_append(pc_buf_t *b, const void *data, size_t n)
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

static esp_err_t on_event(esp_http_client_event_t *evt)
{
    if (evt->event_id == HTTP_EVENT_ON_DATA && s_cur && evt->data_len > 0) {
        buf_append(&s_cur->body, evt->data, (size_t)evt->data_len);
    }
    return ESP_OK;
}

void pc_http_drop(void)
{
    if (s_client) {
        esp_http_client_cleanup(s_client);
        s_client = NULL;
    }
}

esp_err_t pc_http_request(const char *host, uint16_t port, const char *path_query, bool post, int timeout_ms, pc_resp_t *resp)
{
    char url[96];
    snprintf(url, sizeof url, "http://%s:%u%s", host, (unsigned)port, path_query);

    if (s_client && (strcmp(s_client_host, host) != 0 || s_client_port != port)) {
        pc_http_drop();                                  /* companion moved: new connection */
    }
    if (!s_client) {
        esp_http_client_config_t cfg = {
            .url = url,
            .event_handler = on_event,
            .timeout_ms = timeout_ms,
            .keep_alive_enable = true,
            .buffer_size = 1024,
            .buffer_size_tx = 512,
        };
        s_client = esp_http_client_init(&cfg);
        if (!s_client) {
            return ESP_ERR_NO_MEM;
        }
        strlcpy(s_client_host, host, sizeof s_client_host);
        s_client_port = port;
    }

    resp->status = 0;
    resp->body.len = 0;
    resp->body.truncated = false;
    if (resp->body.data) {
        resp->body.data[0] = '\0';
    }
    s_cur = resp;
    esp_http_client_set_url(s_client, url);
    esp_http_client_set_timeout_ms(s_client, timeout_ms);
    if (post) {
        esp_http_client_set_method(s_client, HTTP_METHOD_POST);
        esp_http_client_set_header(s_client, "X-Desk-Client", "esp32");     /* the companion rejects POSTs without it */
        esp_http_client_set_post_field(s_client, NULL, 0);
        esp_http_client_set_header(s_client, "Content-Length", "0");
    } else {
        esp_http_client_set_method(s_client, HTTP_METHOD_GET);
        esp_http_client_delete_header(s_client, "X-Desk-Client");
        esp_http_client_delete_header(s_client, "Content-Length");
    }

    esp_err_t err = esp_http_client_perform(s_client);
    s_cur = NULL;
    if (err == ESP_OK) {
        resp->status = esp_http_client_get_status_code(s_client);
    } else {
        ESP_LOGD(TAG, "%s %s failed: %s", post ? "POST" : "GET", path_query, esp_err_to_name(err));
        pc_http_drop();                                  /* stale or dead socket: start clean next time */
    }
    return err;
}

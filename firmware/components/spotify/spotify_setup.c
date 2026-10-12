/*
 * On-device "Link Spotify" page (served on port 80 while no authorization is stored).
 *
 * Flow (PKCE, loopback redirect): the page offers an authorize link; Spotify then sends the browser to the
 * registered redirect URI (http://127.0.0.1:<port>/callback?code=...), which cannot load because 127.0.0.1 is the
 * browser's own computer - the user copies that address into the box below and the device completes the exchange.
 * The first-run captive portal (Milestone 11) will replace this with a smoother hand-off.
 *
 * Nothing sensitive is logged here: the pasted address contains the one-time authorization code.
 */
#include <ctype.h>
#include <string.h>

#include "esp_http_server.h"
#include "esp_log.h"

#include "spotify_internal.h"

static const char *TAG = "sp_setup";

static httpd_handle_t s_srv;
static volatile bool s_linked;

static const char PAGE_HEAD[] =
    "<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
    "<title>Spotify Desk Display</title><style>"
    "body{font-family:system-ui,Segoe UI,sans-serif;background:#0b0b10;color:#fff;max-width:540px;margin:0 auto;padding:24px 18px}"
    "h1{font-size:22px}p{color:#c8c8d4;line-height:1.45}b{color:#fff}"
    "a.btn,button{display:block;width:100%;box-sizing:border-box;background:#1ed760;color:#000;border:0;padding:16px;"
    "border-radius:32px;text-align:center;font-weight:700;font-size:16px;text-decoration:none;margin:14px 0}"
    "textarea{width:100%;box-sizing:border-box;height:7em;background:#1a1a22;color:#fff;border:1px solid #333;border-radius:10px;"
    "padding:10px;font-size:14px}.ok{color:#1ed760}.err{color:#ff5a5f}"
    "</style></head><body><h1>Link your Spotify</h1>";

static const char PAGE_TAIL[] = "</body></html>";

static void send_html_escaped(httpd_req_t *req, const char *s)
{
    char chunk[256];
    size_t o = 0;
    for (; *s; s++) {
        const char *rep = NULL;
        switch (*s) {
        case '&': rep = "&amp;"; break;
        case '<': rep = "&lt;"; break;
        case '>': rep = "&gt;"; break;
        case '"': rep = "&quot;"; break;
        default: break;
        }
        size_t need = rep ? strlen(rep) : 1;
        if (o + need >= sizeof chunk) {
            httpd_resp_send_chunk(req, chunk, o);
            o = 0;
        }
        if (rep) {
            memcpy(chunk + o, rep, need);
        } else {
            chunk[o] = *s;
        }
        o += need;
    }
    if (o) {
        httpd_resp_send_chunk(req, chunk, o);
    }
}

static esp_err_t root_get(httpd_req_t *req)
{
    char url[768];
    httpd_resp_set_type(req, "text/html; charset=utf-8");
    httpd_resp_send_chunk(req, PAGE_HEAD, HTTPD_RESP_USE_STRLEN);
    if (!sp_auth_authorize_url(url, sizeof url)) {
        httpd_resp_sendstr_chunk(req, "<p class=err>This build has no Spotify Client ID yet.</p>");
    } else if (sp_auth_has_refresh_token()) {
        httpd_resp_sendstr_chunk(req, "<p class=ok><b>Spotify is linked.</b> You can close this page.</p>");
    } else {
        httpd_resp_sendstr_chunk(req, "<p><b>Step 1.</b> Tap the green button, sign in to Spotify and press <b>Agree</b>.</p>"
                                      "<a class=btn target=_blank rel=noopener href=\"");
        send_html_escaped(req, url);
        httpd_resp_sendstr_chunk(req, "\">Authorize with Spotify</a>"
                                      "<p><b>Step 2.</b> Spotify then opens a page that says it <b>cannot be reached</b> - that is expected. "
                                      "Copy the <b>whole address</b> of that page (it starts with <b>http://127.0.0.1:8888/callback?code=</b>) "
                                      "and paste it here:</p>"
                                      "<form method=post action=/link><textarea name=url required autocomplete=off "
                                      "placeholder='http://127.0.0.1:8888/callback?code=...'></textarea>"
                                      "<button type=submit>Finish linking</button></form>");
    }
    httpd_resp_send_chunk(req, PAGE_TAIL, HTTPD_RESP_USE_STRLEN);
    return httpd_resp_send_chunk(req, NULL, 0);
}

/* Extracts and percent-decodes the value of `name` from an application/x-www-form-urlencoded body. */
static bool form_get(const char *body, const char *name, char *out, size_t out_len)
{
    size_t nlen = strlen(name);
    for (const char *p = body; p && *p;) {
        if (strncmp(p, name, nlen) == 0 && p[nlen] == '=') {
            const char *v = p + nlen + 1;
            size_t o = 0;
            for (; *v && *v != '&' && o + 1 < out_len; v++) {
                if (*v == '%' && isxdigit((unsigned char)v[1]) && isxdigit((unsigned char)v[2])) {
                    char h[3] = { v[1], v[2], 0 };
                    out[o++] = (char)strtol(h, NULL, 16);
                    v += 2;
                } else if (*v == '+') {
                    out[o++] = ' ';
                } else {
                    out[o++] = *v;
                }
            }
            out[o] = '\0';
            return true;
        }
        const char *amp = strchr(p, '&');
        p = amp ? amp + 1 : NULL;
    }
    return false;
}

static esp_err_t link_post(httpd_req_t *req)
{
    char body[1600];
    if (req->content_len == 0 || req->content_len >= sizeof body) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "bad request");
        return ESP_OK;
    }
    size_t got = 0;
    while (got < req->content_len) {
        int n = httpd_req_recv(req, body + got, req->content_len - got);
        if (n <= 0) {
            httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "bad request");
            return ESP_OK;
        }
        got += (size_t)n;
    }
    body[got] = '\0';

    char pasted[1200], detail[128] = "";
    sp_auth_result_t r = SP_AUTH_REVOKED;
    if (form_get(body, "url", pasted, sizeof pasted)) {
        r = sp_auth_complete(pasted, detail, sizeof detail);
    } else {
        snprintf(detail, sizeof detail, "nothing was pasted");
    }
    memset(body, 0, sizeof body);
    memset(pasted, 0, sizeof pasted);

    httpd_resp_set_type(req, "text/html; charset=utf-8");
    httpd_resp_send_chunk(req, PAGE_HEAD, HTTPD_RESP_USE_STRLEN);
    if (r == SP_AUTH_OK) {
        s_linked = true;
        ESP_LOGI(TAG, "Spotify linked via the setup page");
        httpd_resp_sendstr_chunk(req, "<p class=ok><b>Done - Spotify is linked.</b></p>"
                                      "<p>The display will start showing your music in a few seconds. You can close this page.</p>");
    } else {
        ESP_LOGW(TAG, "linking failed (%d)", (int)r);
        httpd_resp_sendstr_chunk(req, "<p class=err><b>That did not work.</b></p><p>");
        send_html_escaped(req, detail);
        httpd_resp_sendstr_chunk(req, "</p><a class=btn href=/>Try again</a>");
    }
    httpd_resp_send_chunk(req, PAGE_TAIL, HTTPD_RESP_USE_STRLEN);
    return httpd_resp_send_chunk(req, NULL, 0);
}

esp_err_t sp_setup_start(void)
{
    if (s_srv) {
        return ESP_OK;
    }
    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.stack_size = 12288;                 /* the /link handler does a TLS request to Spotify */
    cfg.max_uri_len = 256;
    cfg.max_req_hdr_len = 1024;
    cfg.lru_purge_enable = true;
    esp_err_t err = httpd_start(&s_srv, &cfg);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "web server start failed: %s", esp_err_to_name(err));
        s_srv = NULL;
        return err;
    }
    const httpd_uri_t root = { .uri = "/", .method = HTTP_GET, .handler = root_get };
    const httpd_uri_t link = { .uri = "/link", .method = HTTP_POST, .handler = link_post };
    httpd_register_uri_handler(s_srv, &root);
    httpd_register_uri_handler(s_srv, &link);
    ESP_LOGI(TAG, "link page is up");
    return ESP_OK;
}

void sp_setup_stop(void)
{
    if (s_srv) {
        httpd_stop(s_srv);
        s_srv = NULL;
        ESP_LOGI(TAG, "link page stopped");
    }
}

bool sp_setup_take_linked_flag(void)
{
    bool v = s_linked;
    s_linked = false;
    return v;
}

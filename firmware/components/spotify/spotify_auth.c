/*
 * Spotify Authorization Code flow with PKCE (RFC 7636). No client secret is used or stored.
 * Secrets (refresh token, access token, code verifier, authorization code) are never logged.
 */
#include <ctype.h>
#include <stdio.h>
#include <string.h>

#include "cJSON.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "mbedtls/base64.h"
#include "mbedtls/sha256.h"
#include "nvs.h"

#include "spotify_internal.h"

static const char *TAG = "sp_auth";

#define NVS_NS              "spotify"
#define NVS_KEY_CLIENT      "client_id"
#define NVS_KEY_REFRESH     "refresh"

#define AUTHORIZE_URL       "https://accounts.spotify.com/authorize"
#define TOKEN_URL           "https://accounts.spotify.com/api/token"
#define SCOPES              "user-read-playback-state user-modify-playback-state user-read-currently-playing"
#define TOKEN_SKEW_US       (60LL * 1000 * 1000)

static struct {
    SemaphoreHandle_t lock;
    char client_id[64];
    char redirect_uri[160];
    char refresh[512];
    char access[768];
    int64_t access_expires_us;        /* esp_timer time at which the access token expires */
    char verifier[96];                /* PKCE code_verifier of the pending authorization, "" if none */
    char challenge[48];
    char state[24];
} s;

/* ---- small helpers ------------------------------------------------------------------------------ */

static void url_encode(const char *in, char *out, size_t out_len)
{
    static const char hex[] = "0123456789ABCDEF";
    size_t o = 0;
    for (; *in && o + 4 < out_len; in++) {
        unsigned char c = (unsigned char)*in;
        if (isalnum(c) || c == '-' || c == '.' || c == '_' || c == '~') {
            out[o++] = (char)c;
        } else {
            out[o++] = '%';
            out[o++] = hex[c >> 4];
            out[o++] = hex[c & 15];
        }
    }
    out[o] = '\0';
}

static void url_decode_inplace(char *str)
{
    char *w = str;
    for (const char *r = str; *r; r++) {
        if (*r == '%' && isxdigit((unsigned char)r[1]) && isxdigit((unsigned char)r[2])) {
            char h[3] = { r[1], r[2], 0 };
            *w++ = (char)strtol(h, NULL, 16);
            r += 2;
        } else if (*r == '+') {
            *w++ = ' ';
        } else {
            *w++ = *r;
        }
    }
    *w = '\0';
}

/* Finds "name=" in a query string and copies its (decoded) value; false if absent. */
static bool query_param(const char *query, const char *name, char *out, size_t out_len)
{
    size_t nlen = strlen(name);
    const char *p = query;
    while (p && *p) {
        if (strncmp(p, name, nlen) == 0 && p[nlen] == '=') {
            const char *v = p + nlen + 1;
            size_t n = strcspn(v, "&# \r\n");
            if (n >= out_len) {
                n = out_len - 1;
            }
            memcpy(out, v, n);
            out[n] = '\0';
            url_decode_inplace(out);
            return true;
        }
        const char *amp = strchr(p, '&');
        p = amp ? amp + 1 : NULL;
    }
    return false;
}

static void base64url(const uint8_t *in, size_t in_len, char *out, size_t out_len)
{
    size_t n = 0;
    if (mbedtls_base64_encode((unsigned char *)out, out_len, &n, in, in_len) != 0) {
        out[0] = '\0';
        return;
    }
    out[n] = '\0';
    for (char *c = out; *c; c++) {
        if (*c == '+') *c = '-';
        else if (*c == '/') *c = '_';
    }
    while (n > 0 && out[n - 1] == '=') {
        out[--n] = '\0';
    }
}

static void new_pkce_session(void)
{
    static const char alphabet[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~";
    uint8_t rnd[64];
    esp_fill_random(rnd, sizeof rnd);
    for (size_t i = 0; i < sizeof rnd; i++) {
        s.verifier[i] = alphabet[rnd[i] % (sizeof alphabet - 1)];
    }
    s.verifier[sizeof rnd] = '\0';

    uint8_t digest[32];
    mbedtls_sha256((const unsigned char *)s.verifier, strlen(s.verifier), digest, 0);
    base64url(digest, sizeof digest, s.challenge, sizeof s.challenge);

    uint8_t st[12];
    esp_fill_random(st, sizeof st);
    base64url(st, sizeof st, s.state, sizeof s.state);
}

static void nvs_store(const char *key, const char *value)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) == ESP_OK) {
        if (value && *value) {
            nvs_set_str(h, key, value);
        } else {
            nvs_erase_key(h, key);
        }
        nvs_commit(h);
        nvs_close(h);
    }
}

static bool nvs_load(const char *key, char *out, size_t out_len)
{
    nvs_handle_t h;
    bool ok = false;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) == ESP_OK) {
        size_t n = out_len;
        ok = (nvs_get_str(h, key, out, &n) == ESP_OK);
        nvs_close(h);
    }
    if (!ok) {
        out[0] = '\0';
    }
    return ok;
}

/* ---- public API --------------------------------------------------------------------------------- */

esp_err_t sp_auth_init(const char *client_id, const char *redirect_uri)
{
    if (!s.lock) {
        s.lock = xSemaphoreCreateMutex();
    }
    xSemaphoreTake(s.lock, portMAX_DELAY);
    strlcpy(s.redirect_uri, redirect_uri ? redirect_uri : "", sizeof s.redirect_uri);

    char stored[sizeof s.client_id] = "";
    nvs_load(NVS_KEY_CLIENT, stored, sizeof stored);
    if (client_id && *client_id) {
        strlcpy(s.client_id, client_id, sizeof s.client_id);
        if (strcmp(stored, s.client_id) != 0) {
            /* A different Spotify app: refresh tokens of the old one are useless. */
            nvs_store(NVS_KEY_CLIENT, s.client_id);
            nvs_store(NVS_KEY_REFRESH, NULL);
        }
    } else {
        strlcpy(s.client_id, stored, sizeof s.client_id);
    }
    nvs_load(NVS_KEY_REFRESH, s.refresh, sizeof s.refresh);
    s.access[0] = '\0';
    s.access_expires_us = 0;
    xSemaphoreGive(s.lock);
    ESP_LOGI(TAG, "client id %s, authorization %s", s.client_id[0] ? "configured" : "MISSING",
             s.refresh[0] ? "present" : "not yet granted");
    return ESP_OK;
}

bool sp_auth_has_client_id(void) { return s.client_id[0] != '\0'; }
bool sp_auth_has_refresh_token(void) { return s.refresh[0] != '\0'; }

bool sp_auth_authorize_url(char *out, size_t out_len)
{
    if (!s.client_id[0]) {
        return false;
    }
    xSemaphoreTake(s.lock, portMAX_DELAY);
    if (!s.verifier[0]) {
        new_pkce_session();
    }
    char redirect[256], scope[200];
    url_encode(s.redirect_uri, redirect, sizeof redirect);
    url_encode(SCOPES, scope, sizeof scope);
    int n = snprintf(out, out_len,
                     AUTHORIZE_URL "?response_type=code&client_id=%s&redirect_uri=%s&scope=%s"
                     "&code_challenge_method=S256&code_challenge=%s&state=%s",
                     s.client_id, redirect, scope, s.challenge, s.state);
    xSemaphoreGive(s.lock);
    return n > 0 && (size_t)n < out_len;
}

/* POSTs a form to the token endpoint and applies the answer. */
static sp_auth_result_t token_request(const char *form, char *err_out, size_t err_len)
{
    sp_req_t req = {
        .url = TOKEN_URL,
        .method = HTTP_METHOD_POST,
        .content_type = "application/x-www-form-urlencoded",
        .body = form,
        .body_len = strlen(form),
        .timeout_ms = 15000,
    };
    sp_resp_t resp;
    sp_resp_init(&resp, 8192);
    esp_err_t err = sp_http_request(&req, &resp);
    sp_auth_result_t result = SP_AUTH_NET;

    if (err != ESP_OK) {
        ESP_LOGW(TAG, "token request failed: %s", esp_err_to_name(err));
        if (err_out) snprintf(err_out, err_len, "network error contacting Spotify");
    } else if (resp.status == 200 && resp.body.data) {
        cJSON *root = cJSON_ParseWithLength(resp.body.data, resp.body.len);
        const cJSON *at = cJSON_GetObjectItemCaseSensitive(root, "access_token");
        const cJSON *ex = cJSON_GetObjectItemCaseSensitive(root, "expires_in");
        const cJSON *rt = cJSON_GetObjectItemCaseSensitive(root, "refresh_token");
        if (cJSON_IsString(at) && at->valuestring[0] && strlen(at->valuestring) < sizeof s.access) {
            double secs = cJSON_IsNumber(ex) ? ex->valuedouble : 3600;
            xSemaphoreTake(s.lock, portMAX_DELAY);
            strlcpy(s.access, at->valuestring, sizeof s.access);
            s.access_expires_us = esp_timer_get_time() + (int64_t)(secs * 1e6);
            if (cJSON_IsString(rt) && rt->valuestring[0] && strlen(rt->valuestring) < sizeof s.refresh &&
                strcmp(rt->valuestring, s.refresh) != 0) {
                strlcpy(s.refresh, rt->valuestring, sizeof s.refresh);
                nvs_store(NVS_KEY_REFRESH, s.refresh);
            }
            xSemaphoreGive(s.lock);
            ESP_LOGI(TAG, "access token obtained (valid %d s)", (int)secs);
            result = SP_AUTH_OK;
        } else if (err_out) {
            snprintf(err_out, err_len, "unexpected answer from Spotify");
        }
        cJSON_Delete(root);
    } else if (resp.status == 400 || resp.status == 401) {
        /* e.g. {"error":"invalid_grant","error_description":"Invalid authorization code"} - safe to log, no secrets. */
        char code[48] = "", desc[96] = "";
        if (resp.body.data) {
            cJSON *root = cJSON_ParseWithLength(resp.body.data, resp.body.len);
            const cJSON *e = cJSON_GetObjectItemCaseSensitive(root, "error");
            const cJSON *d = cJSON_GetObjectItemCaseSensitive(root, "error_description");
            if (cJSON_IsString(e)) strlcpy(code, e->valuestring, sizeof code);
            if (cJSON_IsString(d)) strlcpy(desc, d->valuestring, sizeof desc);
            cJSON_Delete(root);
        }
        ESP_LOGW(TAG, "token endpoint rejected the request: HTTP %d %s: %s", resp.status, code, desc);
        if (err_out) snprintf(err_out, err_len, "Spotify says: %s %s", code, desc);
        result = SP_AUTH_REVOKED;
    } else {
        ESP_LOGW(TAG, "token endpoint HTTP %d", resp.status);
        if (err_out) snprintf(err_out, err_len, "Spotify answered HTTP %d", resp.status);
    }
    sp_resp_free(&resp);
    return result;
}

sp_auth_result_t sp_auth_complete(const char *input, char *err_out, size_t err_len)
{
    if (!s.client_id[0]) {
        return SP_AUTH_NO_CLIENT_ID;
    }
    if (!s.verifier[0]) {
        if (err_out) snprintf(err_out, err_len, "no authorization in progress - reload the page and start again");
        return SP_AUTH_NO_TOKEN;
    }

    /* Accept the full redirect URL (".../callback?code=...&state=...") or a bare code. */
    const char *q = strchr(input, '?');
    q = q ? q + 1 : input;
    char code[600] = "", state[48] = "", error[48] = "";
    bool have_code = query_param(q, "code", code, sizeof code);
    bool have_state = query_param(q, "state", state, sizeof state);
    if (query_param(q, "error", error, sizeof error)) {
        if (err_out) snprintf(err_out, err_len, "Spotify reported: %s", error);
        return SP_AUTH_REVOKED;
    }
    if (!have_code) {
        if (strlen(input) > 20 && !strchr(input, '=') && !strchr(input, '/')) {
            strlcpy(code, input, sizeof code);               /* looks like a bare code */
        } else {
            if (err_out) snprintf(err_out, err_len, "that does not contain a Spotify code");
            code[0] = '\0';
            return SP_AUTH_REVOKED;
        }
    }
    if (have_state && strcmp(state, s.state) != 0) {
        if (err_out) snprintf(err_out, err_len, "this link belongs to an older attempt - reload the page and start again");
        memset(code, 0, sizeof code);
        return SP_AUTH_REVOKED;
    }

    char c_enc[700], r_enc[256], v_enc[128], form[1400];
    url_encode(code, c_enc, sizeof c_enc);
    url_encode(s.redirect_uri, r_enc, sizeof r_enc);
    url_encode(s.verifier, v_enc, sizeof v_enc);
    snprintf(form, sizeof form, "grant_type=authorization_code&code=%s&redirect_uri=%s&client_id=%s&code_verifier=%s",
             c_enc, r_enc, s.client_id, v_enc);
    sp_auth_result_t r = token_request(form, err_out, err_len);

    memset(code, 0, sizeof code);
    memset(c_enc, 0, sizeof c_enc);
    memset(form, 0, sizeof form);
    if (r == SP_AUTH_OK || r == SP_AUTH_REVOKED) {
        /* The code is single-use either way: start a fresh PKCE session for the next attempt. */
        xSemaphoreTake(s.lock, portMAX_DELAY);
        memset(s.verifier, 0, sizeof s.verifier);
        xSemaphoreGive(s.lock);
    }
    return r;
}

sp_auth_result_t sp_auth_access_token(char *out, size_t out_len, bool force_refresh)
{
    if (!s.client_id[0]) {
        return SP_AUTH_NO_CLIENT_ID;
    }
    xSemaphoreTake(s.lock, portMAX_DELAY);
    bool have_refresh = s.refresh[0] != '\0';
    bool fresh = s.access[0] && (esp_timer_get_time() + TOKEN_SKEW_US) < s.access_expires_us;
    if (fresh && !force_refresh) {
        strlcpy(out, s.access, out_len);
        xSemaphoreGive(s.lock);
        return SP_AUTH_OK;
    }
    if (!have_refresh) {
        xSemaphoreGive(s.lock);
        return SP_AUTH_NO_TOKEN;
    }
    char r_enc[600], c_enc[80], form[800];
    url_encode(s.refresh, r_enc, sizeof r_enc);
    url_encode(s.client_id, c_enc, sizeof c_enc);
    snprintf(form, sizeof form, "grant_type=refresh_token&refresh_token=%s&client_id=%s", r_enc, c_enc);
    xSemaphoreGive(s.lock);

    char detail[96] = "";
    sp_auth_result_t r = token_request(form, detail, sizeof detail);
    memset(form, 0, sizeof form);
    memset(r_enc, 0, sizeof r_enc);
    if (r == SP_AUTH_OK) {
        xSemaphoreTake(s.lock, portMAX_DELAY);
        strlcpy(out, s.access, out_len);
        xSemaphoreGive(s.lock);
    }
    return r;
}

void sp_auth_forget(void)
{
    xSemaphoreTake(s.lock, portMAX_DELAY);
    memset(s.refresh, 0, sizeof s.refresh);
    memset(s.access, 0, sizeof s.access);
    memset(s.verifier, 0, sizeof s.verifier);
    s.access_expires_us = 0;
    nvs_store(NVS_KEY_REFRESH, NULL);
    xSemaphoreGive(s.lock);
    ESP_LOGW(TAG, "stored Spotify authorization erased");
}

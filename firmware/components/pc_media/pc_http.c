/*
 * pc_http: one persistent plain-HTTP connection to the companion, with the request signing and response verification of
 * docs/protocol.md section 2.
 *
 * Why the low-level esp_http_client API (open / fetch_headers / read) and not esp_http_client_perform(): perform() gives up on a 401
 * without a WWW-Authenticate header before the body is read, but the display needs that body ("auth" or "unpaired"). Reading the body
 * ourselves also lets us hash it as it arrives and never buffer more than the caller allows.
 *
 * Only the poll task makes requests (the statics below are not locked).
 */
#include <stdio.h>
#include <string.h>
#include <strings.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "mbedtls/sha256.h"

#include "pc_internal.h"

static const char *TAG = "pc_http";

#define NONCE_CHARS     32
#define CHUNK_BYTES     1024
#define NONCE_MAX_AGE_MS 45000                  /* the companion forgets a nonce after 60 s: do not spend a stale one (a refused signature counts
                                                 * against the per-address failure limiter) */

static esp_http_client_handle_t s_client;
static char s_client_host[24];
static uint16_t s_client_port;
static char s_nonce[NONCE_CHARS + 1];       /* the next signed request's nonce: X-DM-Next of the last answer we may use; "" = none */
static int64_t s_nonce_ms;                  /* when it was received */
static pc_auth_result_t s_last;
static uint8_t s_chunk[CHUNK_BYTES];        /* body read buffer */

static struct {                             /* the interesting headers of the answer being read (filled by on_event) */
    char next[NONCE_CHARS + 1];
    char proof[65];
    bool have_next, have_proof;
} s_hdr;

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

static void resp_reset(pc_resp_t *r)
{
    r->status = 0;
    r->body.len = 0;
    r->body.truncated = false;
    if (r->body.data) {
        r->body.data[0] = '\0';
    }
}

/* Only the two protocol headers are looked at; both are validated here, so a hostile value can never be longer than its buffer. */
static esp_err_t on_event(esp_http_client_event_t *evt)
{
    if (evt->event_id == HTTP_EVENT_ON_HEADER && evt->header_key && evt->header_value) {
        if (strcasecmp(evt->header_key, "X-DM-Next") == 0) {
            s_hdr.have_next = pc_auth_valid_nonce(evt->header_value);
            if (s_hdr.have_next) {
                memcpy(s_hdr.next, evt->header_value, NONCE_CHARS + 1);
            }
        } else if (strcasecmp(evt->header_key, "X-DM-Proof") == 0) {
            s_hdr.have_proof = strlen(evt->header_value) == 64;
            if (s_hdr.have_proof) {
                memcpy(s_hdr.proof, evt->header_value, 65);
            }
        }
    }
    return ESP_OK;
}

static void keep_nonce(void)                /* the answer just read offered s_hdr.next */
{
    memcpy(s_nonce, s_hdr.next, sizeof s_nonce);
    s_nonce_ms = esp_timer_get_time() / 1000;
}

void pc_http_drop(void)
{
    if (s_client) {
        esp_http_client_cleanup(s_client);
        s_client = NULL;
    }
    s_nonce[0] = '\0';                                   /* it may have been spent on the request that failed */
}

/* The routes that need no signature (protocol section 2). */
static bool route_is_open(const char *pq)
{
    size_t n = strcspn(pq, "?");
    return (n == 6 && strncmp(pq, "/hello", 6) == 0) || (n == 7 && strncmp(pq, "/health", 7) == 0) || strncmp(pq, "/pair/", 6) == 0;
}

/* One HTTP exchange on the kept-alive connection. nonce / mac NULL = unsigned. On ESP_OK: resp->status and the body are set, `sha` is
 * the SHA-256 of the whole body and s_hdr holds X-DM-Next / X-DM-Proof. Any failure (also a body that does not fit) drops the
 * connection and leaves status 0. */
static esp_err_t exchange(const char *host, uint16_t port, const char *pq, bool post, const char *json, int timeout_ms,
                          const char *nonce, const char *mac, pc_resp_t *resp, uint8_t sha[32])
{
    char url[272];
    if (snprintf(url, sizeof url, "http://%s:%u%s", host, (unsigned)port, pq) >= (int)sizeof url) {
        return ESP_ERR_INVALID_SIZE;
    }
    resp_reset(resp);
    memset(&s_hdr, 0, sizeof s_hdr);

    if (s_client && (strcmp(s_client_host, host) != 0 || s_client_port != port)) {
        pc_http_drop();                                  /* companion moved: new connection, and its nonce is not ours */
    }
    if (!s_client) {
        esp_http_client_config_t cfg = {
            .url = url,
            .event_handler = on_event,
            .timeout_ms = timeout_ms,
            .keep_alive_enable = true,
            .buffer_size = 1024,
            .buffer_size_tx = 768,
        };
        s_client = esp_http_client_init(&cfg);
        if (!s_client) {
            return ESP_ERR_NO_MEM;
        }
        strlcpy(s_client_host, host, sizeof s_client_host);
        s_client_port = port;
    }

    const int jlen = json ? (int)strlen(json) : 0;
    esp_http_client_set_url(s_client, url);
    esp_http_client_set_timeout_ms(s_client, timeout_ms);
    if (post) {
        esp_http_client_set_method(s_client, HTTP_METHOD_POST);
        esp_http_client_set_header(s_client, "X-Desk-Client", "esp32");     /* the companion rejects POSTs without it */
        if (json) {
            esp_http_client_set_header(s_client, "Content-Type", "application/json");
        } else {
            esp_http_client_delete_header(s_client, "Content-Type");
        }
    } else {
        esp_http_client_set_method(s_client, HTTP_METHOD_GET);
        esp_http_client_delete_header(s_client, "X-Desk-Client");
        esp_http_client_delete_header(s_client, "Content-Type");
    }
    if (nonce && mac) {
        esp_http_client_set_header(s_client, "X-DM-Nonce", nonce);
        esp_http_client_set_header(s_client, "X-DM-Auth", mac);
    } else {
        esp_http_client_delete_header(s_client, "X-DM-Nonce");
        esp_http_client_delete_header(s_client, "X-DM-Auth");
    }

    int status = 0;
    int64_t clen = -1;
    esp_err_t err = esp_http_client_open(s_client, jlen);          /* (sets Content-Length itself) */
    if (err == ESP_OK && jlen > 0 && esp_http_client_write(s_client, json, jlen) != jlen) {
        err = ESP_ERR_HTTP_WRITE_DATA;
    }
    if (err == ESP_OK) {
        clen = esp_http_client_fetch_headers(s_client);           /* > 0: Content-Length, 0: chunked / unknown, < 0: error */
        if (clen < 0) {
            err = ESP_ERR_HTTP_FETCH_HEADER;
        } else {
            status = esp_http_client_get_status_code(s_client);
            if (status < 100 || status > 599) {
                err = ESP_ERR_INVALID_RESPONSE;
            }
        }
    }

    mbedtls_sha256_context sc;
    mbedtls_sha256_init(&sc);
    if (err == ESP_OK && mbedtls_sha256_starts(&sc, 0) != 0) {
        err = ESP_FAIL;
    }
    if (err == ESP_OK && clen > 0 && (uint64_t)clen + 1 > resp->body.max) {
        resp->body.truncated = true;                              /* would not fit: nothing larger than the caller's limit is read */
        err = ESP_ERR_INVALID_SIZE;
    }
    int64_t total = 0;
    while (err == ESP_OK) {
        size_t want = sizeof s_chunk;
        if (clen > 0) {
            if (total >= clen) {
                break;
            }
            if ((int64_t)want > clen - total) {
                want = (size_t)(clen - total);
            }
        }
        int n = esp_http_client_read(s_client, (char *)s_chunk, (int)want);
        if (n < 0) {
            err = ESP_FAIL;                                       /* timeout or reset in the middle of the body */
        } else if (n == 0) {
            if (clen > 0) {
                err = ESP_ERR_INVALID_RESPONSE;                   /* ended before Content-Length */
            }
            break;                                                /* chunked / unknown length: complete */
        } else {
            total += n;
            if (mbedtls_sha256_update(&sc, s_chunk, (size_t)n) != 0) {
                err = ESP_FAIL;
            } else if (!usb_link_buf_append(&resp->body, s_chunk, (size_t)n)) {
                err = ESP_ERR_INVALID_SIZE;                       /* too big (or no memory): truncated flag is set */
            }
        }
    }
    if (err == ESP_OK && mbedtls_sha256_finish(&sc, sha) != 0) {
        err = ESP_FAIL;
    }
    mbedtls_sha256_free(&sc);

    if (err != ESP_OK) {
        ESP_LOGD(TAG, "%s %s failed: %s", post ? "POST" : "GET", pq, esp_err_to_name(err));
        resp->status = 0;
        pc_http_drop();                                           /* stale or dead socket, or half a body: start clean next time */
        return err;
    }
    resp->status = status;
    if (!esp_http_client_is_persistent_connection(s_client)) {
        esp_http_client_close(s_client);                          /* the companion closes after this answer */
    }
    return ESP_OK;
}

/* A nonce for the next signed request: GET /hello (unsigned) answers with X-DM-Next. */
static esp_err_t fetch_nonce(const char *host, uint16_t port, int timeout_ms)
{
    pc_resp_t r;
    pc_resp_init(&r, 1024);
    uint8_t sha[32];
    esp_err_t err = exchange(host, port, "/hello", false, NULL, timeout_ms, NULL, NULL, &r, sha);
    pc_resp_free(&r);
    if (err != ESP_OK) {
        return err;
    }
    if (!s_hdr.have_next) {
        return ESP_ERR_INVALID_RESPONSE;
    }
    keep_nonce();
    return ESP_OK;
}

static esp_err_t signed_request(const char *host, uint16_t port, const char *pq, bool post, int timeout_ms, pc_resp_t *resp)
{
    if (!pc_auth_has_key()) {
        s_last = PC_AUTH_NO_KEY;
        return ESP_ERR_INVALID_STATE;
    }
    const char *method = post ? "POST" : "GET";
    for (int attempt = 0; attempt < 2; attempt++) {
        if (s_nonce[0] && esp_timer_get_time() / 1000 - s_nonce_ms > NONCE_MAX_AGE_MS) {
            s_nonce[0] = '\0';                                    /* too old to be accepted: ask for a new one */
        }
        if (!s_nonce[0]) {
            esp_err_t e = fetch_nonce(host, port, timeout_ms);
            if (e != ESP_OK) {
                return e;
            }
        }
        char nonce[NONCE_CHARS + 1], mac[65];
        memcpy(nonce, s_nonce, sizeof nonce);
        s_nonce[0] = '\0';                                        /* a nonce works exactly once */
        if (!pc_auth_sign(method, pq, nonce, mac)) {
            return ESP_FAIL;
        }
        uint8_t sha[32];
        esp_err_t err = exchange(host, port, pq, post, NULL, timeout_ms, nonce, mac, resp, sha);
        if (err != ESP_OK) {
            return err;
        }
        const int st = resp->status;
        if (st == 401 || st == 429) {                             /* never trusted, but the reason is useful */
            const bool unpaired = st == 401 && resp->body.data && strstr(resp->body.data, "\"unpaired\"") != NULL;
            const bool fresh = s_hdr.have_next;
            if (fresh) {
                keep_nonce();
            }
            s_last = st == 429 ? PC_AUTH_LIMITED : (unpaired ? PC_AUTH_UNPAIRED : PC_AUTH_FAILED);
            if (st == 401 && !unpaired && fresh && attempt == 0) {
                continue;                                         /* an expired / replayed nonce: one retry with the fresh one */
            }
            return ESP_OK;
        }
        if (!(s_hdr.have_proof && !resp->body.truncated && pc_auth_verify_proof(nonce, st, sha, s_hdr.proof))) {
            ESP_LOGW(TAG, "answer (HTTP %d) without a valid proof: discarded", st);
            resp_reset(resp);
            s_last = PC_AUTH_BAD_PROOF;
            pc_http_drop();
            return ESP_ERR_INVALID_RESPONSE;
        }
        if (s_hdr.have_next) {
            keep_nonce();
        }
        s_last = PC_AUTH_OK;
        return ESP_OK;
    }
    return ESP_FAIL;                                              /* not reached: the second attempt always returns above */
}

esp_err_t pc_http_request(const char *host, uint16_t port, const char *path_query, bool post, int timeout_ms, pc_resp_t *resp)
{
    resp_reset(resp);
    s_last = PC_AUTH_NONE;
    if (route_is_open(path_query)) {
        uint8_t sha[32];
        esp_err_t err = exchange(host, port, path_query, post, NULL, timeout_ms, NULL, NULL, resp, sha);
        if (err == ESP_OK && s_hdr.have_next) {
            keep_nonce();                                         /* every answer offers a nonce: GET /hello is how a display asks */
        }
        if (err == ESP_OK && resp->status == 429) {
            s_last = PC_AUTH_LIMITED;
        }
        return err;
    }
    return signed_request(host, port, path_query, post, timeout_ms, resp);
}

esp_err_t pc_http_post_json(const char *host, uint16_t port, const char *path, const char *json, int timeout_ms, pc_resp_t *resp)
{
    resp_reset(resp);
    s_last = PC_AUTH_NONE;
    if (!route_is_open(path) || !json) {
        return ESP_ERR_INVALID_ARG;                               /* only the unauthenticated pairing routes take a body */
    }
    uint8_t sha[32];
    esp_err_t err = exchange(host, port, path, true, json, timeout_ms, NULL, NULL, resp, sha);
    if (err == ESP_OK && s_hdr.have_next) {
        keep_nonce();
    }
    if (err == ESP_OK && resp->status == 429) {
        s_last = PC_AUTH_LIMITED;
    }
    return err;
}

pc_auth_result_t pc_http_last_auth(void)
{
    return s_last;
}

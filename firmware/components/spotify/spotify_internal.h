#pragma once
/* Internal interfaces between the spotify component's source files. Not part of the public API. */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"
#include "esp_http_client.h"
#include "spotify.h"

/* ---- spotify_http.c: HTTPS helper (one persistent connection to api.spotify.com, one-shot others) ---- */

typedef struct {
    char  *data;            /* NUL-terminated, lives in PSRAM */
    size_t len;
    size_t cap;
    size_t max;             /* hard limit; body beyond it is dropped and `truncated` is set */
    bool   truncated;
} sp_buf_t;

typedef struct {
    int      status;        /* HTTP status, 0 if the request itself failed */
    int      retry_after_s; /* from the Retry-After header, 0 if absent */
    sp_buf_t body;
} sp_resp_t;

typedef struct {
    const char *url;
    esp_http_client_method_t method;
    const char *bearer;          /* access token, or NULL */
    const char *content_type;    /* for a body, or NULL */
    const char *body;
    size_t      body_len;
    int         timeout_ms;      /* 0 = default */
    bool        persistent;      /* reuse the keep-alive connection to api.spotify.com */
} sp_req_t;

void sp_http_init(void);
void sp_resp_init(sp_resp_t *r, size_t max_body);
void sp_resp_free(sp_resp_t *r);
esp_err_t sp_http_request(const sp_req_t *req, sp_resp_t *resp);   /* ESP_OK = got an HTTP response (see resp->status) */
void sp_http_drop_persistent(void);

/* ---- spotify_auth.c ---- */

typedef enum {
    SP_AUTH_OK = 0,
    SP_AUTH_NET,            /* temporary failure, try again later */
    SP_AUTH_REVOKED,        /* refresh token / code rejected: user must authorize again */
    SP_AUTH_NO_CLIENT_ID,
    SP_AUTH_NO_TOKEN,       /* never authorized */
} sp_auth_result_t;

esp_err_t sp_auth_init(const char *client_id, const char *redirect_uri);
bool sp_auth_has_client_id(void);
bool sp_auth_has_refresh_token(void);
/* Builds the https://accounts.spotify.com/authorize URL for the current PKCE session (stable until used). */
bool sp_auth_authorize_url(char *out, size_t out_len);
/* Takes the URL the browser ended up on (or a bare code), checks `state`, exchanges the code for tokens. */
sp_auth_result_t sp_auth_complete(const char *redirect_url_or_code, char *err_out, size_t err_len);
/* Copies a valid access token into out (refreshing first if it expires within 60 s). */
sp_auth_result_t sp_auth_access_token(char *out, size_t out_len, bool force_refresh);
void sp_auth_forget(void);

/* ---- spotify_art.c ---- */

/* Downloads `url` (a JPEG) and decodes it to RGB565. Returns a descriptor that stays valid until the call after next. */
esp_err_t sp_art_load(const char *url, const lv_image_dsc_t **out);
void sp_art_release_all(void);

/* ---- spotify_setup.c: on-device "link Spotify" web page ---- */

esp_err_t sp_setup_start(void);
void sp_setup_stop(void);
/* Set by the web handler after a successful link; polled by the Spotify task. */
bool sp_setup_take_linked_flag(void);

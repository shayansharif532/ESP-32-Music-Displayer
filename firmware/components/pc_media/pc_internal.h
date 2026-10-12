#pragma once
/* Internal interfaces of the pc_media component. */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"
#include "esp_http_client.h"
#include "lvgl.h"

/* ---- pc_http.c: one persistent plain-HTTP connection to the companion, response buffer in PSRAM ---- */

typedef struct {
    char  *data;        /* NUL-terminated */
    size_t len;
    size_t cap;
    size_t max;
    bool   truncated;
} pc_buf_t;

typedef struct {
    int      status;    /* HTTP status, 0 if the request failed */
    pc_buf_t body;
} pc_resp_t;

void pc_resp_init(pc_resp_t *r, size_t max_body);
void pc_resp_free(pc_resp_t *r);

/* host/port select the companion; path_query is e.g. "/now-playing". `post` = send a control POST. */
esp_err_t pc_http_request(const char *host, uint16_t port, const char *path_query, bool post, int timeout_ms, pc_resp_t *resp);
void pc_http_drop(void);        /* forget the kept-alive connection (after an error / host change) */

/* ---- pc_art.c ---- */

/* Decodes a JPEG into one of two fixed PSRAM RGB565 buffers and builds the matching blurred full-screen background. */
esp_err_t pc_art_decode(const uint8_t *jpeg, size_t len, const lv_image_dsc_t **out_art, const lv_image_dsc_t **out_bg);
/* Tell the background generator where the UI draws the cover (it bakes a soft shadow behind it). */
void pc_art_set_cover_rect(int x, int y, int w, int h);

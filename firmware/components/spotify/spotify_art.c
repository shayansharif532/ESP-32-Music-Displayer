/*
 * Album art: download a JPEG over HTTPS and decode it to RGB565 for LVGL.
 * Two fixed PSRAM output buffers are allocated once and used alternately (the one on screen is never overwritten);
 * the download buffer is reused and only ever grows. No allocation churn, so no heap fragmentation.
 */
#include <string.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "jpeg_decoder.h"

#include "spotify_internal.h"

static const char *TAG = "sp_art";

#define ART_MAX_DIM         320                              /* 300 px images decode 1:1, 640 px ones at 1:2 */
#define ART_BUF_BYTES       (ART_MAX_DIM * ART_MAX_DIM * 2)
#define ART_DOWNLOAD_MAX    (512 * 1024)

static struct {
    uint8_t *px[2];
    lv_image_dsc_t dsc[2];
    int next;                       /* slot to decode into */
    sp_resp_t download;
    bool inited;
} s;

static esp_err_t ensure_init(void)
{
    if (s.inited) {
        return ESP_OK;
    }
    for (int i = 0; i < 2; i++) {
        s.px[i] = heap_caps_aligned_alloc(64, ART_BUF_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (!s.px[i]) {
            ESP_LOGE(TAG, "no PSRAM for art buffer %d", i);
            return ESP_ERR_NO_MEM;
        }
    }
    sp_resp_init(&s.download, ART_DOWNLOAD_MAX);
    s.inited = true;
    return ESP_OK;
}

esp_err_t sp_art_load(const char *url, const lv_image_dsc_t **out)
{
    esp_err_t err = ensure_init();
    if (err != ESP_OK) {
        return err;
    }

    sp_req_t req = { .url = url, .method = HTTP_METHOD_GET, .timeout_ms = 15000 };
    err = sp_http_request(&req, &s.download);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "download failed: %s", esp_err_to_name(err));
        return err;
    }
    if (s.download.status != 200 || s.download.body.len < 128 || s.download.body.truncated) {
        ESP_LOGW(TAG, "download: HTTP %d, %u bytes%s", s.download.status, (unsigned)s.download.body.len,
                 s.download.body.truncated ? " (too large)" : "");
        return ESP_FAIL;
    }

    esp_jpeg_image_cfg_t cfg = {
        .indata = (uint8_t *)s.download.body.data,
        .indata_size = (uint32_t)s.download.body.len,
        .outbuf = s.px[s.next],
        .outbuf_size = ART_BUF_BYTES,
        .out_format = JPEG_IMAGE_FORMAT_RGB565,
        .out_scale = JPEG_IMAGE_SCALE_0,
    };
    esp_jpeg_image_output_t info = { 0 };
    for (int sc = JPEG_IMAGE_SCALE_0; sc <= JPEG_IMAGE_SCALE_1_8; sc++) {      /* shrink until it fits the buffer */
        cfg.out_scale = (esp_jpeg_image_scale_t)sc;
        if (esp_jpeg_get_image_info(&cfg, &info) != ESP_OK) {
            ESP_LOGW(TAG, "not a decodable JPEG (progressive or corrupt?)");
            return ESP_FAIL;
        }
        if (info.width <= ART_MAX_DIM && info.height <= ART_MAX_DIM && info.output_len <= ART_BUF_BYTES) {
            break;
        }
    }
    if (info.output_len > ART_BUF_BYTES) {
        ESP_LOGW(TAG, "image too large: %ux%u", info.width, info.height);
        return ESP_ERR_NO_MEM;
    }
    memset(&cfg.priv, 0, sizeof cfg.priv);
    err = esp_jpeg_decode(&cfg, &info);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "JPEG decode failed: %s", esp_err_to_name(err));
        return err;
    }

    lv_image_dsc_t *d = &s.dsc[s.next];
    memset(d, 0, sizeof *d);
    d->header.magic = LV_IMAGE_HEADER_MAGIC;
    d->header.cf = LV_COLOR_FORMAT_RGB565;
    d->header.w = info.width;
    d->header.h = info.height;
    d->header.stride = info.width * 2;
    d->data_size = info.width * info.height * 2;
    d->data = s.px[s.next];
    *out = d;
    s.next ^= 1;
    return ESP_OK;
}

void sp_art_release_all(void)
{
    /* Intentionally a no-op: the buffers are permanent by design (see file comment). */
}

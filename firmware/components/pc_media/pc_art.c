/*
 * Album art: JPEG -> RGB565 for LVGL, plus the artwork-driven screen background.
 *
 * Two fixed PSRAM buffer sets (art + background) are allocated once and used alternately, so the set on screen is never
 * overwritten and there is no allocation churn / heap fragmentation. The companion sends a baseline JPEG that already has the
 * size the cover is drawn at (a square, or a landscape card up to ART_MAX_W x ART_MAX_H); anything larger is shrunk by the
 * decoder (1/2, 1/4, 1/8) until it fits.
 *
 * Background ("blurred cover"): the cover is averaged down to a 40x40 colour grid, saturated a little, smoothed, then
 * interpolated up to the full 480x480 with smooth-step weights (no blocky/diamond artefacts), darkened with a vertical
 * gradient (darker towards the text area), given a soft drop shadow behind the cover rectangle, and ordered-dithered to
 * RGB565 so the dark gradient does not band. Everything is baked into ONE image, so LVGL only has to blit it.
 */
#include <math.h>
#include <string.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "jpeg_decoder.h"

#include "pc_internal.h"

static const char *TAG = "pc_art";

#define ART_MAX_W       432                                      /* largest cover the companion is asked for (landscape card) */
#define ART_MAX_H       320
#define ART_BUF_BYTES   (ART_MAX_W * ART_MAX_H * 2)              /* RGB565 result */
#define ART_RGB_BYTES   (ART_MAX_W * ART_MAX_H * 3)              /* RGB888 decode scratch (dithered down to RGB565) */

#define BG_W            480
#define BG_H            480
#define BG_BYTES        (BG_W * BG_H * 2)
#define GRID            40

/* look */
#define SAT_BOOST       1.40f       /* colourfulness of the blurred background */
#define DARK_TOP        0.52f       /* brightness factor at the top ... */
#define DARK_BOTTOM     0.30f       /* ... and at the bottom, where the text sits */
#define ADAPT_LUM       100.0f      /* mean luminance above which the background is darkened further */
#define SHADOW_RADIUS   46
#define SHADOW_STRENGTH 0.55f

typedef struct {                    /* scratch for make_background(), lives in PSRAM */
    uint32_t sum[GRID][GRID][3];
    uint32_t cnt[GRID][GRID];
    float cell[GRID][GRID][3];
    float tmp[GRID][GRID][3];
    uint8_t gxmap[ART_MAX_W];
    int16_t ix0[BG_W], ix1[BG_W];
    uint16_t wx[BG_W];
} bgwork_t;

static struct {
    bgwork_t *work;
    uint8_t *rgb;                   /* decoder output, R,G,B bytes */
    uint8_t *art_px[2];
    uint16_t *bg_px[2];
    lv_image_dsc_t art_dsc[2];
    lv_image_dsc_t bg_dsc[2];
    int next;                       /* slot to decode into */
    bool inited;
    struct { int x, y, w, h; } rect;        /* where the UI draws the cover (for the baked shadow) */
    pc_cover_area_fn area_fn;
} s = { .rect = { 84, 18, 312, 312 } };

void pc_art_set_cover_area_fn(pc_cover_area_fn fn)
{
    s.area_fn = fn;
}

static esp_err_t ensure_init(void)
{
    if (s.inited) {
        return ESP_OK;
    }
    for (int i = 0; i < 2; i++) {
        s.art_px[i] = heap_caps_aligned_alloc(64, ART_BUF_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        s.bg_px[i] = heap_caps_aligned_alloc(64, BG_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (!s.art_px[i] || !s.bg_px[i]) {
            ESP_LOGE(TAG, "no PSRAM for art/background buffers (slot %d)", i);
            return ESP_ERR_NO_MEM;
        }
    }
    s.rgb = heap_caps_aligned_alloc(64, ART_RGB_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    s.work = heap_caps_calloc(1, sizeof(bgwork_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!s.rgb) {
        ESP_LOGE(TAG, "no PSRAM for the decode scratch buffer");
        return ESP_ERR_NO_MEM;
    }
    if (!s.work) {
        ESP_LOGE(TAG, "no PSRAM for background scratch");
        return ESP_ERR_NO_MEM;
    }
    s.inited = true;
    return ESP_OK;
}

/* ---- background generation ------------------------------------------------------------------------ */

static inline int clampi(int v, int lo, int hi)
{
    return v < lo ? lo : (v > hi ? hi : v);
}

/* 4x4 Bayer matrix, values 0..15 */
static const uint8_t BAYER[4][4] = { { 0, 8, 2, 10 }, { 12, 4, 14, 6 }, { 3, 11, 1, 9 }, { 15, 7, 13, 5 } };

static void make_background(const uint8_t *src, int w, int h, uint16_t *dst)
{
    /* 1. average the cover down to a GRID x GRID colour grid */
    bgwork_t *wk = s.work;
    uint32_t (*sum)[GRID][3] = wk->sum;
    uint32_t (*cnt)[GRID] = wk->cnt;
    float (*cell)[GRID][3] = wk->cell;
    float (*tmp)[GRID][3] = wk->tmp;
    uint8_t *gxmap = wk->gxmap;
    memset(wk->sum, 0, sizeof wk->sum);
    memset(wk->cnt, 0, sizeof wk->cnt);
    for (int x = 0; x < w; x++) {
        gxmap[x] = (uint8_t)(x * GRID / w);
    }
    for (int y = 0; y < h; y++) {
        int gy = y * GRID / h;
        const uint8_t *row = src + (size_t)y * w * 3;
        for (int x = 0; x < w; x++) {
            int gx = gxmap[x];
            sum[gy][gx][0] += row[3 * x];
            sum[gy][gx][1] += row[3 * x + 1];
            sum[gy][gx][2] += row[3 * x + 2];
            cnt[gy][gx]++;
        }
    }
    for (int gy = 0; gy < GRID; gy++) {
        for (int gx = 0; gx < GRID; gx++) {
            float n = cnt[gy][gx] ? (float)cnt[gy][gx] : 1.0f;
            float r = sum[gy][gx][0] / n;
            float g = sum[gy][gx][1] / n;
            float b = sum[gy][gx][2] / n;
            float lum = 0.299f * r + 0.587f * g + 0.114f * b;     /* push colours away from grey */
            cell[gy][gx][0] = lum + (r - lum) * SAT_BOOST;
            cell[gy][gx][1] = lum + (g - lum) * SAT_BOOST;
            cell[gy][gx][2] = lum + (b - lum) * SAT_BOOST;
        }
    }
    /* 2. two passes of a 3x3 box blur on the grid (wide, soft colour fields) */
    for (int pass = 0; pass < 2; pass++) {
        for (int gy = 0; gy < GRID; gy++) {
            for (int gx = 0; gx < GRID; gx++) {
                float a0 = 0, a1 = 0, a2 = 0;
                for (int dy = -1; dy <= 1; dy++) {
                    for (int dx = -1; dx <= 1; dx++) {
                        int yy = clampi(gy + dy, 0, GRID - 1), xx = clampi(gx + dx, 0, GRID - 1);
                        a0 += cell[yy][xx][0];
                        a1 += cell[yy][xx][1];
                        a2 += cell[yy][xx][2];
                    }
                }
                tmp[gy][gx][0] = a0 / 9.0f;
                tmp[gy][gx][1] = a1 / 9.0f;
                tmp[gy][gx][2] = a2 / 9.0f;
            }
        }
        memcpy(wk->cell, wk->tmp, sizeof wk->cell);
    }

    /* bright covers get a darker background so the white text keeps its contrast */
    float mean_lum = 0.0f;
    for (int gy = 0; gy < GRID; gy++) {
        for (int gx = 0; gx < GRID; gx++) {
            mean_lum += 0.299f * cell[gy][gx][0] + 0.587f * cell[gy][gx][1] + 0.114f * cell[gy][gx][2];
        }
    }
    mean_lum /= (float)(GRID * GRID);
    const float adapt = mean_lum > ADAPT_LUM ? powf(ADAPT_LUM / mean_lum, 0.75f) : 1.0f;

    /* 3. horizontal / vertical interpolation tables (smooth-step weights, 0..256) */
    int16_t *ix0 = wk->ix0, *ix1 = wk->ix1;
    uint16_t *wx = wk->wx;
    for (int ox = 0; ox < BG_W; ox++) {
        float f = ((float)ox + 0.5f) * GRID / BG_W - 0.5f;
        int i0 = (int)floorf(f);
        float t = f - (float)i0;
        t = t * t * (3.0f - 2.0f * t);
        ix0[ox] = (int16_t)clampi(i0, 0, GRID - 1);
        ix1[ox] = (int16_t)clampi(i0 + 1, 0, GRID - 1);
        wx[ox] = (uint16_t)(t * 256.0f + 0.5f);
    }

    /* 4. soft shadow lookup: factor (0..256) by distance outside the (slightly lowered) cover rectangle */
    static uint16_t shadow_lut[SHADOW_RADIUS + 1];
    for (int d = 0; d <= SHADOW_RADIUS; d++) {
        float t = 1.0f - (float)d / SHADOW_RADIUS;
        shadow_lut[d] = (uint16_t)((1.0f - SHADOW_STRENGTH * t * t) * 256.0f);
    }
    const int rx0 = s.rect.x, rx1 = s.rect.x + s.rect.w - 1;
    const int ry0 = s.rect.y + 10, ry1 = s.rect.y + s.rect.h - 1 + 10;

    /* 5. render */
    static int32_t rowbuf[GRID][3];
    for (int oy = 0; oy < BG_H; oy++) {
        float f = ((float)oy + 0.5f) * GRID / BG_H - 0.5f;
        int i0 = (int)floorf(f);
        float t = f - (float)i0;
        t = t * t * (3.0f - 2.0f * t);
        int y0 = clampi(i0, 0, GRID - 1), y1 = clampi(i0 + 1, 0, GRID - 1);
        int wy = (int)(t * 256.0f + 0.5f);
        for (int gx = 0; gx < GRID; gx++) {
            for (int c = 0; c < 3; c++) {      /* 8.8 fixed point */
                rowbuf[gx][c] = (int32_t)(cell[y0][gx][c] * (256 - wy) + cell[y1][gx][c] * wy);
            }
        }
        int dark = (int)((DARK_TOP + (DARK_BOTTOM - DARK_TOP) * (float)oy / (BG_H - 1)) * adapt * 256.0f);
        int dy = oy < ry0 ? ry0 - oy : (oy > ry1 ? oy - ry1 : 0);
        uint16_t *out = dst + (size_t)oy * BG_W;
        for (int ox = 0; ox < BG_W; ox++) {
            int a = ix0[ox], b = ix1[ox], wxv = wx[ox];
            int k = dark;
            if (dy < SHADOW_RADIUS) {          /* near the cover: apply the shadow */
                int dxs = ox < rx0 ? rx0 - ox : (ox > rx1 ? ox - rx1 : 0);
                if (dxs < SHADOW_RADIUS) {
                    int d = (dxs == 0 || dy == 0) ? (dxs + dy) : (int)(sqrtf((float)(dxs * dxs + dy * dy)) + 0.5f);
                    if (d <= SHADOW_RADIUS) {
                        k = (k * shadow_lut[d]) >> 8;
                    }
                }
            }
            int bay = BAYER[oy & 3][ox & 3] * 2 - 15;          /* -15..+15 */
            int v[3];
            for (int c = 0; c < 3; c++) {
                int32_t m = (rowbuf[a][c] * (256 - wxv) + rowbuf[b][c] * wxv) >> 8;    /* 8.8 * 256 >> 8 -> value*256 (0..65280) */
                m = (int32_t)(((int64_t)m * k) >> 8);
                v[c] = (int)m;
            }
            /* dither before quantising: step is 2048 (5 bit) / 1024 (6 bit) in 8.8 units */
            int r = clampi(v[0] + bay * 64, 0, 65535) >> 11;
            int g = clampi(v[1] + bay * 32, 0, 65535) >> 10;
            int bl = clampi(v[2] + bay * 64, 0, 65535) >> 11;
            out[ox] = (uint16_t)((r << 11) | (g << 5) | bl);
        }
    }
}

/* RGB888 -> RGB565 with a 4x4 ordered dither, so smooth gradients in the cover do not show contour bands. */
static void rgb888_to_565_dithered(const uint8_t *src, int w, int h, uint16_t *dst)
{
    for (int y = 0; y < h; y++) {
        const uint8_t *p = src + (size_t)y * w * 3;
        uint16_t *out = dst + (size_t)y * w;
        for (int x = 0; x < w; x++, p += 3) {
            int d = BAYER[y & 3][x & 3] * 2 - 15;            /* -15..+15 */
            int r = clampi(p[0] + ((d * 8) >> 5), 0, 255) >> 3;
            int g = clampi(p[1] + ((d * 4) >> 5), 0, 255) >> 2;
            int b = clampi(p[2] + ((d * 8) >> 5), 0, 255) >> 3;
            out[x] = (uint16_t)((r << 11) | (g << 5) | b);
        }
    }
}

/* ---- public ------------------------------------------------------------------------------------------ */

esp_err_t pc_art_decode(const uint8_t *jpeg, size_t len, const lv_image_dsc_t **out_art, const lv_image_dsc_t **out_bg)
{
    esp_err_t err = ensure_init();
    if (err != ESP_OK) {
        return err;
    }
    esp_jpeg_image_cfg_t cfg = {
        .indata = (uint8_t *)jpeg,
        .indata_size = (uint32_t)len,
        .outbuf = s.rgb,
        .outbuf_size = ART_RGB_BYTES,
        .out_format = JPEG_IMAGE_FORMAT_RGB888,
        .out_scale = JPEG_IMAGE_SCALE_0,
    };
    esp_jpeg_image_output_t info = { 0 };
    for (int sc = JPEG_IMAGE_SCALE_0; sc <= JPEG_IMAGE_SCALE_1_8; sc++) {      /* shrink until it fits the buffer */
        cfg.out_scale = (esp_jpeg_image_scale_t)sc;
        if (esp_jpeg_get_image_info(&cfg, &info) != ESP_OK) {
            ESP_LOGW(TAG, "not a decodable JPEG (progressive or corrupt?)");
            return ESP_FAIL;
        }
        if (info.width <= ART_MAX_W && info.height <= ART_MAX_H && info.output_len <= ART_RGB_BYTES) {
            break;
        }
    }
    if (info.output_len > ART_RGB_BYTES) {
        ESP_LOGW(TAG, "image too large: %ux%u", info.width, info.height);
        return ESP_ERR_NO_MEM;
    }
    memset(&cfg.priv, 0, sizeof cfg.priv);
    err = esp_jpeg_decode(&cfg, &info);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "JPEG decode failed: %s", esp_err_to_name(err));
        return err;
    }

    if (s.area_fn) {                                    /* the UI decides where a cover of this shape is drawn */
        lv_area_t a = s.area_fn((int)info.width, (int)info.height);
        s.rect.x = a.x1;
        s.rect.y = a.y1;
        s.rect.w = a.x2 - a.x1 + 1;
        s.rect.h = a.y2 - a.y1 + 1;
    }
    rgb888_to_565_dithered(s.rgb, info.width, info.height, (uint16_t *)s.art_px[s.next]);
    make_background(s.rgb, info.width, info.height, s.bg_px[s.next]);

    lv_image_dsc_t *d = &s.art_dsc[s.next];
    memset(d, 0, sizeof *d);
    d->header.magic = LV_IMAGE_HEADER_MAGIC;
    d->header.cf = LV_COLOR_FORMAT_RGB565;
    d->header.w = info.width;
    d->header.h = info.height;
    d->header.stride = info.width * 2;
    d->data_size = info.width * info.height * 2;
    d->data = s.art_px[s.next];

    lv_image_dsc_t *b = &s.bg_dsc[s.next];
    memset(b, 0, sizeof *b);
    b->header.magic = LV_IMAGE_HEADER_MAGIC;
    b->header.cf = LV_COLOR_FORMAT_RGB565;
    b->header.w = BG_W;
    b->header.h = BG_H;
    b->header.stride = BG_W * 2;
    b->data_size = BG_BYTES;
    b->data = (const uint8_t *)s.bg_px[s.next];

    *out_art = d;
    *out_bg = b;
    s.next ^= 1;
    return ESP_OK;
}

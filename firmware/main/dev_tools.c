/*
 * Development-only helpers, compiled only with  idf.py -DAPP_DEV_TOOLS=1 build  (never in the product build).
 *
 * "screenshot": renders the LVGL screen into RAM and sends it over the USB serial console as base64 lines
 *     SHOT_BEGIN <w> <h> <stride> <format>
 *     S<index> <base64 of 192 raw bytes>      (raw RGB565, little endian)
 *     SHOT_END <crc32-of-raw-bytes>
 * tools/screenshot.py turns that into a PNG, so the real display content can be inspected from the PC.
 * "tap_play" / "tap_next" / "tap_prev" / "tap_art": press the on-screen control (same path as a finger).
 * Both are triggered by the companion's /debug route (companion started with --dev-tools).
 */
#ifdef APP_DEV_TOOLS

#include <stdio.h>
#include <string.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_rom_crc.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "mbedtls/base64.h"

#include "bsp/esp-bsp.h"
#include "dev_tools.h"
#include "lvgl.h"
#include "ui.h"

static const char *TAG = "dev_tools";

#define CHUNK 192

static void shot_task(void *arg)
{
    (void)arg;
    if (!bsp_display_lock(5000)) {
        ESP_LOGW(TAG, "screenshot: could not take the LVGL lock");
        vTaskDelete(NULL);
    }
    lv_draw_buf_t *buf = lv_snapshot_take(lv_screen_active(), LV_COLOR_FORMAT_RGB565);
    bsp_display_unlock();
    if (!buf) {
        ESP_LOGW(TAG, "screenshot: snapshot failed (out of memory?)");
        vTaskDelete(NULL);
    }
    const uint8_t *raw = buf->data;
    const uint32_t w = buf->header.w, h = buf->header.h, stride = buf->header.stride;
    const size_t total = (size_t)stride * h;

    uint32_t crc = esp_rom_crc32_le(0, raw, total);
    esp_log_level_set("*", ESP_LOG_NONE);                  /* no log lines in the middle of the picture */
    vTaskDelay(pdMS_TO_TICKS(30));
    printf("SHOT_BEGIN %u %u %u %u\n", (unsigned)w, (unsigned)h, (unsigned)stride, (unsigned)buf->header.cf);
    char line[CHUNK / 3 * 4 + 8];
    unsigned idx = 0;
    for (size_t off = 0; off < total; off += CHUNK, idx++) {
        size_t n = total - off < CHUNK ? total - off : CHUNK;
        size_t outlen = 0;
        mbedtls_base64_encode((unsigned char *)line, sizeof line, &outlen, raw + off, n);
        line[outlen] = '\0';
        printf("S%u %s\n", idx, line);
        if ((idx & 1) == 1) {
            vTaskDelay(1);                                  /* let the USB host drain its FIFO */
        }
    }
    printf("SHOT_END %u\n", (unsigned)crc);
    fflush(stdout);
    esp_log_level_set("*", ESP_LOG_INFO);
    lv_draw_buf_destroy(buf);
    ESP_LOGI(TAG, "screenshot sent: %ux%u, %u bytes", (unsigned)w, (unsigned)h, (unsigned)total);
    vTaskDelete(NULL);
}

void dev_tools_handle(const char *what)
{
    if (strcmp(what, "screenshot") == 0) {
        xTaskCreatePinnedToCore(shot_task, "shot", 6144, NULL, 2, NULL, 0);
    } else if (strncmp(what, "tap_", 4) == 0) {
        ESP_LOGI(TAG, "debug tap: %s", what + 4);
        ui_debug_tap(what + 4);
    }
}

#endif /* APP_DEV_TOOLS */

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
#include "side_keys.h"
#include "lvgl.h"
#include "ui.h"

static const char *TAG = "dev_tools";

#define CHUNK 192

static void send_screenshot(void)
{
    if (!bsp_display_lock(5000)) {
        ESP_LOGW(TAG, "screenshot: could not take the LVGL lock");
        return;
    }
    lv_draw_buf_t *buf = lv_snapshot_take(lv_screen_active(), LV_COLOR_FORMAT_RGB565);
    bsp_display_unlock();
    if (!buf) {
        ESP_LOGW(TAG, "screenshot: snapshot failed (out of memory?)");
        return;
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
}

static void shot_task(void *arg)
{
    (void)arg;
    send_screenshot();
    vTaskDelete(NULL);
}

/* ---- scripted finger / key actions: a virtual pointer device walks the real LVGL input path ----------------- */

static void finger_to(int x, int y, bool down)
{
    ui_debug_touch(x, y, down);
    vTaskDelay(pdMS_TO_TICKS(35));                      /* about one LVGL input-read period */
}

static bool s_shot_mid;                                 /* "<action>_shot": photograph the screen while the effect is visible */

static void drag(int x0, int y0, int x1, int y1, int steps)
{
    finger_to(x0, y0, true);
    vTaskDelay(pdMS_TO_TICKS(60));
    for (int i = 1; i <= steps; i++) {
        finger_to(x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps, true);
        if (s_shot_mid && i == steps / 2 + 2) {         /* shortly after the swipe fired: the feedback animation is running */
            send_screenshot();
        }
    }
    vTaskDelay(pdMS_TO_TICKS(60));
    finger_to(x1, y1, false);
    vTaskDelay(pdMS_TO_TICKS(120));
}

static void tap(int x, int y)
{
    finger_to(x, y, true);
    vTaskDelay(pdMS_TO_TICKS(90));
    finger_to(x, y, false);
    vTaskDelay(pdMS_TO_TICKS(120));
}

static char s_action[24];
static volatile bool s_busy;

static void action_task(void *arg)
{
    (void)arg;
    char name[24];
    strlcpy(name, s_action, sizeof name);
    char *suffix = strstr(name, "_shot");
    s_shot_mid = suffix != NULL;
    if (suffix) {
        *suffix = '\0';
    }
    const char *a = name;
    ESP_LOGI(TAG, "debug action: %s%s", a, s_shot_mid ? " (+ screenshot)" : "");
    if (strcmp(a, "swipe_right") == 0)      drag(150, 200, 350, 200, 8);     /* on the cover: -> next */
    else if (strcmp(a, "swipe_left") == 0)  drag(350, 200, 150, 200, 8);     /* -> previous */
    else if (strcmp(a, "swipe_up") == 0)    drag(240, 330, 240, 120, 8);     /* vertical: nothing */
    else if (strcmp(a, "swipe_diag") == 0)  drag(150, 150, 330, 330, 8);     /* 45 degrees: nothing */
    else if (strcmp(a, "swipe_bar") == 0)   drag(120, 406, 330, 406, 8);     /* starts on the progress bar: seeks, never changes track */
    else if (strcmp(a, "swipe_text") == 0)  drag(110, 352, 370, 352, 8);     /* starts on the text: still a swipe -> next */
    else if (strcmp(a, "touch_cover") == 0) tap(240, 200);                   /* a real tap on the cover: full-screen toggle */
    else if (strcmp(a, "touch_play") == 0)  tap(240, 446);                   /* a real tap on play / pause */
    else if (strcmp(a, "key_up") == 0 || strcmp(a, "key_down") == 0) {       /* as if the upper / lower side button was pressed */
        app_side_key(a[4] == 'u' ? SIDE_KEY_UP : SIDE_KEY_DOWN);
        if (s_shot_mid) {
            vTaskDelay(pdMS_TO_TICKS(350));              /* the PC answers in well under that; the pill is up for ~1 s */
            send_screenshot();
        }
    }
    else if (strncmp(a, "overlay_", 8) == 0) {                                  /* volume pill texts, no media involved */
        if (strcmp(a, "overlay_none") == 0)       ui_show_volume(-1, false);
        else if (strcmp(a, "overlay_pc") == 0)    ui_show_volume(-2, false);
        else if (strcmp(a, "overlay_muted") == 0) ui_show_volume(40, true);
        else                                      ui_show_volume(65, false);
        if (s_shot_mid) {
            vTaskDelay(pdMS_TO_TICKS(250));
            send_screenshot();
        }
    }
    else if (strcmp(a, "keyhold_up") == 0 || strcmp(a, "keyhold_down") == 0) {   /* a key held for 3 s: press, then repeats as the driver does */
        side_key_t k = a[8] == 'u' ? SIDE_KEY_UP : SIDE_KEY_DOWN;
        app_side_key(k);
        vTaskDelay(pdMS_TO_TICKS(SIDE_KEYS_REPEAT_AFTER_MS));
        for (int t = SIDE_KEYS_REPEAT_AFTER_MS; t < SIDE_KEYS_HOLD_MAX_MS; t += SIDE_KEYS_REPEAT_EVERY_MS) {
            app_side_key(k);
            vTaskDelay(pdMS_TO_TICKS(SIDE_KEYS_REPEAT_EVERY_MS));
        }
    }
    s_shot_mid = false;
    s_busy = false;
    vTaskDelete(NULL);
}

void dev_tools_handle(const char *what)
{
    if (strcmp(what, "screenshot") == 0) {
        xTaskCreatePinnedToCore(shot_task, "shot", 6144, NULL, 2, NULL, 0);
    } else if (strncmp(what, "tap_", 4) == 0) {
        ESP_LOGI(TAG, "debug tap: %s", what + 4);
        ui_debug_tap(what + 4);
    } else if (!s_busy && (strncmp(what, "swipe_", 6) == 0 || strncmp(what, "touch_", 6) == 0 || strncmp(what, "key_", 4) == 0 || strncmp(what, "keyhold_", 8) == 0 ||
                         strncmp(what, "overlay_", 8) == 0)) {
        s_busy = true;
        strlcpy(s_action, what, sizeof s_action);
        xTaskCreatePinnedToCore(action_task, "dev_act", 6144, NULL, 2, NULL, 0);
    }
}

#endif /* APP_DEV_TOOLS */

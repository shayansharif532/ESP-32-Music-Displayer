/*
 * Development-only helpers, compiled only with  idf.py -DAPP_DEV_TOOLS=1 build  (never in the product build).
 *
 * "screenshot": renders the LVGL screen into RAM and sends it over the USB link (as LOG frames; the companion hands them to the test tool) as base64 lines
 *     SHOT_BEGIN <w> <h> <stride> <format>
 *     S<index> <base64 of 192 raw bytes>      (raw RGB565, little endian)
 *     SHOT_END <crc32-of-raw-bytes>
 * tools/screenshot.py turns that into a PNG, so the real display content can be inspected from the PC.
 * "tap_play" / "tap_next" / "tap_prev" / "tap_art": press the on-screen control (same path as a finger).
 * "pair_accept" / "pair_cancel": press Pair / Cancel on the pairing prompt (the function the buttons call); "pair_shot": screenshot
 * of the pairing screen once its fade-in is over. (While the display is unpaired the companion's one-shot request rides on its /hello
 * and /pair/status answers: pc_media reads it from there in development builds.)
 * All are triggered by the companion's /debug route (companion started with --dev-tools).
 */
#ifdef APP_DEV_TOOLS

#include <stdarg.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_rom_crc.h"
#include "esp_rom_sys.h"
#include "esp_system.h"
#include "hal/usb_serial_jtag_ll.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "mbedtls/base64.h"

#include "bsp/esp-bsp.h"
#include "cJSON.h"
#include "dev_tools.h"
#include "dmlink.h"
#include "idle_date.h"
#include "idle_screen.h"
#include "side_keys.h"
#include "lvgl.h"
#include "app_config.h"
#include "ui.h"
#include "usb_link.h"

static const char *TAG = "dev_tools";

#define CHUNK 192

/* One text line to the test tool, through the USB link (the native port carries frames only, so printf cannot be used). Unlike a log
 * line it waits for the host instead of being dropped: a missing line would ruin the picture. */
static void out(const char *fmt, ...)
{
    char line[CHUNK / 3 * 4 + 40];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(line, sizeof line, fmt, ap);
    va_end(ap);
    usb_link_log_line(line);
}

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
    out("SHOT_BEGIN %u %u %u %u\n", (unsigned)w, (unsigned)h, (unsigned)stride, (unsigned)buf->header.cf);
    char line[CHUNK / 3 * 4 + 8];
    unsigned idx = 0;
    for (size_t off = 0; off < total; off += CHUNK, idx++) {
        size_t n = total - off < CHUNK ? total - off : CHUNK;
        size_t outlen = 0;
        mbedtls_base64_encode((unsigned char *)line, sizeof line, &outlen, raw + off, n);
        line[outlen] = '\0';
        out("S%u %s\n", idx, line);
    }
    out("SHOT_END %u\n", (unsigned)crc);
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

/* "idle_cfg {json}": show the idle screen with exactly this configuration and clock, no companion involved:
 *   {"clock_ms":1791849600000,"theme":"neon","accent":"#FF4FA3","clock":true,"clock_24h":false,"date":true,"date_fmt":1,"text":"Hello"}
 * (the keys of the companion's GET /idle; clock_ms = the PC's local time as milliseconds). It is stored like a real sync (the companion
 * replaces it at its next settings version), so tests that use it end by letting the companion send its own settings. */
static void idle_cfg_task(void *arg)
{
    char *json = arg;
    cJSON *j = cJSON_Parse(json);
    if (j) {
        const cJSON *ms = cJSON_GetObjectItemCaseSensitive(j, "clock_ms");
        cJSON_DeleteItemFromObjectCaseSensitive(j, "version");
        cJSON_DeleteItemFromObjectCaseSensitive(j, "ok");
        cJSON_AddBoolToObject(j, "ok", true);
        cJSON_AddNumberToObject(j, "version", 99999);
        char *cfg = cJSON_PrintUnformatted(j);
        if (cfg) {
            if (cJSON_IsNumber(ms)) {
                idle_screen_set_clock((int64_t)ms->valuedouble);
            }
            const int rc = idle_screen_apply_config(cfg, strlen(cfg));
            ESP_LOGI(TAG, "idle_cfg applied (rc %d)", rc);
            if (bsp_display_lock(2000)) {
                idle_screen_activate(true);
                lv_screen_load(idle_screen_obj());
                bsp_display_unlock();
            }
            cJSON_free(cfg);
        }
        cJSON_Delete(j);
    } else {
        ESP_LOGW(TAG, "idle_cfg: the JSON could not be parsed");
    }
    free(json);
    vTaskDelete(NULL);
}

static void pair_shot_task(void *arg)
{
    (void)arg;
    vTaskDelay(pdMS_TO_TICKS(500));                        /* the prompt fades in over 350 ms: let it finish */
    send_screenshot();
    vTaskDelete(NULL);
}

/* "link_0" .. "link_6": shows one onboarding screen (ui_link_kind_t, in that order) and photographs it, for reviewing the wording and the layout */
static void link_shot_task(void *arg)
{
    ui_link_show((ui_link_kind_t)(intptr_t)arg, DM_SETUP_URL, DM_SETUP_URL_SHORT, "OFFICE-PC");
    vTaskDelay(pdMS_TO_TICKS(700));
    send_screenshot();
    vTaskDelete(NULL);
}

/* "usb_replug_<ms>": what pulling the USB cable and plugging it back in does to this board, which the cable also powers: the PC sees the device
 * vanish (the display lets go of the USB data lines), and after <ms> the board is reset the way a power-up resets it (a SYSTEM reset: unlike the
 * CPU reset of esp_restart() it also resets the USB-Serial/JTAG peripheral, so the device enumerates again from scratch). For testing the companion's
 * re-detection and the display's start-up. NOTE: letting go of the data lines without that reset leaves the port detached until the cable is
 * physically re-plugged: do not "just detach". */
static void replug_task(void *arg)
{
    const int ms = (int)(intptr_t)arg;
    ESP_LOGW(TAG, "debug: USB replug in %d ms", ms);
    vTaskDelay(pdMS_TO_TICKS(150));                         /* the log line leaves first */
    const usb_serial_jtag_pull_override_vals_t off = { .dp_pu = false, .dp_pd = false, .dm_pu = false, .dm_pd = false };
    usb_serial_jtag_ll_phy_enable_pull_override(&off);
    vTaskDelay(pdMS_TO_TICKS(ms));
    esp_rom_software_reset_system();
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
    if (strcmp(a, "swipe_right") == 0)      drag(150, 200, 350, 200, 8);     /* on the cover: finger moves right -> previous track */
    else if (strcmp(a, "swipe_left") == 0)  drag(350, 200, 150, 200, 8);     /* finger moves left -> next track */
    else if (strcmp(a, "swipe_up") == 0 || strcmp(a, "swipe_down") == 0) {   /* vertical: next / previous media SOURCE (selection only) */
        const bool up = a[6] == 'u';
        const bool shot = s_shot_mid;
        s_shot_mid = false;                                                   /* the pill appears after the PC answered: shoot after the drag */
        if (up) drag(240, 330, 240, 120, 8);
        else    drag(240, 120, 240, 330, 8);
        if (shot) {
            vTaskDelay(pdMS_TO_TICKS(250));
            send_screenshot();
        }
    }
    else if (strcmp(a, "swipe_diag") == 0)  drag(150, 150, 330, 330, 8);     /* 45 degrees: nothing */
    else if (strcmp(a, "swipe_bar") == 0)   drag(120, 406, 330, 406, 8);     /* starts on the progress bar: seeks, never changes track */
    else if (strcmp(a, "swipe_bar_v") == 0) drag(240, 406, 240, 200, 8);     /* vertical from the progress bar: seeks, never changes source */
    else if (strcmp(a, "swipe_text") == 0)  drag(110, 352, 370, 352, 8);     /* starts on the text: still a swipe -> previous */
    else if (strcmp(a, "touch_cover") == 0) tap(240, 200);                   /* a real tap on the cover: full-screen toggle */
    else if (strcmp(a, "touch_play") == 0)  tap(240, 446);                   /* a real tap on play / pause */
    else if (strcmp(a, "touch_shuffle") == 0) tap(62, 446);                  /* Spotify only: the small shuffle button */
    else if (strcmp(a, "touch_repeat") == 0)  tap(418, 446);                 /* Spotify only: the small repeat button */
    else if (strcmp(a, "touch_pair") == 0)       tap(345, 306);              /* the pairing prompt's Pair button (a real tap through the input path) */
    else if (strcmp(a, "touch_paircancel") == 0) tap(135, 306);              /* ... and its Cancel button */
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
    } else if (strcmp(what, "selftest") == 0) {                 /* the on-target self-tests of the pure code, reported in the log */
        const char *a = dm_selftest(), *b = idle_date_selftest();
        /* cJSON must refuse deep nesting (a peer's "[[[[..." would otherwise eat the task stack) and still read the shapes this firmware uses */
        static const char shallow[] = "{\"a\":{\"b\":[1,{\"c\":2}]}}";
        char deep[48];
        memset(deep, '[', sizeof deep);
        cJSON *dj = cJSON_ParseWithLength(deep, sizeof deep), *sj = cJSON_ParseWithLength(shallow, sizeof shallow - 1);
        const char *c = dj ? "deep nesting accepted" : (sj ? NULL : "ordinary nesting refused");
        cJSON_Delete(dj);
        cJSON_Delete(sj);
        ESP_LOGI(TAG, "selftest dmlink: %s | idle date: %s | json nesting: %s", a ? a : "ok", b ? b : "ok", c ? c : "ok");
    } else if (strncmp(what, "idle_cfg ", 9) == 0) {
        char *copy = strdup(what + 9);
        if (copy && xTaskCreatePinnedToCore(idle_cfg_task, "idle_cfg", 6144, copy, 2, NULL, 0) != pdPASS) {
            free(copy);
        }
    } else if (strcmp(what, "reboot") == 0) {                   /* a software reset: the native USB port stays enumerated (tests of "after a board reboot") */
        ESP_LOGW(TAG, "debug reboot");
        vTaskDelay(pdMS_TO_TICKS(150));                          /* the log line leaves first */
        esp_restart();
    } else if (strncmp(what, "link_", 5) == 0 && what[5] >= '0' && what[5] <= '6' && what[6] == '\0') {
        xTaskCreatePinnedToCore(link_shot_task, "shot", 6144, (void *)(intptr_t)(what[5] - '0'), 2, NULL, 0);
    } else if (strncmp(what, "usb_replug_", 11) == 0 && atoi(what + 11) >= 200 && atoi(what + 11) <= 60000) {
        xTaskCreatePinnedToCore(replug_task, "replug", 4096, (void *)(intptr_t)atoi(what + 11), 2, NULL, 0);
    } else if (strcmp(what, "pair_shot") == 0) {
        xTaskCreatePinnedToCore(pair_shot_task, "shot", 6144, NULL, 2, NULL, 0);
    } else if (strcmp(what, "pair_accept") == 0 || strcmp(what, "pair_cancel") == 0) {
        ESP_LOGI(TAG, "debug action: %s", what);
        ui_pairing_debug_press(strcmp(what, "pair_accept") == 0);
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

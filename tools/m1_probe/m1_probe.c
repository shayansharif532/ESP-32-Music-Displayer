/*
 * Milestone-1 diagnostic sidecar (NOT part of Waveshare's demo, NOT part of the product).
 *
 * Attaches itself to the running official demo without touching its sources:
 *   - waits until the BSP has created the touch input device,
 *   - traces press / drag / release coordinates on the serial console,
 *   - draws a dot + "x,y" label on the LVGL system layer that follows the finger,
 *     so touch orientation / corner coverage can be checked by eye,
 *   - every 5 s logs LVGL refresh count and heap headroom (internal RAM and PSRAM).
 *
 * The component is removed once our own UI replaces the demo (Milestone 2).
 */
#include <inttypes.h>
#include <stdbool.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lvgl.h"

#include "bsp/esp-bsp.h"

static const char *TAG = "m1_probe";

#define SCREEN_W      480
#define SCREEN_H      480
#define DOT_SIZE      34
#define LABEL_W       104
#define LABEL_H       26
#define MOVE_LOG_MS   100   /* rate limit for drag logging */
#define STATS_MS      5000

static lv_indev_t *s_indev;
static lv_obj_t *s_dot;
static lv_obj_t *s_label;
static bool s_down;
static int32_t s_last_x;
static int32_t s_last_y;
static uint32_t s_last_move_log;
static uint32_t s_press_count;
static uint32_t s_redraws;        /* only touched from the LVGL task */
static uint32_t s_redraws_seen;

static void on_render_ready(lv_event_t *e)
{
    LV_UNUSED(e);
    s_redraws++;
}

static void show_marker(int32_t x, int32_t y)
{
    lv_obj_set_pos(s_dot, x - DOT_SIZE / 2, y - DOT_SIZE / 2);
    lv_label_set_text_fmt(s_label, "%d,%d", (int)x, (int)y);

    int32_t lx = x + DOT_SIZE / 2 + 6;
    int32_t ly = y + DOT_SIZE / 2 + 6;
    if (lx + LABEL_W > SCREEN_W) {
        lx = x - DOT_SIZE / 2 - 6 - LABEL_W;
    }
    if (ly + LABEL_H > SCREEN_H) {
        ly = y - DOT_SIZE / 2 - 6 - LABEL_H;
    }
    lv_obj_set_pos(s_label, lx, ly);

    lv_obj_remove_flag(s_dot, LV_OBJ_FLAG_HIDDEN);
    lv_obj_remove_flag(s_label, LV_OBJ_FLAG_HIDDEN);
}

static void hide_marker(void)
{
    lv_obj_add_flag(s_dot, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(s_label, LV_OBJ_FLAG_HIDDEN);
}

static void touch_timer_cb(lv_timer_t *t)
{
    LV_UNUSED(t);
    lv_point_t p;
    lv_indev_get_point(s_indev, &p);
    const uint32_t now = lv_tick_get();

    if (lv_indev_get_state(s_indev) == LV_INDEV_STATE_PRESSED) {
        s_last_x = p.x;
        s_last_y = p.y;
        if (!s_down) {
            s_down = true;
            s_press_count++;
            s_last_move_log = now;
            ESP_LOGI(TAG, "touch DOWN  x=%3d y=%3d   (press #%" PRIu32 ")", (int)p.x, (int)p.y, s_press_count);
        } else if (now - s_last_move_log >= MOVE_LOG_MS) {
            s_last_move_log = now;
            ESP_LOGI(TAG, "touch MOVE  x=%3d y=%3d", (int)p.x, (int)p.y);
        }
        show_marker(p.x, p.y);
    } else if (s_down) {
        s_down = false;
        ESP_LOGI(TAG, "touch UP    x=%3d y=%3d", (int)s_last_x, (int)s_last_y);
        hide_marker();
    }
}

static void stats_timer_cb(lv_timer_t *t)
{
    LV_UNUSED(t);
    const uint32_t delta = s_redraws - s_redraws_seen;
    s_redraws_seen = s_redraws;
    ESP_LOGI(TAG, "stats: LVGL redraw cycles in last %d s = %" PRIu32 " | internal RAM free %u B (largest block %u B) | PSRAM free %u B (largest block %u B)",
             STATS_MS / 1000, delta,
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_SPIRAM));
}

/* Must be called with the LVGL lock held. */
static void start_overlay(lv_indev_t *indev)
{
    s_indev = indev;
    lv_display_t *disp = lv_display_get_default();

    lv_obj_t *layer = lv_layer_sys();

    s_dot = lv_obj_create(layer);
    lv_obj_remove_style_all(s_dot);
    lv_obj_set_size(s_dot, DOT_SIZE, DOT_SIZE);
    lv_obj_set_style_radius(s_dot, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(s_dot, lv_color_hex(0xFFD400), 0);
    lv_obj_set_style_bg_opa(s_dot, LV_OPA_70, 0);
    lv_obj_set_style_border_width(s_dot, 3, 0);
    lv_obj_set_style_border_color(s_dot, lv_color_hex(0xFFFFFF), 0);
    lv_obj_remove_flag(s_dot, LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(s_dot, LV_OBJ_FLAG_HIDDEN);

    s_label = lv_label_create(layer);
    lv_obj_set_width(s_label, LABEL_W);
    lv_obj_set_style_text_align(s_label, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_color(s_label, lv_color_hex(0xFFFFFF), 0);
    lv_obj_set_style_bg_color(s_label, lv_color_hex(0x000000), 0);
    lv_obj_set_style_bg_opa(s_label, LV_OPA_70, 0);
    lv_obj_set_style_radius(s_label, 6, 0);
    lv_obj_set_style_pad_all(s_label, 4, 0);
    lv_obj_remove_flag(s_label, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_flag(s_label, LV_OBJ_FLAG_HIDDEN);

    lv_display_add_event_cb(disp, on_render_ready, LV_EVENT_RENDER_READY, NULL);   /* only fires when something was redrawn */
    lv_timer_create(touch_timer_cb, 15, NULL);
    lv_timer_create(stats_timer_cb, STATS_MS, NULL);

    ESP_LOGI(TAG, "overlay active: display %dx%d, touch dot + serial trace enabled",
             (int)lv_display_get_horizontal_resolution(disp), (int)lv_display_get_vertical_resolution(disp));
}

static void probe_task(void *arg)
{
    LV_UNUSED(arg);
    lv_indev_t *indev = NULL;
    for (int i = 0; i < 600 && indev == NULL; i++) {   /* wait up to 60 s for bsp_display_start() */
        indev = bsp_display_get_input_dev();
        if (indev == NULL) {
            vTaskDelay(pdMS_TO_TICKS(100));
        }
    }
    if (indev == NULL) {
        ESP_LOGW(TAG, "no touch input device appeared within 60 s; probe not started");
        vTaskDelete(NULL);
        return;
    }
    vTaskDelay(pdMS_TO_TICKS(500));   /* let app_main finish building its screen first */
    bsp_display_lock(0);
    start_overlay(indev);
    bsp_display_unlock();
    vTaskDelete(NULL);
}

/* Runs inside main_task right before app_main(); linked in via WHOLE_ARCHIVE. */
__attribute__((constructor)) static void m1_probe_ctor(void)
{
    xTaskCreate(probe_task, "m1_probe", 4096, NULL, 3, NULL);
}

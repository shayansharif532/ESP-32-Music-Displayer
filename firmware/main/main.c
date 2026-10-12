/*
 * Spotify desk display - application entry point.
 *
 * Milestone 2 state: custom 480x480 Now Playing UI driven by a local demo player (no network yet).
 * The display/LVGL start-up is owned here (instead of the stock bsp_display_start()) so the LVGL task
 * gets a larger stack and is pinned away from the Wi-Fi core; see docs/milestones/M1-lcd-touch.md.
 */
#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "nvs_flash.h"

#include "bsp/esp-bsp.h"
#include "ui.h"

#include "health.h"

static const char *TAG = "app";

/* LVGL runs on core 1, leaving core 0 to Wi-Fi/lwIP and our network tasks. */
#define LVGL_TASK_STACK_BYTES   (24 * 1024)
#define LVGL_TASK_PRIORITY      4
#define LVGL_TASK_CORE          1

/* ------------------------------------------------------------------------------------------------ */
/* Local demo player (Milestone 2): exercises the UI exactly the way the Spotify task will later.    */
/* ------------------------------------------------------------------------------------------------ */

typedef struct {
    ui_cmd_t cmd;
    uint32_t arg;
} demo_evt_t;

static QueueHandle_t s_demo_q;

static void demo_on_ui_cmd(ui_cmd_t cmd, uint32_t arg, void *ctx)
{
    (void)ctx;
    demo_evt_t e = { cmd, arg };
    xQueueSend(s_demo_q, &e, 0);                    /* LVGL task: never block here */
}

static const struct {
    const char *title, *artist;
    uint32_t duration_ms;
} DEMO_TRACKS[] = {
    { "Midnight City Lights", "The Neon Drivers", 214000 },
    { "A Very Long Track Title That Has To Scroll Across The Screen", "An Artist With A Long Name feat. Someone Else", 187000 },
    { "Short", "Solo", 95000 },
};
#define DEMO_COUNT (sizeof DEMO_TRACKS / sizeof DEMO_TRACKS[0])

static void demo_task(void *arg)
{
    (void)arg;
    size_t idx = 0;
    uint32_t pos = 0;
    bool playing = true;

    ui_show_track(DEMO_TRACKS[idx].title, DEMO_TRACKS[idx].artist, DEMO_TRACKS[idx].duration_ms);
    ui_set_progress(pos, playing);

    for (;;) {
        demo_evt_t e;
        bool changed_track = false;
        bool changed_pos = false;

        if (xQueueReceive(s_demo_q, &e, pdMS_TO_TICKS(1000)) == pdTRUE) {
            switch (e.cmd) {
            case UI_CMD_NEXT:
                idx = (idx + 1) % DEMO_COUNT;
                pos = 0;
                changed_track = true;
                break;
            case UI_CMD_PREV:
                if (pos > 3000) {
                    pos = 0;                         /* like Spotify: first restart the track */
                    changed_pos = true;
                } else {
                    idx = (idx + DEMO_COUNT - 1) % DEMO_COUNT;
                    pos = 0;
                    changed_track = true;
                }
                break;
            case UI_CMD_PLAY_PAUSE:
                playing = !playing;
                changed_pos = true;
                break;
            case UI_CMD_SEEK:
                pos = e.arg;
                changed_pos = true;
                break;
            }
        } else if (playing) {
            pos += 1000;
            if (pos >= DEMO_TRACKS[idx].duration_ms) {
                idx = (idx + 1) % DEMO_COUNT;
                pos = 0;
                changed_track = true;
            }
        }

        if (changed_track) {
            ui_show_track(DEMO_TRACKS[idx].title, DEMO_TRACKS[idx].artist, DEMO_TRACKS[idx].duration_ms);
        }
        if (changed_track || changed_pos) {
            ui_set_progress(pos, playing);
        }
    }
}

/* ------------------------------------------------------------------------------------------------ */

void app_main(void)
{
    ESP_LOGI(TAG, "spotify-desk-display (milestone 2: UI) starting");

    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());           /* app's own NVS partition only; never a chip erase */
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    bsp_display_cfg_t cfg = {
        .lvgl_port_cfg = ESP_LVGL_PORT_INIT_CONFIG(),
    };
    cfg.lvgl_port_cfg.task_stack = LVGL_TASK_STACK_BYTES;
    cfg.lvgl_port_cfg.task_priority = LVGL_TASK_PRIORITY;
    cfg.lvgl_port_cfg.task_affinity = LVGL_TASK_CORE;

    lv_display_t *disp = bsp_display_start_with_config(&cfg);
    ESP_ERROR_CHECK(disp ? ESP_OK : ESP_FAIL);
    bsp_display_backlight_on();

    s_demo_q = xQueueCreate(8, sizeof(demo_evt_t));
    ui_init(demo_on_ui_cmd, NULL);

    xTaskCreatePinnedToCore(demo_task, "demo", 4096, NULL, 3, NULL, 0);
    health_start();                                    /* periodic heap / stack / touch log line */
}

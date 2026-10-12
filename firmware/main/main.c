/*
 * Spotify desk display - application entry point and glue.
 *
 *   wifi_mgr  -> network + clock            spotify -> auth, playback polling, commands, album art
 *   ui        -> LVGL Now Playing screen    this file -> turns Spotify/Wi-Fi events into UI calls and UI taps into commands
 *
 * Without developer credentials in firmware/secrets.local.h the firmware runs the UI-only demo player instead.
 * The display/LVGL start-up is owned here (instead of the stock bsp_display_start()) so the LVGL task gets a
 * larger stack and is pinned away from the Wi-Fi core; see docs/milestones/M1-lcd-touch.md.
 */
#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"

#include "bsp/esp-bsp.h"

#include "app_config.h"
#include "demo_player.h"
#include "health.h"
#include "net_selftest.h"
#include "spotify.h"
#include "ui.h"
#include "wifi_mgr.h"

static const char *TAG = "app";

/* ---- UI taps -> Spotify commands (runs on the LVGL task: only queues, never blocks) -------------- */

static void on_ui_cmd(ui_cmd_t cmd, uint32_t arg, void *ctx)
{
    (void)ctx;
    switch (cmd) {
    case UI_CMD_PREV:       spotify_command(SPOTIFY_CMD_PREV, 0); break;
    case UI_CMD_PLAY_PAUSE: spotify_command(SPOTIFY_CMD_PLAY_PAUSE, 0); break;
    case UI_CMD_NEXT:       spotify_command(SPOTIFY_CMD_NEXT, 0); break;
    case UI_CMD_SEEK:       spotify_command(SPOTIFY_CMD_SEEK, arg); break;
    }
}

/* ---- Spotify events -> UI (called from the Spotify task) ------------------------------------------ */

static char s_shown_track[40];       /* track id currently on screen, "" = idle screen */
static bool s_idle_shown;

static void on_playback(const spotify_playback_t *pb, void *ctx)
{
    (void)ctx;
    if (pb->has_item) {
        if (strcmp(pb->track_id, s_shown_track) != 0 || s_idle_shown) {
            ui_show_track(pb->title, pb->artist, pb->duration_ms);
            strlcpy(s_shown_track, pb->track_id, sizeof s_shown_track);
            s_idle_shown = false;
        }
        ui_set_progress(pb->progress_ms, pb->is_playing);
    } else if (!s_idle_shown) {
        s_shown_track[0] = '\0';
        s_idle_shown = true;
        ui_show_idle("Nothing playing", "Start Spotify on any device");
    }
}

static void on_art(const lv_image_dsc_t *dsc, void *ctx)
{
    (void)ctx;
    ui_set_art(dsc);
}

static void idle_message(const char *l1, const char *l2)
{
    s_shown_track[0] = '\0';
    s_idle_shown = true;
    ui_show_idle(l1, l2);
}

static void on_status(spotify_status_t st, const char *detail, void *ctx)
{
    (void)ctx;
    switch (st) {
    case SPOTIFY_STATUS_STARTING:
        break;
    case SPOTIFY_STATUS_WAITING_NETWORK:
        ui_set_status(UI_STATUS_INFO, strcmp(detail, "clock") == 0 ? "Setting the clock..." : "Connecting to Wi-Fi...");
        if (!s_shown_track[0]) {
            idle_message("Connecting...", "");
        }
        break;
    case SPOTIFY_STATUS_NO_CLIENT_ID:
        ui_set_status(UI_STATUS_WARN, "Setup needed");
        idle_message("Spotify not set up", "This build has no Spotify Client ID");
        break;
    case SPOTIFY_STATUS_NEED_AUTH: {
        char l2[96];
        snprintf(l2, sizeof l2, "On your phone or PC open  %s", detail);
        ui_set_status(UI_STATUS_WARN, "Spotify is not linked yet");
        idle_message("Link your Spotify", l2);
        break;
    }
    case SPOTIFY_STATUS_AUTH_ERROR:
        ui_set_status(UI_STATUS_ERROR, detail[0] ? detail : "Spotify authorization problem");
        break;
    case SPOTIFY_STATUS_OK:
        ui_set_status(UI_STATUS_INFO, "");
        break;
    case SPOTIFY_STATUS_NO_DEVICE:
        ui_set_status(UI_STATUS_INFO, detail[0] ? detail : "No active Spotify device");
        break;
    case SPOTIFY_STATUS_NET_ERROR:
        ui_set_status(UI_STATUS_WARN, detail[0] ? detail : "Network problem - retrying");
        break;
    case SPOTIFY_STATUS_RATE_LIMITED:
        ui_set_status(UI_STATUS_WARN, detail);
        break;
    case SPOTIFY_STATUS_PREMIUM_REQUIRED:
        ui_set_status(UI_STATUS_ERROR, detail[0] ? detail : "Playback control needs Spotify Premium");
        break;
    }
}

/* ---- Wi-Fi state -> UI ------------------------------------------------------------------------------ */

static void on_wifi_state(wifi_mgr_state_t st, void *ctx)
{
    (void)ctx;
    /* Called from the Wi-Fi event task / a timer: only log here. The Spotify task shows network state on screen. */
    ESP_LOGI(TAG, "wifi state -> %d", (int)st);
}

#ifdef APP_WIFI_SELFTEST
/* Milestone 3 check: pull the link once, 60 s after it first came up, and watch the log for the recovery. */
static void wifi_selftest_task(void *arg)
{
    (void)arg;
    while (!wifi_mgr_wait_connected(1000)) {}
    vTaskDelay(pdMS_TO_TICKS(60000));
    int64_t t0 = esp_timer_get_time();
    wifi_mgr_debug_drop_link();
    vTaskDelay(pdMS_TO_TICKS(500));
    if (wifi_mgr_wait_connected(60000)) {
        ESP_LOGI(TAG, "SELFTEST wifi reconnect: recovered in %d ms", (int)((esp_timer_get_time() - t0) / 1000));
    } else {
        ESP_LOGE(TAG, "SELFTEST wifi reconnect: NOT recovered within 60 s");
    }
    vTaskDelete(NULL);
}
#endif

/* ------------------------------------------------------------------------------------------------------ */

/* noipa: keep the "no credentials built in" decision at run time so the network code is always compiled AND linked. */
__attribute__((noipa)) static const char *dev_wifi_ssid(void)
{
    return DEV_WIFI_SSID;
}

void app_main(void)
{
    const bool network_mode = dev_wifi_ssid()[0] != '\0';
    ESP_LOGI(TAG, "spotify-desk-display starting (%s)", network_mode ? "network mode" : "UI demo mode, no credentials built in");

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

    health_start();                                    /* periodic heap / stack / touch log line */

    if (!network_mode) {
        ui_init(demo_player_on_ui_cmd, NULL);
        demo_player_start();
        return;
    }

    ui_init(on_ui_cmd, NULL);
    ui_show_idle("Starting...", "");

    ESP_ERROR_CHECK(wifi_mgr_init(on_wifi_state, NULL));
    ESP_ERROR_CHECK(wifi_mgr_connect(DEV_WIFI_SSID, DEV_WIFI_PASSWORD));
    net_selftest_start();

    const spotify_config_t sp = {
        .client_id = DEV_SPOTIFY_CLIENT_ID,
        .redirect_uri = SPOTIFY_REDIRECT_URI,
        .cbs = { .on_playback = on_playback, .on_art = on_art, .on_status = on_status, .ctx = NULL },
    };
    ESP_ERROR_CHECK(spotify_start(&sp));

#ifdef APP_WIFI_SELFTEST
    xTaskCreatePinnedToCore(wifi_selftest_task, "wifi_selftest", 4096, NULL, 2, NULL, 0);
#endif
}

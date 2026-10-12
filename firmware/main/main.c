/*
 * Desk media display - application entry point and glue.
 *
 *   wifi_mgr  -> network                      pc_media -> finds the Windows companion, polls now-playing, artwork, controls
 *   ui        -> LVGL Now Playing screen      this file -> turns media/Wi-Fi events into UI calls and UI taps into commands
 *
 * Without developer credentials in firmware/secrets.local.h the firmware runs the UI-only demo player instead.
 * The display/LVGL start-up is owned here (instead of the stock bsp_display_start()) so the LVGL task gets a
 * larger stack and is pinned away from the Wi-Fi core; see docs/milestones/M1-lcd-touch.md.
 */
#include <inttypes.h>
#include <stdio.h>
#include <string.h>
#include <strings.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"

#include "bsp/esp-bsp.h"

#include "app_config.h"
#include "demo_player.h"
#include "dev_tools.h"
#include "health.h"
#include "pc_media.h"
#include "side_keys.h"
#include "ui.h"
#include "wifi_mgr.h"

static const char *TAG = "app";

#define VOLUME_STEP_PERCENT     5

/* ---- UI taps -> companion commands (runs on the LVGL task: only queues, never blocks) ----------------- */

static void on_ui_cmd(ui_cmd_t cmd, uint32_t arg, void *ctx)
{
    (void)ctx;
    switch (cmd) {
    case UI_CMD_PREV:       pc_media_command(PC_MEDIA_CMD_PREV, 0); break;
    case UI_CMD_PLAY_PAUSE: pc_media_command(PC_MEDIA_CMD_PLAY_PAUSE, 0); break;
    case UI_CMD_NEXT:       pc_media_command(PC_MEDIA_CMD_NEXT, 0); break;
    case UI_CMD_SEEK:       pc_media_command(PC_MEDIA_CMD_SEEK, arg); break;
    case UI_CMD_SOURCE_NEXT: pc_media_command(PC_MEDIA_CMD_SOURCE_NEXT, 0); break;
    case UI_CMD_SOURCE_PREV: pc_media_command(PC_MEDIA_CMD_SOURCE_PREV, 0); break;
    }
}

/* ---- side buttons -> per-app volume (called from the side_keys task; only queues a command) ---------------- */

void app_side_key(side_key_t key)
{
    pc_media_command(key == SIDE_KEY_UP ? PC_MEDIA_CMD_VOLUME_UP : PC_MEDIA_CMD_VOLUME_DOWN, VOLUME_STEP_PERCENT);
}

static void on_side_key(side_key_t key, void *ctx)
{
    (void)ctx;
    app_side_key(key);
}

static void on_volume(int percent, bool muted, void *ctx)
{
    (void)ctx;
    ui_show_volume(percent, muted);
}

static void on_source(const char *name, int index, int count, void *ctx)
{
    (void)ctx;
    ui_show_source(name, index, count);
}

/* ---- companion events -> UI (called from the pc_media poll task) ----------------------------------------- */

static char s_shown_track[16];       /* track id currently on screen, "" = idle screen */
static bool s_idle_shown;
static char s_source[32];            /* friendly name of the app that is playing, shown in the status line */

static bool ci_contains(const char *hay, const char *needle)
{
    size_t n = strlen(needle);
    for (; *hay; hay++) {
        size_t i = 0;
        while (i < n && hay[i] && ((hay[i] | 0x20) == (needle[i] | 0x20))) {
            i++;
        }
        if (i == n) {
            return true;
        }
    }
    return false;
}

static void friendly_source(const char *app, char *out, size_t n)
{
    static const struct { const char *needle, *name; } MAP[] = {
        { "spotify", "Spotify" }, { "chrome", "Chrome" }, { "msedge", "Edge" }, { "edge", "Edge" }, { "firefox", "Firefox" },
        { "brave", "Brave" }, { "opera", "Opera" }, { "vlc", "VLC" }, { "applemusic", "Apple Music" }, { "itunes", "Apple Music" },
        { "zunemusic", "Media Player" }, { "wmplayer", "Media Player" }, { "youtube", "YouTube" },
    };
    for (size_t i = 0; i < sizeof MAP / sizeof MAP[0]; i++) {
        if (ci_contains(app, MAP[i].needle)) {
            strlcpy(out, MAP[i].name, n);
            return;
        }
    }
    strlcpy(out, app, n);
    char *dot = strrchr(out, '.');
    if (dot && strcasecmp(dot, ".exe") == 0) {
        *dot = '\0';
    }
}

static void idle_message(const char *l1, const char *l2)
{
    s_shown_track[0] = '\0';
    s_idle_shown = true;
    ui_show_idle(l1, l2);
}

static void on_state(const pc_media_state_t *st, void *ctx)
{
    (void)ctx;
    if (!st->has_session) {
        if (!s_idle_shown) {
            idle_message("Nothing playing", "Play something on your PC");
            s_source[0] = '\0';
        }
        return;
    }
    ui_set_sources(st->sources);
    if (strcmp(st->track_id, s_shown_track) != 0 || s_idle_shown) {
        char src[sizeof s_source];
        friendly_source(st->source[0] ? st->source : st->app, src, sizeof src);
        strlcpy(s_source, src, sizeof s_source);
        const char *title = st->title[0] ? st->title : s_source;
        const char *artist = st->artist[0] ? st->artist : st->album;
        ui_show_track(title, artist, st->duration_ms);
        strlcpy(s_shown_track, st->track_id, sizeof s_shown_track);
        s_idle_shown = false;
    }
    ui_set_progress(st->position_ms, st->playing);
    ui_set_capabilities(st->can_prev, st->can_next, st->can_seek);
}

static void on_art(const lv_image_dsc_t *art, const lv_image_dsc_t *bg, void *ctx)
{
    (void)ctx;
    ui_set_art(art, bg);
}

#ifdef APP_DEV_TOOLS
static void on_debug(const char *what, void *ctx)
{
    (void)ctx;
    dev_tools_handle(what);
}
#endif

static void on_status(pc_media_status_t st, const char *detail, void *ctx)
{
    (void)ctx;
    switch (st) {
    case PC_MEDIA_WAITING_NETWORK:
        ui_set_status(UI_STATUS_INFO, "Connecting to Wi-Fi...");
        if (!s_shown_track[0]) {
            idle_message("Connecting...", "");
        }
        break;
    case PC_MEDIA_SEARCHING:
        ui_set_status(UI_STATUS_INFO, "Looking for your PC...");
        if (!s_shown_track[0]) {
            idle_message("Looking for your PC", "Is Desk Media Companion running on it?");
        }
        break;
    case PC_MEDIA_CONNECTED: {                         /* detail = the Windows app id of the session */
        char src[sizeof s_source];
        friendly_source(detail, src, sizeof src);
        ui_set_status(UI_STATUS_INFO, src);
        break;
    }
    case PC_MEDIA_NO_SESSION:
        ui_set_status(UI_STATUS_INFO, "");
        break;
    case PC_MEDIA_UNREACHABLE:
        ui_set_status(UI_STATUS_WARN, "Can't reach your PC");
        if (!s_shown_track[0]) {
            idle_message("Can't reach your PC", "Is it on, and is Desk Media Companion running?");
        }
        break;
    }
}

/* ---- Wi-Fi state ------------------------------------------------------------------------------------- */

static void on_wifi_state(wifi_mgr_state_t st, void *ctx)
{
    (void)ctx;
    /* Called from the Wi-Fi event task / a timer: only log here. pc_media shows network state on screen. */
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
    ESP_LOGI(TAG, "desk media display starting (%s)", network_mode ? "network mode" : "UI demo mode, no Wi-Fi credentials built in");

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

    const pc_media_config_t pc = {
        .static_host = DEV_COMPANION_HOST,
        .port = 0,
        .art_max_w = UI_ART_MAX_W,
        .art_max_h = UI_ART_SIZE,
        .cover_area = ui_art_area,
        .cbs = {
            .on_state = on_state, .on_art = on_art, .on_status = on_status, .on_volume = on_volume, .on_source = on_source,
#ifdef APP_DEV_TOOLS
            .on_debug = on_debug,
#endif
            .ctx = NULL,
        },
    };
    ESP_ERROR_CHECK(pc_media_start(&pc));
    if (side_keys_start(on_side_key, NULL) != ESP_OK) {            /* the two side buttons = volume up / down */
        ESP_LOGW(TAG, "side keys not available");
    }

#ifdef APP_WIFI_SELFTEST
    xTaskCreatePinnedToCore(wifi_selftest_task, "wifi_selftest", 4096, NULL, 2, NULL, 0);
#endif
}

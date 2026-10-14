/*
 * DeskMedia Display - application entry point and glue.
 *
 *   usb_link  -> the USB cable to the PC      pc_media -> finds the Windows companion, polls now-playing, artwork, controls
 *   ui        -> LVGL Now Playing screen      this file -> turns media / link events into UI calls and UI taps into commands
 *   wifi_mgr  -> optional LAN fallback (only when credentials are compiled in for development; the product runs with Wi-Fi off)
 *
 * The display/LVGL start-up is owned here (instead of the stock bsp_display_start()) so the LVGL task gets a
 * larger stack and is pinned away from the network core; see docs/development-history/M1-lcd-touch.md.
 */
#include <inttypes.h>
#include <stdio.h>
#include <string.h>
#include <strings.h>

#include "esp_app_desc.h"
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
#include "idle_screen.h"
#include "pc_media.h"
#include "side_keys.h"
#include "ui.h"
#include "wifi_mgr.h"

static const char *TAG = "app";

#define VOLUME_STEP_PERCENT     5

static bool s_idle_visible;          /* the idle screen is (becoming) the visible one (defined with the screen logic below) */
static void peek_arm(void);

/* ---- UI taps -> companion commands (runs on the LVGL task: only queues, never blocks) ----------------- */

static void on_ui_cmd(ui_cmd_t cmd, uint32_t arg, void *ctx)
{
    (void)ctx;
    switch (cmd) {
    case UI_CMD_PREV:       pc_media_command(PC_MEDIA_CMD_PREV, 0); break;
    case UI_CMD_PLAY_PAUSE: pc_media_command(PC_MEDIA_CMD_PLAY_PAUSE, 0); break;
    case UI_CMD_NEXT:       pc_media_command(PC_MEDIA_CMD_NEXT, 0); break;
    case UI_CMD_SEEK:       pc_media_command(PC_MEDIA_CMD_SEEK, arg); break;
    case UI_CMD_SHUFFLE:    pc_media_command(PC_MEDIA_CMD_SHUFFLE, 0); break;
    case UI_CMD_REPEAT:     pc_media_command(PC_MEDIA_CMD_REPEAT, 0); break;
    case UI_CMD_SOURCE_NEXT:
    case UI_CMD_SOURCE_PREV:                                 /* arg 1 = "the display shows the idle screen" (it is an item of the ring) */
        peek_arm();                                          /* swiping from the idle screen onto a paused session must not bounce back to idle */
        pc_media_command(cmd == UI_CMD_SOURCE_NEXT ? PC_MEDIA_CMD_SOURCE_NEXT : PC_MEDIA_CMD_SOURCE_PREV, s_idle_visible ? 1 : 0);
        break;
    }
}

/* ---- side buttons -> per-app volume (called from the side_keys task; only queues a command) ---------------- */

void app_side_key(side_key_t key)
{
    if (s_idle_visible) {
        return;              /* the idle screen shows no app: a volume key must not change an unrelated app's volume (ADR-008) */
    }
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
    ui_show_source_on(s_idle_visible ? idle_screen_obj() : NULL, name, index, count);   /* over the idle screen when that is the new source */
}

/* ---- companion events -> UI (called from the pc_media poll task) ----------------------------------------- */

static char s_shown_track[16];       /* track id currently on screen, "" = the player shows a message instead of a track */
static bool s_idle_shown;            /* ... such a message: "Starting...", "Looking for your PC", "Nothing playing" */
static bool s_link_idle;             /* ... and it is a connection message (Wi-Fi / PC), not "nothing playing" */
static char s_source[32];            /* friendly name of the app that is playing, shown in the status line */

/* ---- player <-> idle screen --------------------------------------------------------------------------------
 * Two LVGL screens. Rules (all decided here from what the companion reports):
 *   PC not reachable / Wi-Fi / searching      -> the player screen with its connection message (never the idle screen)
 *   PC connected, something plays             -> the player, at once (<= one poll)
 *   PC connected, nothing plays               -> the idle screen, after the user's delay (default 4 s) so a gap between two
 *                                                tracks does not flash it; at once when nothing has played since connecting
 * Tapping the idle screen while a (paused) session exists brings the player back for PEEK_HOLD_MS (so a track paused from the
 * display can be resumed); touching the player keeps it up the same way. */
#define SCREEN_FADE_MS      350
#define PEEK_HOLD_MS        30000

static lv_obj_t *s_player_scr;
static bool s_have_session;          /* the PC has a (maybe paused) media session */
static bool s_have_session_or_idle;  /* a companion has answered since boot (the idle screen is meaningful) */
static bool s_idle_selected;         /* the user swiped onto the idle screen (the companion's MANUAL_TEMP selection "idle") */
static volatile uint32_t s_leave_idle_until;   /* (uint32 ms) after a tap that left the idle selection: ignore the (stale) selection report until then */
static int64_t s_last_playing_ms;    /* when something last played; 0 = not since connecting */
static volatile uint32_t s_peek_until;   /* (uint32 ms) the player stays up until then even though nothing plays; 0 = no peek */
static uint32_t s_last_touch;

static int64_t now_ms(void)
{
    return esp_timer_get_time() / 1000;
}

static void show_screen(bool idle)
{
    if (!s_player_scr || idle == s_idle_visible || !bsp_display_lock(1000)) {
        return;
    }
    s_idle_visible = idle;
    idle_screen_activate(idle);
    if (ui_pairing_defer(idle ? idle_screen_obj() : s_player_scr)) {   /* the pairing prompt keeps the display; it loads this screen when dismissed */
        bsp_display_unlock();
        return;
    }
    lv_screen_load_anim(idle ? idle_screen_obj() : s_player_scr, LV_SCREEN_LOAD_ANIM_FADE_IN, SCREEN_FADE_MS, 0, false);
    bsp_display_unlock();
    ESP_LOGI(TAG, "screen -> %s", idle ? "idle" : "player");
}

static void peek_arm(void)
{
    uint32_t t = (uint32_t)now_ms() + PEEK_HOLD_MS;
    s_peek_until = t ? t : 1;
}

/* LVGL task: the idle screen was tapped. */
static void on_idle_tap(void *ctx)
{
    (void)ctx;
    if (ui_touch_moved()) {
        return;                                            /* a sloppy swipe, not a tap */
    }
    if (s_idle_selected) {                                 /* the idle screen was chosen as a source: a tap leaves that choice (back to automatic) */
        s_idle_selected = false;
        s_leave_idle_until = (uint32_t)now_ms() + 1500;
        peek_arm();
        pc_media_command(PC_MEDIA_CMD_SOURCE_AUTO, 0);
        if (s_have_session) {
            show_screen(false);
        }
        return;
    }
    if (s_have_session) {
        peek_arm();
        show_screen(false);
    }
}

/* From the latest report of the PC: should the idle screen be showing? (Also keeps the timers.) */
static bool idle_wanted(const pc_media_state_t *st)
{
    const int64_t now = now_ms();
    const uint32_t touches = ui_touch_count();
    const bool touched = touches != s_last_touch;
    s_last_touch = touches;
    if (st->any_playing) {
        s_last_playing_ms = now;
    }
    if (st->idle_selected && (int32_t)(s_leave_idle_until - (uint32_t)now) <= 0) {
        return true;                                       /* chosen in the source ring: shown whatever plays, no delay */
    }
    if (st->any_playing) {
        s_peek_until = 0;
        return false;
    }
    if (touched && !s_idle_visible) {
        peek_arm();                                        /* somebody is using the player: leave it up */
    }
    if (s_peek_until && (int32_t)(s_peek_until - (uint32_t)now) > 0) {
        return false;
    }
    return s_last_playing_ms == 0 || now - s_last_playing_ms >= (int64_t)idle_screen_delay_ms();
}

static bool on_idle_have(uint32_t version, void *ctx)
{
    (void)ctx;
    return idle_screen_have_version(version);
}

static int on_idle_config(const char *json, size_t len, void *ctx)
{
    (void)ctx;
    return idle_screen_apply_config(json, len);
}

static bool on_idle_picture(const uint8_t *jpeg, size_t len, void *ctx)
{
    (void)ctx;
    return idle_screen_apply_picture(jpeg, len);
}

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
    s_link_idle = false;
    ui_show_idle(l1, l2);
}

/* A connection message (Wi-Fi / looking for the PC / PC lost): on the player screen, which is brought forward if the idle screen was up. */
static void link_message(const char *l1, const char *l2)
{
    idle_message(l1, l2);
    s_link_idle = true;
    s_last_playing_ms = 0;
    s_peek_until = 0;
    show_screen(false);
}

/* The connection / onboarding overlay (ui_link.c): the player screen is brought forward and covered by it. */
static void link_overlay(ui_link_kind_t kind, const char *pc_name)
{
    s_shown_track[0] = '\0';
    s_idle_shown = true;
    s_link_idle = true;
    s_last_playing_ms = 0;
    s_peek_until = 0;
    ui_show_idle("", "");                                    /* nothing of a previous track shows through */
    ui_link_show(kind, DM_SETUP_URL, DM_SETUP_URL_SHORT, pc_name);
    show_screen(false);
}

/* The idle screen may stay up through a short reconnect (the companion restarting): its clock and date keep running, and the status
 * line says what is going on. Only a longer outage brings the overlay (pc_media decides when). */
static bool reconnecting_on_idle(void)
{
    if (!s_idle_visible || !s_have_session_or_idle) {
        return false;
    }
    idle_screen_set_status("Reconnecting to your PC...");
    return true;
}

static void on_state(const pc_media_state_t *st, void *ctx)
{
    (void)ctx;
    ui_link_hide();                                          /* talking to the companion again: no connection message */
    s_have_session_or_idle = true;
    idle_screen_set_clock(st->clock_ms);
    s_have_session = st->has_session;
    s_idle_selected = st->idle_selected && (int32_t)(s_leave_idle_until - (uint32_t)now_ms()) <= 0;
    const bool want_idle = idle_wanted(st);
    if (!st->has_session) {
        if (want_idle) {
            if (!s_idle_shown || s_link_idle) {              /* reset the player to its "nothing playing" placeholder under the idle screen */
                idle_message("Nothing playing", "Play something on your PC");
                s_source[0] = '\0';
            }
            ui_set_sources(st->sources);                     /* (the idle message reset it) the ring: just the idle item, nothing to swipe to */
            idle_screen_set_status(s_idle_selected ? "Idle screen" : "Nothing playing");
            show_screen(true);
        }                                                    /* else: a short gap - keep what is on screen until the delay is over */
        return;
    }
    s_link_idle = false;
    ui_set_sources(st->sources);
    ui_set_spotify_controls(st->spotify, st->can_shuffle, st->can_repeat, st->shuffle, st->repeat);   /* Spotify only */
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
    idle_screen_set_status(s_idle_selected ? (st->any_playing ? "Playing in the background - tap for the player" : "Idle screen - tap for the player")
                                           : "Paused - tap to show the player");
    show_screen(want_idle);                                  /* something plays -> player (at once); nothing for a while -> idle */
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
    case PC_MEDIA_WAITING_NETWORK:                         /* (only with a compiled-in Wi-Fi fallback) */
        ui_set_status(UI_STATUS_INFO, "Connecting to Wi-Fi...");
        ui_link_hide();
        if (!s_shown_track[0]) {
            link_message("Connecting...", "");
        } else {
            show_screen(false);
        }
        break;
    case PC_MEDIA_SEARCHING:
        ui_set_status(UI_STATUS_INFO, "Looking for your PC...");
        ui_link_hide();
        if (!s_shown_track[0]) {
            link_message("Looking for your PC", "Is DeskMedia Companion running on it?");
        } else {
            show_screen(false);
        }
        break;
    case PC_MEDIA_CONNECTED: {                         /* detail = the Windows app id of the session */
        ui_link_hide();
        char src[sizeof s_source];
        friendly_source(detail, src, sizeof src);
        ui_set_status(UI_STATUS_INFO, src);
        break;
    }
    case PC_MEDIA_NO_SESSION:
        ui_link_hide();
        ui_set_status(UI_STATUS_INFO, "");
        break;
    case PC_MEDIA_UNREACHABLE:
        ui_set_status(UI_STATUS_WARN, "Can't reach your PC");
        if (reconnecting_on_idle()) {
            break;                                           /* the idle screen (clock, date) stays up while it reconnects */
        }
        if (!s_shown_track[0]) {
            link_message("Can't reach your PC", "Is it on, and is DeskMedia Companion running?");
        } else {
            s_last_playing_ms = 0;
            s_peek_until = 0;
            show_screen(false);                              /* the warning belongs on the player, never on the idle screen */
        }
        break;
    case PC_MEDIA_NEEDS_PAIRING:                           /* the PC answers, this display holds no key (only /hello is polled) */
        ui_set_status(UI_STATUS_INFO, "Not paired");
        link_overlay(UI_LINK_PAIRING_WAIT, detail);
        break;
    case PC_MEDIA_PAIRING:                                 /* the code prompt is up (ui_pairing owns the screen) */
        break;
    case PC_MEDIA_PAIRING_LOST:                            /* the PC no longer accepts the stored key */
        ui_set_status(UI_STATUS_WARN, "Pairing lost");
        link_overlay(UI_LINK_PAIRING_LOST, detail);
        break;
    case PC_MEDIA_NO_USB_DATA:                             /* powered but no data: charge-only cable, charger, hub without data */
        ui_set_status(UI_STATUS_WARN, "No USB data");
        link_overlay(UI_LINK_NO_USB_DATA, NULL);
        break;
    case PC_MEDIA_USB_CONNECTED:                           /* data is there, the companion has not answered (first seconds / restarting) */
        ui_set_status(UI_STATUS_INFO, "USB connected");
        if (!reconnecting_on_idle()) {
            link_overlay(UI_LINK_USB_CONNECTED, NULL);
        }
        break;
    case PC_MEDIA_COMPANION_MISSING:                       /* not installed, or not running */
        ui_set_status(UI_STATUS_WARN, "DeskMedia Companion not found");
        link_overlay(UI_LINK_COMPANION_MISSING, NULL);
        break;
    case PC_MEDIA_INCOMPATIBLE:
        ui_set_status(UI_STATUS_WARN, "Update needed");
        link_overlay(detail && strcmp(detail, "companion") == 0 ? UI_LINK_UPDATE_COMPANION : UI_LINK_UPDATE_FIRMWARE, NULL);
        break;
    }
}

/* ---- pairing prompt: pc_media (poll task) shows / hides it, the user's tap (LVGL task) comes back as an answer ---- */

static void on_pair_answer(bool accepted, void *ctx)
{
    (void)ctx;
    pc_media_pair_answer(accepted);                        /* only posts: an atomic flag + a task notification */
}

static void on_pair_prompt(const char *pc_name, const char *code, void *ctx)
{
    (void)ctx;
    if (code) {
        ui_pairing_show(pc_name, code, on_pair_answer, NULL);
    } else {
        ui_pairing_hide();
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

/* noipa: keep the "are Wi-Fi credentials compiled in" decision at run time so the network code is always compiled AND linked. */
__attribute__((noipa)) static const char *dev_wifi_ssid(void)
{
    return DEV_WIFI_SSID;
}

void app_main(void)
{
    const bool wifi_configured = dev_wifi_ssid()[0] != '\0';          /* development builds only: the product has no Wi-Fi credentials */
    ESP_LOGI(TAG, "DeskMedia Display %s starting (USB%s)", esp_app_get_description()->version, wifi_configured ? " + Wi-Fi fallback" : ", Wi-Fi off");

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

#ifdef APP_UI_DEMO
    ui_init(demo_player_on_ui_cmd, NULL);              /* UI development: a scripted fake player, no PC needed */
    demo_player_start();
    return;
#endif

    ui_init(on_ui_cmd, NULL);
    ui_show_idle("Starting...", "");
    if (bsp_display_lock(0)) {
        s_player_scr = lv_screen_active();                 /* the player screen; the idle screen is a second one (idle_screen.c) */
        bsp_display_unlock();
    }
    idle_screen_init(on_idle_tap, NULL);
    ui_attach_vertical_swipes(idle_screen_obj());          /* swipe up / down on the idle screen walks the source ring too */

    if (wifi_configured) {                                 /* optional LAN fallback (development builds) */
        ESP_ERROR_CHECK(wifi_mgr_init(on_wifi_state, NULL));
        ESP_ERROR_CHECK(wifi_mgr_connect(DEV_WIFI_SSID, DEV_WIFI_PASSWORD));
    }

    const pc_media_config_t pc = {
        .lan_enabled = wifi_configured,
        .static_host = DEV_COMPANION_HOST,
        .port = 0,
        .art_max_w = UI_ART_MAX_W,
        .art_max_h = UI_ART_SIZE,
        .cover_area = ui_art_area,
        .cbs = {
            .on_state = on_state, .on_art = on_art, .on_status = on_status, .on_volume = on_volume, .on_source = on_source,
            .idle_have = on_idle_have, .idle_config = on_idle_config, .idle_picture = on_idle_picture,
            .on_pair_prompt = on_pair_prompt,
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

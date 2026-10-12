#pragma once
/*
 * Client for the Windows companion app ("Desk Media Companion", see companion/): finds the PC on the LAN (UDP beacon,
 * or a fixed address), polls GET /now-playing, downloads GET /artwork when it changes, and sends control commands.
 * Plain HTTP on the local network - no accounts, no tokens, no TLS.
 *
 * Everything runs in its own tasks; results arrive through the callbacks (called from the poll task, never from LVGL).
 */
#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"
#include "lvgl.h"

#define PC_MEDIA_DEFAULT_PORT       8765
#define PC_MEDIA_DISCOVERY_PORT     8766

typedef struct {
    bool     has_session;       /* the PC has a media session at all */
    bool     playing;
    char     state[12];         /* "playing" "paused" "stopped" ... */
    char     app[48];           /* Windows app id of the session, e.g. "Spotify.exe", "Chrome" */
    char     title[128];
    char     artist[128];       /* artist, or channel name for videos */
    char     album[96];
    uint32_t position_ms;       /* already extrapolated by the companion */
    uint32_t duration_ms;       /* 0 = unknown / live */
    char     track_id[16];      /* changes whenever the track changes */
    char     art_id[16];        /* "" = no artwork */
    bool     can_play, can_pause, can_next, can_prev, can_seek;
} pc_media_state_t;

typedef enum {
    PC_MEDIA_WAITING_NETWORK,   /* Wi-Fi not up yet */
    PC_MEDIA_SEARCHING,         /* no companion found yet */
    PC_MEDIA_CONNECTED,         /* companion answers and a media session exists */
    PC_MEDIA_NO_SESSION,        /* companion answers, but nothing is playing on the PC */
    PC_MEDIA_UNREACHABLE,       /* companion known but not answering (PC asleep / app closed / firewall) */
} pc_media_status_t;

typedef enum {
    PC_MEDIA_CMD_PLAY_PAUSE,
    PC_MEDIA_CMD_NEXT,
    PC_MEDIA_CMD_PREV,
    PC_MEDIA_CMD_SEEK,          /* arg = position in ms */
} pc_media_cmd_t;

typedef struct {
    void (*on_state)(const pc_media_state_t *st, void *ctx);
    /* New artwork: the cover and the blurred full-screen background built from it (RGB565 descriptors, valid until the
     * next on_art call returns), or both NULL when there is none. */
    void (*on_art)(const lv_image_dsc_t *art, const lv_image_dsc_t *bg, void *ctx);
    /* Development only: a one-shot request from the companion's /debug route ("screenshot", "tap_next", ...). */
    void (*on_debug)(const char *what, void *ctx);
    void (*on_status)(pc_media_status_t status, const char *detail, void *ctx);
    void *ctx;
} pc_media_callbacks_t;

typedef struct {
    const char *static_host;    /* "192.168.1.143" to skip discovery, or NULL / "" for automatic */
    uint16_t    port;           /* 0 = PC_MEDIA_DEFAULT_PORT */
    uint16_t    art_size;       /* pixel size of the cover as drawn (square); the companion renders exactly this size */
    struct { int x, y, size; } cover;   /* where the UI draws the cover - used for the shadow baked into the background */
    pc_media_callbacks_t cbs;
} pc_media_config_t;

esp_err_t pc_media_start(const pc_media_config_t *cfg);

/* Thread-safe, non-blocking. */
void pc_media_command(pc_media_cmd_t cmd, uint32_t arg);

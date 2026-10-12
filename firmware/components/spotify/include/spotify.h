#pragma once
/*
 * Spotify Web API client: Authorization Code + PKCE (no client secret, no Implicit Grant), token refresh,
 * playback polling, transport commands and album-art download/decode.
 *
 * Everything runs in its own task; results are delivered through the callbacks below (called from that task,
 * never from the LVGL task). Tokens live in RAM and in NVS ("spotify" namespace) and are never logged.
 */
#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"
#include "lvgl.h"

typedef struct {
    bool     has_item;           /* something is loaded (track / episode) */
    bool     is_ad;
    char     track_id[40];
    char     title[128];
    char     artist[128];
    char     album[96];
    char     art_url[200];
    uint32_t duration_ms;
    uint32_t progress_ms;        /* already corrected for network latency */
    bool     is_playing;
    bool     device_active;
    char     device_name[64];
    char     device_type[24];
} spotify_playback_t;

typedef enum {
    SPOTIFY_STATUS_STARTING,
    SPOTIFY_STATUS_WAITING_NETWORK,     /* Wi-Fi / clock not ready yet */
    SPOTIFY_STATUS_NO_CLIENT_ID,        /* build has no Spotify Client ID */
    SPOTIFY_STATUS_NEED_AUTH,           /* detail = URL of the on-device link page */
    SPOTIFY_STATUS_AUTH_ERROR,          /* authorization revoked / rejected; will ask again */
    SPOTIFY_STATUS_OK,                  /* last poll succeeded and something is playing/paused */
    SPOTIFY_STATUS_NO_DEVICE,           /* HTTP 204: no active Spotify device / nothing playing */
    SPOTIFY_STATUS_NET_ERROR,
    SPOTIFY_STATUS_RATE_LIMITED,
    SPOTIFY_STATUS_PREMIUM_REQUIRED,    /* playback control needs Spotify Premium */
} spotify_status_t;

typedef enum {
    SPOTIFY_CMD_PLAY_PAUSE,
    SPOTIFY_CMD_NEXT,
    SPOTIFY_CMD_PREV,
    SPOTIFY_CMD_SEEK,                   /* arg = position in ms */
} spotify_cmd_t;

typedef struct {
    void (*on_playback)(const spotify_playback_t *pb, void *ctx);
    /* New album art (RGB565 descriptor valid until the next on_art call returns), or NULL when there is none. */
    void (*on_art)(const lv_image_dsc_t *dsc, void *ctx);
    void (*on_status)(spotify_status_t status, const char *detail, void *ctx);
    void *ctx;
} spotify_callbacks_t;

typedef struct {
    const char *client_id;              /* from the Spotify dashboard; "" = use the one stored in NVS */
    const char *redirect_uri;           /* must be registered in the dashboard, e.g. http://127.0.0.1:8888/callback */
    spotify_callbacks_t cbs;
} spotify_config_t;

esp_err_t spotify_start(const spotify_config_t *cfg);

/* Thread-safe, non-blocking (queues the command for the Spotify task). */
void spotify_command(spotify_cmd_t cmd, uint32_t arg);

/* Forget the stored authorization (the link page will ask again). */
void spotify_forget_authorization(void);

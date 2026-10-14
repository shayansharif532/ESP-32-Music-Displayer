#pragma once
/*
 * Client for the Windows companion app ("DeskMedia Companion", see companion/): finds the PC, polls GET /now-playing, downloads
 * GET /artwork when it changes, and sends control commands.
 *
 * Two transports carry the same API (docs/protocol.md): the USB cable (primary, docs/usb-protocol.md: nothing to configure, works
 * with Wi-Fi off) and, optionally, the local network (UDP beacon or a fixed address, plain HTTP, only when Wi-Fi is configured).
 * USB is used whenever a companion answers on it, the LAN otherwise. The display pairs once with its companion (numeric code compared
 * on both screens, docs/protocol.md section 3); after that a USB link proves the key once per link, a LAN link signs every request.
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
    char     source[24];        /* short name of that app for the screen: "Spotify", "Chrome", "Edge" ("" from an old companion) */
    uint8_t  sources;           /* how many media sources (sessions) a vertical swipe can switch between; < 2: nothing to switch */
    uint8_t  source_index;      /* 1-based position of this session in that list */
    char     title[128];
    char     artist[128];       /* artist, or channel name for videos */
    char     album[96];
    uint32_t position_ms;       /* already extrapolated by the companion */
    uint32_t duration_ms;       /* 0 = unknown / live */
    char     track_id[16];      /* changes whenever the track changes */
    char     art_id[16];        /* "" = no artwork */
    bool     can_play, can_pause, can_next, can_prev, can_seek;
    /* Spotify only (decided by the companion from the session's app identity): its shuffle toggle and repeat button. */
    bool     spotify;
    bool     shuffle;           /* shuffle is on */
    uint8_t  repeat;            /* 0 = off, 1 = repeat the playlist / album, 2 = repeat the track */
    bool     can_shuffle, can_repeat;
    /* Idle screen (see components/idle_screen): nothing plays anywhere on the PC, the version of the PC's idle settings, and the
     * PC's local time (ms since the epoch; 0 = not sent). An older companion sends none of these: any_playing then follows `playing`. */
    bool     any_playing;
    uint32_t idle_v;
    int64_t  clock_ms;
    /* The user swiped onto the "Idle Screen" item of the source ring (a display choice only: media keeps playing). The companion
     * ends it by itself when a session starts playing. `sources` / `source_index` count that item too. */
    bool     idle_selected;
} pc_media_state_t;

typedef enum {
    PC_MEDIA_WAITING_NETWORK,   /* Wi-Fi is configured but not up yet (LAN fallback only) */
    PC_MEDIA_SEARCHING,         /* no companion found yet */
    PC_MEDIA_CONNECTED,         /* companion answers and a media session exists */
    PC_MEDIA_NO_SESSION,        /* companion answers, but nothing is playing on the PC */
    PC_MEDIA_UNREACHABLE,       /* companion known but not answering (PC asleep / app closed / firewall) */
    PC_MEDIA_NEEDS_PAIRING,     /* companion answers, this display holds no key; detail = the PC's name. Nothing but /hello is polled */
    PC_MEDIA_PAIRING,           /* a pairing attempt is running and the code prompt is on screen (on_pair_prompt); detail = the PC's name */
    PC_MEDIA_PAIRING_LOST,      /* a key is stored but the companion no longer accepts it (forgotten on the PC / reinstalled / replaced); detail = PC name */
    PC_MEDIA_NO_USB_DATA,       /* powered, but no USB data connection for several seconds: a charge-only cable, a charger, or a hub without data */
    PC_MEDIA_USB_CONNECTED,     /* USB data is there, the companion has not answered yet (the first seconds) */
    PC_MEDIA_COMPANION_MISSING, /* USB data for a while and still no companion: it is not installed, or not running */
    PC_MEDIA_INCOMPATIBLE,      /* detail "companion": the PC app is too old for this firmware; "firmware": the firmware is too old for the PC app */
} pc_media_status_t;

typedef enum {
    PC_MEDIA_CMD_PLAY_PAUSE,
    PC_MEDIA_CMD_NEXT,
    PC_MEDIA_CMD_PREV,
    PC_MEDIA_CMD_SEEK,          /* arg = position in ms */
    PC_MEDIA_CMD_VOLUME_UP,     /* arg = step in percent points (0 = 5): the displayed app's own volume, not the PC's master volume */
    PC_MEDIA_CMD_VOLUME_DOWN,
    PC_MEDIA_CMD_SHUFFLE,       /* Spotify only: toggle shuffle / cycle repeat (off -> playlist -> track -> off) of the displayed session */
    PC_MEDIA_CMD_REPEAT,
    PC_MEDIA_CMD_SOURCE_NEXT,   /* switch the display (and everything it controls) to the next / previous source on the PC: a media session,
                                 * or the synthetic "Idle Screen" item. arg = 1: the display is showing the idle screen right now */
    PC_MEDIA_CMD_SOURCE_PREV,
    PC_MEDIA_CMD_SOURCE_AUTO,   /* leave a manual choice (e.g. the idle screen): back to following what plays */
} pc_media_cmd_t;

typedef struct {
    void (*on_state)(const pc_media_state_t *st, void *ctx);
    /* New artwork: the cover and the blurred full-screen background built from it (RGB565 descriptors, valid until the
     * next on_art call returns), or both NULL when there is none. */
    void (*on_art)(const lv_image_dsc_t *art, const lv_image_dsc_t *bg, void *ctx);
    /* Result of a volume command: the app's new volume 0..100 (muted = its session is muted), -1 when the displayed app has
     * no controllable audio session, -2 when the PC did not answer. */
    void (*on_volume)(int percent, bool muted, void *ctx);
    /* The PC switched to another media source after a swipe: its short name and 1-based position among `count` sources. The new
     * session's state arrives through on_state / on_art right before this call. */
    void (*on_source)(const char *name, int index, int count, void *ctx);
    /* Development only: a one-shot request from the companion's /debug route ("screenshot", "tap_next", ...). */
    void (*on_debug)(const char *what, void *ctx);
    void (*on_status)(pc_media_status_t status, const char *detail, void *ctx);
    /* Idle-screen settings sync (all optional; called from the poll task, only when the PC's settings version changed):
     * idle_have(v) = the display already holds exactly that version; idle_config(json, len) gets GET /idle and returns 1 = "now send
     * me the picture", 0 = applied, -1 = unusable; idle_picture(jpeg, len) gets GET /idle/image and returns false to ask for a retry. */
    bool (*idle_have)(uint32_t version, void *ctx);
    int (*idle_config)(const char *json, size_t len, void *ctx);
    bool (*idle_picture)(const uint8_t *jpeg, size_t len, void *ctx);
    /* Pairing prompt (optional, but without it the display never pairs): code != NULL = show "Pair this display?" with the PC's name
     * and the 6 digits (a string of exactly 6 digits); code == NULL = hide it again. The user's answer goes back through
     * pc_media_pair_answer(). Called from the poll task, never from LVGL. */
    void (*on_pair_prompt)(const char *pc_name, const char *code, void *ctx);
    void *ctx;
} pc_media_callbacks_t;

typedef struct {
    bool        lan_enabled;    /* Wi-Fi is configured: also look for the companion on the network (USB works either way) */
    const char *static_host;    /* "192.168.1.50" to skip discovery, or NULL / "" for automatic (LAN only) */
    uint16_t    port;           /* 0 = PC_MEDIA_DEFAULT_PORT */
    /* The cover is requested from the companion inside this box and keeps its aspect ratio: square art comes back
     * art_max_h x art_max_h, 16:9 art as a landscape card up to art_max_w wide. The companion renders exactly the size drawn. */
    uint16_t    art_max_w, art_max_h;
    /* Where the UI draws a cover of the given pixel size - used for the shadow baked into the background. */
    lv_area_t (*cover_area)(int art_w, int art_h);
    pc_media_callbacks_t cbs;
} pc_media_config_t;

esp_err_t pc_media_start(const pc_media_config_t *cfg);

/* Starts the USB link (and the log mirror to the companion) ahead of pc_media_start(): call it right after NVS is up, before the slow display
 * start, so the companion sees the display about three seconds sooner and the whole boot log reaches it. Idempotent; pc_media_start() calls it too. */
void pc_media_early_start(bool wifi_fallback);   /* wifi_fallback: this build will start Wi-Fi (development / LAN fallback): the SAR-ADC entropy source is then left off */

/* Thread-safe, non-blocking. */
void pc_media_command(pc_media_cmd_t cmd, uint32_t arg);

/* The user tapped Pair (true) or Cancel (false) on the prompt shown through on_pair_prompt. Thread-safe and non-blocking (an atomic flag
 * plus a task notification), so the LVGL tap handler may call it; ignored when no prompt is showing. */
void pc_media_pair_answer(bool accept);

/* A pairing key is stored (also while the pairing is currently "lost"). Thread-safe. */
bool pc_media_is_paired(void);

#pragma once
/*
 * Now Playing screen (480x480). All functions are thread-safe: they take the LVGL lock themselves,
 * so network / app tasks may call them directly.  Do NOT call them from inside an LVGL callback.
 *
 * The command callback runs on the LVGL task: it must only post to a queue, never block or do I/O.
 */
#include <stdbool.h>
#include <stdint.h>
#include "lvgl.h"

/* The cover is shown at its own shape: a square art_size x art_size (UI_ART_SIZE), or - for 16:9 video thumbnails and other
 * wide art - a landscape card up to UI_ART_MAX_W wide (never stretched). The network side asks the companion for a cover that
 * already has the size it is drawn at (no scaling) and bakes the soft shadow behind ui_art_area() into the background image. */
#define UI_ART_SIZE     312
#define UI_ART_MAX_W    432
#define UI_ART_X        ((480 - UI_ART_SIZE) / 2)
#define UI_ART_Y        18

/* Where a cover of w x h pixels is drawn in the normal (non full-screen) layout. Pure function: no LVGL lock needed. */
lv_area_t ui_art_area(int w, int h);

typedef enum {
    UI_CMD_PREV,
    UI_CMD_PLAY_PAUSE,
    UI_CMD_NEXT,
    UI_CMD_SEEK,            /* arg = target position in ms */
    UI_CMD_SHUFFLE,         /* Spotify only: the small shuffle button beside "previous" */
    UI_CMD_REPEAT,          /* Spotify only: the small repeat button beside "next" (cycles off / playlist / track) */
    UI_CMD_SOURCE_NEXT,     /* vertical swipe up:   switch to the next media source (app / session) on the PC */
    UI_CMD_SOURCE_PREV,     /* vertical swipe down: switch to the previous one */
} ui_cmd_t;

typedef void (*ui_cmd_cb_t)(ui_cmd_t cmd, uint32_t arg, void *ctx);

typedef enum {
    UI_STATUS_INFO,
    UI_STATUS_OK,
    UI_STATUS_WARN,
    UI_STATUS_ERROR,
} ui_status_level_t;

/* Build the screen. Call once, after the display has been started. */
void ui_init(ui_cmd_cb_t cb, void *ctx);

/* A track is loaded: show its text and total length. Progress is set with ui_set_progress(). */
void ui_show_track(const char *title, const char *artist, uint32_t duration_ms);

/* Nothing to show (no track / no device / setup message): two text lines, bar reset, art placeholder. */
void ui_show_idle(const char *line1, const char *line2);

/* Re-anchor the local progress interpolation (called after every poll / seek). */
void ui_set_progress(uint32_t progress_ms, bool playing);

/* Dim the controls the current media session does not support (they stay tappable; the session just refuses). */
void ui_set_capabilities(bool can_prev, bool can_next, bool can_seek);

/* Artwork: the cover and the blurred full-screen background derived from it (both 16-bit RGB565 image descriptors), or
 * NULL/NULL for the placeholder. The pixel buffers must stay valid until another ui_set_art() call has returned. */
void ui_set_art(const lv_image_dsc_t *art, const lv_image_dsc_t *bg);

/* One small status line at the very top (empty/NULL text hides it). */
void ui_set_status(ui_status_level_t level, const char *text);

/* Brief translucent "Volume 65%" pill (about one second, then it fades). percent < 0: the displayed app has no volume to
 * control ("No volume control"), -2: the PC did not answer; muted shows "Muted". No permanent screen space is used. */
void ui_show_volume(int percent, bool muted);

/* Spotify's shuffle / repeat buttons. They exist only while the displayed session is Spotify (spotify = false hides them and
 * leaves the screen exactly as for every other app). repeat: 0 off, 1 playlist / album, 2 track. can_*: Spotify allows the change
 * right now (the button is dimmed otherwise). Cheap to call on every poll: nothing is redrawn unless something changed. */
void ui_set_spotify_controls(bool spotify, bool can_shuffle, bool can_repeat, bool shuffle, int repeat);

/* How many media sources the PC currently offers (a vertical swipe only does something when there are at least two). */
void ui_set_sources(int count);

/* Brief translucent pill with the name of the source the display just switched to (and its place among the `count` sources, as
 * small dots). Shares the pill slot with the volume overlay; fades after about a second. */
void ui_show_source(const char *name, int index, int count);

/* Monotonic count of touch presses seen by the UI (for soak-test logging). */
uint32_t ui_touch_count(void);

#ifdef APP_DEV_TOOLS
/* Development only: act as if a control was tapped ("play", "next", "prev", "art"). */
void ui_debug_tap(const char *which);
/* Development only: drive a virtual finger through the real LVGL input path (hit-testing, swipes, clicks). Callable from any task. */
void ui_debug_touch(int x, int y, bool pressed);
#endif

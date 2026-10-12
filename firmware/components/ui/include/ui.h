#pragma once
/*
 * Now Playing screen (480x480).  All functions are thread-safe: they take the LVGL lock themselves,
 * so network / app tasks may call them directly.  Do NOT call them from inside an LVGL callback.
 *
 * The command callback runs on the LVGL task: it must only post to a queue, never block or do I/O.
 */
#include <stdbool.h>
#include <stdint.h>
#include "lvgl.h"

typedef enum {
    UI_CMD_PREV,
    UI_CMD_PLAY_PAUSE,
    UI_CMD_NEXT,
    UI_CMD_SEEK,            /* arg = target position in ms */
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

/* Album art: 16-bit RGB565 image descriptor, or NULL for the placeholder. The pixel buffer must stay valid
 * until another ui_set_art() call has returned. */
void ui_set_art(const lv_image_dsc_t *dsc);

/* One small status line at the very top (empty/NULL text hides it). */
void ui_set_status(ui_status_level_t level, const char *text);

/* Monotonic count of touch presses seen by the UI (for soak-test logging). */
uint32_t ui_touch_count(void);

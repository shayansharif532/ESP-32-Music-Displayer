#pragma once
/*
 * The idle screen: what the display shows while the PC companion is connected but nothing is playing.
 *
 * An LVGL screen of its own (the player screen is never touched by it). What it looks like is decided on the PC (Desk Media
 * Companion -> Settings -> Idle Screen): a theme (Minimal / Dark / Neon / Photo), an accent colour, an optional clock (12 / 24 h),
 * an optional custom line and, for the Photo theme, a background picture the companion has already cropped / fitted / blurred to
 * exactly 480x480 and encoded as a small baseline JPEG.
 *
 * The display keeps what it was sent: the settings in NVS, the picture in the `idle_img` flash partition, so the last chosen idle
 * screen is shown again after a reboot without the PC resending anything. The companion's settings carry a version number that
 * appears in every /now-playing answer; only when it differs from the stored one does pc_media call the sync functions below.
 * Anything wrong with the picture (missing, corrupt, undecodable) falls back to the built-in Dark theme.
 *
 * Threading: everything takes the LVGL lock itself and may be called from any task except an LVGL callback, except where noted.
 */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "lvgl.h"

/* Called on the LVGL task when the idle screen is tapped: must only post / flag, never block. */
typedef void (*idle_tap_cb_t)(void *ctx);

/* Builds the screen (not shown) and loads the stored configuration + picture in the background. Call once, after the display and
 * the player UI exist. */
void idle_screen_init(idle_tap_cb_t on_tap, void *ctx);

/* The screen object, for lv_screen_load_anim() by whoever switches screens. */
lv_obj_t *idle_screen_obj(void);

/* The owner of the screen switching tells it when it becomes visible / hidden (the clock only ticks while visible). */
void idle_screen_activate(bool visible);

/* The PC's local time as milliseconds since the epoch (companion's clock_ms); the clock then runs on the display's own timer.
 * Any task. 0 is ignored. */
void idle_screen_set_clock(int64_t local_ms);

/* The small status line at the bottom, e.g. "Nothing playing". */
void idle_screen_set_status(const char *text);

/* How long nothing must have been playing before the idle screen is shown (the user's setting, default 4 s). */
uint32_t idle_screen_delay_ms(void);

/* ---- sync with the companion (called by pc_media from its poll task) ---- */

/* true when the display already holds exactly this settings version. */
bool idle_screen_have_version(uint32_t version);

/* The text of GET /idle. Returns 1 when the settings are good but the picture has to be downloaded next (GET /idle/image, then
 * idle_screen_apply_picture), 0 when everything is applied, -1 when the answer is unusable (nothing changed; retry later). */
int idle_screen_apply_config(const char *json, size_t len);

/* The body of GET /idle/image for the config just passed to idle_screen_apply_config(). false = rejected (corrupt transfer: retry
 * later); a picture that arrives intact but cannot be decoded is applied as "no picture" (Dark theme) and returns true. */
bool idle_screen_apply_picture(const uint8_t *jpeg, size_t len);

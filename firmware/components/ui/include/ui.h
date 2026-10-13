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

/* The same pill, drawn over another screen: the pill follows the screen that is (becoming) visible, e.g. the idle screen when the
 * user swiped onto the "Idle Screen" item of the source ring. NULL = the player screen. */
void ui_show_source_on(lv_obj_t *screen, const char *name, int index, int count);

/* Vertical swipes (up = next source, down = previous source) also work on another full-screen LVGL screen, e.g. the idle screen: the
 * same thresholds and safeguards as on the player; horizontal swipes do nothing there. A swipe consumes its touch (no tap follows). */
void ui_attach_vertical_swipes(lv_obj_t *screen);

/* The touch that is just ending travelled further than a tap may (a sloppy swipe): handlers of a "tap" on another screen use this to
 * ignore it. Valid inside a CLICKED callback. */
bool ui_touch_moved(void);

/* Monotonic count of touch presses seen by the UI (for soak-test logging). */
uint32_t ui_touch_count(void);

/* ---- pairing prompt (ui_pairing.c) ------------------------------------------------------------------------------
 * A screen of its own, loaded over whatever is showing (with the same 350 ms fade as the player / idle switching): "Pair this display?",
 * the PC's name, the 6-digit code and two large buttons, Pair and Cancel. The three calls take the LVGL lock themselves: call them from
 * any task, NOT from an LVGL callback. The answer callback runs on the LVGL task after a tap: it must only post (no I/O, no blocking). */

/* Shows the prompt. code6 = exactly 6 digits (shown as "NNN NNN"). A second call while it is up just replaces the texts. */
void ui_pairing_show(const char *pc_name, const char *code6, void (*cb)(bool accepted, void *ctx), void *ctx);

/* Hides it again and returns to the screen that should be showing (the one that was active before, or the one the screen switching
 * logic asked for through ui_pairing_defer() in the meantime). */
void ui_pairing_hide(void);

/* The prompt is on screen. Lock-free, any task. */
bool ui_pairing_active(void);

/* For whoever loads screens (main.c's player / idle switching): while the prompt is up it keeps the display. Call with the LVGL lock
 * held, instead of lv_screen_load_anim(): true = the prompt is up, `target` is remembered and loaded when it is dismissed. */
bool ui_pairing_defer(lv_obj_t *target);

/* ---- connection / onboarding overlay (ui_link.c) ------------------------------------------------------------------
 * What the display says while it has no working link to the PC app: a full-screen layer on top of the player screen (the screen
 * switching logic keeps working underneath; the layer swallows touches). A step strip shows how far the setup is (USB, Companion,
 * Paired). The calls take the LVGL lock themselves: any task, NOT from an LVGL callback. Showing the same kind again is free. */
typedef enum {
    UI_LINK_NO_USB_DATA,        /* powered, but no USB data connection (charge-only cable, charger) */
    UI_LINK_USB_CONNECTED,      /* data is there; looking for the companion (the first seconds) */
    UI_LINK_COMPANION_MISSING,  /* the companion is not installed / not running: install QR code + address */
    UI_LINK_PAIRING_WAIT,       /* companion found, waiting for the pairing to be opened / confirmed on the PC */
    UI_LINK_PAIRING_LOST,       /* a stored pairing is not accepted any more: pair again */
    UI_LINK_UPDATE_COMPANION,   /* the PC app is too old for this firmware (QR code + address) */
    UI_LINK_UPDATE_FIRMWARE,    /* the firmware is too old for the PC app */
} ui_link_kind_t;

void ui_link_init(lv_obj_t *player_screen);      /* called by ui_init() */
/* url = the full setup address (encoded in the QR code), url_short = how it is printed under the code; pc_name = the PC (may be NULL). */
void ui_link_show(ui_link_kind_t kind, const char *url, const char *url_short, const char *pc_name);
void ui_link_hide(void);
bool ui_link_visible(void);

#ifdef APP_DEV_TOOLS
/* Development only: act as if a control was tapped ("play", "next", "prev", "art"). */
void ui_debug_tap(const char *which);
/* Development only: drive a virtual finger through the real LVGL input path (hit-testing, swipes, clicks). Callable from any task. */
void ui_debug_touch(int x, int y, bool pressed);
/* Development only: press Pair (true) / Cancel (false) on the pairing prompt through the same function the buttons use. */
void ui_pairing_debug_press(bool accept);
#endif

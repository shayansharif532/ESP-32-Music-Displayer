/* Now Playing screen, 480x480.  See ui.h for the threading rules. */
#include "ui.h"

#include <stdatomic.h>
#include <stdio.h>
#include <string.h>

#include "bsp/esp-bsp.h"

/* ---- layout (normal mode) ------------------------------------------------------------------ */
#define ART_SIZE        260
#define ART_X           110
#define ART_Y           24
#define FULL_SIZE       460
#define FULL_POS        10

/* ---- palette --------------------------------------------------------------------------------- */
#define COL_BG          0x0B0B10
#define COL_TEXT        0xFFFFFF
#define COL_SUBTEXT     0xA6A6B3
#define COL_DIM         0x6B6B78
#define COL_TRACK       0x2A2A35
#define COL_ACCENT      0x1ED760
#define COL_BTN         0x24242E
#define COL_BTN_PRESS   0x3A3A48
#define COL_INFO        0x8A8A99
#define COL_OK          0x1ED760
#define COL_WARN        0xFFB020
#define COL_ERROR       0xFF5A5F

#define SLIDER_MAX      1000
#define TICK_PERIOD_MS  250

static struct {
    lv_obj_t *art_box, *art_img, *art_icon;
    lv_obj_t *status, *title, *artist;
    lv_obj_t *slider, *t_elapsed, *t_total;
    lv_obj_t *btn_prev, *btn_play, *btn_next, *lbl_play;

    ui_cmd_cb_t cb;
    void *ctx;

    bool has_track;
    bool playing;
    bool fullscreen;
    uint32_t duration_ms;
    uint32_t anchor_ms;      /* progress at anchor_tick */
    uint32_t anchor_tick;
    int32_t shown_sec;       /* elapsed seconds currently on the label, -1 = none */
    bool ready;
} s;

static atomic_uint_fast32_t s_touches;

/* ---- helpers ---------------------------------------------------------------------------------- */

static void fmt_time(char *out, size_t n, uint32_t ms)
{
    uint32_t sec = ms / 1000;
    if (sec >= 3600) {
        snprintf(out, n, "%u:%02u:%02u", (unsigned)(sec / 3600), (unsigned)((sec / 60) % 60), (unsigned)(sec % 60));
    } else {
        snprintf(out, n, "%u:%02u", (unsigned)(sec / 60), (unsigned)(sec % 60));
    }
}

static void set_elapsed_label(uint32_t pos_ms)
{
    int32_t sec = (int32_t)(pos_ms / 1000);
    if (sec == s.shown_sec) {
        return;
    }
    s.shown_sec = sec;
    char buf[16];
    fmt_time(buf, sizeof buf, pos_ms);
    lv_label_set_text(s.t_elapsed, buf);
}

static uint32_t current_pos_ms(void)
{
    uint32_t pos = s.anchor_ms;
    if (s.playing) {
        pos += lv_tick_elaps(s.anchor_tick);
    }
    if (s.duration_ms && pos > s.duration_ms) {
        pos = s.duration_ms;
    }
    return pos;
}

static void set_play_icon(bool playing)
{
    lv_label_set_text(s.lbl_play, playing ? LV_SYMBOL_PAUSE : LV_SYMBOL_PLAY);
}

static void apply_slider(uint32_t pos_ms)
{
    int32_t v = s.duration_ms ? (int32_t)(((uint64_t)pos_ms * SLIDER_MAX) / s.duration_ms) : 0;
    lv_slider_set_value(s.slider, v, LV_ANIM_OFF);
}

/* ---- events ----------------------------------------------------------------------------------- */

static void fire(ui_cmd_t cmd, uint32_t arg)
{
    if (s.cb) {
        s.cb(cmd, arg, s.ctx);
    }
}

static void on_prev(lv_event_t *e)  { (void)e; fire(UI_CMD_PREV, 0); }
static void on_play(lv_event_t *e)  { (void)e; fire(UI_CMD_PLAY_PAUSE, 0); }
static void on_next(lv_event_t *e)  { (void)e; fire(UI_CMD_NEXT, 0); }

static void on_slider_changed(lv_event_t *e)
{
    (void)e;
    if (s.has_track && lv_slider_is_dragged(s.slider)) {
        uint32_t pos = (uint32_t)(((uint64_t)lv_slider_get_value(s.slider) * s.duration_ms) / SLIDER_MAX);
        set_elapsed_label(pos);
    }
}

static void on_slider_released(lv_event_t *e)
{
    (void)e;
    if (!s.has_track || s.duration_ms == 0) {
        return;
    }
    uint32_t pos = (uint32_t)(((uint64_t)lv_slider_get_value(s.slider) * s.duration_ms) / SLIDER_MAX);
    s.anchor_ms = pos;                  /* hold the new position until the next poll confirms it */
    s.anchor_tick = lv_tick_get();
    fire(UI_CMD_SEEK, pos);
}

static void set_chrome_hidden(bool hidden)
{
    lv_obj_t *items[] = { s.status, s.title, s.artist, s.slider, s.t_elapsed, s.t_total,
                          s.btn_prev, s.btn_play, s.btn_next };
    for (size_t i = 0; i < sizeof items / sizeof items[0]; i++) {
        if (hidden) {
            lv_obj_add_flag(items[i], LV_OBJ_FLAG_HIDDEN);
        } else {
            lv_obj_remove_flag(items[i], LV_OBJ_FLAG_HIDDEN);
        }
    }
}

static void on_art_clicked(lv_event_t *e)
{
    (void)e;
    s.fullscreen = !s.fullscreen;
    if (s.fullscreen) {
        set_chrome_hidden(true);
        lv_obj_set_size(s.art_box, FULL_SIZE, FULL_SIZE);
        lv_obj_set_pos(s.art_box, FULL_POS, FULL_POS);
    } else {
        lv_obj_set_size(s.art_box, ART_SIZE, ART_SIZE);
        lv_obj_set_pos(s.art_box, ART_X, ART_Y);
        set_chrome_hidden(false);
    }
}

static void on_indev_pressed(lv_event_t *e)
{
    (void)e;
    atomic_fetch_add(&s_touches, 1);    /* no logging here: this runs on the LVGL task */
}

/* Runs on the LVGL task every TICK_PERIOD_MS: moves the bar between polls. No logging, no allocation. */
static void tick_cb(lv_timer_t *t)
{
    (void)t;
    if (!s.has_track) {
        return;
    }
    uint32_t pos = current_pos_ms();
    if (!lv_slider_is_dragged(s.slider)) {
        apply_slider(pos);
        set_elapsed_label(pos);
    }
}

/* ---- construction ----------------------------------------------------------------------------- */

static lv_obj_t *make_label(lv_obj_t *parent, const lv_font_t *font, uint32_t color)
{
    lv_obj_t *l = lv_label_create(parent);
    lv_obj_set_style_text_font(l, font, 0);
    lv_obj_set_style_text_color(l, lv_color_hex(color), 0);
    return l;
}

static lv_obj_t *make_round_button(lv_obj_t *parent, int32_t size, uint32_t bg, uint32_t bg_pressed,
                                   const char *symbol, const lv_font_t *font, uint32_t fg, lv_obj_t **label_out)
{
    lv_obj_t *b = lv_button_create(parent);
    lv_obj_set_size(b, size, size);
    lv_obj_set_style_radius(b, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(b, lv_color_hex(bg), 0);
    lv_obj_set_style_bg_color(b, lv_color_hex(bg_pressed), LV_STATE_PRESSED);
    lv_obj_set_style_shadow_width(b, 0, 0);
    lv_obj_set_style_border_width(b, 0, 0);
    lv_obj_set_ext_click_area(b, 8);
    lv_obj_t *l = make_label(b, font, fg);
    lv_label_set_text(l, symbol);
    lv_obj_center(l);
    if (label_out) {
        *label_out = l;
    }
    return b;
}

void ui_init(ui_cmd_cb_t cb, void *ctx)
{
    if (!bsp_display_lock(0)) {
        return;
    }
    memset(&s, 0, sizeof s);
    s.cb = cb;
    s.ctx = ctx;
    s.shown_sec = -1;

    lv_obj_t *scr = lv_screen_active();
    lv_obj_set_style_bg_color(scr, lv_color_hex(COL_BG), 0);
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);
    lv_obj_remove_flag(scr, LV_OBJ_FLAG_SCROLLABLE);

    /* status line */
    s.status = make_label(scr, &lv_font_montserrat_12, COL_INFO);
    lv_obj_set_width(s.status, 460);
    lv_obj_set_style_text_align(s.status, LV_TEXT_ALIGN_CENTER, 0);
    lv_label_set_long_mode(s.status, LV_LABEL_LONG_MODE_DOTS);
    lv_obj_align(s.status, LV_ALIGN_TOP_MID, 0, 5);
    lv_label_set_text(s.status, "");

    /* album art box (placeholder until real art arrives) */
    s.art_box = lv_obj_create(scr);
    lv_obj_set_size(s.art_box, ART_SIZE, ART_SIZE);
    lv_obj_set_pos(s.art_box, ART_X, ART_Y);
    lv_obj_remove_flag(s.art_box, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_pad_all(s.art_box, 0, 0);
    lv_obj_set_style_border_width(s.art_box, 0, 0);
    lv_obj_set_style_radius(s.art_box, 20, 0);
    lv_obj_set_style_clip_corner(s.art_box, true, 0);
    lv_obj_set_style_bg_opa(s.art_box, LV_OPA_COVER, 0);
    lv_obj_set_style_bg_color(s.art_box, lv_color_hex(0x2B2540), 0);
    lv_obj_set_style_bg_grad_color(s.art_box, lv_color_hex(0x14141C), 0);
    lv_obj_set_style_bg_grad_dir(s.art_box, LV_GRAD_DIR_VER, 0);
    lv_obj_add_event_cb(s.art_box, on_art_clicked, LV_EVENT_CLICKED, NULL);

    s.art_icon = make_label(s.art_box, &lv_font_montserrat_48, 0x5F5A78);
    lv_label_set_text(s.art_icon, LV_SYMBOL_AUDIO);
    lv_obj_center(s.art_icon);

    s.art_img = lv_image_create(s.art_box);
    lv_obj_set_size(s.art_img, LV_PCT(100), LV_PCT(100));
    lv_image_set_inner_align(s.art_img, LV_IMAGE_ALIGN_STRETCH);
    lv_obj_add_flag(s.art_img, LV_OBJ_FLAG_HIDDEN);
    lv_obj_remove_flag(s.art_img, LV_OBJ_FLAG_CLICKABLE);     /* clicks go to the box */

    /* title / artist */
    s.title = make_label(scr, &lv_font_montserrat_26, COL_TEXT);
    lv_obj_set_width(s.title, 430);
    lv_obj_set_style_text_align(s.title, LV_TEXT_ALIGN_CENTER, 0);
    lv_label_set_long_mode(s.title, LV_LABEL_LONG_MODE_SCROLL_CIRCULAR);
    lv_obj_align(s.title, LV_ALIGN_TOP_MID, 0, 292);

    s.artist = make_label(scr, &lv_font_montserrat_20, COL_SUBTEXT);
    lv_obj_set_width(s.artist, 430);
    lv_obj_set_style_text_align(s.artist, LV_TEXT_ALIGN_CENTER, 0);
    lv_label_set_long_mode(s.artist, LV_LABEL_LONG_MODE_SCROLL_CIRCULAR);
    lv_obj_align(s.artist, LV_ALIGN_TOP_MID, 0, 328);

    /* progress / seek */
    s.slider = lv_slider_create(scr);
    lv_slider_set_range(s.slider, 0, SLIDER_MAX);
    lv_obj_set_size(s.slider, 400, 8);
    lv_obj_align(s.slider, LV_ALIGN_TOP_MID, 0, 366);
    lv_obj_set_ext_click_area(s.slider, 22);
    lv_obj_set_style_bg_color(s.slider, lv_color_hex(COL_TRACK), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(s.slider, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_bg_color(s.slider, lv_color_hex(COL_ACCENT), LV_PART_INDICATOR);
    lv_obj_set_style_bg_opa(s.slider, LV_OPA_COVER, LV_PART_INDICATOR);
    lv_obj_set_style_bg_color(s.slider, lv_color_hex(COL_TEXT), LV_PART_KNOB);
    lv_obj_set_style_bg_opa(s.slider, LV_OPA_COVER, LV_PART_KNOB);
    lv_obj_set_style_pad_all(s.slider, 3, LV_PART_KNOB);
    lv_obj_add_event_cb(s.slider, on_slider_changed, LV_EVENT_VALUE_CHANGED, NULL);
    lv_obj_add_event_cb(s.slider, on_slider_released, LV_EVENT_RELEASED, NULL);

    s.t_elapsed = make_label(scr, &lv_font_montserrat_16, COL_DIM);
    lv_obj_align(s.t_elapsed, LV_ALIGN_TOP_LEFT, 40, 380);
    lv_label_set_text(s.t_elapsed, "0:00");
    s.t_total = make_label(scr, &lv_font_montserrat_16, COL_DIM);
    lv_obj_align(s.t_total, LV_ALIGN_TOP_RIGHT, -40, 380);
    lv_label_set_text(s.t_total, "0:00");

    /* transport controls */
    s.btn_prev = make_round_button(scr, 60, COL_BTN, COL_BTN_PRESS, LV_SYMBOL_PREV, &lv_font_montserrat_24, COL_TEXT, NULL);
    lv_obj_align(s.btn_prev, LV_ALIGN_TOP_MID, -110, 406);
    lv_obj_add_event_cb(s.btn_prev, on_prev, LV_EVENT_CLICKED, NULL);

    s.btn_play = make_round_button(scr, 76, COL_ACCENT, 0x17B04D, LV_SYMBOL_PLAY, &lv_font_montserrat_32, 0x000000, &s.lbl_play);
    lv_obj_align(s.btn_play, LV_ALIGN_TOP_MID, 0, 398);
    lv_obj_add_event_cb(s.btn_play, on_play, LV_EVENT_CLICKED, NULL);

    s.btn_next = make_round_button(scr, 60, COL_BTN, COL_BTN_PRESS, LV_SYMBOL_NEXT, &lv_font_montserrat_24, COL_TEXT, NULL);
    lv_obj_align(s.btn_next, LV_ALIGN_TOP_MID, 110, 406);
    lv_obj_add_event_cb(s.btn_next, on_next, LV_EVENT_CLICKED, NULL);

    lv_label_set_text(s.title, "Starting...");
    lv_label_set_text(s.artist, "");

    lv_timer_create(tick_cb, TICK_PERIOD_MS, NULL);

    lv_indev_t *indev = bsp_display_get_input_dev();
    if (indev) {
        lv_indev_add_event_cb(indev, on_indev_pressed, LV_EVENT_PRESSED, NULL);
    }

    s.ready = true;
    bsp_display_unlock();
}

/* ---- public updates ----------------------------------------------------------------------------- */

void ui_show_track(const char *title, const char *artist, uint32_t duration_ms)
{
    if (!s.ready || !bsp_display_lock(1000)) {
        return;
    }
    s.has_track = true;
    s.duration_ms = duration_ms;
    lv_label_set_text(s.title, title ? title : "");
    lv_label_set_text(s.artist, artist ? artist : "");
    char buf[16];
    fmt_time(buf, sizeof buf, duration_ms);
    lv_label_set_text(s.t_total, buf);
    s.shown_sec = -1;
    bsp_display_unlock();
}

void ui_show_idle(const char *line1, const char *line2)
{
    if (!s.ready || !bsp_display_lock(1000)) {
        return;
    }
    s.has_track = false;
    s.playing = false;
    s.duration_ms = 0;
    s.anchor_ms = 0;
    s.shown_sec = -1;
    lv_label_set_text(s.title, line1 ? line1 : "");
    lv_label_set_text(s.artist, line2 ? line2 : "");
    lv_label_set_text(s.t_elapsed, "0:00");
    lv_label_set_text(s.t_total, "0:00");
    lv_slider_set_value(s.slider, 0, LV_ANIM_OFF);
    set_play_icon(false);
    lv_obj_add_flag(s.art_img, LV_OBJ_FLAG_HIDDEN);
    lv_obj_remove_flag(s.art_icon, LV_OBJ_FLAG_HIDDEN);
    bsp_display_unlock();
}

void ui_set_progress(uint32_t progress_ms, bool playing)
{
    if (!s.ready || !bsp_display_lock(1000)) {
        return;
    }
    s.anchor_ms = progress_ms;
    s.anchor_tick = lv_tick_get();
    s.playing = playing;
    set_play_icon(playing);
    if (s.has_track && !lv_slider_is_dragged(s.slider)) {
        apply_slider(current_pos_ms());
        set_elapsed_label(current_pos_ms());
    }
    bsp_display_unlock();
}

void ui_set_art(const lv_image_dsc_t *dsc)
{
    if (!s.ready || !bsp_display_lock(1000)) {
        return;
    }
    if (dsc) {
        lv_image_set_src(s.art_img, dsc);
        lv_obj_remove_flag(s.art_img, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(s.art_icon, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_add_flag(s.art_img, LV_OBJ_FLAG_HIDDEN);
        lv_obj_remove_flag(s.art_icon, LV_OBJ_FLAG_HIDDEN);
    }
    bsp_display_unlock();
}

void ui_set_status(ui_status_level_t level, const char *text)
{
    if (!s.ready || !bsp_display_lock(1000)) {
        return;
    }
    static const uint32_t colors[] = { COL_INFO, COL_OK, COL_WARN, COL_ERROR };
    lv_obj_set_style_text_color(s.status, lv_color_hex(colors[level]), 0);
    lv_label_set_text(s.status, text ? text : "");
    bsp_display_unlock();
}

uint32_t ui_touch_count(void)
{
    return (uint32_t)atomic_load(&s_touches);
}

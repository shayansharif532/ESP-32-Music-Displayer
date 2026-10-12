/* Now Playing screen, 480x480.  See ui.h for the threading rules.
 *
 * Look: a blurred, darkened full-screen version of the cover is the background (one pre-baked RGB565 image, see
 * pc_media/pc_art.c); the sharp cover sits on it with no frame or card; title and artist are centred below, then a thin
 * progress line with the times at its ends, and the transport controls (big white play button, plain prev/next icons).
 */
#include "ui.h"

#include <stdatomic.h>
#include <stdio.h>
#include <string.h>

#include "bsp/esp-bsp.h"
#include "esp_heap_caps.h"

/* ---- layout ------------------------------------------------------------------------------------ */
#define SCR             480
#define ART_RADIUS      14
#define FULL_SIZE       456
#define FULL_POS        ((SCR - FULL_SIZE) / 2)

#define TITLE_Y         (UI_ART_Y + UI_ART_SIZE + 6)       /* 336 */
#define ARTIST_Y        (TITLE_Y + 31)                     /* 367 */
#define BAR_Y           406                                /* vertical centre of the progress line */
#define BAR_W           UI_ART_SIZE                        /* as wide as the cover */
#define BAR_H           4
#define TIME_W          58
#define CTRL_Y          446                                /* vertical centre of the control row */
#define PLAY_D          60
#define SIDE_D          64                                 /* touch area of prev / next (no visible circle) */
#define SIDE_DX         112

/* ---- palette ----------------------------------------------------------------------------------- */
#define COL_SCREEN_TOP  0x1D1D36                           /* shown when there is no artwork */
#define COL_SCREEN_BOT  0x07070C
#define COL_TEXT        0xFFFFFF
#define COL_ARTIST      0xD2D2DC
#define COL_TIME        0xB4B4C2
#define COL_INFO        0xB4B4C2
#define COL_OK          0x1ED760
#define COL_WARN        0xFFB020
#define COL_ERROR       0xFF5A5F

#define SLIDER_MAX      1000
#define TICK_PERIOD_MS  250

static struct {
    lv_obj_t *bg_img;
    lv_image_dsc_t default_bg;           /* dithered dark gradient shown when there is no artwork */
    uint16_t *default_bg_px;
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
    /* the play triangle looks off-centre in a circle: nudge it right a little */
    lv_obj_align(s.lbl_play, LV_ALIGN_CENTER, playing ? 0 : 2, 0);
}

static void apply_slider(uint32_t pos_ms)
{
    int32_t v = s.duration_ms ? (int32_t)(((uint64_t)pos_ms * SLIDER_MAX) / s.duration_ms) : 0;
    lv_slider_set_value(s.slider, v, LV_ANIM_OFF);
}

/* The no-artwork background: a vertical gradient baked into an image so it can be ordered-dithered (LVGL's own gradient
 * bands visibly in RGB565). Lives in PSRAM, built once. */
static const uint8_t BAYER4[4][4] = { { 0, 8, 2, 10 }, { 12, 4, 14, 6 }, { 3, 11, 1, 9 }, { 15, 7, 13, 5 } };

static bool make_default_bg(void)
{
    uint16_t *px = heap_caps_aligned_alloc(64, SCR * SCR * 2, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!px) {
        return false;
    }
    const int top[3] = { (COL_SCREEN_TOP >> 16) & 0xFF, (COL_SCREEN_TOP >> 8) & 0xFF, COL_SCREEN_TOP & 0xFF };
    const int bot[3] = { (COL_SCREEN_BOT >> 16) & 0xFF, (COL_SCREEN_BOT >> 8) & 0xFF, COL_SCREEN_BOT & 0xFF };
    for (int y = 0; y < SCR; y++) {
        int t = y * 256 / (SCR - 1);
        int c8[3];
        for (int c = 0; c < 3; c++) {
            c8[c] = top[c] * (256 - t) + bot[c] * t;                 /* value * 256 */
        }
        for (int x = 0; x < SCR; x++) {
            int d = BAYER4[y & 3][x & 3] * 2 - 15;
            int r = c8[0] + d * 64, g = c8[1] + d * 32, b = c8[2] + d * 64;
            r = r < 0 ? 0 : (r > 65535 ? 65535 : r);
            g = g < 0 ? 0 : (g > 65535 ? 65535 : g);
            b = b < 0 ? 0 : (b > 65535 ? 65535 : b);
            px[y * SCR + x] = (uint16_t)(((r >> 11) << 11) | ((g >> 10) << 5) | (b >> 11));
        }
    }
    s.default_bg_px = px;
    memset(&s.default_bg, 0, sizeof s.default_bg);
    s.default_bg.header.magic = LV_IMAGE_HEADER_MAGIC;
    s.default_bg.header.cf = LV_COLOR_FORMAT_RGB565;
    s.default_bg.header.w = SCR;
    s.default_bg.header.h = SCR;
    s.default_bg.header.stride = SCR * 2;
    s.default_bg.data_size = SCR * SCR * 2;
    s.default_bg.data = (const uint8_t *)px;
    return true;
}

static void show_default_bg(void)
{
    if (s.default_bg_px) {
        lv_image_set_src(s.bg_img, &s.default_bg);
        lv_obj_remove_flag(s.bg_img, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_add_flag(s.bg_img, LV_OBJ_FLAG_HIDDEN);
    }
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
        lv_obj_set_size(s.art_box, UI_ART_SIZE, UI_ART_SIZE);
        lv_obj_set_pos(s.art_box, UI_ART_X, UI_ART_Y);
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

static lv_obj_t *make_text_line(lv_obj_t *scr, const lv_font_t *font, uint32_t color, int32_t y)
{
    lv_obj_t *l = make_label(scr, font, color);
    lv_obj_set_width(l, 440);
    lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_CENTER, 0);
    lv_label_set_long_mode(l, LV_LABEL_LONG_MODE_SCROLL_CIRCULAR);
    lv_obj_align(l, LV_ALIGN_TOP_MID, 0, y);
    return l;
}

static lv_obj_t *make_time_label(lv_obj_t *scr, lv_text_align_t align, int32_t x)
{
    lv_obj_t *l = make_label(scr, &lv_font_montserrat_14, COL_TIME);
    lv_obj_set_width(l, TIME_W);
    lv_obj_set_style_text_align(l, align, 0);
    lv_obj_set_pos(l, x, BAR_Y - 8);
    lv_label_set_text(l, "0:00");
    return l;
}

/* A round button without any chrome: transparent unless `fill` is set. */
static lv_obj_t *make_button(lv_obj_t *scr, int32_t d, uint32_t fill, uint32_t fill_pressed, bool filled,
                             const char *symbol, const lv_font_t *font, uint32_t fg, lv_obj_t **label_out)
{
    lv_obj_t *b = lv_button_create(scr);
    lv_obj_set_size(b, d, d);
    lv_obj_set_style_radius(b, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_shadow_width(b, 0, 0);
    lv_obj_set_style_border_width(b, 0, 0);
    lv_obj_set_style_pad_all(b, 0, 0);
    if (filled) {
        lv_obj_set_style_bg_opa(b, LV_OPA_COVER, 0);
        lv_obj_set_style_bg_color(b, lv_color_hex(fill), 0);
        lv_obj_set_style_bg_color(b, lv_color_hex(fill_pressed), LV_STATE_PRESSED);
    } else {
        lv_obj_set_style_bg_opa(b, LV_OPA_TRANSP, 0);
        lv_obj_set_style_opa(b, LV_OPA_50, LV_STATE_PRESSED);
    }
    lv_obj_set_ext_click_area(b, 6);
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
    lv_obj_set_style_bg_color(scr, lv_color_hex(COL_SCREEN_TOP), 0);
    lv_obj_set_style_bg_grad_color(scr, lv_color_hex(COL_SCREEN_BOT), 0);
    lv_obj_set_style_bg_grad_dir(scr, LV_GRAD_DIR_VER, 0);
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);
    lv_obj_remove_flag(scr, LV_OBJ_FLAG_SCROLLABLE);

    /* blurred artwork background (bottom-most), hidden until artwork arrives */
    s.bg_img = lv_image_create(scr);
    lv_obj_set_size(s.bg_img, SCR, SCR);
    lv_obj_set_pos(s.bg_img, 0, 0);
    lv_obj_remove_flag(s.bg_img, LV_OBJ_FLAG_CLICKABLE);
    make_default_bg();
    show_default_bg();

    /* status line */
    s.status = make_label(scr, &lv_font_montserrat_12, COL_INFO);
    lv_obj_set_width(s.status, 460);
    lv_obj_set_style_text_align(s.status, LV_TEXT_ALIGN_CENTER, 0);
    lv_label_set_long_mode(s.status, LV_LABEL_LONG_MODE_DOTS);
    lv_obj_align(s.status, LV_ALIGN_TOP_MID, 0, 2);
    lv_label_set_text(s.status, "");

    /* cover: no frame, no card. The box only exists to round the corners (and to hold the placeholder look) */
    s.art_box = lv_obj_create(scr);
    lv_obj_set_size(s.art_box, UI_ART_SIZE, UI_ART_SIZE);
    lv_obj_set_pos(s.art_box, UI_ART_X, UI_ART_Y);
    lv_obj_remove_flag(s.art_box, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_pad_all(s.art_box, 0, 0);
    lv_obj_set_style_border_width(s.art_box, 0, 0);
    lv_obj_set_style_shadow_width(s.art_box, 0, 0);
    lv_obj_set_style_radius(s.art_box, ART_RADIUS, 0);
    lv_obj_set_style_clip_corner(s.art_box, true, 0);
    lv_obj_set_style_bg_color(s.art_box, lv_color_hex(0xFFFFFF), 0);
    lv_obj_set_style_bg_opa(s.art_box, LV_OPA_10, 0);                 /* placeholder tint; cleared when art arrives */
    lv_obj_add_event_cb(s.art_box, on_art_clicked, LV_EVENT_CLICKED, NULL);

    s.art_icon = make_label(s.art_box, &lv_font_montserrat_48, 0xFFFFFF);
    lv_obj_set_style_text_opa(s.art_icon, LV_OPA_40, 0);
    lv_label_set_text(s.art_icon, LV_SYMBOL_AUDIO);
    lv_obj_center(s.art_icon);

    s.art_img = lv_image_create(s.art_box);
    lv_obj_set_size(s.art_img, LV_PCT(100), LV_PCT(100));
    lv_image_set_inner_align(s.art_img, LV_IMAGE_ALIGN_STRETCH);
    lv_obj_add_flag(s.art_img, LV_OBJ_FLAG_HIDDEN);
    lv_obj_remove_flag(s.art_img, LV_OBJ_FLAG_CLICKABLE);     /* clicks go to the box */

    /* title / artist */
    s.title = make_text_line(scr, &lv_font_montserrat_26, COL_TEXT, TITLE_Y);
    s.artist = make_text_line(scr, &lv_font_montserrat_20, COL_ARTIST, ARTIST_Y);

    /* progress / seek: a thin line, the knob only appears while dragging */
    s.slider = lv_slider_create(scr);
    lv_slider_set_range(s.slider, 0, SLIDER_MAX);
    lv_obj_set_size(s.slider, BAR_W, BAR_H);
    lv_obj_set_pos(s.slider, UI_ART_X, BAR_Y - BAR_H / 2);
    lv_obj_set_ext_click_area(s.slider, 22);
    lv_obj_set_style_bg_color(s.slider, lv_color_hex(0xFFFFFF), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(s.slider, LV_OPA_30, LV_PART_MAIN);
    lv_obj_set_style_bg_color(s.slider, lv_color_hex(0xFFFFFF), LV_PART_INDICATOR);
    lv_obj_set_style_bg_opa(s.slider, LV_OPA_COVER, LV_PART_INDICATOR);
    lv_obj_set_style_bg_color(s.slider, lv_color_hex(0xFFFFFF), LV_PART_KNOB);
    lv_obj_set_style_bg_opa(s.slider, LV_OPA_TRANSP, LV_PART_KNOB);
    lv_obj_set_style_pad_all(s.slider, 0, LV_PART_KNOB);
    lv_obj_set_style_bg_opa(s.slider, LV_OPA_COVER, LV_PART_KNOB | LV_STATE_PRESSED);
    lv_obj_set_style_pad_all(s.slider, 7, LV_PART_KNOB | LV_STATE_PRESSED);
    lv_obj_add_event_cb(s.slider, on_slider_changed, LV_EVENT_VALUE_CHANGED, NULL);
    lv_obj_add_event_cb(s.slider, on_slider_released, LV_EVENT_RELEASED, NULL);

    s.t_elapsed = make_time_label(scr, LV_TEXT_ALIGN_RIGHT, UI_ART_X - 8 - TIME_W);
    s.t_total = make_time_label(scr, LV_TEXT_ALIGN_LEFT, UI_ART_X + BAR_W + 8);

    /* transport controls */
    s.btn_prev = make_button(scr, SIDE_D, 0, 0, false, LV_SYMBOL_PREV, &lv_font_montserrat_24, COL_TEXT, NULL);
    lv_obj_align(s.btn_prev, LV_ALIGN_TOP_MID, -SIDE_DX, CTRL_Y - SIDE_D / 2);
    lv_obj_add_event_cb(s.btn_prev, on_prev, LV_EVENT_CLICKED, NULL);

    s.btn_play = make_button(scr, PLAY_D, 0xFFFFFF, 0xD8D8E0, true, LV_SYMBOL_PLAY, &lv_font_montserrat_32, 0x101015, &s.lbl_play);
    lv_obj_align(s.btn_play, LV_ALIGN_TOP_MID, 0, CTRL_Y - PLAY_D / 2);
    lv_obj_add_event_cb(s.btn_play, on_play, LV_EVENT_CLICKED, NULL);

    s.btn_next = make_button(scr, SIDE_D, 0, 0, false, LV_SYMBOL_NEXT, &lv_font_montserrat_24, COL_TEXT, NULL);
    lv_obj_align(s.btn_next, LV_ALIGN_TOP_MID, SIDE_DX, CTRL_Y - SIDE_D / 2);
    lv_obj_add_event_cb(s.btn_next, on_next, LV_EVENT_CLICKED, NULL);

    lv_label_set_text(s.title, "Starting...");
    lv_label_set_text(s.artist, "");
    set_play_icon(false);

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

static void show_placeholder_art(void)
{
    lv_obj_add_flag(s.art_img, LV_OBJ_FLAG_HIDDEN);
    lv_obj_remove_flag(s.art_icon, LV_OBJ_FLAG_HIDDEN);
    lv_obj_set_style_bg_opa(s.art_box, LV_OPA_10, 0);
    show_default_bg();
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
    show_placeholder_art();
    lv_obj_set_style_opa(s.btn_prev, LV_OPA_40, 0);        /* nothing to control */
    lv_obj_set_style_opa(s.btn_next, LV_OPA_40, 0);
    lv_obj_set_style_opa(s.slider, LV_OPA_50, 0);
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

static void apply_caps(bool can_prev, bool can_next, bool can_seek)
{
    lv_obj_set_style_opa(s.btn_prev, can_prev ? LV_OPA_COVER : LV_OPA_40, 0);
    lv_obj_set_style_opa(s.btn_next, can_next ? LV_OPA_COVER : LV_OPA_40, 0);
    lv_obj_set_style_opa(s.slider, can_seek ? LV_OPA_COVER : LV_OPA_60, 0);
}

void ui_set_capabilities(bool can_prev, bool can_next, bool can_seek)
{
    if (!s.ready || !bsp_display_lock(1000)) {
        return;
    }
    apply_caps(can_prev, can_next, can_seek && s.duration_ms > 0);
    bsp_display_unlock();
}

void ui_set_art(const lv_image_dsc_t *art, const lv_image_dsc_t *bg)
{
    if (!s.ready || !bsp_display_lock(1000)) {
        return;
    }
    if (art) {
        lv_image_set_src(s.art_img, art);
        lv_obj_remove_flag(s.art_img, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(s.art_icon, LV_OBJ_FLAG_HIDDEN);
        lv_obj_set_style_bg_opa(s.art_box, LV_OPA_TRANSP, 0);
        if (bg) {
            lv_image_set_src(s.bg_img, bg);
            lv_obj_remove_flag(s.bg_img, LV_OBJ_FLAG_HIDDEN);
        } else {
            show_default_bg();
        }
    } else {
        show_placeholder_art();
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

#ifdef APP_DEV_TOOLS
void ui_debug_tap(const char *which)
{
    if (!s.ready || !bsp_display_lock(1000)) {
        return;
    }
    lv_obj_t *target = NULL;
    if (strcmp(which, "play") == 0) target = s.btn_play;
    else if (strcmp(which, "next") == 0) target = s.btn_next;
    else if (strcmp(which, "prev") == 0) target = s.btn_prev;
    else if (strcmp(which, "art") == 0) target = s.art_box;
    if (target) {
        lv_obj_send_event(target, LV_EVENT_CLICKED, NULL);
    }
    bsp_display_unlock();
}
#endif

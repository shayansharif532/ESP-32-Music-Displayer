/* Now Playing screen, 480x480.  See ui.h for the threading rules.
 *
 * Look: a blurred, darkened full-screen version of the cover is the background (one pre-baked RGB565 image, see
 * pc_media/pc_art.c); the sharp cover sits on it with no frame or card; title and artist are centred below, then a thin
 * progress line with the times at its ends, and the transport controls (big white play button, plain prev/next icons).
 *
 * The cover keeps its own shape: square art is a large square, 16:9 video thumbnails are a landscape card (rounded
 * corners, never stretched), and the title / artist move with it. Extra touch input (no permanent widgets):
 *   - horizontal swipe on the screen: left = next track, right = previous track (not from the progress bar)
 *   - vertical swipe on the screen: up = next media source (another app's session on the PC), down = previous source;
 *     a short pill shows the source's name (not from the progress bar, nothing happens with a single source)
 *   - a short translucent "Volume 65%" pill on top when the side buttons change the volume
 * Spotify only: two small buttons flank the transport row - shuffle (left of previous) and repeat (right of next; off ->
 * playlist -> track). Subtle when off, Spotify green with a dot when on, a "1" on the repeat icon for "repeat the track".
 */
#include "ui.h"

#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "bsp/esp-bsp.h"
#include "esp_heap_caps.h"

/* ---- layout ------------------------------------------------------------------------------------ */
#define SCR             480
#define ART_RADIUS      14
#define FULL_SIZE       456                                /* longest side of the cover in full-screen mode */
#define SQUARE_MIN_H    (UI_ART_SIZE - 8)                  /* covers at least this tall are laid out like a square */
#define GAP_SQUARE      6                                  /* cover -> title */
#define GAP_LANDSCAPE   18
#define TEXT_BLOCK_H    55                                 /* title (31) + artist (24) */
#define LAYOUT_TOP      22                                 /* landscape card + text are centred between these two lines */
#define LAYOUT_BOTTOM   396
#define BAR_Y           406                                /* vertical centre of the progress line */
#define BAR_X           UI_ART_X
#define BAR_W           UI_ART_SIZE                        /* as wide as a square cover */
#define BAR_H           4
#define TIME_W          58
#define CTRL_Y          446                                /* vertical centre of the control row */
#define PLAY_D          60
#define SIDE_D          64                                 /* touch area of prev / next (no visible circle) */
#define SIDE_DX         112
#define SMALL_D         52                                 /* touch area of the Spotify-only shuffle / repeat buttons */
#define SMALL_DX        178                                /* ... centred this far from the middle */

/* ---- touch gestures ---------------------------------------------------------------------------- */
#define SWIPE_MIN       72      /* px of travel along the swipe axis before it counts as a swipe (a tap moves < 10) */
#define SWIPE_STRAY     44      /* more travel than this across the axis at any time: a diagonal, not a swipe */
#define SWIPE_MAX_MS    1000    /* slower than this: a drag, not a swipe */
#define TAP_SLOP        24      /* a press that moved further than this is not a tap (no full-screen toggle / button press) */
#define FX_SIZE         92
#define FX_MS           420
#define VOL_HOLD_MS     900
#define VOL_FADE_MS     260
#define SRC_HOLD_MS     1100    /* the source pill stays a little longer: it names a whole screen change */
#define SRC_DOTS_MAX    6

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
    lv_obj_t *btn_shuffle, *btn_repeat, *lbl_shuffle, *lbl_repeat, *lbl_repeat1, *dot_shuffle, *dot_repeat;   /* Spotify only */
    lv_obj_t *fx, *fx_lbl;               /* swipe feedback (top-most) */
    lv_obj_t *vol, *vol_lbl, *vol_bar;   /* volume / source pill (top-most) */
    lv_obj_t *dots[SRC_DOTS_MAX];        /* source position dots inside the pill (instead of the volume bar) */
    lv_timer_t *vol_timer;

    ui_cmd_cb_t cb;
    void *ctx;

    bool has_track;
    bool playing;
    bool fullscreen;
    bool can_prev, can_next;
    bool spotify, sp_can_shuffle, sp_can_repeat, sp_shuffle;     /* what the Spotify buttons currently show */
    int sp_repeat;
    int sources;                         /* media sources the PC offers; a vertical swipe needs >= 2 */
    int art_w, art_h;                    /* pixel size of the cover on screen (normal layout) */
    int fx_dx, fx_dy;                    /* direction the swipe disc drifts (-1, 0, +1) */
    uint32_t duration_ms;
    uint32_t anchor_ms;      /* progress at anchor_tick */
    uint32_t anchor_tick;
    int32_t shown_sec;       /* elapsed seconds currently on the label, -1 = none */
    bool ready;
} s;

/* swipe tracking for the current touch (LVGL task only) */
static struct {
    bool tracking;           /* a touch that may still turn into a swipe */
    bool stray_x, stray_y;   /* moved more than SWIPE_STRAY sideways / up-down at some point: that axis can no longer be a swipe's cross-axis */
    bool moved;              /* travelled further than TAP_SLOP: not a tap */
    bool swiped;             /* a swipe was already acted on during this touch */
    int32_t x0, y0;
    uint32_t t0;
} sw;

static atomic_uint_fast32_t s_touches;

/* ---- layout ------------------------------------------------------------------------------------ */

lv_area_t ui_art_area(int w, int h)
{
    if (w < 16 || h < 16) {
        w = h = UI_ART_SIZE;
    }
    if (w > UI_ART_MAX_W) {
        w = UI_ART_MAX_W;
    }
    if (h > UI_ART_SIZE) {
        h = UI_ART_SIZE;
    }
    int y = UI_ART_Y;
    if (h < SQUARE_MIN_H) {              /* shorter than a square: centre card + title + artist between the status line and the bar */
        int group = h + GAP_LANDSCAPE + TEXT_BLOCK_H;
        y = LAYOUT_TOP + (LAYOUT_BOTTOM - LAYOUT_TOP - group) / 2;
        if (y < UI_ART_Y) {
            y = UI_ART_Y;
        }
    }
    int x = (SCR - w) / 2;
    lv_area_t a = { x, y, x + w - 1, y + h - 1 };
    return a;
}

static int title_y_for(const lv_area_t *a)
{
    int h = a->y2 - a->y1 + 1;
    return a->y2 + 1 + (h < SQUARE_MIN_H ? GAP_LANDSCAPE : GAP_SQUARE);
}

/* Positions the cover and the two text lines for the current cover shape / full-screen state. */
static void apply_layout(void)
{
    lv_area_t a = ui_art_area(s.art_w, s.art_h);
    int w = a.x2 - a.x1 + 1, h = a.y2 - a.y1 + 1;
    if (s.fullscreen) {                  /* same aspect ratio, longest side = FULL_SIZE (never stretched) */
        int fw = w >= h ? FULL_SIZE : FULL_SIZE * w / h;
        int fh = w >= h ? FULL_SIZE * h / w : FULL_SIZE;
        lv_obj_set_size(s.art_box, fw, fh);
        lv_obj_set_pos(s.art_box, (SCR - fw) / 2, (SCR - fh) / 2);
    } else {
        lv_obj_set_size(s.art_box, w, h);
        lv_obj_set_pos(s.art_box, a.x1, a.y1);
    }
    int ty = title_y_for(&a);
    lv_obj_align(s.title, LV_ALIGN_TOP_MID, 0, ty);
    lv_obj_align(s.artist, LV_ALIGN_TOP_MID, 0, ty + 31);
}

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

/* A press that travelled (e.g. a swipe that started on a button) must not also act as a tap on that button. */
static void on_prev(lv_event_t *e)  { (void)e; if (!sw.moved) fire(UI_CMD_PREV, 0); }
static void on_play(lv_event_t *e)  { (void)e; if (!sw.moved) fire(UI_CMD_PLAY_PAUSE, 0); }
static void on_next(lv_event_t *e)  { (void)e; if (!sw.moved) fire(UI_CMD_NEXT, 0); }
static void on_shuffle(lv_event_t *e) { (void)e; if (!sw.moved) fire(UI_CMD_SHUFFLE, 0); }
static void on_repeat(lv_event_t *e)  { (void)e; if (!sw.moved) fire(UI_CMD_REPEAT, 0); }

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

static void apply_spotify_buttons(void);

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
    apply_spotify_buttons();                       /* hidden in full screen; otherwise only while the session is Spotify */
}

static void on_art_clicked(lv_event_t *e)
{
    (void)e;
    if (sw.moved) {                      /* a drag that happened to start on the cover is not a tap */
        return;
    }
    s.fullscreen = !s.fullscreen;
    set_chrome_hidden(s.fullscreen);
    apply_layout();
}

static void on_indev_pressed(lv_event_t *e)
{
    (void)e;
    atomic_fetch_add(&s_touches, 1);    /* no logging here: this runs on the LVGL task */
}

/* ---- swipe feedback ------------------------------------------------------------------------------ */

static void fx_anim_cb(void *var, int32_t v)       /* v: 0..255 over the animation */
{
    (void)var;
    lv_obj_set_style_opa(s.fx, (lv_opa_t)(((255 - v) * 210) / 255), 0);
    lv_obj_set_style_translate_x(s.fx, s.fx_dx * (v * 34 / 255), 0);
    lv_obj_set_style_translate_y(s.fx, s.fx_dy * (v * 34 / 255), 0);
}

static void fx_anim_done(lv_anim_t *a)
{
    (void)a;
    lv_obj_add_flag(s.fx, LV_OBJ_FLAG_HIDDEN);
}

/* A faint disc with a symbol appears over the cover and drifts a little in the direction of the swipe. */
static void show_swipe_fx(const char *symbol, int dx, int dy)
{
    s.fx_dx = dx;
    s.fx_dy = dy;
    lv_label_set_text(s.fx_lbl, symbol);
    lv_obj_center(s.fx_lbl);
    lv_area_t a = ui_art_area(s.art_w, s.art_h);
    int cy = s.fullscreen ? SCR / 2 : (a.y1 + a.y2) / 2;
    lv_obj_set_pos(s.fx, (SCR - FX_SIZE) / 2, cy - FX_SIZE / 2);
    lv_anim_delete(NULL, fx_anim_cb);
    lv_obj_remove_flag(s.fx, LV_OBJ_FLAG_HIDDEN);
    fx_anim_cb(NULL, 0);
    lv_anim_t an;
    lv_anim_init(&an);
    lv_anim_set_var(&an, s.fx);
    lv_anim_set_exec_cb(&an, fx_anim_cb);
    lv_anim_set_values(&an, 0, 255);
    lv_anim_set_duration(&an, FX_MS);
    lv_anim_set_path_cb(&an, lv_anim_path_ease_out);
    lv_anim_set_completed_cb(&an, fx_anim_done);
    lv_anim_start(&an);
}

/* ---- swipe detection (screen-level; the progress bar does not bubble its events, so it never starts a swipe) ---- */

static void on_screen_touch(lv_event_t *e)
{
    lv_event_code_t code = lv_event_get_code(e);
    lv_indev_t *indev = lv_event_get_indev(e);
    if (!indev) {
        return;
    }
    lv_point_t p;
    lv_indev_get_point(indev, &p);

    if (code == LV_EVENT_PRESSED) {
        sw.tracking = true;
        sw.stray_x = sw.stray_y = sw.moved = sw.swiped = false;
        sw.x0 = p.x;
        sw.y0 = p.y;
        sw.t0 = lv_tick_get();
        return;
    }
    if (code != LV_EVENT_PRESSING || !sw.tracking) {
        return;
    }
    int32_t dx = p.x - sw.x0, dy = p.y - sw.y0;
    int32_t adx = dx < 0 ? -dx : dx, ady = dy < 0 ? -dy : dy;
    if (adx > TAP_SLOP || ady > TAP_SLOP) {
        sw.moved = true;
    }
    if (adx > SWIPE_STRAY) {
        sw.stray_x = true;
    }
    if (ady > SWIPE_STRAY) {
        sw.stray_y = true;
    }
    if (sw.swiped || lv_tick_elaps(sw.t0) > SWIPE_MAX_MS) {
        return;
    }
    /* A horizontal swipe: far enough sideways, never strayed up/down, and clearly more horizontal than vertical.
     * A vertical swipe is the same with the axes swapped. A diagonal drag strays on both axes and is neither. */
    const bool horizontal = adx >= SWIPE_MIN && !sw.stray_y && ady * 2 <= adx;
    const bool vertical = ady >= SWIPE_MIN && !sw.stray_x && adx * 2 <= ady;
    if (!horizontal && !vertical) {
        return;
    }
    sw.swiped = true;
    sw.tracking = false;
    lv_indev_wait_release(indev);                        /* no release / click on whatever the finger started on */
    if (horizontal) {
        if (dx < 0) {                                    /* swipe left -> next track */
            if (s.can_next) {
                show_swipe_fx(LV_SYMBOL_NEXT, -1, 0);
                fire(UI_CMD_NEXT, 0);
            }
        } else if (s.can_prev) {                         /* swipe right -> previous track */
            show_swipe_fx(LV_SYMBOL_PREV, +1, 0);
            fire(UI_CMD_PREV, 0);
        }
    } else if (s.sources >= 2) {                         /* vertical: another media source exists */
        if (dy < 0) {                                    /* swipe up -> next source */
            show_swipe_fx(LV_SYMBOL_UP, 0, -1);
            fire(UI_CMD_SOURCE_NEXT, 0);
        } else {                                         /* swipe down -> previous source */
            show_swipe_fx(LV_SYMBOL_DOWN, 0, +1);
            fire(UI_CMD_SOURCE_PREV, 0);
        }
    }
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

/* ---- volume pill ------------------------------------------------------------------------------- */

static void vol_fade_cb(void *var, int32_t v)
{
    (void)var;
    lv_obj_set_style_opa(s.vol, (lv_opa_t)v, 0);
}

static void vol_fade_done(lv_anim_t *a)
{
    (void)a;
    lv_obj_add_flag(s.vol, LV_OBJ_FLAG_HIDDEN);
}

static void vol_timer_cb(lv_timer_t *t)
{
    lv_timer_pause(t);
    lv_anim_t an;
    lv_anim_init(&an);
    lv_anim_set_var(&an, s.vol);
    lv_anim_set_exec_cb(&an, vol_fade_cb);
    lv_anim_set_values(&an, LV_OPA_COVER, LV_OPA_TRANSP);
    lv_anim_set_duration(&an, VOL_FADE_MS);
    lv_anim_set_completed_cb(&an, vol_fade_done);
    lv_anim_start(&an);
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
    lv_obj_add_flag(b, LV_OBJ_FLAG_EVENT_BUBBLE);                  /* so a swipe that starts on a button is still seen */
    lv_obj_t *l = make_label(b, font, fg);
    lv_label_set_text(l, symbol);
    lv_obj_center(l);
    if (label_out) {
        *label_out = l;
    }
    return b;
}

/* A 4 px dot under an active Spotify button (so the state is not carried by colour alone). */
static lv_obj_t *make_dot(lv_obj_t *parent)
{
    lv_obj_t *d = lv_obj_create(parent);
    lv_obj_remove_style_all(d);
    lv_obj_set_size(d, 4, 4);
    lv_obj_set_style_radius(d, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(d, lv_color_hex(COL_OK), 0);
    lv_obj_set_style_bg_opa(d, LV_OPA_COVER, 0);
    lv_obj_remove_flag(d, LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_align(d, LV_ALIGN_CENTER, 0, 17);
    lv_obj_add_flag(d, LV_OBJ_FLAG_HIDDEN);
    return d;
}

/* Overlays are the last (= top-most) children of the screen and never clickable, so they never take part in hit-testing. */
static void make_overlays(void)
{
    lv_obj_t *top = lv_screen_active();

    s.fx = lv_obj_create(top);
    lv_obj_remove_style_all(s.fx);
    lv_obj_set_size(s.fx, FX_SIZE, FX_SIZE);
    lv_obj_set_style_radius(s.fx, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(s.fx, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(s.fx, LV_OPA_50, 0);
    lv_obj_remove_flag(s.fx, LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_SCROLLABLE);
    s.fx_lbl = make_label(s.fx, &lv_font_montserrat_32, 0xFFFFFF);
    lv_label_set_text(s.fx_lbl, LV_SYMBOL_NEXT);
    lv_obj_center(s.fx_lbl);
    lv_obj_add_flag(s.fx, LV_OBJ_FLAG_HIDDEN);

    s.vol = lv_obj_create(top);
    lv_obj_remove_style_all(s.vol);
    lv_obj_set_size(s.vol, 236, 62);
    lv_obj_align(s.vol, LV_ALIGN_TOP_MID, 0, 20);                  /* just below the status line */
    lv_obj_set_style_radius(s.vol, 31, 0);
    lv_obj_set_style_bg_color(s.vol, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(s.vol, LV_OPA_60, 0);
    lv_obj_remove_flag(s.vol, LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_SCROLLABLE);
    s.vol_lbl = make_label(s.vol, &lv_font_montserrat_20, 0xFFFFFF);
    lv_obj_set_width(s.vol_lbl, 216);
    lv_obj_set_style_text_align(s.vol_lbl, LV_TEXT_ALIGN_CENTER, 0);
    lv_label_set_long_mode(s.vol_lbl, LV_LABEL_LONG_MODE_DOTS);
    lv_label_set_text(s.vol_lbl, "Volume");
    lv_obj_align(s.vol_lbl, LV_ALIGN_TOP_MID, 0, 10);
    s.vol_bar = lv_bar_create(s.vol);
    lv_obj_remove_style_all(s.vol_bar);
    lv_obj_set_size(s.vol_bar, 168, 4);
    lv_obj_align(s.vol_bar, LV_ALIGN_BOTTOM_MID, 0, -12);
    lv_obj_set_style_radius(s.vol_bar, LV_RADIUS_CIRCLE, LV_PART_MAIN);
    lv_obj_set_style_radius(s.vol_bar, LV_RADIUS_CIRCLE, LV_PART_INDICATOR);
    lv_obj_set_style_bg_color(s.vol_bar, lv_color_white(), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(s.vol_bar, LV_OPA_30, LV_PART_MAIN);
    lv_obj_set_style_bg_color(s.vol_bar, lv_color_white(), LV_PART_INDICATOR);
    lv_obj_set_style_bg_opa(s.vol_bar, LV_OPA_COVER, LV_PART_INDICATOR);
    lv_bar_set_range(s.vol_bar, 0, 100);
    for (int i = 0; i < SRC_DOTS_MAX; i++) {                       /* source position dots, shown instead of the bar */
        s.dots[i] = lv_obj_create(s.vol);
        lv_obj_remove_style_all(s.dots[i]);
        lv_obj_set_size(s.dots[i], 7, 7);
        lv_obj_set_style_radius(s.dots[i], LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_bg_color(s.dots[i], lv_color_white(), 0);
        lv_obj_set_style_bg_opa(s.dots[i], LV_OPA_30, 0);
        lv_obj_remove_flag(s.dots[i], LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_add_flag(s.dots[i], LV_OBJ_FLAG_HIDDEN);
    }
    lv_obj_add_flag(s.vol, LV_OBJ_FLAG_HIDDEN);
    s.vol_timer = lv_timer_create(vol_timer_cb, VOL_HOLD_MS, NULL);
    lv_timer_pause(s.vol_timer);
}

#ifdef APP_DEV_TOOLS
/* A second, virtual pointer device so the dev tools can drive real swipes / taps through the whole LVGL input path. */
static struct { volatile int32_t x, y; volatile bool pressed; } s_vt;

static void vt_read_cb(lv_indev_t *indev, lv_indev_data_t *data)
{
    (void)indev;
    data->point.x = s_vt.x;
    data->point.y = s_vt.y;
    data->state = s_vt.pressed ? LV_INDEV_STATE_PRESSED : LV_INDEV_STATE_RELEASED;
}

void ui_debug_touch(int x, int y, bool pressed)
{
    s_vt.x = x;
    s_vt.y = y;
    s_vt.pressed = pressed;
}
#endif

void ui_init(ui_cmd_cb_t cb, void *ctx)
{
    if (!bsp_display_lock(0)) {
        return;
    }
    memset(&s, 0, sizeof s);
    memset(&sw, 0, sizeof sw);
    s.cb = cb;
    s.ctx = ctx;
    s.shown_sec = -1;
    s.art_w = s.art_h = UI_ART_SIZE;

    lv_obj_t *scr = lv_screen_active();
    lv_obj_set_style_bg_color(scr, lv_color_hex(COL_SCREEN_TOP), 0);
    lv_obj_set_style_bg_grad_color(scr, lv_color_hex(COL_SCREEN_BOT), 0);
    lv_obj_set_style_bg_grad_dir(scr, LV_GRAD_DIR_VER, 0);
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);
    lv_obj_remove_flag(scr, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_event_cb(scr, on_screen_touch, LV_EVENT_PRESSED, NULL);      /* swipe detection (bubbled from the cover / buttons) */
    lv_obj_add_event_cb(scr, on_screen_touch, LV_EVENT_PRESSING, NULL);

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
    lv_obj_add_flag(s.art_box, LV_OBJ_FLAG_EVENT_BUBBLE);
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

    /* title / artist (positions are set by apply_layout) */
    s.title = make_text_line(scr, &lv_font_montserrat_26, COL_TEXT, 0);
    s.artist = make_text_line(scr, &lv_font_montserrat_20, COL_ARTIST, 0);

    /* progress / seek: a thin line, the knob only appears while dragging. Its events do not bubble: a drag that starts
     * here seeks and never changes the track. */
    s.slider = lv_slider_create(scr);
    lv_slider_set_range(s.slider, 0, SLIDER_MAX);
    lv_obj_set_size(s.slider, BAR_W, BAR_H);
    lv_obj_set_pos(s.slider, BAR_X, BAR_Y - BAR_H / 2);
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

    s.t_elapsed = make_time_label(scr, LV_TEXT_ALIGN_RIGHT, BAR_X - 8 - TIME_W);
    s.t_total = make_time_label(scr, LV_TEXT_ALIGN_LEFT, BAR_X + BAR_W + 8);

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

    /* Spotify only: small, subtle shuffle (left of previous) and repeat (right of next); hidden for every other app */
    s.btn_shuffle = make_button(scr, SMALL_D, 0, 0, false, LV_SYMBOL_SHUFFLE, &lv_font_montserrat_20, COL_TEXT, &s.lbl_shuffle);
    lv_obj_align(s.btn_shuffle, LV_ALIGN_TOP_MID, -SMALL_DX, CTRL_Y - SMALL_D / 2);
    lv_obj_add_event_cb(s.btn_shuffle, on_shuffle, LV_EVENT_CLICKED, NULL);
    s.dot_shuffle = make_dot(s.btn_shuffle);

    s.btn_repeat = make_button(scr, SMALL_D, 0, 0, false, LV_SYMBOL_LOOP, &lv_font_montserrat_20, COL_TEXT, &s.lbl_repeat);
    lv_obj_align(s.btn_repeat, LV_ALIGN_TOP_MID, SMALL_DX, CTRL_Y - SMALL_D / 2);
    lv_obj_add_event_cb(s.btn_repeat, on_repeat, LV_EVENT_CLICKED, NULL);
    s.dot_repeat = make_dot(s.btn_repeat);
    s.lbl_repeat1 = make_label(s.btn_repeat, &lv_font_montserrat_14, COL_OK);        /* "1" = repeat the track */
    lv_label_set_text(s.lbl_repeat1, "1");
    lv_obj_align(s.lbl_repeat1, LV_ALIGN_CENTER, 10, -9);
    lv_obj_add_flag(s.lbl_repeat1, LV_OBJ_FLAG_HIDDEN);
    apply_spotify_buttons();

    lv_label_set_text(s.title, "Starting...");
    lv_label_set_text(s.artist, "");
    set_play_icon(false);
    apply_layout();
    make_overlays();

    lv_timer_create(tick_cb, TICK_PERIOD_MS, NULL);

    lv_indev_t *indev = bsp_display_get_input_dev();
    if (indev) {
        lv_indev_add_event_cb(indev, on_indev_pressed, LV_EVENT_PRESSED, NULL);
    }
#ifdef APP_DEV_TOOLS
    lv_indev_t *vt = lv_indev_create();
    lv_indev_set_type(vt, LV_INDEV_TYPE_POINTER);
    lv_indev_set_read_cb(vt, vt_read_cb);
    lv_indev_set_display(vt, lv_display_get_default());
#endif

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
    s.art_w = s.art_h = UI_ART_SIZE;
    apply_layout();
    show_default_bg();
}

void ui_show_idle(const char *line1, const char *line2)
{
    if (!s.ready || !bsp_display_lock(1000)) {
        return;
    }
    s.has_track = false;
    s.playing = false;
    s.sources = 0;
    s.spotify = false;
    apply_spotify_buttons();
    s.can_prev = s.can_next = false;
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
    s.can_prev = can_prev;
    s.can_next = can_next;
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

/* Shows / hides and styles the two Spotify buttons from the stored state. LVGL lock held by the caller (or init time). */
static void apply_spotify_buttons(void)
{
    const bool show = s.spotify && !s.fullscreen;
    lv_obj_t *btns[] = { s.btn_shuffle, s.btn_repeat };
    for (size_t i = 0; i < 2; i++) {
        if (show) {
            lv_obj_remove_flag(btns[i], LV_OBJ_FLAG_HIDDEN);
        } else {
            lv_obj_add_flag(btns[i], LV_OBJ_FLAG_HIDDEN);
        }
    }
    if (!show) {
        return;
    }
    const bool sh_on = s.sp_shuffle, rp_on = s.sp_repeat != 0;
    lv_obj_set_style_text_color(s.lbl_shuffle, lv_color_hex(sh_on ? COL_OK : COL_TEXT), 0);
    lv_obj_set_style_text_opa(s.lbl_shuffle, sh_on ? LV_OPA_COVER : LV_OPA_50, 0);
    if (sh_on) lv_obj_remove_flag(s.dot_shuffle, LV_OBJ_FLAG_HIDDEN); else lv_obj_add_flag(s.dot_shuffle, LV_OBJ_FLAG_HIDDEN);
    lv_obj_set_style_opa(s.btn_shuffle, s.sp_can_shuffle ? LV_OPA_COVER : LV_OPA_30, 0);          /* dimmed when Spotify refuses it */

    lv_obj_set_style_text_color(s.lbl_repeat, lv_color_hex(rp_on ? COL_OK : COL_TEXT), 0);
    lv_obj_set_style_text_opa(s.lbl_repeat, rp_on ? LV_OPA_COVER : LV_OPA_50, 0);
    if (rp_on) lv_obj_remove_flag(s.dot_repeat, LV_OBJ_FLAG_HIDDEN); else lv_obj_add_flag(s.dot_repeat, LV_OBJ_FLAG_HIDDEN);
    if (s.sp_repeat == 2) lv_obj_remove_flag(s.lbl_repeat1, LV_OBJ_FLAG_HIDDEN); else lv_obj_add_flag(s.lbl_repeat1, LV_OBJ_FLAG_HIDDEN);
    lv_obj_set_style_opa(s.btn_repeat, s.sp_can_repeat ? LV_OPA_COVER : LV_OPA_30, 0);
}

void ui_set_spotify_controls(bool spotify, bool can_shuffle, bool can_repeat, bool shuffle, int repeat)
{
    if (repeat < 0 || repeat > 2) {
        repeat = 0;
    }
    if (!s.ready || (s.spotify == spotify && s.sp_can_shuffle == can_shuffle && s.sp_can_repeat == can_repeat &&
                     s.sp_shuffle == shuffle && s.sp_repeat == repeat)) {
        return;                                          /* unchanged: no lock, no redraw */
    }
    if (!bsp_display_lock(1000)) {
        return;
    }
    s.spotify = spotify;
    s.sp_can_shuffle = can_shuffle;
    s.sp_can_repeat = can_repeat;
    s.sp_shuffle = shuffle;
    s.sp_repeat = repeat;
    apply_spotify_buttons();
    bsp_display_unlock();
}

void ui_set_art(const lv_image_dsc_t *art, const lv_image_dsc_t *bg)
{
    if (!s.ready || !bsp_display_lock(1000)) {
        return;
    }
    if (art) {
        s.art_w = art->header.w;
        s.art_h = art->header.h;
        lv_image_set_src(s.art_img, art);
        lv_obj_remove_flag(s.art_img, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(s.art_icon, LV_OBJ_FLAG_HIDDEN);
        lv_obj_set_style_bg_opa(s.art_box, LV_OPA_TRANSP, 0);
        apply_layout();                                  /* square cover or landscape card, with the text following it */
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

void ui_set_sources(int count)
{
    if (!s.ready || !bsp_display_lock(1000)) {
        return;
    }
    s.sources = count;
    bsp_display_unlock();
}

/* Shows the pill (volume or source) and (re)starts its hold-then-fade timer. LVGL lock held by the caller. */
static void pill_show(uint32_t hold_ms)
{
    lv_anim_delete(s.vol, vol_fade_cb);
    lv_obj_set_style_opa(s.vol, LV_OPA_COVER, 0);
    lv_obj_remove_flag(s.vol, LV_OBJ_FLAG_HIDDEN);
    lv_timer_set_period(s.vol_timer, hold_ms);
    lv_timer_reset(s.vol_timer);
    lv_timer_resume(s.vol_timer);
}

void ui_show_source(const char *name, int index, int count)
{
    if (!s.ready || !bsp_display_lock(1000)) {
        return;
    }
    lv_label_set_text(s.vol_lbl, (name && name[0]) ? name : "Source");
    lv_obj_align(s.vol_lbl, LV_ALIGN_TOP_MID, 0, 10);
    lv_obj_add_flag(s.vol_bar, LV_OBJ_FLAG_HIDDEN);
    const int n = count < 1 ? 1 : (count > SRC_DOTS_MAX ? SRC_DOTS_MAX : count);
    const int x0 = (236 - (n * 7 + (n - 1) * 9)) / 2;
    for (int i = 0; i < SRC_DOTS_MAX; i++) {
        if (i < n) {
            lv_obj_set_pos(s.dots[i], x0 + i * 16, 43);
            lv_obj_set_style_bg_opa(s.dots[i], i == index - 1 ? LV_OPA_COVER : LV_OPA_30, 0);
            lv_obj_remove_flag(s.dots[i], LV_OBJ_FLAG_HIDDEN);
        } else {
            lv_obj_add_flag(s.dots[i], LV_OBJ_FLAG_HIDDEN);
        }
    }
    pill_show(SRC_HOLD_MS);
    bsp_display_unlock();
}

void ui_show_volume(int percent, bool muted)
{
    if (!s.ready || !bsp_display_lock(1000)) {
        return;
    }
    char buf[40];
    if (percent < 0) {
        snprintf(buf, sizeof buf, "%s", percent == -2 ? LV_SYMBOL_WARNING "  PC not reachable" : LV_SYMBOL_MUTE "  No volume control");
        lv_bar_set_value(s.vol_bar, 0, LV_ANIM_OFF);
    } else if (muted || percent == 0) {
        snprintf(buf, sizeof buf, LV_SYMBOL_MUTE "  %s", muted ? "Muted" : "Volume 0%");
        lv_bar_set_value(s.vol_bar, 0, LV_ANIM_OFF);
    } else {
        snprintf(buf, sizeof buf, "%s  Volume %d%%", percent < 50 ? LV_SYMBOL_VOLUME_MID : LV_SYMBOL_VOLUME_MAX, percent);
        lv_bar_set_value(s.vol_bar, percent, LV_ANIM_OFF);
    }
    lv_label_set_text(s.vol_lbl, buf);
    lv_obj_align(s.vol_lbl, LV_ALIGN_TOP_MID, 0, 10);
    lv_obj_remove_flag(s.vol_bar, LV_OBJ_FLAG_HIDDEN);
    for (int i = 0; i < SRC_DOTS_MAX; i++) {
        lv_obj_add_flag(s.dots[i], LV_OBJ_FLAG_HIDDEN);
    }
    pill_show(VOL_HOLD_MS);
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
    sw.moved = false;                    /* these taps do not come with a PRESSED event that would reset it */
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

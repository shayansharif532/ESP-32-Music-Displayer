/* The pairing prompt (see ui.h): "Pair this display?", the PC's name, the 6-digit code, Pair / Cancel.
 *
 * A screen of its own (created on first use), loaded over whatever is active with the 350 ms fade the player / idle switching uses.
 * It remembers the screen it covered and returns to it when dismissed. While it is up the screen switching logic in main.c must not
 * steal the display: it calls ui_pairing_defer() instead of loading a screen, which records the screen it wanted; that one is loaded
 * on dismissal. Everything here runs under the LVGL lock (the public calls take it; the button handlers run inside it).
 *
 * The buttons only post the answer through the callback. After a tap the buttons give feedback (Pair: the Pair button is disabled and
 * the line below says to click Pair on the PC, Cancel stays usable; Cancel: both disabled) so a double tap cannot post twice.
 */
#include "ui.h"

#include <stdatomic.h>
#include <string.h>

#include "bsp/esp-bsp.h"

#define SCR_FADE_MS     350

#define COL_TOP         0x1D1D36                            /* the player's background gradient, so the prompt belongs to the app */
#define COL_BOT         0x07070C
#define COL_TITLE       0xFFFFFF
#define COL_NAME        0xD2D2DC
#define COL_TEXT        0xB4B4C2
#define COL_HINT        0x8C90AA
#define COL_OK          0x1ED760
#define COL_OK_PRESSED  0x17B04D
#define COL_ON_OK       0x101015

#define BTN_W           190
#define BTN_H           72                                  /* >= 120 x 64 px touch targets */
#define BTN_Y           270

static struct {
    lv_obj_t *scr, *name, *code, *info, *btn_pair, *btn_cancel;
    lv_obj_t *prev;                                         /* the screen the prompt covered */
    lv_obj_t *target;                                       /* the screen to return to (prev, or what main.c wanted meanwhile) */
    void (*cb)(bool accepted, void *ctx);
    void *ctx;
    bool pair_tapped, cancelled;
} p;

static atomic_bool s_active;

/* The one function a tap on a button (and the development action "pair_accept" / "pair_cancel") goes through. LVGL lock held. */
static void press(bool accept)
{
    if (!p.scr || !atomic_load(&s_active)) {
        return;
    }
    if (accept) {
        if (p.pair_tapped || p.cancelled) {
            return;                                         /* once is enough */
        }
        p.pair_tapped = true;
        lv_obj_add_state(p.btn_pair, LV_STATE_DISABLED);
        lv_label_set_text(p.info, "Now click Pair on your PC");
    } else {
        if (p.cancelled) {
            return;
        }
        p.cancelled = true;
        lv_obj_add_state(p.btn_pair, LV_STATE_DISABLED);
        lv_obj_add_state(p.btn_cancel, LV_STATE_DISABLED);
        lv_label_set_text(p.info, "Cancelling...");
    }
    if (p.cb) {
        p.cb(accept, p.ctx);                                /* posts only: no I/O, no blocking */
    }
}

static void on_pair(lv_event_t *e)
{
    (void)e;
    press(true);
}

static void on_cancel(lv_event_t *e)
{
    (void)e;
    press(false);
}

static lv_obj_t *make_label(lv_obj_t *parent, const lv_font_t *font, uint32_t color, int32_t y, int32_t width)
{
    lv_obj_t *l = lv_label_create(parent);
    lv_obj_set_style_text_font(l, font, 0);
    lv_obj_set_style_text_color(l, lv_color_hex(color), 0);
    lv_obj_set_width(l, width);
    lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_align(l, LV_ALIGN_TOP_MID, 0, y);
    return l;
}

static lv_obj_t *make_button(lv_obj_t *parent, int32_t x, const char *text, bool primary, lv_event_cb_t cb)
{
    lv_obj_t *b = lv_button_create(parent);
    lv_obj_set_size(b, BTN_W, BTN_H);
    lv_obj_set_pos(b, x, BTN_Y);
    lv_obj_set_style_radius(b, 20, 0);
    lv_obj_set_style_shadow_width(b, 0, 0);
    lv_obj_set_style_border_width(b, 0, 0);
    lv_obj_set_style_bg_opa(b, LV_OPA_COVER, 0);
    if (primary) {
        lv_obj_set_style_bg_color(b, lv_color_hex(COL_OK), 0);
        lv_obj_set_style_bg_color(b, lv_color_hex(COL_OK_PRESSED), LV_STATE_PRESSED);
    } else {
        lv_obj_set_style_bg_color(b, lv_color_hex(0xFFFFFF), 0);
        lv_obj_set_style_bg_opa(b, LV_OPA_20, 0);
        lv_obj_set_style_bg_opa(b, LV_OPA_40, LV_STATE_PRESSED);
    }
    lv_obj_set_style_opa(b, LV_OPA_40, LV_STATE_DISABLED);
    lv_obj_set_ext_click_area(b, 6);
    lv_obj_add_event_cb(b, cb, LV_EVENT_CLICKED, NULL);
    lv_obj_t *l = lv_label_create(b);
    lv_obj_set_style_text_font(l, &lv_font_montserrat_26, 0);
    lv_obj_set_style_text_color(l, lv_color_hex(primary ? COL_ON_OK : 0xFFFFFF), 0);
    lv_label_set_text(l, text);
    lv_obj_center(l);
    return b;
}

/* Built once, on first use. LVGL lock held. */
static bool ensure_screen(void)
{
    if (p.scr) {
        return true;
    }
    lv_obj_t *scr = lv_obj_create(NULL);
    if (!scr) {
        return false;
    }
    lv_obj_set_style_bg_color(scr, lv_color_hex(COL_TOP), 0);
    lv_obj_set_style_bg_grad_color(scr, lv_color_hex(COL_BOT), 0);
    lv_obj_set_style_bg_grad_dir(scr, LV_GRAD_DIR_VER, 0);
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);
    lv_obj_remove_flag(scr, LV_OBJ_FLAG_SCROLLABLE);
    p.scr = scr;

    lv_obj_t *title = make_label(scr, &lv_font_montserrat_32, COL_TITLE, 36, 440);
    lv_label_set_text(title, "Pair this display?");

    p.name = make_label(scr, &lv_font_montserrat_20, COL_NAME, 90, 420);
    lv_label_set_long_mode(p.name, LV_LABEL_LONG_MODE_DOTS);
    lv_label_set_text(p.name, "");

    p.code = make_label(scr, &lv_font_montserrat_48, COL_TITLE, 134, 440);
    lv_obj_set_style_text_letter_space(p.code, 3, 0);
    lv_label_set_text(p.code, "--- ---");

    lv_obj_t *line = make_label(scr, &lv_font_montserrat_16, COL_TEXT, 214, 420);
    lv_label_set_text(line, "Check that your PC shows the same code");

    p.btn_cancel = make_button(scr, 40, "Cancel", false, on_cancel);
    p.btn_pair = make_button(scr, 480 - 40 - BTN_W, "Pair", true, on_pair);

    p.info = make_label(scr, &lv_font_montserrat_16, COL_TEXT, 366, 420);
    lv_label_set_text(p.info, "");

    lv_obj_t *hint = make_label(scr, &lv_font_montserrat_14, COL_HINT, 426, 420);
    lv_label_set_text(hint, "No matching code on your PC? Tap Cancel");
    return true;
}

void ui_pairing_show(const char *pc_name, const char *code6, void (*cb)(bool accepted, void *ctx), void *ctx)
{
    if (!bsp_display_lock(1000)) {
        return;
    }
    if (!ensure_screen()) {
        bsp_display_unlock();
        return;
    }
    char grouped[8] = "--- ---";                             /* "NNN NNN" */
    bool digits = code6 && strlen(code6) == 6;
    for (int i = 0; digits && i < 6; i++) {
        digits = code6[i] >= '0' && code6[i] <= '9';
    }
    if (digits) {
        memcpy(grouped, code6, 3);
        grouped[3] = ' ';
        memcpy(grouped + 4, code6 + 3, 3);
        grouped[7] = '\0';
    }
    lv_label_set_text(p.name, pc_name ? pc_name : "");
    lv_label_set_text(p.code, grouped);
    lv_label_set_text(p.info, "");
    lv_obj_remove_state(p.btn_pair, LV_STATE_DISABLED);
    lv_obj_remove_state(p.btn_cancel, LV_STATE_DISABLED);
    p.cb = cb;
    p.ctx = ctx;
    p.pair_tapped = p.cancelled = false;
    if (!atomic_load(&s_active)) {
        p.prev = lv_screen_active();                        /* whatever is showing (a start-up screen included) */
        p.target = p.prev;
        atomic_store(&s_active, true);
        lv_screen_load_anim(p.scr, LV_SCREEN_LOAD_ANIM_FADE_IN, SCR_FADE_MS, 0, false);
    }
    bsp_display_unlock();
}

void ui_pairing_hide(void)
{
    if (!atomic_load(&s_active) || !bsp_display_lock(2000)) {
        return;
    }
    atomic_store(&s_active, false);
    p.cb = NULL;
    p.ctx = NULL;
    lv_obj_t *back = p.target ? p.target : p.prev;
    if (back && back != p.scr) {
        lv_screen_load_anim(back, LV_SCREEN_LOAD_ANIM_FADE_IN, SCR_FADE_MS, 0, false);
    }
    bsp_display_unlock();
}

bool ui_pairing_active(void)
{
    return atomic_load(&s_active);
}

bool ui_pairing_defer(lv_obj_t *target)
{
    if (!atomic_load(&s_active)) {
        return false;
    }
    p.target = target;                                      /* (the caller holds the LVGL lock) */
    return true;
}

#ifdef APP_DEV_TOOLS
void ui_pairing_debug_press(bool accept)
{
    if (!bsp_display_lock(1000)) {
        return;
    }
    press(accept);
    bsp_display_unlock();
}
#endif

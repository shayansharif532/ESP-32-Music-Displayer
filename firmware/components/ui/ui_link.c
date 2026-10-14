/* The connection / onboarding overlay (see ui.h): what the display says while it has no working link to the PC app.
 *
 * A full-screen child of the player screen (not a screen of its own: the player / idle switching in main.c and the pairing prompt keep
 * working unchanged, and the screenshot tool sees it). It swallows touches, so the hidden player controls beneath cannot be pressed. It
 * has three parts: a step strip ("USB - Companion - Paired") that shows how far the setup is, a message (icon, title, text), and a hint.
 * Built on first use; every call takes the LVGL lock itself.
 *
 * The two "get the PC app" screens (install, update) print the project's address (DM_SETUP_ADDRESS); the text tells the person to open it
 * and click Releases. No QR code, no download from the display.
 */
#include <stdio.h>
#include <string.h>

#include "bsp/esp-bsp.h"
#include "ui.h"

#define COL_TOP         0x1D1D36                            /* the player's own gradient, so the overlay belongs to the app */
#define COL_BOT         0x07070C
#define COL_TITLE       0xFFFFFF
#define COL_TEXT        0xB4B4C2
#define COL_HINT        0x8C90AA
#define COL_PENDING     0x5C5F78
#define COL_OK          0x1ED760
#define COL_WARN        0xFFB020
#define COL_ACCENT      0x4DA3FF

typedef enum { ST_PENDING, ST_CURRENT, ST_WARN, ST_DONE } step_t;

static struct {
    lv_obj_t *root, *step[3], *icon, *title, *body, *url, *name, *hint;
    bool built;
    bool visible;
    ui_link_kind_t kind;
} k;

static lv_obj_t *label(lv_obj_t *parent, const lv_font_t *font, uint32_t color, int32_t width)
{
    lv_obj_t *l = lv_label_create(parent);
    lv_obj_set_style_text_font(l, font, 0);
    lv_obj_set_style_text_color(l, lv_color_hex(color), 0);
    lv_obj_set_width(l, width);
    lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_CENTER, 0);
    lv_label_set_long_mode(l, LV_LABEL_LONG_MODE_WRAP);
    return l;
}

static void build(lv_obj_t *player)
{
    k.root = lv_obj_create(player);
    lv_obj_remove_style_all(k.root);
    lv_obj_set_size(k.root, 480, 480);
    lv_obj_set_pos(k.root, 0, 0);
    lv_obj_set_style_bg_color(k.root, lv_color_hex(COL_TOP), 0);
    lv_obj_set_style_bg_grad_color(k.root, lv_color_hex(COL_BOT), 0);
    lv_obj_set_style_bg_grad_dir(k.root, LV_GRAD_DIR_VER, 0);
    lv_obj_set_style_bg_opa(k.root, LV_OPA_COVER, 0);
    lv_obj_remove_flag(k.root, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(k.root, LV_OBJ_FLAG_CLICKABLE);         /* swallows touches; no EVENT_BUBBLE: the player's swipes do not see them */
    lv_obj_add_flag(k.root, LV_OBJ_FLAG_HIDDEN);

    for (int i = 0; i < 3; i++) {
        k.step[i] = label(k.root, &lv_font_montserrat_16, COL_PENDING, 150);
        lv_obj_align(k.step[i], LV_ALIGN_TOP_MID, (i - 1) * 150, 22);
    }
    k.icon = label(k.root, &lv_font_montserrat_48, COL_WARN, 200);
    k.title = label(k.root, &lv_font_montserrat_32, COL_TITLE, 440);
    k.body = label(k.root, &lv_font_montserrat_20, COL_TEXT, 410);
    k.name = label(k.root, &lv_font_montserrat_16, COL_HINT, 400);
    k.url = label(k.root, &lv_font_montserrat_20, COL_ACCENT, 440);
    k.hint = label(k.root, &lv_font_montserrat_14, COL_HINT, 420);
    k.built = true;
}

static void set_step(int i, const char *text, step_t st)
{
    static const uint32_t col[] = { COL_PENDING, COL_TITLE, COL_WARN, COL_OK };
    char buf[40];
    snprintf(buf, sizeof buf, "%s%s", st == ST_DONE ? LV_SYMBOL_OK " " : (st == ST_PENDING ? "" : "> "), text);
    lv_label_set_text(k.step[i], buf);
    lv_obj_set_style_text_color(k.step[i], lv_color_hex(col[st]), 0);
}

static void place(lv_obj_t *o, int32_t y)
{
    lv_obj_align(o, LV_ALIGN_TOP_MID, 0, y);
    lv_obj_remove_flag(o, LV_OBJ_FLAG_HIDDEN);
}

void ui_link_init(lv_obj_t *player_screen)
{
    if (!k.built && player_screen) {
        build(player_screen);                               /* LVGL lock held by ui_init() */
    }
}

void ui_link_show(ui_link_kind_t kind, const char *address, const char *pc_name)
{
    if (!bsp_display_lock(1000)) {
        return;
    }
    if (!k.built) {
        bsp_display_unlock();
        return;
    }
    if (k.visible && k.kind == kind) {
        bsp_display_unlock();                               /* nothing changed: no redraw */
        return;
    }
    k.kind = kind;
    k.visible = true;
    const bool get_app = kind == UI_LINK_COMPANION_MISSING || kind == UI_LINK_UPDATE_COMPANION;

    const char *icon = LV_SYMBOL_USB, *title = "", *body = "", *hint = "";
    uint32_t icon_col = COL_WARN;
    step_t usb = ST_DONE, comp = ST_PENDING, pair = ST_PENDING;
    switch (kind) {
    case UI_LINK_NO_USB_DATA:
        usb = ST_WARN;
        title = "No USB data";
        body = "This cable only supplies power, or it is not connected to a PC. Use a USB cable that carries data and plug it into your PC.";
        hint = "Try another cable, or the display's other USB-C connector";
        break;
    case UI_LINK_USB_CONNECTED:
        comp = ST_CURRENT;
        icon = LV_SYMBOL_REFRESH;
        icon_col = COL_ACCENT;
        title = "USB connected";
        body = "Looking for DeskMedia Companion on your PC...";
        break;
    case UI_LINK_COMPANION_MISSING:
        comp = ST_WARN;
        icon = LV_SYMBOL_DOWNLOAD;
        icon_col = COL_ACCENT;
        title = "Install DeskMedia Companion";
        body = "On your PC, open the address below, click Releases, download the Windows installer and run it.";
        hint = "Already installed? Start DeskMedia Companion from the Start menu.";
        break;
    case UI_LINK_PAIRING_WAIT:
        comp = ST_DONE;
        pair = ST_CURRENT;
        icon = LV_SYMBOL_OK;
        icon_col = COL_OK;
        title = "Pair with your PC";
        body = "Confirm the pairing on your PC. If no window appeared, right-click the DeskMedia icon near the clock and choose Pair a display.";
        break;
    case UI_LINK_PAIRING_LOST:
        comp = ST_DONE;
        pair = ST_WARN;
        icon = LV_SYMBOL_WARNING;
        title = "Pair again";
        body = "This display is not paired with this PC. Confirm the pairing on your PC. If no window appeared, right-click the DeskMedia icon near the clock and choose Pair a display.";
        break;
    case UI_LINK_UPDATE_COMPANION:
        comp = ST_WARN;
        icon = LV_SYMBOL_DOWNLOAD;
        icon_col = COL_ACCENT;
        title = "Update DeskMedia Companion";
        body = "This display needs a newer PC app. Open the address below, click Releases and run the latest installer.";
        break;
    case UI_LINK_UPDATE_FIRMWARE:
        comp = ST_WARN;
        icon = LV_SYMBOL_DOWNLOAD;
        title = "Update the display";
        body = "The PC app is newer than this display's software. Update the display firmware (see the project's update guide).";
        break;
    }
    set_step(0, "USB", usb);
    set_step(1, "Companion", comp);
    set_step(2, "Paired", pair);

    lv_label_set_text(k.title, title);
    lv_label_set_text(k.body, body);
    lv_label_set_text(k.hint, hint);
    lv_label_set_text(k.name, (pc_name && pc_name[0] && kind == UI_LINK_PAIRING_WAIT) ? pc_name : "");
    lv_label_set_text(k.url, (get_app && address) ? address : "");
    lv_obj_set_style_text_color(k.icon, lv_color_hex(icon_col), 0);
    lv_label_set_text(k.icon, icon);

    /* the "get the PC app" screens carry the longest titles ("Install DeskMedia Companion"): a smaller font keeps them on one line; the
     * address ("github.com/<owner>/<repo>") is printed small enough to stay on one line too */
    lv_obj_set_style_text_font(k.title, get_app ? &lv_font_montserrat_24 : &lv_font_montserrat_32, 0);
    lv_obj_set_style_text_font(k.url, &lv_font_montserrat_16, 0);

    place(k.icon, get_app ? 80 : 92);                       /* icon, title, text, (address), (PC name), hint */
    place(k.title, get_app ? 150 : 164);
    place(k.body, get_app ? 194 : 218);
    if (get_app) {
        place(k.url, 306);
    } else {
        lv_obj_add_flag(k.url, LV_OBJ_FLAG_HIDDEN);
    }
    if (k.name && pc_name && pc_name[0] && kind == UI_LINK_PAIRING_WAIT) {
        place(k.name, 330);
    } else {
        lv_obj_add_flag(k.name, LV_OBJ_FLAG_HIDDEN);
    }
    if (hint[0]) {
        place(k.hint, 424);
    } else {
        lv_obj_add_flag(k.hint, LV_OBJ_FLAG_HIDDEN);
    }
    lv_obj_remove_flag(k.root, LV_OBJ_FLAG_HIDDEN);
    lv_obj_move_foreground(k.root);
    bsp_display_unlock();
}

void ui_link_hide(void)
{
    if (!k.visible || !bsp_display_lock(1000)) {
        return;
    }
    k.visible = false;
    if (k.built) {
        lv_obj_add_flag(k.root, LV_OBJ_FLAG_HIDDEN);
    }
    bsp_display_unlock();
}

bool ui_link_visible(void)
{
    return k.visible;
}

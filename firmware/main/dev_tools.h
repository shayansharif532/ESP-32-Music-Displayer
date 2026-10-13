#pragma once

#ifdef APP_DEV_TOOLS
/* Handles a one-shot request from the companion's /debug route: "screenshot", "tap_play", "tap_next", "tap_prev", "tap_art",
 * "pair_accept", "pair_cancel", "pair_shot" (and the swipe_* / touch_* / key_* / overlay_* actions of dev_tools.c). */
void dev_tools_handle(const char *what);
#include "side_keys.h"
/* implemented in main.c: the same path a physical side-button press takes */
void app_side_key(side_key_t key);
#endif

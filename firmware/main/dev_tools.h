#pragma once

#ifdef APP_DEV_TOOLS
/* Handles a one-shot request from the companion's /debug route: "screenshot", "tap_play", "tap_next", "tap_prev", "tap_art". */
void dev_tools_handle(const char *what);
#include "side_keys.h"
/* implemented in main.c: the same path a physical side-button press takes */
void app_side_key(side_key_t key);
#endif

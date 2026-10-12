#pragma once

#ifdef APP_DEV_TOOLS
/* Handles a one-shot request from the companion's /debug route: "screenshot", "tap_play", "tap_next", "tap_prev", "tap_art". */
void dev_tools_handle(const char *what);
#endif

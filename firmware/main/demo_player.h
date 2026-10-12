#pragma once
#include "ui.h"

/* UI-only demo player (no network): fake tracks, working buttons/seek. Used when no developer credentials are built in. */
void demo_player_start(void);
void demo_player_on_ui_cmd(ui_cmd_t cmd, uint32_t arg, void *ctx);

#pragma once

/* Starts a low-priority task that logs one health line every few seconds:
 * uptime, free/min-free internal heap, largest internal block, free PSRAM, LVGL task stack headroom, touch presses.
 * Used for soak tests - a reset shows up as a fresh boot banner, a leak as a falling heap number. */
void health_start(void);

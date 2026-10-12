#pragma once
/*
 * Build-time configuration.
 *
 * Developer credentials come from firmware/secrets.local.h (git-ignored; create it with tools/set-dev-secrets.ps1).
 * Without it the firmware still builds and runs in UI-demo mode (no network). The finished product will take the Wi-Fi
 * credentials from the first-run setup portal and NVS instead (Milestone 11).
 */
#if defined(__has_include)
#  if __has_include("secrets.local.h")
#    include "secrets.local.h"
#  endif
#endif

#ifndef DEV_WIFI_SSID
#define DEV_WIFI_SSID           ""
#endif
#ifndef DEV_WIFI_PASSWORD
#define DEV_WIFI_PASSWORD       ""
#endif
/* Optional: fixed IP of the PC running the companion ("" = find it automatically via the companion's UDP beacon). */
#ifndef DEV_COMPANION_HOST
#define DEV_COMPANION_HOST      ""
#endif

/* LVGL runs on core 1, leaving core 0 to Wi-Fi/lwIP and our network tasks. */
#define LVGL_TASK_STACK_BYTES   (24 * 1024)
#define LVGL_TASK_PRIORITY      4
#define LVGL_TASK_CORE          1

#pragma once
/*
 * Build-time configuration.
 *
 * The product needs no configuration at all: it talks to the PC over the USB cable (nothing to set up) and Wi-Fi stays off. For
 * development a LAN fallback can be compiled in: credentials then come from firmware/secrets.local.h (git-ignored; create it with
 * tools/set-dev-secrets.ps1). Without that file the firmware builds and runs normally, USB only.
 */
#if defined(__has_include) && !defined(APP_NO_WIFI)       /* -DAPP_NO_WIFI=1: ignore secrets.local.h, build the Wi-Fi-less product configuration */
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

/* The address the on-screen "install DeskMedia Companion" message prints: the project's GitHub page, whose Releases hold the Windows
 * installer. A fork can override it: idf.py -DDM_SETUP_ADDRESS=\"example.org/deskmedia\" (see docs/installation.md). */
#ifndef DM_SETUP_ADDRESS
#define DM_SETUP_ADDRESS        "github.com/shayansharif532/deskmedia-display"
#endif

/* LVGL runs on core 1, leaving core 0 to Wi-Fi/lwIP and our network tasks. */
#define LVGL_TASK_STACK_BYTES   (24 * 1024)
#define LVGL_TASK_PRIORITY      4
#define LVGL_TASK_CORE          1

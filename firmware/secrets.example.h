#pragma once
/*
 * TEMPORARY developer credentials - template only. Never put real values in this file.
 *
 * Run  tools\set-dev-secrets.ps1  (it asks for the values and writes firmware\secrets.local.h), or copy this file
 * to firmware\secrets.local.h by hand. secrets.local.h is git-ignored and is compiled into the dev firmware only;
 * the finished product takes Wi-Fi / Spotify setup from the first-run portal (Milestone 11) and keeps it in NVS.
 */

#define DEV_WIFI_SSID           ""      /* 2.4 GHz network name (the ESP32-S3 has no 5 GHz radio) */
#define DEV_WIFI_PASSWORD       ""
#define DEV_SPOTIFY_CLIENT_ID   ""      /* Client ID of your Spotify app - not secret under PKCE */

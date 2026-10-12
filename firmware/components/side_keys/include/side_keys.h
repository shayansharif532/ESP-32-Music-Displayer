#pragma once
/*
 * The two physical side buttons of the Waveshare ESP32-S3-Touch-LCD-4B, used as volume up / down.
 *
 * Hardware (schematic page 1, "KEYS" block, and the PCB layout on page 2):
 *   - PWR  = Key3, wired ONLY to the AXP2101 PWRON pin (10 k pull-up). It is not connected to any ESP32 pin, and the AXP2101's
 *            IRQ pin is not routed to the ESP32 either, so the key is read by polling the PMIC's interrupt-status register over
 *            I2C (address 0x34, INTSTS2 = 0x49: bit0 PWRON rising edge, bit1 falling edge, bit2 long press, bit3 short press).
 *   - BOOT = Key1, wired to GPIO0 (10 k pull-up, 100 nF). A normal input once the app runs; held while the chip resets it
 *            selects the ROM download mode, which is unaffected by this driver.
 * On the PCB (viewed from the back, the display's FPC at the bottom) PWR sits above BOOT on the same board edge, so with the
 * display upright PWR is the upper button and BOOT the lower one.
 *
 * Safety: the driver only reads the PMIC and sets the key-IRQ enable bits (INTEN2 bits 0..3). It never touches the PMIC's
 * power-off / long-press configuration. A very long PWR press (about 6 s) is handled by the PMIC itself and powers the board
 * off; hold-repeat therefore stops after SIDE_KEYS_HOLD_MAX_MS so that normal use never gets near it.
 */
#include "esp_err.h"

/* Which physical key is "up". 1: PWR = volume up (upper), BOOT = volume down (lower). Set 0 to swap them. */
#ifndef SIDE_KEYS_UPPER_IS_PWR
#define SIDE_KEYS_UPPER_IS_PWR      1
#endif

#define SIDE_KEYS_REPEAT_AFTER_MS   500     /* holding a key repeats after this long ... */
#define SIDE_KEYS_REPEAT_EVERY_MS   220     /* ... then every this many ms (about 4 steps per second) ... */
#define SIDE_KEYS_HOLD_MAX_MS       3000    /* ... but never longer than this (the PMIC powers off at ~6 s) */

typedef enum {
    SIDE_KEY_UP,        /* upper button */
    SIDE_KEY_DOWN,      /* lower button */
} side_key_t;

/* Called from the side_keys task (never from LVGL): once per press, and repeatedly while the key stays held. Must not block. */
typedef void (*side_key_cb_t)(side_key_t key, void *ctx);

esp_err_t side_keys_start(side_key_cb_t cb, void *ctx);

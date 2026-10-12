#include <stdbool.h>
#include <stdint.h>

#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "bsp/esp-bsp.h"
#include "side_keys.h"

static const char *TAG = "side_keys";

#define BOOT_GPIO           GPIO_NUM_0
#define POLL_MS             20

#define AXP_ADDR            0x34
#define AXP_REG_IC_TYPE     0x03        /* reads 0x4A on an AXP2101 */
#define AXP_CHIP_ID         0x4A
#define AXP_REG_INTEN2      0x41
#define AXP_REG_INTSTS2     0x49
#define AXP_PKEY_POS        0x01        /* PWRON rising edge  */
#define AXP_PKEY_NEG        0x02        /* PWRON falling edge */
#define AXP_PKEY_LONG       0x04
#define AXP_PKEY_SHORT      0x08
#define AXP_PKEY_MASK       0x0F
#define AXP_LOST_RELEASE_MS 6500        /* no release edge for this long: assume it was missed and start over */

#if SIDE_KEYS_UPPER_IS_PWR
#define KEY_FOR_PWR         SIDE_KEY_UP
#define KEY_FOR_BOOT        SIDE_KEY_DOWN
#else
#define KEY_FOR_PWR         SIDE_KEY_DOWN
#define KEY_FOR_BOOT        SIDE_KEY_UP
#endif

typedef struct {
    bool down;
    int64_t t_down_ms;
    int64_t t_next_ms;
    side_key_t key;
    const char *name;
} key_t_;

static struct {
    side_key_cb_t cb;
    void *ctx;
    i2c_master_dev_handle_t axp;
    bool axp_ok;
    bool pwr_edges_seen;            /* the PMIC reports press / release edges (otherwise fall back to the short-press flag) */
    key_t_ pwr, boot;
    bool boot_armed;                /* BOOT must be seen released once first (it may be held while the board resets) */
    int boot_raw_prev;
    int boot_stable;
} s;

static int64_t now_ms(void)
{
    return esp_timer_get_time() / 1000;
}

static void emit(key_t_ *k, const char *why)
{
    ESP_LOGI(TAG, "%s key %s -> volume %s", k->name, why, k->key == SIDE_KEY_UP ? "up" : "down");
    if (s.cb) {
        s.cb(k->key, s.ctx);
    }
}

static void press(key_t_ *k, int64_t now)
{
    k->down = true;
    k->t_down_ms = now;
    k->t_next_ms = now + SIDE_KEYS_REPEAT_AFTER_MS;
    emit(k, "pressed");
}

static void repeat_if_held(key_t_ *k, int64_t now)
{
    if (k->down && now >= k->t_next_ms && now - k->t_down_ms < SIDE_KEYS_HOLD_MAX_MS) {
        k->t_next_ms += SIDE_KEYS_REPEAT_EVERY_MS;
        emit(k, "held");
    }
}

/* ---- BOOT (GPIO0, active low) -------------------------------------------------------------------------- */

static void poll_boot(int64_t now)
{
    int raw = gpio_get_level(BOOT_GPIO);               /* 1 = released, 0 = pressed */
    if (raw == s.boot_raw_prev) {
        if (s.boot_stable < 1000) {
            s.boot_stable++;
        }
    } else {
        s.boot_raw_prev = raw;
        s.boot_stable = 0;
    }
    if (s.boot_stable < 2) {                            /* two equal samples in a row (40 ms): debounced */
        repeat_if_held(&s.boot, now);
        return;
    }
    bool pressed = (raw == 0);
    if (!s.boot_armed) {
        s.boot_armed = !pressed;
        return;
    }
    if (pressed && !s.boot.down) {
        press(&s.boot, now);
    } else if (!pressed && s.boot.down) {
        s.boot.down = false;
    }
    repeat_if_held(&s.boot, now);
}

/* ---- PWR (AXP2101 key edges over I2C) -------------------------------------------------------------------- */

static esp_err_t axp_read(uint8_t reg, uint8_t *val)
{
    return i2c_master_transmit_receive(s.axp, &reg, 1, val, 1, 30);
}

static esp_err_t axp_write(uint8_t reg, uint8_t val)
{
    uint8_t buf[2] = { reg, val };
    return i2c_master_transmit(s.axp, buf, sizeof buf, 30);
}

static void poll_pwr(int64_t now)
{
    uint8_t sts;
    if (axp_read(AXP_REG_INTSTS2, &sts) != ESP_OK) {
        return;                                          /* a lost poll is harmless; the next one catches the flags */
    }
    uint8_t ev = sts & AXP_PKEY_MASK;
    if (ev) {
        axp_write(AXP_REG_INTSTS2, ev);                  /* write-1-to-clear, only the key flags */
        ESP_LOGI(TAG, "AXP2101 key flags 0x%02X (1 = rising edge, 2 = falling edge, 4 = long, 8 = short)", ev);
    }
    if (ev & (AXP_PKEY_POS | AXP_PKEY_NEG)) {
        s.pwr_edges_seen = true;
        if ((ev & (AXP_PKEY_POS | AXP_PKEY_NEG)) == (AXP_PKEY_POS | AXP_PKEY_NEG)) {
            /* press and release inside one poll interval: a quick tap */
            if (!s.pwr.down) {
                emit(&s.pwr, "tapped");
            }
            s.pwr.down = false;
        } else if (!s.pwr.down) {
            press(&s.pwr, now);                          /* the first edge of a touch is the press, whichever polarity it has */
        } else {
            s.pwr.down = false;                          /* the next edge is the release */
        }
    } else if ((ev & AXP_PKEY_SHORT) && !s.pwr_edges_seen && !s.pwr.down) {
        emit(&s.pwr, "short-press flag");               /* fallback if this PMIC revision never reports the edges */
    }
    if (s.pwr.down && now - s.pwr.t_down_ms > AXP_LOST_RELEASE_MS) {
        s.pwr.down = false;                              /* release edge missed */
    }
    repeat_if_held(&s.pwr, now);
}

static void keys_task(void *arg)
{
    (void)arg;
    for (;;) {
        int64_t now = now_ms();
        poll_boot(now);
        if (s.axp_ok) {
            poll_pwr(now);
        }
        vTaskDelay(pdMS_TO_TICKS(POLL_MS));
    }
}

static void axp_init(void)
{
    i2c_master_bus_handle_t bus = bsp_i2c_get_handle();     /* the shared bus (GPIO47/48) that the touch controller uses */
    if (!bus || i2c_master_probe(bus, AXP_ADDR, 50) != ESP_OK) {
        ESP_LOGW(TAG, "AXP2101 not found at 0x%02X: the PWR key is not available (BOOT still is)", AXP_ADDR);
        return;
    }
    const i2c_device_config_t dev = { .dev_addr_length = I2C_ADDR_BIT_LEN_7, .device_address = AXP_ADDR, .scl_speed_hz = 400000 };
    if (i2c_master_bus_add_device(bus, &dev, &s.axp) != ESP_OK) {
        ESP_LOGW(TAG, "could not add the AXP2101 to the I2C bus");
        return;
    }
    uint8_t id = 0, en = 0;
    if (axp_read(AXP_REG_IC_TYPE, &id) != ESP_OK || id != AXP_CHIP_ID) {
        ESP_LOGW(TAG, "device at 0x%02X answered with id 0x%02X, expected AXP2101 (0x%02X): PWR key disabled", AXP_ADDR, id, AXP_CHIP_ID);
        return;
    }
    if (axp_read(AXP_REG_INTEN2, &en) != ESP_OK || axp_write(AXP_REG_INTEN2, en | AXP_PKEY_MASK) != ESP_OK) {
        ESP_LOGW(TAG, "could not enable the AXP2101 key interrupts: PWR key disabled");
        return;
    }
    axp_write(AXP_REG_INTSTS2, AXP_PKEY_MASK);                  /* forget anything pressed before we started */
    s.axp_ok = true;
    ESP_LOGI(TAG, "AXP2101 found (id 0x%02X), key interrupts enabled (INTEN2 0x%02X -> 0x%02X)", id, en, en | AXP_PKEY_MASK);
}

esp_err_t side_keys_start(side_key_cb_t cb, void *ctx)
{
    s.cb = cb;
    s.ctx = ctx;
    s.pwr = (key_t_){ .key = KEY_FOR_PWR, .name = "PWR" };
    s.boot = (key_t_){ .key = KEY_FOR_BOOT, .name = "BOOT" };

    gpio_config_t io = {
        .pin_bit_mask = 1ULL << BOOT_GPIO,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,                       /* there is also a 10 k pull-up on the board */
    };
    esp_err_t err = gpio_config(&io);
    if (err != ESP_OK) {
        return err;
    }
    s.boot_raw_prev = gpio_get_level(BOOT_GPIO);
    axp_init();
    ESP_LOGI(TAG, "volume keys: upper = %s, lower = %s", SIDE_KEYS_UPPER_IS_PWR ? "PWR" : "BOOT", SIDE_KEYS_UPPER_IS_PWR ? "BOOT" : "PWR");
    return xTaskCreatePinnedToCore(keys_task, "side_keys", 3072, NULL, 4, NULL, 0) == pdPASS ? ESP_OK : ESP_ERR_NO_MEM;
}

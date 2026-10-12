#include "health.h"

#include <inttypes.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "ui.h"

static const char *TAG = "health";

#define HEALTH_PERIOD_MS 10000

static void health_task(void *arg)
{
    (void)arg;
    TaskHandle_t lvgl = NULL, poll = NULL;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(HEALTH_PERIOD_MS));
        if (!lvgl) {
            lvgl = xTaskGetHandle("taskLVGL");
        }
        if (!poll) {
            poll = xTaskGetHandle("pc_poll");
        }
        uint32_t lvgl_hw = lvgl ? (uint32_t)uxTaskGetStackHighWaterMark(lvgl) * sizeof(StackType_t) : 0;
        uint32_t poll_hw = poll ? (uint32_t)uxTaskGetStackHighWaterMark(poll) * sizeof(StackType_t) : 0;
        ESP_LOGI(TAG, "up=%" PRIu32 "s touches=%" PRIu32 " int_free=%u int_min=%u int_big=%u psram_free=%u lvgl_stack_free_min=%" PRIu32 " poll_stack_free_min=%" PRIu32,
                 (uint32_t)(esp_timer_get_time() / 1000000), ui_touch_count(),
                 (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
                 (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL),
                 (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL),
                 (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM),
                 lvgl_hw, poll_hw);
    }
}

void health_start(void)
{
    xTaskCreatePinnedToCore(health_task, "health", 3072, NULL, 1, NULL, 0);
}

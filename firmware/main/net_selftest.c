/*
 * Milestone 4 check: outbound HTTPS with certificate validation (needs a correct clock) and a harmless request.
 * GET https://api.spotify.com/v1/ with no token must answer with a JSON error (401/404) - proving DNS, TCP, TLS,
 * the certificate bundle and the HTTP client all work. Runs once per boot and only logs.
 */
#include <inttypes.h>
#include <time.h>

#include "esp_crt_bundle.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "net_selftest.h"
#include "wifi_mgr.h"

static const char *TAG = "https_test";

static void https_test_task(void *arg)
{
    (void)arg;
    while (!wifi_mgr_wait_connected(1000)) {}
    if (!wifi_mgr_wait_time(20000)) {
        ESP_LOGW(TAG, "clock not synchronised after 20 s - TLS certificate checks will fail");
    }
    time_t now = time(NULL);
    struct tm tm;
    gmtime_r(&now, &tm);
    ESP_LOGI(TAG, "clock: %04d-%02d-%02d %02d:%02d:%02d UTC", tm.tm_year + 1900, tm.tm_mon + 1, tm.tm_mday,
             tm.tm_hour, tm.tm_min, tm.tm_sec);

    esp_http_client_config_t cfg = {
        .url = "https://api.spotify.com/v1/",
        .crt_bundle_attach = esp_crt_bundle_attach,
        .timeout_ms = 15000,
    };
    size_t int_before = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
    int64_t t0 = esp_timer_get_time();
    esp_http_client_handle_t c = esp_http_client_init(&cfg);
    esp_err_t err = c ? esp_http_client_perform(c) : ESP_ERR_NO_MEM;
    int ms = (int)((esp_timer_get_time() - t0) / 1000);
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "HTTPS OK: HTTP %d, %" PRId64 " bytes, %d ms (TLS handshake + request)",
                 esp_http_client_get_status_code(c), esp_http_client_get_content_length(c), ms);
    } else {
        ESP_LOGE(TAG, "HTTPS FAILED after %d ms: %s", ms, esp_err_to_name(err));
    }
    if (c) {
        esp_http_client_cleanup(c);
    }
    ESP_LOGI(TAG, "internal heap during request: %u free before, %u free after", (unsigned)int_before,
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
    vTaskDelete(NULL);
}

void net_selftest_start(void)
{
    xTaskCreatePinnedToCore(https_test_task, "https_test", 8192, NULL, 2, NULL, 0);
}

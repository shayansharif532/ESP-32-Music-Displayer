#include "wifi_mgr.h"

#include <inttypes.h>
#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"

static const char *TAG = "wifi";

#define BIT_CONNECTED   BIT0
#define BIT_TIME        BIT1

/* Back-off between reconnect attempts (ms). The last value repeats forever. */
static const uint32_t BACKOFF_MS[] = { 1000, 2000, 4000, 8000, 15000, 30000 };
#define BACKOFF_STEPS (sizeof BACKOFF_MS / sizeof BACKOFF_MS[0])

static struct {
    bool inited;
    bool started;
    bool sntp_started;
    EventGroupHandle_t events;
    esp_timer_handle_t retry_timer;
    wifi_mgr_cb_t cb;
    void *cb_ctx;
    volatile wifi_mgr_state_t state;
    unsigned attempt;                 /* consecutive failures since last IP */
    char ip[16];
} s;

static void set_state(wifi_mgr_state_t st)
{
    if (s.state == st) {
        return;
    }
    s.state = st;
    if (s.cb) {
        s.cb(st, s.cb_ctx);
    }
}

static void retry_timer_cb(void *arg)
{
    (void)arg;
    set_state(WIFI_MGR_CONNECTING);
    esp_err_t err = esp_wifi_connect();
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "esp_wifi_connect failed: %s", esp_err_to_name(err));
    }
}

static void schedule_retry(void)
{
    uint32_t ms = BACKOFF_MS[s.attempt < BACKOFF_STEPS ? s.attempt : BACKOFF_STEPS - 1];
    if (s.attempt < 1000) {
        s.attempt++;
    }
    set_state(WIFI_MGR_WAITING_RETRY);
    ESP_LOGI(TAG, "retrying in %" PRIu32 " ms (attempt %u)", ms, s.attempt);
    esp_timer_stop(s.retry_timer);
    esp_timer_start_once(s.retry_timer, (uint64_t)ms * 1000);
}

static void on_sntp_sync(struct timeval *tv)
{
    (void)tv;
    xEventGroupSetBits(s.events, BIT_TIME);
    ESP_LOGI(TAG, "time synchronised (SNTP)");
}

static void start_sntp_once(void)
{
    if (s.sntp_started) {
        return;
    }
    esp_sntp_config_t cfg = ESP_NETIF_SNTP_DEFAULT_CONFIG_MULTIPLE(2, ESP_SNTP_SERVER_LIST("pool.ntp.org", "time.google.com"));
    cfg.sync_cb = on_sntp_sync;
    if (esp_netif_sntp_init(&cfg) == ESP_OK) {
        s.sntp_started = true;
    } else {
        ESP_LOGW(TAG, "SNTP init failed");
    }
}

static void on_wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg; (void)base;
    switch (id) {
    case WIFI_EVENT_STA_START:
        set_state(WIFI_MGR_CONNECTING);
        esp_wifi_connect();
        break;
    case WIFI_EVENT_STA_CONNECTED:
        ESP_LOGI(TAG, "associated");
        break;
    case WIFI_EVENT_STA_DISCONNECTED: {
        const wifi_event_sta_disconnected_t *d = data;
        xEventGroupClearBits(s.events, BIT_CONNECTED);
        s.ip[0] = '\0';
        if (!s.started) {
            break;
        }
        ESP_LOGW(TAG, "link down (reason %d)%s", d->reason,
                 (d->reason == WIFI_REASON_AUTH_FAIL || d->reason == WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT) ? " - wrong password?" : "");
        schedule_retry();
        break;
    }
    default:
        break;
    }
}

static void on_ip_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg; (void)base;
    if (id == IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *e = data;
        snprintf(s.ip, sizeof s.ip, IPSTR, IP2STR(&e->ip_info.ip));
        s.attempt = 0;
        ESP_LOGI(TAG, "connected, ip %s", s.ip);
        xEventGroupSetBits(s.events, BIT_CONNECTED);
        start_sntp_once();
        set_state(WIFI_MGR_CONNECTED);
    } else if (id == IP_EVENT_STA_LOST_IP) {
        ESP_LOGW(TAG, "lost IP address");
        xEventGroupClearBits(s.events, BIT_CONNECTED);
        s.ip[0] = '\0';
    }
}

esp_err_t wifi_mgr_init(wifi_mgr_cb_t cb, void *ctx)
{
    if (s.inited) {
        return ESP_OK;
    }
    s.cb = cb;
    s.cb_ctx = ctx;
    s.events = xEventGroupCreate();
    if (!s.events) {
        return ESP_ERR_NO_MEM;
    }
    const esp_timer_create_args_t targs = { .callback = retry_timer_cb, .name = "wifi_retry" };
    ESP_ERROR_CHECK_WITHOUT_ABORT(esp_timer_create(&targs, &s.retry_timer));

    ESP_ERROR_CHECK(esp_netif_init());
    esp_err_t err = esp_event_loop_create_default();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        return err;
    }
    esp_netif_t *netif = esp_netif_create_default_wifi_sta();
    esp_netif_set_hostname(netif, "spotify-desk");

    wifi_init_config_t wcfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&wcfg));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));      /* credentials stay in RAM, not in flash */

    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_wifi_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, ESP_EVENT_ANY_ID, on_ip_event, NULL));
    s.inited = true;
    return ESP_OK;
}

esp_err_t wifi_mgr_connect(const char *ssid, const char *password)
{
    if (!s.inited || !ssid || !*ssid) {
        return ESP_ERR_INVALID_ARG;
    }
    wifi_config_t cfg = { 0 };
    strlcpy((char *)cfg.sta.ssid, ssid, sizeof cfg.sta.ssid);
    if (password && *password) {
        strlcpy((char *)cfg.sta.password, password, sizeof cfg.sta.password);
        cfg.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;          /* accepts WPA2 and WPA3 networks */
    } else {
        cfg.sta.threshold.authmode = WIFI_AUTH_OPEN;
    }
    cfg.sta.pmf_cfg.capable = true;
    cfg.sta.pmf_cfg.required = false;
    cfg.sta.sae_pwe_h2e = WPA3_SAE_PWE_BOTH;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &cfg));
    memset(&cfg, 0, sizeof cfg);                                   /* do not leave the password on the stack */
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));                /* mains powered: lowest latency, steadier TLS */
    s.started = true;
    s.attempt = 0;
    return esp_wifi_start();
}

wifi_mgr_state_t wifi_mgr_state(void) { return s.state; }

bool wifi_mgr_is_connected(void)
{
    return s.events && (xEventGroupGetBits(s.events) & BIT_CONNECTED);
}

bool wifi_mgr_wait_connected(uint32_t timeout_ms)
{
    return s.events && (xEventGroupWaitBits(s.events, BIT_CONNECTED, pdFALSE, pdTRUE, pdMS_TO_TICKS(timeout_ms)) & BIT_CONNECTED);
}

bool wifi_mgr_time_synced(void)
{
    return s.events && (xEventGroupGetBits(s.events) & BIT_TIME);
}

bool wifi_mgr_wait_time(uint32_t timeout_ms)
{
    return s.events && (xEventGroupWaitBits(s.events, BIT_TIME, pdFALSE, pdTRUE, pdMS_TO_TICKS(timeout_ms)) & BIT_TIME);
}

int wifi_mgr_rssi(void)
{
    wifi_ap_record_t ap;
    return (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) ? ap.rssi : 0;
}

const char *wifi_mgr_ip(void) { return s.ip; }

void wifi_mgr_debug_drop_link(void)
{
    ESP_LOGW(TAG, "self-test: dropping the link now");
    esp_wifi_disconnect();            /* raises STA_DISCONNECTED -> same recovery path as a vanished router */
}

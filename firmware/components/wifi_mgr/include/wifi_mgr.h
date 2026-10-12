#pragma once
/*
 * Station-mode Wi-Fi with automatic reconnect (exponential back-off) and SNTP time sync.
 * Credentials are passed in by the caller and kept in RAM only (nothing is written to flash by the Wi-Fi driver).
 * Neither the SSID nor the password is ever logged.
 */
#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

typedef enum {
    WIFI_MGR_IDLE = 0,          /* not started */
    WIFI_MGR_CONNECTING,        /* association / DHCP in progress */
    WIFI_MGR_CONNECTED,         /* has an IP address */
    WIFI_MGR_WAITING_RETRY,     /* link lost, back-off timer running */
} wifi_mgr_state_t;

/* Called on every state change from the ESP event task / a timer: keep it short, never block. */
typedef void (*wifi_mgr_cb_t)(wifi_mgr_state_t state, void *ctx);

esp_err_t wifi_mgr_init(wifi_mgr_cb_t cb, void *ctx);
esp_err_t wifi_mgr_connect(const char *ssid, const char *password);   /* then reconnects forever by itself */

wifi_mgr_state_t wifi_mgr_state(void);
bool wifi_mgr_is_connected(void);
bool wifi_mgr_wait_connected(uint32_t timeout_ms);
bool wifi_mgr_time_synced(void);                      /* SNTP has set the clock (needed for TLS certificate checks) */
bool wifi_mgr_wait_time(uint32_t timeout_ms);
int  wifi_mgr_rssi(void);                             /* dBm, 0 if not connected */
const char *wifi_mgr_ip(void);                        /* dotted quad, "" if not connected */

/* Self-test helper: drop the link as if the router had vanished; the manager must bring it back. */
void wifi_mgr_debug_drop_link(void);

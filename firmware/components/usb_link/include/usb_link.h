#pragma once
/*
 * The display's end of the USB link (docs/usb-protocol.md): a CDC-ACM serial port on the ESP32-S3's native USB-Serial/JTAG controller,
 * carrying DMLINK frames (dmlink.h). Nothing here knows what the requests mean: pc_media sends REQUEST frames (the routes of the
 * companion API) and gets the answers; this component only moves frames, keeps the presence handshake (BEACON / ACK) and forwards the
 * firmware log as LOG frames.
 *
 * Threads: usb_link_request() is called by the single poll task; everything else is thread-safe. Nothing here ever blocks the LVGL
 * task or any other caller for more than the time stated, and nothing blocks at all while no host is attached.
 *
 * The port is shared with nothing: the firmware console is on UART0 (sdkconfig), so no log line can interleave with a frame.
 */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

#define USB_LINK_API            2           /* the routes / JSON of docs/protocol.md this firmware speaks ... */
#define USB_LINK_MIN_API        2           /* ... and the oldest companion API it still understands */

/* A growable response body (PSRAM, NUL-terminated); `max` bounds it. The same type the LAN client of pc_media fills. */
typedef struct {
    char  *data;
    size_t len;
    size_t cap;
    size_t max;
    bool   truncated;
} usb_link_buf_t;

bool usb_link_buf_append(usb_link_buf_t *b, const void *data, size_t n);
bool usb_link_buf_reserve(usb_link_buf_t *b, size_t total);

typedef struct {
    uint8_t     dev_id[8];                  /* the display's persistent identity */
    const char *fw_version;                 /* "1.0.0" */
    bool      (*is_paired)(void);           /* a pairing key is stored (the BEACON says so) */
} usb_link_config_t;

/* Installs the USB-Serial/JTAG driver and starts the receive and presence tasks. */
esp_err_t usb_link_start(const usb_link_config_t *cfg);

typedef enum {
    USB_LINK_NO_HOST,           /* no USB host data connection: a charge-only cable, a charger, or nothing plugged in */
    USB_LINK_NO_COMPANION,      /* a host is there but no companion has answered (not installed / not running yet) */
    USB_LINK_COMPANION,         /* a companion answered within the last 6 s */
} usb_link_state_t;

usb_link_state_t usb_link_state(void);

typedef struct {
    char    name[48];           /* the PC's name, printable ASCII */
    char    app[16];            /* the companion's version */
    uint8_t cid[8];             /* the companion id (also what pairing stores) */
    bool    cid_valid;
    int     api, min_api;
    bool    compat;             /* the companion's verdict: its API and this firmware's fit together */
} usb_link_peer_t;

/* The companion's latest ACK. false = none seen (yet). */
bool usb_link_peer(usb_link_peer_t *out);

/* Changes whenever the companion (re)appears after an absence of more than a few seconds, or answers with another identity: whoever
 * holds a session with it must start over. */
uint32_t usb_link_epoch(void);

/* One exchange: METHOD target [body]. On ESP_OK *status is the companion's HTTP-like status and resp holds the body (it is cleared
 * first; a body larger than resp->max is a failure, ESP_ERR_INVALID_SIZE with resp->truncated set). Blocks up to timeout_ms; a lost
 * request is repeated once with the same id (the companion answers a repeat from its memory without executing it again).
 * ESP_ERR_INVALID_STATE at once when no host is attached. */
esp_err_t usb_link_request(const char *method, const char *target, const char *body, int timeout_ms, usb_link_buf_t *resp, int *status);

/* Sends the firmware log (every esp_log line) to the companion as LOG frames, without blocking the logging task: when the port is not
 * being read, lines are dropped. The previous log sink (the UART console) keeps working. */
void usb_link_log_install(void);

/* Development builds: one line of text as a LOG frame, waiting up to a few hundred ms for the host instead of dropping it (screenshot
 * data). false when it could not be sent. */
bool usb_link_log_line(const char *line);

/* Development builds only: a DEBUG frame from the host carries the name of a development action ("screenshot", "tap_play", ...). The
 * product registers no handler, so the frame is simply ignored. The handler runs on the receive task: it must not block. */
void usb_link_set_debug_handler(void (*cb)(const char *action, void *ctx), void *ctx);

typedef struct {
    uint32_t rx_frames, rx_bad, rx_noise_bytes, rx_partial;
    uint32_t tx_frames, tx_dropped, log_dropped;
    uint32_t req_ok, req_retry, req_fail;
} usb_link_stats_t;

void usb_link_stats(usb_link_stats_t *out);

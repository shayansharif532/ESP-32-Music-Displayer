#pragma once
/* Internal interfaces of the pc_media component. */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"
#include "esp_http_client.h"
#include "lvgl.h"
#include "usb_link.h"

/* ---- pc_http.c: one persistent plain-HTTP connection to the companion, response buffer in PSRAM ---- */

typedef usb_link_buf_t pc_buf_t;       /* { char *data (NUL-terminated); size_t len, cap, max; bool truncated; }: shared with the USB link */

typedef struct {
    int      status;    /* HTTP status, 0 if the request failed */
    pc_buf_t body;
} pc_resp_t;

void pc_resp_init(pc_resp_t *r, size_t max_body);
void pc_resp_free(pc_resp_t *r);

/* What the last pc_http_request / pc_http_post_json learned about authentication (protocol section 2). */
typedef enum {
    PC_AUTH_NONE,       /* not a signed request, or it failed before there was an answer (transport error / timeout) */
    PC_AUTH_OK,         /* signed request, the answer carried a valid X-DM-Proof */
    PC_AUTH_FAILED,     /* 401 "auth" (also after the one retry with a fresh nonce) */
    PC_AUTH_UNPAIRED,   /* 401 "unpaired": the companion does not know this display */
    PC_AUTH_BAD_PROOF,  /* the answer had no valid X-DM-Proof (or was too big to check): discarded, status 0 */
    PC_AUTH_LIMITED,    /* 429: the companion's failure limiter; never trusted, never retried */
    PC_AUTH_NO_KEY,     /* a signed route was asked for but no key is stored: nothing was sent */
} pc_auth_result_t;

/* host/port select the companion; path_query is e.g. "/now-playing". `post` = send a control POST. Every route except GET /hello,
 * GET /health and /pair/... is signed (X-DM-Nonce / X-DM-Auth) and its answer must carry a valid X-DM-Proof, else the call fails
 * (ESP_ERR_INVALID_RESPONSE, status 0). 401 / 429 answers are returned as they are (status set, never trusted for anything but
 * the reason). Only the poll task calls these. */
esp_err_t pc_http_request(const char *host, uint16_t port, const char *path_query, bool post, int timeout_ms, pc_resp_t *resp);
/* Unauthenticated POST of a small JSON body (the /pair/... routes). */
esp_err_t pc_http_post_json(const char *host, uint16_t port, const char *path, const char *json, int timeout_ms, pc_resp_t *resp);
pc_auth_result_t pc_http_last_auth(void);
void pc_http_drop(void);        /* forget the kept-alive connection (after an error / host change); the stored nonce goes with it */

/* ---- pc_auth.c: pairing key in NVS, request / response signatures, the pairing crypto (docs/protocol.md sections 2 and 3) ---- */

void pc_hex_encode(const uint8_t *in, size_t n, char *out);                         /* lowercase, NUL-terminated: out needs 2n+1 */
bool pc_hex_decode(const char *hex, size_t hex_len, uint8_t *out, size_t out_len);   /* exactly 2*out_len hex digits, else false */
bool pc_auth_valid_nonce(const char *s);                                             /* 32 lowercase hex characters */
bool pc_auth_ct_equal(const void *a, const void *b, size_t n);                       /* constant time */

bool pc_auth_init(bool adc_entropy);                        /* once, before the poll task: loads the key + companion id from NVS (malformed entries are erased), creates
                                                 * the display id on first boot and switches the hardware RNG's entropy source on */
bool pc_auth_dev_id(uint8_t dev[8]);            /* the display's persistent identity (BEACON, pairing) */
void pc_auth_random(void *buf, size_t n);       /* hardware RNG (true random once pc_auth_init() has run) */
/* USB session proof: HMAC-SHA256(K, "DMLINK-PC
" or "DMLINK-DEV
" || cid || dev || nd || nc); false when there is no key. */
bool pc_link_proof(bool from_companion, const uint8_t dev[8], const uint8_t nd[16], const uint8_t nc[16], uint8_t out[32]);
bool pc_auth_has_key(void);                     /* thread-safe */
bool pc_auth_cid(uint8_t cid[8]);               /* the paired companion's id */
bool pc_auth_store(const uint8_t key[32], const uint8_t cid[8]);    /* persists, then switches to the new key; false = nothing stored */
/* out_hex = hex(HMAC-SHA256(K, "DMREQ1\n" + nonce + "\n" + METHOD + "\n" + target)); false when there is no key or the input is bad. */
bool pc_auth_sign(const char *method, const char *target, const char *nonce, char out_hex[65]);
/* X-DM-Proof check (constant time): HMAC-SHA256(K, "DMRESP1\n" + nonce + "\n" + status + "\n" + hex(SHA-256(body))). */
bool pc_auth_verify_proof(const char *nonce, int status, const uint8_t body_sha256[32], const char *proof_hex);

/* One pairing attempt. The display's private values live only in here and are wiped by pc_pair_wipe(). */
typedef struct {
    uint8_t dev[8], ne[16], nc[16], cid[8];
    uint8_t priv[32], pub_e[32], pub_c[32], ce[32];
    uint8_t t[32], k[32], proof_dev[32], proof_pc[32];
    char    code[7];
    bool    have_peer, derived;
} pc_pair_t;

bool pc_pair_begin(pc_pair_t *p);                                       /* fresh dev id, key pair, nE; computes ce */
void pc_pair_dev_hex(const pc_pair_t *p, char out[17]);
int  pc_pair_offer_json(const pc_pair_t *p, char *out, size_t n);       /* {"dev":..,"pub":..,"ce":..} ; length, or -1 if it does not fit */
int  pc_pair_reveal_json(const pc_pair_t *p, char *out, size_t n);      /* {"dev":..,"ne":..} */
int  pc_pair_confirm_json(const pc_pair_t *p, char *out, size_t n);     /* {"dev":..,"proof":<proof_dev>} (after pc_pair_derive) */
int  pc_pair_cancel_json(const pc_pair_t *p, char *out, size_t n);      /* {"dev":..} */
void pc_pair_set_peer(pc_pair_t *p, const uint8_t pub_c[32], const uint8_t nc[16], const uint8_t cid[8]);
bool pc_pair_derive(pc_pair_t *p);                                      /* Z, T, K, code, proofs; false if Z is all zero / the crypto failed */
bool pc_pair_verify_pc_proof(const pc_pair_t *p, const uint8_t proof[32]);
bool pc_pair_commit(const pc_pair_t *p);                                /* pc_auth_store(K, cid): call only after pc_pair_verify_pc_proof() */
void pc_pair_wipe(pc_pair_t *p);

#ifdef APP_DEV_TOOLS
bool pc_auth_selftest(void);                    /* every vector of the protocol appendix; logs "dmauth selftest ok" or the failing item */
#endif

/* ---- pc_art.c ---- */

/* Decodes a JPEG into one of two fixed PSRAM RGB565 buffers and builds the matching blurred full-screen background. */
esp_err_t pc_art_decode(const uint8_t *jpeg, size_t len, const lv_image_dsc_t **out_art, const lv_image_dsc_t **out_bg);
/* Where the UI draws a cover of the given pixel size (the background generator bakes a soft shadow behind that area). */
typedef lv_area_t (*pc_cover_area_fn)(int art_w, int art_h);
void pc_art_set_cover_area_fn(pc_cover_area_fn fn);

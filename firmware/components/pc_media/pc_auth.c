/*
 * pc_auth: the display's half of pairing and request authentication (docs/protocol.md sections 2 and 3).
 *
 *   - the pairing key K (32 B) and the paired companion's id (8 B) live in NVS (namespace "dm_auth", blobs "key" and "cid") and are
 *     read once into a static buffer; the key is never logged and never leaves this file except through the signing helpers;
 *   - request MACs (X-DM-Auth) and response proofs (X-DM-Proof), HMAC-SHA256 with the key;
 *   - the pairing crypto: X25519 (RFC 7748, through mbedTLS' generic ECP code), SHA-256, HKDF-SHA256 (written here: the IDF build has
 *     MBEDTLS_HKDF_C off), the transcript hash T, key K, 6-digit code and the two proofs.
 *
 * Threading: the key / cid buffers are touched only by the poll task (and by pc_auth_init() before it exists); other tasks may only
 * ask pc_auth_has_key(), which is an atomic flag.
 */
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>

#include "bootloader_random.h"
#include "esp_log.h"
#include "esp_random.h"
#include "mbedtls/ecp.h"
#include "mbedtls/md.h"
#include "mbedtls/platform_util.h"
#include "mbedtls/sha256.h"
#include "nvs.h"

#include "pc_internal.h"

static const char *TAG = "pc_auth";

#define NVS_NS      "dm_auth"
#define NVS_KEY     "key"
#define NVS_CID     "cid"
#define NVS_DEV     "dev"

static struct {
    uint8_t key[32];
    uint8_t cid[8];
} s_store;
static atomic_bool s_has_key;

/* ---- small helpers ----------------------------------------------------------------------------------- */

typedef struct {
    const void *p;
    size_t      n;
} seg_t;

static void wipe(void *p, size_t n)
{
    mbedtls_platform_zeroize(p, n);
}

/* mbedTLS wants a random-number callback for blinding the scalar multiplication. esp_fill_random is the hardware RNG, which is a
 * true random source only while the RF subsystem is on or the internal SAR-ADC entropy source is enabled. The USB-first firmware runs
 * with Wi-Fi off, so pc_auth_init() switches the ADC source on for good (see there): without it the pairing keys would come from a
 * pseudo-random stream. */
static int rng_cb(void *ctx, unsigned char *buf, size_t n)
{
    (void)ctx;
    esp_fill_random(buf, n);
    return 0;
}

void pc_hex_encode(const uint8_t *in, size_t n, char *out)
{
    static const char D[] = "0123456789abcdef";
    for (size_t i = 0; i < n; i++) {
        out[2 * i] = D[in[i] >> 4];
        out[2 * i + 1] = D[in[i] & 15];
    }
    out[2 * n] = '\0';
}

static int hexval(char c)
{
    if (c >= '0' && c <= '9') {
        return c - '0';
    }
    if (c >= 'a' && c <= 'f') {
        return c - 'a' + 10;
    }
    if (c >= 'A' && c <= 'F') {
        return c - 'A' + 10;
    }
    return -1;
}

bool pc_hex_decode(const char *hex, size_t hex_len, uint8_t *out, size_t out_len)
{
    if (!hex || !out || hex_len != out_len * 2) {
        return false;
    }
    for (size_t i = 0; i < out_len; i++) {
        int hi = hexval(hex[2 * i]), lo = hexval(hex[2 * i + 1]);
        if (hi < 0 || lo < 0) {
            return false;
        }
        out[i] = (uint8_t)(hi << 4 | lo);
    }
    return true;
}

bool pc_auth_valid_nonce(const char *s)
{
    if (!s || strlen(s) != 32) {
        return false;
    }
    for (int i = 0; i < 32; i++) {
        if (!((s[i] >= '0' && s[i] <= '9') || (s[i] >= 'a' && s[i] <= 'f'))) {
            return false;
        }
    }
    return true;
}

bool pc_auth_ct_equal(const void *a, const void *b, size_t n)
{
    const volatile uint8_t *x = a, *y = b;
    uint8_t d = 0;
    for (size_t i = 0; i < n; i++) {
        d |= (uint8_t)(x[i] ^ y[i]);
    }
    return d == 0;
}

static bool all_zero(const uint8_t *p, size_t n)
{
    uint8_t acc = 0;
    for (size_t i = 0; i < n; i++) {
        acc |= p[i];
    }
    return acc == 0;
}

static void random_bytes(void *buf, size_t n)
{
    esp_fill_random(buf, n);
}

/* ---- SHA-256 / HMAC / HKDF ---------------------------------------------------------------------------- */

static bool sha256_segs(const seg_t *seg, size_t nseg, uint8_t out[32])
{
    mbedtls_sha256_context c;
    mbedtls_sha256_init(&c);
    bool ok = mbedtls_sha256_starts(&c, 0) == 0;
    for (size_t i = 0; ok && i < nseg; i++) {
        ok = mbedtls_sha256_update(&c, seg[i].p, seg[i].n) == 0;
    }
    ok = ok && mbedtls_sha256_finish(&c, out) == 0;
    mbedtls_sha256_free(&c);
    return ok;
}

static bool hmac256(const uint8_t *key, size_t klen, const seg_t *seg, size_t nseg, uint8_t out[32])
{
    const mbedtls_md_info_t *info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
    if (!info) {
        return false;
    }
    mbedtls_md_context_t c;
    mbedtls_md_init(&c);
    bool ok = mbedtls_md_setup(&c, info, 1) == 0 && mbedtls_md_hmac_starts(&c, key, klen) == 0;
    for (size_t i = 0; ok && i < nseg; i++) {
        ok = mbedtls_md_hmac_update(&c, seg[i].p, seg[i].n) == 0;
    }
    ok = ok && mbedtls_md_hmac_finish(&c, out) == 0;
    mbedtls_md_free(&c);
    return ok;
}

/* RFC 5869 with HMAC-SHA256: Extract(salt, ikm) = HMAC(salt, ikm). */
static bool hkdf_extract(const uint8_t *salt, size_t slen, const uint8_t *ikm, size_t ilen, uint8_t prk[32])
{
    const seg_t seg[1] = { { ikm, ilen } };
    return hmac256(salt, slen, seg, 1, prk);
}

/* Expand(prk, info, len): T(i) = HMAC(prk, T(i-1) || info || i), output = T(1) || T(2) ... cut to len (len <= 255 * 32). */
static bool hkdf_expand(const uint8_t prk[32], const char *info, uint8_t *out, size_t len)
{
    uint8_t block[32] = { 0 };
    size_t have = 0, blen = 0;
    uint8_t ctr = 1;
    while (have < len) {
        const seg_t seg[3] = { { block, blen }, { info, strlen(info) }, { &ctr, 1 } };
        if (!hmac256(prk, 32, seg, 3, block)) {         /* (finish writes only after every update has read `block`) */
            wipe(block, sizeof block);
            return false;
        }
        blen = sizeof block;
        size_t n = len - have < sizeof block ? len - have : sizeof block;
        memcpy(out + have, block, n);
        have += n;
        ctr++;
    }
    wipe(block, sizeof block);
    return true;
}

/* ---- X25519 (RFC 7748) --------------------------------------------------------------------------------- */

/* out = X25519(priv, peer), or X25519(priv, 9) when peer is NULL (the public key). priv is clamped here as the RFC prescribes (so an
 * unclamped test vector works), the peer's top bit is ignored by mbedTLS' point reader. */
static bool x25519(uint8_t out[32], const uint8_t priv[32], const uint8_t *peer)
{
    static const uint8_t BASE[32] = { 9 };
    mbedtls_ecp_group grp;
    mbedtls_mpi d;
    mbedtls_ecp_point pt, q;
    uint8_t k[32];
    size_t olen = 0;
    bool ok = false;

    mbedtls_ecp_group_init(&grp);
    mbedtls_mpi_init(&d);
    mbedtls_ecp_point_init(&pt);
    mbedtls_ecp_point_init(&q);
    memcpy(k, priv, sizeof k);
    k[0] &= 248;
    k[31] &= 127;
    k[31] |= 64;
    if (mbedtls_ecp_group_load(&grp, MBEDTLS_ECP_DP_CURVE25519) == 0 &&
        mbedtls_mpi_read_binary_le(&d, k, sizeof k) == 0 &&
        mbedtls_ecp_point_read_binary(&grp, &pt, peer ? peer : BASE, 32) == 0 &&
        mbedtls_ecp_mul(&grp, &q, &d, &pt, rng_cb, NULL) == 0 &&
        mbedtls_ecp_point_write_binary(&grp, &q, MBEDTLS_ECP_PF_UNCOMPRESSED, &olen, out, 32) == 0 && olen == 32) {
        ok = true;
    }
    wipe(k, sizeof k);
    mbedtls_mpi_free(&d);
    mbedtls_ecp_point_free(&pt);
    mbedtls_ecp_point_free(&q);
    mbedtls_ecp_group_free(&grp);
    return ok;
}

/* ---- persistent key -------------------------------------------------------------------------------------- */

static void nvs_erase_pairing(void)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) {
        return;
    }
    nvs_erase_key(h, NVS_KEY);
    nvs_erase_key(h, NVS_CID);
    nvs_commit(h);
    nvs_close(h);
}

/* The display's persistent identity: 8 random bytes made once (first boot, or after the storage was erased) and kept in NVS. It is not
 * secret and not derived from the MAC address; the USB BEACON and every pairing attempt use it, so the companion's pairing record names
 * this display and another one can be told apart. */
static uint8_t s_dev[8];
static bool s_dev_valid;

static void load_dev_id(void)
{
    s_dev_valid = false;
    nvs_handle_t h;
    size_t n = sizeof s_dev;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) {
        ESP_LOGW(TAG, "cannot open the identity store: using a temporary display id");
        random_bytes(s_dev, sizeof s_dev);
        s_dev_valid = true;
        return;
    }
    if (nvs_get_blob(h, NVS_DEV, s_dev, &n) != ESP_OK || n != sizeof s_dev || all_zero(s_dev, sizeof s_dev)) {
        random_bytes(s_dev, sizeof s_dev);
        if (all_zero(s_dev, sizeof s_dev)) {
            s_dev[0] = 1;
        }
        if (nvs_set_blob(h, NVS_DEV, s_dev, sizeof s_dev) != ESP_OK || nvs_commit(h) != ESP_OK) {
            ESP_LOGW(TAG, "storing the display id failed: it will change at the next boot");
        } else {
            ESP_LOGI(TAG, "new display id created");
        }
    }
    nvs_close(h);
    s_dev_valid = true;
}

bool pc_auth_dev_id(uint8_t dev[8])
{
    if (!s_dev_valid) {
        return false;
    }
    memcpy(dev, s_dev, sizeof s_dev);
    return true;
}

void pc_auth_random(void *buf, size_t n)
{
    random_bytes(buf, n);
}

bool pc_auth_init(void)
{
    atomic_store(&s_has_key, false);
    bootloader_random_enable();                             /* keep the SAR-ADC entropy source on: Wi-Fi may never start (docs: Random Number Generation) */
    load_dev_id();
    nvs_handle_t h;
    esp_err_t e = nvs_open(NVS_NS, NVS_READONLY, &h);
    if (e == ESP_ERR_NVS_NOT_FOUND) {
        return false;                                       /* never paired */
    }
    if (e != ESP_OK) {
        ESP_LOGW(TAG, "cannot open the pairing store (%s): running unpaired", esp_err_to_name(e));
        return false;
    }
    size_t kl = sizeof s_store.key, cl = sizeof s_store.cid;
    esp_err_t ek = nvs_get_blob(h, NVS_KEY, s_store.key, &kl);
    esp_err_t ec = nvs_get_blob(h, NVS_CID, s_store.cid, &cl);
    nvs_close(h);
    if (ek == ESP_OK && ec == ESP_OK && kl == sizeof s_store.key && cl == sizeof s_store.cid &&
        !all_zero(s_store.key, sizeof s_store.key) && !all_zero(s_store.cid, sizeof s_store.cid)) {
        atomic_store(&s_has_key, true);
        ESP_LOGI(TAG, "paired display (key found)");
        return true;
    }
    if (ek != ESP_ERR_NVS_NOT_FOUND || ec != ESP_ERR_NVS_NOT_FOUND) {
        ESP_LOGW(TAG, "stored pairing is incomplete or damaged: discarded");
        nvs_erase_pairing();
    }
    wipe(&s_store, sizeof s_store);
    return false;
}

bool pc_auth_has_key(void)
{
    return atomic_load(&s_has_key);
}

bool pc_auth_cid(uint8_t cid[8])
{
    if (!atomic_load(&s_has_key)) {
        return false;
    }
    memcpy(cid, s_store.cid, sizeof s_store.cid);
    return true;
}

/* The old key is erased first, the new id written, the new key last: a power cut in between leaves "no key" (unpaired), never a
 * key with the wrong companion id. */
bool pc_auth_store(const uint8_t key[32], const uint8_t cid[8])
{
    if (all_zero(key, 32) || all_zero(cid, 8)) {
        return false;
    }
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) {
        ESP_LOGW(TAG, "cannot open the pairing store");
        return false;
    }
    esp_err_t e = nvs_erase_key(h, NVS_KEY);
    if (e != ESP_OK && e != ESP_ERR_NVS_NOT_FOUND) {
        nvs_close(h);
        return false;
    }
    bool ok = nvs_set_blob(h, NVS_CID, cid, 8) == ESP_OK && nvs_set_blob(h, NVS_KEY, key, 32) == ESP_OK && nvs_commit(h) == ESP_OK;
    nvs_close(h);
    if (!ok) {
        ESP_LOGW(TAG, "storing the pairing failed");
        nvs_erase_pairing();
        atomic_store(&s_has_key, false);
        wipe(&s_store, sizeof s_store);
        return false;
    }
    memcpy(s_store.key, key, 32);
    memcpy(s_store.cid, cid, 8);
    atomic_store(&s_has_key, true);
    return true;
}

/* ---- request MAC / response proof ------------------------------------------------------------------------- */

static bool request_mac(const uint8_t key[32], const char *method, const char *target, const char *nonce, uint8_t out[32])
{
    const seg_t seg[6] = { { "DMREQ1\n", 7 }, { nonce, 32 }, { "\n", 1 }, { method, strlen(method) }, { "\n", 1 }, { target, strlen(target) } };
    return hmac256(key, 32, seg, 6, out);
}

static bool response_mac(const uint8_t key[32], const char *nonce, int status, const uint8_t body_sha256[32], uint8_t out[32])
{
    char st[12], hex[65];
    int n = snprintf(st, sizeof st, "%d", status);
    if (n <= 0 || n >= (int)sizeof st) {
        return false;
    }
    pc_hex_encode(body_sha256, 32, hex);
    const seg_t seg[6] = { { "DMRESP1\n", 8 }, { nonce, 32 }, { "\n", 1 }, { st, (size_t)n }, { "\n", 1 }, { hex, 64 } };
    return hmac256(key, 32, seg, 6, out);
}

bool pc_auth_sign(const char *method, const char *target, const char *nonce, char out_hex[65])
{
    uint8_t mac[32];
    if (!atomic_load(&s_has_key) || !method || !target || !pc_auth_valid_nonce(nonce) || !request_mac(s_store.key, method, target, nonce, mac)) {
        return false;
    }
    pc_hex_encode(mac, sizeof mac, out_hex);
    return true;
}

bool pc_auth_verify_proof(const char *nonce, int status, const uint8_t body_sha256[32], const char *proof_hex)
{
    uint8_t want[32], got[32];
    if (!atomic_load(&s_has_key) || !pc_auth_valid_nonce(nonce) || !proof_hex || !pc_hex_decode(proof_hex, strlen(proof_hex), got, sizeof got) ||
        !response_mac(s_store.key, nonce, status, body_sha256, want)) {
        return false;
    }
    return pc_auth_ct_equal(want, got, sizeof want);
}

/* ---- USB session proofs (docs/usb-protocol.md section 4) ----------------------------------------------------------- */

/* HMAC-SHA256(K, "DMLINK-PC\n" || cid || dev || nd || nc) from the companion, "DMLINK-DEV\n" ... from the display. Both sides bind the
 * same four values, so the two proofs cannot be replayed into another link. */
static bool link_proof_with(const uint8_t key[32], const uint8_t cid[8], bool from_companion, const uint8_t dev[8], const uint8_t nd[16],
                            const uint8_t nc[16], uint8_t out[32])
{
    const char *label = from_companion ? "DMLINK-PC\n" : "DMLINK-DEV\n";
    const seg_t seg[5] = { { label, strlen(label) }, { cid, 8 }, { dev, 8 }, { nd, 16 }, { nc, 16 } };
    return hmac256(key, 32, seg, 5, out);
}

bool pc_link_proof(bool from_companion, const uint8_t dev[8], const uint8_t nd[16], const uint8_t nc[16], uint8_t out[32])
{
    return atomic_load(&s_has_key) && link_proof_with(s_store.key, s_store.cid, from_companion, dev, nd, nc, out);
}

/* ---- pairing --------------------------------------------------------------------------------------------- */

/* transcript hash T = SHA-256("DMPAIR1" || cid || dev || pubE || pubC || nE || nC) */
static bool pair_transcript(const pc_pair_t *p, uint8_t t[32])
{
    const seg_t seg[7] = { { "DMPAIR1", 7 }, { p->cid, 8 }, { p->dev, 8 }, { p->pub_e, 32 }, { p->pub_c, 32 }, { p->ne, 16 }, { p->nc, 16 } };
    return sha256_segs(seg, 7, t);
}

/* From the exchanged values and the DH secret z: T, K and the code. PRK = HKDF-Extract(salt = T, ikm = Z). */
static bool pair_derive_from(pc_pair_t *p, const uint8_t z[32])
{
    uint8_t prk[32], sas[4];
    bool ok = pair_transcript(p, p->t) && hkdf_extract(p->t, 32, z, 32, prk) && hkdf_expand(prk, "DMKEY1", p->k, 32) &&
              hkdf_expand(prk, "DMSAS1", sas, sizeof sas);
    if (ok) {
        const uint32_t v = (uint32_t)sas[0] << 24 | (uint32_t)sas[1] << 16 | (uint32_t)sas[2] << 8 | sas[3];
        snprintf(p->code, sizeof p->code, "%06u", (unsigned)(v % 1000000U));
    }
    wipe(prk, sizeof prk);
    wipe(sas, sizeof sas);
    return ok;
}

bool pc_pair_begin(pc_pair_t *p)
{
    memset(p, 0, sizeof *p);
    if (!pc_auth_dev_id(p->dev)) {                          /* the persistent display id (BEACON and pairing use the same one) */
        random_bytes(p->dev, sizeof p->dev);
    }
    random_bytes(p->priv, sizeof p->priv);
    random_bytes(p->ne, sizeof p->ne);
    if (!x25519(p->pub_e, p->priv, NULL)) {
        pc_pair_wipe(p);
        return false;
    }
    const seg_t seg[2] = { { p->pub_e, 32 }, { p->ne, 16 } };
    if (!sha256_segs(seg, 2, p->ce)) {                                 /* ce = SHA-256(pubE || nE) */
        pc_pair_wipe(p);
        return false;
    }
    return true;
}

void pc_pair_dev_hex(const pc_pair_t *p, char out[17])
{
    pc_hex_encode(p->dev, sizeof p->dev, out);
}

static int json_fields(char *out, size_t n, const pc_pair_t *p, const char *k2, const uint8_t *v2, size_t v2len, const char *k3, const uint8_t *v3, size_t v3len)
{
    char dev[17], a[65], b[65];
    pc_hex_encode(p->dev, sizeof p->dev, dev);
    int len;
    if (!k2) {
        len = snprintf(out, n, "{\"dev\":\"%s\"}", dev);
    } else if (!k3) {
        pc_hex_encode(v2, v2len, a);
        len = snprintf(out, n, "{\"dev\":\"%s\",\"%s\":\"%s\"}", dev, k2, a);
    } else {
        pc_hex_encode(v2, v2len, a);
        pc_hex_encode(v3, v3len, b);
        len = snprintf(out, n, "{\"dev\":\"%s\",\"%s\":\"%s\",\"%s\":\"%s\"}", dev, k2, a, k3, b);
    }
    return (len > 0 && (size_t)len < n) ? len : -1;
}

int pc_pair_offer_json(const pc_pair_t *p, char *out, size_t n)
{
    return json_fields(out, n, p, "pub", p->pub_e, 32, "ce", p->ce, 32);
}

int pc_pair_reveal_json(const pc_pair_t *p, char *out, size_t n)
{
    return json_fields(out, n, p, "ne", p->ne, 16, NULL, NULL, 0);
}

int pc_pair_confirm_json(const pc_pair_t *p, char *out, size_t n)
{
    return p->derived ? json_fields(out, n, p, "proof", p->proof_dev, 32, NULL, NULL, 0) : -1;
}

int pc_pair_cancel_json(const pc_pair_t *p, char *out, size_t n)
{
    return json_fields(out, n, p, NULL, NULL, 0, NULL, NULL, 0);
}

void pc_pair_set_peer(pc_pair_t *p, const uint8_t pub_c[32], const uint8_t nc[16], const uint8_t cid[8])
{
    memcpy(p->pub_c, pub_c, 32);
    memcpy(p->nc, nc, 16);
    memcpy(p->cid, cid, 8);
    p->have_peer = true;
}

/* Z = X25519(own private, peer public); an all-zero Z aborts the pairing. The private key is wiped as soon as Z exists. */
bool pc_pair_derive(pc_pair_t *p)
{
    uint8_t z[32];
    bool ok = false;
    if (p->have_peer && x25519(z, p->priv, p->pub_c) && !all_zero(z, sizeof z) && pair_derive_from(p, z)) {
        char t_hex[65];
        pc_hex_encode(p->t, 32, t_hex);
        const seg_t dev[2] = { { "DMPAIR-DEV\n", 11 }, { t_hex, 64 } };
        const seg_t pcs[2] = { { "DMPAIR-PC\n", 10 }, { t_hex, 64 } };
        ok = hmac256(p->k, 32, dev, 2, p->proof_dev) && hmac256(p->k, 32, pcs, 2, p->proof_pc);
    }
    wipe(z, sizeof z);
    wipe(p->priv, sizeof p->priv);
    p->derived = ok;
    return ok;
}

bool pc_pair_verify_pc_proof(const pc_pair_t *p, const uint8_t proof[32])
{
    return p->derived && pc_auth_ct_equal(p->proof_pc, proof, 32);
}

bool pc_pair_commit(const pc_pair_t *p)
{
    return p->derived && pc_auth_store(p->k, p->cid);
}

void pc_pair_wipe(pc_pair_t *p)
{
    wipe(p, sizeof *p);
}

/* ---- self-test (development builds only: the vectors of the protocol appendix) ------------------------------ */

#ifdef APP_DEV_TOOLS

static int s_fail;

static void check(const char *what, const uint8_t *got, const char *want_hex)
{
    uint8_t want[64];
    size_t n = strlen(want_hex) / 2;
    if (n > sizeof want || !pc_hex_decode(want_hex, strlen(want_hex), want, n) || !pc_auth_ct_equal(got, want, n)) {
        ESP_LOGE(TAG, "dmauth selftest FAILED: %s", what);
        s_fail++;
    }
}

static void check_hex(const char *what, const char *got_hex, const char *want_hex)
{
    if (strcmp(got_hex, want_hex) != 0) {
        ESP_LOGE(TAG, "dmauth selftest FAILED: %s", what);
        s_fail++;
    }
}

static void unhex(const char *hex, uint8_t *out, size_t n)
{
    if (!pc_hex_decode(hex, strlen(hex), out, n)) {
        memset(out, 0, n);
    }
}

bool pc_auth_selftest(void)
{
    s_fail = 0;
    uint8_t a[32], b[32], z[32];

    /* RFC 7748 section 6.1 */
    unhex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a", a, 32);
    unhex("de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f", b, 32);
    if (!x25519(z, a, b)) {
        ESP_LOGE(TAG, "dmauth selftest FAILED: x25519 error");
        s_fail++;
    } else {
        check("RFC 7748 X25519", z, "4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742");
    }

    /* pairing vectors */
    pc_pair_t p;
    memset(&p, 0, sizeof p);
    unhex("e77c4f598df3914465af5303551ac58efc71bd617ca5877c8cbc590cf2d62a78", p.priv, 32);
    uint8_t c_priv[32], pub_c[32];
    unhex("00bd2f1bc098fe7a86b8ec025568f65e03c2992c9fef9af1d9cd2c40f76bc5ce", c_priv, 32);
    if (!x25519(p.pub_e, p.priv, NULL) || !x25519(pub_c, c_priv, NULL)) {
        ESP_LOGE(TAG, "dmauth selftest FAILED: x25519 base point");
        s_fail++;
    }
    check("pubE", p.pub_e, "d86f406d8e9dfd35ba4b3a63c4f40c4076be2fd20d29f653d6258f2c014c1e53");
    check("pubC", pub_c, "0e7f912e8edb5f58030b926447b3789b122f1ba7dde136ec86a483cb27b2546c");
    memset(p.dev, 0x11, sizeof p.dev);
    memset(p.ne, 0xaa, sizeof p.ne);
    uint8_t nc[16], cid[8] = { 1, 2, 3, 4, 5, 6, 7, 8 };
    memset(nc, 0xbb, sizeof nc);
    const seg_t cs[2] = { { p.pub_e, 32 }, { p.ne, 16 } };
    if (!sha256_segs(cs, 2, p.ce)) {
        s_fail++;
    }
    check("ce", p.ce, "8238badc7f8e71df9aaa6bbce41c1340f898bd7be3e150a301e182f488710ac7");
    pc_pair_set_peer(&p, pub_c, nc, cid);

    /* Z from both sides, then the rest from the display's side */
    if (!x25519(z, c_priv, p.pub_e)) {
        s_fail++;
    }
    check("Z (companion side)", z, "226406b45f50ac0e954dd7682c9ff42620d4680c334afe6621329d02b8dda434");
    uint8_t z2[32];
    if (!x25519(z2, p.priv, p.pub_c)) {
        s_fail++;
    }
    check("Z (display side)", z2, "226406b45f50ac0e954dd7682c9ff42620d4680c334afe6621329d02b8dda434");
    if (!pc_pair_derive(&p)) {
        ESP_LOGE(TAG, "dmauth selftest FAILED: derive");
        s_fail++;
    } else {
        check("T", p.t, "d3448c8c90e680cc95666ef536e80b30b429022d72afff163a39c1e29bbdf628");
        check("K", p.k, "a388cfc735774ba3e13991eadd90a4ffa2020becaf2d78a70541f26db7a09821");
        check_hex("code", p.code, "959079");
        check("proof_dev", p.proof_dev, "fc1bace96c591d3072b8b2bfb2007e2ce9db361c84746ff7c871210016910dac");
        check("proof_pc", p.proof_pc, "4c2dee3dfc1e9e3f213bb1243186408e285ec92b0034b76b4e31a3ac65fbce95");
        uint8_t good[32], bad[32];
        unhex("4c2dee3dfc1e9e3f213bb1243186408e285ec92b0034b76b4e31a3ac65fbce95", good, 32);
        memcpy(bad, good, sizeof bad);
        bad[31] ^= 1;
        if (!pc_pair_verify_pc_proof(&p, good) || pc_pair_verify_pc_proof(&p, bad)) {
            ESP_LOGE(TAG, "dmauth selftest FAILED: proof_pc verification");
            s_fail++;
        }

        /* request MAC / response proof (the appendix key is K) */
        const char *nonce = "00112233445566778899aabbccddeeff";
        uint8_t mac[32];
        char hex[65];
        if (!request_mac(p.k, "GET", "/now-playing", nonce, mac)) {
            s_fail++;
        }
        pc_hex_encode(mac, 32, hex);
        check_hex("X-DM-Auth", hex, "53a8bdfd2b22c9daef7645c4c96d3a61214f63f3811fdc4f5a04be4d1595f8c8");
        const seg_t bs[1] = { { "{\"ok\":true}", 11 } };
        uint8_t sha[32];
        if (!sha256_segs(bs, 1, sha) || !response_mac(p.k, nonce, 200, sha, mac)) {
            s_fail++;
        }
        pc_hex_encode(mac, 32, hex);
        check_hex("X-DM-Proof", hex, "3ec39b8f361d53cf98d7f1df2384653ebe137c037ae8b95844b7b16c6c5f26d2");

        /* the USB session proofs (docs/usb-protocol.md section 10), same K, cid 0102..08, dev 11.., nd cc.., nc dd.. */
        uint8_t nd[16], ncs[16], lp[32];
        memset(nd, 0xcc, sizeof nd);
        memset(ncs, 0xdd, sizeof ncs);
        if (!link_proof_with(p.k, cid, true, p.dev, nd, ncs, lp)) {
            s_fail++;
        }
        check("link proof (companion)", lp, "6eb2d91074d29d26f2c752fd75c9577e41516d44a7acbb5ad032686d09cb56dd");
        if (!link_proof_with(p.k, cid, false, p.dev, nd, ncs, lp)) {
            s_fail++;
        }
        check("link proof (display)", lp, "b9e783272a2baef27e9f868e45a9c64d8728c37b081aecb5cc3f7c0c92006c89");
    }
    pc_pair_wipe(&p);
    wipe(c_priv, sizeof c_priv);
    wipe(z, sizeof z);
    wipe(z2, sizeof z2);

    if (s_fail == 0) {
        ESP_LOGI(TAG, "dmauth selftest ok");
    }
    return s_fail == 0;
}

#endif /* APP_DEV_TOOLS */

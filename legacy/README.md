# legacy/ - OBSOLETE, kept for reference only (not built)

On 2026-10-12 the architecture changed: the display no longer talks to the Spotify Web API. A small Windows companion app
reads the PC's media session and the ESP32 polls it over the LAN (see `docs/ARCHITECTURE.md`, ADR-003 in `docs/DECISIONS.md`).

`spotify-component/` is the work-in-progress Spotify client that was written for the old plan (commit `db51d85` on `feature/mvp`):
Authorization Code + PKCE, token refresh, `/v1/me/player` polling, transport commands, album-art download and decode, and an
on-device "link Spotify" page. It compiled and linked but was **never run against real Spotify** (no Client ID was ever created).
Nothing outside this folder references it. Parts were reused in the new `firmware/components/pc_media` component (JPEG art decode into
two fixed PSRAM buffers, growable PSRAM HTTP response buffer).

It may be revived if a Spotify-API-based mode is ever wanted (it would need its own Client ID and a Premium app owner - see ADR-002).
`net_selftest.*.txt` is the one-shot HTTPS/TLS check that went with it (renamed so it is not compiled).

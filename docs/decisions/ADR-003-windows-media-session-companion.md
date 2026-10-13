# ADR-003 — Windows media-session companion replaces the Spotify Web API (2026-10-12) — **current**

Status: **accepted** (maintainer decision). Supersedes ADR-002 as the primary architecture.

Context: the device will only ever be used with the user's Windows PC. The Spotify route needs a Spotify developer app, a **Premium**
app owner, the 5-user cap, OAuth/PKCE with an awkward redirect on a screen-only device, token storage and a TLS client - and it only covers Spotify.

Decision: a lightweight **Windows companion** reads the active **media session** (`GlobalSystemMediaTransportControlsSessionManager` /
`Windows.Media.Control`) and exposes it over a small **HTTP API on the LAN**; the ESP32 polls it and sends control commands. Details:
[architecture.md](../architecture.md).

Why: covers Spotify desktop, YouTube / YouTube Music in browsers, VLC, Apple Music, ...; no accounts / tokens / Premium; plain HTTP on the
LAN is simple and fast on the ESP32; the PC pre-normalises artwork to a baseline 300x300 JPEG; transport commands behave like media keys.

Consequences / accepted trade-offs: the PC must be on, on the same network and running the companion; the API is unauthenticated on the
LAN (mitigated by LAN-only / Host-header / custom-header rails now, pairing token later); the Windows Firewall will ask once to allow the
companion's port; non-Latin titles need a fallback font on the device (known issue). Companion language: Python + `winrt-*` + Pillow
(already available on the dev PC, quick to iterate; packaged as a single `.exe` with PyInstaller for end users - Milestone 12).

The old Spotify code was removed from the tree; it lives in git history under the tag `legacy-spotify-web-api` (commit `db51d85` introduced it).

**Update (later work):** the unauthenticated-LAN trade-off above was closed afterwards by one-time pairing and authenticated requests, see [protocol.md](../protocol.md) and [security.md](../security.md).

# ADR-002 — Spotify: rules that shape the design (verified against Spotify's own pages, 2026-10-11) — **OBSOLETE, superseded by ADR-003**

*Kept for the record in case a Spotify-API mode is ever revived. The client code written for this plan was removed from the tree; it lives in git history under the tag `legacy-spotify-web-api`.*

Sources: developer.spotify.com — *February 2026 Web API Dev Mode Changes (Migration Guide)* and *Redirect URIs*.

* **Premium.** Every **Development Mode** app needs its **owner** (the Spotify account that created the app on the developer
  dashboard) to have an active **Premium** subscription — if it lapses, the app stops working. Separately, the playback *control*
  endpoints (play / pause / next / previous / seek) only work for Premium **listeners**; reading "now playing" works for free accounts.
* **Users / Client IDs.** A Development Mode app is limited to **5 users per app** (still applies). The Feb 2026 change allowed only
  1 Client ID per developer, but Spotify's **July 2026 update raised that to 25 Client IDs per developer account** (confirmed by the
  maintainer), so creating one dedicated app per end user is fine. The end user's Spotify account must be added under *User Management*
  in that app (name + the e-mail of their Spotify account) before they can authorise.
* **Endpoints.** The February 2026 changes removed/changed library, batch-lookup, browse and playlist-item endpoints. The **Player**
  endpoints we use (`GET /me/player`, `GET /me/player/currently-playing`, play/pause/next/previous/seek, devices) are **not** in the
  removed/changed list. We re-verify live in Milestone 5.
* **OAuth.** Authorization Code **with PKCE** is the only flow we implement — **never Implicit Grant** (maintainer's explicit requirement;
  Spotify also removed it in Nov 2025). No client secret ever on the device. Redirect URIs must be
  **HTTPS**, except loopback IP literals (`http://127.0.0.1[:port]`, `http://[::1]`); `localhost` and plain-HTTP LAN addresses
  are rejected; matching is exact.
* **Consequences for the design**
  * The Spotify **Client ID is entered at setup time and stored in NVS** (never compiled in), so no secret and no shared credential
    lives in the firmware or the repo.
  * `http://<device-ip>/callback` is **not** an allowed redirect URI. Plan: a tiny static **HTTPS "bridge" page** (free static hosting,
    e.g. GitHub Pages) registered as the redirect URI. It receives `?code=…` and bounces the phone to the device's local setup page, which
    does the PKCE token exchange. Fallback with no hosting: register `http://127.0.0.1/callback`, and paste the resulting URL's `code`
    into the device's setup page.
  * Only the **refresh token** (+ Client ID) is persisted; access tokens live in RAM and are refreshed automatically; nothing sensitive is logged.
  * If the listener is not Premium, or no device is active, the UI shows a clear state (controls disabled / "No active device — open
    Spotify on your phone") instead of failing silently.
* **Open question for the maintainer (needed before Milestone 5):** whose Spotify developer account owns the app (needs Premium)?
  Either the person who builds the display creates it and adds the end user to the 5-user list, or the end user creates their own.

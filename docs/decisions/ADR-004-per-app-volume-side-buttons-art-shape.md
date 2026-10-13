# ADR-004 — Per-app volume through Core Audio sessions, side buttons as volume keys, art kept in its own shape (2026-10-12) — **current**

Status: **accepted** (maintainer request after reviewing the M8 redesign on the real display). Details and evidence: [`docs/development-history/M8b-art-layout-gestures-volume.md`](../development-history/M8b-art-layout-gestures-volume.md).

* **Volume is per application, never the master volume.** `Windows.Media.Control` has no volume, so the companion uses the Core Audio session API (`ISimpleAudioVolume`, via `pycaw`/`comtypes`) on the audio
  sessions owned by the displayed app's processes. The display names the app it shows in every volume request so the target always matches the screen. Chromium browsers mix all tabs in one session
  (whole-browser volume) and Firefox cannot be matched - both documented limits, accepted for a "best effort" browser story.
* **The side buttons are the volume keys**: PWR (AXP2101, polled over I2C because neither the key nor the PMIC IRQ reaches an ESP32 pin) = up, BOOT (GPIO0) = down. No permanent on-screen volume control.
  Consequence: "hold BOOT at power-up" can no longer be the Wi-Fi-reset gesture in Milestone 11 (it is the ROM download-mode gesture anyway); use a long press while running.
* **Artwork keeps its aspect ratio**: the companion renders the cover at the size it is drawn (square or landscape card) instead of letterboxing onto a square, the UI lays out around it.
* **App logos are removed structurally, never by fixed crops**: only alpha composites with exactly one solid block plus sparse bands are split (Spotify's 300x300 RGBA composite); everything else is untouched.
* **Gestures**: horizontal swipe with distance / direction / time rules and a guard that a travelled press is never a tap; the progress bar never starts a swipe. *(The direction mapping was reversed
  by ADR-005: left = next, right = previous.)*

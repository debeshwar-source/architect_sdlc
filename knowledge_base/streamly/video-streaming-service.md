# video-streaming-service

> **DEMO DATA.** This document is synthetic, written for demonstrating
> `sdlc_architect`. It does not describe a real system.

- **Repo:** `https://github.com/streamly/video-streaming-service`
- **Team / owner:** Video Platform Team
- **Tech stack:** Go, AWS MediaConvert (transcoding), CloudFront CDN,
  Widevine/FairPlay DRM
- **Purpose:** Turns an uploaded video asset into adaptive-bitrate
  streams (HLS/DASH) and serves playback: signed CDN manifest URLs and
  short-lived DRM licenses.

## Exposes (public API, via api-gateway)

- `GET /api/v1/stream/{episode_id}/manifest` -- returns a signed
  playback manifest URL
- `POST /api/v1/stream/{episode_id}/license` -- issues a short-lived
  DRM license for a playback session

## Depends on (calls)

- `content-catalog-service` (HTTP) -- confirms the episode exists and
  is within its licensing window before issuing a manifest
- `user-service` (HTTP) -- confirms the user's subscription tier
  permits this title's resolution/quality and the user's region

## Data owned

- Transcoded asset locations (CDN paths per resolution/bitrate)
- DRM license issuance records

## Events

Does not currently produce or consume events.

## Known constraints

- Transcoding a newly uploaded episode into all required bitrates
  takes 15-45 minutes. An episode is not playable through this
  service until transcoding completes, independent of whatever
  `available_from` time is set in `content-catalog-service`.
- This service assumes `content-catalog-service` already has the
  episode's metadata present before a manifest is requested; it does
  not itself track upcoming/scheduled episodes.

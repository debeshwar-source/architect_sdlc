# ott-streaming-application

**What it is.** A from-scratch OTT streaming MVP made of two applications; repos, owners and stacks are not yet assigned.

- **ott-media-service** (backend): owns the content catalog and video assets. Exposes `GET /catalog` (list titles) and `GET /content/{id}/stream` (playable stream/reference for a title). Depends on nothing; no events. MVP delivery is single-quality/progressive; adaptive bitrate (HLS/DASH) and transcoding are not built.
- **ott-web-client** (user-facing client): launch, browse the catalog, play video. Exposes no API, owns no data, depends only on `ott-media-service` for the catalog and the stream request. The target platform (web, native mobile, smart TV) is undecided; the document assumes one client.

**Contract to respect:** the client calls `GET /catalog` and `GET /content/{id}/stream` on the media service; changing either shape affects the client. No events exist anywhere yet.

## Applications

- `ott-media-service` — Backend service that owns the content catalog and video assets, and exposes APIs to list available content and retrieve a playable video stream for a selected title.
- `ott-web-client` — The user-facing OTT client application that lets a user launch the app, browse available content, and play streaming video/media.

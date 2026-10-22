# streamly

**What it is.** Streamly: a demo (synthetic) video-streaming platform of eight documented applications, described for demonstrating the planning pipeline. Clients enter through the gateway; some services also call each other directly over HTTP (see dependencies).

**Applications and owners**
- `api-gateway` (Platform, Go): the only public entry point; validates JWTs and routes by path to catalog, streaming, user, watch-history and notification services. Pure synchronous HTTP, no events. Route changes are a config deploy. It never issues playback tokens.
- `content-catalog-service` (Content Platform, Java/Spring, PostgreSQL + Elasticsearch): source of truth for titles, seasons, episodes and territory licensing windows. Availability is computed at read time; nothing is notified when a window opens.
- `video-streaming-service` (Video Platform, Go, MediaConvert/CloudFront/DRM): signed HLS/DASH manifests and short-lived DRM licenses. Needs catalog (episode and licensing check) and user-service (tier, region). Transcoding takes 15-45 minutes, independent of `available_from`.
- `user-service` (Core Platform, Python/FastAPI): the only authoritative source of profiles, credentials, subscription tier and notification preferences. Check here first; never duplicate this data.
- `watch-history-service` (Growth, Node.js, Cassandra): playback progress and continue-watching; "watchers" of a series are derived from playback history only (there is no follow feature). Depends on catalog.
- `notification-service` (Growth, Node.js, Kafka consumer, SendGrid, FCM): email and push only (no in-app real-time). It already has a consume-event, check-preferences, send pipeline, so a new notification type is a new consumer plus a template. Depends on user-service.
- `event-bus-contracts` (Platform, Protobuf package): the single source of truth for every Kafka event schema. A new event type must be added here and the new package version adopted by producer and consumer first; this is always a prerequisite, never parallel work.
- `web-homepage` (new, static): a single static landing page; no calls, no personalization.

**Dependencies (caller to callee).** gateway to catalog, streaming, notification, watch-history, user; streaming to catalog and user; watch-history to catalog; notification to user. Today no application produces events and only notification consumes them.

**Rules of thumb.** Anything gating by plan tier or sending notifications must consult `user-service`. New events start in `event-bus-contracts`. New public endpoints on a service also need a gateway route.

## Applications

- `api-gateway` — Single public entry point for every client (mobile apps, web player, smart TV apps, set-top box apps). Handles authentication (JWT validation) and routes requests to internal services. Clients never call internal services directly.
- `content-catalog-service` — The source of truth for every title, season, and episode on the platform -- metadata, cast, genres, artwork, and per-territory licensing windows (a title may be licensed for some countries and date ranges but not others).
- `event-bus-contracts` — The single source of truth for every event schema published on the shared Kafka event bus. No service is allowed to publish or consume an event whose schema isn't defined here first -- this repo is the contract layer between producers and consumers.
- `notification-service` — Sends outbound user notifications (email and push). Consumes events from the shared event bus and delivers notifications according to each user's preferences.
- `user-service` — Source of truth for user profiles, authentication credentials, subscription plan/tier (Basic / Standard / Premium), and all per-user preferences.
- `video-streaming-service` — Turns an uploaded video asset into adaptive-bitrate streams (HLS/DASH) and serves playback: signed CDN manifest URLs and short-lived DRM licenses.
- `watch-history-service` — Tracks what each user has watched: per-episode viewing progress (resume position), completion state, and which series a user has been watching (used to power "Continue Watching").
- `web-homepage` — Serves a single, static HTML landing page as the default entry point for the web experience, reachable at the platform's root URL with no prior navigation, authentication, or personalization.

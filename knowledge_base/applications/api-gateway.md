# api-gateway

> **DEMO DATA.** This document is synthetic, written for demonstrating
> `sdlc_architect`. It does not describe a real system.

- **Repo:** `https://github.com/streamly/api-gateway`
- **Team / owner:** Platform Team
- **Tech stack:** Go, Kong-style reverse proxy config, deployed on Kubernetes
- **Purpose:** Single public entry point for every client (mobile apps,
  web player, smart TV apps, set-top box apps). Handles authentication
  (JWT validation) and routes requests to internal services. Clients
  never call internal services directly.

## Exposes (public API)

- `POST /api/v1/auth/login`
- `GET /api/v1/catalog/*` → routes to `content-catalog-service`
- `GET /api/v1/stream/*`, `POST /api/v1/stream/*` → routes to
  `video-streaming-service`
- `GET /api/v1/users/me` → routes to `user-service`
- `GET /api/v1/watch-history/*` → routes to `watch-history-service`
- `GET /api/v1/notifications` → routes to `notification-service`

## Depends on (calls)

- `content-catalog-service` (HTTP)
- `video-streaming-service` (HTTP)
- `user-service` (HTTP)
- `watch-history-service` (HTTP)
- `notification-service` (HTTP)

## Data owned

None -- stateless routing layer.

## Events

Does not produce or consume events. Pure synchronous HTTP proxy.

## Known constraints

- Route table changes require a config deploy (not a code deploy) and
  can be rolled out independently of the services behind it.
- DRM license tokens are issued exclusively by `video-streaming-service`
  -- the gateway never generates or caches a playback token itself.
- Any new public endpoint on a downstream service is NOT reachable by
  clients until a route is added here.

# watch-history-service

> **DEMO DATA.** This document is synthetic, written for demonstrating
> `sdlc_architect`. It does not describe a real system.

- **Repo:** `https://github.com/streamly/watch-history-service`
- **Team / owner:** Growth Team
- **Tech stack:** Node.js, Cassandra (high write volume from playback
  progress events)
- **Purpose:** Tracks what each user has watched: per-episode viewing
  progress (resume position), completion state, and which series a
  user has been watching (used to power "Continue Watching").

## Exposes (public API, via api-gateway)

- `GET /api/v1/watch-history/continue-watching`
- `POST /api/v1/watch-history/progress` -- called by client apps
  periodically during playback
- `GET /api/v1/watch-history/series/{series_id}/watchers` --
  internal-only, returns the user IDs who have watched at least one
  episode of a given series

## Depends on (calls)

- `content-catalog-service` (HTTP) -- resolves an episode ID to its
  parent series ID

## Data owned

- Per-user, per-episode viewing progress and completion state
- Derived per-series "has watched at least one episode" state

## Events

Does not currently produce or consume events.

## Known constraints

- There is no explicit "follow this show" or "subscribe to series"
  feature today. The `watchers` list for a series is entirely derived
  from playback history -- any user who watched at least one episode
  counts as a watcher, whether or not they finished it or intend to
  keep watching.

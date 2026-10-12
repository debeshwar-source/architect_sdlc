# content-catalog-service

> **DEMO DATA.** This document is synthetic, written for demonstrating
> `sdlc_architect`. It does not describe a real system.

- **Repo:** `https://github.com/streamly/content-catalog-service`
- **Team / owner:** Content Platform Team
- **Tech stack:** Java, Spring Boot, PostgreSQL, Elasticsearch (search index)
- **Purpose:** The source of truth for every title, season, and episode
  on the platform -- metadata, cast, genres, artwork, and per-territory
  licensing windows (a title may be licensed for some countries and
  date ranges but not others).

## Exposes (public API, via api-gateway)

- `GET /api/v1/catalog/titles/{title_id}`
- `GET /api/v1/catalog/titles/{title_id}/episodes`
- `GET /api/v1/catalog/search?q=`

## Depends on (calls)

- Elasticsearch (internal, not a separate application in this KB)

## Data owned

- Title, season, and episode metadata
- Genre/cast/artwork data
- Per-territory licensing windows (`available_from`, `available_until`,
  `territories`)

## Events

- Does not currently produce or consume events on the shared event bus.

## Known constraints

- New episodes are scheduled in advance with an `available_from`
  timestamp. Whether an episode is actually playable right now is
  computed at read time (checking the current time against the
  licensing window) -- there is currently no mechanism that notifies
  any other service at the moment an episode's availability window
  actually opens. Nothing downstream is told "this episode just went
  live"; they would have to poll this service to find out.
- Licensing windows can be added or removed by the content ops team at
  any time (e.g. a title pulled from a territory for rights reasons)
  independent of any deploy.

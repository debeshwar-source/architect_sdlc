# user-service

> **DEMO DATA.** This document is synthetic, written for demonstrating
> `sdlc_architect`. It does not describe a real system.

- **Repo:** `https://github.com/streamly/user-service`
- **Team / owner:** Core Platform Team
- **Tech stack:** Python, FastAPI, PostgreSQL
- **Purpose:** Source of truth for user profiles, authentication
  credentials, subscription plan/tier (Basic / Standard / Premium),
  and all per-user preferences.

## Exposes (public API, via api-gateway)

- `GET /api/v1/users/me`
- `PATCH /api/v1/users/me/preferences`
- `GET /api/v1/users/{id}/preferences` -- internal-only, used by other
  services to check a user's notification/communication preferences
  and subscription tier

## Depends on (calls)

- PostgreSQL (own database, not a separate application in this KB)

## Data owned

- User profile records
- Authentication credentials (hashed)
- Subscription plan/tier (billing itself is handled by an external
  billing provider, not represented in this knowledge base)
- Preference settings, including:
  - `notifications.email_enabled: bool`
  - `notifications.push_enabled: bool`
  - `notifications.new_episode_alerts_enabled: bool` (currently unused
    by any producer -- the field exists but nothing emits this kind of
    notification yet)
  - `parental_controls.max_rating: string`
  - `preferred_language: string`

## Events

- Does not currently produce events.
- Does not currently consume events.

## Known constraints

- This service is the ONLY authoritative source for notification
  preferences and subscription tier. Any feature that sends a user a
  notification, or that gates content by plan tier, must check here
  first -- do not duplicate this data elsewhere.

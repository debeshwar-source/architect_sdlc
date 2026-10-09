# notification-service

> **DEMO DATA.** This document is synthetic, written for demonstrating
> `sdlc_architect`. It does not describe a real system.

- **Repo:** `https://github.com/streamly/notification-service`
- **Team / owner:** Growth Team
- **Tech stack:** Node.js, TypeScript, Kafka consumer, SendGrid (email),
  Firebase Cloud Messaging (push)
- **Purpose:** Sends outbound user notifications (email and push).
  Consumes events from the shared event bus and delivers notifications
  according to each user's preferences.

## Exposes (public API, via api-gateway)

- `GET /api/v1/notifications` -- a user's own notification history

## Depends on (calls)

- `user-service` (HTTP) -- checks notification preferences before
  sending anything
- SendGrid, FCM (external, not applications in this KB)

## Data owned

- Notification delivery history/log

## Events

- **Consumes:**
  - `user.signup.completed` (from `user-service` -- not yet wired, see
    constraints)
  - `subscription.renewed` (from an external billing provider, outside
    the scope of this KB)
- **Produces:** none

## Known constraints

- This service already has a generic "consume event → check
  preferences → send notification" pipeline in place. Adding a new
  notification TYPE is normally a matter of adding a new event
  consumer + a message template, not a new pipeline.
- Delivery is via email and push only. There is no in-app real-time
  badge, toast, or websocket channel -- an "instant" or "real-time
  in-app" notification requirement cannot be satisfied by this service
  alone.
- Notification templates live in `templates/` in this repo, one file
  per notification type.

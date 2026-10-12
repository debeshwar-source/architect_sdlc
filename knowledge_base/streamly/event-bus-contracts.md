# event-bus-contracts

> **DEMO DATA.** This document is synthetic, written for demonstrating
> `sdlc_architect`. It does not describe a real system.

- **Repo:** `https://github.com/streamly/event-bus-contracts`
- **Team / owner:** Platform Team
- **Tech stack:** Protobuf schema definitions, published as a versioned
  package consumed by other services (Java, Go, Python, and Node.js
  codegen)
- **Purpose:** The single source of truth for every event schema
  published on the shared Kafka event bus. No service is allowed to
  publish or consume an event whose schema isn't defined here first --
  this repo is the contract layer between producers and consumers.

## Exposes

Not an API -- a versioned schema package (`event-contracts` on the
internal package registry). Services import generated types from this
package rather than hand-writing event payloads.

## Depends on

Nothing. This is a leaf dependency -- everything depends on it, it
depends on nothing.

## Data owned

Schema definitions only, no runtime data.

## Currently defined events

- `user.signup.completed`
- `subscription.renewed`
- `subscription.cancelled`

## Known constraints

- A new event type MUST be added here, published as a new package
  version, and the new package version MUST be adopted by both the
  producer and consumer(s) before either side can actually use it in
  production. This repo change is always a prerequisite, never a
  parallel-track change, for any feature that introduces a new event.
- Schema changes are additive-only by convention (no removing/renaming
  fields on existing events) to avoid breaking consumers on old package
  versions during a rollout.

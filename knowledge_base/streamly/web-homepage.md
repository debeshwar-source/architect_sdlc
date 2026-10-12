# web-homepage

- **Repo:** `to be created`
- **Team / owner:** to be assigned
- **Tech stack:** Static HTML (no framework/backend required by this requirement)
- **Purpose:** Serves a single, static HTML landing page as the default entry point for the web experience, reachable at the platform's root URL with no prior navigation, authentication, or personalization.

## Exposes (public API)

- `GET /` -- returns the static homepage HTML

## Depends on (calls)

- None -- the page is fully static and makes no calls to `api-gateway` or any backend service in this version.

## Data owned

- The static homepage HTML markup/asset itself. No user, catalog, or session data.

## Events

- Produces: none
- Consumes: none

## Known constraints

- Must not include personalized content (Continue Watching, watch history, recommendations), authentication/login, live catalog data, or navigation beyond the single page -- all explicitly out of scope for this version.
- Must render as valid, well-formed HTML in current Chrome, Safari, Firefox, and Edge, and load successfully on first arrival at the base URL with no prior navigation step.

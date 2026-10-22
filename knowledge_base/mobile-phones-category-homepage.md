# mobile-phones-category-homepage

**What it is.** A single new application, `mobile-phones-homepage-web`: a standalone, static frontend that serves a dedicated mobile-phones homepage (featured phones, hero/banner content, navigation entry points). Repo and owner are not yet assigned; the framework is to be chosen by the delivery team.

**Hard constraints**
- Fully standalone: no dependency on any existing application or backend; no APIs called, no events produced or consumed.
- Content is bundled static data; changing it needs a code change and redeploy.
- Exposes one route: `GET /` (or the canonical equivalent).
- Out of scope: product detail pages, checkout/payment/orders, catalog/inventory/pricing backends, other categories' homepages, accounts/auth/personalization.
- Navigation entry points may point at placeholder or future destinations.

The folder also contains `d2cuser-kb/`, a nested generated knowledge base about the D2C user service; it is not part of this application's document set.

## Applications

- `mobile-phones-homepage-web` — Serves a dedicated, standalone homepage focused solely on mobile phones. It is the primary entry point from which visitors discover a curated selection of mobile phone content/products and navigate onward into further mobile-phone-related content or listings.

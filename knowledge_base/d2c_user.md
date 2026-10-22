# D2C-User-Data (repo MythikAI/D2CUser): architect one-pager

## 1. Purpose and boundary
User-data backend and OAuth2 login server for the Mythik streaming app . One Spring Boot 3.5 / Java 21 ser(com.mythik.mythik)vice, PostgreSQL, Redis, AWS. [F]
Owns: identity, profile, coin wallet, payments (Apple, Google, Juspay), subscriptions, content ownership, DRM license proxy, comments, likes, watch progress.
Does not own: catalogue, prices, coin packs, runtime settings, login allow-list. These live in the admin/CMS service, called over Feign. [F]
Size: 454 Java files, ~24k lines; payments is 123 files/~8.8k lines. 60 endpoints, 28 owned tables, 10 external systems. [F]
README says only "CRUD apis for user related tables". It is out of date.

## 2. Neighbours (A -> B means A calls B)
app -> gateway(Envoy) -> service: public paths only [F]
other internal services -> service: "internal" paths, in-cluster [F]
service -> PostgreSQL, Redis, AWS SQS, AWS SNS / End User Messaging (SMS), Firebase Admin [F]
service -> admin service (Feign): content validation, prices, coin packs, settings, allow-list, episodes/chapters [F]
service -> Apple StoreKit, Google Play API, Juspay: verify, status, consume/acknowledge [F]
service -> DoverRunner/PallyCon: DRM licenses [F]
Apple/Google/Juspay -> service: signed webhooks via gateway [F]
K8s CronJobs -> service: HTTP calls; code has no @Scheduled [F]

## 3. Building blocks (package -> role)
security: three filter chains, OAuth2 server, OTP, version gate (426 if app too old)
usersession: optional one-active-device rule (config concurrent_session_mode, default OFF, fails open)
ottuserinfo: login identity + OAuth2 tables. usermetadata: profile; the hub entity, imported by 15 packages
wallet + payment/service/coin: balance and ledger; row lock + version; idempotent credit
payment: purchase flow, webhooks, gateways (one adapter per store), jobs
subscription: plan, user subscription, mandate (store link)
userownership: coin unlock with time windows. drm: license proxy
comment, userlike, watchhistory, continuewatching, userfieldresolver: engagement
queue, cache, store(+client), featureflag, exceptions: plumbing
Dead/unused: admobssv (commented out), featureflag annotations (no prod users), watch-state queue (producer only).

## 4. Trust and security [F unless tagged]
Chain 1: /oauth2/* (authorization server). Chain 2: fixed public list, CSRF off (login, OTP, webhooks, job triggers, guest comments, counts, internal reads, create-user). Chain 3: everything else needs JWT.
Tokens: HS256 JWT, one shared key signs and verifies. Access 15 min, refresh 7 days. Public client mobile-app with PKCE. Claims: sub, authorities, email, phone, language, subscription_status, sid.
Internal endpoints need no token inside the app; safety depends on gateway routes exposing only public paths. DELETE /user-info/{userId} has no ownership check.
Webhook auth: Apple signed-JWS chain; Juspay hashed basic auth (constant-time); Google signed bearer token checked for signature/expiry/issuer only, audience not checked in that class [H].
Defaults for signing key and some credentials are committed in application.properties (values not repeated here).

## 5. Key flows
Login (OTP or Firebase):
1. App POST /mobile/login/auth-request with PKCE challenge; session in Redis 5 min.
2. OTP path: /otp/request (phone must be on allow-list, else waitlist) then /otp/verify. Firebase path: /auth-code with ID token.
3. Service issues 5-min authorization code. App POST /oauth2/token; gets access + refresh.
OTP rules: 6 digits, 5 min, resend after 60 s, 3 failed attempts = 1 h block.
Buy coins or subscribe:
1. App buys in store. Two signals race: client verify (/purchase/verify or /verify-svod) and store webhook.
2. Webhook path: verify signature, log raw, normalize to NormalizedPaymentEvent, publish to SQS payments queue, consumer calls WebhookResolverService, idempotency row, route to TVOD or SVOD processing.
3. Client path builds the same event and processes it directly, not via SQS.
4. Whichever is first creates/completes the order; credits are idempotent. Webhook can create the order itself if it beats the app.
5. Backstops: status polling, hourly SVOD reconciliation, Google consume/ack retry job, order reconciliation job (not scheduled, see risks).
Refund/void: claws back paid coins only, not bonus. Out-of-order subscription events ignored via lastEventAt. Expiry is lazy: access cut at next DRM license request.
Unlock and play:
1. POST /content/play (SEASON or AUDIO_RESEARCH only). Admin validates content and gives price.
2. Price 0 = FREE_ACCESS. Balance < price = error (checked before ownership). Owned = ALREADY_OWNED. Else create ownership and debit = COINS_DEDUCTED.
3. POST /drm/license-proxy: requires an active subscription (ownership check is commented out); builds signed vendor token; license 24 h, non-persistent.
Ownership windows: purchase 720 h, then 48 h after first play (both config). Valid only while both are open.
Engagement: comments post to SQS then consumer saves as PENDING; comment like counts recomputed by a job; content likes immediate, is-liked cached up to 24 h; watched = 95% progress, parent complete only when all children are; stale events ignored.

## 6. Data
Domains: identity (ott_user_info, oauth2_* x3, user_session, waitlist); profile (user_metadata, 3 preference tables); money (wallet, coin_transactions, user_order, payment_transaction, payment_events, processed_webhooks, raw_webhook_log, voided_purchase_log); subscriptions (subscription_plan, user_subscription, subscription_mandate); access (user_ownership); engagement (comment, user_comment_like, user_like, user_watch_history, user_continue_watching); ops (ott_job_details).
Read via admin, not SQL: client_configuration, login_access, service_configuration.
Soft delete on ott_user_info, user_metadata, wallet. user_continue_watching is read-only in code; its writer is not in this repo [F].
Redis: otp:value/meta, blocked, mobileAuthSession, L2 cache, web sessions.
Schema: no migration tool. Manual PR-numbered db-updates.sql. ddl-auto=update by default, validate in K8s with a TODO.

## 7. Runtime and delivery
One image (Corretto 21 + OpenTelemetry agent). Deployment = app + otel-collector sidecar; Service 80->8080; HPA; Envoy route exposes public paths only; CORS policy. Stateless; L1 caches are per pod, no cross-pod invalidation.
Defaults OFF: queue.enabled, L1/L2 cache, AOP logging. Check production values.
CI: PR workflow (tests, package, Docker build). Deploy workflow (push to dev or manual): tests+Sonar, ECR push, applies configmaps, service account, deployment, service only. CronJobs, HPA, route, security policy are not applied by it.
CronJobs: comment-like sync every minute; Google consume every 2 min; SVOD reconciliation hourly. No CronJob for order reconciliation-job.
Tests: 54 files; payments 8, comments 6, subscriptions 6. No e2e or contract tests seen.

## 8. Design rules that matter
Access model is split three ways: DRM = subscription only; "locked" flag = subscription tier only; ownership = coins. Coin buyers without a subscription get no license.
Admin outage degrades silently: prices 0 (looks free), lists empty, language ENG.
Session control fails open; feature flags fail closed (true only for "true").
Profile update replaces preference sets; omitted fields erase them.

## 9. Top risks (ranked)
1. Payment events can be lost: queue off by default, publish failure unnoticed, consumer deletes failed messages, webhook still returns 200.
2. Order reconciliation job has no scheduler.
3. Price lookup failure = free content.
4. DRM gate ignores coin ownership; policy undecided.
5. Comment-like CronJob uses GET with no token; endpoint is POST behind JWT. Likely never works [H live].
6. Internal endpoints open in-app; shared HS256 key; committed defaults.
7. Google webhook audience not checked [H].
8. No migration tool; deploy pipeline skips CronJobs/HPA/route.
9. Already-owned item refused when balance is low.
10. Bonus coins not clawed back.

## 10. Decisions needed
Access policy (coin vs subscription vs either). Bonus clawback. Single-device on in prod? Admin-outage behaviour. Token key strategy. Queue mandatory? Owner of user_continue_watching. Where comment moderation happens.

## 11. Where to read more
Condensed set: d2cuser-kb-condensed (09 files). Detailed: d2cuser-kb (90 notes, graph/edges.json for all links). Open questions with file references: d2cuser-kb/open-questions.md.
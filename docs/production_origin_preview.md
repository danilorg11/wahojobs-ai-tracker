# Production-origin + preview-only `/jobs` proof

This milestone creates a production-grade public origin while keeping the real
`www.wahojobs.com` route unchanged. The only Vercel route eligible to reach the
new origin is exact `/jobs` in a Vercel **Preview** environment. The gateway's
production-environment behavior is hard-coded to the current legacy site.

## Boundaries

- The origin owns guest `GET` and `HEAD /jobs` only.
- Homepage, job detail, company, online-jobs, static, random, robots, sitemap,
  WorkOS, account, profile, tracker, and action paths are rejected by origin.
- No browser Cookie, Authorization, or Origin header reaches the catalog.
- The Python process binds to loopback and also verifies the 43-character
  origin secret. Caddy verifies the same secret before proxying.
- The core catalog renderer still receives the exact configured public Host and
  no proxy headers; normalization happens only after loopback and secret checks.
- Preview configuration rejects `wahojobs.com` and `www.wahojobs.com` as its
  public origin.

## Public-only database

`scripts/build_public_catalog_preview_database.py` opens the reviewed source
database read-only, asks the existing catalog policy for its currently eligible
public inventory, and writes a fresh SQLite database. Only companies,
canonical opportunities, representative jobs, qualifying crawl runs, and
automatic opportunity enrichment documents are projected.

The output schema retains empty compatibility tables, then attests that every
non-catalog table has zero rows. It does not copy account, identity, session,
profile, invitation, WorkOS, source-body, override-actor, pipeline, applicant,
or M009 public-route identity data. The runtime pins the projection's SHA-256,
opens it immutable/read-only, runs SQLite integrity and foreign-key checks, and
rechecks those invariants for readiness.

## Privacy-safe operations

The origin emits one JSON event per request containing only a generated request
ID, normalized method class, route class, status, and bounded duration. It does
not log the remote address, raw target, query, Host, cookies, authorization,
referrer, user agent, request body, job query, or response body. Caddy access
logging is not enabled. Vercel logs the same bounded routing fields.

Token-protected operator endpoints are:

- `/__origin/live`
- `/__origin/ready`
- `/__origin/metrics`

They are not routed by the Vercel gateway. The metrics contain only aggregate
counts for jobs, health, and rejected requests so a proof can show that legacy
probes never touched the Python process.

## Preview routing and rollback

The standalone Vercel gateway defaults every path to the live legacy site. Its
exact `/jobs` function reaches the new origin only when `VERCEL_ENV=preview`
and `WAHOJOBS_PREVIEW_JOBS_ENABLED=1`. A disabled flag or any Vercel production
environment retrieves legacy `/jobs` instead. Origin timeouts and failures are
visible as uncached preview 502/503 responses; they are not mislabeled as
legacy responses. The gateway forces browser and CDN `no-store` headers so a
rollback cannot be delayed by a cached catalog response. An explicit `/` rule
precedes the legacy wildcard because Vercel's `/:path*` pattern does not own the
empty homepage path.

The required rollback proof is:

1. record origin metrics;
2. verify enabled preview `/jobs` is marked `new-origin` and increments only the
   jobs counter;
3. verify representative legacy paths match direct legacy status, redirect,
   and body and do not increment origin counters;
4. make the preview origin unavailable and observe a bounded 502/503;
5. set the preview-only enable flag to `0`, redeploy that preview, and verify
   `/jobs` exactly matches current legacy `/jobs`;
6. re-enable only after the origin is ready.

No custom domain is attached to the preview gateway. Publishing `/jobs` on
`www` remains a separate approval after exact job-detail routing is covered.

## Verified deployment evidence (2026-08-21)

- DigitalOcean runs one Ubuntu 24.04 Basic 1 GiB Droplet named
  `wahojobs-public-catalog-preview`. The Python service is active and enabled as
  `wahojobs-preview`; it listens only on `127.0.0.1:8080`. Caddy and UFW are
  active and enabled, with only SSH, HTTP, and HTTPS allowed inbound.
- The persistent projection SHA-256 is
  `1785d431e6ab83d908ca74829bd40f15b2d4c6d91d40e172908a39a081a0726a`.
  It contains 171 public jobs/opportunities, 171 automatic enrichments, and
  three companies. All compatibility tables for profiles, pipeline, applicant,
  source bodies, overrides, and job events are empty; account, session,
  invitation, WorkOS, and public-job-identity families are absent.
- Vercel project `danrgdy-projects/wahojobs-hybrid-preview-proof` has only
  `vercel.app` aliases and retains Vercel Authentication. Its three origin
  variables target Preview only; the isolated project's production deployment
  is hard-coded to `legacy-fallback`.
- Authenticated Preview `/jobs` returned `200`, owner `new-origin`, origin marker
  `public-catalog-preview`, `no-store`, and exactly one jobs-counter increment.
  Homepage, representative job/company/online-jobs/static paths, and a random
  path matched current legacy status, redirect, and body byte-for-byte while
  producing zero origin jobs/rejected-counter increments.
- Missing and incorrect origin tokens returned `403`; an authenticated,
  non-owned company path returned `404` at ingress.
- With the Python service stopped, Preview `/jobs` returned an uncached `502`.
  Setting only the Preview enable flag to `0` and redeploying returned owner
  `legacy-fallback` and matched current legacy `/jobs` status, redirect, and body
  while the origin was still stopped. The service and Preview flag were then
  restored and readiness plus final `new-origin` ownership reverified.
- Real `https://www.wahojobs.com/jobs` remained the legacy `307` to `/404`.
  No Wahojobs custom domain, DNS, WorkOS, Karl, wildcard route, robots, sitemap,
  or real production routing was changed.

## Post-audit exact-path remediation (2026-08-21)

The Preview gateway now validates the original request pathname inside the
function before it reads origin configuration or performs a fetch. Exact
`/jobs` remains eligible for the new origin; direct `/api/jobs` returns an
uncached `404` with owner `rejected` and zero origin traffic. The Node test was
moved to repository-level `tests/`, outside the Vercel project root.

Preview deployment `dpl_3ETcHViYr7Cfij8SFjFWa7ysXEqy` built only
`api/jobs`, and the dedicated preview alias points to that deployment. Live
proof increased the origin jobs counter from three to four for `/jobs`; direct
`/api/jobs`, legacy `/api/jobs.test`, homepage, job, company, online-jobs,
static, and random probes left it at four with zero rejected-origin requests.
Real `www.wahojobs.com/jobs` remained the legacy `307` to `/404` before and
after the proof.

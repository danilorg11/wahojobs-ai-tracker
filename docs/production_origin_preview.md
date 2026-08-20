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
visible as preview 503s; they are not mislabeled as legacy responses.

The required rollback proof is:

1. record origin metrics;
2. verify enabled preview `/jobs` is marked `new-origin` and increments only the
   jobs counter;
3. verify representative legacy paths match direct legacy status, redirect,
   and body and do not increment origin counters;
4. make the preview origin unavailable and observe a bounded 503;
5. set the preview-only enable flag to `0`, redeploy that preview, and verify
   `/jobs` exactly matches current legacy `/jobs`;
6. re-enable only after the origin is ready.

No custom domain is attached to the preview gateway. Publishing `/jobs` on
`www` remains a separate approval after exact job-detail routing is covered.

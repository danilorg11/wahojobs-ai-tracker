# Vercel hybrid-routing preview gateway

This directory is deployed as a separate Vercel project with no custom domain.
Its default behavior proxies every route to the current `www.wahojobs.com`
site. Exact `/jobs` and the two paths in `release-manifest.json` use the new
origin only when all of the following are true:

- the Vercel environment is `preview`;
- `WAHOJOBS_PREVIEW_PUBLIC_ROUTES_ENABLED=1`;
- `WAHOJOBS_NEW_ORIGIN_URL` is an exact HTTPS origin; and
- `WAHOJOBS_ORIGIN_AUTH_TOKEN` is the matching 43-character secret.

Both functions independently check the original request pathname before
reading origin configuration or making any upstream request. Direct filesystem
access at `/api/job` or `/api/jobs` therefore returns an uncached `404` with
owner `rejected` and cannot reach either origin. Gateway and origin also require
the same release ID. Tests live outside this Vercel project directory so they
cannot be built as functions.

Production-environment deployments and disabled previews fetch legacy for all
three exact routes. There is no wildcard or pattern rewrite for `/job/*`:
unpublished job paths flow through the ordinary legacy catch-all without
invoking either function. No company, WorkOS, robots, sitemap, DNS, or
custom-domain ownership exists here. The function strips browser cookies and authorization,
sets an origin request ID, forces browser and CDN `no-store`, and emits
privacy-safe route logs. The homepage has its own explicit legacy rewrite
because Vercel's catch-all pattern does not match the empty path.

The proof project may retain Vercel Authentication. Use `vercel curl` for
operator probes so protection is bypassed through the authenticated CLI without
making the Preview public. Vercel assigns a first deployment to its production
environment; the code deliberately makes that isolated bootstrap deployment a
legacy fallback. Only later Preview deployments can reach the new origin.

`scripts/render_public_job_preview_routes.py` renders `vercel.json` and the
origin Caddy boundary from the exact attested manifest. Its `--check` mode is a
release gate: a changed manifest cannot be deployed with stale route files.

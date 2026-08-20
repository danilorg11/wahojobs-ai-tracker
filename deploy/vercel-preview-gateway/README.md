# Vercel hybrid-routing preview gateway

This directory is deployed as a separate Vercel project with no custom domain.
Its default behavior proxies every route to the current `www.wahojobs.com`
site. Exact `/jobs` uses the new origin only when all of the following are true:

- the Vercel environment is `preview`;
- `WAHOJOBS_PREVIEW_JOBS_ENABLED=1`;
- `WAHOJOBS_NEW_ORIGIN_URL` is an exact HTTPS origin; and
- `WAHOJOBS_ORIGIN_AUTH_TOKEN` is the matching 43-character secret.

Production-environment deployments and disabled previews fetch legacy `/jobs`.
No wildcard job/company, WorkOS, Karl, robots, sitemap, DNS, or custom-domain
ownership exists here. The function strips browser cookies and authorization,
sets an origin request ID, disables caching, and emits privacy-safe route logs.

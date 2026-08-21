# Preview-only public catalog and job-detail release

This milestone proves the public `/jobs` catalog and two registry-published job
details on an isolated Vercel Preview gateway and a dedicated DigitalOcean
Preview origin. It does not attach a custom domain or change
`www.wahojobs.com`, DNS, WorkOS, or either legacy production droplet.

## Exact routing contract

The generated Vercel boundary owns only:

- `/jobs`
- `/job/handshake-ai-evaluation-specialist-j125e8ced56da8007c92ab964f58f9f0f`
- `/job/oneforma-karl-llm-1`

There is no `/job/*` pattern. Every other job path uses the ordinary legacy
catch-all without invoking a detail function. The detail function independently
requires an exact manifest path in case it is invoked directly or after
platform normalization. Direct `/api/job` and `/api/jobs` return an uncached
gateway `404` and make no upstream request.

All three exact routes reach the new origin only when `VERCEL_ENV=preview` and
`WAHOJOBS_PREVIEW_PUBLIC_ROUTES_ENABLED=1`. Disabled Preview deployments and
all deployments in Vercel's production environment retrieve the current legacy
response. Origin failures are exposed as an uncached `503`; they never silently
claim a legacy response.

`scripts/render_public_job_preview_routes.py` generates both `vercel.json` and
the Caddy exact-path matcher from the release manifest. Its `--check` mode
prevents a changed manifest from shipping with stale boundaries.

## Registry, projection, and release identity

The release inputs in `deploy/public-job-preview` contain exactly two serving
identities, two primary paths, no aliases, and two canonical-key bindings.
Karl is fixed to public ID `j7b8550e11700c9b26ac68deb753e1f82`, path
`/job/oneforma-karl-llm-1`, and canonical key `oneforma::177080`. The other
entry must be a strict lowercase new-format path whose suffix equals its public
ID.

The public-only projection builder copies the 171 catalog-eligible jobs plus
Karl's historical detail row, runs M009, imports the exact registry, binds the
two identities, and attests all private compatibility tables as empty. The
frozen Preview release contains:

- database SHA-256:
  `9303ade8fa93b086e7c781059d2cf14e655a200a9ab4dad2bd147cab3da44400`
- registry SHA-256:
  `59d0295a7edcfa7b41767abb97b84e560e77b60c1da355d55c52bc1ee1fdbccb`
- release ID:
  `bc74cbbc24f5d6b05e9c2e08ac3edc93f77e357e6c1d450760f8b4bb00298743`
- 4 companies, 172 opportunities, 172 jobs, and 172 automatic enrichment
  documents
- exactly 2 identities, 2 paths, and 2 bindings

The release ID is a SHA-256 commitment to the projection digest, registry
digest, exact paths, public IDs, and canonical keys. The gateway sends that ID
on every owned request; the origin rejects a missing or different value with
`409`, and the gateway rejects a response that does not echo the same value.

## Catalog link publication

When the release gate is enabled, `/jobs` asks the M009 registry for each
catalog canonical ID. A serving manifest binding gets its exact primary path.
Every other card uses its validated official source URL, or no link if it has no
safe official URL. No temporary `/job/opportunity-<database-id>` URL is emitted.

The active canary appears on unfiltered page 6 and in the search for “AI
Evaluation Specialist.” The live search response contained two internal job
`href` occurrences—title and button for the same manifest path—and no Karl or
temporary internal path. Vercel normalizes query spaces to `%20`; the gateway
normalizes only that equivalent spelling to `+` at the trusted origin hop so
space-containing searches do not self-redirect.

## Origin boundary

The Python process binds only to `127.0.0.1`. Caddy exposes only exact
`GET`/`HEAD` requests for the three published routes and token-protected
operator health routes. Unknown job paths return `404` at Caddy before Python.
Missing origin auth returns `403`; missing or incorrect release identity on an
owned public path returns `409` from Python.

The application opens the projection immutable/read-only, verifies its digest,
SQLite integrity, foreign keys, exact table set, empty private tables, M009
consistency, exported registry digest, exact path/binding rows, and rebuilt
release identity at startup and readiness. Logs contain only request ID, method
class, route class, status, and bounded duration.

## Live proof (2026-08-21)

Final gateway deployment `dpl_7H7vy6SacymjYXdHn7L6QVmb7cCx` is assigned only
to `wahojobs-hybrid-preview-jobs.vercel.app`. The isolated project's original
Vercel production deployment remains at
`wahojobs-hybrid-preview-proof-m362d8eyo-danrgdy-projects.vercel.app` and stays
hard-coded to legacy fallback. All aliases are `vercel.app`; no Wahojobs custom
domain is attached.

The dedicated origin runs code commit
`dde3be92095903a6e2006c4b2b671fc3b370805e`. Both services are active and
readiness is `200`.

Enabled routing proof:

- `/jobs?q=AI+Evaluation+Specialist`, the strict new detail, Karl, and new-detail
  `HEAD` returned `200`, owner `new-origin`, origin marker
  `public-catalog-preview`, the exact release ID, and `no-store`.
- Unknown syntactically valid IDs, uppercase paths, percent-encoded slug/ID and
  separator forms, trailing slash, malformed IDs, double slash, unrelated
  legacy paths, `/jobs/`, and Karl case/encoding variants remained legacy-owned.
- Representative legacy paths matched current production status, location,
  byte length, and SHA-256. Unknown/uppercase/encoded/malformed paths shared the
  current legacy body SHA
  `29e8cece8d18470002c2b1c2d39fcd1d3b904f37ee45c2419f457bfe77218978`.
- Direct `/api/job`, `/api/jobs`, and query forms returned gateway `404` with
  owner `rejected`; an owned `POST` returned `405`. None reached the origin.
- Around all legacy and attack probes, origin counters were unchanged at
  `details=7`, `jobs=5`, and `rejected=2` (the two rejects were intentional
  direct-origin release-mismatch probes made before the baseline).
- With only the Python service stopped, all three exact routes returned
  uncached `503 new-origin`; an unknown job path still returned its legacy
  response. The service was restored and readiness reverified.

Rollback proof:

1. Set only `WAHOJOBS_PREVIEW_PUBLIC_ROUTES_ENABLED=0` in Preview and redeploy.
2. Deployment `dpl_7DVPPLvURuHensZuEFvrBrkHsoLc` returned
   `legacy-fallback` for `/jobs`, its query, the new canary, and Karl.
3. `/jobs` matched production `307 /404` and body SHA
   `97c6ea1a52363d7e8a7af6f9d9da2a1dcd3a46bcc3bd3f6d98011b4dd41a0eee`;
   Karl matched production `200` and body SHA
   `5e9b0671e29877e20c3de2b25e53409f16908d8952a9e227c1f07590e84f3ccc`.
   The new canary matched the current legacy `500` response and body SHA
   `29e8cece8d18470002c2b1c2d39fcd1d3b904f37ee45c2419f457bfe77218978`.
4. Origin counters remained `details=0`, `jobs=0`, and `rejected=0`.
5. Restore the same flag to `1`, redeploy, and reassign only the dedicated
   Preview alias. Final counters after one catalog and both details were exactly
   `jobs=1`, `details=2`, and `rejected=0`.

The origin updater also demonstrated its independent restoration guard: its
first start check raced the process bind, automatically restored the prior
database/configuration/Caddy/release, and left both services healthy. A bounded
readiness retry was added and the same attested release then installed
successfully, preserving the prior release under the rollback directory.

## Production preservation and next milestone

After the final Preview restoration, real production remained:

- `/jobs`: `307` to `/404`, 17,879 bytes, SHA
  `97c6ea1a52363d7e8a7af6f9d9da2a1dcd3a46bcc3bd3f6d98011b4dd41a0eee`
- Karl: `200`, 147,680 bytes, SHA
  `5e9b0671e29877e20c3de2b25e53409f16908d8952a9e227c1f07590e84f3ccc`
- unknown new-format ID: current legacy `500`, 1,995 bytes, SHA
  `29e8cece8d18470002c2b1c2d39fcd1d3b904f37ee45c2419f457bfe77218978`

Real `/jobs` publication still requires a separately approved production
milestone: publish a reviewed production release, establish its refresh and
atomic gateway/origin rollout procedure, configure the real public boundary,
and remove Preview-only deployment protection for that public route. This
Preview proof does not grant permission to attach `www`, change DNS, or expand
the identity registry.

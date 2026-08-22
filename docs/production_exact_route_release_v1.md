# Production Exact-Route Release v1

This milestone is an offline release package. It prepares and verifies production-
specific artifacts but does not publish routes, deploy services, alter domains, or
mutate a production database.

## Ownership boundary

The activation document owns exactly these literal request paths:

- `/jobs`
- `/job/handshake-ai-evaluation-specialist-j125e8ced56da8007c92ab964f58f9f0f`

Karl's exact legacy path and every unlisted path remain legacy-owned. The gateway
and origin have no `/job/*` wildcard, no catch-all route, and no legacy fallback.
The rollback document contains zero production-owned routes, making later rollback
a single replacement/removal of the exact activation rules.

## Offline build and attestation

Run from the repository root with an immutable input snapshot. The builder opens
the source SQLite database in read-only mode and creates new output files only.

```powershell
python -B scripts/build_public_catalog_production_database.py `
  --source data/wahojobs.sqlite `
  --output deploy/public-job-production/catalog.sqlite3 `
  --manifest deploy/public-job-production/projection-manifest.json `
  --registry deploy/public-job-production/registry.json `
  --bindings deploy/public-job-production/bindings.json `
  --release-manifest deploy/vercel-production-gateway/release-manifest.json `
  --observed-at <fixed-UTC-time>

python -B scripts/render_public_job_production_routes.py --check

python -B scripts/attest_public_catalog_production_artifact.py `
  --database deploy/public-job-production/catalog.sqlite3 `
  --release-manifest deploy/vercel-production-gateway/release-manifest.json
```

The attestation verifies the database digest, schema, integrity and foreign keys;
the one-row path/identity/binding registry; an empty private-data surface; exact
release reconstruction; authenticated and release-pinned readiness; production
canonical URLs; production no-store headers; exact card links; and Karl rejection.
It also parses every `href` emitted by the landing page and Handshake search and
rejects any internal path outside `/jobs` and the exact published canary. The
production origin omits matching and login navigation; auth-enabled integrations
retain their existing navigation through the default rendering policy.
The runtime guard accepts only canonical root-relative published paths or
canonical absolute HTTPS URLs. Backslashes, HTTP and scheme-relative spellings,
noncanonical authorities, credentials, same-host ports, and trailing-dot hosts
fail closed before any browser normalization can occur.

## Generated review documents

- `deploy/production-exact-route-v1/route-publication-activation.json` is a
  provider-neutral review document with two exact routes and preservation probes.
- `deploy/production-exact-route-v1/route-publication-rollback.json` is the
  corresponding zero-route rollback state.
- `deploy/vercel-production-gateway/vercel.json` is the exact gateway route table.
- `deploy/public-catalog-production-origin/Caddyfile` is the exact authenticated
  origin boundary and is not a DigitalOcean deployment instruction.

The activation destination is intentionally symbolic. A future separately approved
production change must substitute the immutable gateway deployment identifier and
must verify the immutable origin hostname/TLS certificate and bearer-token setup.

## Future activation guardrails

Before any separately approved activation, stop if the public projection or
registry hashes differ, the origin does not pass authenticated release-pinned
readiness, either exact route produces a redirect or non-200 response, cache headers
allow storage, canonical URLs are not on `https://www.wahojobs.com`, an unowned path
reaches the new gateway, any preservation probe changes, or the rollback document
cannot be applied as one route-table replacement/removal action.

No production activation is part of this milestone.

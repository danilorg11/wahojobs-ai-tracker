# Local development inventory refresh

Use the existing `scripts/crawl.py` entry point with an explicit `--db` for the
new authenticated application. No diagnostic database, special preview bridge,
alternate crawler, scheduler, or default-configuration change is required.
This workflow does not target the public website or a deployed service.

## Select and prepare the database

Choose one dedicated local development file outside Git checkouts. This example
uses `C:\Users\danrg\AppData\Local\Wahojobs\development\inventory.sqlite3`.
Do not substitute the workspace database or either running preview/QA database.
The selected file must already exist, be canonical and single-linked, and have
no SQLite sidecars. The refresh opens it in existing-file mode and acquires the
existing offline-operator lifetime ownership. A running authenticated app using
that file blocks refresh before catalog requests or crawl-run writes.

For a **new, nonexistent** target only, initialize the existing base schema and
source configuration using its existing explicit-path API. From the repository:

```powershell
python -B -c "from pathlib import Path; from wahojobs.db.repository import initialize_database; p=Path(r'C:\Users\danrg\AppData\Local\Wahojobs\development\inventory.sqlite3'); assert not p.exists(), 'Target already exists'; p.parent.mkdir(parents=True, exist_ok=True); initialize_database(p)"
```

Then provision the normal authenticated-app schema using the existing migration
commands, in order. Stop on any failure. This is initial setup, not a refresh:

```powershell
$inventoryTarget = 'C:\Users\danrg\AppData\Local\Wahojobs\development\inventory.sqlite3'
$migrationNames = @('pipeline_state_migration', 'accounts_migration', 'ownership_migration', 'persistent_profiles_migration', 'persistent_profile_canonical_v2_migration', 'google_oidc_authorization_transactions_migration', 'closed_schema_convergence_migration')
foreach ($migrationName in $migrationNames) {
    python -B "scripts/$migrationName.py" --db $inventoryTarget --yes
    if ($LASTEXITCODE -ne 0) { throw "Migration failed: $migrationName" }
}
```

An existing, correctly provisioned development database needs neither setup nor
replacement. Keep its account/profile state. Inventory refresh does not migrate,
copy, import, or replace accounts, profiles, confirmed revisions, or sessions.
They currently share the selected physical database with inventory; isolation is
by the existing table/service contracts, not a new second-database architecture.

## Inspect before authorizing requests

Stop only the app owning the selected target before its offline refresh. Leave
other preview/QA processes and databases alone. Inspection itself is read-only:

```powershell
python -B scripts/crawl.py alignerr mercor micro1 --db "C:\Users\danrg\AppData\Local\Wahojobs\development\inventory.sqlite3" --details needed --inspect
```

The JSON identifies the resolved database, configured catalog destinations and
methods, first paginated URLs, existing official detail candidates, detail-reuse
policy, and completeness/failure boundaries. It performs **zero HTTP/model
requests and zero database writes**. It cannot know new returned IDs, changed
catalog material, page counts, or the final detail-request count before retrieval.
Review the printed target and destinations; inspection is not authorization.

With the standard source configuration, the external scope is:

| Source | Catalog requests | Detail requests with `--details needed` |
| --- | --- | --- |
| Alignerr | GET `https://www.alignerr.com/api/jobs?limit=120&offset=0`, then adapter-validated pages | Exact returned, identity-validated `https://www.alignerr.com/jobs/<id>` URLs when accepted details are missing or catalog material changed |
| Mercor | GET `https://aws.api.mercor.com/work/listings-explore-page` | None; structured fields and description geography come from the listing |
| micro1 | POST `https://prod-api.micro1.ai/api/v1/job/portal?page=1&limit=100&keyword=`, then adapter-validated pages; body `{"action":"get_all_jobs","filters":{"type":["EXPERT"]}}` | Exact returned, identity-validated `https://jobs.micro1.ai/post/<id>` URLs when needed |

Recorded query parameters on detail URLs are retained. Each detail request is one
GET with no redirect, retry, asset fetch, application traversal, or model call.
Catalog redirects are rejected before dispatch. This explicit development batch
permits at most 1,000 HTTP transactions, including at most 500 detail requests
shared across Alignerr and micro1, with no retries. Attempts are counted before
dispatch, including failed attempts. The CLI prints the request ledger/counts on
exit; it never retries a failed source. A transport failure is recorded by the
pipeline and the remaining explicitly selected sources may still run.

Alignerr retains its 100-page/20,000-record safety bounds and 90-second timeout.
Mercor makes one catalog request with a 30-second timeout. micro1 is limited to
50 pages and 5,000 returned records (before deduplication), with its existing
60-second timeout. An oversized declared total stops collection early as partial;
an unexpectedly oversized response is reported as observed, not silently truncated
or promoted as complete. Reaching a catalog ceiling withholds completeness and
absence-removal authority. Detail requests retain their 25-second timeout and
2 MB response limit. Detail-budget exhaustion reports `pending`, preserves accepted
content and does not alter the catalog's completeness decision.

This command selects only these three sources. Existing
other-provider inventory is retained; the initializer's additional configured
sources are not automatically refreshed. Other sources require their own explicit
selection and scope review. No external refresh was performed to validate this
workflow: tests use captured content and mocked catalog transports.

## Refresh, then run the normal application

Only after authorization for that printed external scope, remove `--inspect`:

```powershell
python -B scripts/crawl.py alignerr mercor micro1 --db "C:\Users\danrg\AppData\Local\Wahojobs\development\inventory.sqlite3" --details needed
```

Explicit local refresh uses deterministic enrichment and never enables optional
model enrichment, even if it is enabled in the environment. The unchanged legacy
invocation without `--db` still uses its existing default; do not use it for this
workflow. Catalog-only `--db` without `--details` remains possible but does not
recover missing Alignerr/micro1 details.

Set `database_path` in a separate normal development configuration JSON to this
**same exact file**. Follow [the existing authenticated launcher configuration and
account workflow](durable_google_login_browser.md); keep its secret files outside
Git. Launch from the corrected repository code:

```powershell
python -B scripts/durable_google_login_app.py --config "C:\Users\danrg\AppData\Local\Wahojobs\development\app.json"
```

The configuration is independently provisioned under the existing documented
contract; this refresh does not generate credentials, create a synthetic login
bypass, or choose a port. Use its configured HTTPS origin and `/find-matches`.
Normal cards, exact-variant `/job/opportunity-<id>?variant=<id>` navigation, profile
editing, authentication and eligibility use the same database. The normal launcher
delegates these job paths to the existing authenticated detail handler.

Refresh is offline: finish it before restarting this target's app. Do not replace
the database file under a running runtime. Restart drops in-memory recommendation
contexts; the existing revision checks also reject changed inventory evidence if
an old context is presented in a supported composition.

## Evidence and failure behavior

- Detail HTTP allowance is reserved equally for selected detail-capable sources:
  Alignerr and micro1 receive 250 each; a single selected source receives 500.
  Duplicate source arguments do not reserve extra allowance. Any integer remainder
  is assigned by source name. Failed attempts consume allowance; compatible reuse
  does not. Missing usable exact-variant details precede rechecks of accepted
  content within each source. No regional/canonical content sharing is introduced.
- The existing sequential refresh releases unused allowance to sources still
  awaiting their turn, including when a catalog fails. It does not revisit a source
  already processed, so unused allowance from the last source can remain unused.
  Both global ceilings still apply, including catalog transactions. An exhausted
  total budget can prevent later details even with a reserved detail allowance.
  Inspection shows initial allocations; the final request-usage report shows each
  source's effective allocation, requests, released/unused allowance and recovery
  counts. `pending` means skipped for budget; `failed`/`held` remain separate.
  `recovery: null` means detail processing was not reached, not zero backlog.

- Mercor remains a partial listing source. Only returned records satisfying its
  accepted individual-observation contract advance. Missing/invalid lifecycle
  evidence does not gain authority from HTTP success. Absent records are neither
  renewed nor removed by the partial response. Structured applicant restrictions
  and accepted description restrictions are prepared by the existing adapter.
- Alignerr/micro1 removal still requires their existing validated complete
  snapshot contract. Partial or failed retrieval is not a complete empty catalog.
- Detail recovery runs after committed catalog ingestion, outside matching and
  outside a write transaction during HTTP. It uses the existing content-only
  reprocessing/acceptance contract, source identity and canonical enrichment.
  It does not advance catalog observations, renew absent records, or grant
  snapshot completeness. A detail also needs accepted catalog authority; fetching
  text cannot repair missing catalog authority by itself.
- `needed` reuses dated accepted details for the same source variant when no
  catalog-material change is observed. This does **not** prove that the public
  detail page has stayed unchanged. To inspect detail-only changes, separately
  authorize `--details all`, which requests all returned detail records. Neither
  mode retrieves absent records. Source identity/version checks still apply.
- Failed, empty, truncated or mismatched detail responses preserve valid accepted
  content. Held/failed/reused/accepted counts appear in the crawl summary. A fatal
  catalog or integrity error stops execution; previously completed source updates
  are not rolled back as a multi-source transaction. Failed network attempts are
  reported without retry; subsequent selected sources may run within the same
  shared budget. Do not rerun sources blindly.
- Original qualification wording, alternatives, preferred/required distinctions,
  opportunity type and exact application destination remain source evidence.
  Unknown fields stay unknown. Freshness still uses genuine catalog observations
  and the app's current clock; old saved captures are not relabeled current.
- Catalog observation and description recovery do not verify external application
  acceptance, an active vacancy, candidate eligibility, or successful submission.

## Offline verification

`tests/test_local_inventory_refresh.py` mocks listing/detail transports around the
ordinary adapters and `run_crawl`; all seven saved detail bodies retain their
original capture times and hashes. Synthetic envelopes/failure variants are
identified as such. It covers unchanged reuse, catalog changes, failed/empty/
mismatched details, partial/failed catalogs, Mercor observation/geography, explicit
target inspection, standard initial migrations, and active-runtime exclusion.

`tests/test_local_inventory_app.py` uses fresh disposable databases and the normal
configured HTTPS launcher with the existing in-memory login test provider. It
checks all seven details, exact variant/application identity, the synthetic biology
profile, future-opportunity wording, geography exclusion, anonymous access, method
boundaries, account/profile preservation and old-context invalidation. It does not
refresh or operate either existing preview, contact public providers, or replay a
full benchmark.

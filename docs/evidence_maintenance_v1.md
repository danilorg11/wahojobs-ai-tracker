# Evidence maintenance v1

One operational entry point: `python -B scripts/evidence_maintenance.py`.

Inspection and plans include a per-source `coverage` summary: verification and
accepted-body counts, derived freshness, held latest observation IDs, oldest
known verification age, unknown ages, and the applicable operation IDs with
their blocked permissions, prerequisites and write scope. This summary uses the
same evidence as the detailed records. A held observation does not replace an
accepted full body; source trust alone does not establish candidate eligibility.
The default is help; inspect and plan are read-only. No route, timer, scheduler
or background worker invokes maintenance.

## Maintained execution plan

1. Verify baseline/contracts and isolate feature — complete.
2. Connect inspection, bounded plans, explicit execution and durable recovery — complete.
3. Add offline two-cycle demonstration and consumer/preservation tests — complete.
4. Validate required 636 IDs and affected source/enrichment/durable/continuity/onboarding selections — fixed-tree results indexed in the milestone bundle.
5. Independent review, corrections, re-review and feature commit — independent review approved; final commit and validation disposition recorded in the milestone handoff.

Baseline: `58087ab69815910a3d1d2f991dd57b9ce72e5dca`.
The milestone bundle's INDEX.md records validation and delivery. No external
provider/model/identity requests, real databases, app activation, 8802 access,
recurring execution, main integration or push are authorized for this milestone.

## Inspect, review, execute, report

Run from the checkout using the approved CPython 3.12 environment and pinned
requirements.lock dependencies. Select an absolute existing local database
outside every Git checkout and one durable private journal directory.
Inspection does not migrate, log in, initialize preparation storage, or inspect
other owners' private profiles.

PowerShell example for a **separately authorized future source-only operation**:

```powershell
$Database = 'C:\operator-data\inventory.sqlite3'
$Journal = 'C:\operator-data\maintenance-journal'
$Plan = 'C:\operator-data\mercor-plan.json'
python -B scripts/evidence_maintenance.py inspect --db $Database --providers mercor --phase source --details none
python -B scripts/evidence_maintenance.py plan --db $Database --providers mercor --phase source --details none --http-limit 1 --detail-limit 0 --out $Plan
Get-Content -LiteralPath $Plan
python -B scripts/evidence_maintenance.py execute --plan $Plan --journal $Journal --yes --allow-provider-requests
$PlanId = (Get-Content -Raw -LiteralPath $Plan | ConvertFrom-Json).plan_id
python -B scripts/evidence_maintenance.py report --journal $Journal --plan-id $PlanId
python -B scripts/evidence_maintenance.py inspect --db $Database --providers mercor --phase source --details none
```

These production execution commands were **not** run for this milestone.
Plan creation grants no execution authority. Execution requires --yes and
separate --allow-provider-requests, --allow-derived-writes, --allow-preparation
or --allow-enrichment-model grants. A source grant includes the existing
pipeline's canonical rollup and deterministic enrichment effects; the plan lists
these separately from source writes.

Plans identify exact provider scope, IDs with gaps, verification freshness,
accepted/detail coverage, latest observations, materiality validity,
endpoint/method/query, limits, write scope, ownership prerequisites and blocks.
execution_budget_absent is separate from source_inputs_unavailable.
An Alignerr catalog teaser is never labelled full detail.

Plans expire after one hour. Execution checks physical database identity,
schema, selected provider configuration/evidence, installed code/recipes,
transport binding, owner/profile/preparation configuration when authorized,
and selected model repair configuration. It reconstructs executable operations
from current contracts: editing operations and recomputing the checksum cannot
silently widen scope. Changed inputs require a new inspected plan.

Use --phase source for observation, --phase derived after source work for
current derivations, or default all to see both. Model and owner preparation
in a plan containing source observation remains pending re-inspection, even
with a model grant. Missing owner/model authority never prevents otherwise
authorized source-only work.

## Source semantics and bounds

| Provider | Requests | Authority and detail |
| --- | --- | --- |
| Alignerr | GET official /api/jobs; existing pagination, page size 120, at most 100 pages / 20,000 records, 90-second timeout | Complete catalog observation owns lifecycle renewal. Optional official /jobs/{id} detail GET is content-only, 25-second timeout, 2 MB cap, no redirect/retry. Only validated records returned in this observation are eligible. |
| Mercor | One GET https://aws.api.mercor.com/work/listings-explore-page, 30-second timeout | Always partial. Returned records can be positively verified; omitted records cannot establish closure. No exact-posting renewal or detail capability is invented. |

Choose total --http-limit (0–1000) and --detail-limit (0–500), subject to the
existing hard ceilings and source allocations. --details needed conservatively
reuses valid detail; all explicitly rechecks supported returned details; none
disables detail requests. Missing detail allowance is reported as pending.
There are no hidden retries, redirects or asset requests.

Every source mutation uses run_crawl, existing capture/acceptance and lifecycle
logic, content-only detail recovery, canonical rollup and deterministic
enrichment. Failed, capped or partial observation never becomes complete.
Held/degraded material cannot overwrite stronger accepted detail.
Raw responses and acceptance decisions remain separately traceable.
Staleness does not establish closure; stored semantics do not renew availability.

Alignerr may renew verification while retaining full-detail acceptance,
annotations and owner interpretation bindings. An actual accepted-capture
replacement, including same-text Mercor replacement, invalidates prior binding
authority. No historical result inherits a new source reference.

## Database ownership and application lifecycle

Follow [local inventory operation](local_development_inventory.md) and
[supported WorkOS launcher](workos_authkit_staging.md). The database must have
applicable migrations already installed through existing migration tools.
The WorkOS runner checks the existing exact M008/M009 or M010/M011 attestors,
including full schema, lineage and reconciliation; no startup migration occurs.
This command performs no real migration. Owner preparation also requires the
existing initialized professional-background companion store used by consumers.

Stop only the known selected runtime through normal owner shutdown. The
database must be canonical, existing, single-linked, outside Git, with no SQLite
journal/WAL/SHM. Execution holds the existing offline lifetime ownership lease
from preflight through final receipts. Ownership conflicts block execution.
Never delete ownership locks, kill unknown processes or start a second writer.

After separately authorized maintenance, use the existing supported launcher:
`python -B scripts/workos_authkit_staging_app.py --config <ABSOLUTE_CONFIG_PATH>`.
No custom recovery launcher, cache clearing, forced Matches membership or
ranking override is required. This milestone did not start or access a real app.

To consume persisted owner interpretations after a restart, add this optional
field to that runner's existing private configuration, using the same companion,
model and basis as the authorized preparation:

```json
"professional_background_companion": {
  "path": "C:\\operator-data\\professional-background.sqlite3",
  "model": "<exact prepared model identifier>",
  "basis": "semantic_model_output"
}
```

The companion must already exist outside Git, be distinct from the product
database, and pass the existing store attestation. Follow the explicit setup
contract in [professional-background preparation](professional_background_preparation.md).
Application startup does not initialize storage or configure a preparation
client/budget; Matches and details only validate and consume existing results.
Omitting this configuration preserves the existing unconfigured behavior.
Use `offline_labelled_stub` only for labelled synthetic evidence. Invalid or
incompatible configured storage fails activation through sanitized errors.

## Explicit owner preparation

Omit --owner-session for provider-only operation. To inspect one authorized
owner's applicability, supply a private JSON selection:

```json
{
  "environment": "private_beta",
  "account_id": "<existing account ID>",
  "principal_id": "<existing owner principal>",
  "profile_id": "<confirmed profile ID>",
  "job_ids": [123],
  "companion": "C:\\operator-data\\professional-background.sqlite3",
  "session_token": "<current valid session>",
  "csrf_secret": "<matching CSRF secret>"
}
```

Normal session, CSRF, profile-read and ownership services validate this scope.
Jobs must belong to selected providers, at most eight pairs. The CLI never logs
in or manufactures authority. Credentials are omitted from plans and journals.
Scoped model payloads/responses can contain role/source information; keep the
journal private.

Read-only inspection can omit the preparation budget. For explicitly authorized
preparation, pass the same --owner-session, --preparation-budget and
--enable-preparation options to plan --phase derived and execute, with the
separate --allow-preparation --yes grant. The JSON budget uses existing fields:

```json
{"request_limit": 1, "token_limit": 50000, "usd_limit": "0",
 "input_usd_per_million": "0", "output_usd_per_million": "0"}
```

Zero-dollar/rate examples deliberately cannot authorize real requests. Future
paid work requires approved verified rates/ceiling and existing OpenAI
environment configuration. No pricing is inferred here. Existing minimization,
recipe, token/response limits, identity validation, generation CAS and durable
attempt consumption remain authoritative. Conservative not_established and
ambiguous results survive restart and remain reusable, not regeneration
triggers. Invalid/revoked/consumed attempts remain limitations; no reset or
historical rebinding is offered.

## Selective source materiality preparation/repair

Global materiality is separate from candidate interpretation. Inspection uses
existing enrichment/source-clause validation. Deterministic repair uses the
existing selected-enrichment API and re-inspects its outcome; unresolved
materiality is never falsely reported completed.

For one explicitly selected canonical, supply --enrichment-canonical-id <ID>,
--enrichment-budget <JSON_PATH>, and --enrichment-journal <STABLE_JOURNAL> on
a derived plan and its execution; grant --allow-enrichment-model --yes.
This wraps the existing single-canonical enrichment API, structured client,
prompt/schema, acceptance and run accounting. Scope: one canonical, one physical
request, 8,000 maximum output tokens, 262,144 response bytes, no retries.
The existing budget value object reserves the request. Current same-model
conservative annotations are reusable.

## Interruption and return

First authorized execution pins the journal beside the database in immutable
<database>.evidence-maintenance.json. Preserve it with the journal. Choosing a
different directory cannot bypass an interrupted materiality reservation.
Automatic pin reset or journal relocation is not supported.

Each plan has an exclusive directory containing its original plan, numbered
hash-chained events and separately hashed raw responses. Writes are exclusive
and fsynced. Capture precedes parsing/acceptance. Interrupted/oversized streams
retain a bounded prefix marked capture_complete=false, never complete evidence.
Reservations precede dispatch; uncertainty consumes the attempt.

report and recover are read-only aliases validating receipts and raw hashes:

```powershell
python -B scripts/evidence_maintenance.py recover --journal $Journal --plan-id $PlanId
```

States include completed, partially_completed, failed, blocked,
awaiting_authorized_repair, and interrupted (no terminal receipt).
Executing the same plan returns its existing receipt without requests.
For source interruption, inspect and create a fresh authorized plan with a new
creation time. Restart at the supported catalog entry; never stitch snapshots
to fabricate completeness. Original runs/responses remain unchanged.
Owner companion attempts and materiality reservations remain consumed on
interruption. Without usage metadata, cost is unknown and the full reservation
is retained; it is not a zero-cost success.

## Synthetic two-cycle demonstration

Use a **new disposable directory outside the checkout**. No real accounts,
providers, app, identity service or models are used.

```powershell
python -B scripts/evidence_maintenance.py demo --directory C:\temporary\maintenance-demo-01 --step run
```

Or operate every step in a separate process:

```powershell
$Demo = 'C:\temporary\maintenance-demo-02'
python -B scripts/evidence_maintenance.py demo --directory $Demo --step init
python -B scripts/evidence_maintenance.py demo --directory $Demo --step inspect
python -B scripts/evidence_maintenance.py demo --directory $Demo --step plan
Get-Content -LiteralPath "$Demo\plan-1.json"
python -B scripts/evidence_maintenance.py demo --directory $Demo --step execute
python -B scripts/evidence_maintenance.py demo --directory $Demo --step report
python -B scripts/evidence_maintenance.py demo --directory $Demo --step next
python -B scripts/evidence_maintenance.py demo --directory $Demo --step inspect
python -B scripts/evidence_maintenance.py demo --directory $Demo --step plan
Get-Content -LiteralPath "$Demo\plan-2.json"
python -B scripts/evidence_maintenance.py demo --directory $Demo --step execute
python -B scripts/evidence_maintenance.py demo --directory $Demo --step report
python -B scripts/evidence_maintenance.py demo --directory $Demo --step next
```

The maintained repository fixture calls actual planner/executor and production
consumers through intercepted provider and labelled offline model transports.
It installs migrations only in its new marked disposable database, using
recorded Alignerr HTML plus synthetic catalogs/Mercor bodies. Initialization
observes sources and prepares one scoped synthetic candidate. Normal handlers
exercise Save, reminder, applied and hidden actions. Another synthetic account
has a production-service-created entitlement/reserved import attempt.

Cycle 1 is seed +73 hours, crossing the 72-hour freshness boundary. Alignerr
completes, retains full detail and renews verification. Mercor remains partial,
positively verifies its returned stable posting, and replaces its accepted
binding; the owner interpretation is invalid until explicit offline repair.
An omitted Mercor posting remains stale.

Cycle 2 is seed +146 hours. Alignerr pagination is incomplete and cannot renew
verification. Mercor again positively verifies only its returned record.
Binding/repair and preservation checks repeat. Targeted tests additionally
cover failed pagination/observation, held/degraded material, caps, stale plans,
authorization, conservative reuse and interrupted re-entry.

Normal Matches and exact detail consumers evaluate current evidence in fresh
instances of the supported WorkOS runtime loaded through its strict launcher
configuration, with a disabled preparer and no model client. Assertions
require legitimate stale-to-trusted effects, removal of obsolete semantic
authority, restored offline authority and equality of all non-source tables:
profiles, preferences, entitlements/attempts, ownership, saved/applied/hidden
state and reminders. Hidden/applied behavior remains effective; detail uses
the normal exact route without forcing a Matches card.

These are **simulated cycles**, not days of observed operation, real provider
verification, semantic-quality evidence or sustained unattended operation.

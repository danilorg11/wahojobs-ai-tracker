# Accepted catalog release and Daily Inventory Operations V1

Implementation follow-up (2026-09-22): see
[the tested implementation and bounded activation procedure](daily_inventory_operations_v1.md).
The historical report below is preserved; the September 21 operational checkpoint
supersedes its earlier host-access and deployment observations.

Prepared 2026-09-21. This is an operational handoff, not scheduler activation.

## Release checkpoint and deployment blocker

Accepted correction commit: `4ee28ce1de994eaac2afcd46e8d89eb8642c06c9`, branch
`codex/candidate-ux-cleanup-v1`, based on `f8046724ce7c3b28f2ca70b3480815119d7c9222`.
The 23 committed files are the previously owner-accepted catalog corrections and
their focused regressions. No preview server/authentication helper, database,
synthetic inventory or rejected enrichment result is included.

178 affected tests passed locally; earlier independent source, structured-field,
Mercor and compensation reviews remain applicable. The only closure edit was
removing an extra trailing blank line reported by Git. No behavior changed.

Promotion did **not** run. The established server at `174.138.50.176:22` timed out
before authentication, including an independent six-second reachability check
outside the local network sandbox. `https://beta.wahojobs.com/login` returned
HTTP 200. HTTPS availability does not establish the running code revision.
The last verified deployment receipt, dated 2026-09-19T23:38:42Z, names
`ce9f318818843f0af591facc4b22af4cdb464243`; it is historical, not a fresh host check.
No private handoff was requested because SSH transport was unavailable.

Main remains `226183748acb251d21729ac596146cd7d56364d1`. There was no push,
hosted write, source collection, model request, scheduler activation or invitation.

## Exact data reconciliation boundary

Private artifacts live in the sibling directory
`candidate-ux-evidence/release-closure/`. The complete identity/hash/timestamp and
changed-field manifest is `accepted-evidence-change-manifest.json`; it compares
the frozen hosted inventory with the accepted `catalog-display-review-v2.sqlite`.
It is a reviewed local delta, not authority to overwrite newer hosted rows.

- 14 content observations, for 14 exact variants and 12 canonical opportunities:
  seven historical Alignerr responses from September 5 (jobs 4578, 4453, 3783,
  4577, 4576, 3357, 4580); six September 20 responses (24, 25, 49, 209, 346, 893);
  one Mercor observation for job 6012/canonical 879 at
  `2026-09-21T01:49:14.011208+00:00` supporting $50/hour USD.
- All 6,029 original captures remain byte-for-byte logically unchanged. The
  isolated appended IDs 6030–6043 are audit references, **not** IDs to force on host.
- Fourteen jobs/source-content/acceptance rows differ; job changes are content
  `updated_at`, plus the already reviewed location correction on job 4577.
- 41 Alignerr derivations differ: canonicals 4, 5, 6, 7, 8, 9, 12, 17, 18, 19,
  20, 22, 25, 28, 29, 30, 32, 33, 35, 37, 47, 48, 49, 50, 51, 59, 70, 138,
  206, 207, 209, 210, 212, 270, 281, 302, 326, 327, 328, 334, 434.
  Top-level attribute changes: work activities on 23; languages on 2; domain on
  1; minimum/maximum weekly hours on 1. The remaining differences concern exact
  variant facts, evidence bindings and unknown-field bookkeeping. These counts
  overlap and are not 41 newly populated opportunities.
- Companies, canonical identity, crawl runs, job events, manual overrides and
  original source availability clocks are unchanged. No experimental model
  result is in the accepted database. Account data is absent from this source-only
  snapshot and must instead be protected/checked against the live host baseline.

When access returns: freshly verify host/release/config-002/database target;
reconcile each URL/external identity/timestamp/hash against current source state;
skip existing identical captures, stop on an unreviewed conflict. Use
`reprocess_saved_detail` and the existing selective/history-preserving enrichment
path, constrained to the manifest's canonical IDs and field/evidence changes.
Do not replace full derived documents indiscriminately or mark old derivations as
a newly completed model run. Preserve live overrides and newer accepted facts.
Capture IDs/provenance references must bind to the actual host acceptance results.

Use the established cold backup of the quiescent storage cohort, verify its
manifest, test the immutable candidate release on Linux, apply only the reconciled
data operation under exclusive ownership, select the release and restart only
`wahojobs-beta.service`. Verify protected table fingerprints, sidecar drafts,
source clocks, identities, acceptance integrity and actual-time catalog queries.
Reuse `docs/candidate_ux_beta_promotion.md` and `docs/private_beta_data_operations.md`.
Rollback means prior compatible code with current data; do not restore an older
database over later owner activity. Never copy the preview database to the server.

## Freshness and current isolated review

At `2026-09-21T03:15:43Z`, `/jobs` returned HTTP 200 and 879 distinct opportunities;
Mercor + Brazil returned 1, Alignerr + Mathematics 7, Generalist + English 4.
This is an observation, not a guaranteed release count.

- Alignerr qualifying run finished `2026-09-18T13:04:03Z`; 72-hour boundary:
  **2026-09-21T13:04:03Z** (10:04:03 America/Sao_Paulo).
- Mercor qualifying individual observations: `2026-09-18T13:07:32Z`; boundary:
  **2026-09-21T13:07:32Z** (10:07:32 America/Sao_Paulo).

After those boundaries, unchanged evidence fails normal freshness admission:
491 Alignerr and then 388 Mercor canonical opportunities cease to be current.
Their saved-job history is retained. Content imports do not postpone expiry.
If promotion occurs after expiry, separately authorize discovery-only verification:
at most 100 Alignerr catalog requests (120/page, no redirects/retries/details), and
one Mercor listing request, no paid calls. With unchanged Alignerr total, 47 pages
would suffice; the cap is not a promise of completeness. Only successfully
qualified returned observations refresh authority. No such operation ran here.

## Is unattended daily collection active?

**Not verified because administrative SSH is unreachable.** Do not label it
absent or failing based on local documentation. The running web service and HTTP
200 are not scheduler evidence. The prior source-operation documents explicitly
left scheduling disabled; the two retained hosted crawl records are the approved
September 18 manual runs, not proof of automatic activity.

Intended execution host: `wahojobs-private-beta-rehearsal-20260917` (existing host).
Known selected database from the last verified receipt:
`/var/lib/wahojobs-beta/rehearsal-recovered-001/product.sqlite3`.
Current timer/cron/process identity, enabled source set/cadence, last automatic
attempt, next execution, enforced limits, locking, timeout/failure handling and
alert destination are all **host-unverified**. A fixed read-only audit is staged
as `release-closure/hosted-audit.py` but was not dispatched. Inspect relevant
system/user timers, cron and actual execution records before adding a scheduler.

## Existing source status

All 15 CORE adapters exist and ordinary local dispatch allows them. That is not
beta activation approval. Only Alignerr/Mercor are in the reviewed beta source
maintenance scope; `evidence_maintenance.PROVIDERS` also limits operations to them.
Meridial additionally has explicit approved terms/acceptance and production registry
enablement. Other legacy CORE adapters are not covered by that registry's approval
records. Current upstream behavior was not tested in this execution.

Hosted counts below are the retained September 19 snapshot, not a current host
inspection. All current hosted-presence claims remain unverified. For every zero
row the proven Browse absence is **no hosted job records/no qualifying hosted
observation**, not missing crawler code. Companies may already have seeded rows.

| Source | Last retained non-synthetic evidence (UTC) | Hosted jobs/canonicals | Smallest next action beyond beta scope approval |
|---|---|---:|---|
| Alignerr | Sep 18, complete successful catalog | 5,624 / 491 | Publish accepted corrections; qualifying catalog refresh before expiry |
| Appen | Sep 4, partial retained; last old success Jun 19 | 0 / 0 | Establish current completeness/record contract; bounded Lever verification |
| DataAnnotation | Jun 19 usable; Sep 4 failed `destination_or_method_outside_plan` | 0 / 0 | Review exact public destinations in transport contract; retain evergreen semantics |
| DataForce | Sep 4, successful, 40 returned | 0 / 0 | Approve bounded live-source run and canonical/eligibility checks |
| Handshake | Sep 4, partial retained; last old success Jun 19 | 0 / 0 | Establish observation/completeness contract; keep public-inventory semantics |
| Meridial | Sep 4, successful, 821 returned; registry controls July 13–15 | 0 / 0 | Add narrowly approved Greenhouse transport/maintenance scope, then one bounded run |
| Mercor | Sep 18, 388 individually accepted records in partial catalog | 388 / 388 | Publish shared individual-verification predicate; refresh returned records only |
| micro1 | Sep 5, successful, 312 returned; dated full detail also retained | 0 / 0 | Approve existing POST transport under beta maintenance; bounded verification |
| Mindrift | Sep 4, successful, 145 returned | 0 / 0 | Approve bounded Workable run, preserving 12-hour cooldown |
| OneForma | Sep 4, successful, 429 returned | 0 / 0 | Approve bounded run with canonical variant/locale checks |
| Outlier | Sep 4, partial retained; last old success Jun 19 | 0 / 0 | Establish current supported snapshot/record contract before availability claims |
| RWS | Sep 4, partial retained; last old success Jun 19 | 0 / 0 | Establish contract for filtered TrainAI Lever subset; no corporate expansion |
| Surge | Sep 4, partial retained; last old success Jun 19 | 0 / 0 | Establish worker-page contract; preserve mixed/report-separately classification |
| Turing | Sep 4, successful, 222 returned | 0 / 0 | Approve bounded run with canonical identity validation |
| Welocalize | Sep 4, successful, 440 returned | 0 / 0 | Approve bounded run preserving AI-work scope/canonicalization |

Retained local partial runs are useful source records, not fresh or complete
availability attestations. June success labels predate later contract safeguards.
No adapter was declared healthy merely because code/fixtures/registry entries exist.

## Smallest next implementation: Daily Inventory Operations V1

1. **Discovery/availability:** after the host scheduler audit, reuse its existing
   mechanism or install one systemd timer/oneshot pair on the beta host, only after
   explicit activation approval. Proposed daily schedule: **06:00 UTC / 03:00
   America/Sao_Paulo**, persistent missed-run handling, no parallel execution and
   no immediate retries. Per run/day: Alignerr **100 HTTP attempts**, Mercor **1**;
   **101 total**, zero details, zero model calls/$0 model spend. Redirects remain
   rejected. Alignerr timeout 90s/request, Mercor 30s; overall maintenance hard
   deadline 15 minutes plus at most 2 minutes for recovery/health checks. Budgets
   are ceilings, not permission to keep retrying failures.
2. Add an explicit due-observation plan option to the existing maintenance planner:
   it currently requests a catalog only when empty, stale, failed or needing
   details. A timer alone would therefore **not** guarantee daily verification.
   Preserve manual defaults; bind the daily option, source allowlist, real clock,
   database identity, revision and budgets in each freshly generated plan/journal.
   Reuse per-request durable reservations and existing acceptance/lifecycle rules.
3. Respect existing lifetime ownership: drain/stop only the beta app, make a verified
   coherent backup, take the exclusive offline lease, execute bounded maintenance,
   release the lease, restart and health-check in a guaranteed cleanup path.
   This smallest design entails a daily beta maintenance window, up to the stated
   limit; do not promise zero downtime or bypass the lock. Test interruption,
   shutdown, budget exhaustion and guaranteed service recovery before activation.
   Preserve user tables/sidecar drafts and record counts/fingerprints in receipts.
4. A complete validated Alignerr run can authorize source-specific absence handling.
   Partial/failed runs never prove closure. Mercor refreshes only individually
   qualified returned records; missing records retain their original clocks and
   can naturally expire. Save/history identity survives expiry/removal. Do not
   interpret a positive personalized match as source verification.
5. Record last attempt, qualifying source/per-record observation, next due time,
   completeness, request usage, schema drift, failures and age distributions. An
   hourly read-only freshness check should warn at 36 hours, escalate at 48 hours,
   and alert immediately on failed run/service recovery. Daily success leaves
   approximately 48 hours before the 72-hour boundary. Include unreturned Mercor
   records in expiry warnings even when its latest partial run is healthy.
   Activation requires an owner-selected alert transport/recipient, configured
   secret and successful delivery test. None is invented or configured here.
6. **Detail/structured work stays separate and off initially:** zero detail and
   model budget. Preserve retained bodies and override/conflict rules; use input
   and derivation identities to skip unchanged processing. Deterministic updates
   only for changed accepted inputs/variant membership; no bulk repair from merely
   changing recipe versions. Any model follow-up must first resolve the rejected
   experiment's output-contract/integration failure; no spend increase by default.
   Full-description coverage and exact-variant restriction coverage are separate
   measures: a representative description cannot certify sibling eligibility.
7. **Next existing-source batch: Meridial only**, after daily Alignerr/Mercor works.
   Rationale: explicit registry approvals, three recorded controls, later complete
   retained catalog and an AI department boundary; not maximum inventory volume.
   Inspect the existing Greenhouse jobs request with content/meta count and its
   required department-tree request. Proposed cap **2 HTTP requests**, existing 60s request timeout,
   5-minute overall batch, zero retries/redirects/detail/model calls. Add that exact
   pair of destinations to the bounded transport and source-operation allowlist without
   broadening general corporate sources. Each response may contain many records;
   validate the accepted AI subtree, identity and completeness before admission.
   Current upstream readiness remains unverified until this separately approved run.

Required implementation tests are narrowly tied to daily due-trigger behavior,
real-time expiry, request reservations/interruptions, exclusive ownership and app
recovery, partial no-closure, Mercor individual verification, unchanged-input skips,
variant identity, history preservation and actual alert delivery. Do not reopen
UI or paid enrichment milestones.

# Daily Inventory Operations V1 — all-source activation package

Prepared 2026-09-22 UTC. **Deployment, live delivery and recurring status are recorded
in the separately dated activation receipt; this document does not activate them.** This file
and the [consolidated manifest](../deploy/private-beta/daily-inventory-activation-manifest.json)
replace the earlier two-source operating proposal. All 15 existing core sources
are accounted for: ten are ready for controlled activation checks, five are
blocked by specific contract gaps. Overall daily coverage is not yet complete.

Continue `codex/daily-inventory-operations-v1` from the preserved checkpoint
`9bd47127228856095e4164beb8dc309e52d1ea75`. Its compensation correction and accepted
beta ancestry remain intact. The final release receipt supplies the exact new
commit, tree and archive SHA-256. Never deploy a moving branch tip or main.

The governing host checkpoint remains:
`C:/Users/danrg/.codex/visualizations/2026/09/19/01a0ba06-976c-77f3-b156-3646f54e9aab/candidate-ux-evidence/access-recovery-20260921/daily-inventory-operations-v1-checkpoint.md`.
Its sibling `release-completion.md/json`, `release-deploy.py`, `operation_body.py`,
`availability_worker.py`, `reconcile_pay_worker.py` and `hosted-read-011.json`
retain the accepted procedures and evidence. Use the established pinned private
administrative access; do not reconstruct credentials from account data.

Historical hosted state: release `4ee28ce1de994eaac2afcd46e8d89eb8642c06c9`,
862 current canonical opportunities / 5,995 variants, 902 stored opportunities /
6,035 records. These are previous observations, not counts to force at activation.
No accepted corrections or later documentation were discarded. No Meridial pilot
was run; Meridial's existing approved production contract is included in the plan.

## Coverage, request units and readiness

The owner-authorized September 4 endpoint plan and executed transaction ledger
cover all 15 public sources. Their exact paths and SHA-256 values are in the
manifest. The old Alignerr/Mercor pilot is not an exclusion rule. Meridial has
structured approved terms/readiness entries in the existing source registry.
The other legacy adapters have retained public retrieval scope; this package
does not fabricate a separate terms approval, waive an access restriction or
authorize authentication/bypass. A current challenge or contract change fails
closed and remains visible while independent ready sources continue.

An **HTTP request** is a dispatched attempt, including a 3xx response. Redirects
are rejected before a follow-up request, and there are no retries. A fetched
**page** is a successful response document: API page, HTML page or public probe;
it is not a posting count. Parsed upstream rows/posts, emitted exact variants
and distinct canonical opportunities have separate counters. Filtered corporate
or private rows are neither rejected malformed records nor eligible variants.
A complete check describes only the adapter's supported public surface.

**Alignerr:** the current contract is `jobs`, stable `total`, `limit` and `offset`,
with unique IDs and exact pagination checks. At 120 rows per page, 5,624 retained
rows need `ceil(5624/120) = 47` HTTP requests, yielding 491 canonical opportunities.
The 100-request cap is a safety ceiling. A smaller validated page size can require
more requests; the runner then uses the validated size. At full pages the cap
covers at most 12,000 returned rows, even though the adapter has a separate
20,000-record ceiling. A cap, changing total, duplicate, premature empty page or
interruption cannot be labelled complete or authorize absence closure.

**Mercor:** one GET returns the public `listings` array. The latest retained run
qualified 371 exact records; 40 absent records remained expired/unconfirmed, not
closed. Hundreds of returned rows do not prove provider-wide coverage. The locally
retained evidence does not establish a pagination/cursor contract. The runner
retains the full response and reports envelope keys/continuation signals for the
controlled validation. It does not invent a second endpoint or repeatedly fetch
the same response. If the captured envelope establishes more discovery steps,
review that narrow endpoint/body/cursor contract and revise the finite budget;
until then this source is explicitly `partial_individual`.

| Source | Initial plan | Retained HTTP / hard cap | Per-source deadline | Surface and admission requirement |
|---|---|---:|---:|---|
| Alignerr | Ready | 47 / 100 | 360s | Validated public API pagination |
| Appen | Ready | 1 / 1 | 60s | Complete validated Lever list; new strict required-field/duplicate checks |
| DataAnnotation | Blocked | 1 stopped / 11 | 360s, disabled | `/coding` redirected outside the approved paths. Review canonical route/fixed page set, validate evergreen page evidence and every redirect hop. |
| DataForce | Blocked | 3 / 20 | 360s, disabled | Any 200 without `views-row` currently looks like an empty terminal page. Require genuine inventory/empty-state structure; retain and test terminal HTML before admission. |
| Handshake | Blocked | 29 / 40 | 360s, disabled | Unbounded modules, unvalidated CMS destination, chunk-0-only discovery, no qualifying record contract. Constrain linked assets/chunks and retain raw identity/visibility evidence. |
| Meridial | Ready | 2 / 2 | 150s | Existing approved Greenhouse full jobs + AI department tree, exact attestations and count-drop guard |
| Mercor | Ready, limited surface | 1 / 1 | 60s | Individual public active records; never absent-record closure or provider-wide completion |
| micro1 | Ready | 4 / 50 | 240s | Native 50 pages × 100 rows / 5,000-record limit; exact totals and identities |
| Mindrift | Ready, cooldown | 17 / 70 | 360s | Two public probes + up to 68 token pages; stable total, public published rows, 0.2s spacing, zero retries; 12h since last successful crawl start |
| OneForma | Ready | 1 / 3 | 210s | Stable WordPress page count; 100 posts/page. Retained 29 post identities emitted 429 variants. Header evidence is now retained. |
| Outlier | Blocked | 1 / 1 | 60s, disabled | Existing failure/empty sample fallback and no qualifying public-record contract. Daily sample output is now rejected before tracking, but real envelope/visibility validation is still needed. |
| RWS | Ready | 1 / 1 | 60s | Strict public Lever list, existing TrainAI filter and category mapping |
| Surge | Blocked | 9 / 20 | 360s, disabled | Slug fallback can accept generic 200 pages. Require actual title, role and application evidence and a qualifying public/evergreen record contract. Retain raw index/page fixtures. |
| Turing | Ready | 1 / 3 | 210s | Exact public success/totalCount pagination; 500 rows/page, at most 1,500 under this policy |
| Welocalize | Ready | 1 / 1 | 90s | Complete Lever list, existing AI Services department filter: retained 562 upstream rows, 440 eligible variants, 122 filtered |

All five blocked entries require their stated narrow repair, fixture validation
and a bounded live validation before joining execution. The manifest includes
exact endpoint scopes, historical counters, timeout/cooldown, corrective action
and admission condition for each source. Disabled ceilings are review proposals,
not permission for the runner to dispatch those broken adapters. Their combined
92-request ceiling is excluded from daily totals. A hypothetical 324-request sum
across all entries is not an executable or approved all-ready plan.

The initial ten-source set expects about **76 requests** at retained surface sizes,
with **232 requests maximum**. Source deadlines total 1,800 seconds; 240 seconds
cover separately bounded publication, backup and final integrity: **2,040 seconds
(34 minutes) execution plus at most 120 seconds recovery = 36 minutes**. The request
cap and time cap are independent: a slow source can time out before spending its
HTTP allowance. Three-page OneForma/Turing and 70-request Mindrift limits are
explicit operating ceilings, not native completeness guarantees. Mindrift's cap
also accommodates the retained older 647-row inventory at approximately ten rows
per token page. Caps never change completeness rules to fit expected counts.

Public network collection runs while beta remains online. Only current-storage
backup, publication and final integrity require the stopped writer. This interval
has a separate **240-second hard bound**, followed by at most 120 seconds of
recovery; it is never the full 34-minute collection interval. Publication workers
share the remaining publication interval by the larger of observed and stored
record counts, with up to five seconds reserved per remaining sibling and 30 seconds
for final checks. Unused shares remain available; the four-minute cap cannot grow.
The bound is a safety ceiling, not an expected outage duration.
Representative native publication timing must pass before activation; actual stop
to ready time is then measured separately from total runtime. The old 2m14s release
outage and historical network timings are not ordinary-run measurements. Optional
report reconstruction, health checks and email run after normal service resumes.
Damaged infrastructure can still require manual recovery beyond the automated cap.
The September 22 native benchmark used a private copy of all current stored data
and journals, 47 retained Alignerr responses / 5,624 records and one retained Mercor
response / 371 records. The other eight sources used small labelled contract
fixtures. Backup plus publication and integrity took 103.651 seconds (12.988s
backup, 76.958s Alignerr, 9.172s Mercor); actual beta downtime was zero. This supports
an initial estimate around two minutes plus real source growth and restart time,
not a guarantee for the other sources' full inventories. Their first live results
and actual stop-to-ready duration remain commissioning measurements.

## Proven compensation failure and correction

The reduced public fixture `tests/fixtures/mercor_supplemental_retention.json`
contains the retained pre-refresh and refresh capture fields. Replaying them under
the original Mercor promotion policy reproduces the failure: the summary has the
same body and semantic job fields, but omits `pay` and `wahojobs_source_detail_v1`.
The old policy accepted the summary as the whole current content projection.
Historical capture 6043 survived, but derived fields and selected-variant display
could no longer find its compensation. Refresh capture 12038 was valid availability
evidence; it was not a retraction of pay. Manual reacceptance 12039 repaired only
that instance on the previous release.

The correction runs inside the normal source reconciliation contract, without
job-ID exceptions or a post-refresh repair script:

* `mercor_record_promotion_v2` holds a compatible summary omission against the
  accepted detailed content, while independently retaining the raw observation
  for existing exact-record availability qualification.
* When current summary conditions change, a validated content-only
  `mercor_supplemental_composition_v1` capture combines those current conditions
  with the compatible, dated supplemental pay pair. It references the immutable
  raw catalog capture and original accepted detail capture/hash/time. Replay
  checks both references and their immediate accepted predecessor; an old
  composition cannot resurrect superseded compensation.
* Summary `payRate: null`/empty means undisclosed in this source contract. A
  nonempty changed rate, changed unit, supported new pay text or explicit
  withdrawal/unpaid correction supersedes incompatible supplemental evidence.
  Other explicit field changes are preserved as changes. A later validated detail
  can replace older pay with its own amount/range, unit, supported currency and
  capture date; older/conflicting detail remains held. Bare `$` does not infer USD.
* Historical promotion versions still replay under their original rules. New
  policies do not rewrite original captures or their timestamps. The approved
  $50/hour USD evidence remains dated `2026-09-21T01:49:14.011208+00:00` when a
  daily availability observation is newer. Supplemental content never qualifies
  availability by itself.

Canonical/exact-variant identities and manual override precedence remain intact.
Current workload/body text reaches shared Matches evidence instead of old detail
display text; geography uses the latest qualifying raw observation, including
explicitly cleared fields. Failed/unqualified observations cannot clear that
geography. Browse, detail and Matches keep the shared exact-record verification
and pay contracts. This does not broaden detail collection or enrichment.

## Implemented operation and publication

The existing systemd units, `scripts/daily_inventory.py`, maintenance plans,
append-only evidence journal, backup/recovery and SQLite ownership contracts are
reused. The disabled example policy pins all 15 entries and the exact beta paths.
Owner configuration may reduce budgets or disable a ready source; it cannot widen
an endpoint, exceed compiled ceilings or enable a blocked contract. Changes to
readiness need code review and validation. Existing registry dispatch gates remain.

At 06:00 UTC the root supervisor verifies the actual host, immutable release,
`current` symlink, beta process/command, LoadCredential and effective config-002.
It takes the common maintenance gate and durably reserves the UTC slot. A read-only
coverage plan records due, cooldown, disabled and blocked sources. Separate bounded
beta-user collectors run while the normal beta service retains database ownership.
They write strict adapter-result envelopes and raw responses into the existing
pinned maintenance journal. They do not write product records, renew availability
or qualify publication. The existing backup procedure includes this raw evidence.

After online collection, only completed bound observations are offered for
publication. If none is admissible, beta is never stopped. Otherwise the parent
records the maintenance marker immediately before stopping beta, takes a verified
cold backup of current storage and protected domains, and publishes through the
existing pipeline under the offline lifetime lease. This preserves user activity
accepted during collection. It never swaps in an old database copy. Publication
has a zero-HTTP budget and cannot invoke an employer adapter. Original collection
and detailed-capture dates survive the delay; a publication timestamp is not a new
observation. Source/run/release/contract bindings, a verified hash chain and an
exclusive publication claim prevent cross-run reuse and replay.

Parent PID, phase, one-use claim and deadline bind every worker dispatch. A timed-
out source is reaped before its sibling starts. SIGTERM cancels further dispatch.
The supervisor restores normal beta in finally; ExecStopPost and boot recovery
handle stopped-service interruption under the remaining recovery allowance.
Online-only interruption is finalized as consumed without stopping or restarting
beta. Pending maintenance restoration always precedes optional accounting. Journals
with proven publication results can reconstruct qualification after a reporting
failure; a collected-only journal is explicitly unpublished. The common gate also
excludes manual maintenance and backup throughout both phases. The beta's lifetime
lease remains held during collection and is required exclusively for publication.
No preview storage or synthetic authentication can pass native configuration checks.

All ready adapters now share audited endpoint/method/query/body validation,
request reservation before network dispatch, no-redirect transport, remaining-time
clipping and raw response retention before parsing. Only public completeness
headers are retained, never Set-Cookie. This applies to ordinary bounded maintenance
plans as well as daily plans; standalone legacy scripts are not the scheduled
entry point. The runner never calls the unrestricted 15-adapter workflow.

Daily due is independent of the existing 72-hour eligibility clock. Mindrift alone
also checks its documented 12-hour cooldown from the latest non-synthetic successful
crawl start. A cooldown skip is reported, not a qualifying check; no same-slot retry
occurs when the cooldown elapses. Real runtime clocks and expiry semantics remain.
Persistent timers can catch only the latest missed slot within one hour. Duplicate,
failed and interrupted slots remain consumed; no burst of backfill requests.
On boot, recovery orders before the inventory service without ordering cycles
against its timer or beta. A beta-not-ready preflight fails closed; no automatic
retry or employer requests follow. Do not delete slot directories or claims.

Successful new observations use the normal crawl/tracking/source acceptance,
canonical rollup and deterministic publication pipeline, with paid enrichment off.
The integration tests verify new source opportunities reach shared detail/Matches
reads. Complete-source absence rules remain provider-specific; failures, partial
results and Mercor absence cannot infer closure. Exact IDs, canonical relations,
manual overrides, user saved jobs and application history remain intact. Public
inventory/evergreen classifications are preserved; a public application is not
relabelled a guaranteed active project. Surge's inherent allowlisted opportunity
page requests are specified in its blocked manifest, not confused with optional
full-description enrichment. Daily detail enrichment remains zero.

Existing Alignerr/micro1 accepted-detail precedence keeps dated supplementary
content when a catalog arrives. Daily reports now expose held catalog content and
reason counts separately from renewed availability; a changed held summary can be
reviewed through the existing maintenance evidence report. It does not silently
refresh the detail capture date or initiate a detail request. The retained Mercor
fix below continues to preserve compatible pay while applying supported current
conditions and superseding contradicted compensation.

## Reporting and alert setup

Private receipts remain under `/var/lib/wahojobs-beta/daily-inventory-v1`, with
per-source plans, summaries, failures, coverage plan, verified backup and final
worker proof. Journald and the existing CLI `report` / maintenance `report` remain
the operational interface. No dashboard or candidate-card warnings were added.

Every source is accounted for. Reports distinguish complete, partial-individual,
partial/failed, interrupted, blocked, disabled, cooldown and not-started outcomes.
They include trigger/run/plan identity, times, HTTP attempts, received/fetched
response pages, upstream record units, normalized variants, canonical opportunity
counts, new/changed/reconfirmed/closed records, held content, missing/uncertain/stale
cohorts, qualifying verification dates, expiry, next scheduled execution and actual
maintenance duration. Unknown counts stay null. A zero exit code does not qualify
an observation. A successful ready-set run with blocked sources is labelled
`complete_with_coverage_gaps`, never overall complete coverage.

Hourly stored-state health runs at **:40 UTC**, following the maximum 37-minute run
grace, so an absent 06:00 execution is detected at 06:40. It contacts no employers.
It detects missed/failed/partial runs, all-source coverage gaps, count drops below
half the retained baseline (baseline at least ten records), and each active cohort
reaching 36 hours / escalating at 48 hours. Renewal of one provider cohort cannot
hide another approaching expiry. Failed attempts preserve old cohort dates and
count baselines. The normal Mercor partial-surface limitation remains informational.
Stable issue keys deduplicate unchanged hourly problems and record recovery.

**Approved recipient: danilo@wahojobs.com.** No suitable operational email sender
is implemented in the existing code; authentication-code delivery is not reused.
The smallest configured boundary is an absolute reviewed executable receiving one
version-2 JSON batch on stdin per health invocation:

```json
{"version":2,"recipient":"danilo@wahojobs.com","application":"wahojobs-beta","events":[{"id":"durable-event-id","kind":"opened","key":"source:issue"}]}
```

The command runs as the beta user, has 15 seconds for the batch, and must deduplicate
each event ID. Events are durably marked attempted before dispatch. Failed or
uncertain delivery is not retried automatically. Adapter acceptance is not owner
receipt. Isolated tests use a test transport only.

The reviewed Resend HTTPS adapter is documented in [operational_email_v1.md](operational_email_v1.md).
Its fixed sender is `Wahojobs Operations <alerts@ops.wahojobs.com>` and recipient is
`danilo@wahojobs.com`. It uses a sending-only key restricted to the verified domain,
provided through systemd LoadCredential. Linux root-owned 0440 credentials
are accepted only with the exact read-only ACL for the service UID; group/world-readable or
writable credentials are rejected. No SMTP, auth-code transport, redirects, retries,
paid overages or provider SDK is introduced. The domain/DNS authorization and actual
receipt belong in the activation evidence. Disabled examples stay disabled.

## Validation and independent review

The affected suite ran **352 tests: 349 passed, three Windows platform skips,
zero failures/errors**, in 116.848 seconds. The final release receipt records the
exact command and subsequent focused checks, including cooldown enforcement and
manifest consistency, plus independent review results. Tests use isolated storage, retained
real Mercor pay captures and recorded Alignerr content, existing provider fixtures,
explicit synthetic transport envelopes and injected clocks only. September 4 URL,
count and timing receipts have null raw-body hashes: they are not falsely presented
as retained HTTP bodies. Handshake/Surge HTML and DataForce terminal fixtures are
still missing, which is part of their explicit admission work.

Tests cover all ten ready transports and new-record publication, all 15 policy
entries, source-specific scope/budgets, daily due within freshness, Mindrift cooldown,
request/deadline/redirect limits, partial-result safety, ordinary maintenance budget
isolation, source timeout/failure isolation, SIGTERM cancellation, phase replay,
original Mercor compensation through bounded maintenance, changed/held detailed
content, preserved user domains, closure/expiry, alert batching/deduplication,
missed schedules and restart calculations. Earlier unchanged UI/release evidence
is reused. Independent reviews identified and verified fixes for ordinary
maintenance budget bypass, cancellation dispatch, private headers and retention
of completeness headers. No unresolved reviewed implementation blocker remains.

The initial Windows evidence did not establish native systemd behavior. During
controlled commissioning, 140 isolated Linux regressions passed on systemd 255;
transient timer metadata, manager timeout, descendant reaping and ExecStopPost lock
recovery were verified with normal beta uninterrupted. Native LoadCredential uses
a service-UID POSIX ACL, now checked by the adapter. The online-collection split
adds original-date/provenance, concurrent user activity, retained backup custody,
per-source publication isolation and interrupted-reporting tests. The separately
dated activation receipt records final native timing and real email receipt.
Unit tests and isolated/manual runs do not activate recurrence.

## One controlled validation and activation procedure

Proposed activation window: **2026-09-23 05:30 UTC**, first timer execution
**2026-09-23 06:00 UTC**, then daily 06:00 UTC (03:00 America/Sao_Paulo).
If approval/preflight is later, select the first future 06:00 after completing the
checks, update `first_run_at`, and verify that next timer. Do not backdate or fill
missed days. The owner reviews this one package and its manifest, including the
initial ten-source 232-request / 34-minute execution / two-minute recovery policy.
Ordinary daily runs inside the subsequently activated policy need no new
conversational approval. Blocked admission, new scopes/budgets/access requirements,
paid work, new providers and delivery changes are explicit exceptions.

1. Through the established pinned administrative procedure, reverify actual host
   `wahojobs-private-beta-rehearsal-20260917`, HEAD/release/current link, normal
   beta process, config-002 and all later legitimate timers/cron/configuration
   changes. Stop on conflicts. Stage the exact archived commit under
   `/opt/wahojobs-beta/releases/IMPLEMENTATION_COMMIT`; verify archive SHA-256 and
   reuse the existing Python 3.12 dependencies. Never copy a fixture database into
   authority or replay accepted corrections.
2. Before authoritative downtime, run the affected tests in isolated Linux storage.
   Use `systemd-analyze verify` on all five units and `systemd-analyze calendar` on
   daily 06:00 UTC / hourly :40 UTC. Exercise source timeout, SIGTERM/SIGKILL,
   process-group reaping, stop/restore, ExecStopPost and restart with disposable
   service/storage fixtures under the real systemd version. Verify lock/ownership
   exclusion and beta/startup ordering. No employer calls are needed for this step.
3. Pin authority to database
   `/var/lib/wahojobs-beta/rehearsal-recovered-001/product.sqlite3`, its existing
   `.correction-drafts.sqlite3` sidecar and journal in that same directory. Use
   root config `/etc/wahojobs-beta/config-002/runtime.json`, matching
   `/run/wahojobs-beta/runtime.json`, namespace `private_beta`, origin
   `https://beta.wahojobs.com`, no preparation companion. Prepare state, `runs` and
   `backups` directories owned by beta with mode 0700; prepare adjacent
   `product.sqlite3.wahojobs-maintenance.lock` as beta, 0600 only if absent. Never
   replace an existing lock inode. Install root:beta 0640 policy at
   `/etc/wahojobs-inventory-v1.json`, exact commit and future first-run date, disabled.
   Verify source company rows/configuration and the existing journal binding;
   missing source configuration is a coverage blocker, not a synthetic seed.
4. Install units disabled, daemon-reload, and deploy using the retained beta stopped-
   writer cold-backup/recovery procedure. Take the common gate, stop only
   `wahojobs-beta.service`, confirm exclusive ownership, use existing
   `scripts/beta_recovery.py backup` / `verify` for the storage cohort, record
   protected hashes, atomically select the immutable release, then start beta and
   run `scripts/private_beta_health.py --config /run/wahojobs-beta/runtime.json`
   as beta. Compare protected/inventory/schema/acceptance fingerprints and ordinary
   Browse/detail/Matches reads. No collection or correction replay during deployment.
   Measure deployment downtime separately. Preserve backup custody; no automatic
   snapshot deletion is introduced.
5. Install the approved email adapter/sender configuration outside Git. Confirm its
   beta-user permissions and required credential mechanism without exposing secrets.
   Set recipient as above, approved command and approval bit, then `enabled=true`
   with timers still disabled. Run stored-state health once through the health
   service; the initial issues form one delivery batch. Verify actual receipt at
   danilo@wahojobs.com and repeat stored-state health to verify deduplication.
   Ambiguous acceptance is not a reason to resend. If delivery or native checks
   fail, leave timers disabled and resolve them without employer requests.
6. Enable boot recovery and both timers only after approval and the preceding checks:

   ```sh
   systemctl enable wahojobs-inventory-recovery.service
   systemctl enable --now wahojobs-inventory.timer wahojobs-inventory-health.timer
   systemctl list-timers --all wahojobs-inventory.timer wahojobs-inventory-health.timer
   systemctl show wahojobs-inventory.timer -p ActiveState -p LastTriggerUSec -p NextElapseUSecRealtime
   ```

   Verify the actual next daily trigger is the approved first 06:00 UTC, health is
   hourly :40, and persistent pre-first-run triggers make no employer requests.
   Do not invoke workers directly or run all 15 adapters against hosted storage.
7. Observe that first timer-initiated run as the single bounded live validation of
   the initial ready set. Capture systemd invocation/result, timer LastTrigger and
   next trigger, coverage plan, source journal IDs, HTTP/page/record/canonical
   counters, completeness and cohort evidence. Inspect Mercor's retained envelope
   for unsupported continuation; verify no provider-wide claim. Confirm original
   pay dates/current supported conditions, user tracking and protected domains,
   normal beta readiness and measured backup/source/restart/unavailable intervals.
   Check hourly alert/recovery delivery. A failing source remains incomplete or
   blocked; keep independent healthy sources scheduled within policy. Disable the
   affected entry if access/contract needs repair, recalculate the aggregate policy
   deadline, and document its admission work. Do not weaken safeguards to match
   historical counts. Only real scheduler execution/next-trigger/delivery/data
   evidence supports declaring the ready subset active; five blocked sources mean
   the overall coverage milestone remains incomplete.

## Disable and recovery

```sh
systemctl disable --now wahojobs-inventory.timer wahojobs-inventory-health.timer
```

Set policy `enabled=false`. If running, `systemctl stop wahojobs-inventory.service`
interrupts collection and invokes bounded normal-beta recovery. Keep boot recovery
available until normal readiness is confirmed; disable it only when retiring the
operation. Inspect journald, run/source receipts and existing maintenance reports.
Never delete consumed slots, phase claims, journals or persistent lock files.

If recovery fails, use the established operator procedure to confirm no worker or
lifetime owner remains, start the same selected beta service and verify readiness
and current protected data. Do not start a preview app over authoritative storage.
There is no automatic retry; the next approved daily slot is the next collection.

After v2 capture policies exist, blindly rolling back to `4ee28ce1...` cannot read
those policies. Disable recurrence and keep a compatible release (this extension
or proven compatible `9bd4712...`) while fixing forward. New filtered-count reporting
adds no database migration. Never restore an old database over newer user history.
Disaster recovery remains the existing verified snapshot-to-new-directory procedure
with journal/pin and consumed-operation reconciliation, not live-file replacement.

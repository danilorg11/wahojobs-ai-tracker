# Daily inventory reliability repair — September 27–28, 2026

## Release and incident status

**Production is running d6e9c393dc23a3f3c299b5eee9adac76277ae204**, installed and
ready at 02:48:03 UTC on September 28. Its immutable integrated Linux suite
completed **436 tests in 175.472 seconds: 428 passed and eight environment or
optional-fixture tests were skipped**. The release includes the validated
publication/backup corrections, truthful cooldown reporting, Mercor negative
page v2, and DataForce contributor-family v3. The installed DataForce allowance
is 70 HTTP requests / 180 seconds, making the total configured HTTP allowance
583. The 2,580-second execution, 240-second publication, 60-second cold-backup,
and 120-second recovery limits are unchanged.

The final fresh two-source repair
20260928T024929Z-repair-f26067388ad28225 finished at 02:54:28 UTC with
**2/2 qualifying source publications, zero source failures, protected user
domains unchanged and normal application readiness restored**. Its outcome is
complete_with_coverage_gaps: DataForce published 24 new variants representing
16 new canonical opportunities; Mercor reconfirmed 373 records and confirmed
40 exact closures, leaving zero pending, missing or stale Mercor records.
DataForce retains one explicitly contradictory onsite Viola identity as pending.
The temporary native override was removed and the original automatic service,
recovery command and 06:00 UTC timer were verified at 02:55:11 UTC. Temporary
administrative access was removed around 03:00 UTC, preserving all six original
firewall rules; the memory-only operator session closed cleanly at 03:00:54 UTC.

The final read-only health observation at 02:56:05 UTC confirms the application
ready, collection enabled, 13 currently qualifying source states plus Mindrift's
proven recent verification, and **zero expired posting records**. Across the
three completed native repairs, verified catalog impact totals 48 new canonical
opportunities, 107 new variants and 173 confirmed closures. These are cumulative
repair results, not a claim that all were added during the final two-source run.

The preceding 5403540 full-source rehearsal qualified 14/14 enabled sources.
Its fresh native production repair
20260928T020550Z-repair-1d8cdb6f2ddfcf01 completed at 02:14:08 UTC: **all 13
attempted sources produced qualifying publications, with zero source failures**.
Mindrift was deferred by its unchanged 12-hour minimum interval after its real
successful c521565 publication earlier that day. That is 13 new source checks
plus one retained recent verification, not 14 new checks in this repair.

The original repair outcome remains **partial_or_failed**. Its catalog changes
were 18 new opportunities / variants and 44 confirmed closures. DataForce still
had 25 pending IDs; Mercor had 40 pending IDs, including 27 expired records.
micro1 remained disabled. Follow-up source-contract corrections are now deployed
in d6e9c39 and the subsequent fresh two-source repair has completed. These
older counts and outcomes remain historical; no retained capture was replayed
to create freshness.

Administrative access was authorized and established. The original shared
failure has been diagnosed on the beta host; access is no longer an outstanding
incident blocker. Subsequent deployments preserved the active private database,
protected user domains, retained source evidence, and existing daily timers.

The earlier production catalog repair
20260928T004257Z-repair-60d731c46f83928e ran on c521565 and produced qualifying
publication receipts for **8 of 14 enabled sources**. Appen, DataForce, Mindrift,
OneForma, Outlier, RWS, Turing, and Welocalize published. Alignerr,
DataAnnotation, Handshake, Mercor, Meridial, and Surge exceeded their publication
worker deadlines. Normal service resumed; the repair is correctly recorded as
partial_or_failed. micro1 remains disabled following a fresh host HTTP 403.

## Confirmed failure causes

### Shared backup blocked publication

The September 27 scheduled run reached a shared backup deadline before source
publication. The retained failure stack identifies SQLite snapshot verification
(verify_snapshot._check_sqlite, copying into its in-memory validation database)
after the journal and database copy had consumed most of the 60-second phase.
The worker reported worker_execution_deadline_expired after approximately
57.486 seconds of worker execution. This was a common pre-publication failure,
not evidence that every company collector failed.

The initial host inspection found 2,451 retained journal files totaling
389,313,795 bytes and a 240,136,192-byte product database. Independent verification
of the retained completed snapshot succeeded in 10.714 seconds; the evidence did
not indicate a corrupt snapshot. Copying and validating all growing history
inside one cold phase was the operational bottleneck.

Release 1cde7ff moved bounded journal preparation online and bound cold adoption
to the preparation receipt returned directly to the supervisor. Release
c521565 corrected worker dispatch to use the configured 2,580-second aggregate
budget instead of the older 2,040-second default. The earlier dispatch refusal
occurred before stopping the app or publishing catalog changes.

The c521565 deployment completed at 00:42:33 UTC on September 28. Its online
preparation took 51.241 seconds, cold backup 22.989 seconds, and maintenance
69.596 seconds. The product database hash, protected domains, source-state bytes,
existing unit files, and timers were unchanged by deployment. These deployment
timings are separate from the following repair's publication outcome.

### Publication allocations and repeated history replay

After the shared backup problem was removed, the production repair exposed a
second bottleneck: small per-source publication allocations and repeated full
semantic replay of historical captures. Deadline failures occurred in lifecycle
processing or receipt preparation, even though fresh collection had succeeded.

The installed release allocates publication time by completion order and remaining work
within the existing 240-second total publication allowance. It reuses the full
source fingerprint produced by the independently reconstructed plan, eliminating
one duplicate fingerprint pass without removing any rows or historical material.
The source/schema set and reconstructed operations must still match exactly.

Canonical fingerprint encoding now streams the same JSON bytes into SHA-256.
The SQL selection, complete material, key ordering, Unicode handling, separators,
and rejection of non-finite numbers are unchanged. A full-size local Alignerr
inspection retained the same digest while peak process memory fell from
817,368 KiB to 518,108 KiB; elapsed time changed from 5.828 to 6.093 seconds.
This is a local measurement, not a production timing guarantee.

Release 5403540 also introduces an explicitly marked staged baseline for
single-source daily publication. Initial planning and independent reconstruction
still hash all source rows and history, validate current accepted material, and
reconstruct the allowed operation. They defer historical acceptance replay until
the **mandatory complete inspection inside the publication transaction**.
That final inspection includes inactive and currently omitted jobs. Invalid or
incomplete evidence, a missing atomic callback, or a missing staged observation
cannot produce a qualified receipt. Failures roll back tentative catalog writes.
Ordinary nonstaged maintenance continues to perform the original full inspection.
No accepted clock is refreshed by replaying old evidence.

## Source corrections and retained limits

| Source | Implemented behavior and authority |
| --- | --- |
| Mindrift | Versioned evidence identifies each explicitly published, non-internal Workable record. When the count-drop guard applies, exact attested positives may publish while absence-based closures remain withheld. Missing records keep their previous clocks. The complete raw capture and partial publication outcome remain distinct; old rejected captures cannot gain authority through replay. |
| OneForma | Requires consistent total/page headers, exact page sizes, positive integer post IDs, and nonempty valid application-variant lists. Truncation or missing variant fields cannot authorize closures. |
| Mercor | Keeps the explorer's partial authority and bounded inspection of up to 100 known missing exact IDs. Installed negative-page v2 adds explicit disabled and archived-role notices, with exact identity and replacement-pool binding. Existing v1 positive observations can renew exact availability without replacing accepted description/pay content. Absence, privacy alone, denial, timeout, and unsupported pages cannot close records. Larger eligible cohorts rotate and remain explicitly pending. The installed host policy allows 201 requests / 240 seconds. |
| Outlier | Allows the board plus up to 50 index-linked details under the installed 51-request / 60-second policy. Known and supported records receive bounded priority/rotation. Over-cap, failed, ambiguous, mismatched, or unsupported records remain pending; one bad record cannot discard valid observations. The reviewed role families and expression-of-interest distinction remain enforced. |
| DataForce | Installed v3 individually admits evidenced paid remote Cadence, Ronia, Gardenia, Triton and TTS contributor families alongside existing Thyme contracts. Exact index/detail/application evidence, visible work terms and eligibility must agree. TTS casting preserves its unpaid sample and conditional compensation and is excluded from the live-job estimate. The host now allows 70 requests / 180 seconds; the unchanged bounded adapter supports at most 20 index and 50 detail pages. Unsupported, contradictory and unobserved records remain pending, with no absence-based closure authority. |
| Surge | Isolates individual detail failures and failure of the excluded fellowship page so independently attested workforce roles can still publish. Pending identities and transport telemetry remain visible. Fellowship remains excluded; incomplete detail coverage cannot become a complete inventory claim. The installed time allowance is 180 seconds. |
| DataAnnotation | Individually verifies the ten configured evergreen application routes. These are standing application opportunities, not a complete employer project inventory. The historical bilingual route is outside the configured ten-route scope. |
| Handshake | Individually qualifies public CMS records with resolved identity and facets. Missing or hidden CMS rows and unresolved facets retain uncertainty; they do not authorize employer closure. |
| Alignerr | Uses current bounded catalog pagination; the diagnosed failure was publication time/history replay. Catalog reconfirmation does not falsely replace separately dated accepted full details. |
| Appen, Meridial, RWS, Turing, Welocalize | Preserve their existing complete-snapshot contracts for the configured public board or department/subset. A qualifying complete snapshot can establish additions and removals within that declared scope. Their current source coverage was not broadened to unrelated corporate or internal roles. |
| micro1 | Remains disabled. The existing approved API works from the operator's local machine but a fresh request from the beta host returned HTTP 403. The retained official opportunities HTML is an empty client shell pointing to that same API, with no independently usable server-rendered job inventory. No alternate endpoint, proxy, challenge bypass, or re-enablement was introduced. |

The read-only Mercor host validation inspected 439 known records against 373
explorer listings and found 44 exact typed closures using 169 requests in about
84 seconds. Those observations establish collector behavior; only a later
successful production publication receipt establishes catalog changes.

DataAnnotation, DataForce, Handshake, Outlier, and Surge have **no negative-record
closure contract**. Records disappearing from their public surfaces retain their
old verification dates and become availability-unconfirmed. Successful current
records can still be published independently. Pending qualification may identify
unsupported scope or bounded rotation rather than a failed request. These limits,
together with micro1 host access, prevent a truthful guarantee that every job at
every company is verified and closed daily.

## Installed coverage corrections and fresh publication

Release d6e9c39 includes the following accepted changes. Their installation and
read-only retained-page validation do not themselves establish new production
observations or remove pending records. The later completed two-source repair
supplies the separate fresh collection and qualifying publication receipts
reported below.

* **Health/email classification, 569a8ff:** a zero-request cooldown skip can be
  explained as recently verified only when the original collection/publication
  journal, exact committed SQLite crawl row, latest inserted crawl, retained
  cohorts and original cooldown interval all agree. Missing, expired, changed or
  failed evidence remains actionable. The original run/source receipts and
  verification clocks are unchanged. Reporting separates current qualifying
  sources from recently verified deferrals, retains DataForce/Mercor gaps, and
  does not emit a fake recovery or first verification. The historical deferral
  remains explainable after its minimum interval ends; future collection still
  follows the unchanged timer and eligibility policy.
* **Mercor negative-page v2, df567bd:** adds the exact observed disabled-page span
  and archived-role notice formats. The latter must bind the original listing
  ID/title to the visible named closure notice and a distinct active, public,
  enabled evergreen pool with the matching visible replacement title. Supported
  retained match modes are domain and subdomain. Privacy, explorer absence,
  hidden or contradictory controls, or a pool alone cannot establish closure.
  This contract can close only the known exact original job; it cannot insert a
  replacement pool or renew the old job's last-seen clock. Existing v1 replay and
  positive availability contracts remain unchanged. The three inspected retained
  representative HTML pages qualify under v2 and remain indeterminate under v1;
  complete retained host validation also inspected all 40 pending pages: 38
  exact disabled notices and two archived-role notices qualified under v2, while
  all 40 remained indeterminate under v1. This used zero network requests and
  made no database/source-state changes. The later fresh production repair
  independently confirmed all 40 exact closures from new HTTP observations.
* **DataForce family v3:** installed with the reviewed 70-request host policy.
  Retained-fixture validation qualifies 32 supported records using 35 HTTP
  transactions simulated from retained responses: eight existing Thyme records
  and 24 new-family records (10 Cadence, eight Ronia, four Gardenia, one Triton
  and one TTS casting). Of these, 31 have live-posting classification and TTS is
  inventory-only; its unpaid sample terms remain visible. The contradictory
  onsite Viola card remains pending. Nine new offline regressions and two
  existing authority cases passed independently; the final immutable suite also
  passes. The later fresh repair observed all 32 supported records in 35 requests,
  published 24 new variants / 16 new canonical opportunities, reconfirmed eight
  variants and retained only the contradictory onsite Viola identity as pending.
  Full proof and scope are documented in
  [DataForce remote contributor families v3](dataforce_remote_families_v3.md).

Both new source contracts have now been published. Recovery and rollback must
retain a reader that supports **both** DataForce v3 and Mercor negative v2, as
well as packaged snapshot v2. Installed d6e9c39 is the minimum reviewed combined
reader for these writes. Release 5403540 was a rollback option only before the
new source writes; it is no longer a compatible code-only rollback target.
A successful backup does not make an older source-evidence reader compatible
with newly published records.

## Approved packaged backup implementation

The owner explicitly authorized implementation, testing, and installation after
validation of the bounded v2 proposal. Installed release d6e9c39 retains the
approved implementation introduced in 5403540. Its deployment created and
verified a v2 snapshot;
existing v1 snapshots remain available under the compatibility rules below.

V2 preserves every retained journal byte in bounded compressed segments. The
existing 10,000-physical-file and 8 MB outer-receipt limits are unchanged; archive
members, expanded bytes, paths, indexes, and lineage chunks have separate finite
bounds. No live history is deleted. V1 snapshots remain independently readable
and restorable. Full format, expansion, disk-preflight, restore, and downgrade
rules are documented in [Packaged journal recovery](packaged_recovery_v2.md).

Online preparation verifies complete logical history and chain provenance.
Cold adoption authenticates the returned receipt through the supervisor's
in-memory digest, rechecks current source identities/content and compressed
bytes, and snapshots the current database. Intervening user writes are preserved.
The optimized cold verification accepts only the immediate manifest returned by
successful creation; standalone verification and restore always perform full
logical validation. Backup remains capped at 60 seconds and publication at
240 seconds; configured collection and aggregate limits are not extended.

The full-size host rehearsal used an independently verified retained v1 snapshot
and inert private clones only. It did not retire or activate live storage.
On f0fb233, all 3,479 retained journal files / 450,879,856 bytes were preserved
in seven segments (54,566,960 compressed bytes), with ten physical snapshot files.
Source and restored database hashes matched, and retained input bytes were
unchanged.

| Host rehearsal stage | Elapsed time |
| --- | ---: |
| Online v2 preparation | 65.300 s |
| Cold creation plus trusted verification | 47.182 s — within the unchanged 60 s limit |
| Independent deep verification | 42.816 s |
| Full restore | 130.128 s |
| Inert-only relocation reconciliation | 98.524 s |

The measured cgroup used CPU quota 100% and memory limit 600 MiB; it recorded no
OOM event. Peak process RSS was 350,572 KiB. An earlier conservative rehearsal
at CPU 50% / 440 MiB exceeded the cold deadline (83.324 seconds); that failed
measurement is retained. The successful rehearsal does not claim unlimited
growth or waive disk and execution bounds. Older releases cannot restore v2;
retain the tested v2-aware release for recovery, and follow its lineage downgrade
restrictions after any actual v2 restoration.

## Validation already established

* Immutable d6e9c39 integrated Linux validation: **436 tests, 428 passed and
  eight environment/optional-fixture skips, in 175.472 seconds**.
* Read-only candidate proof on the actual beta host completed in 1.479 seconds,
  with zero HTTP requests, no database writes and unchanged source-state bytes.
  It verified all 40 retained Mercor pages under negative v2 while preserving
  v1's 40 indeterminate decisions. It also proved Mindrift's original committed
  verification at 00:44:49 UTC, valid deferral and unchanged false qualifying
  flag for the skipped repair. DataForce/Mercor pending conditions, all expired
  cohorts, micro1's disabled state and the original failed daily run stayed
  visible. This proof is not a fresh collection or publication.
* Immutable 5403540 integrated Linux validation: **411 tests, 403 passed and
  eight environment/optional-fixture skips, in 168.188 seconds**. Two native
  ownership checks had already passed on the beta host with f0fb233; the
  ownership implementation is unchanged in 5403540.
* The earlier inert publication rehearsal on f0fb233 qualified 12 of 14 sources.
  Alignerr and Mercor still exceeded receipt-preparation deadlines. This is the
  measured reason for the staged baseline correction in 5403540, not a claim
  that the earlier candidate was ready to install.
* The final host rehearsal on 5403540 qualified **14 of 14 expected sources**
  using retained observations in an inert copy, with zero provider requests.
  Protected domains and the retained source snapshot database were unchanged.
  Publisher processes took 175.736 seconds, including completed final accounting
  of 18.309 seconds. With the measured 47.182-second backup and 1.995-second stop
  deductions, total budget usage was 224.913 seconds, within the unchanged
  240-second allowance. This verifies the candidate's retained-data publication
  path; it does not publish or refresh production records.
* Nine staged-baseline regressions pass, including normal/light fingerprint and
  operation parity, legacy changed/reconfirmed-count parity, complete atomic
  inspection of inactive/omitted rows, tampering before reconstruction and inside
  the transaction, rollback, invalid/incomplete final evidence, and missing
  callback/observation refusal. All 30 existing maintenance tests also pass.
* Native staged integration verifies the persisted baseline marker, original
  observation clocks, protected user writes, and zero publication HTTP requests.
  Direct nonstaged execution still rejects corrupted history before collection.
* Health/cooldown reporting passed 80 focused reporting tests, including eight
  real-publication integration cases. The tests preserve raw receipts and clocks,
  reject later failed crawls even with older observation timestamps, distinguish
  current and prior verification, retain other pending sources, and avoid fake
  recovery/first-verification events. Independent review found no blockers.
* Mercor v2 and unchanged v1 passed 26 tests, including both exact negative
  shapes, identity/title/pool contradictions, hidden controls, sealed raw-journal
  tampering, contract downgrade refusal, rollback and original clocks. A wider
  76-test staged-publication/receipt/health regression also passed. An archived
  role closes only its known job; no replacement pool is inserted and unrelated
  jobs remain unchanged. Independent review found no blockers.
* V2 tests cover v1 compatibility, more than 10,000 logical files, full restore
  and repeated relocation, interrupted fencing/retry, bounded space/size failure,
  and resealed archive attacks including hidden DEFLATE suffixes, paths, links,
  duplicate keys/members, ZIP64, CRC, and expansion discrepancies.

## Production repair results and remaining verification

The preceding 5403540 deployment receipt confirms readiness at
2026-09-28T02:04:10.095425+00:00. Online preparation took 98.345 seconds, cold
creation/verification 21.174 seconds, and maintenance 75.536 seconds. The snapshot
uses private_beta_cold_snapshot_v2, with manifest SHA-256
5ba90c7c347bc55fde324ab542e002516c2ab7b5fc6f16e1608997b49198bcc1.
The active database was not replaced, and its byte SHA-256 remained
1611ea6c7080d4cd2a7daebbff10ec11d1f289164c9e3086848ace6160ca3e1d.
Protected domains, source-state bytes, existing unit files, timers, and configured
execution limits were unchanged. micro1 remains disabled. The readiness
inspection reported NRestarts=0 and ExecMainStatus=0 for the application unit.

The native 5403540 repair finished at 2026-09-28T02:14:08.922451+00:00.
Its cold backup took 23.828 seconds and total maintenance 220.436 seconds.
Protected user domains were unchanged, normal application service resumed, and
the temporary native service override was removed. The original timer remained
scheduled for 06:00 UTC on September 28. The 13 qualifying fresh publications,
Mindrift's legitimate deferral, remaining pending/expired counts, and immutable
partial_or_failed outcome are recorded in repair-5403540-summary.json.

The final d6e9c39 deployment completed at 2026-09-28T02:48:03.295121+00:00.
Online preparation took 96.207 seconds, cold creation/verification 23.546 seconds,
and maintenance 74.795 seconds. It created a v2 backup with manifest SHA-256
3d24ee7e3146a6e9aed7845b99604f3cd786def0e7810a37db79422bd12ebd59.
The database was not replaced and its SHA-256 remained
f6b2ad15b55210f98553f9dd96bb14cada46adb49bdec72f6a0e402a720717ed.
Protected user domains, source-state bytes, existing unit files and timers were
unchanged. Application readiness passed with NRestarts=0 and ExecMainStatus=0.
The explicit DataForce policy change is 70 HTTP requests / 180 seconds; global
HTTP allowance is 583, with all execution/recovery bounds unchanged.

Fresh repair 20260928T024929Z-repair-f26067388ad28225 finished at
2026-09-28T02:54:28.489876+00:00 with complete_with_coverage_gaps. Both attempted
sources qualified and the failure map was empty. Worker completion and protected
domain equality were verified; normal service resumed. Total runtime was
299.436 seconds, maintenance 115.737 seconds, and restore/readiness 62.026
seconds. Online preparation took 109.828 seconds; cold backup 27.882 seconds;
DataForce publication 7.560 seconds; Mercor publication 13.099 seconds; final
integrity/accounting 2.388 seconds. The 30-second finish reservation and all
existing hard bounds were preserved.

| Fresh final source result | DataForce | Mercor |
| --- | ---: | ---: |
| Newly observed exact records | 32 | 373 |
| New variants / new canonical opportunities | 24 / 16 | 0 / 0 |
| Reconfirmed variants | 8 | 373 |
| Confirmed exact closures | 0 | 40 |
| HTTP requests | 35 | 81 |
| Pending identities | 1: contradictory onsite Viola | 0 |
| Missing / stale records | 0 / 0 | 0 / 0 |
| Original verification clock | 2026-09-28 02:49:49 UTC | 2026-09-28 02:50:41 UTC |

At 02:55:11 UTC, native cleanup proved the temporary override absent, original
--trigger auto and recovery command restored, application active, inventory unit
successfully inactive, and the enabled timer waiting for 06:00 UTC. No original
failed/partial receipt was rewritten. The retained final proof SHA-256 is
8bc173548d63c2633a545cd1345a634e5d76ee03f4af03689fe8ac0c6ad3bc4f;
the native cleanup proof SHA-256 is
fd1e6a57964be1113214bd2242bf269b990509444b23eb24276fb7763c3ff72c.

The read-only final operating snapshot at 02:56:05.701311 UTC is coherent:
application available, collection enabled, next collection scheduled at 06:00 UTC,
13 qualifying source states plus one separately proven recent Mindrift deferral,
and expired_records=0. It retains exactly three operational conditions:

1. DataForce's one contradictory onsite Viola identity remains unqualified.
2. micro1 remains disabled after host HTTP 403.
3. The last scheduled September 27 run remains historically failed.

The unsent email preview therefore still has the headline "Daily check failed",
referring to that unchanged last scheduled cycle. Its body separately reports
the subsequent two-source repair and the remaining DataForce scope gap. No old
failed receipt is erased or relabeled as a successful daily run. No email was
sent, no health outbox was written, and source-state bytes were unchanged by this
verification. The snapshot SHA-256 is
6980e00782df4eaccfd86dc2d416498d2d2a1ecaa928f4f65618bd88bf10d3ffb.

Administrative cleanup was confirmed around 03:00 UTC. The temporary operator
SSH /32 firewall rule was removed, leaving the original administrative SSH rule,
HTTP 80, HTTPS 443 and three outbound rules unchanged: six rules in total.
The memory-only operator helper exited successfully at 03:00:54 UTC, its private
HTTP receiver stopped, in-memory secret state was cleared, and the temporary
cloud-console tab was closed. Final private_beta_ready passed; the host reported
17 GB free disk space and 67% used. No remote cleanup action remains outstanding.

The September 28 06:00 UTC natural run has not yet been observed; its future
success must not be inferred from these manual repairs. The remaining
unsupported DataForce identity and micro1 host denial are coverage/access
limits, not hidden successes.

| Required proof | Status at this revision |
| --- | --- |
| Integrated immutable 5403540 Linux suite | **PASSED:** 411 tests, 403 passes / eight environment or optional-fixture skips; retain the exact archive and test receipt |
| Inert 5403540 full-source publication rehearsal | **PASSED:** 14/14 qualifying sources, zero provider requests, protected domains/input snapshot unchanged, 224.913 s including measured backup/stop deductions |
| 5403540 production installation | **PASSED:** ready at 02:04:10 UTC; v2 cold backup 21.174 s; active database bytes, protected domains, source state, unit files, timers, and execution limits unchanged |
| Fresh production repair on 5403540 | **COMPLETED:** 13/13 attempted sources qualified, zero source failures; Mindrift deferred after earlier real verification. 18 new opportunities/variants, 44 closures; protected domains unchanged, readiness restored, native override removed. Retained outcome partial_or_failed; DataForce 25/Mercor 40 pending, Mercor 27 expired |
| Existing scheduled operation | **Timer preserved:** next execution 2026-09-28 06:00 UTC; next natural-cycle outcome remains **PENDING** |
| Integrated immutable d6e9c39 suite | **PASSED:** 436 tests, 428 passes / eight environment or optional-fixture skips, 175.472 s |
| d6e9c39 production installation and retained proof | **PASSED:** ready 02:48:03 UTC, cold backup 23.546 s, protected domains/database/source state preserved; actual host proof confirmed Mercor 40/40 retained v2 negatives and real Mindrift deferral without network or inventory writes |
| Fresh DataForce/Mercor repair on d6e9c39 | **COMPLETED:** 2/2 qualified, zero source failures; 24 new variants / 16 new opportunities and 40 exact closures. Mercor pending/missing/stale all zero; one contradictory DataForce identity remains pending. Protected domains unchanged; readiness restored |
| Final read-only operating/health snapshot | **VERIFIED 02:56:05 UTC:** application ready, collection enabled, coherent state, 13 qualifying plus one recent Mindrift verification, zero expired records. DataForce one pending, micro1 HTTP403 and historical September 27 failure remain; no email/outbox/source-state writes |
| Temporary native override | **REMOVED and verified at 02:55:11 UTC:** original automatic runner/recovery restored, timer enabled for 06:00 UTC, application active and inventory service successful |
| Temporary administrative SSH access | **REMOVED around 03:00 UTC:** original six firewall rules preserved; memory-only helper exited successfully at 03:00:54 UTC, receiver stopped, secret state cleared and temporary console tab closed |
| micro1 host access | **UNRESOLVED external HTTP 403**; preserve disabled state until authorized bounded validation succeeds |

Do not mark this incident resolved from local tests, a successful backup, an
inert publication, or a changed alert alone. The final production claim must
state supported-scope publication results and the remaining access/coverage
limits separately.

## Retained evidence references

The private September 27 task evidence directory contains the operational
receipts; no private database or credential material is embedded in this document.
Relevant filenames are:

* production-inspection-initial.json — original host failure and retained run evidence.
* production-deployment-c521565.json and repair-attempt-c521565-run.json: previous deployment and partial native repair before 5403540 installation.
* production-deployment-5403540.json — successful preceding installation, v2 backup, readiness, and unchanged storage/configuration proofs.
* production-deployment-d6e9c39.json and linux-validation-d6e9c39.txt — current installed release, reviewed source-budget change, deployment integrity and immutable 436-test suite.
* coverage-candidate-proof-d6e9c39.json — actual-host read-only Mercor40/40 retained negative proof and legitimate Mindrift deferral, with zero HTTP requests and unchanged inventory/source-state bytes.
* repair-coverage-operational-proof.json: final fresh d6e9c39 two-source collection/publication, original clocks, protected user domains, phase budgets and normal readiness.
* firewall-restored-dom.txt and firewall-restored.png: confirmed removal of the temporary operator SSH rule while preserving the original six firewall rules.
* final-operating-health-d6e9c39.json: final coherent readiness/collection state, zero expired records, three truthful residual conditions and unsent email preview; no outbox or source-state writes.
* native-repair-cleanup-d6e9c39.json: final temporary override removal, restored automatic runner/recovery command, successful unit and unchanged next 06:00 UTC timer.
* repair-5403540-summary.json: completed native 13-source fresh repair, original Mindrift deferral, catalog impact, protected domains, restored readiness and native override cleanup.
* mercor-v2-local-retained-validation.json: zero-request interpretation of three retained Mercor HTML examples under unchanged v1 and new negative v2; not a fresh production check.
* retained-mercor-40-diagnosis.json: pending Mercor identities and retained page-format diagnosis.
* host-v2-conservative-rehearsal.json and host-v2-f0fb233-rehearsal.json — failed conservative measurement and successful full restore/reconciliation proof.
* inert-publication-f0fb233-result.json — earlier candidate's 12/14 publication result.
* inert-publication-5403540-result.json and linux-validation-5403540.txt: preceding candidate's 14/14 inert publication and 411-test Linux validation.
* publication-fingerprint-memory-benchmark.json — byte-identical fingerprint memory measurement.
* micro1-host-validation.json and micro1-official-opportunities.html — host denial and retained official client shell.

Add the next natural-cycle receipt reference only after its contents have been
verified. Preserve the
original failed/partial run receipts and their original observation clocks.

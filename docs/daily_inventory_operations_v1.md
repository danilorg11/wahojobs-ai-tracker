# Daily Inventory Operations V1 — implementation and activation handoff

Prepared 2026-09-22 UTC. **Implemented and tested locally; not deployed or active.**
This is the single proposed activation procedure. Owner approval, an approved alert
recipient/adapter and the native checks below are still required. Routine runs
inside the approved policy need no new conversational approval after activation.

## Checkpoint and scope

Development branch: `codex/daily-inventory-operations-v1`, created directly from
`f8346ae02661c6f01e64433af182242e39438822` on `codex/candidate-ux-cleanup-v1`.
That preserves the accepted `4ee28ce1de994eaac2afcd46e8d89eb8642c06c9` implementation
and subsequent documentation. The final delivery receipt supplies the full new
commit and archive SHA-256; substitute that exact commit for `IMPLEMENTATION_COMMIT`
below. Never substitute main or a branch tip that has moved.

The governing retained operational checkpoint is:
`C:/Users/danrg/.codex/visualizations/2026/09/19/01a0ba06-976c-77f3-b156-3646f54e9aab/candidate-ux-evidence/access-recovery-20260921/daily-inventory-operations-v1-checkpoint.md`.
Its sibling `release-completion.md`, `release-completion.json`, `release-deploy.py`,
`operation_body.py`, `availability_worker.py`, `reconcile_pay_worker.py` and
`hosted-read-011.json` retain the deployment, protected-domain and source evidence.
Use the established private administrative handoff and pinned-host procedure;
do not print credentials or reconstruct private access from account data.

The last reported host state remains release `4ee28ce1de994eaac2afcd46e8d89eb8642c06c9`,
862 current opportunities / 5,995 current variants, 902 stored opportunities /
6,035 stored records. These are historical observations, not expected activation
counts. No new host inspection, employer request, hosted write, notification,
model call, provider activation, main/public-site change or Meridial pilot occurred
in this implementation task.

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

## Daily operation and native units

`scripts/daily_inventory.py` supplies `run`, `worker`, `recover`, `health` and
`report`. `wahojobs/daily_inventory.py` implements policy, stored receipts and
health evaluation. Unit files and a **disabled** example policy are under
`deploy/private-beta/`. Nothing runs on import or by installing source files.
The example deliberately has an invalid commit placeholder until pinned.

| Policy | Enforced behavior |
|---|---|
| Schedule | Daily at 06:00 UTC (03:00 America/Sao_Paulo) |
| Sources | Alignerr and Mercor only; never unrestricted adapter dispatch |
| HTTP budget | At most 100 Alignerr attempts and 1 Mercor attempt per consumed daily slot |
| Redirects | Rejected; the initial HTTP attempt counts, no unbudgeted follow-up |
| Details, retries, models | Zero detail requests, zero retries, zero model calls |
| Execution | 900 seconds, including stop, verified backup and collection; remaining time clips request timeouts |
| Recovery | Up to 120 additional seconds; existing deadline survives fallback recovery |
| Freshness | Existing 72-hour record semantics, using real runtime clocks |
| Health | Stored-state check hourly at :25 UTC; no employer or product database requests |

The native run requires root on `wahojobs-private-beta-rehearsal-20260917`, inside
`wahojobs-inventory.service`; it verifies the exact immutable release, selected
`current` link, normal beta process/command/working directory/service user,
LoadCredential selection and effective configuration. Required authority is:

* Database `/var/lib/wahojobs-beta/rehearsal-recovered-001/product.sqlite3` and its
  existing correction-draft sidecar.
* Pinned journal `/var/lib/wahojobs-beta/rehearsal-recovered-001/journal`.
* Root configuration `/etc/wahojobs-beta/config-002/runtime.json`, matching the
  effective `/run/wahojobs-beta/runtime.json`, namespace `private_beta`, origin
  `https://beta.wahojobs.com`, no preparation companion.

Recovery rechecks host, release, selected root configuration and service credential
pins even when the beta process is stopped. Preview and synthetic runtime
configuration cannot pass these guards.

The supervisor takes a persistent cross-process operation gate shared with manual
maintenance and backup, reserves the UTC slot durably, stops only
`wahojobs-beta.service`, and starts a parent-bound, single-dispatch beta-user worker.
The worker holds the existing offline database lifetime lease continuously across
verified cold backup and both source plans. Daily discovery is an explicit hashed
plan option independent of freshness; a still-fresh source is due each day.
Existing source rate constraints and contract guards remain in force.

Alignerr must prove pagination and count completeness inside its budget. A capped,
interrupted or partial observation cannot infer closures. Mercor remains a partial
catalog: only qualifying individually returned records renew; exact identity,
activity, execution, count integrity and non-synthetic evidence checks still
apply. An absent Mercor record keeps its old clock and can expire without being
marked closed. User tracking/history survives all catalog lifecycle outcomes.

The worker verifies product schema/integrity/foreign keys and hashes protected
non-inventory tables plus the complete correction-draft sidecar. These hashes
expose no user records in reports. Failure or timeout terminates the child process
group before normal beta restoration. Supervisor cleanup, native `ExecStopPost`
and the boot recovery unit cover interrupted operation. Receipt-write failure
cannot prevent the restoration attempt. Optional summary reconstruction and alert
delivery take place after normal-service restoration.

`Persistent=true` permits a native missed timer to fire after restart. The runner
accepts only the latest slot within one hour of 06:00, at most once, with the same
locks and budgets. Later triggers record `missed_window` without collection.
Interrupted/failed reservations remain consumed; no same-slot retry or burst of
older catch-up requests occurs. Manual operation must use the native service and
does not grant a second daily budget. On a much later reboot, recovery observes
an already restored normal beta service without reopening the old recovery window.
Do not delete reservation directories or persistent lock/journal files.

Boot recovery orders before the inventory **service**, not its timer, avoiding
the implicit timer/basic-target dependency cycle. On a clean boot, if normal beta
is still starting when the persistent trigger reaches preflight, that attempt
fails closed without employer requests. The health check reports the missed run;
there is no automatic retry before the next daily slot. Include this simultaneous
startup case in native activation testing.

Timer semantics are documented by [systemd](https://github.com/systemd/systemd/blob/main/man/systemd.timer.xml).
The units intentionally have no ordering dependency on the beta service: recovery
must be able to start that service while the inventory unit is stopping.
Trigger provenance uses invocation-bound `TRIGGER_UNIT`,
`TRIGGER_TIMER_REALTIME_USEC` and `INVOCATION_ID`, documented in
[systemd.exec](https://github.com/systemd/systemd/blob/v255/man/systemd.exec.xml).
A scheduled firing delayed by recovery remains `timer`; a late firing is
`timer_catch_up`. Unsupported/absent metadata is explicitly `unknown`, not proof
of a manual run. Activation must correlate the actual timer and invocation
records before declaring automatic operation active.

## Operational reports and alert boundary

Private operational state lives at `/var/lib/wahojobs-beta/daily-inventory-v1`:
`runs/<YYYYMMDDT060000Z>/run.json`, per-source plans/summaries, worker and backup
proofs; latest `<provider>-state.json`; and `health.json` with active issues and a
durable alert outbox. The existing maintenance journal remains authoritative for
transport reservations and observations. Native service output goes to journald.
Use the CLI `report` plus existing `evidence_maintenance.py report` for inspection.
No candidate-card warnings or new dashboard were added.

Source rows distinguish complete, qualifying partial-individual, partial/failed,
interrupted, not-started and unavailable-accounting outcomes. They report trigger,
run/plan IDs, start/end, HTTP attempts, exact posting-record counts for observed,
new, changed, reconfirmed and confirmed-closed records, missing/uncertain/stale
records, all active verification cohorts, last qualifying date, next expiry,
next scheduled execution and measured maintenance duration. Changed counts compare
accepted semantic hashes; identical sightings are reconfirmations. Unknown counts
after failure are null, not invented zeros. Interrupted accounting is recovered
from durable journal reservations without another request. Ordinary process exit
alone is never proof of source qualification.

Health detects a missing/unfinished expected run after 06:20 UTC, failed or partial
source observations, a record-count drop below half the retained baseline (baseline
at least 10 records), 36-hour warnings and 48-hour escalation. Age checks evaluate
every active-record cohort, including old absent Mercor records when newer records
were renewed. The normal Mercor partial-catalog limitation has informational
severity. Failed attempts keep the previous qualifying dates and aging cohorts.
Count-drop alerts remain open until recovery; entering a new day's grace interval
does not falsely recover an earlier failed/missed-run alert.

Stable issue keys deduplicate unchanged hourly problems. Opening, escalation and
recovery events have unique IDs. The smallest delivery boundary is a reviewed
absolute executable plus argument list receiving JSON on stdin:
`{"recipient": "owner-configured", "application": "wahojobs-beta", "event": {...}}`.
There is no suitable existing operational alert sender; authentication-code
delivery and candidate reminders are not repurposed. The adapter runs as
`wahojobs-beta`, has 15 seconds per event, and must deduplicate event IDs. Dispatch
is durably marked attempted first; ambiguous/failing delivery is not retried
automatically. `accepted_by_adapter` proves adapter acceptance only; actual owner
receipt must be checked separately. Pending/failed delivery remains visible in
the report. Adapter credentials, if needed, stay outside Git and runtime logs.

**Minimal missing configuration:** an owner-selected recipient, approved delivery
transport, installed/reviewed absolute adapter command and any separately supplied
transport credential. Set `alert_delivery.approved=true` only after that approval.
No recipient was inferred, no account credential was used, and no live delivery
was tested. These missing details block activation, not this implementation.

## Validation and review

The final affected suite ran **253 tests: 250 passed, three Windows platform
skips, zero failures/errors**, in 99.822 seconds. This includes 39 milestone
regressions and the existing affected source, maintenance, backup, ownership,
card, variant and compensation suites. The validation receipt records the exact
command. Tests use isolated SQLite/filesystem
storage, retained real Mercor fields, fixture HTTP transports and test-only clocks.
They never contact employers or change real expiry semantics.

Coverage includes the real retention regression under old and corrected policies;
repeated summaries; changed/new pay and currency; same catalog timestamp;
workload changes; superseded-origin rejection; manual overrides and sibling
isolation; shared Browse/detail/Matches consumers; fresh-but-due daily execution;
100-request incomplete Alignerr pagination; missing Mercor records; source failure;
cross-process/manual locking; duplicate dispatch; elapsed request deadlines;
process-group timeout; stop/restore and receipt-write failures; remaining recovery
allowance; protected user-domain equality; age cohorts/count drops; alert
deduplication/recovery; native-trigger record interpretation; and restart/next-slot
calculation.

Independent retention and operational reviews found concrete edge cases and their
corrections were retested; neither review has an outstanding blocker in its
reviewed scope. Earlier unchanged UI/catalog and hosted Linux evidence
from the accepted checkpoint remains applicable. This Windows environment has no
usable Linux execution environment. The new native systemd units, Linux filesystem
permissions and real stop/timeout/recovery behavior are **not yet host-verified**.
Unit tests and a fixture/manual runner are not scheduler activation evidence.

## One bounded owner-approved activation

Proposed window: **2026-09-23 05:30 UTC** (02:30 America/Sao_Paulo). First collection:
**2026-09-23 06:00 UTC**, then daily 06:00 UTC, with the fixed 100+1 attempt budget,
15-minute execution and two-minute recovery limits. If approval/preflight is later,
select the first future 06:00 UTC after completing these checks and explicitly
update `first_run_at`; do not backdate it or manually fill missed days.

1. Using the existing pinned private administrative procedure, reverify hostname,
   deployed commit, config-002, normal process, authoritative database/journal and
   current protected-data baseline. Inspect actual timers/cron again for later
   legitimate changes; stop on conflicts. Stage the exact committed archive under
   `/opt/wahojobs-beta/releases/IMPLEMENTATION_COMMIT` and verify its archive hash.
   Reuse the existing Python 3.12 environment and pinned dependencies. Do not copy
   any fixture/preview database into authority or reapply completed corrections.
2. Before authoritative downtime, run the affected tests in isolated Linux storage,
   including POSIX ownership and socket restart. Verify the supplied units with
   `systemd-analyze verify` and calendar expressions with `systemd-analyze calendar`.
   Exercise the supervisor's stop, worker timeout, SIGTERM/SIGKILL cleanup and
   restart/ExecStopPost path against disposable service/storage fixtures under the
   actual systemd version. Verify that recovery has no ordering cycle. Record the
   results; do not use employer requests for this preparation. Abort activation
   if these native checks fail.
3. Prepare private directories `daily-inventory-v1`, `runs` and `backups` owned by
   `wahojobs-beta:wahojobs-beta`, mode 0700. Create the shared adjacent
   `product.sqlite3.wahojobs-maintenance.lock` as that user, mode 0600, only if absent;
   never replace/truncate an existing lock inode. Install the reviewed policy at
   **`/etc/wahojobs-inventory-v1.json`**, root:wahojobs-beta, mode 0640. This separate
   path avoids changing traversal permissions of private config-002 directories.
   Pin the exact commit and future `first_run_at`; keep `enabled=false` initially.
   Check readability as the beta user and writable/private state/backups, free disk
   space and existing immutable journal binding. Daily snapshots are not deleted
   automatically; use the existing approved backup custody/retention procedure.
4. Install the five native service/timer files under `/etc/systemd/system`, mode
   0644; `systemctl daemon-reload`. Keep both timers and boot recovery disabled.
   Deploy code through the established stopped-writer cold-backup procedure:
   stop only normal beta, confirm exclusive ownership, capture and verify the
   storage cohort with `scripts/beta_recovery.py backup` / `verify`, record protected
   hashes, atomically select this immutable release, start normal beta and run
   `scripts/private_beta_health.py --config /run/wahojobs-beta/runtime.json` as
   the beta user. Compare fresh protected/inventory/schema/acceptance fingerprints
   and ordinary read-only Browse/detail/Matches behavior. Deployment performs no
   collection or correction replay. Measure deployment downtime separately.
5. Install the owner-approved alert adapter/credentials outside the release and
   populate the recipient, absolute command list and approval bit. With timers
   still disabled, set the reviewed policy `enabled=true` and run the hourly
   health service once. It only reads stored operational state and sends queued
   operational events. Use its initial unverified-state event to confirm **real
   owner receipt**, then repeat the health check to prove unchanged issues do not
   resend. A zero exit code is insufficient. If delivery is uncertain, leave
   recurring timers disabled; resolve the adapter's event-ID receipt without an
   automatic resend. This step requires the later explicit delivery approval.
6. After the preceding checks pass, enable boot recovery and both timers:

   ```sh
   systemctl enable wahojobs-inventory-recovery.service
   systemctl enable --now wahojobs-inventory.timer wahojobs-inventory-health.timer
   systemctl list-timers --all wahojobs-inventory.timer wahojobs-inventory-health.timer
   systemctl show wahojobs-inventory.timer -p ActiveState -p LastTriggerUSec -p NextElapseUSecRealtime
   ```

   Confirm the actual next trigger is the approved first 06:00 UTC, and health is
   hourly at :25 UTC. Check the recovery unit's boot ordering. A persistent trigger
   before `first_run_at` must consume no provider budget. Do not invoke the worker
   or an unrestricted crawler manually.
7. Observe the first timer-initiated execution, recording systemd invocation/result,
   actual timer LastTriggerUSec and next execution, UTC run ID/trigger, all source
   journal plan IDs, budgets, qualifying observations and per-record cohorts.
   Verify the original supplemental pay date and current workload, integrity,
   protected user history, normal beta readiness, actual maintenance duration and
   next daily schedule. Verify hourly alert delivery and recovery deduplication.
   Counts may change legitimately; do not weaken expiry/closure rules to meet old
   totals. Only after this evidence may recurring operation be called active.

The ordinary daily unavailable interval is measured from the stop request through
normal readiness. It includes verified backup, bounded collection, deterministic
processing and integrity checks. **Expected duration is not yet measured**: record
the first real daily interval and backup/collection/restart timings during
activation; the earlier 2m14s deployment/reconciliation window is not a benchmark.
The automated execution/recovery budget is at most **17 minutes**. This is not a
guarantee that damaged infrastructure can become healthy within that time: a
failed readiness/recovery remains a visible failure requiring operator recovery.
Optional reports/alerts must never extend the service stop. A late reboot records
unknown interruption duration rather than inventing a measured interval.

## Disable and recover using the existing beta mechanisms

Disable future invocations first:

```sh
systemctl disable --now wahojobs-inventory.timer wahojobs-inventory-health.timer
```

Set policy `enabled=false`. If an inventory service is running, use
`systemctl stop wahojobs-inventory.service`; its bounded cleanup/ExecStopPost
restores normal beta. Keep the recovery unit available until normal readiness is
confirmed, then disable that unit if retiring this operation. Inspect journald,
`run.json`, the existing maintenance journal report and health state; do not delete
consumed slots, worker claims, source reservations or persistent lock files.

If native recovery failed, use the established operator procedure to confirm no
worker/lifetime owner remains, start the same selected normal beta service and
verify readiness/current protected data. Never launch a preview/recovery app over
authoritative storage. A fresh source retry is not part of V1 automatic recovery;
the next approved slot is the next opportunity.

**Rollback compatibility:** before any v2 observation has been written, the prior
immutable code can be selected with current unchanged storage using the established
code-only rollback procedure. After v2 captures/compositions exist, the old
`4ee28ce1...` code does not understand those acceptance policies. Disable scheduling,
restore normal beta using this compatible release, and fix forward or choose a
proven compatible release. Do not blindly switch to old code or restore an older
database over newer user activity. Disaster recovery remains the existing verified
snapshot-to-new-directory procedure with journal/pin and consumed-operation
reconciliation, never an overwrite of live authority.

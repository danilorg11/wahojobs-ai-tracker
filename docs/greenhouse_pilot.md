# Greenhouse source-registry pilot

The Greenhouse pilot is deliberately separate from normal crawling and product
inventory. Registry contract version 2 uses three exact Boolean controls:

- `connector_enabled_for_dry_run` permits public GET validation through the pilot.
- `product_enabled` permits product inventory admission.
- `production_crawl_enabled` permits ordinary crawler dispatch.

GitLab, Customer.io, and Testlio are dry-run-only. Invisible is disabled for all
three controls, including legacy aliases. Meridial is the reviewed production
control and retains root department `4012485101`.

The registry rejects unknown fields, unsupported versions, malformed nested
objects, unknown enum values, and arbitrary family, country, region, or language
targets. Job hosts are explicit per source. A Greenhouse job URL is accepted only
when its HTTPS host is allowed and its path is exactly
`/{board_identifier}/jobs/{greenhouse_job_id}`.

## Operational safety

Each entry has a count-drop policy. The default applies after a previously accepted
snapshot of at least 10 rows and quarantines a new result retaining less than 50%.
The observed rows remain diagnostic, but the anomalous result is non-successful and
cannot close missing rows. Failed, partial, and anomalous observations never replace
the last accepted count.

Readiness uses only version-controlled or future durable observations. It requires
three distinct run IDs and timestamps, the same parser contract, complete and
non-anomalous outcomes, an unbroken trailing streak, and at least 24 hours between
the first and last observation. Repeating `--snapshots 3` is a technical probe and
records zero readiness observations.

Regional labels are applicant restrictions. `Americas`/`AMER`, `EMEA`, and `APAC`
use the documented country mapping in `wahojobs/matching/locations.py`; exact labels
such as `Remote US` and `Remote Germany` remain country-specific. Generic `Remote`
is unknown rather than worldwide. Only explicit `Global` or `Worldwide` location
metadata is broad.

## Dry-run command

The command prints deterministic JSON to stdout:

```powershell
python -B scripts/greenhouse_pilot.py --snapshots 1 --lifecycle-probe
```

Use `--format human` for a compact summary derived from the same report object.
Both formats name technical validity, structural completeness, count-anomaly
safety, closure authorization, historical readiness, terms, coverage, product
enablement, and production-crawl enablement separately.

It uses public Greenhouse GET surfaces. Lifecycle and Meridial canonicalization
probes use automatically removed temporary SQLite databases. Persona coverage reads
the current baseline database in SQLite read-only mode, keeps pilot rows in memory,
and reports baseline versus each board and all pilots combined.

Exit codes are:

- `0`: every selected board completed a strict, non-anomalous technical dry run;
- `1`: any board was partial, malformed, contract-invalid, URL-invalid, anomalous,
  or otherwise incomplete;
- `2`: command usage or registry/configuration validation failed before execution.

`--require-production-ready` also returns `1` when technical validation passes but
terms, historical readiness, closure, acceptance, coverage, or enablement gates are
not satisfied.

## Local operational-observation ledger

Operational evidence is stored outside SQLite and outside Git as one immutable JSON
bundle and one immutable commit receipt per command invocation. Nothing is written unless
`--record-observation-dir` is supplied explicitly. The default ignored location is
`exports/greenhouse_pilot_observations/`; the command does not modify the source
registry, create crawl runs, import jobs, or use the ordinary production crawler.

Record the next durable observation with:

```powershell
python -B scripts/greenhouse_pilot.py --snapshots 1 --lifecycle-probe --format json --record-observation-dir exports/greenhouse_pilot_observations
```

Do not use `--evaluated-at` while recording. Observation times and IDs are generated
inside the actual invocation. Multiple fetch attempts requested with `--snapshots`
still produce at most one observation per requested board.

Verify the entire local history without network or database access:

```powershell
python -B scripts/greenhouse_pilot.py --history-dir exports/greenhouse_pilot_observations --verify-history --format json
```

Evaluate per-board readiness from verified durable history only:

```powershell
python -B scripts/greenhouse_pilot.py --history-dir exports/greenhouse_pilot_observations --evaluate-readiness --format json
```

Offline option handling is an allowlist, not an ignore list. Verification accepts
only `--history-dir`, `--verify-history`, and `--format`. Readiness accepts only
`--history-dir`, `--evaluate-readiness`, `--format`, and the optional strict
`--require-production-ready` check. Board selection, registry overrides, snapshot
counts, lifecycle and coverage flags, recording flags, and every other fetch option
are usage errors in offline modes and exit `2` before network, SQLite, history
evaluation, or filesystem writes.

Use repeated commands on separate operating days. A valid operational snapshot
streak requires three distinct successful runs, observations, and non-overlapping
invocation intervals,
an unchanged parser and compatible registry contract, closure safety, no intervening
failure or anomaly, and at least 24 hours from the earliest to latest observation.
The earlier unpersisted Operational Readiness Observation 1 had no durable run ID,
observation ID, or evidence bundle. It is a technical rehearsal only and never
counts. After this ledger is accepted, the first recorded command above becomes the
new durable Observation 1.

### Bundle, receipt, and recovery contract

Each ledger has this fixed layout:

```text
<ledger>/
  bundles/<sequence>--<bundle-id>.json
  receipts/<sequence>--<receipt-id>.json
  working/
  .ledger-lock
```

The first publication creates a random `ledger_id`; every later bundle and receipt
must retain it. Bundle schema `greenhouse_pilot_observation_bundle_v2` contains bounded identities,
timestamps, registry/parser hashes, technical metrics, closure results, measured or
explicitly unmeasured canonical/coverage summaries, and authorization statuses. It
does not contain credentials, cookies, raw HTML pages, or complete job descriptions.
Final readiness and streak fields are deliberately absent: they are derived only
from the complete verified history.

Receipt schema `greenhouse_pilot_observation_receipt_v1` is the local publication
witness. It binds the ledger sequence, ledger/run/bundle identities, bundle hash,
previous bundle and receipt hashes, and UTC publication time. Canonical JSON and
SHA-256 protect both linear chains. Verification requires a one-to-one bundle and
receipt pair at every contiguous sequence, matching hashes and identities, one root,
one ledger ID, and strictly increasing, non-overlapping invocation intervals.
Readiness walks that verified chain in publication order; it never sorts timestamps
to repair invalid evidence.

Recording uses an OS-backed process lock on Windows and POSIX. It writes and fsyncs
private temporary files only in `working/`, publishes the bundle create-only, then
publishes its receipt create-only, and fsyncs directory entries where supported.
Existing artifacts are never replaced. Readers take the same process-safe lock and
cannot accept a half-published pair. Before staging begins, the writer verifies the
existing chain and validates the complete candidate append, including invocation and
receipt chronology, head fingerprints, sequence, and ledger identity. A clock rollback
is rejected without fabricating a future timestamp or publishing an artifact.

A crash before bundle publication may leave a recognized, non-authoritative staging
file in `working/`. It is reported as residue but never counts as evidence, never
blocks otherwise valid published history, and is ignored by readiness evaluation.
Verification and evaluation are read-only and never clean residue. A later recording
invocation, while holding the exclusive process lock, may remove only recognized
regular staging files before recording its new pair. Unknown files, symlinks, hard
links, and malformed staging names remain blocking. A crash after bundle publication
but before receipt publication leaves an orphan published bundle; verification fails
closed and requires reviewed quarantine or recovery. A failure after receipt
publication leaves a complete pair even if the command cannot report success. Always
verify before retrying an uncertain publication; never delete or auto-repair an orphan.

One board failure does not rewrite the other boards' facts. A mixed invocation is
stored as `partial_success`: failed board evidence breaks only that board's streak,
while successful board evidence may extend its own. The command still exits `1`.

Evidence limits are shared code constants: at most 64 boards per invocation, 64
reason/diagnostic codes per board, 96 characters per code, 512 characters per safe
diagnostic label, and 128 retained entries per bounded metric. Bundles are limited to
256 KiB and receipts to 16 KiB. Coverage title/company truncation records an omitted
count and canonical digest. Oversized or structurally unbounded evidence is rejected;
5,000 diagnostic reasons are never stored.

Ordinary JSON and human output use the same allowlisted failure diagnostics as the
stored observation. They contain a bounded stable code and generic safe message only;
raw exception strings, arguments, chained causes, URLs, paths, credentials, cookies,
HTML, and source payloads are never included. Local traceback debugging, if added in
the future, must be explicitly enabled, written to standard error only, and must never
be recorded in an observation bundle.

Verification uses `lstat`-style checks and rejects symlinked ledger/artifact paths,
nonregular or hard-linked JSON evidence, unsafe generated identities, unexpected
nested directories, stale temporary files, filename mismatches, two roots, forks,
cycles, missing predecessors, duplicate/skipped sequences, mixed ledger IDs, copied
splices, and orphan bundles or receipts. Deleting only the newest bundle or only its
receipt is therefore detected and can never increase readiness.

If local evidence is invalid, stop scheduling observations and preserve the
directory for review. Do not edit evidence to repair it and do not import the old
ephemeral rehearsal. This remains a local tamper-evident ledger, not remote
attestation. A machine owner can delete both the newest bundle and matching receipt,
or replace the complete directory with an older valid copy; without an external
witness, that deliberate tail rollback is not always detectable. Future high-stakes
automation would require an external hash anchor, signed release, or remote witness.
Production activation therefore always remains a human-reviewed decision.

Technical history never grants legal or product authorization. Terms review,
`independent_acceptance_approved`, persona-coverage approval, closure approval,
`product_enabled`, and `production_crawl_enabled` remain separate gates. Repeated
technical observations cannot change any of them. GitLab, Customer.io, and Testlio
therefore remain non-production sources while their terms and acceptance reviews are
pending.

## Metric definitions

The report separates raw records, accepted source records, stable identities, exact
titles, normalized title repetition, and actual canonicalization. Canonical count
and yield are `null`/`unmeasured` unless the existing canonicalizer ran in a temporary
database. Title repetition is never labeled canonical duplication.

`relevant_posting_count` is the number of distinct fetched source records admitted
by the unchanged personalized product projection for at least one of the 28 reviewed
personas when that source is evaluated independently. It is explicitly `unmeasured`
when `--skip-coverage` is used. Coverage deltas are separate:
they compare the complete read-only product baseline with each pilot and with all
pilots combined. Regional and qualification rejection counts are calculated across
the full evaluated result set rather than a capped top-results sample.

Rich source records preserve typed scalar fields plus deterministic JSON for public
metadata, compliance, education, compensation, and the bounded raw public payload.
HTML and URLs remain untrusted source data; browser rendering must still escape them.

## Production boundary

Technical validity is not production readiness. Until company-specific terms are
approved, independent acceptance is recorded, three historical snapshots pass,
temporary closure behavior passes, and persona coverage has no new eligibility
leakage, pilot entries remain `product_enabled=false` and
`production_crawl_enabled=false`.

Testlio `Future Roles` remain always-open public leads. Testlio airport work is kept
as local field work in pilot rows, not promoted as remote digital inventory.

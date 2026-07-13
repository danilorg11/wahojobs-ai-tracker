# Pipeline State Foundation

`user_pipeline_items` remains a transitional compatibility mirror. The normalized
projection and transition ledger are authoritative for the cutover browser reads and
actions described below, while legacy consumers continue to receive the deterministic
compatibility fields maintained by the orchestrator.

## State model

`user_pipeline_state` separates three dimensions:

- `workflow_status`: progress through an application or work relationship.
- `visibility`: whether the user has hidden the opportunity.
- `reminder_at`: an independent reminder timestamp.

The projection is the current query-friendly state. `version` is incremented for every
successful transition and is used for optimistic concurrency checks.

`user_pipeline_transitions` is the append-only audit record. Every mutation stores a
deterministic before/after snapshot, state versions, actor, idempotency key, and optional
undo/correction reference. SQLite triggers reject updates and deletes.

## Legacy migration

The normal runtime schema at `wahojobs/db/schema.sql` intentionally contains only the
legacy/bootstrap objects. Application startup, `run_daily.py`, repository initialization,
and product-state initialization do not install the normalized projection or ledger.

The versioned DDL lives at `wahojobs/db/migrations/001_pipeline_state.sql` and is installed
only by the explicit migration command:

```text
python scripts/pipeline_state_migration.py --db path/to/reviewed-copy.sqlite --dry-run
python scripts/pipeline_state_migration.py --db path/to/reviewed-copy.sqlite --yes
```

The CLI requires separate `--allow-workspace-db` authorization before it will access the
configured workspace database. It never installs the legacy/base schema into an empty file.

The backfill creates at most one `legacy_snapshot` transition per existing pipeline item.
It is a baseline observation, not evidence that a user performed an action.

- Legacy workflow statuses become `inferred_legacy` workflow state.
- `not_interested` becomes hidden with unknown legacy workflow state.
- `remind_later` preserves a valid reminder date but leaves workflow state unknown.
- Unknown statuses and malformed reminder values remain in baseline metadata and are not
  promoted into valid projected values.

The migration is deterministic and idempotent. Dry-run is read-only. Apply requires `--yes`
and should first be exercised against a temporary database copy. DDL installation, baseline
backfill, reconciliation, and the `001_pipeline_state` marker commit in one transaction. A
failure rolls all of them back. A complete marked migration is a no-op on rerun; incomplete
or inconsistent external installations are rejected for explicit review.

## Analytics

Baseline transitions do not count as funnel conversions. Visibility and reminder events
also do not count as conversions. Correction history is a linear chain rooted in a workflow
transition. Each correction replaces the effective meaning of the transition or terminal
correction it references. The terminal unsuperseded row counts only when its before/after
workflow states form an allowed forward workflow transition. A terminal Undo suppresses the
entire referenced chain. Independent later workflow transitions remain independently
effective.

The service prevents competing correction/Undo children and rejects corrections of
baselines or non-workflow dimensions. A correction may itself be corrected or, while it is
the latest compatible mutation, undone. Malformed, cross-owner, cyclic, out-of-order, or
branching histories fail closed instead of producing analytics events. Original,
superseded, corrected, and undone rows remain queryable in the append-only ledger.

`applicant_status_updates` remains unchanged. It is still a legacy external-funnel and
directional-signal record, not the authoritative pipeline transition ledger.

## Product-action orchestration foundation

`wahojobs.pipeline_actions` composes the low-level state service, the temporary legacy
mirror, and applicant updates under one outer `pipeline_state.atomic()` boundary. Current
production callers are intentionally not connected yet.

Every product mutation requires an owner, MatchRun identity, and an unpredictable caller
idempotency key. Expected versions must be built-in integers: zero or omission is accepted
only for opportunity-identity creation requests, while existing items require a positive
version. Booleans, integer subclasses, coercible values, explicit null, and negative values
are rejected before stored-key lookup or any write.

Caller keys may not use the reserved `wahojobs-internal:pipeline-action:v1:` namespace.
The caller key is reserved for the operation's terminal ledger transition. Preparatory
initialization or unknown-workflow resolution keys hash the caller key, complete canonical
operation fingerprint, pipeline identity, and stable step name under that reserved prefix.
They reveal none of the original payload and remain inside the same transaction.

The terminal transition acts as the complete-operation commit marker. Its metadata stores
the canonical request fingerprint and a versioned original result snapshot: item identity,
normalized state/version, compatibility fields and timestamps, creation outcome, terminal
and preparatory transition identities, and the complete applicant row returned at operation
time or explicit null. Exact retries validate persisted ownership and snapshot integrity,
then return the immutable snapshot without querying mutable applicant, projection, or mirror
state. Changed semantic input receives the privacy-preserving global idempotency conflict.

`wahojobs.pipeline_transition_metadata` owns the shared fail-closed contracts used by replay
and reconciliation. New terminal operations, operation no-ops, user initializations, and
applicant receipts carry explicit versioned schema identifiers. Applicant-producing actions
also store a canonical receipt bound to the action, deterministic update ID, status, profile,
opportunity, and writer-controlled fields; non-applicant actions store an explicit null
receipt. Snapshot objects reject missing, extra, coercible, or incorrectly typed fields.
Migration 001 predates these identifiers, so its legacy baseline contract instead recognizes
the exact five-field payload already installed: raw status/reminder, reminder validity,
classification, and the legacy-snapshot marker. Empty or semantically incompatible baseline
metadata is blocking drift, while the 70 existing baseline rows remain unchanged.

Migration-created `legacy_snapshot` transitions remain `baseline`. New product items use a
protected `user_initialization` transition class on the existing workflow dimension, with
Saved as the initial state and an internal key. Accepted fresh-key repeats use a protected
`operation_noop` workflow-class transition: before/after dimensions are identical, version
increments once, and no applicant or legacy compatibility field changes. Initialization,
no-op, visibility, and reminder transitions are excluded from funnel conversions; no-op and
user-initialization transitions cannot be corrected or undone.

Repeated-action no-ops are action/state bound by one shared contract. Save, Applied,
Assessment Started, Assessment Completed, Accepted, and Rejected require the matching
workflow state; Remind Later requires the same canonical reminder; Not Interested requires
hidden visibility; and Show Again requires a visible Saved item. New user-created items may
start only with Save, Applied, or Not Interested. Their initialization metadata is bound to
exactly one terminal operation by item, owner, complete operation fingerprint, action,
terminal shape, explicit preparatory transition reference, and an exactly recomputed
SHA-256 internal key. A reserved-prefix lookalike is not sufficient.

The compatibility mirror is deterministic: hidden state maps to `not_interested`, then a
known workflow wins over any reminder, then an unknown workflow with a reminder maps to
`remind_later`. A visible unknown workflow without a reminder is an invariant error.
Reminder dates are mirrored independently. This layer remains transitional and never
becomes authoritative.

`wahojobs.pipeline_records` supplies a non-repairing joined read model. The read-only
`scripts/pipeline_state_reconcile.py --db PATH` command checks migration objects,
projection/ledger consistency, ownership, version chains, mirrors, unresolved workflows,
and deterministically expected applicant updates. Ledger reconciliation parses every state,
checks complete before/after continuity, dimension-specific changes, initialization/no-op
contracts, references, branching and cycles, and final projection equality. Applicant rows
are grouped by stable update identity and compared with the latest deterministic orchestrator
snapshot; database-generated `id`, `created_at`, and `updated_at` are intentionally excluded.
It reports migration baselines, user initializations, and operation no-ops separately and
has no repair mode. Human output groups blocking findings by stable reason code and prints
only safe item/transition identifiers plus a concise remediation description; JSON uses the
same findings. Neither format exposes raw fingerprints, snapshots, applicant evidence, or
notes.

## Browser cutover

Matches and My Jobs load browser state through `wahojobs.pipeline_records` and treat
`user_pipeline_state` as authoritative for workflow, provenance, visibility, reminders,
versions, filters, badges, and available controls. Browser mutations call only
`wahojobs.pipeline_actions`; the legacy pipeline row and applicant update are coordinated
inside that transaction. Rendered forms use server-generated caller keys, omit versions for
untracked opportunities, and include the current normalized version for tracked items.
Stale and conflicting requests are reported without automatic retry or stored-request
disclosure.

Action POSTs use strict single-value parsing for identities, versions, caller keys, and
return context. Duplicate, whitespace-normalized, aliased, or noncanonical values fail
before orchestration. Idempotent operation results remain immutable, while browser status,
controls, reminders, and replacement-form versions are always rendered from a fresh
normalized record. My Jobs inline responses return server-rendered normalized filter and
count fragments so cards leave their old view immediately after cross-filter transitions.

The legacy dashboard URLs retain their market/reporting context, but their pipeline records,
tracked index, action plan, badges, reminders, and controls are overlaid from normalized
pipeline records before browser rendering. They do not fall back to the compatibility
status for browser decisions. Reconciliation distinguishes **fully reconciled** state from
state that is **safe for normalized reads**: legacy status or reminder mirror drift remains
an operational reconciliation finding, but does not take normalized read-only browser
surfaces offline. Missing or inconsistent projections, ownership or ledger corruption,
protected-metadata failures, and other normalized-integrity findings still fail closed.

Browser mutations continue to require fully reconciled state. Pre-existing compatibility
mirror drift is neither repaired nor bypassed by the read-safety policy, and the normal
reconciliation CLI retains its existing blocking exit behavior for that drift while also
reporting whether the database is safe for normalized reads.

Unknown migrated workflows remain explicit. Hidden unknown rows offer only **Show again as
Saved**, which uses the fixed normalized resolution operation; visible unknown rows with a
reminder remain outside workflow filters until an explicit workflow action resolves them.
A visible unknown workflow without a reminder is blocking drift, not an implicit Saved
state.

The legacy `product_state.py` pipeline writers (`import-pipeline`, `save-opportunity`,
`update-status`, `remind-later`, and `mark-not-interested`) fail before writing whenever
migration 001 or its normalized tables are present. Read-only commands and standalone
applicant-update commands remain available. Legacy-schema test databases without normalized
objects retain their historical command behavior.

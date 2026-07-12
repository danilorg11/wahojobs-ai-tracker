# Pipeline State Foundation

`user_pipeline_items` remains the compatibility source used by the current local UI.
The normalized projection and transition ledger are infrastructure for a later reviewed
cutover; this milestone does not change existing actions or reports.

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

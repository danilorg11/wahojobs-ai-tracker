# Persistent Profile Migration 005

Migration 005 is a dormant, forward-only schema migration for persistent product
profiles. Migration 004 was installed with all persistent-profile tables empty.
Migration 005 uses that empty-state boundary to rebuild the three profile tables
without migrating or reinterpreting any profile data.

The migration is committed infrastructure only and remains unapplied. It is not
part of application startup, browser routes, MatchRuns, matching, ownership, or
account services. No repository mutation service or row reconciliation exists
for persistent profiles yet. That product integration remains future B2B work.

## Durable Canonical Version

After Migration 005, `canonical_profile_v2` is the only canonical version that
may be stored in `product_profile_revisions`. `canonical_profile_v1` remains an
input and matcher-compatibility projection only. The migration refuses to run if
`product_profiles`, `product_profile_revisions`, `product_profile_sources`, or
`current_product_profiles` contains any row, so no durable V1 row is migrated.

SQLite explicitly presence- and type-checks the required durable V2 envelope
fields, including the schema version, identity object, and persistent profile
ID. It also enforces profile identity agreement, hashes, bounded JSON shape,
lowercase ASCII `snake_case` structural keys, duplicate-key detection, and
recursive raw-content and identity privacy rules. It intentionally does not
duplicate the complete Python Canonical Profile V2 validator. A future service
must perform complete semantic validation before writes, and future row
reconciliation must revalidate durable V2 documents.

## Controlled Lifecycle Source

Lifecycle-only revisions use one source with:

- `source_type = confirmed_lifecycle_action`
- `source_format = application/json`
- `source_schema_version = confirmed_lifecycle_action_v1`
- `source_ordinal = 1`

The exact source bytes must be one of:

```json
{"action":"archive","schema_version":"confirmed_lifecycle_action_v1"}
{"action":"reactivate","schema_version":"confirmed_lifecycle_action_v1"}
{"action":"deletion_request","schema_version":"confirmed_lifecycle_action_v1"}
```

No alternate serialization, free text, user reason, identity, raw profile input,
or arbitrary metadata is accepted. Archive, reactivate, and deletion-request
revisions must use their matching action. Their structured profile JSON, hash,
canonical version, profile, principal, and environment remain unchanged from the
immediately preceding revision. A lifecycle revision has exactly one source and
cannot be a correction.

The installed ordinary source types remain `confirmed_about_you_text` and
`user_confirmed_correction`. Ordinary sources cannot support lifecycle-only
revisions, and lifecycle sources cannot support initial, edit, or correction
revisions. Archived edits and corrections remain archived until an explicit
reactivation. `deletion_requested` remains terminal.

Profile purge remains one controlled transaction that cascades the profile,
revision, and source unit only after the terminal lifecycle state. No durable
purge receipt is added.

## Migration Safety

The guarded CLI is read-only unless `--yes` is supplied:

```text
python scripts/persistent_profile_canonical_v2_migration.py --db TEMP.sqlite --json
python scripts/persistent_profile_canonical_v2_migration.py --db TEMP.sqlite --yes --json
```

Any access to the configured workspace database additionally requires
`--allow-workspace-db` after separate authorization. The implementation is not
approved for workspace application in this milestone.

The controller checks profile-state emptiness before row-level integrity and
foreign-key validation, so valid or malformed profile rows receive the same
`persistent_profile_state_not_empty` refusal without inspection or repair. It
then attests the applicable M004 or M005 state, acquires `BEGIN IMMEDIATE`,
checks emptiness again, and keeps foreign keys on. The CLI preserves specific
M005 partial-installation, conflicting-object, and schema-drift classifications
instead of reducing them to a historical M004 prerequisite error.

The M005 attestation assigns structured finding categories rather than deriving
state from diagnostic text. `partial_inconsistent` means the marker and object
footprint describe an incomplete or interrupted installation, including missing
named objects or migration backup residue. `schema_mismatch` is reserved for a
complete, correctly typed footprint whose definitions differ from the canonical
manifest. `conflicting` means an unexpected M005-owned object exists or an
expected name is occupied by the wrong SQLite object type. When findings coexist,
the deterministic precedence is conflicting, then partial/inconsistent, then
schema mismatch.

It removes the M004 view, triggers, and table-owned indexes; renames the three
empty tables to migration-specific backup names; creates the final tables;
verifies the backups remain empty; drops them child-first; recreates exact
indexes, triggers, and view; writes marker 005; then attests schema, emptiness,
integrity, foreign keys, and preservation before commit. Any failure rolls the
entire operation back to the exact M004 state.

Failure injection exposes 106 logical before/after hook labels across 43
distinct transaction-visible durable states. Adjacent validation hooks that do
not change durable state are intentionally not counted as separate states.

Valid durable states are either clean M004 with marker 005 absent, or the exact
final M005 manifest with both M004 and M005 lineage markers. Forged markers,
missing markers, weakened definitions, unexpected objects, and leftover backup
tables are blocking.

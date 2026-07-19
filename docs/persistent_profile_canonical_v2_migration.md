# Persistent Profile Migration 005

Migration 005 is a dormant, forward-only schema migration for persistent product
profiles. Migration 004 was installed with all persistent-profile tables empty.
Migration 005 uses that empty-state boundary to rebuild the three profile tables
without migrating or reinterpreting any profile data.

Migration 005 is installed, fully attested, empty, and dormant. It is not part
of application startup, browser routes, MatchRuns, matching, ownership, or
account services. No repository mutation service or row reconciliation exists
for persistent profiles yet. The installed schema is capability only and
remains disconnected from normal product runtime.

## Installation Record

Migration 005 was installed on 2026-07-19 from source commit `7ad3c6e` using:

```text
python -B scripts/persistent_profile_canonical_v2_migration.py --db data/wahojobs.sqlite --yes --allow-workspace-db --json
```

The migration completed successfully with `database_state = migrated` and
`changed = true`. The installed migration marker is
`005_persistent_profile_canonical_v2`. Marker state after installation was:

```text
001 / 002 / 003 / 004 / 005 = 1 / 1 / 1 / 1 / 1
```

The installed persistent-profile schema contains exactly:

- 3 tables: `product_profiles`, `product_profile_revisions`, and
  `product_profile_sources`;
- 1 view: `current_product_profiles`;
- 7 named indexes;
- 12 SQLite automatic indexes;
- 9 triggers;
- 32 persistent-profile schema objects;
- 167 total `sqlite_master` objects.

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
`--allow-workspace-db` after separate authorization. Migration 005 has been
installed exactly once; further workspace mutation is neither required nor
authorized by this installation record. A read-only installed-state inspection
reports `database_state = already_migrated` and `changed = false`.

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

## Installed Durable Capabilities

The installed schema enforces these durable boundaries:

- `canonical_profile_v2` is the only accepted durable canonical version;
- `canonical_profile_v1` remains an input and runtime matcher compatibility
  format only;
- required Canonical V2 schema and identity fields use explicit null-safe
  presence and type checks;
- `identity.profile_id` must agree with the persistent profile container;
- the lowercase-ASCII structural-key and privacy envelope remains enforced;
- `confirmed_about_you_text` and `user_confirmed_correction` remain supported;
- `confirmed_lifecycle_action` uses the exact
  `confirmed_lifecycle_action_v1` JSON contract;
- archive, reactivate, and deletion-request revisions require their matching
  lifecycle action;
- lifecycle-only revisions preserve the prior structured profile content,
  hash, and schema version;
- source sealing, revision/source immutability, current-view derivation,
  terminal deletion, and controlled purge remain enforced;
- no purge-receipt table exists.

## Installed-State Safety

Immediately after installation, `product_profiles`,
`product_profile_revisions`, `product_profile_sources`, and
`current_product_profiles` each contained zero rows. All Accounts and Ownership
tables also remained empty. No profile, revision, source, account, principal,
alias, binding, identity, session, or ownership event was created.

No seed, backfill, conversion, or legacy-profile rewrite occurred. `local_user`
and all real legacy aliases remained unregistered. Browser persistence,
authentication, authorization, ownership claiming, MatchRun persistence, and
normal-runtime Canonical V2 integration remain disconnected.

Preserved legacy state after installation was:

- legacy `user_profiles`: 10;
- pipeline items: 73;
- pipeline state/projections: 73;
- transitions: 86;
- applicant updates: 77.

All 22 pre-existing non-migration data tables were content-compared with the
verified immediate recovery backup, with zero differences.

## Validation Record

- Combined Migration 004/Migration 005 schema attestation:
  `correctly_installed`, with zero findings.
- Integrity check: `ok`.
- Foreign-key violations: 0.
- SQLite sidecars: none.
- Focused Migration 005, Canonical V2, and runtime tests: 75 passed.
- Full repository suite: 809 passed, with 4 documented platform skips.
- Canonical projections: 25/25.
- Matching comparisons: 4,000.
- Label, section, top-10, and snapshot changes: zero.
- Benchmark unchanged: labels 26/30, sections 29/30, full agreement 26/30.

A verified UTC-timestamped recovery backup was created outside the repository
immediately before installation. It matched the approved pre-migration state;
integrity and foreign-key checks passed; it contained the Migration 004 schema
and markers 001 through 004 only; persistent-profile, Accounts, and Ownership
state was empty; legacy and pipeline counts matched; and no sidecars existed.
Exact local paths, filenames, fingerprints, timestamps, byte sizes, and other
machine-specific evidence remain only in local operational records.

The workspace database was independently verified after installation. Journal
mode remained `delete`, integrity remained `ok`, foreign-key violations were
zero, schema count remained 167, markers were 1 / 1 / 1 / 1 / 1, and no SQLite
sidecars or temporary rebuild objects remained. Persistent-profile, Accounts,
and Ownership tables remained empty. Exact file fingerprints and
machine-specific details remain only in local operational evidence.

## Product Integration Boundaries

Installation does not make persistent profiles available to users. There is no
profile repository or mutation service, row-level reconciliation, browser About
You persistence, MatchRun persistence, active account ownership, authenticated
profile access, or resume ingestion.

Future milestones remain separated as follows:

- B2B1: domain commands, IDs, hashes, fingerprints, errors, and canonical
  projection;
- B2B2: create, append, read, and purge repository services;
- B2B3: row reconciliation and CLI;
- B2C: controlled development/test profile creation;
- B2D: reviewed MatchRun persistence;
- B2E: authenticated browser persistence and authorization cutover.

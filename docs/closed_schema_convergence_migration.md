# Closed-Schema Convergence Migration 007

## Authority and purpose

Migration 007 establishes one closed SQLite schema for durable runtime and
offline private-beta operations. It resolves two accepted Migration-001 through
Migration-006 construction lineages that contain the same 176 named schema
objects but different stored `CREATE TABLE` text:

- the historical ALTER-evolved lineage has schema fingerprint
  `e866286ba8b1dd28c6b5258c3bd04ddb30ccb00760e677893c22b2c6decf042e`;
- the current public `initialize_database(<explicit Path>)` lineage has schema
  fingerprint
  `37a156dd9677e2bdb0eba5168a4ea150e30d771e1e7104a3b368f31667c2eaed`.

The sole object-definition differences are the `companies` and `jobs` table
definitions. Historical ALTER installation placed later columns at the end of
each table and did not install the
`jobs.canonical_opportunity_id -> canonical_opportunities.id` foreign key.
Current base-schema creation has the canonical column order and that foreign
key. Migration 007 converges the historical form to the current form and then
records `007_closed_schema_convergence`.

Both accepted helper indexes remain authoritative:

- `idx_jobs_live_market` on
  `(include_in_live_market_estimate, is_active)`; and
- `idx_jobs_canonical_opportunity` on `(canonical_opportunity_id)`.

The former supports live/countable inventory filtering and the latter supports
canonical-opportunity linkage. They affect query performance, not row
semantics. The former 174-object fingerprint
`f45e9d4c8c0f487a8437fdf1f5a323010d7c0b56c5d4a61a07ee4fe1f4f53735`,
which omitted both indexes, is unsupported drift and is not a migration source.

The installed authority is exactly:

- 176 schema objects;
- fingerprint
  `37a156dd9677e2bdb0eba5168a4ea150e30d771e1e7104a3b368f31667c2eaed`;
- exact ordered migration markers 001 through 007; and
- no temporary-schema object or `companies_m007_backup` /
  `jobs_m007_backup` residue.

Runtime and PB-OPS consume the same read-only closed-schema authority. Both
reject missing or altered helper indexes, extra objects, changed table SQL,
missing or forged markers, wrong marker lineages, and migration residue. Their
outer database boundaries also retain rollback-journal, path/identity,
sidecar, integrity, and foreign-key validation.

## Supported states

The migration accepts only these exact source states:

| State | Schema | Markers | Apply action |
| --- | --- | --- | --- |
| `legacy_rebuild_pending` | historical 176 / `e866...` | exact 001-006 | atomically rebuild `companies` and `jobs`, then write 007 |
| `canonical_marker_pending` | canonical 176 / `37a...` | exact 001-006 | write only marker 007 |
| `exact_installed` | canonical 176 / `37a...` | exact 001-007 | byte-preserving no-op |

All other states fail closed. Distinct reports are retained for schema drift,
partial or forged M007 state, invalid prerequisite markers, migration residue,
incompatible canonical links, quick/integrity failure, foreign-key failure,
invalid journal mode, unsafe target identity, sidecars, contention, and cleanup
or post-commit verification failure. The migration never guesses a lineage and
never upgrades the unsupported 174- or 175-object variants.

## Data-preservation contract

The historical path uses one `BEGIN IMMEDIATE` transaction. It creates
transaction-local backup tables, verifies the copied values, rebuilds only
`companies` and `jobs` from the checked-in Migration-007 SQL, restores the two
tables' exact rows, recreates their accepted indexes, restores exact
`sqlite_sequence` authority, drops the backup tables, and writes the marker.
Any pre-commit failure rolls the entire operation back.

The final database preserves every `companies` and `jobs` value and SQLite
storage type, including IDs, timestamps, source classifications, nullable
fields, Unicode, and maximum-range integer authorities. Every unrelated table,
row, schema object, and migration marker remains unchanged. The complete final
`sqlite_sequence` inventory is identical to the source: Migration 007 may
rewrite the `companies` and `jobs` sequence rows internally while rebuilding
those tables, but restores them exactly, and it does not rewrite any other
sequence row.

The migration explicitly protects the direct dependency graph:

- `jobs.company_id -> companies.id`;
- `jobs.canonical_opportunity_id -> canonical_opportunities.id` in the final
  schema;
- `canonical_opportunities.company_id -> companies.id`;
- `crawl_runs.company_id -> companies.id`; and
- `job_events.job_id -> jobs.id`.

Existing canonical-opportunity or other foreign-key orphans block migration;
they are reported, not repaired. Successful apply and post-commit reopen both
require `quick_check(1) = ok`, `integrity_check = ok`, zero foreign-key
violations, rollback-journal mode, exact schema/marker authority, stable file
identity, no SQLite sidecar, and clean PB-OWN release.

## Command boundary

The dedicated command is target-selecting and read-only unless `--yes` is
present:

```text
python -B scripts/closed_schema_convergence_migration.py --db <ABSOLUTE_EXISTING_DATABASE> --json
python -B scripts/closed_schema_convergence_migration.py --db <ABSOLUTE_EXISTING_DATABASE> --yes --json
```

`--help` is a genuine read-only parser path. `--db` is mandatory; there is no
default database, environment fallback, creation path, initializer, seed path,
or automatic runtime invocation. Inspection opens only the explicitly selected
existing target read-only. Apply requires the `offline_operator` PB-OWN
capability and refuses symlinks, reparse points, hard links, identity changes,
SQLite sidecars, active ownership, noncanonical paths, and unsupported schema
states.

An explicitly authorized workspace-database apply has two additional gates:

```text
python -B scripts/closed_schema_convergence_migration.py --db <ABSOLUTE_WORKSPACE_DATABASE> --yes --allow-workspace-db --verified-backup <ABSOLUTE_EXTERNAL_EXACT_COPY> --json
```

The command does not create that backup. It requires a distinct ordinary file
outside the repository, proves byte size and SHA-256 equality with the selected
target, opens the backup read-only, and requires the backup to attest as the
same recognized pre-migration state. Workspace access or apply without these
separate authorizations fails closed.

This implementation and its focused validation have not applied Migration 007
to any workspace or product database. Every database constructed or mutated by
the regression suite is a new disposable external test target.

## Failure and restart behavior

Pre-commit interruption leaves the exact source lineage and no backup-table or
marker residue. The command exposes deterministic fault boundaries around
ownership, target open, every DDL/DML operation, data/sequence/schema checks,
and commit; regression tests prove rollback at every pre-commit boundary.

After commit, the canonical schema and M007 marker are durable. The command
then revalidates the exact path and identity, ownership, sidecar state, journal
mode, schema, integrity, foreign keys, and close boundary. A failure in that
phase reports a committed-but-verification-incomplete state rather than
claiming rollback. Reinspection or an exact retry observes `exact_installed`
and performs no mutation. There is no down migration and no automatic restore;
any later restoration requires a separately authorized recovery decision.

## Validation boundary

Focused regression construction uses only disposable databases outside every
checkout. The historical constructor reproduces the original Phase-1 table
definitions plus their tracked ALTER order, installs the unaffected current
base objects, and applies public Migrations 001 through 006. The fresh
constructor calls `initialize_database(<explicit temporary Path>)` and then the
same public migration APIs. Tests prove both exact predecessor fingerprints,
both convergence paths, exact row/type/sequence preservation, dependency and
orphan behavior, idempotence, consumer fail-closed behavior, CLI target guards,
all declared fault hooks, and zero live socket access.

Normal durable runtime, PB-OPS, and product requests never install Migration
007. An externally prepared private-beta database must be explicitly migrated
and attest as exact M001-M007 before either consumer may open it.

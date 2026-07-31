# Dormant Product Principal Ownership Bridge

Accounts Milestone B1 defines a durable ownership namespace without changing any current product owner. Migration 003 is installed in the workspace database as dormant infrastructure, no principal or alias has been registered, and browser authentication and product authorization remain disconnected.

## Identity boundaries

Wahojobs distinguishes three identities:

- Authentication identity proves access to an account. It is represented by `users.user_id` and provider identities from Migration 002. Email is an attribute, never an ownership key.
- Product principal is the future authorization identity for product resources. A principal has an explicit environment, lifecycle, claim policy, and immutable creation identity.
- Profile identity is the existing product-facing profile slug used by current matching and pipeline flows. It remains unchanged during B1.

The planned direction is `authenticated account -> product principal -> immutable legacy owner alias -> existing product resource`. No historical ownership-bearing row is rewritten by B1.

## Migration 003

`wahojobs/db/migrations/003_product_principals.sql` creates only:

- `product_principals`
- `legacy_owner_aliases`
- `principal_account_bindings`
- `ownership_binding_events`

It creates no claims, persistent profiles, MatchRuns, browser sessions, or product rows. It inserts no principal automatically. Migration 003 requires complete, reconciled Migrations 001 and 002. Inspection remains read-only by default; applying requires `--yes`, and accessing the configured workspace path additionally requires `--allow-workspace-db` after separate authorization. DDL, the marker, integrity checks, foreign-key checks, and ownership reconciliation are one migration-owned transaction. A failed statement leaves no Migration-003 object or marker.

Migration 003 was installed on `2026-07-18` from source commit `76991be` as version `003_product_principals` with:

```text
python -B scripts/ownership_migration.py --db data/wahojobs.sqlite --yes --allow-workspace-db --json
```

The migration completed successfully with `database_state = migrated` and `changed = true`. Marker state after installation was `001 / 002 / 003 = 1 / 1 / 1`.

The installed schema added exactly 4 tables, 7 named indexes, 8 SQLite automatic indexes, 13 triggers, and 32 Migration-003 schema objects. The raw `sqlite_master` object count changed from 103 to 135. The four installed tables are:

- `product_principals`
- `legacy_owner_aliases`
- `principal_account_bindings`
- `ownership_binding_events`

Immediately after installation, all four ownership tables and all eight account tables contained zero rows. No principal, legacy alias, account binding, or ownership event was created; `local_user` remained unregistered. No historical product owner or profile identifier was rewritten. Browser authentication and product authorization remained disconnected, so the ownership bridge remains dormant infrastructure.

Existing data remained intact:

- jobs: `9446`
- canonical opportunities: `2347`
- crawl runs: `268`
- user profiles: `10`
- pipeline items: `73`
- pipeline state/projections: `73`
- transitions: `86`
- applicant updates: `77`

Post-installation validation reported canonical schema attestation correctly installed with zero findings, clean ownership reconciliation, `integrity_check = ok`, zero foreign-key violations, and no SQLite sidecars. Discovery found 34 distinct raw values, 88 distinct alias kind/value pairs, and 483 observations. Validation completed with 46 ownership tests passed, 52 Accounts tests passed, and 711 repository tests passed with 2 documented skips. Matching benchmark results remained unchanged at labels `26/30`, sections `29/30`, and full agreement `26/30`.

A verified, timestamped recovery backup was created outside the repository immediately before Migration 003. It matched the approved pre-migration database state, passed its integrity check, had no foreign-key violations, contained 103 schema objects and only Migration 001 and 002 markers, had no ownership schema, kept all account tables empty, and had no sidecars. Exact paths and cryptographic fingerprints are retained only in local operational evidence.

The post-installation workspace database was independently verified. Journal mode remained `delete`, `integrity_check = ok`, foreign-key violations remained zero, sidecars were absent, the schema object count was 135, and marker state was `1 / 1 / 1`. Ownership and account tables remained empty. Exact cryptographic fingerprints are retained only in local operational evidence.

## Principals and immutable aliases

Principal IDs use `prn_` plus 128 random bits. Environment namespace and principal type are immutable. Supported types are `legacy_profile`, `account_native`, `development`, `sample`, and `system`; claim policies are `nonclaimable`, `manual_approval`, and `account_native`.

Legacy alias IDs use `loa_` plus 128 random bits. Exact historical values are authoritative: they are bounded but never case-folded or transformed. `(environment_namespace, alias_kind, alias_value)` is unique, and aliases reject UPDATE and DELETE. Normal services generate every principal, alias, binding, and event ID from at least 128 random bits; syntax checks also reject all-zero and other repeated-character payloads, though syntax alone cannot prove historical entropy.

Alias kinds belong to database-derived semantic families. `profile_id`, `pipeline_owner`, `applicant_user_id`, and `legacy_user_id` form the `owner_resource` family. `anonymous_user_key` forms a separate `anonymous` family. Within one environment, the same exact value in one family must resolve to one principal even when it appears under several alias kinds. The same value may resolve independently across the owner-resource and anonymous families or across environments. Callers cannot supply the family column.

Discovery is read-only. Before cutover, a well-formed unregistered legacy alias is informational. Malformed identity data or inconsistent existing ownership is blocking. Public output uses aggregate counts and deterministic report-local references such as `legacy-owner-0001`; it never emits raw aliases or stable unkeyed alias fingerprints. Report-local references are nonpersistent and may change when the inspected data changes. Discovery reports distinct raw values, distinct kind/value pairs, and total observations as separate units, with both pair-level and observation-level classification totals.

`local_user` is described only as a development owner-resource identity and is nonclaimable by default. If it is ever registered through reviewed SQL, its principal must be a nonclaimable development principal. B1 itself never registers or claims it.

## Bindings and event history

A `principal_account_bindings` row is the future authorization projection. It joins one product principal to one Migration-002 account in the same principal environment, records role and lifecycle, and uses a guarded version. Exclusive principals may have only one active owner binding. Active binding creation requires an active principal, an active account, and a claimable policy.

`ownership_binding_events` is the append-only historical source of truth. `principal_account_bindings` is its current projection. Event versions are contiguous per binding and begin with `binding_activated` at version 1. Later events support suspension, reactivation, release, and administrative correction. Relationships, environment, time boundaries, and idempotency are constrained; UPDATE and DELETE are rejected.

The dormant `create_binding_with_initial_event()` and `append_binding_event()` operations are the supported mutation boundary. They compute a lowercase SHA-256 request fingerprint internally from every semantic command field and canonical metadata. Idempotency is scoped to `(principal_id, idempotency_key)`. An exact retry returns the original sanitized event result without changing the current projection, including after later events. Reusing the key for a changed command raises one generic conflict. Event insertion and projection update share a transaction or collision-resistant caller savepoint; failure leaves neither a partial event nor a projection change. Reconciliation recomputes fingerprints from durable event content.

The `ensure_account_native_principal()` authority bootstraps one canonical active account into one active `account_native` principal and owner binding. Principal insertion delegates binding and initial-event creation to the existing mutation boundary inside the same write transaction. The append-only event remains the source of truth and the binding remains its validated projection. Exact retry, repository reconstruction, and independently connected concurrent callers converge without adding history or refreshing timestamps. Existing state is schema-attested and reconciled under the write authority; ambiguous owners, historical non-active bindings, unavailable accounts, or inconsistent event projections fail closed without replacement or repair.

Durable Google completion now invokes that authority after canonical account resolution or invited provisioning and before any trusted-login session operation. Both new and existing accounts use the same server-private dependency. Bootstrap failure issues no session; a valid lineage survives later session failure. Runtime reconstruction, retries, and concurrent logins resolve the same principal and binding without adding an event. Sessions remain account-oriented and expose no ownership identifiers.

The account ID is only the binding target. Email, provider subject, invitation material, session or cookie values, browser-selected owner IDs, and raw legacy-owner values are neither accepted nor persisted as ownership identity, and bootstrap creates no legacy alias. Authenticated empty-profile availability, legacy claiming, and ordinary-runtime activation remain deferred to later slices.

Provenance and event metadata store only bounded deterministic JSON. No redundant content-hash column is persisted because ordinary SQLite cannot independently recompute SHA-256. Domain validation uses Unicode normalization and semantic separator removal; direct-SQL triggers enforce the same bounded JSON shapes and an ASCII-key subset so Unicode lookalikes cannot bypass SQL checks. Both layers reject sensitive nested fields such as email, provider subjects, session/CSRF material, invitation secrets, resumes, raw claims or application content, credentials, database paths, SQL, authorization, and secrets/tokens, plus bearer material in values. Limits cover serialized bytes, nesting depth, node count, collection size, key length, and string length. Reconciliation applies the domain policy again and never includes rejected values in findings.

## Reconciliation

The ownership reconciler is strictly read-only:

```text
python -B scripts/ownership_reconcile.py --db <database>
python -B scripts/ownership_reconcile.py --db <database> --json
```

It performs complete schema attestation against an in-memory canonical Migration-003 manifest: normalized table and trigger SQL, column order/types/nullability/defaults/primary keys, CHECK and UNIQUE clauses, foreign keys, named and automatic indexes, index columns/origin/uniqueness, and unexpected ownership objects. Pending, partial, same-name conflict, and definition drift are distinct states. Schema drift is reported first but does not suppress independent row checks; each check runs when its own required columns are available, otherwise one bounded `check_unavailable` finding is emitted.

The reconciler also checks principal, alias, binding, and event integrity; cross-kind alias-family splits; account availability; event chains; projection agreement; durable request fingerprints; principal-scoped idempotency; append-only protection; metadata privacy; foreign keys; and database integrity. It discovers legacy aliases from `user_profiles`, `user_pipeline_items`, `user_pipeline_transitions`, and `applicant_status_updates` without registering them. Human and JSON output separates blocking drift from informational dormant aliases and never prints raw account identities, secrets, application content, raw legacy aliases, or reversible identifier hashes.

## Environment isolation

Every principal, alias, binding, and event carries an explicit environment namespace. Aliases, bindings, and events must agree with their principal; bindings and events cannot cross their established relation. Environment is part of immutable identity, not a display label or a value inferred from email/profile content.

## Deferred boundaries

- B2: reviewed legacy alias registration policy and principal seeding in an isolated migration.
- B3: guarded claim/binding service, concurrency, authorization policy, and administrative approval.
- B4: persistent canonical profiles owned by principals, with explicit migration and provenance.
- B5: browser session-to-principal authorization, MatchRun/pipeline cutover, account upgrade, and recovery UX.

Until those milestones are separately reviewed, existing product reads and writes continue using legacy profile identifiers. B1 does not change matching, My Jobs, pipeline actions, MatchRun ownership, source ingestion, or browser behavior.

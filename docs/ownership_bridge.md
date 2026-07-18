# Dormant Product Principal Ownership Bridge

Accounts Milestone B1 defines a durable ownership namespace without changing any current product owner. Migration 003 is not installed in the workspace database, no principal or alias has been registered, and browser authentication remains disconnected.

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

It creates no claims, persistent profiles, MatchRuns, browser sessions, or product rows. It inserts no principal automatically. Migration 003 requires complete, reconciled Migrations 001 and 002 and is installed only by the guarded command:

```text
python -B scripts/ownership_migration.py --db <temporary-or-reviewed-database>
python -B scripts/ownership_migration.py --db <temporary-or-reviewed-database> --yes
```

Inspection is read-only by default. Applying requires `--yes`; accessing the configured workspace path additionally requires `--allow-workspace-db` after separate authorization. DDL, the marker, integrity checks, foreign-key checks, and ownership reconciliation are one migration-owned transaction. A failed statement leaves no Migration-003 object or marker.

Migration 003 remains unapplied to `data/wahojobs.sqlite` in B1.

## Principals and immutable aliases

Principal IDs use `prn_` plus 128 random bits. Environment namespace and principal type are immutable. Supported types are `legacy_profile`, `account_native`, `development`, `sample`, and `system`; claim policies are `nonclaimable`, `manual_approval`, and `account_native`.

Legacy alias IDs use `loa_` plus 128 random bits. Exact historical values are authoritative: they are bounded but never case-folded or transformed. `(environment_namespace, alias_kind, alias_value)` is unique, and aliases reject UPDATE and DELETE. Normal services generate every principal, alias, binding, and event ID from at least 128 random bits; syntax checks also reject all-zero and other repeated-character payloads, though syntax alone cannot prove historical entropy.

Alias kinds belong to database-derived semantic families. `profile_id`, `pipeline_owner`, `applicant_user_id`, and `legacy_user_id` form the `owner_resource` family. `anonymous_user_key` forms a separate `anonymous` family. Within one environment, the same exact value in one family must resolve to one principal even when it appears under several alias kinds. The same value may resolve independently across the owner-resource and anonymous families or across environments. Callers cannot supply the family column.

Discovery is read-only. Before cutover, a well-formed unregistered legacy alias is informational. Malformed identity data or inconsistent existing ownership is blocking. Public output uses aggregate counts and deterministic report-local references such as `legacy-owner-0001`; it never emits raw aliases or stable unkeyed alias fingerprints. Report-local references are nonpersistent and may change when the inspected data changes. Discovery reports distinct raw values, distinct kind/value pairs, and total observations as separate units, with both pair-level and observation-level classification totals.

`local_user` is described only as a development owner-resource identity and is nonclaimable by default. If it is ever registered through reviewed SQL, its principal must be a nonclaimable development principal. B1 itself never registers or claims it.

## Bindings and event history

A `principal_account_bindings` row is the future authorization projection. It joins one product principal to one Migration-002 account in the same principal environment, records role and lifecycle, and uses a guarded version. Exclusive principals may have only one active owner binding. Active binding creation requires an active principal, an active account, and a claimable policy.

`ownership_binding_events` is the append-only historical source of truth. `principal_account_bindings` is its current projection. Event versions are contiguous per binding and begin with `binding_activated` at version 1. Later events support suspension, reactivation, release, and administrative correction. Relationships, environment, time boundaries, and idempotency are constrained; UPDATE and DELETE are rejected.

The dormant `create_binding_with_initial_event()` and `append_binding_event()` operations are the supported mutation boundary. They compute a lowercase SHA-256 request fingerprint internally from every semantic command field and canonical metadata. Idempotency is scoped to `(principal_id, idempotency_key)`. An exact retry returns the original sanitized event result without changing the current projection, including after later events. Reusing the key for a changed command raises one generic conflict. Event insertion and projection update share a transaction or collision-resistant caller savepoint; failure leaves neither a partial event nor a projection change. Reconciliation recomputes fingerprints from durable event content. There is still no browser route, account authorization, claiming flow, or product integration that invokes these operations.

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

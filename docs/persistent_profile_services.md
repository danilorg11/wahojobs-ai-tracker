# Persistent Profile Services Boundary

Accounts Milestone B2B1 provides pure persistent-profile domain foundations.
Milestone B2B2 adds a dormant repository boundary that can execute those
commands only when explicitly given an eligible caller-owned connection.

## Dormant Scope

`wahojobs.persistent_profiles` contains dependency-light validators, command
models, source drafts, hashes, fingerprints, result models, and errors. Importing
the module opens no database, reads no secret, accesses no network, creates no
file, installs no schema, and changes no product state.

B2B1 does not provide:

- a SQLite repository or connection helper;
- profile create, append, read, or purge execution;
- durable idempotency lookup or concurrency control;
- row reconciliation or a reconciliation CLI;
- account/session resolution or ownership claiming;
- browser, MatchRun, matcher, pipeline, crawler, or Greenhouse integration.

## Trusted Contexts

`TrustedPrincipalContext` represents a principal already resolved by a future
trusted authorization boundary. It distinguishes an active account-native
principal with an active exclusive owner binding from a nonclaimable
development principal restricted to the `development` or `test` namespace. It
stores no email, provider subject, session, token, browser form, or legacy
alias. Its public form and representation omit principal and environment
identity.

`TrustedPrivacyAdminContext` is separate from ordinary principal eligibility.
It carries only a bounded `deletion_request` or `purge` scope and an internal
environment namespace. It does not contain administrator identity or session
material. `TrustedPersistentProfileReference` carries the internal
profile/principal/environment relationship without exposing it publicly.

These types have no request-dictionary constructors and perform no lookup. A
future trusted boundary remains responsible for constructing them from
authorized durable state.

## Migration 005 Capabilities

`PersistentProfileSchemaCapabilities` is an immutable descriptor supplied to
domain preparation. The default `MIGRATION_005_CAPABILITIES` value describes,
without live database attestation, support for:

- durable `canonical_profile_v2`;
- `confirmed_about_you_text`;
- `user_confirmed_correction`;
- `confirmed_lifecycle_action`;
- lifecycle schema `confirmed_lifecycle_action_v1`.

There is no mutable global capability state. The B2B2 repository attests its
installed schema separately and requires the applicable descriptor.

## Source Drafts

Ordinary source drafts preserve exact accepted UTF-8 bytes. Raw source text is
not NFC-normalized. Each source is limited to 1 through 32,768 bytes and follows
the installed control policy: tab, line feed, and carriage return are allowed;
other C0 controls, DEL, and C1 controls are rejected. JSON correction sources
must be JSON objects and reject duplicate keys and non-finite values. Source
ordinals, IDs, and content hashes are derived later and cannot be supplied by
the caller.

`LifecycleActionSourceDraft.for_action()` is the only lifecycle constructor. It
accepts `archive`, `reactivate`, or `deletion_request` and emits the exact
installed canonical JSON bytes for `confirmed_lifecycle_action_v1`. It accepts
no free text, reason, metadata, identity, or arbitrary JSON.

## Command Models

`CreatePersistentProfileCommand.prepare()` validates a Canonical V2 document,
generates a secure profile ID, rebinds the V2 identity internally, validates an
ordered ordinary source bundle, and derives all hashes and the request
fingerprint. At least one confirmed About You source is required. The caller
cannot supply the generated profile ID, a durable hash, or a fingerprint.

`AppendProfileRevisionCommand.prepare()` requires a trusted profile reference,
expected current revision number, complete V2 snapshot, revision kind, and
ordered source bundle. Edit and correction commands reject lifecycle sources.
Corrections require a prior revision target and a confirmed correction source.
Archive, reactivate, and deletion-request commands require exactly one matching
generated lifecycle source. B2B1 validates command coherence; the dormant B2B2
repository additionally verifies the previous durable snapshot and lifecycle.

`PurgePersistentProfileCommand.prepare()` requires a trusted privacy context
with `purge` scope, a trusted profile reference, a stable operation key, and a
stable accepted timestamp. It neither performs nor claims a purge and creates
no receipt.

## Identity and Time

Profile, revision, and source IDs use `secrets.token_hex(16)`:

- `prf_` plus 32 lowercase hexadecimal characters;
- `pvr_` plus 32 lowercase hexadecimal characters;
- `pfs_` plus 32 lowercase hexadecimal characters.

Validators reject the wrong prefix or length, uppercase/nonhex payloads,
all-zero payloads, and repeated single-character payloads. Collision callbacks
are internal, bounded to a finite attempt count, and perform no database lookup
inside B2B1. They must return an actual Boolean; malformed responses and
ordinary callback failures become detached, sanitized domain errors. IDs are
never derived from identity, content, idempotency keys, or fingerprints.

All command and source times are timezone-aware UTC values at whole-second
precision, encoded as `YYYY-MM-DDTHH:MM:SS+00:00`. Naive, non-UTC, and
subsecond timestamps are rejected. A future server boundary assigns
`accepted_at` on the first attempt and reuses it for retries; browser callers do
not control it freely.

Result timestamps use the same contract. Their shape and actual calendar/time
value are validated and must round-trip to the canonical UTC representation.

## Hash and Fingerprint Contracts

All digests are lowercase SHA-256 hexadecimal strings.

- `persistent_profile_source_content_v1`: SHA-256 over exact stored UTF-8
  source bytes, without normalization.
- `persistent_profile_structured_profile_v1`: SHA-256 over bytes from the
  accepted `canonical_profile_v2_json_bytes()` serializer.
- `persistent_profile_source_bundle_v1`: SHA-256 over deterministic canonical
  JSON containing the algorithm version and each source in semantic ordinal
  order. Each source entry includes ordinal, type, format, schema/parser
  version, confirmed timestamp, byte length, and source-content hash. Generated
  source IDs are excluded.
- `persistent_profile_request_v1`: SHA-256 over deterministic canonical JSON
  containing the operation and every semantic command field. It includes the
  principal/environment relationship, content and source-bundle hashes,
  versions, actor, reason, accepted timestamp, and versioned principal-level
  idempotency scope. Append also includes profile relationship, expected
  revision, revision kind, correction target, and derived lifecycle. Purge may
  compute this identity transiently but does not persist a receipt.

For create requests, the durable structured-profile hash contains the newly
generated profile ID, while request identity uses an ID-neutral semantic V2
hash. Thus retries that generate different candidate resource IDs retain the
same fingerprint. Append fingerprints include the existing trusted profile
relationship. Generated revision and source IDs are not command inputs.

## Idempotency

The durable scope is `(principal_id, idempotency_key)`. Creation and
append share this principal-level namespace. Keys are bounded, control-free,
and never returned publicly. `classify_replay()` is pure:

- same scope and same internally recomputed fingerprint: `exact_replay`;
- same scope and different fingerprint: `changed_conflict`, which a future
  service maps to generic `idempotency_conflict`.

Source order and `accepted_at` affect request identity. Resource IDs generated
for a create attempt do not. Validated fingerprints are compared with a
constant-time digest comparison. B2B1 performs no I/O; the B2B2 repository
performs the durable lookup before profile-existence or stale-revision checks.

## Results and Errors

Result models are immutable. Public serialization excludes principal/account
identity, opaque resource IDs, raw sources, and complete structured profiles.
Current and history content is available only through an explicit trusted
serializer opt-in. `PurgeResult` exposes only `absent_or_completed` and has no
receipt identifier.

`PersistentProfileDomainError` uses stable reason codes and bounded generic
messages. Public JSON and representations contain no rejected values, resource
identity, content, SQL, paths, secrets, sessions, or tokens. Private exception
objects and tracebacks from mapped failures are discarded rather than attached
as causes or contexts. The repository maps lock contention and expected
durable conflicts to this taxonomy without exposing SQLite exceptions.

## Dormant B2B2 Repository

`wahojobs.persistent_profiles_repository` implements the approved dormant
SQLite row services. It accepts a caller-owned `sqlite3.Connection`; it never
opens or closes that connection, installs or repairs schema, changes foreign
key mode, or imports itself into normal product runtime. Before every operation
it requires foreign keys and the exact, canonically attested Migration-005
capability and marker.

Mutations without an outer transaction use `BEGIN IMMEDIATE` and own their
commit or rollback. Within a caller transaction, the repository uses an
internally generated savepoint and releases or rolls back only that savepoint.
The caller retains control of the outer transaction and its lock mode.

Creation revalidates durable principal eligibility, checks replay before
profile existence, inserts the profile container, ordered source bundle, and
initial immutable revision, then verifies the current view and hashes. Exact
replay returns the original resource result; changed replay and different-key
duplicate creation remain distinct generic conflicts.

Append resolves the durable profile relationship and checks replay before the
expected revision. It enforces contiguous optimistic concurrency, correction
targets, lifecycle transitions, unchanged lifecycle snapshots, ordered source
bundles, and current-view verification. Ordinary writes require current durable
eligibility. A deletion request may proceed after account or binding drift when
the durable profile/principal relationship remains established.

Trusted reads return current summaries or revision history in descending
revision-number order. History uses a revision-number cursor, defaults to 25
items, permits at most 100, and enforces a serialized response bound. Current
and history reads always parse each selected durable document through the raw
Canonical-V2 boundary, require exact canonical UTF-8 storage, verify durable
profile identity and schema version, and recompute the structured-profile hash.
Omitting structured content affects only the response; it never skips these
integrity checks. A malformed selected row fails generically with
`internal_consistency_failure` rather than producing a partial page.

The inclusion option also controls result-object retention. When structured
content is omitted, the repository discards the validated content before it
constructs either a current or history result. A trusted serializer cannot
later expose content that the originating read omitted. Obtaining structured
content requires a separate trusted repository read with inclusion explicitly
enabled; public serialization still omits it by default.

History validation is page-local. A malformed revision outside the requested
revision-number page does not block the current page, but it fails when a later
page selects it. The 1 MiB limit measures the exact compact UTF-8 trusted
history array, including brackets and commas. The existing cursor is the last
revision number actually returned, so an item that does not fit is the first
candidate on the next request and is neither skipped nor consumed. Sources are
never returned. Canonical content remains behind the explicit trusted result
serializer. Reads do not start write transactions and do not represent browser
authorization. Exhaustive database-wide reconciliation, including unselected
rows, remains B2B3 work.

Purge requires a trusted privacy-admin command and a current
`deletion_requested` revision. It deletes only the profile container and relies
on installed transactional cascades, verifies complete absence, and returns
the same nonconfirming `absent_or_completed` result when already absent. It
stores no receipt, tombstone, hash, or replay record.

Repository preconditions and SQLite result codes map replay, stale revision,
lifecycle, missing relationship, schema capability, and temporary contention
to bounded domain errors. Unexpected durable or SQLite failures become a
detached `internal_consistency_failure`. Test-only failure hooks exercise every
write boundary without becoming product API behavior.

## Dormant B2B3 Reconciliation

`wahojobs.persistent_profiles_reconciliation` provides database-wide,
read-only reconciliation for the dormant Migration-005 profile rows. The
library accepts a caller-owned `sqlite3.Connection`; it never opens or closes
that connection, installs or repairs schema, writes a receipt, creates a
temporary table, changes journal or foreign-key modes, or accesses network or
secrets. It requires foreign keys and the exact committed M005 schema
attestation and capability descriptor.

When no transaction is active, reconciliation owns one deferred read
transaction for the complete scan and ends only that transaction from a
guaranteed cleanup boundary. This includes interruption immediately after the
transaction begins and interruption during attestation, scanning, or report
construction. Cleanup verifies that the owned transaction ended. If an initial
rollback is interrupted or fails, cleanup makes a bounded second rollback
attempt and may use one fixed internal `ROLLBACK` operation before checking the
postcondition again. Failures and interruptions become detached, bounded
unavailable reports without retaining caller exception text. When a caller
transaction already exists, reconciliation uses the caller's snapshot without
committing it, rolling it back, or creating a savepoint. Cleanup never touches
that caller-owned transaction. Caller reads, writes, and the decision to commit
or roll back remain untouched. The scan checks every durable profile, revision,
source, and current-view row rather than only rows selected by a current or
paginated history read.

The scan covers profile/principal relationships, environment coherence,
revision numbering and lifecycle chains, correction targets, canonical
timestamps, Canonical Profile V2 parsing and deterministic bytes, constant-time
digest comparison, source order and bundles, idempotency format and scope,
foreign-key violations, orphans, and independently derived current-view
agreement. Account or binding lifecycle drift that B2B2 deliberately permits
does not become corruption. Request fingerprints are checked for lowercase
digest format, principal scope, revision binding, and internal durable
coherence. No semantic fingerprint-mismatch finding is advertised because all
original command inputs are not retained durably and the reconciler never
invents them.

Findings are immutable and use only stable codes, the closed entity-kind set
`database`, `profile`, `revision`, `source`, and `current_view`, plus bounded
one-based profile, revision, source, or orphan ordinals. Severity is derived
from the authoritative code specification rather than supplied by a caller.
Every current finding has severity `error`, meaning a durable corruption or
contradiction that requires operator attention. No unused warning severity is
advertised. Findings never contain durable IDs, hashes, profile/source content,
SQL, paths, constraint names, free-form messages, or exception text. The stable
taxonomy is grouped as follows:

- database: `foreign_key_violation`, `row_read_failure`;
- profile: `invalid_profile_id`, `missing_principal_relationship`,
  `profile_environment_mismatch`, `duplicate_principal_profile`,
  `missing_current_revision`, `foreign_current_revision`,
  `stale_current_revision`, `profile_lifecycle_mismatch`, and
  `invalid_profile_timestamp`;
- revision: `missing_revision_history`, `orphan_revision`,
  `revision_relationship_mismatch`, `invalid_revision_id`,
  `revision_number_gap`, `duplicate_revision_number`,
  `invalid_revision_chain`, `invalid_initial_revision`,
  `unexpected_initial_revision`, `unsupported_revision_kind`,
  `invalid_lifecycle_transition`, `revision_after_deletion_request`,
  `invalid_correction_target`, and `invalid_revision_timestamp`;
- Canonical V2: `malformed_structured_profile`,
  `invalid_canonical_profile_v2`, `structured_profile_identity_mismatch`,
  `noncanonical_structured_profile`, `malformed_structured_hash`,
  `structured_hash_mismatch`, and `canonical_schema_version_mismatch`;
- sources: `invalid_source_id`, `orphan_source`,
  `source_relationship_mismatch`, `source_ordinal_gap`,
  `duplicate_source_ordinal`, `unsupported_source_type`,
  `invalid_source_for_revision_kind`, `malformed_source_payload`,
  `invalid_source_timestamp`, `malformed_source_hash`,
  `source_hash_mismatch`, `source_bundle_hash_mismatch`, and
  `source_count_mismatch`;
- idempotency: `malformed_idempotency_key`,
  `malformed_request_fingerprint`, and `idempotency_scope_conflict`;
- current view: `missing_current_view_row`, `unexpected_current_view_row`,
  `duplicate_current_view_row`, and `current_view_mismatch`.

The authoritative immutable taxonomy contains 52 codes. Forty-two are
row-reachable and have direct durable-corruption scanner regressions under the
exact M005 schema. `row_read_failure` is among them: a durable SQLite value
whose storage class cannot be decoded by the expected row contract produces a
bounded, privacy-safe per-row finding through the public reconciler while
unrelated rows continue to be checked. Ten are
`schema_unreachable_under_exact_m005`: `duplicate_principal_profile`,
`duplicate_revision_number`, `duplicate_source_ordinal`,
`idempotency_scope_conflict`, `foreign_current_revision`,
`stale_current_revision`, `profile_lifecycle_mismatch`,
`unexpected_current_view_row`, `duplicate_current_view_row`, and
`current_view_mismatch`. The first four require violating exact M005 uniqueness;
the remaining six require changing the attested current-view derivation.
Attestation rejects that schema drift before row reconciliation begins.
In particular, exact M005 cannot produce duplicate current-view rows without
changing the view or its uniqueness prerequisites. No finding is
prerequisite-only. Complete request fingerprint reconstruction remains
unavailable because all original command inputs are not durably retained. The
scanner validates only the persisted properties it can prove.

Cascade handling favors the most specific independently provable condition. A
malformed Canonical document does not produce downstream identity,
canonical-byte, or digest comparisons that require a successfully parsed
document. Source bundle comparison proceeds only when the observed source
material needed by that comparison is valid; missing source rows still permit
comparison against the observed empty bundle.

`PersistentProfileReconciliationReport` serializes as deterministic compact
UTF-8 JSON under report version
`persistent_profile_reconciliation_v1`. It reports `clean`, `findings`, or
`unavailable`, exact inventory and finding counts, truncation state, and a
bounded finding list. The default display limit is 1,000 findings, the safe
maximum is 10,000, and serialized output never exceeds 1 MiB. Summary-only
mode displays no individual findings but still scans all rows and calculates
exact totals.

The operator CLI is read-only:

```text
python -B scripts/persistent_profiles_reconcile.py --db <DB>
```

It supports `--json`, `--summary-only`, `--max-findings`, and the explicit
`--allow-workspace-db` safety acknowledgement. Invalid arguments are handled by
a sanitized parser that never echoes rejected options, values, or paths. An
invalid JSON-mode invocation emits exactly one compact unavailable object;
human mode emits only a bounded generic message. Serialization, human
rendering, and output writes are also inside detached failure boundaries. If a
normal output write fails before successful emission, the CLI makes one bounded
second write containing only a fixed JSON or generic human fallback and exits
2. If that fallback also fails, it still exits 2 without a traceback. Recovery
is not claimed for an output stream that may already have emitted partial
content.

The CLI treats `--db` only as a filesystem path. It resolves an existing file,
encodes the resolved path internally as a URI, and appends only controlled
SQLite options. Literal `%`, `%23`, `#`, `?`, spaces, non-ASCII characters,
`&`, `=`, and URI-looking names are therefore either opened as the exact
filesystem name or rejected safely by the platform; they are never interpreted
as caller-supplied SQLite options. Before and after opening and scanning, the
CLI compares resolved-path, same-file, device/inode, size, and modification-time
identity and confirms SQLite's opened main database. Workspace aliases remain
guarded.

With no sidecars present, the CLI opens the database in static immutable
read-only mode, enables foreign keys and `query_only`, and creates no WAL, SHM,
or rollback-journal files. A clean checkpointed database whose persistent mode
is WAL is detected from its SQLite header and scanned through that static
snapshot. Rollback-journal databases receive a separate read-only lock probe.
Any existing `-wal`, `-shm`, or `-journal` file is treated as temporary
contention and is neither ignored, opened, deleted, nor modified. A database
identity or timestamp change during the immutable scan discards the result as
unavailable. Exit code 0 means clean, 1 means a complete scan found findings,
and 2 means invalid input or an unavailable scan.

B2B3 performs no automatic or interactive repair, deletion, rewrite,
migration, receipt creation, legacy conversion, or backfill. It is not imported
by browser, MatchRun, matching, pipeline, Accounts, Ownership, crawler,
Greenhouse, or package startup paths.

## Future Boundary

B2B2 and B2B3 remain dormant infrastructure. No browser route, form, login/session,
OAuth, About You flow, MatchRun, account claiming, matching, pipeline, or
normal-runtime path imports or invokes them. Any repair tool, browser
persistence, or authorization cutover remains a separately designed and
reviewed milestone.

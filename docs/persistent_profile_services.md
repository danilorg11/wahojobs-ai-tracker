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
generated profile ID, while request identity uses the hash of a typed semantic
V2 projection that removes `identity.profile_id` entirely and canonically
encodes the remaining content. Thus retries that generate different candidate
resource IDs retain the same fingerprint. Append fingerprints include the
existing trusted profile relationship. Generated revision and source IDs are
not command inputs.

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

## B2C1 Read-Only Application and Browser Boundary

`wahojobs.persistent_profiles_application` orchestrates the accepted B2B2 read
contracts behind separate trusted authentication and profile-read authorization
callbacks. `wahojobs.persistent_profiles_browser` renders the resulting bounded,
browser-safe view model at `GET /account/profile`; `HEAD` is also supported.
The history page contains at most 20 revisions in deterministic newest-first
order and accepts only a strictly validated `before` revision-number cursor.
History never includes structured profile documents or source content.

This integration is disabled by default. The ordinary local-product startup
does not import or construct the application service, authentication gateway,
authorization gateway, read-only connection provider, or browser integration.
Consequently, `/account/profile` is not an active normal-runtime route. Trusted
composition code must explicitly inject a complete browser integration into the
existing handler factory. The browser and application modules know no database
path, provide no fallback actor, and do not derive identity, environment, or
authorization scope from HTTP input. Query parameters cannot select an account,
principal, or profile. A request-lifetime, redacted authentication input is
available only to the injected authentication gateway so it can validate an
external credential; it is nonserializable, is never retained by the result
model, and cannot itself select the authorized durable principal. Only the
separate authorization grant supplies that trusted principal context.

The injected connection provider owns a fresh bounded read-only connection for
each request. It must enable foreign keys and SQLite `query_only`; B2B2 continues
to enforce exact Migration-005 capability and durable profile/principal
relationships. A request is read within one transaction snapshot and rolls that
snapshot back before the provider closes the connection. No request runs
reconciliation, migration, repair, or a writable fallback.

Authorized active and archived profiles display selected Canonical Profile V2
fields, lifecycle state, current revision number, last accepted update time, and
safe revision metadata. Archived profiles are clearly read-only. For the more
restrictive deletion-requested policy, structured profile content and the
display name are hidden; only lifecycle and revision metadata remain visible.
Rendering is bounded to eight field groups and 32 values per group, and the
complete HTML response may not exceed 1 MiB; an oversized result is replaced by
a generic unavailable response rather than partially exposed.
An authorized principal without a persistent profile receives a stable empty
state that makes clear creation is not enabled and does not copy existing About
You data. Authorization denial uses a generic not-found response so the route
cannot enumerate another principal's profile.

B2C1 adds no profile creation or editing form and no POST, archive, reactivate,
deletion, correction, or purge action. It adds no login, session, OAuth,
password, account signup, ownership claiming, automatic About You or resume
persistence, MatchRun persistence, or mutation API. Authentication,
authorization, explicit mutations, and normal-runtime enablement remain separate
future milestones.

## B2C3 Dormant Durable Browser Session Authentication

`wahojobs.browser_session_authentication` provides the dormant authentication
gateway for an explicitly composed B2C1+B2C2 read path. It is disabled by
default and is not imported, constructed, or routed by normal local-product
startup. The fixed cookie name is `wahojobs_session`. The browser supplies only
one opaque token containing exactly 43 ASCII base64url characters. The complete
Cookie header is limited to 4,096 bytes, and the complete request to 64 headers.
Multiple Cookie headers, duplicate `wahojobs_session` occurrences, and whitespace
inside or around the target `wahojobs_session=<token>` assignment are rejected.
Account, identity, principal, profile, and environment identifiers are never
accepted from browser input.

The gateway derives the indexed SHA-256 lookup digest required by Migration 002,
checks the stored digest with a constant-time comparison, and validates the full
session row, rotation ancestry capped at 32 edges, active account, and complete
bounded identity inventory before issuing the sealed B2C2 actor. Root-session request
fingerprints are recomputed. Rotation fingerprints are also recomputed when the
stored idle expiry proves the requested idle lifetime was not clamped. Migration
002 does not retain the requested idle lifetime after an absolute-expiry clamp,
so that narrow replacement case can validate only the canonical fingerprint
shape and the complete rotation relationship. No alternate algorithm or
downgrade is accepted.

Exact Migration 002 schema attestation also rejects unexpected user-defined
objects that target or reference the session and rotation tables while allowing
SQLite-managed internal indexes. An active/current `account_sessions` row must
use session row version `1`. This includes an active root session and an active
replacement/current session after rotation. A revoked or rotated historical
predecessor `account_sessions` row must use session row version `2`. Any other
session row version is structurally invalid and produces sanitized authentication
unavailable.

The session row version is distinct from the rotation edge sequence/version,
token digest version, CSRF digest version, and request-fingerprint version.
Rotation edge sequence follows its own authoritative contiguous-lineage contract
and must not be interpreted as the `account_sessions` row version.
Rotation edges have no interchangeable row-version field: their structural
continuity and ancestry sequence are validated separately from session-row,
fingerprint, and digest versions.

Only sessions created at or before the injected trusted time and strictly before
both idle and absolute expiry authenticate. Equality with either expiry is
expired. Revoked and rotated sessions, inactive accounts, and sessions without
an active supporting identity are ordinary authentication failures. Malformed
durable state, schema drift, contention, and infrastructure failures produce a
generic unavailable result. Raw credentials, session identifiers, identity
details, lifecycle details, and timestamps never enter browser models or public
errors. The raw session token is never logged, rendered, serialized, persisted,
retained in actor or view models, or attached to errors.

Migration 002 sessions are globally account-scoped and contain no product
environment column. Explicit trusted composition therefore supplies the bounded
product environment used by B2C2; browser headers, cookies, queries, environment
variables, and profile state cannot select it. In durable mode, B2C3
authentication, B2C2 authorization, and B2B2 current/history reads use one
provider-owned read-only connection and one request transaction snapshot. There
is no auxiliary connection, session-table scan, last-used update, or other write.

B2C3 adds no login UI, credential collection, password support, OAuth, signup,
session creation, renewal, rotation, logout, revocation action, ownership
mutation, profile mutation, automatic About You persistence, MatchRun
persistence, migration, repair, or runtime activation. Legacy callback-based
B2C1 tests and injected-actor B2C2 tests remain separate explicit modes; durable
authentication cannot fall back to either.

## B2C2 Durable Read Authorization Boundary

`wahojobs.persistent_profile_read_authorization` provides a dormant durable
authorization gateway for the B2C1 read path. It consumes only an exact trusted
browser actor produced by a future authentication gateway. That actor carries a
stable account reference and trusted environment namespace; B2C2 does not parse,
verify, or create credentials and does not implement authentication.

Authenticated actors and durable read grants have sealed internal issuance
boundaries. Their public value constructors reject direct values, copied request
dictionaries, subclasses, and deserialization. Only trusted authentication-side
composition may issue an actor, and only a successful B2C2 durable authorization
decision may issue a `persistent_profile_read` grant. The grant has no mutation
capability. Explicit dormant legacy B2C1 tests use a separate sealed legacy grant;
durable composition cannot fall back to it.

Authorization requires an active durable account and exactly one active owner
binding in the actor's trusted environment. The bound principal must be active,
account-native, account-claimable, and exclusively account-bound, with a coherent
current binding-event lineage. The binding and principal environments must agree
exactly with the trusted actor environment. Suspended, deletion-requested, or
deactivated accounts; suspended or released bindings; non-active principals; and
development, sample, system, or legacy principals receive no browser read grant.
The Accounts schema stores accounts globally, so environment authorization is
durably scoped by the account's binding and principal rather than by a separate
account environment column.

Authorization validates the complete bounded account-identity inventory before
reading bindings. It reuses the pure Migration-002 identity-row contract for the
durable identity identifier, account relationship, provider data, email state,
canonical timestamps, disablement chronology, idempotency key, fingerprint, and
SQLite storage classes. The current identity schema has no separate identity
lifecycle, row version, updated timestamp, or metadata field. At most 16 identity
rows may be evaluated; a 17th row is an overflow sentinel and fails unavailable
without authorizing from a prefix. An active account without a supporting durable
identity also fails unavailable.

Authorization then validates complete binding, principal, and binding-event
records before evaluating eligibility. The relevant binding inventory is every
binding associated with the account, including non-owner, suspended, released,
and historical rows. Every referenced principal is resolved and fully validated.
Binding, principal, and event environments must agree, and every active binding
must satisfy M003 principal-availability rules unless the selected owner is being
classified as a structurally valid but operationally ineligible denial. Every
event lineage must be complete and current before an active owner can be selected.
M002/M003-invalid nonwinning or historical state therefore makes authorization
unavailable rather than being ignored.

Binding-event integrity uses the same pure authoritative M003 contracts as
Ownership reconciliation. Stored event fingerprints are recomputed from the
canonical event fields and metadata and compared without ordinary string
equality. Event times must satisfy M003's inclusive account-identity, principal,
binding, and prior-event creation boundaries. Principal, binding, and event
provenance is validated with the complete M003 structural and privacy policy,
not only as generic JSON. A lineage that M003 rejects for structural,
cryptographic, temporal, or provenance integrity cannot receive a durable read
grant.

At most 64 relevant account bindings may be evaluated. The query reads a 65th
row only as an overflow sentinel: 65 or more bindings fail closed as unavailable,
without truncating the inventory or authorizing from the first 64 rows. The B2C2
authorization-safety policy `MAX_AUTHORIZATION_EVENTS_PER_BINDING = 128` applies
independently to every binding. Event queries read at most 129 rows; a 129th event
fails unavailable without prefix authorization. This operational cap does not
claim that M003 considers an otherwise valid longer history corrupt. Within the
cap, missing, additional, or noncontiguous history fails closed against the
durable current event version.

Before making a decision, the gateway checks the accepted Accounts and Ownership
schema capabilities and migration markers without installing or repairing them.
Attestation uses immutable committed object fingerprints with the caller-supplied
connection. Import, gateway construction, cold authorization, and later
authorization create no auxiliary, temporary, or `:memory:` SQLite connection.
Missing, partial, weakened, or contradictory durable state produces a sanitized
unavailable outcome. Ordinary lack of authorization produces a generic denial.
The browser continues to render denial as not found and unavailable state as a
bounded temporary failure, without revealing account, principal, binding, profile,
lifecycle, or ownership details.

In explicit B2C2 composition, authentication still occurs before any database is
opened. The injected read-only connection provider then supplies one connection,
and the application begins one request-owned read snapshot. Durable authorization
and all B2B2 current/history profile reads use that same connection and snapshot.
The application rolls back only its request-owned transaction, and the provider
closes only its own connection. Direct gateway calls neither begin nor end a
caller-owned transaction.

The resulting grant has the fixed `persistent_profile_read` scope and cannot
authorize mutation. HTTP query, form, JSON, header, cookie, and path values cannot
select an account, principal, profile, environment, role, or scope. B2C2 queries
only Accounts and Ownership authorization state; B2B2 remains the sole owner of
persistent-profile SQL, Canonical Profile V2 validation, lifecycle presentation,
and history pagination.

B2C2 is disabled by default. Normal startup does not instantiate the gateway,
connection provider, authentication gateway, or browser integration, and the
`/account/profile` route remains absent. It performs no writes, migrations,
reconciliation, repair, receipt creation, login, session or cookie handling,
signup, ownership claiming or transfer, profile mutation, MatchRun persistence,
or automatic About You persistence. Session authentication and controlled runtime
composition remain separate future milestones.

## B2C4 Dormant Durable Browser Session Lifecycle Services

`wahojobs.browser_session_lifecycle` provides the dormant Migration-002
mutation boundary for creating a browser session after a future trusted
authentication decision, rotating one eligible current session, and revoking
one current session. Each operation requires sealed trusted commands; the three
command types are separate. Test issuers exist only in test support; normal
runtime has no command issuer. No browser request can invoke these mutations,
and no request field, cookie, email address, username, or provider subject can
select durable account authority.

The service accepts only an existing caller-owned SQLite connection with exact
Accounts schema attestation, foreign keys enabled, and write capability. It
opens no connection, applies no migration, runs no reconciliation or repair,
and creates no temporary or parallel state. A top-level call owns one
`BEGIN IMMEDIATE` transaction and its commit or rollback. Inside a caller
transaction it uses one collision-safe internal savepoint, releases only that
savepoint on success, and rolls back only to that savepoint on failure.
Cleanup retries a one-time rollback or savepoint failure through bounded fixed
paths and verifies the resulting transaction state. A failed top-level mutation
must leave no active transaction. A failed nested mutation must remove all
lifecycle writes while preserving the active caller transaction and unrelated
caller work. No failed lifecycle operation may leave partial state that the
caller can commit.

Creation revalidates the complete active account and eligible supporting
identity inside the mutation transaction. Rotation and revocation revalidate
the complete active account, supporting identity inventory, session structure,
lineage, ownership, current state, expiry, and optimistic session version.
Structurally malformed state fails with a sanitized unavailable result;
well-formed but ineligible account or identity state is not promoted into a
session mutation.

Creation and rotation independently generate a 32-byte opaque session secret
and a 32-byte opaque CSRF secret. Both use unpadded URL-safe Base64 and are
exactly 43 ASCII base64url characters. Only the authoritative SHA-256 hashes
are persisted. Raw issued credentials are never stored in
`IssuedBrowserSession`. That sealed result contains only an independently
generated opaque nonsecret issuance handle, nonsecret status, and safe expiry
metadata. It does not retain or reference the vault, a callback, closure, bound
method, holder, finalizer, or context manager that can reach credentials.
Ordinary recursive inspection of result fields, slots, methods, function
attributes, defaults, and closure cells cannot reach either credential.

Credentials are held only in an independently supplied, sealed,
request-scoped secret vault. No module-global registry, process-global cache,
thread-local store, filesystem, SQLite table, network service, or normal
runtime singleton holds them. The vault has bounded entries and secret bytes,
one-shot atomic removal, redacted display, no public entry API, and explicit
request cleanup that clears every unconsumed mutable buffer. Trusted response
consumption requires the exact result, the independent exact vault, the trusted
composition capability, and a canonical response time. Successful consumption
atomically removes and clears the vault entry before returning one final cookie
header and one CSRF credential. Failed or expired terminal consumption clears
the entry and cannot be retried. Results and vaults cannot be copied, pickled,
serialized, or subclassed. Secret-bearing exceptions are contained before they
cross the public lifecycle boundary, so public traceback-facing state does not
retain generated credentials. Python cannot guarantee physical memory zeroization,
so bounded lifetime and best-effort buffer clearing remain the
documented protections.

For an `issued` result, a trusted consumption attempt becomes terminal after
the exact result type, exact request-scoped vault type, exact composition
capability, and canonical response time have been validated. The sealed
response-consumption state moves from `issued` to `consumed` on success or to
`terminal_failed` on any failure after that boundary; neither terminal state
can return to `issued`. Within response consumption, `already_completed` is
reserved for a result previously consumed successfully. A terminal failure
returns only a generic failure and permanently prevents retry.

Each issued result and vault entry also share an independent opaque nonsecret per-issuance binding nonce.
It can verify that a valid handle still identifies
the result's own entry, but it cannot locate, access, or reconstruct a vault or
credential. A handle that identifies another entry therefore fails terminally
rather than consuming that entry's credentials.

A handle that is absent, unmatched, inconsistent, or incompatible with the
expected effective expiry causes immediate fail-closed cleanup. With the
correct vault, the complete request-scoped vault is cleared and closed
immediately, all mutable credential buffers are overwritten where practical,
and its entry count becomes zero before the failure returns. With an
independently supplied wrong vault, the issued result is still permanently
invalidated and that supplied vault is cleared and closed; the independently
managed original vault cannot be located through the result and is cleared by
mandatory request cleanup. No result-to-vault reference, weak reference,
callback, closure, global registry, or thread-local registry exists.
Vault close is mandatory at request completion, idempotent, and uses bounded
fail-closed cleanup if an initial cleanup attempt fails.

For a service-owned top-level create or rotation, vault deposit occurs only
after the database transaction commits. If that request-local deposit fails,
the operation returns a sanitized internal failure, no partial vault entry or
credential escapes, and the already committed but unreachable durable session
is not presented as issued or recoverable by replay. A future trusted caller
must begin a new issuance request. For a caller-owned outer transaction, the
savepoint-complete operation deposits a non-consumable pending vault entry and
returns a noncredential `pending_commit` result. After the caller commits, an
explicit trusted finalization step on the same connection verifies the exact
durable session and rotation lineage before marking the entry consumable. A
pre-commit consume or finalize fails closed; rollback followed by finalization
clears the pending entry. Request cleanup clears any abandoned pending entry.
This explicit coordination preserves nested savepoint rollback behavior without
issuing credentials for rows the caller can still roll back.

Trusted one-shot response consumption emits one `wahojobs_session` assignment with
`Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/`, coherent `Max-Age`, and
`Expires` attributes. It has no `Domain` attribute and rejects any CR or LF.
Effective session expiry is the earlier of idle expiry and absolute expiry.
`Expires` equals that effective expiry, while `Max-Age` is computed from the
canonical trusted response-composition time. A delayed response therefore
reduces `Max-Age`, and the cookie never outlives either durable deadline. If
response composition occurs at or after effective expiry, consumption returns
no cookie or CSRF credential and cannot be retried.
CSRF delivery remains an internal future-composition concern. B2C4 does not
emit a `Set-Cookie` header from any normal route.

An active/current session row uses version `1`.
A rotated or revoked historical row uses version `2`.
Rotation atomically inserts one version-`1` replacement,
marks the predecessor as version `2` with `session_rotated`, and appends the
authoritative edge. The absolute expiry is inherited. The requested idle expiry
is the earlier of the accepted rotation time plus the bounded idle TTL or the
inherited absolute expiry. Equality at the absolute boundary is therefore an
intentional clamp, while an already elapsed result is rejected. Rotation allows
at most 32 edges; a requested 33rd edge fails closed. The edge sequence remains
separate from the session row version, and validation does not invent the
non-durable requested idle TTL for a clamped historical replacement.

Trusted accepted times and injected trusted-clock values must be canonical
whole-second UTC timestamps. Future accepted times are rejected. Creation is
also rejected before credential generation when its derived idle, absolute, or
effective expiry is at or before the trusted current time. Rotation similarly
rejects an elapsed predecessor or replacement deadline before generating new
credentials. Idle TTL is
bounded from one minute through 30 days; absolute TTL is bounded from one
minute through 90 days; creation idle expiry cannot exceed absolute expiry.
Revocation changes only the selected current session to historical version `2`
and leaves unrelated sessions unchanged.

Creation and rotation reuse the authoritative Migration-002 request, session,
secret-digest, rotation-fingerprint, and lineage contracts. Durable global
creation idempotency keys make an exact repeated creation or rotation return a
sealed `already_completed` result with no issuance handle and no vault entry.
That durable mutation-replay status is a noncredential result and is distinct
from the successful-consumption completion of one issued response result.
Because raw session and CSRF credentials are never durable, replay cannot reproduce either prior raw credential,
recover a previous request vault entry,
or return replacement credentials. A changed request under the same durable key fails
as an idempotency conflict. Migration 002 has no separate revocation
idempotency-key column; revocation replay is recognized only by the exact
persisted session, accepted timestamp, reason, and expected current version.
All comparisons of reconstructible fingerprints and secret verification use
constant-time comparison.

B2C4 remains disabled and unexported from package startup. Ordinary local
startup does not import it, create sealed commands, open a database, mutate a
session, emit a cookie, or activate `/account/profile`.
It adds no login or logout UI. It adds no password or OAuth flow, signup,
automatic renewal, background rotation, revoke-all action, ownership mutation,
profile mutation, MatchRun persistence, About You persistence, migration,
repair, or runtime activation.

## B2D1 Dormant Trusted Login Completion

B2D1 provides a dormant, default-disabled completion boundary for a future
authentication-provider gateway. It consumes one sealed proof only after that
gateway has already authenticated an external identity. The proof contains
only bounded durable references, provider kind, canonical authentication and
expiry times, assurance-policy version, and the trusted product environment.
It cannot be constructed, copied, serialized, subclassed, or produced from a
browser request. Test-only issuance remains outside normal runtime. B2D1 does
not perform OAuth calls or callbacks, authorization-code exchange, password or
magic-link verification, email verification, or provider network access.

Completion accepts only an existing caller-owned database connection, an exact
sealed proof, a sealed trusted completion policy supplied independently from
that proof, an independent request-scoped secret vault, a canonical trusted
current time, and a bounded idempotency key. The policy fixes the
exact expected provider and assurance-policy version, a member of the closed trusted login
environment domain, its own bounded policy version, and the accepted session
lifetime policy. Normal runtime has no policy issuer or default instance. These
facts are never selected from a browser request, provider-controlled text, or
process environment. Completion opens no connection and performs no migration,
reconciliation, repair, account creation, identity creation, ownership change,
or profile change. The trusted environment appears independently in the proof
and sealed policy and must agree. The assertion provider and assurance-policy
version must exactly match the trusted expectations before B2C4 command
issuance; browser queries, headers, cookies, forms, JSON, environment variables,
and profile state cannot select or override those expectations.

Inside one shared immediate mutation transaction or caller-owned savepoint,
B2D1 attests the schema and validates the complete account and identity rows,
active lifecycle, exact relationship, installed provider kind, temporal
boundaries, identity eligibility, and unambiguous durable relationship. A
structurally valid but ineligible state produces only generic authentication
denial. Malformed durable state, schema failure, contention, or unexpected
failure produces only generic unavailability. Neither result discloses the
underlying reason or durable authority.

Only after every prerequisite succeeds does B2D1 create a one-use internal
validation proof and issue the sealed B2C4 creation command. B2C4 remains the
sole owner of lifetime bounds, request comparison, secret generation, durable
session mutation, and vault deposit. The accepted authentication time is the
session command time. The completion idempotency namespace binds the complete
trusted proof, expected provider and assurance policy, trusted environment,
completion-policy version, and accepted session policy while preserving B2C4's
authoritative creation comparison. An exact replay returns `already_completed`
with no new secret and no vault entry. A changed proof, policy, environment, or
account under the same caller key returns only `idempotency_conflict`.

For a top-level call, validation and B2C4 creation share one transaction; after
commit, exact B2C4 finalization makes the pending request-vault entry consumable
and the completion returns `issued`. A failed finalization receives one
bounded independent retry using the same issuance and vault entry, so a transient failure
does not create another session or credential pair. Lower B2C4 lifecycle
sanitizers do not convert `SystemExit` or `GeneratorExit` into ordinary
failures. Control-flow exceptions are not retried or converted into login
outcomes; the boundary first establishes the required safe session and vault
state and then propagates the exact exception.

If both ordinary finalization attempts fail, the exact committed session is
revoked as undelivered through B2C4 and independently verified as ineligible for
authentication. If bounded ordinary compensation fails, a separate canonical
B2C4 terminal compensation primitive selects the exact issuance from sealed
metadata, establishes durable ineligibility without invoking the ordinary
compensation implementation, and authoritatively rereads the terminal state.
No status or exception leaves an active undelivered session. No ordinary result returns
before durable session eligibility is safe and the request vault is verified
closed and has zero entries. Ordinary cleanup is bounded. If its
attempts fail, a fail-closed, idempotent emergency terminalization clears all
request-vault entries and mutable secret buffers before verifying terminal
state. Failed delivery therefore cannot leave both an active undelivered session
and reachable credentials.

For a caller-owned transaction, B2D1 returns `pending_commit` without consumable
browser material. The caller retains control of unrelated work and makes the
outer commit or rollback decision. Only a successful outer commit followed by
exact finalization enables consumption; rollback followed by finalization clears
the pending vault entry. Explicit post-commit finalization uses the same retry,
compensation, and cleanup policy without committing or rolling back caller work.
Nested transaction ownership remains with the caller. Exact replay after
rotation or revocation remains credential-free.

`already_completed` applies only to a prior successfully finalized and delivered
issuance. Credentials for a compensated or otherwise failed delivery cannot be
recovered or replayed, the original request returns no usable success, and a
new trusted login request with a new idempotency key is required.

The immutable completion result contains only a safe status and, for successful
or replay outcomes, the accepted nonsecret B2C4 issued-session result. It does
not retain the proof, durable authority, provider data, caller key, database
connection, vault, callback, exception, session token, or CSRF value. B2D1 does
not compose a browser response, emit `Set-Cookie`, register a login or logout
route, activate `/account/profile`, implement signup, or enable account and
session runtime. The future provider gateway, browser response composition, and
login routes remain separate reviewable milestones.

## Dormant Google OIDC Gateway

The Google OIDC gateway is a dormant, default-disabled bridge into the accepted
B2D1 completion boundary. Its initial application platform is CPython 3.12 on
Windows x86-64, and Google is the only supported provider. The reviewed runtime
dependency boundary is the exact hash-locked Authlib 1.7.2, joserfc 1.7.4, and
Requests 2.34.2 stack, with cryptography 49.0.0 promoted from that unchanged
closure as the direct storage-protection dependency. Those libraries own the
standard OAuth, OpenID Connect, JOSE, HTTPS, and AES-GCM primitive work;
Wahojobs owns only the narrow transaction, freshness, bounded-transport,
verified-identity projection, durable-resolution, and failure policies around
that work.

Authorization requires one-use state and nonce values, PKCE S256, an accepted
Google issuer, one exact audience, an exact `azp` value when present, an RS256
signature, and valid expiry and `iat` values. A signed `auth_time` value is
mandatory, may be at most 86,400 seconds old, and is subject to the approved
60-second clock-skew policy. Deployment therefore requires Google session-age
support that supplies `auth_time`, in addition to production credentials and
redirect registration.

Callback parameter names and values are structurally percent-decoded and
strictly UTF-8 validated before provider exchange. Invalid escapes, invalid or
replacement text, and decoded control characters fail closed. Only a canonical
callback rebuilt from the exact validated, unique, allowed fields reaches
Authlib; the original raw query is never reparsed downstream.

Google authorization success responses require exactly one each of `state`,
`code`, and RFC 9207 `iss`. Redirected Google error responses require exactly
one each of `state`, `error`, and `iss`, and may contain only the existing
optional `error_description` and `error_uri` fields. The response `iss` must
equal the pinned modern Google issuer `https://accounts.google.com` exactly;
the callback does not normalize issuer URLs, accept the legacy bare issuer, or
ignore additional parameters. The strict response shape and issuer are checked
before durable state lookup, transaction claim, or provider exchange. This
authorization-response check is separate from the later signed ID-token `iss`
claim validation, whose modern and legacy issuer contract is unchanged.

Token and JWKS responses accept only identity content, including the documented
absent-encoding form, or the reviewed gzip and deflate encodings. Encoding names
are case-insensitive, but unknown, malformed, duplicated, multiple, or
conflicting declarations fail closed before response content reaches Authlib or
joserfc. The 64 KiB token and 256 KiB JWKS limits count actual decoded bytes.
Response resources close on every accepted and rejected path, including
decoding, read, and size failures.

Gateway construction accepts only deployment client values and always installs
the fixed real Google adapter; it accepts no provider, claims, projection,
decoder, key set, transport, or clock authority. The resulting configuration is
gateway-owned rather than caller-supplied. Each authorization transaction
belongs to the exact gateway and configuration that created it. After provider
verification and durable lookup, one synchronized boundary rechecks the exact
invocation owner and expiry, atomically consumes the transaction, clears its
state, nonce, PKCE, callback, and request buffers, and extracts one internal
delegation value before proof issuance or B2D1 begins. A close or expiry before
that boundary prevents delegation; after it succeeds, close or clock movement
cannot revoke the one committed winner. B2D1 denial or failure never makes the
transaction reusable.

Provider-key freshness state also remains instance-local. Concurrent callbacks
record the cache generation they decoded. When one callback installs a newer
valid JWKS generation, other callbacks reuse that generation without another
fetch or a refresh-rate denial. A per-instance refresh flight admits only one
required network refresh, wakes waiters on failure, preserves the prior valid
cache when refresh fails, and never permits stale keys. There is no persistent
or process-global transaction registry, key cache, background refresh, or
startup fetch.

After provider verification, the gateway performs an exact read-only lookup of
one existing Google identity and its active owning account. The provider's
opaque, case-sensitive subject is the identity authority; an email address is
never an identity key or a source of authority. Missing or ineligible durable
state produces generic authentication denial, while duplicate, malformed,
ambiguous, schema-invalid, or infrastructurally inconsistent state produces
generic unavailability. The gateway creates, links, and updates nothing.

Successful verification and durable resolution delegate directly to B2D1 using
the transaction-owned request identity and the caller-supplied accepted
completion boundaries. The exact B2D1 result is returned unchanged. Failures
before delegation are limited to bounded, non-enumerating outcomes for
authentication denial, provider unavailability, an invalid or expired
transaction, or general unavailability; they do not disclose provider or
durable-account details.

Acceptance tests are network-free and run with sockets blocked. Deterministic
local HTTP responses supply signed tokens and JWKS documents, but every
successful outcome still traverses the real Authlib and joserfc exchange,
signature, and claim-validation path. No test provider or asserted verified
projection is accepted by production code. If `KeyboardInterrupt`,
`SystemExit`, or `GeneratorExit` interrupts exchange, validation, durable
resolution, proof issuance, or B2D1, the active transaction is consumed and the
gateway fail-closes: credentials, provider adapter, and cached key authority are
cleared before the exact exception propagates. Ordinary mapped application
failures leave the gateway reusable.

The gateway remains isolated from ordinary startup and package activation:
importing normal runtime modules does not prepare authorization, contact Google,
open a database, or register a handler. Login and callback routes, transaction
composition, browser-response composition, cookies, CSRF delivery, production
credentials and keys, and runtime activation all remain deferred deployment
work.

Any dependency refresh requires a new review of the complete pinned and hashed
closure for the target platform, a wheel-only installation check, protocol
probes, and a fresh security-advisory query. The accepted review established
publishing provenance for the direct Authlib, joserfc, and Requests artifacts,
but did not establish equivalent retained provenance evidence for the selected
transitive cffi and pycparser artifacts. That accepted provenance gap remains a
required review item on every dependency refresh. Review the lock at least
quarterly and immediately after a relevant security advisory, upstream
security or repository-ownership change, required interpreter or platform
change, or proposed package-version change.

## Dormant Durable Google OIDC Authorization Transactions

Migration 006 defines an optional restart-safe authorization-transaction
boundary without changing the existing in-memory gateway API. The migration is
not installed by ordinary startup and remains uninstalled in the workspace
database. Durable use is a separate composition path that requires an already
migrated caller-owned connection and an externally established key authority.
Importing or constructing the components opens no database, reads no deployment
secret, starts no worker, and performs no provider request.

Preparation generates fresh state, nonce, PKCE verifier, and B2D1 request key
material once. It derives privacy-preserving state lookup values with
domain-separated HMAC-SHA-256 and protects the canonical secret material with
AES-256-GCM. Associated data binds the immutable transaction, configuration,
key-version, and chronology facts. A short SQLite immediate transaction
reattests the exact schema, inserts and rereads one prepared row, and commits
before the authorization URL becomes available. A failed encryption, collision,
insert, verification, or commit exposes no usable URL; a retry starts with
entirely fresh material.

The repository connection contract requires an idle concrete SQLite connection
with foreign-key and recursive-trigger enforcement enabled. Every mutating
operation establishes and verifies those settings before work and reattests them
after acquiring its own immediate write boundary, so caller setting changes
cannot silently weaken a later operation. The schema admits only an initial
prepared row at row version `1`; its insert, update, and delete guards reject
direct terminal insertion, replacement of an existing prepared or terminal row,
immutable-field changes, and deletion of a prepared row. Only the exact
prepared-to-terminal update and bounded deletion of an already-terminal row are
accepted.

Callback handling validates and extracts the exact state before provider work.
A short one-winner SQLite claim locates the row through accepted keyed-digest
versions and irreversibly changes it from prepared to consumed, expired, or
invalidated. The claim is authoritatively reread and committed before protected
material is decrypted. Replay therefore stops before token exchange, and a
provider or downstream completion failure cannot make the transaction reusable.
Expiry equality and wall-clock rollback fail closed.

After that terminal commit, the process-reconstructed gateway decrypts the
claimed one-use capsule, traverses the same real Authlib and joserfc verification
path, performs the same exact durable Google-subject lookup, and delegates the
stored request key to the unchanged B2D1 and B2C4 boundaries. No SQLite write
transaction or authorization-transaction lock spans provider exchange, durable
identity resolution, or trusted login completion.

The externally supplied key authority accepts small versioned lookup and
protection key rings, designates one active version of each kind, supports
bounded rotation while retained versions can read older transactions, consumes
caller-owned mutable key buffers, and clears its copies on explicit close. It
contains no environment loader, production key, registry, or fallback key.
Rejected supported container shapes are inspected only through bounded built-in
container access so reachable caller-owned mutable key buffers are also cleared
without invoking caller iteration or property code. Ordinary protection and
repository failures cross a sanitized boundary only after sensitive inner
frames, aliases, and exception links have been detached.

Cleanup is explicit and bounded, and it has no implicit deployment retention
default. Every call must supply `terminal_retention_seconds` as an exact
built-in integer from 1 through 31,536,000 seconds (one second through 365
days). This is the supported timestamp-arithmetic and operational envelope, not
a selected product policy; each deployment must deliberately select its value.
Authorization expiry and terminal retention are distinct. Prepared expiry is
the transition at or after the transaction's ten-minute authorization expiry.
Terminal retention starts at the lifecycle-appropriate terminal timestamp.
Terminal deletion is permitted only when that timestamp is no later than the
trusted cleanup time minus the supplied retention duration.

One short immediate transaction establishes an action-start snapshot before
mutation. It validates at most 4,000 transaction rows, fetching only one
additional row to prove the inspection boundary was exceeded. A truncated
snapshot is reported incomplete and performs no expiry or deletion. A complete
snapshot may first expire structurally valid prepared rows and then delete only
rows that were already terminal at action start; it never expires and deletes
the same row in one pass. Expiry and deletion continue to share the caller's
exact mutation limit of 1 through 1,000.

Terminal deletion additionally requires the complete reconciliation structural
row contract, lifecycle-specific chronology, a nonfuture terminal timestamp,
accepted lookup and protection versions from one immutable authority snapshot,
and no contradictory protected-material reuse in the bounded complete
snapshot. Malformed, future-dated, too-recent, contradictory, unknown-version,
or retired-version rows remain stored for reconciliation and investigation.
Cleanup never loads or trials their keys and never repairs or normalizes them.
Its sealed result exposes only bounded counts for prepared expiry, terminal
deletion, terminal candidates inspected, each sanitized skip category, known
remaining work, exactness, completeness, truncation, and commit outcome. It
contains no transaction identifier, timestamp, digest, nonce, or protected
value.

Reconciliation remains strictly read-only, does not decrypt protected
material, and reports bounded sanitized ordinals rather than transaction
identifiers, digests, ciphertext, or row-associated key metadata. Cleanup
retention and deletion policy is separate from reconciliation execution.

One repository-owned reconciliation budget is shared by the domain result,
implementation, renderers, CLI, and tests. It permits at most 1,000 transaction
rows, fetches only one additional row to prove that rows remain, retains at
most 1,000 findings, and permits at most 524,288 UTF-8 output bytes including
the final newline. When that additional row exists, reconciliation reports the
known overflow and does not semantically inspect an arbitrary bounded subset.
The same operation object also caps all SQL result and
finding work at 64,000 items, coherent snapshot copying at 8,192 aggregate
SQLite pages and 16,400 backup callbacks, and private authorizer and progress
work at 100,000 calls each. These counters span the whole operation rather than
resetting per table, finding kind, renderer, or nested schema-analysis
connection. Existing narrower prerequisite attestation bounds remain nested
underneath this aggregate boundary and propagate exhaustion to the same
blocking incomplete result. Exact
limits are complete; observing the additional transaction row or exhausting
any scan, schema, foreign-key, duplicate, finding-retention, snapshot, or output
budget produces a blocking `incomplete` result and can never produce `clean`.

Reports distinguish rows observed and inspected, structurally valid and invalid
rows, exact omitted rows or an explicitly unknown total with one row known to
remain, retained and omitted findings, and row-scan, finding-retention, and
output-rendering truncation. Human and JSON output project the same counters and
flags. Both are assembled only from bounded fields, include their final newline
in the shared byte check, and fall back as a whole to a valid minimal blocking
summary instead of cutting a finding or emitting oversized output. The CLI
opens its already sidecar-free target with SQLite immutable read-only semantics
and writes the checked UTF-8 bytes through the binary standard-output boundary.
It
returns success only for a complete, nonblocking scoped-clean report and returns
its unavailable exit for every incomplete result.

The public reconciler executes no SQL on the supplied connection. An idle exact
standard-library SQLite connection is accepted only when the caller explicitly
establishes that reading it cannot create filesystem sidecars. In-memory,
immutable read-only, and verified non-WAL sources can satisfy that precondition;
an undeclared arbitrary connection returns generic unavailable before backup.
This is required because Python exposes no callback-free journal-mode or
database-path query on an existing connection. An accepted source is copied
with bounded one-page backup steps
into fresh private in-memory connections; main is copied between two TEMP
copies, and unequal TEMP images fail closed. Any source TEMP schema object is
conservatively unavailable because Python cannot import a TEMP database image
into another connection without reconstruction. Active caller transactions
also return generic unavailable before copying. Inspection runs only on the
fresh callback-free main snapshot with query-only mode, a default-deny
read-only authorizer, and a bounded private progress handler. Caller trace,
progress, authorizer, factory, collation, function, adapter, converter,
transaction, and lifetime state is neither invoked nor changed.

Within the bounded canonical row set, findings use a fixed category, severity,
and code order plus private canonical row metadata, never rowid, insertion
order, query-plan order, Python hashes, or a public identifier. Reconciliation
detects exact reuse of contract-valid protected material, contract-valid raw
nonces, and exact
nonce/material reuse across distinct rows, and compares every stored
associated-data input plus lifecycle, row-version, storage-class, nonce-length,
and protected-length metadata. Contradictory copies are blocking and remain
sanitized. The declared integrity scope is
`structural_and_exact_reuse_without_cryptographic_authentication`;
`cryptographic_authenticity_verified` and `runtime_safety_established` are
always false. A scoped `clean` diagnostic therefore supplies neither AEAD
authentication evidence nor authority for a runtime login or safety decision.

The guarded migration and reconciliation commands canonicalize the requested
filesystem target, use correctly escaped read-only SQLite URIs, and verify
SQLite's authoritative main-database identity after every open. The exact
Migration-001 through Migration-005 attestations are supplemented by one
Migration-006 prerequisite-closure boundary: it enumerates both main and temp,
derives the reserved Migration-001 and Migration-003 ownership namespaces from
their authoritative SQL and committed manifests, compares every identifier
with SQLite's ASCII-only case equivalence, and resolves unexpected view
dependencies through actual isolated SQLite read-authorizer events, including
columnless row-set reads, rather than SQL-text matching. One aggregate budget
is shared by main and temp object, view, column, SQL-byte, authorizer-call, and
EXPLAIN-row inspection. Exact limits are accepted; any excess or uninspectable
dependency fails closed.

For an explicitly authorized workspace migration, backup evidence is checked
once as an early refusal and then checked authoritatively again after the
migration owns an immediate write lock, together with sidecars, prerequisite
schemas, markers, integrity, foreign keys, preserved counts, and preserved
objects. After every possible pre-migration connection mutation, the
migration-owned connection serializes its live main image and requires its
length and SHA-256 to equal the reverified exact-copy backup. Before that final
serialization, the connection is reduced to exact standard-library factories,
its trace and progress callbacks are disabled, its authorizer is replaced by a
bounded phase-aware migration authorizer, and its executable binary collation
is reset to SQLite's built-in implementation. The sealed path uses no adapted
bound values or converter-eligible results. That live-image comparison is
immediately followed by the first Migration-006 statement; the same seal
permits only the eight pinned DDL operations, the fixed marker insert, bounded
verification, and commit or rollback. A failure before commit reports no
change; a failure to reopen or verify after a successful commit reports that
the durable change occurred while verification failed and never claims
rollback.

The deterministic configuration binding covers the same complete canonical
durable-context document accepted by the gateway, bounded to 8192 bytes. This
includes an otherwise valid redirect URI at the gateway's existing 2048
character maximum without widening that in-memory limit.

No route, redirect handler, cookie or CSRF delivery, signup, account or profile
provisioning, production key integration, scheduler, cleanup worker, or runtime
activation is included. Those deployment responsibilities remain separate
reviewable work.

## Future Boundary

B2B2, B2B3, B2C1, and B2C2 remain explicitly controlled infrastructure. No
login/session, OAuth, About You flow, MatchRun, account claiming, matching,
pipeline, or default normal-runtime path invokes persistent-profile reads. Any
mutation, repair, persistence, session integration, or authorization cutover
remains a separately designed and reviewed milestone.

## Later dedicated browser activation

The dormant and future-boundary language above continues to describe the
default normal-runtime path. A later reviewed milestone activates a narrow
composition only through the dedicated local HTTPS command
`scripts/durable_google_login_app.py --config <ABSOLUTE_CONFIG_PATH>`.
Ordinary `scripts/local_product_app.py` invocation remains
authentication-dormant; its generic injection seam does not construct a
runtime, inspect configuration or environment variables, open the account
database, load secrets, contact a provider, or activate protected routes.

The dedicated composition owns `/login`, `/auth/google/start`,
`/auth/google/callback`, `/logout`, `/account/profile`, and `/find-matches`
without falling through to unrelated product routes. The authenticated matches
integration owns one bounded process-local `MatchRunRegistry` for no-profile
candidate entry, structured review/draft correction, and explicit confirmation.
Every GET, HEAD, and POST authenticates the durable session and authorizes the
account-native owner. Draft state carries a server-only binding to the account,
environment, principal, and session, so a capability from another authority
cannot select or disclose it. The configured Host/no-proxy guard precedes
rendering and body reads; POST then preserves same-origin, CSRF, strict framing,
bounded single-read decoding, duplicate-field, and unsupported-field rejection.
The durable launcher does not mount `scripts/local_product_app.py` wholesale;
that script remains a local/development tool, while selected pure matching and
presentation helpers are reused. It reuses B2C1 for both an
authenticated owner's existing persistent profile and the accepted no-profile
result.
Authentication uses a fresh read-only connection or snapshot, the accepted
strict `wahojobs_session` parser, and ownership authorization. A canonical
active account with a valid account-native lineage but no profile row receives a
fixed authenticated empty page; no profile or ownership row is synthesized.
Authenticated rendering includes a `Create profile` or `Find matches` action
and otherwise fixed safe profile and logout navigation;
unauthenticated rendering offers `/login`. Refresh and dedicated-runtime
reconstruction preserve access, while refresh after a successful logout again
requires authentication. Existing CSP and escaping remain in force.

The later B2.4d composition adds one explicit create-once mutation without
changing those reads. Review state is a typed immutable identity-free Canonical
V1 projection that omits every `profile_id` occurrence rather than supplying a
preview, sentinel, null, empty, or reserved identity. Its canonical encoding is
UTF-8 JSON with `ensure_ascii=True`, sorted keys, compact separators, and NaN
disabled. The browser receives only the lowercase 64-hex SHA-256 digest of those
bytes as `profile_draft_fingerprint`, never the complete JSON. A private server
callback authenticates the session, resolves the account-native owner without
bootstrap, and issues an immutable process-local artifact. The artifact is
bound to the account, session, environment, exact ownership-event lineage,
purpose, content fingerprint, accepted time, and stable server idempotency key.
The strict confirmation dispatcher invokes that callback only after the
existing review token, digest, schema, field, and explicit-confirmation checks.
It and the later profile POST use the same creation service and vault. There is
no issuance HTTP endpoint or durable artifact table.

The exact original D0 confirmation identity has the bounded lifecycle `DRAFT`,
`ISSUING`, `MAYBE_ISSUED`, or `COMPLETED`. The registry admits at most one
issuance owner and every ownership wait is finite. It releases the registry lock
before invoking a sink or callback. A definite pre-artifact failure restores D0
exactly. An outcome that may have published an artifact enters
`MAYBE_ISSUED`; an exact resubmission reauthenticates and performs only a vault
lookup for that confirmation and binding, never another issuance. A valid offer
is stored as `COMPLETED` before HTTP response delivery, so a failed response
write leaves the original form able to request the same offer. The registry
observes the immutable completed record and its durable binding under lock,
releases the lock for real lookup-only durable session/account/CSRF/ownership
authorization, and returns the cached offer only after the identical record and
binding are rechecked under lock. This replay invokes neither issuance nor
durable mutation, and its authentication failure does not destroy a completion
that another authorized request can replay. Exact concurrent submissions
converge after authorization. The request binding first applies the full strict header and cookie
validation used by the protected profile boundary, then hashes only the trusted
Host and the two validated security-cookie values. This keeps reordered or
unrelated cookies stable without letting malformed cached retries bypass
authentication validation. `MAYBE_ISSUED` and `COMPLETED` retain the run for no
more than 600 non-sliding seconds from the outcome; expiry deletes both state and
run, making the old D0 gone instead of issuable again. Changed and genuinely
stale drafts remain rejected.

`POST /account/profile` accepts only the exact artifact reference and an
artifact-bound, purpose-separated proof derived from the already validated
session-CSRF secret. A distinct mutation grant supplies the canonical trusted
principal; the browser cannot submit an account, owner, profile, provenance,
source, version, timestamp, reason, actor, or idempotency value. The server
revalidates the typed identity-free review projection. The creation service
constructs immutable source ordinal 1 containing the exact confirmed About You
text plus optional source ordinal 2 containing deterministic compact
confirmed-correction JSON.
`CreatePersistentProfileCommand.prepare()` is the sole durable profile-identity
authority: it generates and binds the profile ID once, supplies that same ID to
the private V1-to-V2 conversion builder, and validates and seals the converted
profile and source drafts. Conversion has no independent generator, selected
profile identity, or placeholder. Raw
source content is not embedded in Canonical V2.

The complete create command is prepared once before artifact publication. Its
profile, revision, and ordered source identities, exact source bytes and hashes,
structured and semantic fingerprints, versions, timestamps, actor, reason,
idempotency, bundle hash, and request fingerprint are reused on every attempt.
The structured hash covers full durable Canonical V2 bytes. The semantic hash
covers a typed V2 projection with `identity.profile_id` removed. Each
source-content hash covers exact source UTF-8 bytes; the bundle hash covers the
versioned canonical ordered-source manifest; and the request fingerprint covers
the versioned canonical request envelope containing the semantic and bundle
hashes. The artifact content fingerprint is distinct: its canonical envelope
seals account/session/environment/purpose and ownership lineage, the
identity-free review and source hashes, accepted times, server idempotency, and
the already prepared command IDs and hashes.

Under the trusted, finite, non-reentrant internal-callback contract, vault-owned
callback execution retains one cleanup/terminalization owner for each immutable
in-flight token until a bounded one-shot handoff verifies an exact
owner-and-token transition across ordinary production failures. This does not
claim recovery from arbitrary instruction-boundary exception injection. A proved pre-commit rollback releases
that exact claim; an
uncertain commit retains reconciliation state for exact replay. A repository
call that commits but returns a malformed or locally unvalidated result shape is
also retained for reconciliation and reuses the same prepared command; definite
pre-invocation content or authority failures retain their existing retirement
behavior. Created, conflict, and active
in-flight records all count toward the 64-record, non-sliding 600-second
capacity. Active in-flight records are not evicted merely because their deadline
arrives.

The vault and its claim-cleanup coordinator are constructed dormant. Runtime
composition constructs the service and profile integration, registers that
outer integration with the process cleanup authority, and only then starts the
coordinator's non-daemon worker. Close stops admission and applies a finite
two-second close/join bound to the worker and its owned probes. A live worker
keeps cleanup truthfully unresolved and a later close retries it. Neither
startup nor cleanup relies on daemon exit, finalizers, garbage collection, or
process termination. Earlier statements in this document that a dormant
authorization-transaction component starts no worker remain scoped to that
component, not to this activated profile-artifact vault.

The launcher explicitly validates the private
`issue_confirmed_profile_artifact` capability and its paired lookup-only
completed-replay authenticator for the profile-creation composition. It also
requires the durable browser integration to claim `/find-matches` before binding
the production handler. Routing-only test doubles remain possible only in their
explicit compatibility mode; an absent route capability fails closed and never
activates the local matcher or product handler.

`PersistentProfileRepository` remains the sole durable create authority; the
account-native composition uses its sealed-lineage create entrypoint while the
accepted generic create contract remains unchanged. One success adds one
`product_profiles` row, one initial
`product_profile_revisions` row, and one or two complete
`product_profile_sources` rows; `current_product_profiles` derives its result
from those rows. Account, identity, invitation, session, ownership, event,
alias, legacy, and unrelated state remains unchanged. Same-artifact retries
reuse the stable request and return the original success while its terminal
record remains live; another logical create returns conflict, and concurrent
first creates converge through repository uniqueness. GET and HEAD still never
create, link, seed, repair, claim, touch a session, or perform another write.

The account-native composition enters that repository through its sealed
lineage-required create boundary. Inside the same `BEGIN IMMEDIATE` transaction
and before replay or insert, it requires the original active account and row
version, canonical principal and environment, exact owner binding and version,
sole active ownership, and the complete unchanged ordered event lineage. An
account transfer or replacement therefore fails closed before any profile row
can be attributed to the new owner.

The accepted B2.4d profile creation, immutable artifact, authenticated replay,
reconciliation, and create-once contracts remain frozen. Successful creation now
returns `303` to `/find-matches`; exact replay still converges on the original
single profile, initial revision, and source set.

The later authenticated profile-to-matches composition reads the authorized
current Canonical Profile V2 on a query-only connection and projects it with
`project_v2_to_matcher_v1()` using a fresh server-generated ephemeral matcher
identity. The persistent profile identity is never a browser-selected matcher
identifier. Opportunity rows come only from the durable runtime's explicitly
configured, schema-attested SQLite database through the same query-only provider;
the checked-in metadata overlay is also constructed explicitly. There is no
fallback to `wahojobs.config.DB_PATH`, the workspace database, legacy
`user_profiles`, `local_user`, demo state, or local pipeline data.

Match regeneration is read-only and retains the existing inventory filtering,
ranking, trust, exclusion, section, and result-cap rules. Empty or insufficiently
trusted configured inventory produces an honest empty/refresh-needed page.
Only validated HTTP(S) opportunity destinations are linked, with isolated
new-tab behavior. Durable navigation is limited to profile, logout, and those
destinations; My Jobs, tracker, dashboards, `/action`, preview aliases, demo
personas, legacy selectors, and mutation forms are not rendered. Creation and
correction drafts and confirmed artifacts remain bounded and process-local;
match results are not persisted.

### Bounded durable profile-correction composition

The existing-profile `GET /account/profile` page continues to render the
authorized current profile and `Find matches`, and now also offers one clear
`Update profile` action on that same route. Correction begins from the complete
current Canonical Profile V2 returned by the accepted query-only authorized
read. The server projects that trusted snapshot into an identity-free review
draft and reuses the accepted draft/review/correct/confirm machinery. Browser
fields are untrusted correction input and are never accepted as an authoritative
V2 document or as profile, principal, revision, provenance, or source identity.

Correction drafts and immutable confirmation artifacts are distinct from the
B2.4d creation purpose and remain bounded, expiring, and process-local. Their
server-private binding covers the authenticated account, environment,
account-native principal, session, existing profile, exact base/current
revision, and correction purpose. The browser receives only opaque protected
references and purpose-bound proofs. Cross-account, cross-session, expired,
tampered, wrong-purpose, and restart-lost pending, uncommitted state fails
without disclosure or partial durable mutation. A successfully committed
artifact remains eligible for authenticated exact durable replay after
process-local expiry or runtime reconstruction.

Confirmation rebuilds and validates one complete corrected Canonical V2
snapshot while retaining the same server-authorized persistent profile ID. It
uses the shared reviewed-profile source-bundle builder, always includes the
confirmed correction source, and prepares the existing
`AppendProfileRevisionCommand` with revision kind `correction`, the exact
expected current revision number, the exact prior revision as correction
target, and a stable server-derived idempotency key. The accepted
`append_profile_revision` path atomically appends exactly one immutable revision
and its complete ordered sources and advances the current view. It does not
replace an earlier revision, rewrite the profile container or ownership
lineage, or persist Canonical V1.

The correction source bundle retains the complete server-normalized review
semantics under the installed 32,768-byte-per-source and 16-source limits. The
ordinary case remains one byte-identical correction source. When that canonical
JSON is larger, the correction-only builder deterministically divides it into
at most 15 ordered, hash- and length-bound JSON fragments while the confirmed
About You source remains ordinal 1. Reassembly yields the exact ordinary JSON,
and changed V2 provenance is bound server-side to every correction-source
ordinal. This partition mode is not enabled for the frozen B2.4d creation path.

Exact replay converges on the accepted append, including after runtime
reconstruction. Changed replay conflicts generically. A stale base or two
competing corrections cannot overwrite the newly current revision; the losing
append leaves durable material unchanged. Success returns `303` to
`/find-matches`, which performs a fresh authorized read of the newly current V2
and the already accepted ephemeral V1 matcher projection against only the
configured inventory. B2.4d creation and Authenticated Profile-to-Matches remain
frozen dependencies.

Correction-vault shutdown first rejects new consume operations and keeps a
vault-owned operation token for the entire admitted path, including a missing
local artifact's durable-replay lookup and append. Close waits with a finite
bound. A timeout preserves records and leaves cleanup unresolved for a later
retry; a successful close guarantees that no admitted correction append can
commit afterward.

Arbitrary profile replacement, additional edit kinds, archive, reactivate,
purge and deletion UI, revision-history UI, general onboarding, legacy
claiming, automatic migration, public signup or deployment, scheduled refresh,
My Jobs, tracker/pipeline behavior, and automatic key rotation remain outside
this composition. The ordinary local-product runtime is unchanged.

B2.4d is an application milestone, not a formally verified crash-resilient
state machine. It does not promise transparent in-process recovery when a
trusted internal sink or callback stays blocked forever, asynchronous-exception
recovery at every Python instruction boundary, or formal shutdown when trusted
internal code holds a lock forever. Process restart or a new browser flow is an
accepted operational recovery for such process-local stalls. Cryptographic run
ID collision is outside the practical threat model, and the confirmation cache
may outlive an artifact by the small interval between their clock samples;
artifact consumption remains authoritative. Existing legacy synthetic profile
construction outside this account-native graph remains deferred. The bounded
durable correction flow above does not broaden those recovery claims or activate
any later profile operation.

Callback handling first parses the accepted callback state and terminally
claims the durable authorization transaction. Only after that transaction is
terminal does it compare the claimed transaction identity with the
`__Host-wahojobs_google_tx` browser cookie in constant time. A missing,
malformed, or swapped binding therefore consumes the transaction and stops
before provider traffic, identity lookup, proof issuance, B2D1, or session
creation. No authorization-transaction write lock spans browser binding,
provider verification, identity lookup, B2D1, response construction, or
delivery.

An exact `issued` B2D1 result is delivered through the sealed one-shot session
delivery lease. Header validation, `send_response()`, `send_header()`, or any
ordinary or control-flow failure before successful
`BaseHTTPRequestHandler.end_headers()` compensates the newly created session.
After `end_headers()` succeeds, the response acknowledges the lease and erases
its retained compensation metadata; later body or socket ambiguity does not
compensate. This is only a server-side delivery boundary and is not evidence
that the browser received or persisted a cookie.

See `docs/durable_google_login_browser.md` for the exact route, cookie,
configuration, controlled-fixture, and deployment contracts.

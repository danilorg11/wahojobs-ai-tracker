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

## Future Boundary

B2B2 remains dormant infrastructure. No browser route, form, login/session,
OAuth, About You flow, MatchRun, account claiming, matching, pipeline, or
normal-runtime path imports or invokes it. B2B3 row reconciliation and any
future browser persistence or authorization cutover remain separately designed
and reviewed milestones.

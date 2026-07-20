# Persistent Profile Services Boundary

Accounts Milestone B2B1 provides pure persistent-profile domain foundations.
It prepares validated, immutable commands for a future trusted repository
boundary, but it does not read or write persistent-profile rows.

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

There is no mutable global capability state. A future repository boundary must
attest its installed schema separately and pass the applicable descriptor.

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
generated lifecycle source. B2B1 can validate command coherence but cannot
verify the previous durable snapshot or lifecycle without the future
repository.

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

The future durable scope is `(principal_id, idempotency_key)`. Creation and
append share this principal-level namespace. Keys are bounded, control-free,
and never returned publicly. `classify_replay()` is pure:

- same scope and same internally recomputed fingerprint: `exact_replay`;
- same scope and different fingerprint: `changed_conflict`, which a future
  service maps to generic `idempotency_conflict`.

Source order and `accepted_at` affect request identity. Resource IDs generated
for a create attempt do not. Validated fingerprints are compared with a
constant-time digest comparison. B2B1 performs no durable replay lookup.

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
as causes or contexts. SQLite-specific mapping is future repository work.

## Future Boundary

B2B2 remains future work. It may add separately audited create, append, read,
and controlled purge repository services. Row reconciliation, browser
persistence, authorization cutover, and MatchRun persistence remain later,
separately reviewed milestones.

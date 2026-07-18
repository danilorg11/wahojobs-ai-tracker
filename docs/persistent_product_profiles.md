# Dormant Persistent Product Profiles

Accounts Milestone B2A defines durable profile storage without activating it in the product. Migration 004 is committed infrastructure only. It is not installed by normal runtime startup, does not persist MatchRuns, and is not applied to the workspace database as part of B2A.

## Identity boundaries

Wahojobs keeps six concepts separate:

- An account is an authenticated `users.user_id` from Migration 002.
- A product principal is the future owner and authorization subject from Migration 003.
- A persistent product profile is a resource owned by exactly one principal.
- A profile revision is one immutable, complete structured-profile and lifecycle snapshot.
- A profile source is one exact, confirmed input accepted for a revision.
- Legacy `user_profiles.profile_id` values remain sample, development, and legacy pipeline-owner identifiers. They are not persistent profile IDs or principals.

B2 supports one core persistent profile per principal. Profiles cannot be shared or transferred. Sample, system, and legacy principals cannot own persistent profiles. An active account-native principal requires an active exclusive owner binding. A nonclaimable development principal is allowed only in `development` or `test` environments.

Browser callers never supply a trusted principal, account user, persistent profile, or legacy owner identity. Browser authentication and product authorization remain disconnected in B2A.

## Migration 004

`wahojobs/db/migrations/004_persistent_product_profiles.sql` defines:

- `product_profiles`, an immutable ownership container;
- `product_profile_revisions`, the authoritative immutable history;
- `product_profile_sources`, exact confirmed source inputs;
- `current_product_profiles`, a read-only view derived from the highest revision number.

The profile container holds no raw profile text, structured document, mutable current-revision pointer, account ID, email, provider subject, session data, legacy identifier, or arbitrary metadata. Its immutable `initial_revision_id` is a deferred relational anchor: the container cannot commit without its matching initial revision. It is not a current-state projection.

Revisions begin at 1 and are contiguous. Every later revision names the immediately preceding revision. Corrections additionally name an earlier revision from the same profile. Supported kinds are `initial`, `edit`, `correction`, `archive`, `reactivate`, and `deletion_request`. Lifecycle is `active`, `archived`, or `deletion_requested`. Archived profiles may receive edit or correction revisions, but those revisions remain archived. Editing does not reactivate a profile; reactivation requires a distinct `reactivate` revision. Deletion requested is terminal until controlled purge.

The current view is derived from immutable history. There is no operation that rewinds current state and no mutable projection table. Restoring older content will require a new correction revision in B2B.

## Confirmed sources and privacy

Profile content is eligible for persistence only after review and explicit confirmation. Exact accepted About You text is stored only in `product_profile_sources`. All source rows for a revision are staged before the revision row inside one deferred-foreign-key transaction. Revision insertion validates the declared count, contiguous ordinals, identity, environment, and timestamps, then seals that exact bundle. No source can be inserted, updated, or deleted after its revision exists. A later complete revision may repeat a bounded confirmed source so the revision remains independently reproducible.

Canonical structured JSON uses controlled lowercase ASCII `snake_case` object keys: one to 128 characters drawn only from `a-z`, `0-9`, and `_`. The rule applies recursively, duplicate object keys are rejected, and semantic key matching removes underscores before checking the sensitive-key denylist. Raw input, About You text, evidence copies, résumé/CV content, application content, account and principal identities, provider/session data, credentials, authentication material, and email/OAuth identity fields are prohibited from the structured document. Normal Unicode remains valid in profile values. B2B canonical validation will remain stricter than these durable SQL privacy guards.

The normalized denied forms are: `originaltext`, `rawtext`, `rawinput`, `rawcontent`, `aboutyou`, `aboutyoutext`, `sourcetext`, `sourcecontent`, `evidence`, `evidencesnippet`, `evidencesnippets`, `resume`, `resumecontent`, `cv`, `cvcontent`, `applicationcontent`, `rawapplicationcontent`, `accountid`, `userid`, `principalid`, `providerid`, `providersubject`, `sessionid`, `sessiontoken`, `token`, `cookie`, `authorization`, `authorizationheader`, `authenticationheader`, `password`, `secret`, `credential`, `bearer`, `csrf`, `csrfmaterial`, `invitationhmac`, `rawclaims`, `email`, and `oauthsubject`.

B2A permits only:

- `confirmed_about_you_text` as `text/plain`;
- `user_confirmed_correction` as `application/json`.

PDF, DOCX, image, OCR, binary, email, OAuth, application, and session source types are not supported. Résumé ingestion is deferred until encryption, parser, consent, retention, and deletion contracts are approved.

Limits are 32 KiB of UTF-8 content per source, 128 KiB canonical JSON, 16 sources per revision, JSON depth 12, 4,096 JSON nodes, 256 children per collection, and 4,096 characters per scalar. Source text permits horizontal tab (U+0009), line feed (U+000A), carriage return (U+000D), accented text, non-Latin scripts, emoji, and ordinary punctuation. It rejects every other C0 control (U+0000–U+001F), DEL (U+007F), and every C1 control (U+0080–U+009F).

Future services will preserve exact source text while producing deterministic UTF-8, NFC-oriented canonical structured documents. Full profile purge will remove sources, revisions, and the container in one transaction after the current lifecycle reaches `deletion_requested`. B2A creates no purge receipt or permanent content tombstone.

## Hashes and identifiers

Profile, revision, and source IDs use `prf_`, `pvr_`, and `pfs_` plus 32 lowercase hexadecimal characters. SQL rejects malformed, uppercase, nonhexadecimal, all-zero, and repeated-character payloads. Syntax cannot prove how an externally supplied ID was historically generated. B2B mutation services will generate IDs internally with at least 128 bits of cryptographically secure randomness.

Structured-document hashes, source-content hashes, source-bundle hashes, and request fingerprints have narrow purposes: drift detection, bundle identity, and future idempotency. SQLite enforces lowercase SHA-256 format and relational consistency but cannot recompute SHA-256. B2B services will compute hashes; B2B reconciliation will recompute them from durable content. Public or browser callers will never supply trusted fingerprints.

## Guarded installation

Inspection is read-only by default:

```text
python -B scripts/persistent_profiles_migration.py --db <database> --json
```

Temporary-database installation requires explicit mutation:

```text
python -B scripts/persistent_profiles_migration.py --db <temporary-database> --yes --json
```

Any access to the configured workspace database additionally requires `--allow-workspace-db` after separate authorization. B2A does not authorize that operation.

Migration 004 requires complete, canonically attested Migrations 001, 002, and 003. Its DDL, marker, schema attestation, empty-state validation, integrity check, foreign-key check, and preservation check use one migration-owned transaction. Partial or conflicting installations are refused. A second apply is a documented no-op.

The canonical manifest attests normalized table, view, and trigger SQL; complete column definitions; CHECK and UNIQUE constraints; foreign keys; named and automatic indexes; index order and origin; and unexpected replacement objects. Migration fault testing currently has 54 logical hook labels covering 22 distinct transaction-visible database states; adjacent hooks that share one durable checkpoint are not counted as independent states. Normal product startup does not import or execute this migration.

## Compatibility and rollout

Migration 004 performs no backfill, principal creation, alias registration, account binding, ownership rewrite, MatchRun persistence, browser cutover, or pipeline change. The ten legacy `user_profiles` rows and all current matching, preview, My Jobs, transition, applicant-update, review, metadata overlay, benchmark, and Greenhouse behavior remain unchanged.

Principal, account, and owner-binding eligibility is enforced when a profile container is inserted. Later lifecycle changes can make an existing owner ineligible without rewriting the immutable profile. Preventing unauthorized B2B mutations and detecting that later drift remain explicit B2B service and reconciliation responsibilities. B2A has no profile row service or row-level profile reconciliation and creates no profile rows.

- B2A: dormant schema, canonical attestation, guarded migration, documentation, and isolated tests.
- B2B: dormant creation/revision services, idempotency, concurrency, and row reconciliation.
- B2C: controlled creation for temporary or reviewed internal principals.
- B2D: later MatchRun and reviewed-profile persistence integration.
- B2E: browser authorization cutover after authentication and session milestones.

Consent wording, deletion waiting periods, and any minimal nonidentifying purge receipt policy must be approved before browser persistence activation.

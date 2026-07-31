# Accounts and Sessions Foundation

Accounts Milestone A adds an explicit persistence and domain-service foundation. It does not make the local product multi-user or authenticated. No login route, OAuth callback, cookie middleware, persistent canonical profile, MatchRun ownership cutover, or pipeline ownership migration is included.

## Migration 002

`wahojobs/db/migrations/002_accounts_sessions.sql` is installed only by the dedicated command:

```text
python -B scripts/accounts_migration.py --db <temporary-or-reviewed-database>
python -B scripts/accounts_migration.py --db <temporary-or-reviewed-database> --yes
```

Without `--yes`, inspection is read-only. The configured workspace database is refused unless `--allow-workspace-db` is also supplied after a separate review. Migration 001 must already be complete. The command never creates the base schema and normal runtime initialization never installs migration 002.

Migration 002 was applied to the workspace database on 2026-07-18. The migrated workspace SHA-256 was `8fe98a859c67298c5ea075fee645146c8501d1a046c4910815bcaa790dbf1c6e`; all eight account tables remained empty immediately after installation. The verified pre-migration recovery backup is `wahojobs_immediate_pre_accounts_migration_002_retry_20260718T120808Z.sqlite`.

DDL, the migration marker, integrity checks, foreign-key checks, and account reconciliation are one migration-owned transaction. The implementation executes complete SQL statements individually instead of using `executescript()`. A failed boundary leaves no migration-002 marker or object.

The migration creates:

- `users`
- `auth_identities`
- `account_invitations`
- `account_sessions`
- `account_session_rotations`
- `consent_events`
- `account_lifecycle_events`
- `account_deletion_requests`
- supporting indexes and append-only/identity-immutability triggers

It does not claim or alter `local_user`, `user_profiles`, pipeline items, MatchRuns, jobs, crawler rows, or canonical opportunities.

## Users and identities

Internal user IDs use `usr_` plus 128 random bits. They are not derived from email, provider subject, profile ID, or row order. `users` contains only account lifecycle projection fields; profile content belongs to a later milestone.

The closed lifecycle is:

| Current | Operation | Next |
| --- | --- | --- |
| active | suspend | suspended |
| suspended | reactivate | active |
| active or suspended | request deletion | deletion_requested |
| deletion_requested | cancel deletion | the recorded pre-request active/suspended state |
| deletion_requested | deactivate after cooling | deactivated_pending_purge |

`deactivated_pending_purge` blocks access but truthfully says that personal data still exists. Milestone A has no purge operation and no reachable `purged` state. Every projection change uses a positive guarded `row_version` and a contiguous lifecycle event in the same transaction.

Authentication identity is `(provider, provider_subject)`, never email. The initial provider enum contains only `google`. Provider, provider subject, owning user, and identity ID are immutable. Verified email remains an attribute and can change in a later provider-authentication flow.

`AccountService` is constructed with one `TrustedIdentityVerifier`. That verifier owns an unpredictable in-memory capability and is the only component that can attest a `VerifiedProviderIdentity` for that service. The service checks capability identity and an immutable field snapshot; dictionaries, form values, browser claims, manually constructed lookalikes, altered objects, and identities from another verifier are rejected. The capability, provider subject, and verified email are absent from public serialization and safe representations. This boundary protects the service API from untrusted claims and ordinary callers; it does not claim protection from arbitrary code execution or deliberate monkey-patching inside the server process.

## Invitation lookup

Beta invitations normalize email by trimming surrounding whitespace, case-folding local/domain text, and IDNA-normalizing the domain. A one-time invitation is `inv_<public-id>.<random-secret>`. Candidate lookup uses only the nonsecret public ID. The service then recomputes the submitted secret HMAC and invited-email HMAC with injected, versioned HMAC-SHA-256 key material and compares both with `hmac.compare_digest()` before checking status or expiry. The key must contain at least 256 bits and is never hardcoded or persisted. Tests inject a test-only key.

Only HMACs, the public ID, and a masked diagnostic hint are stored. The raw invitation token is returned once at creation and cannot be recovered. Public authentication failures do not distinguish malformed, missing, expired, revoked, consumed, wrong-key, duplicate-subject, or uninvited cases. Exact successful consumption replay is available only when the caller presents the same valid one-time token and idempotency request.

## Session secret lifecycle

Sessions use opaque 256-bit random secrets. The raw session and CSRF secrets are returned only in `SessionCreation` when a session is created or rotated. Database rows store SHA-256 digests under the versioned `sha256_v1` contract. Public session objects omit both hashes and all raw material.

For uniformly random 256-bit tokens, an unkeyed digest is resistant to offline guessing because the token itself has full entropy. A future deployment can introduce a keyed digest/pepper and hash-version rotation without changing the public contract. Provider tokens and OAuth claims are never session inputs or stored metadata.

Resolution uses the indexed digest followed by constant-time comparison, then enforces:

- token-hash version
- revocation and replacement state
- idle expiry
- absolute expiry
- active user lifecycle
- `created_at <= now < idle_expires_at` and `now < absolute_expires_at`

Clock rollback has no allowance: a time before session creation fails generically and never mutates or revives the session. Milestone A uses a fixed idle deadline established at creation or rotation. Resolution is read-only and does not slide the deadline or update `last_seen_at`; future browser middleware may add an explicitly versioned touch policy. Rotation never extends the original absolute deadline.

`account_session_rotations` is the one authoritative lineage representation. Each immutable row records one predecessor, one replacement, their owning user, and the rotation time. `account_sessions` deliberately has no independently mutable parent or replacement columns; public parent/replacement values are derived from the edge table. Composite foreign keys require both sessions to belong to the edge user. Global predecessor and replacement uniqueness prevents forks and reverse forks, while the insertion guard rejects self-edges, cycles, older replacements, invalid timestamps, an active predecessor, or an inactive replacement. The edge and its predecessor rotation state must agree exactly.

The service owns one outer account transaction and performs rotation in this order: validate the active predecessor and version, create the replacement, revoke the predecessor as `session_rotated`, insert the authoritative edge, validate both derived directions, and return the replacement session and CSRF secrets once. Any failure rolls the replacement, predecessor update, and edge back together. Exact retry cannot recover persisted raw secrets and fails generically without creating another session or edge.

`validate_session_csrf()` first resolves the active session and then compares the submitted CSRF secret hash with `hmac.compare_digest()`. Each session receives an independent 256-bit CSRF secret on creation or session rotation; hashes are globally unique and the raw value is returned only with the corresponding one-time session creation result. Milestone A rotates CSRF with the session, not independently. Browser cookie and form integration remains deferred.

Suspension and deletion requests revoke active sessions. Reactivation or deletion cancellation never resurrects them.

## Consent and lifecycle ledgers

Consent purposes are `profile_storage`, `product_terms`, and `privacy_policy`; actions are `granted` and `revoked`. Effective consent is evaluated from ordered events. The service rejects revocation without a prior grant and consecutive duplicate actions.

Lifecycle events cover account creation, suspension/reactivation, deletion request/cancellation, and deactivation pending purge. Event versions are contiguous with the `users` projection. Consent versions are contiguous per user and purpose. Database triggers reject direct gaps and reject UPDATE and DELETE on both ledgers.

User creation is an inclusive temporal boundary. The immutable `users.created_at` value must be canonical UTC. Every consent and lifecycle event must have canonical UTC `occurred_at >= users.created_at`; equality is valid. Session creation, invitation consumption, and deletion requests use the same owner-creation boundary. Existing event-to-event chronology and contiguous-version rules still apply after the boundary check.

All account metadata uses one recursive validator and deterministic, sorted JSON. The contract allows at most 4096 serialized bytes, depth 8, 64 keys per object, 64 list items, 128 characters per key, and 1024 characters per string. It accepts only exact JSON scalar types. Unicode-normalized, case-folded keys have hyphens, underscores, periods, whitespace, slashes, and colons removed before sensitive-name checks. Authorization, cookies, credentials, CSRF material, invitation HMACs, OAuth/raw claims, provider subjects, resumes, secrets/tokens, database paths, and SQL are rejected at every depth; bearer material in values is also rejected. A depth failure is always `InvalidAccountInput`, never a raw recursion error. Metadata never controls authorization or identity.

## Deletion limitations

A deletion request records `requested_at`, `cooling_period_ends_at`, and `purge_eligible_at`. Only one request may be open for a user. Requesting deletion changes lifecycle state and revokes every session in the same transaction. Cancellation restores the status recorded when the request was made without restoring sessions. Deactivation is forbidden before the cooling deadline.

After cooling, the service may record `deactivated_at`, bounded review evidence, and `deactivated_pending_purge`. It does not populate `purged_at`, claim completion, or purge identities, profiles, pipeline history, crawler data, applicant updates, or backups. Physical purge and erasure evidence belong to a later reviewed service. Exact repeated requests and compatible fresh requests return the existing request without another version, lifecycle event, or session revocation.

## Transactions, retries, and public errors

Services use `BEGIN IMMEDIATE` on an idle connection and collision-resistant savepoints inside a caller transaction. A helper commits only a transaction it owns. Nested failure rolls back only its savepoint, preserving unrelated caller work.

Guarded account/session versions reject stale updates. Provider identity uniqueness, invitation consumption, session lineage, and pending-deletion uniqueness are enforced in SQLite. Explicit idempotency keys prevent duplicate invitations, identity links, sessions, consent events, lifecycle events, and deletion requests. A repeated session-creation/rotation key cannot recover the one-time raw token and therefore fails safely instead of returning stored secret material.

Authentication and session failures are generic and non-enumerating. Unknown, suspended, deletion-requested, and deactivated users share the same session-creation failure; malformed, missing, expired, revoked, rotated, pre-creation, and inactive-user sessions share the same session failure. Public exceptions have no reason attributes. Public dataclasses never include provider subjects, verified email, invitation HMACs, request fingerprints, token hashes, CSRF hashes, raw claims, or database paths.

## Reconciliation

The reconciliation command is always read-only:

```text
python -B scripts/accounts_reconcile.py --db <database>
python -B scripts/accounts_reconcile.py --db <database> --json
```

It reports schema/marker completeness, lifecycle counts, orphans, identity duplicates, invitation/session/CSRF hash violations, CSRF binding drift, expired or pre-creation sessions, inactive-user sessions, same-user lineage branches/cycles/mismatches, truthful deletion-state mismatches, lifecycle version/projection drift, invalid consent chains, append-only triggers, timestamps, foreign keys, integrity, and malformed, noncanonical, or sensitive metadata. Blocking drift exits nonzero. The command never repairs state or prints provider subjects or secrets.

Rotation drift uses distinct reason codes: `session_rotation_cross_user`, `session_rotation_self_reference`, `session_rotation_fork`, `session_rotation_reverse_fork`, `session_rotation_cycle`, `session_rotation_missing_predecessor`, `session_rotation_missing_replacement`, `session_rotation_temporal_mismatch`, `predecessor_not_revoked`, and `active_predecessor_with_active_replacement`. User-boundary drift uses `consent_event_predates_user`, `lifecycle_event_predates_user`, `deletion_request_predates_user`, `session_predates_user`, `invitation_consumption_predates_user`, and `lifecycle_projection_predates_user`. Diagnostics contain stable record identifiers and reason details, never secret hashes, raw tokens, provider subjects, or private metadata.

## Future Google OAuth adapter

A later adapter must use a vetted OAuth/OIDC library and authorization-code flow with PKCE. It must validate state, nonce, issuer, audience, signature, and expiry before asking the injected `TrustedIdentityVerifier` to construct a verifier-bound identity. The verified object contains only provider `google`, immutable provider subject, optional verified email, the email-verification flag, authentication time, and provider metadata version.

Milestone A performs no OAuth HTTP request, token parsing, redirect, callback, or browser session handling.

Migration 002 is installed but dormant in the workspace database. No account service is connected to the local browser application in this milestone, and all account tables were empty at the verified installation boundary.

## Deferred milestones

- Milestone B: persistent canonical profiles and provenance-aware profile updates
- Milestone C: account ownership for MatchRuns and pipeline state, including an explicit migration policy
- Milestone D: Google OAuth/OIDC adapter, login routes, cookies, CSRF middleware, and session-touch policy
- Milestone E: reviewed anonymous/local-user upgrade and merge behavior
- Milestone F: subscriptions, resume upload, LinkedIn integration, retention/purge workers, and end-to-end deletion operations

## Later reviewed durable browser activation

The preceding Milestone A statements remain the historical foundation and
remain true of the ordinary `scripts/local_product_app.py` startup. A later,
narrowly reviewed milestone composes the accepted account-session,
browser-session, trusted-login, Google OIDC, authorization-transaction, and
persistent-profile services only when
`scripts/durable_google_login_app.py --config <ABSOLUTE_CONFIG_PATH>` is
invoked. The ordinary startup does not load a configuration, open an account
database, read login secrets, contact an identity provider, or activate the
login, callback, logout, or protected-profile routes.

The dedicated activation authenticates an already existing active Google
identity without an invitation. It can also accept an operator-created
invitation only in the login-start POST body, bind that credential inside the
durable transaction's protected material, and provision a first account when
the cryptographically verified Google email matches the invitation.
`AccountService.create_invited_user()` remains the sole atomic authority for
invitation consumption, active-account creation, Google-identity creation, and
the initial lifecycle event. Callback input cannot add or replace the bound
credential. Later logins resolve the same immutable provider and subject and
need no invitation; existing identities never consume a presented invitation.

After that canonical account is resolved or provisioned, every durable login
calls `ensure_account_native_principal()` with only the server-resolved account,
configured environment, and trusted time. The canonical principal, active owner
binding, and initial event must resolve successfully before trusted-login may
create a session or credential. Existing valid lineages are reused without
mutation; retry, runtime reconstruction, and concurrent logins converge on the
same lineage. A bootstrap failure after invited-account creation leaves that
account and consumed invitation intact but issues no session, allowing a later
invitation-free login to retry the ownership step. A later session failure does
not compensate or rewrite a valid ownership lineage.

Sessions remain account-oriented and expose no principal, binding, or event
identifier. This activation still does not create a persistent profile,
invitation-delivery mechanism, or administration UI. Consequently the fixed
`/account/profile` redirect is not yet a usable profile surface for a newly
provisioned account. Browser input cannot select an ownership identifier or
environment. Ordinary-runtime activation, live Google deployment, general
identity linking, legacy claiming, and empty-profile activation remain deferred.
The database must already contain the exact Migration-001 through Migration-006
schema; neither the launcher nor login requests initialize, seed, repair, or
migrate it.

Successful B2D1 completion produces an issued session and one request-scoped
compensation authority. The browser adapter converts that authority into a
sealed, noncopyable, nonserializable, one-shot session-delivery lease. The
response layer compensates the new session for any ordinary or control-flow
failure before `BaseHTTPRequestHandler.end_headers()` returns successfully.
Successful `end_headers()` is the accepted server-side delivery boundary: the
lease is then acknowledged once and its compensation and credential metadata
are erased. A later body or socket failure does not revoke the session. This
boundary does not prove that a browser received or persisted either cookie.
There is no client acknowledgement protocol and no Migration 007.

The session cookie remains `wahojobs_session`, using the accepted 43-character
credential encoding with `Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/`, no
`Domain`, and a lifetime bounded by the session's effective expiry. The
companion `__Host-wahojobs_session_csrf` cookie is `Secure`, `HttpOnly`,
`SameSite=Strict`, `Path=/`, has no `Domain`, and is bounded by the same
session. Logout validates that credential through `validate_session_csrf()`,
revokes the current session in a short writable transaction, clears both
cookies, and redirects only to `/login`.

The full activation contract, including its local-only runtime policy and
deferred production work, is documented in
`docs/durable_google_login_browser.md`.

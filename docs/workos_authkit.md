# WorkOS AuthKit email-code slice

This slice replaces a Google-first login surface with WorkOS AuthKit Hosted UI
without changing or activating the existing Google implementation. It accepts
only a completed `MagicAuth` authentication with a verified email address. The
provider identity is always the WorkOS User ID under `workos_authkit`; email is
never an account lookup, linking, or merging key.

The owned route surface is deliberately small:

- `GET /login`
- `POST /auth/workos/start`
- `GET /auth/workos/callback`
- the existing `/logout` routes
- the existing profile routes

A successful callback redirects only to `/account/profile`.

## Authorization transaction

The start route creates state, an S256 PKCE verifier/challenge, and an opaque
browser-binding cookie. The exact configured HTTPS callback URI is passed to
and checked in the Hosted UI URL. `max_age=0` forces active authentication. A
bounded process-local registry retains at most 128 transactions for at most ten
minutes. Claiming a callback removes its transaction before the one-use code
exchange, so replay and concurrent callbacks cannot cause a second exchange.
A process restart discards unfinished transactions and the user can safely
start again.

The code exchange has a five-second timeout and zero automatic retries. The
boundary immediately projects the SDK response to WorkOS User ID, email,
verified-email status, and authentication method; provider access and refresh
material is neither returned to the application boundary nor persisted. SDK,
callback, and browser failures have detail-free public outcomes.

## Accounts and sessions

An invitation supplied for first registration must be pending and unexpired
before redirect. After WorkOS completes, the Accounts boundary revalidates the
same invitation and its canonical email inside the account-creation
transaction. Account creation, WorkOS identity creation, and invitation
consumption are atomic. A verified-email collision is rejected, never linked.

A returning user is resolved only by the exact pair `(workos_authkit, WorkOS
User ID)` and needs no new invitation. A new subject without an invitation is
denied without a WahoJobs account or session.

After exact identity resolution, PB-OWN-1 establishes the account-native
principal. B2D1 receives a narrow proof whose authentication time is the
trusted server clock observed immediately after a successful `max_age=0`
Magic Auth exchange. The proof expires after 60 seconds. WorkOS
`last_sign_in_at` is not used. B2C4 issues the existing WahoJobs session and
CSRF cookies and retains its existing undelivered-cookie compensation. Profile
handoff and WahoJobs logout are unchanged.

## Schema and dependencies

M008 only rebuilds `auth_identities` to expand its provider check from `google`
to `google` plus `workos_authkit`. It copies every existing row and recreates
the existing uniqueness constraints, foreign key, index, checks, and immutable
identity trigger. The migration wrapper requires the exact M007 schema, runs in
one immediate transaction, compares all identity rows, verifies foreign keys
and integrity, and attests the exact M008 closed-schema fingerprint before
commit. It is an explicit library operation: importing it never opens or
modifies a database.

The Python 3.12 lock pins and hashes WorkOS `10.2.0` and its complete compatible
closure while retaining the pre-existing Authlib, joserfc, requests, and Google
test dependencies. Permanent tests use temporary SQLite databases and a fake
WorkOS boundary that performs no network access. No dashboard configuration,
real provider credential, real email, runtime listener, or real-database
migration is part of this slice.

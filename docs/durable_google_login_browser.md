# Durable Google Login Browser Activation

## Status and activation boundary

This milestone is a reviewed, explicit browser composition of the accepted
Accounts, durable browser-session, trusted-login, Google OIDC, durable
authorization-transaction, and persistent-profile boundaries. It is activated
only by:

```text
python -B scripts/durable_google_login_app.py --config <ABSOLUTE_CONFIG_PATH>
```

The dedicated launcher owns an exclusive local HTTPS browser surface. The
ordinary `scripts/local_product_app.py` startup remains
authentication-dormant. Its optional injection seam has no default runtime and
does not load configuration, read secrets, open an account database, contact a
provider, or activate login, callback, logout, or protected-profile routes.
It remains a local/development tool and is not mounted wholesale by the durable
launcher; the durable product surface reuses only deliberately selected pure
matching and presentation helpers.
Importing the browser, runtime, launcher, or fixture modules performs no
database, filesystem-secret, network, route, or environment side effect.

Production activation is unsupported. This milestone supports exactly the
`development`, `test`, and `private_beta` environment names under the strict
local policy below. The browser milestone itself introduced no schema change;
current main now requires the separately installed, offline Migration 007
closed-schema authority documented in
`docs/closed_schema_convergence_migration.md`. It does not introduce an
insecure-cookie mode.

## Strict configuration document

`--config` accepts one explicit absolute path to a nonsecret JSON document. The
document must have exactly the fields below and no others. The
`account_invitation_lookup_key_file` field is optional; omitting it preserves
invitation-free existing-identity login.

```json
{
  "version": 1,
  "environment": "development",
  "database_path": "<ABSOLUTE_EXISTING_DATABASE_PATH>",
  "bind_host": "127.0.0.1",
  "bind_port": 8443,
  "public_origin": "https://127.0.0.1:8443",
  "google_redirect_uri": "https://127.0.0.1:8443/auth/google/callback",
  "google_client_id": "<NONSECRET_CLIENT_ID>",
  "google_client_secret_file": "<ABSOLUTE_EXTERNAL_SECRET_FILE>",
  "account_invitation_lookup_key_file": "<ABSOLUTE_EXTERNAL_M002_INVITATION_KEY_FILE>",
  "oidc_lookup_keys": [
    {"version": 1, "file": "<ABSOLUTE_EXTERNAL_32_BYTE_KEY_FILE>"}
  ],
  "oidc_lookup_active_version": 1,
  "oidc_protection_keys": [
    {"version": 1, "file": "<ABSOLUTE_EXTERNAL_32_BYTE_KEY_FILE>"}
  ],
  "oidc_protection_active_version": 1,
  "session_idle_ttl_seconds": 3600,
  "session_absolute_ttl_seconds": 28800,
  "allowed_post_login_paths": ["/account/profile"]
}
```

The JSON parser rejects duplicate keys, unknown or missing required fields,
an incomplete optional-field shape, non-finite
constants, an unsupported version, and an oversized document. There is no
`DB_PATH` or other environment-variable fallback. Partial configuration fails
before the launcher binds its socket. All pure and cross-field validation
finishes before database, secret, gateway, TLS, or server work. Session idle
TTL is an exact integer from 60 through 2,592,000 seconds; session absolute TTL
is an exact integer from 60 through 7,776,000 seconds; and idle must not exceed
absolute. Booleans, integer subclasses, coercible values, and other types are
not accepted.

The database and every referenced file must already exist as an ordinary,
absolute, nonsymlink file. The runtime rejects reused file identities across
the configuration, database, client secret, optional invitation lookup key,
and all authority-key references. The invitation lookup key is the existing
external M002 HMAC authority used when the operator created the invitation; it
is never placed in the JSON document itself.
The database path must be lexical-canonical, outside every Git checkout,
single-linked, and free of SQLite sidecars. An existing participating lifetime
owner makes startup fail closed, and the independent SQLite attestation still
requires the database to be unavailable to another writer. The runtime seals
that file identity, keeps a raw descriptor pinned across each SQLite
connect-and-verify boundary, opens it explicitly in existing-file mode, and
requires the byte-exact stored SQL plus closed base and Migration-001 through
Migration-007 marker inventory. Every later connection verifies the same
sealed file before application SQL. After constructing the browser integration,
startup repeats the writer, identity, sidecar, quick-integrity, foreign-key,
marker, and
exact-schema checks before publishing the runtime. Startup never initializes,
seeds, repairs, checkpoints, or migrates the database.

The supported local authority policy is exact:

- `bind_host` is `127.0.0.1`;
- `bind_port` is an explicit integer from 1 through 65535;
- `public_origin` is exactly
  `https://127.0.0.1:<bind_port>` or
  `https://localhost:<bind_port>`, with no user information, path, query, or
  fragment;
- the explicit origin port equals `bind_port`;
- `google_redirect_uri` equals
  `<public_origin>/auth/google/callback`; and
- `allowed_post_login_paths` equals exactly `["/account/profile"]`.

The dedicated server keeps its listening socket as raw TCP and applies TLS to
each accepted socket explicitly; it does not weaken `Secure` cookies for HTTP.
Every accepted socket is owned before TLS wrapping or handshake, and a
one-second handshake timeout bounds clients that connect without completing
TLS. Shutdown independently closes pending handshakes. The self-signed
certificate and private key are generated in atomic private external
OS-temporary files, identity-checked before and after TLS loading, and removed
before the TLS context becomes usable. They are not repository artifacts.

## Database ownership and restart contract

PB-OWN-1 adds one server-private lifetime-ownership protocol for one exact
database file. It recognizes only the sealed `durable_runtime` and
`offline_operator` roles. A role is part of the ownership capability; it is not
an invitation command, a database mutation permission, or a public API. The
same exclusion matrix applies within one process and between processes:

| Current participating owner | Durable-runtime acquisition | Offline-operator acquisition |
| --- | --- | --- |
| none | acquire | acquire |
| `durable_runtime` | fail closed with contention | fail closed with contention |
| `offline_operator` | fail closed with contention | fail closed with contention |

Acquisition derives one sibling coordination path by appending
`.wahojobs-lifetime.lock` to the database filename. The coordination file is a
zero-length, ordinary, single-linked file, not a symlink or Windows reparse
point. On POSIX it must belong to the effective user and grant no group or
other permissions. It contains no role, PID, path, token, secret, or status.
The file deliberately remains after release and after process death; its mere
existence is inert, and a valid stale unlocked file is reused rather than
deleted or replaced.

The owner opens that file with an exact non-inheritable descriptor and acquires
without waiting. Windows uses `msvcrt.locking` with `LK_NBLCK` over the one byte
at offset zero; POSIX uses `flock` with `LOCK_EX | LOCK_NB`. A process-local
registry serializes acquisition and rejects a second same-process owner before
native lock semantics could admit it. The database object identity and the
coordination path/descriptor identity are revalidated when ownership is
published and whenever the capability is required. Database or coordination
replacement, an unsafe path component, or a coordination file that is no
longer the sealed empty file invalidates the owner and fails closed.

This is a participating-process protocol, not a universal filesystem mutex.
A program that does not use it can still access the database. The contract
assumes a local filesystem with the documented Windows byte-range-lock or
POSIX `flock` and stable file-identity behavior; it makes no correctness claim
for SMB, NFS, other remote filesystems, or synchronization layers, and the
runtime does not detect all such placements. Consequently the existing SQLite
defenses remain mandatory: rollback-journal format, sidecar rejection, the
`BEGIN IMMEDIATE` writer probe, integrity and exact-schema attestation, and
pinned file-identity checks are not replaced by lifetime ownership.
The ordinary local prototype does not participate. The protocol does not claim
protection from a malicious process running as the same operating-system
identity or from a privileged local administrator. Windows uses only the
standard-library file-lock boundary described here; PB-OWN-1 does not add or
claim a stronger Windows ACL boundary.

After configuration and referenced-file identities are fully validated, the
activation worker registers the lifetime resource with cleanup ownership and
then acquires it for `durable_runtime`. Acquisition and identity revalidation
precede the first database attestation; that attestation precedes TLS-workspace
construction, secret reads, gateway and authority construction, all later
database connections, listener construction and bind, activation, and
readiness. Guard checks revalidate the exact process epoch, role, database
path, sealed capability, and file identities at database and launcher
activation boundaries.

Cleanup retains lifetime ownership until every declared database-capable
dependency is terminal: database descriptors and attestation connections,
runtime database connections, profile and browser integrations, the listener,
routes, request and serve threads, and accepted sockets. A live or uncertain
database-capable dependency prevents release. Closing the exact owned file
object is the native lock-release and descriptor-close primitive; an
incomplete close remains an unresolved `database_lifetime_ownership` cleanup
category, and an indeterminate native close fails closed until process exit
rather than being reported as released. A completed release can be replayed
idempotently. Normal cleanup never deletes the empty coordination file.

The coordination descriptor is non-inheritable across execution. On POSIX,
the fork-child callback closes the child's inherited descriptor, invalidates
its inherited records and process epoch, and replaces its same-process
registry; the parent retains the original lock. The child cannot require or
release the parent's capability, and a fresh child acquisition still contends
while the parent owns the database. On clean process exit the runtime releases
the lock after terminal cleanup. On process death the operating system releases
the native lock, so a later fresh process can revalidate both file identities
and acquire the same persistent coordination file. That recovery does not
bypass SQLite recovery or attestation.

Raw database-validation descriptors and SQLite connections are separate
resources. A raw descriptor is held by one exact, non-inheritable file-object
owner from native creation through validation and its own terminal close.
SQLite's internal descriptors are never inferred from that descriptor or
managed by number: the exact `sqlite3.Connection` object is their sole cleanup
authority. Descriptor numbers, paths, metadata, and object equality are not
issuance identities and are never used to retarget a stale close.

Before opening either resource, the runtime registers a private record with an
opaque issuance identity, runtime-manager identity, connection generation,
creating process identity, and a non-PID process epoch. Native return is
published into that pre-existing record before application code can observe
the resource. The connection lifecycle is:

```text
opening -> leased -> rollback_pending -> closing -> terminal
                      |                  |
                      +-> unresolved <---+
```

Shutdown can change a live lease to `close_pending`, but it cannot close a
connection underneath its borrower. Lease return owns rollback and then the
single close claim. Any uncertain rollback or close quarantines the record;
the manager retries the same exact connection object and never reissues it.
An opener that loses to shutdown cannot publish a lease and remains responsible
for cleanup. Request completion, pruning, and shutdown therefore converge on
one cleanup owner instead of independently closing SQLite.

The process epoch has one short publication lock. Candidate proof generation
happens before that lock; publication rechecks the exact process and existing
winner, so every same-process caller returns the same immutable epoch object.
The fork-child callback clears the inherited epoch and replaces the publication
lock before child code can use it. Inherited authorities first compare the
creating PID, exact epoch, and exact `Thread` object without acquiring any
inherited resource lock. The child therefore rejects them without touching
SQLite, while a fresh child runtime publishes its own epoch and proof.

The manager condition protects record publication, borrower tokens, shutdown,
and close claims. The raw-descriptor lock protects only its exact file-object
offer and terminal state. The manager condition is released before database
open, schema inspection, rollback, close, or other blocking work; descriptor
cleanup does not acquire the manager condition. No socket, SQLite, callback, or
user-controlled operation runs while either ownership lock is held. Connection
issuance proof generation also precedes manager coordination. After generating
the pure-Python candidate, the opener enters the condition only to revalidate
the exact process, target, and accepting state and to publish or reject the
candidate; entropy failure cannot create a record or obstruct shutdown.

Connection use requires the exact unreleased borrower token on its owning
thread and in its creating process epoch. A foreign thread, stale lease
generation, inherited post-fork runtime, connection, descriptor, or lease is
rejected before SQLite is touched. A child does not attempt to close an
inherited SQLite object. It must construct a fresh runtime with a new epoch and
new resource issuances; the parent remains the authority for its original
resources. The public runtime surface yields guarded leases and guarded cursor
results, not raw SQLite lifecycle authority; it exposes no commit, rollback,
interrupt, handler-mutation, or connection-close method. Strict raw
`sqlite3.Connection` access is confined to the production composition's
private, capability-checked call path for existing repositories that require
the exact built-in type. There is no authorized cross-thread transfer API.

A request is registered under an opaque exact owner before route dispatch. The
callback connection factory returns directly into that pre-existing owner's
offer, so the managed connection lease is stable before raw borrowing resumes.
The exact borrowed connection then enriches the offer, and the session-delivery
lease upgrades that same exact bundle before cookie inspection or response
construction. Response construction and the request-return path acknowledge
the same owner; neither a local return value nor a request-release callback is
ever the sole cleanup authority. The registry keeps the owner reachable until
delivery, connection close, and request release have each completed and the
exact released entry is retired. A terminal tombstone can be pruned by
coordinated shutdown after its creating request thread exits without transferring
any live SQLite or delivery authority across threads.

Public delivery outcome and cleanup completion are separate. Once delivery is
terminal, headers are scrubbed, but the owner retains the delivery wrapper,
managed connection lease, response, and exact request claim until connection
close and request release are independently acknowledged. An interruption at
any action or acknowledgement boundary leaves the same registry owner
retryable. Public delivery is never repeated; an authorized retry or shutdown
can continue cleanup after public terminality. Request release is an exact
active-to-released transition for the registered issuance, so it cannot
decrement another request or run twice.

The managed lease also registers that exact request owner as a cleanup
delegate before the callback borrows the raw connection. If the creating
request thread terminates while its registry owner still retains a prepared
raw delivery lease, normal connection, cursor, response, and delivery-wrapper
operations remain rejected on every other thread. Coordinated shutdown may
instead claim one sealed cleanup-only transition after proving that the exact
borrower `Thread` is no longer alive. The claim is bound to the original
manager, record issuance, generation, borrower token, connection identity,
delegate, process epoch, and cleanup thread. It authorizes only the retained
raw lease's terminal delivery action followed by rollback/close of that exact
record; it never exposes or transfers ordinary SQLite use authority.

If interruption occurs after the managed connection is published to the
registry owner but before delegate registration commits, the browser owner can
retire only after the manager record atomically accepts the still-unregistered
lease as its cleanup obligation. No raw connection or delivery work can precede
that registration boundary. The later manager cleanup therefore owns the exact
record and descriptor while the browser request releases independently. The
manager records the exact retired borrower token as the handoff
acknowledgement. If interruption occurs after that acknowledgement but before
the lease wrapper retires, retry recognizes only the same manager, record,
generation, owner identity, capability, and borrower token, then finishes the
idempotent lease retirement without reopening or retargeting cleanup. The same
marker makes partially cleared borrower fields and a pre-transition record
state recoverable: a retry accepts only the exact old-or-cleared identities,
normalizes both fields under the manager condition, and publishes the pending
cleanup state before acknowledging the handoff.

The database record remains the stable claim owner if interruption occurs
before a cleanup claim reaches the browser owner or while compensation or close
is running. Manager shutdown marks a delegate-bound record pending but cannot
close it ahead of required delivery compensation. Browser and database cleanup
claims are per invocation: the invocation releases both claims in its cleanup
boundary even if its thread remains alive afterward. A retry from another
thread can then continue, while the browser owner's exact attempt claim prevents
a concurrent invocation from stealing a live claim. Physical terminal state is
revalidated after an interrupted close, and manager acknowledgement is not
treated as browser completion until the exact lease retirement is observed.
After delivery and exact connection close are acknowledged, the registry owner
releases the active request and retires itself. Thus a dead request thread
cannot leave a prepared delivery, leased connection, active cleanup claim, or
active-request entry dependent on garbage collection or process exit.

The cleanup lock order is lifecycle snapshot, owner claim, manager claim,
raw-delivery compensation and SQLite work, manager terminal commit, owner
phase commit, and lifecycle release/prune. Each lock is released before the
next layer is entered. No SQLite call, delivery callback, or other blocking
work runs under the lifecycle, owner, or manager lock.

The owner binds the manager and record issuance, generation, borrower token,
creating PID, process epoch, exact creating `Thread`, connection object, and
response object. A response operation performs the immutable process/thread
check and validates the exact record and raw connection under the manager
condition before it claims the response/owner lock. No delivery, SQLite, close,
callback, or lifecycle-registry operation runs while that owner lock is held.
The browser lifecycle condition likewise never calls an owner while held.
Rejected foreign-thread or inherited-process operations consequently cannot
detach the response owner, mutate delivery state, or strand a `close_pending`
record; the original borrower and stable request owner retain exact cleanup
authority.

Dead-request recovery repeats that immutable PID and process-epoch fence at
the outer wrapper boundary and before each later owner lock or delegated
cleanup action. An inherited child therefore rejects without entering the
owner, delivery, manager, lifecycle, SQLite, or callback synchronization
graphs, while the authorized parent retains the exact retryable cleanup claim.

Explicit module reload retains the module dictionary but replaces class and
capability identities, so reload remains unsupported while runtime objects are
live. Fork-child registration is itself reload-stable: a module-retained marker
is published only after successful registration, and exactly one stable
callback dispatches through the current module globals to the current epoch
reset implementation. Repeated development-time reloads therefore do not
accumulate callbacks. A subsequent child clears the epoch and replaces the
publication lock once; parent state is untouched. Reload is not a lifecycle
transfer mechanism.

Durable authorization restart depends only on committed Migration-006 state.
The permanent restart proof uses two serial fresh interpreters: process A
commits authorization start and performs production-equivalent cleanup,
including terminal lifetime-ownership release, before it is reaped; process B
then reconstructs configuration, reacquires lifetime ownership, and reconstructs
authorities, runtime, descriptors, and connections from explicit files before
it completes the callback with the network-disabled synthetic provider.
Separate abrupt-exit schedules prove rollback before the start commit and
recovery of one prepared row after the commit; after process death, process B
must still reacquire ownership and pass all SQLite checks. No runtime object,
connection, descriptor, socket, native lock, thread, or in-memory authority
crosses the process boundary. The empty coordination file may persist, but it
does not convey ownership. The B2.1 database/restart contract itself adds no
migration or automatic migration and makes no multiprocess callback-competition
guarantee. Additive retained-key rollover is the separate clean-reconstruction
contract below.

## Activation, readiness, and shutdown

The dedicated launcher uses one all-or-nothing activation sequence:

1. completely validate nonsecret configuration and resolve the database,
   secret-file, and key-file identities and policies;
2. register and acquire exact database lifetime ownership;
3. attest the already migrated database while that capability remains valid;
4. construct and validate the external TLS workspace;
5. load secret material into private construction buffers;
6. construct the gateway, lookup/protection authority, connection owner,
   dormant profile-artifact vault and coordinator, profile service, and profile
   integration privately;
7. register the complete profile integration with the outer cleanup authority,
   start its non-daemon vault coordinator only after that registration, and then
   construct the browser integration;
8. register cleanup ownership, then construct an inactive, unbound server with
   an inert handler and immediately attach it and its concrete listener to that
   owner;
9. build the TLS context and configure explicit per-connection TLS;
10. publish the dedicated handler only after HTTPS construction;
11. repeat the database and secret identity attestations and require current
    lifetime ownership;
12. bind the listener only after another lifetime-ownership check;
13. activate it only after another lifetime-ownership check; and
14. start the owned serve thread, wait boundedly for its first successful
    serving-loop checkpoint, hold that first iteration on a bounded two-party
    decision, require lifetime ownership again, atomically claim readiness
    against shutdown and serve failure, publish readiness, and serve.

Signal handlers are installed after final runtime construction and before bind,
activation, or readiness. A signal received at any later checkpoint prevents
the next activation step. The signal callback records the first fixed signal
category as the lock-free shutdown publication; the main control path performs
all cleanup.

One process-owned cleanup coordinator registers each live resource
immediately. The inactive server and its concrete listener have independent
cleanup actions, so `server_close()` failure cannot suppress the listener
close. It attempts cleanup in reverse ownership order: stop serving and
accepting, close pending handshakes and accepted sockets, boundedly drain
request threads, detach the route, close the high-level server and concrete
listener, stop browser request admission, close database connections and both
logical key-authority cleanup positions, close the Google gateway, remove the
TLS workspace, release database lifetime ownership only after its complete
terminal-dependency set has closed, and finally restore prior signal handlers.
The request-thread and serve-thread drain bounds are two seconds per cleanup
attempt. A live thread, connection, listener, socket, lifetime-ownership
descriptor, or other failed close remains a fixed-category unresolved entry. A
later cleanup call retries only unresolved entries; terminal resources are not
closed twice. Ordinary and explicitly named control-flow cleanup failures
cannot skip another cleanup action or replace an earlier startup/request
failure.

The profile-artifact vault has a separate claim-cleanup coordinator. Its object
and worker are constructed dormant, and neither the vault nor its service
constructor starts a thread. Activation first registers the enclosing profile
integration with the process cleanup coordinator and only then starts the
non-daemon worker. If `Thread.start()` raises after launching, or a control-flow
exception arrives immediately afterward, the registered integration remains the
exact close authority. Vault close stops admission and gives the worker and its
owned transition probes a finite two-second close/join bound. A live worker or
probe keeps cleanup truthfully unresolved, and a later close retries it; close
does not claim success merely because the first bound elapsed.

The concrete serve thread is registered before `start()` and remains observable
until it is no longer alive. A request thread is likewise retained by the
external drain owner through its actual return, including a `start()` call that
launches the worker and then raises. Live request-thread ownership blocks route
and runtime-authority teardown; a later bounded cleanup attempt reaps only
threads that have actually terminated.

The internal shutdown result contains only ready/requested booleans, fixed
closed and unresolved resource categories, a completeness boolean, fixed
cleanup-failure categories, and an optional fixed signal category. It contains
no paths, ports, exception values, object representations, credentials,
transaction values, cookie material, or request-derived thread names, and it
is not exposed through an HTTP route.

Deterministic launcher exit statuses are:

- `0` for an ordinary complete stop;
- `2` for a startup or serve failure;
- `3` for incomplete cleanup;
- `130` for a completely cleaned-up `SIGINT`;
- `143` for a completely cleaned-up `SIGTERM`; and
- `149` for a completely cleaned-up Windows `SIGBREAK`.

The first signal wins. Later signals cannot start another cleanup owner.
Launcher cleanup is synchronous and retryable. The vault does use the explicitly
owned coordinator worker described above, but no cleanup outcome relies on
daemon exit, finalizers, garbage-collection callbacks, an unowned background
scheduler, or process termination.

## Secret and authority handling

The JSON document contains paths, not a Google client secret or OIDC key
bytes. Client-secret and key material comes only from the explicit external
files outside a Git checkout. Secret files must have one filesystem link; on
platforms with POSIX permission bits they must not grant group or other
access. The client secret is bounded to 16 through 512 printable ASCII bytes.
Each lookup or protection key is exactly 32 bytes.

Lookup and protection key rings are distinct. Each contains one through three
sorted positive integer versions and names one retained version as active.
Retaining prior versions permits a transaction prepared before an approved
rotation to complete after runtime reconstruction. Equal key material is
rejected across both authorities.

### Additive retained-key rollover

B2.2 supports an operator-prepared additive rollover only across a clean
runtime stop and reconstruction. The replacement configuration may add one new
lookup-key version and one new protection-key version, select the new versions
as active, and must retain every older version still needed by a prepared
authorization transaction. A callback prepared before the stop is recovered
with the exact versions recorded in its Migration-006 row; a start prepared
after reconstruction uses only the newly active versions. Missing required
retained material fails closed and issues no browser session; the runtime never
substitutes a different key version.

This contract does not hot-reload or rewrite configuration or key files,
generate or distribute keys, delete or retire retained versions, or decide
when retirement is safe. Automated rotation, concurrent rollout, retained-key
retirement, KMS/HSM or other managed custody, and live-runtime replacement
remain separate authorized work.

File contents are read through identity-bound regular-file handles with
before-open, opened-handle, during-read, after-read, and post-close pathname
checks. Replacement, disappearance, new hard links, reparse points, and
identity changes fail closed. Temporary buffers are cleared on success and
failure, and runtime shutdown closes and clears the gateway, key, and database
authorities. The construction configuration and all secret paths are released
after activation; the public runtime configuration retains only bind host,
bind port, and public origin. Secret values are not accepted in the
configuration JSON, command line, environment variables, database, logs,
public representations, public errors, or repository fixtures. Python does
not promise physical memory zeroization; buffer clearing is the bounded
best-effort process contract.

## Exclusive routes and methods

The dedicated HTTPS surface has this exclusive or explicitly admitted route
contract:

| Path | Accepted methods | Purpose |
| --- | --- | --- |
| `/login` | `GET` | Fixed sign-in page and start-CSRF creation |
| `/auth/google/start` | `POST` | Durable authorization preparation |
| `/auth/google/callback` | `GET` | Terminal durable callback completion |
| `/logout` | `GET`, `POST` | Confirmation and CSRF-protected revocation |
| `/account/profile` | `GET`, `HEAD`, `POST` | Owned profile read, explicit first-profile creation, or bounded same-profile correction |
| `/find-matches` | `GET`, `HEAD`, `POST` | Authenticated candidate entry/review or query-only ranked matches from the durable profile |

An unsupported method on an authentication path returns `405` with the fixed
allowed-method declaration. The dedicated launcher enables exclusive
fall-through rejection, and the authenticated matches integration owns one
bounded process-local `MatchRunRegistry`. The same registry carries a no-profile
candidate draft through initial submission, structured review and draft correction,
and explicit confirmation. Every `/find-matches` GET, HEAD, and POST first
authenticates the durable session and authorizes the account-native principal;
draft and review state is additionally bound to the originating account,
environment, principal, and session by a server-only authority digest.

`GET /find-matches` opens candidate entry when no persistent profile exists and
regenerates ranked matches when one does. POST handles only the authenticated
entry/review/confirmation journey. `/preview`, My Jobs, tracker and dashboard
routes, `/action`, demo personas, legacy selectors, and unrelated ordinary
routes receive the exclusive rejection. The configured Host/no-proxy guard runs
before rendering or reading a POST body. The inner boundary then applies the
accepted same-origin, session, CSRF, strict content-type, bounded single-read,
encoding, duplicate-field, and unsupported-field decisions before acting on
candidate state.
Responses use bounded fixed HTML, escaping, `Cache-Control: no-store`, a
default-deny CSP, and `X-Content-Type-Options: nosniff`. Pages that render a
same-origin POST form (login, logout, profile entry/review/creation, and profile
correction stages) use `Referrer-Policy: same-origin` so the browser preserves
its non-opaque same-origin provenance for the protected submission. Callback,
redirect, error, and other non-form or query-sensitive responses retain
`Referrer-Policy: no-referrer`, so callback URLs and authorization codes cannot
become referrers. This response policy does not relax the request boundary:
the exact configured `Origin` remains mandatory for POST, and missing,
mismatched, duplicated, or `null` origins remain rejected.

CSP form-navigation authority is selected explicitly. `/login` alone emits
`form-action 'self' https://accounts.google.com`: `'self'` admits the protected
POST to `/auth/google/start`, and the second source is the origin derived from
the server-private pinned Google authorization endpoint. Chromium applies the
source document's `form-action` directive when the POST's `303` redirects the
form navigation, so that one pinned origin must be present for the authorization
navigation. No browser input or request parameter selects the source. Logout,
profile, matching, and correction forms retain `form-action 'self'`, and no
wildcard, generic HTTPS source, token, JWKS, or userinfo endpoint is admitted.
The exact Origin and CSRF checks remain mandatory. Login-start preparation only
commits the local authorization transaction and constructs the authorization
URL; it performs no provider transport or provider egress.

All owned requests require exactly one configured `Host` authority. Missing,
duplicate, malformed, or mismatched `Host` is rejected. `Forwarded` and every
`X-Forwarded-*` header are rejected, and no proxy is implicitly trusted.
Absolute-form request targets, control characters, invalid percent escapes,
oversized targets, oversized or excessive headers, and ambiguous cookies are
rejected. POST requests additionally require the exact configured same-origin
`Origin`; a supplied `Sec-Fetch-Site` must be `same-origin`.

## Login and durable start

`GET /login` returns fixed escaped HTML with a “Continue with Google” POST form
and one hidden start-CSRF value. The same value is bound to
`__Host-wahojobs_login_csrf`. No provider URL, credential, configuration
value, request-controlled destination, or account detail is rendered.

`POST /auth/google/start` accepts only the
`application/x-www-form-urlencoded` media type, matched ASCII
case-insensitively for its type and subtype. The existing strict parser still
rejects parameters, surrounding or embedded whitespace, malformed syntax,
non-ASCII lookalikes, alternate media types, and duplicate `Content-Type`
headers. The body is limited to 1 KiB and must contain exactly one strictly
decoded `csrf` field and at most one optional strict `invitation` field. The
form value and login-CSRF cookie must match exactly. Invitation credentials are
accepted only in this POST body. A valid value is immediately bound into the
durable transaction's authenticated protected material; it is not copied into
the provider URL, callback input, state, nonce, redirect, cookie, response, or
cleartext database fields. Callback parameters cannot add or replace it.
Query strings, additional fields, duplicate fields, invalid encodings, and
`next` or destination parameters are not accepted.

The route opens a fresh writable request connection and performs the accepted
durable authorization preparation. The authorization transaction is committed
and its prepared capsule is closed before the provider `Location` is exposed.
Only then does the fixed `303` response set the browser transaction cookie.
Preparation, commit, reread, response construction, or validation failure
returns a generic no-store response without a `Location`.

## Browser-bound callback ordering

The callback URL is constructed only from the configured
`google_redirect_uri` plus the request's raw query. Scheme, authority, and
callback prefix are never inferred from `Host`, proxy headers, or another
request value. A Google success callback requires exactly one nonblank `state`,
`code`, and RFC 9207 `iss` and must not contain `error`. An accepted
redirected-error callback requires exactly one nonblank `state`, `error`, and
`iss`, must not contain `code`, and may contain at most one each of the existing
`error_description` and `error_uri` diagnostics. In both shapes `iss` must
equal the pinned modern issuer `https://accounts.google.com` exactly. Missing,
blank, duplicated, or non-exact issuer input fails closed.

The raw ASCII callback remains limited to 8,192 bytes. Strict parsing admits at
most nine decoded fields, with names limited to 64 UTF-8 bytes and values to
4,096 UTF-8 bytes. Percent escapes and UTF-8 are decoded strictly; empty names,
control or replacement characters, duplicate decoded names, malformed fields,
and over-limit callbacks are rejected globally. After those checks, unique
fields outside the authoritative success/error inventory are non-authoritative
extensions and are discarded. This is deliberately not a provider-extension
allowlist. Google may return `authuser`, `hd`, `prompt`, and `scope`, and future
bounded unique extension names are handled the same way.

Callback extensions do not establish account identity, verified hosted-domain
membership, granted-scope authority, login policy, invitation authority, or a
browser session. They cannot replace state, issuer, code, error, provider
configuration, redirect URI, PKCE, nonce, or any verified identity input. Their
values are not logged, persisted, rendered, or passed to durable lookup/claim,
provider exchange, or ID-token validation. Only a canonical callback rebuilt
from the authoritative fields proceeds beyond validation.

The critical response shape and authorization-response issuer check occur
during bounded callback parsing, and extensions are discarded, before durable
state lookup, transaction claim, browser binding, or provider exchange. This
does not alter the separate later validation of the signed ID-token `iss`
claim, which continues to use its existing modern/legacy issuer contract.

The browser callback order is security-significant:

1. strictly parse the bounded callback, validate its critical shape and exact
   response issuer, and discard non-authoritative extensions;
2. recover the callback state and perform the durable digest lookup;
3. atomically and terminally claim the durable authorization transaction;
4. release the authorization-transaction write lock;
5. compare the claimed transaction identity with
   `__Host-wahojobs_google_tx` in constant time;
6. only for a matching binding, perform provider traffic and verification,
   identity lookup, proof issuance, and B2D1 completion; and
7. only for an exact B2D1 `issued` result, prepare the browser-session delivery
   lease.

A missing, malformed, duplicate, or swapped transaction cookie leaves the
transaction terminal and stops before provider traffic, identity lookup,
trusted proof issuance, B2D1, or session work. Replay and the
`already_completed`, `pending_commit`, `conflict`, and `unavailable` outcomes
do not emit a session cookie. Terminal browser outcomes clear the transaction
cookie and use generic, no-store text; provider errors, state, code, identity
details, and exception content are not echoed.

No authorization-transaction write lock spans browser binding, provider work,
identity lookup, proof issuance, B2D1, response construction, or response
delivery.

## Sealed response-delivery lease

An exact successful B2D1 result contains one issued browser session and
request-scoped compensation authority. The response layer prepares a sealed,
immutable, noncopyable, nonserializable, nonsubclassable, one-shot delivery
lease. It privately retains only the material needed to render the accepted
session and companion CSRF cookies and to compensate that exact new session.
Public string and representation output redacts credentials.

The lease state machine is:

```text
prepared --acknowledge_delivery()--> acknowledged
prepared --------fail_delivery()--> failed
```

Neither terminal state can be reused or crossed. A lease cannot be
acknowledged or failed twice, acknowledged after failure, or failed after
acknowledgement. Terminalization erases the retained credentials, connection,
session result, vault reference, time, and compensation metadata. This
activation introduces no additional hidden issued-session registry.

The response writer treats successful
`BaseHTTPRequestHandler.end_headers()` as the accepted server-side delivery
boundary:

- header validation failure, `send_response()` failure, `send_header()`
  failure, `end_headers()` failure, or any ordinary exception,
  `KeyboardInterrupt`, `SystemExit`, or `GeneratorExit` before that boundary
  invokes exact compensation/revocation and fail-closed request-vault cleanup;
- after `end_headers()` returns successfully, the lease is acknowledged
  exactly once and its compensation metadata is erased; and
- a later response-body or socket failure does not compensate the session.

This boundary proves only that the server accepted the headers. It does not
prove that the browser received or persisted a cookie. This milestone adds no
client acknowledgement protocol.

## Cookie contract

No cookie has a `Domain` attribute, and no insecure development variant exists.

| Cookie | Attributes |
| --- | --- |
| `wahojobs_session` | Accepted 43-character opaque credential; `Path=/`; effective-expiry-bounded `Max-Age` and `Expires`; `Secure`; `HttpOnly`; `SameSite=Lax` |
| `__Host-wahojobs_login_csrf` | `Path=/`; `Max-Age=600`; `Secure`; `HttpOnly`; `SameSite=Strict` |
| `__Host-wahojobs_google_tx` | `Path=/`; `Max-Age=600`; `Secure`; `HttpOnly`; `SameSite=Lax` |
| `__Host-wahojobs_session_csrf` | `Path=/`; session-lifetime-bounded `Max-Age` and `Expires`; `Secure`; `HttpOnly`; `SameSite=Strict` |

Cookie clearing preserves the corresponding `Secure`, `HttpOnly`, `Path=/`,
and `SameSite` policy while setting `Max-Age=0` and a fixed expired date.

## Protected profile, correction, refresh, and logout

After accepted delivery, the callback redirects only with `303` to the fixed
`/account/profile` destination. There is no request-controlled redirect. The
protected profile opens a fresh query-only connection or snapshot,
authenticates with the accepted `wahojobs_session` parser, authorizes ownership,
and renders the existing escaped, CSP-protected profile. When the authenticated
account has a valid account-native lineage but no persistent-profile row, the
same read boundary renders a fixed empty state and performs no profile,
ownership, session, or legacy-data write. GET and HEAD remain strictly
read-only; request input cannot select an account, principal, binding, profile,
or environment. The empty page provides a fixed `Create profile` action. An
existing-profile page retains `Find matches` and adds one clear `Update profile`
entry point; navigation is otherwise limited to profile and logout. A refresh
with the active session and a reconstructed dedicated runtime remain
authenticated; unauthenticated access offers `/login`.

`POST /account/profile` remains the sole activated profile-mutation route. It
accepts only fixed server-rendered creation or correction action forms; there
is no second correction route and no request-controlled destination. The
creation target has no query, while correction targets contain only a fixed
action and opaque purpose proof. Bodies are bounded and strictly single-read
URL-encoded forms, and duplicate, additional, percent-reinterpreted, or
browser-selected identity and command fields are rejected. Exact Host and proxy prohibitions are checked
first and POST requires the exact same origin. Correction actions then validate
the durable session cookie, purpose-bound session CSRF, actor/session account
agreement, account-native principal, and authorized current profile before the
correction body is parsed or acted upon. Thus browser data cannot choose the
account, principal, binding, profile, current/base revision, correction target,
environment, actor, source ownership, idempotency identity, or redirect.

The frozen first-creation apply form contains only one 43-character
process-local artifact reference and one 43-character
purpose/artifact-bound CSRF proof. Its accepted authority, artifact claim,
sealed-command validation, and atomic create-once repository call are unchanged.
The response never changes cookies or exposes durable identifiers. Creation
returns `303` to `/find-matches`, same-artifact replay returns the same result
while its tombstone is live, and an unrelated create for an existing profile
returns `409`.

The runtime composes the authenticated matches integration inside the protected
profile integration and routes `/find-matches` through the durable browser
owner. A missing or closed capability fails within that owned route and cannot
fall through to the local product handler. Final confirmation uses the existing
private artifact issuance and completed-replay capabilities. The dispatcher
validates the existing review token, draft digest, schema, fields, authority
binding, and explicit confirmation before issuance. It renders only the opaque
artifact and artifact-bound CSRF proof in the fixed creation form. The sink and
`POST /account/profile` share the B2.4d creation service and process-local vault;
there is no artifact-issuance HTTP endpoint or durable draft/artifact table.

The registry stores an immutable identity-free Canonical V1 review projection.
It omits every `profile_id` occurrence instead of substituting a preview,
sentinel, null, empty, or reserved value. The projection's canonical bytes are
UTF-8 JSON with `ensure_ascii=True`, sorted keys, compact separators, and NaN
disabled. The hidden `profile_draft_fingerprint` is exactly the lowercase
64-hex SHA-256 digest of those bytes; complete canonical JSON is never placed in
the browser field.

Each exact original D0 confirmation identity advances through the bounded
states `DRAFT`, `ISSUING`, `MAYBE_ISSUED`, and `COMPLETED`. Only one issuance
owner exists, registry waits have a finite bound, and no registry lock is held
while a sink or callback executes. A definite failure before an artifact can
exist restores D0 exactly. If publication may have occurred, the state becomes
`MAYBE_ISSUED`; an exact retry must authenticate again and may perform only a
vault lookup for the same confirmation identity and account/session/environment/
purpose binding. It cannot issue another artifact. Once a valid immutable offer
exists, the registry records `COMPLETED` before response delivery. A response
write failure therefore leaves the original browser form able to request the
same completed offer. The registry snapshots the immutable completed record and
authority binding while locked, performs durable lookup-only session, account,
expiry/revocation, CSRF, environment, purpose, and ownership authorization with
the lock released, then returns the offer only after the identical record and
binding are rechecked under lock. This path never invokes issuance or durable
profile mutation; an authentication failure rejects only that request and leaves
the cached completion intact. Changed or genuinely stale forms remain rejected,
and concurrent identical submissions converge on the authorized result.
Authentication canonicalization
strictly validates every header and cookie first, then binds only the trusted
Host plus the validated session and session-CSRF cookie values; cookie order,
legal spacing, and unrelated cookies therefore do not split an exact session.
`MAYBE_ISSUED` and `COMPLETED` are retained for at most 600 non-sliding seconds
from the outcome. Expiry terminally removes both state and run, so the old D0 is
gone and cannot reopen issuance.

The artifact snapshot seals the exact account, session, environment, ownership
lineage, purpose, identity-free reviewed bytes, accepted time, and one fully
prepared repository command. `CreatePersistentProfileCommand.prepare()` alone
generates and binds the durable profile ID and supplies that same ID to the
private V1-to-V2 conversion builder; conversion has no independent generator,
selected identity, or placeholder. Profile, revision, and ordered source IDs are
generated once before vault publication and reused unchanged on every attempt.
Under the trusted, finite, non-reentrant internal-callback contract, the vault
retains an exact owner independently of the request stack until terminalization
or a one-shot protected exact-token release is verified across ordinary
production failures. This is not a claim about arbitrary trace-event exception
injection. Created, conflict, and
in-flight records consume the 64-record capacity until their original
non-sliding 600-second deadline; an active in-flight owner is never recovered by
expiry. A reconstructed runtime rejects an old pending reference with `410`,
while any profile already committed remains readable.

Fingerprint inputs are distinct and canonical: the structured hash covers the
complete durable Canonical V2 bytes, including its generated ID; the semantic
hash covers a typed V2 projection with `identity.profile_id` removed; each
source-content hash covers the source's exact UTF-8 bytes; the source-bundle hash
covers the versioned canonical ordered-source manifest; and the request
fingerprint covers the versioned canonical request envelope containing the
semantic and bundle hashes. The artifact content fingerprint separately covers
the canonical sealed account/session/environment/purpose and ownership-lineage
binding, hashes of the identity-free review and exact source material, accepted
times, server idempotency, and the already prepared command IDs and hashes.

Immediately before the repository call, the writable transaction revalidates
the artifact's sealed account row version, exact account-native principal and
environment, exact active owner binding/version, sole-owner status, and the
complete ordered ownership-event lineage. Transfer or replacement fails before
profile mutation. A proved rollback releases only the exact claim for retry;
an uncertain post-invocation outcome retains reconciliation authority and
reuses the same command for exact repository replay. A repository call that
commits but returns a malformed or locally unvalidated result shape follows the
same reconciliation path rather than being retired. Fatal pre-invocation content
or lineage inconsistency retires the artifact; ordinary cleanup interruption is
handled within the trusted internal-callback contract described above.

The server binds the one generated durable ID to the identity-free reviewed V1
projection only inside command preparation and converts it to Canonical V2.
Exact accepted About You UTF-8 text is immutable source ordinal 1; a
deterministic compact confirmed-correction JSON document is source ordinal 2
only when reviewed material changed. Raw sources are not embedded in Canonical
V2. Actor, reason,
normalizer/reviewer versions, timestamps, transient identifiers, and
idempotency are all server-owned. One successful creation adds only one
`product_profiles` row, one initial `product_profile_revisions` row, and one or
two `product_profile_sources` rows; the current-profile view reflects those
rows. Account, identity, invitation, session, ownership, event, alias, legacy,
and unrelated state does not change.

### Bounded durable profile correction

`Update profile` begins the distinct correction action namespace on the same
`/account/profile` route. The server first authorizes and reads the complete
current Canonical Profile V2, including its exact current revision, through the
accepted query-only profile boundary. It projects that trusted snapshot into an
identity-free review draft and reuses the accepted structured
draft/review/correct/confirm machinery. The `start`, `redraft`, `confirm`, and
`apply` actions use purpose-bound proofs; no hidden browser field contains an
authoritative V2 document, durable identifier, hash, source metadata, or base
revision. HEAD follows the same authority and representation decision as GET,
returns no external body, and remains write-free.

Drafts and immutable correction artifacts are bound server-side to the account,
environment, account-native principal, session, existing profile, exact base
revision and full base-V2 hash, and the distinct correction purpose. They are
process-local: the draft registry and artifact vault each have an independent
64-entry cap and non-sliding 600-second lifetime. Only bounded opaque
draft/review/artifact references and
integrity-protected proofs cross the browser boundary. A wrong account or session, expired or tampered
reference, wrong purpose, or restart-lost pending, uncommitted reference fails
without disclosing or changing durable profile state. After a successful
append, authenticated exact replay can deliberately converge from the durable
revision even after process-local expiry or runtime reconstruction.

Confirmation rebuilds and validates a complete corrected Canonical V2 snapshot
with the same server-authorized persistent profile ID. It constructs the full
ordered source bundle from the accepted reviewed-profile builder, including the
confirmed About You text and deterministic confirmed-correction JSON evidence.
The ordinary evidence remains one byte-identical source. If the complete
server-normalized JSON exceeds the installed per-source limit, correction alone
uses up to 15 ordered, hash- and length-bound fragments within the existing
16-source contract; exact reassembly recovers the ordinary JSON, and changed V2
provenance names every server-selected correction ordinal. B2.4d creation keeps
its frozen single-source behavior.
The server prepares the existing append command with revision kind
`correction`, the authorized expected-current revision number, the exact base
revision as `correction_of_revision_id`, fixed actor/reason/version values, the
accepted timestamp, and a stable idempotency key derived from the sealed
correction authority. Browser input supplies none of those values.

The existing `append_profile_revision` service performs the write in its
accepted transaction: it checks replay before stale state, verifies the
predecessor and contiguous revision number, appends one immutable full-V2
revision and its complete sources, and advances the current view atomically.
The profile container, persistent identity, account-native principal, owner
binding, and earlier revisions and sources do not change. Canonical V1 is never
persisted. Exact artifact replay converges on the same append, including after
runtime reconstruction. Changed replay conflicts; a stale base or competing
correction cannot overwrite the winner and leaves no partial rows.

Correction shutdown stops new consume admission and holds a vault-owned
operation token across both ordinary artifact claims and the durable replay
fallback. Close waits for admitted operations with a finite bound. If that
bound expires, cleanup remains unresolved and retryable and the vault retains
its records; after a successful close, no correction append can commit later.

Successful apply returns only `303 /find-matches`. That request performs a
fresh authenticated read of the newly current V2 and the frozen ephemeral-V1
matching projection against the configured query-only inventory. The accepted
B2.4d create-once and Authenticated Profile-to-Matches behavior remains
unchanged. Correction activates no archive, reactivate, deletion, rollback,
history, local-product, My Jobs, tracker, or pipeline operation.

For an existing authorized profile, match generation reads the current
Canonical Profile V2 and calls the accepted `project_v2_to_matcher_v1()` with a
fresh server-generated ephemeral matcher identity. The durable profile ID is
neither used as that identity nor exposed as browser input. The matcher queries
the same explicitly configured, schema-attested runtime SQLite database through
its query-only connection provider, loads active non-simulation inventory, and
constructs the checked-in metadata overlay explicitly. It never calls
`wahojobs.config.DB_PATH`, the workspace-default database, a legacy
`user_profiles` record, local pipeline state, or demo/`local_user` data. Missing,
empty, stale, or insufficiently trusted inventory produces an honest empty or
refresh-needed result without alternate inventory fallback.

Match GET and HEAD perform no durable, inventory, pipeline, job, profile,
session, ownership, or MatchRun mutation. Results reuse the existing ranking,
trust, exclusion, section, and result-cap helpers, but use a dedicated minimal
renderer. Only validated HTTP(S) opportunity URLs become application links, with
new-tab `noopener noreferrer` isolation. The page exposes no My Jobs, tracker,
dashboard, `/action`, mutation form, preview alias, demo persona, or legacy
profile selection.

`GET /logout` requires a valid current session and companion CSRF
credential and returns a fixed no-store confirmation form. `POST /logout`
accepts only the bounded strict form with exactly one `csrf` field, requires
the exact same-origin request policy and cookie/form equality, validates the
session CSRF, and revokes the current session in a short writable transaction.
Success clears the session and session-CSRF cookies and redirects only with
`303` to `/login`. A subsequent profile refresh requires authentication.

## Existing and invited identities

Identity resolution always starts with immutable Google provider and subject.
An existing active identity follows the established session path without an
invitation and never consumes an invitation that was presented at login start.
Only when that identity is absent may the private completion boundary use the
transaction-bound credential. The cryptographically verified Google result
must contain an authoritative verified email matching the email-bound
invitation. `AccountService.create_invited_user()` then atomically consumes the
invitation and creates the active account, Google identity, and account
lifecycle event before trusted-login completion issues the session. Later
logins resolve the same provider and subject and require no invitation.

Every durable completion then passes the canonical account through the private
`ensure_account_native_principal()` authority before constructing the trusted
login proof or invoking session creation. New accounts and existing identities
without a lineage receive one canonical account-native principal, active owner
binding, and initial event. Existing valid lineages are resolved without new
history or timestamp changes, including after reconstruction and concurrent
login attempts. Ownership failure is generic and precedes all session,
credential, CSRF, cookie, and response delivery. If invited provisioning had
already committed, the account remains valid and a later invitation-free login
may complete bootstrap. If session completion later fails, the valid lineage is
preserved for reuse.

Principal, binding, and event identifiers never enter OAuth material, URLs,
redirects, cookies, browser responses, logs, or account-oriented sessions, and
browser input cannot select the ownership environment or identity. The fixed
callback redirect to `/account/profile` is usable immediately after successful
ownership bootstrap: a newly provisioned account sees the authenticated empty
state until the user explicitly confirms reviewed About You content and submits
the resulting create form. Valid existing profiles remain readable without
mutation. Online invitation delivery, an operator administration UI, profile
editing or replacement, general identity linking or merging, legacy claiming,
and ordinary-runtime activation remain deferred. The separate offline
PB-OPS-1 protected-file boundary is documented below.

## Controlled test and development demo

The provider fixture is explicitly test/development-only:

```text
python -B scripts/durable_google_login_fixture_demo.py
python -B scripts/durable_google_login_fixture_demo.py --smoke
python -B scripts/durable_google_login_fixture_demo.py --smoke --restart-before-callback
```

Each run creates a fresh OS-temporary directory outside every checkout, a fresh
temporary SQLite database with explicit Migration-001 through Migration-007
installation, exactly one existing Google identity with its account,
account-native principal, persistent profile, and ownership relationship, and
deterministic test-only authorities stored under that directory. It serves
loopback HTTPS with a temporary certificate and private key.

The approval bridge remains local and controlled, while token and ID-token
handling still traverses the existing Authlib/Joserfc verification path through
`InMemoryGoogleTransport`. A network guard denies non-loopback sockets. The
smoke flow covers login, local provider approval, durable callback, protected
profile, authenticated refresh, CSRF-protected logout, and unauthenticated
post-logout refresh. `--restart-before-callback` closes and reconstructs the
runtime after durable start and before callback to demonstrate that the
transaction survives process-local state loss.

The demo never reads the Local workspace database or contacts Google. Cleanup
removes only its own temporary directory after the server and runtime close;
it does not delete a user-supplied path.

## Offline private-beta invitation operations (PB-OPS-1)

PB-OPS-1 is the only supported private-beta invitation operator surface. It is
offline and process-bounded; it does not start the durable browser runtime, a
listener, TLS, a route, an OIDC provider, a crawler, a matcher, or an
application server. The exact grammar is:

```text
python -B scripts/private_beta_invitations.py [--json] create \
  --config ABSOLUTE_CONFIG \
  --database ABSOLUTE_DATABASE \
  --invitation-key-file ABSOLUTE_KEY_FILE \
  --request-id OPAQUE_REQUEST_ID \
  --expires-at YYYY-MM-DDTHH:MM:SSZ \
  --credential-output ABSOLUTE_NEW_FILE

python -B scripts/private_beta_invitations.py [--json] status \
  --config ABSOLUTE_CONFIG \
  --database ABSOLUTE_DATABASE \
  --invitation-key-file ABSOLUTE_KEY_FILE \
  --invitation-id inv_<32-lowercase-hex>

python -B scripts/private_beta_invitations.py [--json] revoke \
  --config ABSOLUTE_CONFIG \
  --database ABSOLUTE_DATABASE \
  --invitation-key-file ABSOLUTE_KEY_FILE \
  --invitation-id inv_<32-lowercase-hex>
```

`--json` is accepted only before the operation. There are no defaults,
environment fallbacks, workspace-database fallbacks, inferred targets, email
arguments, email files, email environment variables, stdin fallbacks, list,
search, export, resend, renew, import, consume, or bulk operations. Request IDs
match `[A-Za-z0-9][A-Za-z0-9._~-]{15,127}` and enter the durable namespace
`pb-ops-1:create:v1:<request-id>`. Expiry is whole-second Zulu UTC only.

### Triple selection and provenance boundary

The invocation's local root of trust has two explicit parts: the executing
project/package root and the pinned, strictly validated version-1 configuration
with `environment == "private_beta"`. The operations module must itself be an
absolute, link-free source under that root. Every loaded `scripts`, `tests`, or
`wahojobs` module with a source file must resolve link-free into the same root,
so mixed import roots fail closed. This works unchanged in an exact archive
extraction with no repository metadata.

The configuration's canonical database and
`account_invitation_lookup_key_file` paths are authoritative. The explicit
database and invitation-key arguments are mandatory operator cross-confirmations
and must canonically name those same files. Configuration, database, key,
PB-OWN coordination, output parent, stage, and final identities are pinned and
revalidated at their operation boundaries; unsafe links, reparse points,
aliases, metadata, sidecars, replacements, and identity overlaps fail closed.
Configuration, database, key, and credential output must be outside the
executing project root.

PB-OPS performs no Git operation and does not consult `PATH`, the current
directory, `.git`, worktree metadata, or `GIT_*` interpretation to establish
authority. This boundary therefore does not discover unrelated clones or prove
deployment authenticity, historical freshness, or backup lineage. It also
cannot detect a complete, internally consistent configuration/database/key
bundle substituted before invocation and explicitly selected by the operator.
Stronger provenance requires an external trust root and remains deferred.

### Ownership and database attestation

The order is fixed:

1. parse and validate the nonsecret command structure;
2. open and pin the exact configuration, validate its complete version-1
   syntax without a database descriptor or SQLite open, require private beta,
   and cross-confirm the database and key paths;
3. validate target identities and, for create, the private output parent;
4. for create, obtain the email twice from the native controlling terminal
   while echo is disabled, normalize both entries, and require an exact match;
5. acquire the accepted PB-OWN-1 capability with `offline_operator`, immediately
   authenticate it, and revalidate every pinned identity;
6. only then open the exact database directly (`mode=ro` plus `query_only=ON`
   for status, `mode=rw` for create/revoke); and
7. attest the exact open path and identity, all seven migration markers, the
   complete closed schema, account schema, Migration 006, foreign-key
   enforcement, empty temp schema, rollback-journal mode, integrity via
   `quick_check(1)`, referential integrity via `foreign_key_check`, and forbidden
   sidecar absence.

Create and revoke acquire `BEGIN IMMEDIATE` before mutation. Create reads key
bytes only after that writer acquisition; status and revoke validate the key
file identity but never open or read it and never perform an HMAC. A create
retry may recover SQLite's exact same-database regular rollback journal left by
abrupt pre-commit death while PB-OWN is held: SQLite handles an authentic hot
journal, while the operator identity-checks and retires an ignored zero-magic
non-hot journal. WAL, SHM, master/super journals, malformed rollback journals,
and all sidecars on status or revoke remain forbidden.

Every cursor, transaction, connection, database descriptor, key handle, output
handle, and directory handle must be terminal before authentic PB-OWN release.
The database identity and sidecar-free state are then checked again. No result
is published until release succeeds; close, identity, cleanup, or release
failure suppresses success.

Each close owner retains its stored handle or capability until close succeeds
or terminal closure is independently proven. Close attempts are bounded, and
one failure never suppresses an attempt for another authority. If a database
session remains live, PB-OWN remains held. A process-owned retained-cleanup
coordinator keeps that exact session and ownership capability, plus any live
output or pinned-configuration authority, reachable after the public cleanup
error. A later invocation drains the exact retained cleanup first, closes the
database session before releasing PB-OWN, and only then may open new resources.
No cleanup depends on a finalizer or garbage collection.

The internal operation state records, independently, no irreversible action,
database commit attempted/confirmed, credential publication
attempted/confirmed, ownership release attempted/confirmed, and result delivery
attempted/confirmed. Each attempted flag is set before the corresponding
commit, publication, release, write, or flush can become externally durable.

### Create, protected credential, replay, and recovery

The normalized email is never persisted. Migration 002 stores its domain HMAC
and display hint. Source metadata has exactly these semantics:

```json
{
  "configuration_binding_sha256": "<environment/config/database/key-target binding>",
  "operator_protocol": "pb_ops_1_create_v1",
  "output_binding_sha256": "<canonical-final-path binding>"
}
```

The authoritative domain fingerprint binds that email HMAC, exact expiry,
fixed operator actor/protocol, stable private-beta target binding, exact output
destination, and protocol version. It is computed only by
`invitation_creation_request_fingerprint()`. Inside the outer immediate
transaction, no request row creates one invitation through the existing
`create_invitation()` savepoint; an exact key/fingerprint replays; a changed
fingerprint returns `REQUEST_ID_CONFLICT` without mutation. Descriptor numbers,
inode/file IDs, and process objects are not stable binding inputs.

The new credential file is canonical bounded JSON plus exactly one newline:

```json
{
  "configuration_binding_sha256": "<stable-target-binding>",
  "email_hint": "p***@example.test",
  "expires_at": "2026-09-01T12:00:00Z",
  "format": "wahojobs-private-beta-invitation-v1",
  "invitation_credential": "inv_<public-id>.<raw-secret>",
  "invitation_reference": "inv_<public-id>",
  "output_binding_sha256": "<canonical-output-binding>",
  "request_fingerprint": "<stored-semantic-fingerprint>",
  "request_id_sha256": "<request-id-digest>",
  "recovery_tag": "<domain-separated-hmac>"
}
```

The deterministic stage is
`<parent>/.pb-ops-1-<sha256(canonical-final-path)>.pending`. It is created
exclusively, hardened, completely written, flushed, closed, reopened,
authenticated, and revalidated before SQLite commit. After commit it is
published in the same directory by a native atomic no-replace operation,
reopened and authenticated as the final file, and durably flushed according to
the platform contract. Invitation creation linearizes at SQLite commit;
credential delivery linearizes at no-replace publication; operator success
linearizes only after terminal cleanup and authentic PB-OWN release.

Retries converge as follows:

- before commit, SQLite rolls back; an authenticated inactive stage bound to
  the same semantic request may be reclaimed, while unrelated or
  unauthenticated content is never overwritten;
- after commit and before publication, the authenticated stage is the sole
  credential authority and an exact retry publishes it;
- after publication and before reporting, an exact retry authenticates the
  committed row and final envelope and reports redacted replay success;
- a committed row with neither its bound final file nor an authenticated stage
  is `CREDENTIAL_RECOVERY_UNAVAILABLE`; no replacement credential is generated
  or printed; and
- one output path coordinates across databases as well as within one database:
  only the process that holds the exclusive stage/final name may commit a row
  bound to that destination.

An unrelated final or stage is never replaced. Protected output publication
and any authenticated recovery complete while PB-OWN remains held.

The POSIX hard-link fallback has one additional authentic crash state: after
the no-replace link and before stage unlink, the exact stage and final names can
refer to the same inode. This pair is admitted only provisionally so committed
recovery can authenticate it; ordinary `_validate_output_file()` continues to
require link count one. Recovery requires both exact deterministic names,
regular no-follow files, identical device and inode, link count exactly two,
no third hard link, effective-user ownership, mode `0600`, equal bounded size,
an unchanged authenticated parent directory, an authenticated envelope bound
to the same request/fingerprint/target, and the matching committed pending row.
It then removes only the stage name, durably flushes the directory, reopens and
authenticates the final, and requires link count one. Any mismatch fails closed
without deleting either name.

### Native output guarantees

On POSIX, the pre-existing output parent must be owned by the effective user
and private; path components cannot be unsafe links or writable aliases. Stage
creation uses directory-relative exclusive/no-follow primitives, mode `0600`,
regular-file/owner/link-count/identity checks, file `fsync`, directory `fsync`,
and native same-directory no-replace publication. When
`renameat2(RENAME_NOREPLACE)` is unavailable, a directory-relative no-follow
hard link creates the final name without replacement and the exact stage name
is then unlinked. Exact retry recovers both the two-link interruption and the
post-unlink/pre-directory-flush interruption described above.

On Windows, the output must be on a supported fixed NTFS/ReFS volume. UNC and
mapped-remote paths, alternate data streams, reparses, unsafe identities, and
non-private ACLs are rejected. The parent and credential use a protected
private DACL; creation uses `CREATE_NEW` and non-inheritable handles; file ID,
type, link count, DACL, size, and path identity are checked; file writes are
flushed; and publication uses same-directory no-replace `MoveFileExW` with
write-through. Windows does not expose the POSIX directory-`fsync` contract, so
PB-OPS does not claim one.

These controls do not defend against the same OS identity, administrators or
root, backup privileges, malware, unsupported/network filesystems, or power
loss beyond the native primitives just listed.

### Redacted results, errors, and exit codes

Create success exposes only operation/outcome, invitation reference, email
hint, expiry, and effective status. Status adds `created_at`; revoke returns the
same redacted identity/state fields. No output contains a request ID, target or
credential path, full email, raw credential, key/HMAC material, internal row
identifier, or exception detail. Status selects one exact invitation and never
joins user, identity, profile, principal, or ownership data.

The complete success payload is rendered in memory before any byte is emitted,
bounded to 4096 bytes, written as one record, flushed, and only then confirmed.
Human mode is:

```text
PB_OPS_1_SUCCESS_V1 bytes=<payload-bytes> sha256=<payload-sha256> payload=<redacted-fields>
```

JSON mode is one canonical object with `frame == "pb_ops_1_success_v1"`, the
whitelisted `payload`, `payload_bytes`, and `payload_sha256`. Only a complete
valid frame together with exit 0 is success.

Before success emission and on ordinary application-controlled failures,
stdout is empty. Stderr is one fixed code line, or one compact JSON object when
`--json` was selected. A non-pending revoke may add only its effective status
from `pending`, `expired`, `consumed`, or `revoked`. A hostile stdout stream can
accept an irreversible prefix before raising; that prefix is not a complete
valid frame, cannot be rolled back, and is never followed by a second stdout
record. A stream can also accept the complete record and fail its flush; the
nonzero exit still means the record is not success.

Once database commit or credential publication may have occurred, any
unconfirmed delivery, cleanup interruption, or post-release interruption is
`COMMITTED_RETRY_REQUIRED`/8 rather than `INTERNAL_FAILURE`. Its redacted human
or JSON notice states only that durable mutation or publication may already
have occurred, the result is not an ordinary failure, the exact same invocation
must be repeated, exact retry is the only supported recovery, and success
requires a complete frame plus exit 0. When applicable it also reports the
separate fact `cleanup=INCOMPLETE`; it never includes paths, email, request ID,
database identity, credential, authority value, or exception detail. If no
durable mutation was possible, a nonterminal cleanup failure is instead
`CLEANUP_INCOMPLETE`/7 and overrides the primary validation or business error.

```text
0  success, exact replay/recovery, status, or successful revoke
2  syntax/input, terminal, email, request, reference, expiry, or output form
3  configuration, target, identity, schema, integrity, or key-source failure
4  retryable PB-OWN or SQLite contention
5  request conflict, unknown invitation, or non-pending revoke
6  credential destination, publication, or recovery failure
7  incomplete cleanup, lost ownership, or sanitized internal failure
8  committed or published state may exist; repeat only the exact invocation
```

PB-OPS-1 adds no schema or migration, browser/runtime route, administrator UI,
deployment authorization, managed secret distribution, backup proof, or key
rotation/retirement workflow. The existing deployment, certificate, provider,
monitoring, abuse-control, and key-rotation exclusions remain unchanged.

## Explicit limitations and deferred operations

PB-OWN-1 remains only the server-private lifetime-ownership primitive. PB-OPS-1
uses its `offline_operator` role but derives database and invitation authority
from the separately pinned private-beta configuration and complete attestation;
the role alone grants no product or database authority. Neither milestone adds
a schema, migration, administrator interface, or deployment surface.

This milestone does not provide:

- public/non-loopback serving, proxy trust, certificate deployment, or
  production TLS operations, public signup, or public deployment readiness;
- online or bulk invitation creation, list/search/export, resend, renew,
  browser administration UI, or credential delivery beyond PB-OPS-1's one
  protected offline file, general identity linking or account merge, or profile operations beyond the
  bounded same-profile correction flow, including replacement, archive,
  reactivate, purge, deletion, rollback, or revision-history UI;
- My Jobs, tracker or pipeline actions, match history, or MatchRun persistence;
- scheduled opportunity refresh or new crawler/source activation;
- database initialization, seeding, repair, or automatic migration; Migration
  007 remains an explicit offline operator action;
- a client cookie-receipt acknowledgement protocol;
- a cleanup scheduler, reconciliation scheduler, or automated key rotation;
- production secret distribution, managed key custody, retained-key retirement,
  incident response, observability, rate limiting, or availability operations;
  or
- a claim that successful `end_headers()` proves browser receipt or
  persistence.

It also does not provide a transparent in-process recovery guarantee when a
trusted internal sink or callback remains blocked forever, formal recovery from
asynchronous exceptions injected at every Python instruction boundary, or a
formal shutdown guarantee when trusted internal code holds a lock forever.
Process restart or a new browser flow remains an accepted operational recovery
for such process-local stalls. Cryptographic run-ID collision is outside the
practical threat model, and the confirmation cache may outlive the artifact by
the small interval between their clock samples; artifact consumption remains
authoritative. The accepted B2.4d create-once and replay contracts remain
frozen, as does the accepted Authenticated Profile-to-Matches contract. The
bounded correction flow does not activate any later profile or lifecycle
operation.

Production topology, certificates, secret custody, controlled key rotation and
retirement, provider credentials, operational monitoring, abuse controls,
cleanup scheduling, and deployment authorization remain separate hardening
and review milestones.

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
Importing the browser, runtime, launcher, or fixture modules performs no
database, filesystem-secret, network, route, or environment side effect.

Production activation is unsupported. This milestone supports exactly the
`development`, `test`, and `private_beta` environment names under the strict
local policy below. It introduces neither Migration 007 nor an insecure-cookie
mode.

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
single-linked, free of SQLite sidecars, and unavailable to another writer.
The runtime seals that file identity, keeps a raw descriptor pinned across each
SQLite connect-and-verify boundary, opens it explicitly in existing-file mode,
and requires the byte-exact stored SQL plus closed base and Migration-001
through Migration-006 marker inventory. Every later connection verifies the
same sealed file before application SQL. After constructing the browser
integration, startup repeats the writer, identity, sidecar, integrity, marker,
and exact-schema checks before publishing the runtime. Startup never
initializes, seeds, repairs, checkpoints, or migrates the database.

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
commits authorization start and performs production-equivalent cleanup before
it is reaped; process B then reconstructs configuration, authorities, runtime,
descriptors, and connections from explicit files and completes the callback
with the network-disabled synthetic provider. Separate abrupt-exit schedules
prove rollback before the start commit and recovery of one prepared row after
the commit. No runtime object, connection, descriptor, socket, lock, thread, or
in-memory authority crosses the process boundary. The B2.1 database/restart
contract itself adds no migration or automatic migration and makes no
multiprocess callback-competition guarantee. Additive retained-key rollover is
the separate clean-reconstruction contract below.

## Activation, readiness, and shutdown

The dedicated launcher uses one all-or-nothing activation sequence:

1. completely validate nonsecret configuration;
2. resolve the database, secret-file, key-file, and external TLS-workspace
   identities and policies;
3. load secret material into private construction buffers;
4. attest the already migrated database;
5. construct the gateway, lookup/protection authority, connection owner,
   profile integration, and browser integration privately;
6. register cleanup ownership, then construct an inactive, unbound server with
   an inert handler and immediately attach it and its concrete listener to that
   owner;
7. build the TLS context and configure explicit per-connection TLS;
8. publish the dedicated handler only after HTTPS construction;
9. repeat the database and secret identity attestations;
10. bind the listener;
11. activate it; and
12. start the owned serve thread, wait boundedly for its first successful
    serving-loop checkpoint, hold that first iteration on a bounded two-party
    decision, atomically claim readiness against shutdown and serve failure,
    publish readiness, and serve.

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
TLS workspace, and finally restore prior signal handlers. The request-thread
and serve-thread drain bounds are two seconds per cleanup attempt. A live
thread, connection, listener, socket, or other failed close remains a
fixed-category unresolved entry. A later cleanup call retries only unresolved
entries; terminal resources are not closed twice. Ordinary and explicitly
named control-flow cleanup failures cannot skip another cleanup action or
replace an earlier startup/request failure.

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
Cleanup is synchronous and retryable; it does not depend on daemon threads,
finalizers, garbage-collection callbacks, or a background scheduler.

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

The dedicated integration owns these paths completely:

| Path | Accepted methods | Purpose |
| --- | --- | --- |
| `/login` | `GET` | Fixed sign-in page and start-CSRF creation |
| `/auth/google/start` | `POST` | Durable authorization preparation |
| `/auth/google/callback` | `GET` | Terminal durable callback completion |
| `/logout` | `GET`, `POST` | Confirmation and CSRF-protected revocation |
| `/account/profile` | `GET`, `HEAD` | Existing owned profile read |

An unsupported method on an authentication path returns `405` with the fixed
allowed-method declaration. The dedicated launcher enables exclusive
fall-through rejection, so unrelated ordinary application routes are not
reachable through this browser surface. Responses use bounded fixed HTML,
escaping, `Cache-Control: no-store`, a default-deny CSP, `Referrer-Policy:
no-referrer`, and `X-Content-Type-Options: nosniff`.

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
request value. Existing bounded callback parsing and state lookup remain
authoritative.

The browser callback order is security-significant:

1. parse the callback state under the accepted gateway contract;
2. atomically and terminally claim the durable authorization transaction;
3. release the authorization-transaction write lock;
4. compare the claimed transaction identity with
   `__Host-wahojobs_google_tx` in constant time;
5. only for a matching binding, perform provider traffic and verification,
   identity lookup, proof issuance, and B2D1 completion; and
6. only for an exact B2D1 `issued` result, prepare the browser-session delivery
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

## Protected profile, refresh, and logout

After accepted delivery, the callback redirects only with `303` to the fixed
`/account/profile` destination. There is no request-controlled redirect. The
protected profile opens a fresh query-only connection or snapshot,
authenticates with the accepted `wahojobs_session` parser, authorizes ownership,
and renders the existing escaped, CSP-protected profile. The authenticated page
provides fixed profile and logout navigation. A refresh with the active
session remains authenticated; unauthenticated access offers `/login`.

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

This slice does not create an account-native product principal, bind the new
account to a principal, or create a persistent profile. The fixed callback
redirect remains `/account/profile`, but that surface is not usable for the
newly provisioned account until the later principal-binding slice. Invitation
delivery, operator administration UI, general identity linking or merging,
and ordinary-runtime activation remain deferred.

## Controlled test and development demo

The provider fixture is explicitly test/development-only:

```text
python -B scripts/durable_google_login_fixture_demo.py
python -B scripts/durable_google_login_fixture_demo.py --smoke
python -B scripts/durable_google_login_fixture_demo.py --smoke --restart-before-callback
```

Each run creates a fresh OS-temporary directory outside every checkout, a fresh
temporary SQLite database with explicit Migration-001 through Migration-006
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

## Explicit limitations and deferred operations

This milestone does not provide:

- production runtime activation or production credential integration;
- public/non-loopback serving, proxy trust, certificate deployment, or
  production TLS operations;
- invitation delivery or administration UI, general identity linking or
  account merge, or principal/profile/ownership provisioning for a newly
  invited account;
- database initialization, seeding, repair, automatic migration, or
  Migration 007;
- a client cookie-receipt acknowledgement protocol;
- a cleanup scheduler, reconciliation scheduler, or automated key rotation;
- production secret distribution, managed key custody, retained-key retirement,
  incident response, observability, rate limiting, or availability operations;
  or
- a claim that successful `end_headers()` proves browser receipt or
  persistence.

Production topology, certificates, secret custody, controlled key rotation and
retirement, provider credentials, operational monitoring, abuse controls,
cleanup scheduling, and deployment authorization remain separate hardening
and review milestones.

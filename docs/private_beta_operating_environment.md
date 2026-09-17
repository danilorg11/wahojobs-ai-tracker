# Private beta operating environment v1 — operator procedure

This procedure belongs to the isolated milestone. External execution requires the
single [authorization package](private_beta_external_authorization.md). Nothing
here authorizes touching the recovery application on 8802, accepted demo on 8861,
public www/root site, or any existing real database.

## Runtime and configuration contract

Use Ubuntu 24.04, Python 3.12 and `requirements.lock`. `deploy/private-beta` is for
one dedicated host. Persistent local storage is required; never run this composition
in an ephemeral function or with two database owners. Caddy terminates public TLS
and connects to **127.0.0.1:8870**. Exact Host plus a random internal proxy token are
required. Caddy removes Forwarded, X-Forwarded-*, Via, X-Real-IP and X-Original-Host;
the app rejects their presence rather than deriving authority from them. Existing
origin, session, CSRF and invitation checks remain in force. Caddy's default
forwarding behavior is explicitly overridden. [Caddy reverse proxy documentation](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy).

`runtime.example.json` is an intentionally unusable template. Version 2 requires
`runtime_mode=remote_beta`, a lowercase exact HTTPS DNS origin without port/path,
and exact `/auth/workos/callback`. The client/API key belongs to the chosen WorkOS
environment. Use independently generated 32-byte invitation and proxy keys; encode
the invitation key as base64 and proxy token as 64 lowercase hex characters.
Neither is an account identity or a user-facing login shortcut.

The declared capability set is manual profile entry/confirmation, accepted Matches
and application guidance, My Jobs and saved drafts/history. Upload/extraction routes
are blocked before reading a body. Model preparation/enrichment has no configured
producer or budget. No practice accounts, impersonation or fake provider transport
can be selected through runtime configuration. Forbidden ambient practice/extraction
switches or `OPENAI_API_KEY` make activation fail. Configuration, schema, ownership
or secret failure happens before binding the application listener. Startup never
migrates storage or starts source maintenance.

Local development retains the accepted version-1 local HTTPS configuration and
synthetic fixture tooling. Owner rehearsal uses version 2 + WorkOS Staging +
`rehearsal-001`. Real beta requires WorkOS Production, newly generated secrets and
separate fresh `beta-001` storage. Never promote the synthetic/rehearsal database
to real candidate storage. Real WorkOS staging email is visibly marked STAGING;
local FakeWorkOSBoundary tests are labelled offline and verify no real identity.

## Exact release and initial installation

After batch approval, create a release archive from the feature commit named in
the final closure, using `git archive`, and retain its SHA256. Transfer that archive
to the new host only. No `.env`, local database, `.git`, node_modules, practice
state, existing backup or personal configuration belongs in the archive. The
reviewed Git archive contains test fixtures but no runtime switch imports them.

Provision Python 3.12/venv and Caddy using the selected Ubuntu/official Caddy package
repositories; record versions. Install script prerequisites before running it.
Extract into `/tmp/wahojobs-beta-stage/<full-commit>` and verify the archive hash.
Only on the approved dedicated host, create the root-owned marker
`/etc/wahojobs-beta-dedicated-host`. Run as root:

```sh
bash /tmp/wahojobs-beta-stage/<full-commit>/deploy/private-beta/install.sh \
  /tmp/wahojobs-beta-stage/<full-commit> <full-commit>
```

`install.sh` refuses an existing `/opt/wahojobs-beta` or `/etc/wahojobs-beta`.
It installs root-owned release code, a per-release hash-locked virtual environment,
the dedicated `wahojobs-beta` OS identity and systemd unit. It does not create
storage, start a listener, issue certificates or register provider callbacks.
Do not use this fresh-install script as an upgrade script.

As that **same OS identity**, explicitly initialize new rehearsal storage:

```sh
cd /opt/wahojobs-beta/current
sudo -u wahojobs-beta .venv/bin/python -B scripts/private_beta_storage.py \
  --new-directory /var/lib/wahojobs-beta/rehearsal-001
```

The command executes supported M001–M011 migrations and exact attestors, refusing
an existing destination. `FRESH-READY.json` records zero accounts, invitations,
jobs and executions. Only repository company metadata is initialized. No source
snapshot, demo job, profile or prior execution ledger is imported.

Populate a private complete runtime input file through the owner's secret-manager
or protected host editor; never echo values into shell history. Keep it mode0600.
Run `private_beta_configure.py` as root with that input and a **new final operator
directory** `/var/lib/wahojobs-beta/operator-config-001`. It creates private runtime,
Caddy environment, raw invitation key and a provider-neutral invitation operator
config with exact absolute paths. Example paths below contain no secrets:

```sh
.venv/bin/python -B scripts/private_beta_configure.py \
  --input /root/wahojobs-beta-runtime-input.json \
  --new-directory /var/lib/wahojobs-beta/operator-config-001
install -d -m 0700 /etc/wahojobs-beta/config-001
install -m 0600 /var/lib/wahojobs-beta/operator-config-001/runtime.json /etc/wahojobs-beta/config-001/runtime.json
install -m 0600 /var/lib/wahojobs-beta/operator-config-001/caddy.env /etc/wahojobs-beta/config-001/caddy.env
chown -R wahojobs-beta:wahojobs-beta /var/lib/wahojobs-beta/operator-config-001
```

Retire the two redundant runtime/proxy files in that just-created operator directory
through the owner's approved secret custody procedure; retain invitation.key and
invitation-operations.json there. Do not rename/copy the operator directory: its
configuration pins the key's absolute path. PB-OPS and maintenance must run as
`wahojobs-beta`, because coordination files and private credential outputs require
the same effective OS owner as the runtime. Do not run them as root against an
app-owned lifetime lock or chown a live database to make an operation pass.

Root installs `Caddyfile` at `/etc/caddy/Caddyfile` only on this fresh dedicated
host and `caddy-beta.conf` at `/etc/systemd/system/caddy.service.d/beta.conf`.
Use mode0644 for these **secret-free** templates. Systemd's EnvironmentFile reads
the root-only Caddy secret file; the application receives its root-only JSON via
LoadCredential. Validate Caddy and the service files using the correct protected
environment without printing the expanded configuration (it contains the proxy
token). Source no arbitrary shell file; the generated env has only validated
hostname/hex fields. Run `systemctl daemon-reload` after installation.

## Explicit start, stop and health

Only after exact DNS/callback/TLS authorization, start the application then Caddy:

```sh
systemctl start wahojobs-beta.service
systemctl is-active wahojobs-beta.service
systemctl start caddy.service
```

`ExecStartPost` retries the private readiness endpoint up to 20 times, checking
real database availability with exact Host/token; Caddy returns404 for `/_ops/*`.
Readiness does not claim WorkOS or inventory availability. Verify public TLS and
the hosted login separately. After successful owner acceptance, explicitly enable
only these two services for boot (`systemctl enable wahojobs-beta caddy`). They run
independently of Codex terminals or the owner's computer. No source/model timer
is installed. A real host reboot test remains mandatory external evidence.

Normal stop for any offline operation:

```sh
systemctl stop wahojobs-beta.service
systemctl is-active wahojobs-beta.service
```

Require inactive plus no surviving unit processes. Confirm identities by unit/cgroup
and exact command, not historical PID. The process stops accepting requests, joins
bounded request workers, closes services and releases the native lifetime lease.
If close is nonterminal, ownership remains quarantined; systemd ultimately stops
the whole cgroup after its timeout. Do not delete coordination locks or bypass
blocked ownership. A forced termination can leave SQLite sidecars; preserve them
and investigate under the existing schema/storage contracts before restart.

After an offline operation, restart the same selected service and rerun private
readiness and the owner journey. Caddy may show an unavailable upstream while
maintenance runs; no second serving process is started to hide the outage.

## Invitations and account operators

While stopped, use the existing `scripts/private_beta_invitations.py` create,
status and revoke commands as `wahojobs-beta`. Version-2 invitation-only config
does not require obsolete Google credentials. The WorkOS flow uses the same
invitation key; new accounts require a verified provider email and a matching
unconsumed invitation. Subsequent login uses the existing active account identity.
Email is entered twice in its hidden terminal prompt; token output goes to a
new mode0700 app-owned private credentials directory, never standard output.

```sh
sudo -u wahojobs-beta .venv/bin/python -B scripts/private_beta_invitations.py create \
  --config /var/lib/wahojobs-beta/operator-config-001/invitation-operations.json \
  --database /var/lib/wahojobs-beta/rehearsal-001/product.sqlite3 \
  --invitation-key-file /var/lib/wahojobs-beta/operator-config-001/invitation.key \
  --request-id <unique-owner-approved-request-id> --expires-at <UTC-expiry> \
  --credential-output /var/lib/wahojobs-beta/credentials/<new-file>.json
```

Use the same three target arguments for `status`/`revoke`, with `--invitation-id`
instead of creation arguments. PB-OPS requires the complete success frame and exit0.
A consumed/uncertain request is not a new invitation allowance: retry **the exact
invocation**, same request ID and credential output. Stop on any nonterminal cleanup.
The operator privately supplies the token through the existing login form. No
email-delivery integration or second account system is introduced.

Data access/closure uses [data operations](private_beta_data_operations.md). Source
refresh uses [source operations](private_beta_source_operations.md). Every one of
these commands holds the same exclusive offline ownership model.

## Backups, restore and physical relocation

Cases are different. **A: fresh beta** has no historical execution ledger; create
it through the fresh command, never by stripping an old database. **B: existing
storage** includes its exact confirmed profiles, source bindings, workflow/history,
draft companion, optional preparation companion, original maintenance pin, journals,
consumed/uncertain attempts and relocation history. Treat them as one recovery set.

With all writers stopped, run the existing cold snapshot command as `wahojobs-beta`:

```sh
.venv/bin/python -B scripts/beta_recovery.py backup \
  --database /var/lib/wahojobs-beta/rehearsal-001/product.sqlite3 \
  --destination /var/lib/wahojobs-beta/backups/<new-snapshot> \
  --code-commit <full-commit> --configuration-revision config-001
.venv/bin/python -B scripts/beta_recovery.py verify \
  --snapshot /var/lib/wahojobs-beta/backups/<new-snapshot>
```

Create app-owned private `backups` parent first. If a preparation companion is
ever approved, pass its exact path with `--companion`; omission must not silently
exclude configured evidence. This declared fresh beta has none. SQLite exclusive
transactions additionally exclude nonparticipating product/draft/companion writers
during copy/handoff. Unexpected sidecars, schema or companion state fail closed.
Record nonsecret release/config revision labels; secret files and lifetime locks
are not part of the snapshot. Verify checksums before encrypting or copying.

After separate bucket/key approval, archive and encrypt a complete cold snapshot
with an owner-controlled `age` recipient before upload to the private bucket.
Keep the decryption key outside the Droplet and Git. Use a protected scoped Spaces
credential profile, private object ACL and HTTPS, no CDN. Record ciphertext hash,
snapshot manifest hash, bucket/key/version and source release in the private
operator receipt. Download a copy, verify ciphertext hash, decrypt to a new
private directory and rerun `verify`. This is a manual operation; no unattended
backup job or retention deletion is installed. A provider machine image alone is
not a coherent application snapshot or an execution-history reconciliation.

Restore never overwrites a file. It immediately creates a recovery hold before
copying and blocks both runtime and maintenance acquisition:

```sh
.venv/bin/python -B scripts/beta_recovery.py restore \
  --snapshot /var/lib/wahojobs-beta/backups/<snapshot> \
  --destination /var/lib/wahojobs-beta/rehearsal-recovered-001
.venv/bin/python -B scripts/beta_recovery.py reconcile-relocation \
  --snapshot /var/lib/wahojobs-beta/backups/<snapshot> \
  --destination /var/lib/wahojobs-beta/rehearsal-recovered-001 \
  --authoritative-database /var/lib/wahojobs-beta/rehearsal-001/product.sqlite3
```

The authoritative source must still be present at the **same physical identity**
and equal the snapshot in every protected database/journal/history byte. Supply
`--authoritative-companion` when the snapshot declares one. A newer source, changed
journal-only reservation, missing source, wrong companion, unsafe destination or
conflicting lineage keeps recovery held. Source retirement is durably written only
after the entire destination's files/directories have been persisted. Activation
then binds one destination's physical identity. Retrying that exact handoff can
finish an interruption; a different target cannot steal the retired source.

Original pin and historical receipts remain byte-identical. The appended lineage
authorizes the new journal root without rewriting source/profile/model bindings.
All inherited lineage files must remain present and match their content hashes.
Consumed plans return their immutable result, and new enrichment plans still see
consumed reservations. Invalid/stale semantic evidence stays invalid/stale.

After success, stage a new private runtime configuration selecting the recovered
path, preserve config-001, update LoadCredential to the new revision and stage a
matching invitation operator configuration for that path. Validate/start exactly
one runtime and perform owner continuity checks. The old source stays retired.
Preserve the original invitation lookup key and proxy token during this relocation;
changing a database path is not a key rotation. A separately intended rotation
must update the exact runtime/invitation key and Caddy environment together and
account for old invitations. Never regenerate keys merely by copying the template.
Do not move the recovered file again: inode/path changes require another supported
snapshot/reconciliation, not an edited receipt.

**Disaster limit:** if the authoritative disk/history has been lost, a backup may
omit later consumed operations, revoked sessions or candidate changes. There is
no safe fact-free merge/reset procedure. This implementation deliberately blocks
activation rather than asserting reconciliation. Preserve the backup and any
surviving authoritative ledgers for a separately approved recovery design. Record
RPO/RTO as undecided; do not sell this bounded relocation as total-host-loss failover.

## Migration, upgrade, rollback and bounded logs

No migration runs on app startup. The new beta begins at M011; each later schema
change requires a separately reviewed explicit wrapper, stopped runtime, exact
prerequisite attestation, cold snapshot and disposable migration rehearsal. This
milestone adds no schema migration and authorizes none on the owner's real data.

For a code-only upgrade, stage a new root-owned release directory and its own
hash-locked venv, attest the existing selected schema with the new runtime, stop
the unit, replace only `/opt/wahojobs-beta/current` with the new release symlink,
then start/readiness/journey checks. Preserve prior release and config revisions.
Code rollback uses the same procedure with a previously tested schema-compatible
release. An older schema snapshot is not automatically safe to restore; the recovery
hold/reconciliation still applies. There is no automatic down-migration.

Each application start creates a private run directory with three bounded diagnostic
files (256 KiB each). At 32 run families the launcher refuses another start until
an operator applies the approved archive/retention policy. This bounds accumulation
instead of silently deleting evidence. Caddy request/error logging is discarded
to avoid callback query/header leakage; application diagnostics are allowlisted.
Systemd receives only fixed ready/stopped/failure categories, with rate limits.
On this dedicated host configure journald SystemMaxUse=64M and RuntimeMaxUse=16M;
review resulting log behavior and disk headroom during real rehearsal. Never add
HTTP access logs that include query strings, cookies, submitted bodies or tokens.
Log and backup retention periods remain owner/legal choices, not defaults in code.

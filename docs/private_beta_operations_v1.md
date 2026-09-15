# Wahojobs private beta operations v1

This is the operating contract for the release candidate. It authorizes no real
runtime, source request, account invitation, model call, migration or restoration.
The rehearsal uses disposable synthetic storage and controlled transports.

## Declared capability and runtime configuration

The supported integration is **WorkOS AuthKit MagicAuth email code**, existing
Accounts/session ownership and durable candidate product composition. The
supported launcher is `scripts/workos_authkit_staging_app.py`; the strict current
configuration accepts only namespace `private_beta`, HTTPS origin
`https://127.0.0.1:8443`, matching `/auth/workos/callback`, and an explicit existing
database outside Git. This is a local staging composition, not a remotely
accessible hosting configuration. The isolated owner demo uses the existing
labelled fixture composition on a separate port; it is not this live provider.

| Capability | Declared beta scope | Evidence and activation dependency |
|---|---|---|
| Invited registration / return | WorkOS verified-email MagicAuth, exact WorkOS subject binding; no email account linking | Real SDK integration code + controlled synthetic tests. Real WorkOS delivery, dashboard/callback configuration and fresh invited/returning identities still need separately authorized verification in intended hosting. |
| Sessions / logout | Existing secure session and CSRF cookies, idle 1 hour / absolute 8 hours proposed config, explicit logout | Existing ownership, replay, origin, stale-version and lifecycle tests. Unfinished WorkOS logins expire in 10 minutes or disappear at process restart; start again. |
| Profile | Manual creation, review, explicit confirmation, corrections and resumable drafts | Authenticated synthetic journey and browser/HTTP evidence in release index. Confirmation does not establish employer verification. |
| Document extraction | **Disabled** in the selected capability set | Existing configured OpenAI adapter and offline parser/model fixtures remain. Real-model quality, processing policy and paid budget not verified here. Ensure extractor-related environment configuration is absent in the actual process; do not infer disabled state from missing UI alone. |
| Matches and details | Confirmed evidence + preserved accepted employer sources; explicit unknown/conflicting conditions and freshness | Representative release cohort is separately indexed. Historical snapshots are not current vacancies. |
| Saved/application/reminder/hidden history | Normal existing exact-posting workflow and in-app reminders | Candidate manually tracks actions. No automatic employer application, email reminder or notification delivery. |
| Professional-background preparation | Consume configured existing companion; disabled preparer | Optional `professional_background_companion` contains explicit path, exact model and basis; real data uses `semantic_model_output`, only labelled fixtures use `offline_labelled_stub`. No automatic regeneration or model client in consumption. |
| Source maintenance | Explicit offline `scripts/evidence_maintenance.py` operations | Supported source cohort currently Alignerr/Mercor; other existing providers are not silently refreshed. Plans do not grant provider/model/write budget. |
| Read-only Jobs catalog / company / exact pages | Existing `/jobs`, `/company/<slug>` and `/job/opportunity-<id>` routes, with existing availability rules | Candidates can browse when Matches is limited. Public employer content remains read-only; personalized assessments and workflow actions retain owner authorization. These existing local routes do not authorize deployment or publication. |
| Registry-ID routing canary / public publishing | Registry-ID canary remains disabled by omitted/empty `public_job_canary_ids`; no new publishing activated | This gate controls registered public-ID routes only. It does not disable the existing catalog, company pages or legacy exact routes above. |
| Account lifecycle / privacy | Existing consent/lifecycle ledgers and service-level revocation/deletion requests | No complete self-service account purge or backup erasure guarantee. Owner-approved policy, contact route, retention and executable handling of requests are real-use gates. |
| Backup / recovery | Explicit stopped-writer cold snapshot; restore into a new directory only | Includes product, declared companion, correction sidecar, complete journal and immutable pin. Recovered maintenance is held by original physical identity; no pin rewrite. |
| Hosting / TLS | Supported local HTTPS rehearsal only | Public hostname, trusted TLS, process supervision, secret custody, private filesystem permissions, capacity and real access verification require intended-environment evidence. No production-TLS weakening is supported. |

Use the exact fields documented in [the supported launcher](workos_authkit_staging.md).
Keep the existing pinned Python 3.12 requirements. No package upgrade is required.
Secrets belong in separately permission-restricted external configuration/vault
storage. The backup tool never loads that file or copies keys. Record a nonsecret
configuration revision and the full code commit alongside every snapshot.

## One maintenance procedure

For a 5–20 invitation cohort, inspect before the first candidate session of each
operating day and after every explicit maintenance run. The recommendation is a
manual operator routine; no scheduler exists. Inspect every selected provider's
verification age, complete versus partial observation, accepted full detail,
pending repair and candidate coverage. The current engine's 72-hour freshness
boundary makes more than three days without successful observation a meaningful
availability limitation, not proof that omitted positions closed.

Choose a short announced maintenance window with no candidate writers. Record the
selected process handle/source/config revision; ask candidates to return after
the window. Stop only that exact process using its normal shutdown. Confirm it
exited and released the database lifetime owner. Never delete the persistent
`.wahojobs-lifetime.lock`, kill an unknown process, or contact the recovery app.

1. Take and verify a cold snapshot using the commands below. Select the actual
   configured companion explicitly; do not omit it just because preparation is
   disabled today. Correction drafts and an existing maintenance journal/pin are
   automatically included. Ensure the private snapshot has sufficient disk space
   and operator-only OS permissions; Windows inherits the existing parent ACL.
2. `inspect` and `plan` with explicit database, providers, phase, journal and
   request/detail limits. Review expected endpoint, source/write scope, held
   observations, current bindings, missing input and budget separately.
3. Execute only the approved phase/grants. Alignerr needs official catalog GET
   pages and optionally selected official detail GETs; Mercor uses one partial
   catalog GET. Choose the actual request ceiling after inspection. Source-only
   work has no paid-model authority. See [maintenance](evidence_maintenance_v1.md)
   for the exact commands and per-provider bounds.
4. Inspect the durable receipt and re-inspect source/derived state. A partial,
   failed, interrupted, held or awaiting-repair result stays explicit. A new
   provider observation can invalidate an owner comparison without erasing it.
   If model/owner repair is worthwhile, create a separate bounded derived plan
   with exact owner/session, model, token/request/cost authority. This package
   contains none of those future grants.
5. Restart the same selected supported launcher/config only after successful
   ownership and integrity checks. Verify login, a fresh Matches view, exact
   detail and My Jobs. Keep original receipts and failed attempts. Record actual
   verification timestamps; simulated clocks never become current freshness.

After interruption, `evidence_maintenance.py recover --journal <PATH> --plan-id
<ID>` validates the original receipt/hash chain without new requests. Reexecuting
the same plan returns the recorded result. A source retry needs a fresh inspected
plan. Do not reset companion attempts or model reservations, stitch partial
snapshots, move the journal, or edit the database pin.

## Backup and restore commands

Run in the delivered feature checkout with the approved Python environment.
All destinations below must be new absolute directories outside Git with an
existing private parent. These are command templates for a separately authorized
real operation; only disposable rehearsals were executed for this release.

```powershell
python -B scripts/beta_recovery.py backup --database "C:\operator-data\inventory.sqlite3" --companion "C:\operator-data\professional-background.sqlite3" --destination "C:\private-backups\beta-before-window-001" --code-commit <FULL_COMMIT> --configuration-revision beta-config-v1
python -B scripts/beta_recovery.py verify --snapshot "C:\private-backups\beta-before-window-001"
python -B scripts/beta_recovery.py restore --snapshot "C:\private-backups\beta-before-window-001" --destination "C:\operator-data\recovery-001"
```

Backup requires exact supported M008–M011 product schema, SQLite integrity and
foreign keys, rollback-journal mode, no live sidecars and exclusive participating
offline ownership. Configured companion schema and existing journal hash chains
are checked. File hashes/identities are compared across capture; a changed source
cannot receive a completion marker. All files are copied with exclusive creation
and fsynced. A retained partial directory without `COMPLETE.sha256` or
`RECOVERY-READY.json` is not success. Correct the cause and choose a new directory;
do not reuse or clean the failed evidence automatically.

Restore never overwrites the original database, lock or active storage. It verifies
the snapshot before creating the new directory and checks every copied file.
The recovered product is `product.sqlite3`; correction drafts use its exact
derived sibling name, and the companion is `companion.sqlite3`. Candidate and
source rows, profiles, accounts, session/CSRF hashes, workflow history, entitlements,
consumed attempts, companion store identity/generation and draft owner bindings
remain byte-identical to the snapshot. Raw secret configuration is absent.

**Recovery activation is a separate reviewed action.** Select the recovered
product/companion paths in a new private configuration, retaining the matching
authentication environment and authority keys through the approved secret store.
Review any writes made after the snapshot: never activate a snapshot that would
revive a revoked session/deleted account or reset consumed entitlements/attempts.
There is no automated reconciliation of post-snapshot writes. The disposable
rehearsal has no legitimate post-snapshot writes to lose.

The original maintenance pin is copied byte-for-byte and still names the original
physical database and journal. Ordinary application consumption can restart on
the restored database. **Further maintenance on recovered storage fails closed**
with `maintenance_journal_database_identity_changed`; the copied journal remains
available for read-only report/recover. A separately designed, reviewed and
authorized disaster-relocation/reconciliation operation would be needed to resume
maintenance on that new physical identity. This tool does not fabricate that
authority or pretend the recovery is fully operational for ongoing maintenance.

## Code/configuration rollback

For an application-code regression with intact storage, normally stop the exact
selected process, switch the *new runtime's* working directory to the recorded
previous immutable checkout, and select its recorded external configuration
revision. Keep product, companion and journals in place. This release adds no
product migration; both versions must still pass their existing exact schema and
companion attestors. Launch using the previous checkout's approved interpreter:

```powershell
python -B scripts/workos_authkit_staging_app.py --config "C:\operator-config\beta-config-v1.json"
```

Recheck `/login`, normal authentication/return and the selected read/write journey
before reopening the cohort. The recorded disposable rehearsal executes archived
accepted baseline/configuration v1, candidate/configuration v2, exact baseline/v1
rollback, and candidate/v2 return in separate guarded processes. It preserves the
same physical product, companion, draft and immutable journal/pin, checks profile,
Matches, exact detail, My Jobs and Applied/history, and proves offline ownership
can be reacquired after normal runtime shutdown. It operates no current owner
configuration. See the release OPERATIONS evidence for exact source/configuration
hashes and receipts. If a prior binary cannot attest current storage, stop and retain
the failure. Do not downgrade schemas or restore an older database merely to get
the old process to start. Real key/config custody and code rollback on the actual
host remain part of separately authorized staging acceptance.

## Failed-journey investigation

Enable file diagnostics explicitly for the selected runtime. From its delivered
checkout, create a new run directory under an existing operator-only parent and
pass it to the launcher. These are templates for separately authorized staging;
the paths below are examples and were not created for this milestone.

```powershell
New-Item -ItemType Directory -Path "C:\operator-logs\beta-run-001"
python -B scripts/workos_authkit_staging_app.py --config "C:\operator-config\beta-config-v1.json" --diagnostics-directory "C:\operator-logs\beta-run-001"
```

Windows inherits the existing parent's permissions; use a private parent. The
launcher refuses a directory with an existing `requests.jsonl` log family. Pick
a new directory for each launch, retaining previous logs for incident comparison.
The log family is bounded to three files of at most 256 KiB each. Omitting
`--diagnostics-directory` keeps only the internal bounded memory ring: file
logging is disabled, and that ring is not a candidate or operator HTTP endpoint.

When investigating a failed request, obtain its `X-Wahojobs-Request-ID` response
header and search the selected run's log family locally:

```powershell
Get-ChildItem -LiteralPath "C:\operator-logs\beta-run-001" -Filter "requests.jsonl*" | Select-String -SimpleMatch "<RESPONSE_REQUEST_ID>"
```

The matching record contains only request ID, UTC timestamp, method category,
route category, response status, bounded duration and outcome category. An
interrupted delivery is labelled separately from an integration/service failure.
The log does not contain the submitted profile, invitation or authentication
credentials. Log-sink failure does not alter session delivery, so absence of a
record requires a process/log-storage check rather than a claim that no request
occurred.

Use the response request ID and bounded sanitized diagnostics described in the
release security evidence. Record time, operation category, status and exact
release/configuration revision. Never collect a browser Cookie/Authorization
header, callback URL query, invitation, raw profile or employer application data.
Inspect existing internal reconciliation tools under operator ownership; there
is no public run-registry or owner-bypass endpoint. Check selected process health,
port ownership, database ownership and last maintenance receipt independently.
Preserve a failed invocation and correction; a passing retry does not identify
the cause of an earlier 503.

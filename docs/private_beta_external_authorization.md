# Private beta operating environment v1 — consolidated external authorization

**Prepared scope, not an execution receipt. No external change has been made.**
Approve or amend this package as one batch. The delivered source, independent
reviews and final test results are identified in the milestone evidence bundle's
`CLOSURE.md`. Main integration and push remain outside this authorization.

## Recommended topology and reuse

One **new dedicated DigitalOcean Basic Regular Droplet**, Ubuntu 24.04 LTS,
1 shared vCPU / 2 GiB RAM / 50 GiB local SSD, NYC3; one IPv4 address; one
Caddy service and one supervised Python 3.12 process. Persistent SQLite and its
draft/journal/lineage dependencies live together on that host's persistent disk.
There are no replicas, autoscaling workers, shared network SQLite, managed database,
background crawlers or new orchestration platform. Maintenance stops the app and
takes its existing exclusive offline ownership lease.

Reuse `deploy/digitalocean-preview`'s accepted Caddy/loopback Python/systemd
pattern and the existing WorkOS AuthKit SDK, account/lifecycle, migrations and
Evidence maintenance v1 contracts. `deploy/private-beta` supplies the scoped
replacement packaging for this **separate** host, not for the public site.

Historical `docs/production_origin_preview.md` records a 2026-08-21 Vercel guest
catalog preview (`wahojobs-hybrid-preview-jobs.vercel.app`, deployment
`dpl_8TFev5Jaj351avRuSTmZ8qm2rkTa`; origin `dde3be…`, gateway `ae262…`). Those
artifacts are real prior work, not proof that their resources still exist or can
host private sessions. The Vercel gateway intentionally forwards only a small
guest route set. Vercel functions have no durable shared SQLite filesystem.
[Vercel's SQLite guidance](https://vercel.com/kb/guide/is-sqlite-supported-in-vercel).

Proposed sole hostname: **`beta.wahojobs.com`**. No www/root route, existing
application, Vercel project, public-site origin, wildcard DNS or old preview is
changed. Existing account/workspace/domain ownership is checked only after approval.
No existing droplet is assumed safe to reuse; fresh host isolation is deliberate.

## Resources, cost and bounded changes

Prices checked against official documentation on 2026-09-17; taxes, account credits,
regional stock and availability remain unverified. Creation must stop if the
specified plan or ceiling is unavailable; no automatic upgrade/substitution.

| Item | Proposed action | Recurring base price |
| --- | --- | --- |
| Basic Regular Droplet above | Create one new dedicated host | US$12/month cap, advertised US$0.01786/hour; 2 TB bundled transfer |
| Spaces Standard | One private NYC3 bucket `wahojobs-private-beta-backups-20260917`, CDN disabled, narrowly scoped access key, encrypted manual snapshots | US$5/month base; 250 GiB storage / 1 TiB outbound included |
| WorkOS AuthKit | Reuse owner workspace if available; its **Staging** environment for owner rehearsal only | AuthKit base free up to 1 million MAUs; no paid add-ons |
| DNS + Caddy TLS | Existing domain zone, one beta A record; Caddy ACME certificate for exact beta hostname | No separate paid DNS/TLS product proposed |
| Model/extraction services | No keys, budgets, calls or paid features | **US$0; zero calls** |

Proposed infrastructure base ceiling **US$17/month**, excluding taxes and any
owner-approved overage. Keep stored encrypted snapshots below 20 GiB and total
rehearsal egress below 10 GiB; stop and report instead of exceeding these operational
ceilings. They are operator limits, not a provider billing hard cap. Billing persists
for an allocated powered-off Droplet. Do not select DigitalOcean's separate uncapped
resource-based plan. No automated host backups, bucket lifecycle rules or schedules
are included. [Droplet plans](https://www.digitalocean.com/pricing/droplets),
[billing details](https://docs.digitalocean.com/products/droplets/details/pricing/),
[Spaces pricing](https://docs.digitalocean.com/products/spaces/details/pricing/),
[WorkOS pricing](https://workos.com/pricing).

The batch authorizes these exact external operations **only after owner approval**:

1. Inspect the relevant DigitalOcean project/account, WorkOS workspace/environment
   and authoritative DNS zone metadata to verify ownership, permissions, conflicting
   hostname and quoted resources. Do not inspect real application databases or
   repurpose existing hosts. If the beta name already serves something, stop.
2. Create the dedicated host and private bucket, owner SSH access, and firewall:
   TCP 80/443 public; TCP 22 only the owner's current approved administrator address;
   Python 8870 and Caddy admin 2019 loopback only. Do not publish an AAAA record
   without separately matching the host/firewall IPv6 configuration.
3. Install the reviewed exact release archive and hash-locked dependencies, Caddy,
   systemd unit and private config. Create only new rehearsal storage through explicit
   M001–M011 migration commands. Never mount, upload or copy the 8802 recovery database,
   accepted synthetic demo storage, personal profile/history or their credentials.
4. Add only `beta.wahojobs.com A <new-host-ip>` in the existing zone. Install the
   reviewed Caddy site and request a legitimate ACME certificate for that hostname.
   The TLS issuer needs public DNS and ports 80/443 to reach this host; no wildcard
   challenge or root-domain cutover. [Caddy automatic HTTPS](https://caddyserver.com/docs/automatic-https).
5. Configure only WorkOS **Staging**: exact sign-in/application origin
   `https://beta.wahojobs.com`, exact redirect
   `https://beta.wahojobs.com/auth/workos/callback`; hosted AuthKit email one-time-code
   authentication; no wildcard callbacks, social providers, SSO, organizations or
   custom WorkOS domain. Preserve unrelated callback entries. Application logout is
   the existing local CSRF-protected session revocation and redirect to `/login`;
   do not promise sign-out from every WorkOS/browser session. Use the official SDK
   exchange and its verified-email result. [WorkOS environments](https://workos.com/docs/authkit/environments),
   [authorization URL](https://workos.com/docs/reference/authkit/authentication/get-authorization-url).
6. Generate new beta-only invitation and proxy keys in the protected host/secret
   manager. Load the selected WorkOS client/API key privately; never paste secrets
   into chat, command arguments, Git, receipts or screenshots. Stage files with
   `private_beta_configure.py` at their final immutable path. Root-only config is
   delivered to the app using systemd credentials. Caddy receives only its proxy
   token and hostname. Retain an owner-controlled encrypted secret recovery copy
   separately from snapshots.
7. Create at most **two owner-controlled test invitations/accounts** in rehearsal
   storage, privately selecting the workspace owner's verified email(s). Deliver
   no other invitations. Permit at most ten interactive WorkOS login/email-code
   attempts to those owner addresses; no email marketing or bulk delivery. Expiry
   is 24 hours for these rehearsal invitations; session policy is one-hour idle /
   eight-hour absolute. These are proposed technical rehearsal settings, not final
   legal retention periods.
8. Execute the two approved source-only plans in [source operations](private_beta_source_operations.md):
   Alignerr ≤120 HTTP attempts including ≤20 details, Mercor ≤1; **121 total**, no
   retry/redirect allowance, no other provider or model calls. Save raw evidence,
   original observation times and acceptance/lifecycle reports. No historical jobs
   or synthetic opportunities are imported into externally visible inventory.
9. Perform the owner-only acceptance, clean stop/restart, coherent cold snapshot,
   encrypted private off-host upload/download verification and exact-current
   relocation rehearsal below. No timer or recurring maintenance is installed.

## Owner decisions to batch with approval

- Confirm/amend the exact hostname, NYC3 location and US$17/month base ceiling,
  including private encrypted Spaces storage and its narrowly scoped credentials.
- Confirm WorkOS Staging, owner-only addresses selected privately after approval,
  the two-account/ten-login/24-hour-invitation scope and new separate rehearsal data.
- Approve the **121-request source ceiling and zero-model budget**, accepting that
  current usefulness can fail this bounded rehearsal without forcing recommendations.
- Name the operator/contact channel, permitted data access, hosting/data-location
  and processor disclosures, and retention approach described in [data operations](private_beta_data_operations.md).
  Final retention/legal wording can remain unresolved for owner-only rehearsal;
  it is a hard gate before other real candidates. Choose how manually encrypted
  backup keys are kept and who can restore them. No key is requested in conversation.
- Accept the documented availability limit: ordinary restart and exact-current
  lossless cold relocation are supported; **loss of the authoritative disk/ledger
  leaves an older backup held**. Do not interpret off-host backup possession as
  proof that missing consumed history has been reconciled. If disaster recovery
  with a defined RPO/RTO is required before any real candidate, keep invitations
  closed until an approved authoritative-ledger custody/reconciliation design is
  implemented and exercised. No destructive reset/override is proposed here.

## Owner rehearsal and rollback acceptance

Use the hosted origin and real provider; offline fixtures do not satisfy this gate.
Verify trusted HTTPS, exact DNS/host routing, clean runtime/config attestation,
anonymous catalog/privacy isolation, no practice entry or upload/model path, and
uninvited account denial. Verify invited verified-email login, duplicate/expired
callback failure, wrong-origin POST rejection, logout/re-entry, two-account
isolation, manual profile review/confirmation and saved workflow after a supervised
restart. Capture sanitized response/status receipts without auth queries/cookies.

Inspect actual provider receipts and useful fit evidence for the intended profiles,
without requiring a fixed Matches count. Show uncertainty and employer links under
the accepted product behavior. Stop the service and take a cold snapshot with all
dependent stores/journal/history; verify encrypted off-host round-trip bytes;
restore to another new local path while current authoritative storage still exists;
reconcile the exact snapshot and retire the old path before activating the new
one. Recheck session/profile/workflow, consumed plans, current evidence and readiness.

On failure: stop only this beta service/Caddy, revoke unused beta invitations and
test sessions using the existing lifecycle operators if needed, withdraw only the
beta DNS record if exposure is unsafe, preserve evidence/storage and owner access.
Code rollback selects a previous immutable release **only if its schema attestors
accept the present database**. Never overwrite the database with an older backup,
delete a pin/journal/fence or claim an older binary downgrades M011. Resource
destruction, real account erasure, public-site changes and wider source requests
remain outside this batch.

## Evidence still required before invitations to candidates

Real cloud permission/resource checks; actual trusted TLS; real WorkOS owner login
and email delivery; boot/service/firewall behavior on the host; hosted restart and
recovery acceptance; verified current Alignerr/Mercor evidence and useful honest
candidate coverage; named support/contact and approved data policies; decision on
the authoritative-ledger-loss limitation; then separate WorkOS **Production**
configuration with fresh real-beta storage and a reviewed invitation list. WorkOS
documents Staging for tests and Production for real users; environment credentials,
settings and callbacks are separate. This approval batch does **not** authorize
inviting candidates or silently promoting Staging accounts into Production.

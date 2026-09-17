# Private beta operating environment v1 — living execution plan

Authority: isolated local implementation, disposable tests, official documentation,
independent review and feature commits only. No main integration, push, cloud
inventory, SSH, DNS/TLS issuance, provider/model calls or real data access.

Baseline: `226183748acb251d21729ac596146cd7d56364d1`, tree
`4d5d38c766d48cd96c2b225e89b92374135c3ad5` verified. Accepted closure, real-beta
gate, source operations and activation operations read from the named September 15
evidence bundle. Its local acceptance explicitly excludes remote origin support,
real WorkOS verification, current sources and maintenance after relocation.

One evidence bundle: sibling `operating-evidence/`; no private recovery files copied.
Protected listeners independently identified by netstat and process metadata:
8802 PID 22748 recovery-session.py --serve; 8861 PID 35004 prior guarded beta-demo.py.
No HTTP request, stop, source/config change or database access to either.

## Steps and status

1. **Complete:** recover historical DigitalOcean/Caddy/systemd and Vercel
   contracts; verify current official capabilities/pricing without account access.
2. **Implemented:** strict external HTTPS runtime using the existing WorkOS composition,
   narrow proxy ingress, invitation/account/session controls and explicit disabled
   extraction/preparation/practice capabilities.
3. **Implemented:** explicit fresh migrations, coherent cold backup/restore and append-only
   maintenance relocation/reconciliation with consumed/uncertain history preserved.
4. **Prepared:** single-host deployment, process supervision, release/rollback, data
   handling and manual source refresh. No public-site changes or new framework.
5. **Release verification:** affected development checks pass on Windows; final
   accepted regression plus new affected selections run from the frozen candidate.
   Ubuntu 24.04 container supplies target-family Python/SQLite, Caddy/systemd static
   checks and actual POSIX tests. See the one evidence bundle's final CLOSURE.md and
   machine-readable receipts for completed status; a container is not hosted TLS,
   WorkOS verification or a booted service. No test assertion is replaced by fixtures.
6. **Reviewed and repaired:** separate security, recovery and operating reviews.
   Repairs include ownership retention even after partial activation, exact M011
   invitation compatibility, private staged secret modes, Caddy environment-log
   suppression, inherited recovery history and durable destination-before-fence
   ordering. Review files in the evidence bundle record precise scope/hash limits.
7. **Prepared:** one consolidated external authorization package in
   private_beta_external_authorization.md. Final handoff identifies release source,
   validation disposition and all remaining invitation gates.

## Decisions recovered so far

Prior public preview: persistent single DigitalOcean host, loopback Python,
Caddy HTTPS and systemd. Vercel forwards only an exact guest catalog route set;
it is unsuitable as this product's SQLite process owner. Prior resources and
credentials are not verified or authorized for use. Reuse architecture and
packaging conventions, leaving all public routing untouched.

## Validation record

Historical accepted baseline: 2,198 IDs, 2,195 passes and three Windows POSIX-fork
skips. Development Windows checks cover remote origin/auth, fresh M011 setup,
profiles after restart, accounts, lossless relocation and existing operators.
The reused isolated runner was extended for POSIX dir_fd and Python stdin child
semantics; its guard permits only synthetic storage and non-protected loopback.
Failed development receipts are retained, including a Debian/SQLite3.40 schema
attestation failure. The selected target is Ubuntu24.04/SQLite3.45, not that image.
No new local demo was substituted for the hosted milestone.

## Explicit operational limit

Ordinary restart and exact-current cold relocation are implemented. A stale backup
or missing authoritative disk/ledger remains held, including runtime and dispatch.
No pin reset, discarded reservations or claim of total-host-loss recovery is made.
Authoritative-ledger-loss recovery and the owner's availability/retention policies
are explicit gates in the external package, alongside real TLS, WorkOS and sources.

# Beta account and data operations

The initial externally rehearsed capability set is manual profile creation and
confirmation, accepted recommendations/guidance and saved workflow. The runtime
serves `/privacy` and links it from login. Its technical explanation says that
employer applications happen outside Wahojobs and the Wahojobs profile is not
automatically sent to an employer. It is not a claim of legal compliance.

## Actual data map

| Data | Storage/access in this declared beta | Disclosure / handling |
| --- | --- | --- |
| Identity | WorkOS subject, verified email, linked account and account lifecycle in SQLite; WorkOS separately processes authentication/email codes | Provider exchange uses official SDK. Wahojobs does not store a user's WorkOS password or provider access/refresh tokens for this flow. Operator can inspect selected account for support. |
| Invitations | Email-derived lookup material, redacted display hint, status/expiry/consumption and request ledger; secret invitation credential in restricted output file | Existing PB-OPS create/status/revoke only. No public account list or automatic invitation email sender. |
| Sessions/CSRF | Hashed session/CSRF authority and expiry/revocation records; secure browser cookies | HttpOnly applies to session cookie; companion CSRF cookie supports existing CSRF contract. Host-only Secure cookies, fixed HTTPS origin, exact same-origin mutation checks. Local logout revokes the Wahojobs session. |
| Confirmed profiles | Confirmed canonical profile, source text, revisions, exact principal/profile bindings | Used for matching and guidance; not automatically sent to employers. Candidate-facing access stays owner scoped. Operator has privileged recovery/support access. |
| Drafts | Saved manual and correction drafts in the adjacent supported draft store; some in-memory review state can expire | Saved drafts persist across restart; unconfirmed work is not promoted to confirmed authority. Include the adjacent store in snapshots/export scope. |
| Workflow/history | Saved postings, status transitions, notes, in-app reminders, profile/workflow identity bindings | No automatic email reminders or employer submission is enabled. Preserved through normal restart and approved lossless relocation. |
| Uploaded files / model payloads | Upload/extraction and paid preparation are disabled in remote beta | No real-beta upload corpus, model request or preparation companion is created. Old rehearsal fixture data is not imported. |
| Public-source evidence | Provider responses, accepted/held captures, lifecycle runs, deterministic derivations and immutable maintenance receipts | Source-only information retains actual capture/acceptance provenance. Public source bodies are not re-labelled freshly verified historical snapshots. |
| Logs | Private bounded route/status/duration/request-ID diagnostics and fixed service events | No submitted profile/body, callback query, cookie, provider token or private exception dump. Access restricted to operator. No analytics/advertising integration added. |
| Backups and exports | Private full cold recovery set; owner-approved encrypted off-host copies; selected-account JSON export | May retain historical personal data after an account is closed. No instant removal-from-all-backups promise. Keys/config are managed separately. No current-phase real export or deletion is authorized. |

## Supported operator requests

Use the existing account/lifecycle services through `scripts/private_beta_accounts.py`.
Stop the selected runtime, verify the requester's identity through the established
private support channel, map the exact account ID using existing account records,
and operate under the same `wahojobs-beta` OS identity. Do not accept a claimed
email/profile ID from an unrelated caller as authorization. No data is sent by this
tool; delivering an export to someone is a separate explicitly approved action.

- `export --database <absolute-db> --user-id <usr_id> --new-directory <new-private-dir>`
  creates one private JSON with that account's identity/lifecycle, invitations and
  session summary (no credentials), exclusive account-native profile/revisions/
  source text, workflow/history and saved manual/correction drafts. It rejects
  ambiguous/shared ownership rather than exporting another candidate's information.
  Backups/logs and disabled upload/model services need separate policy handling.
- `suspend --database <absolute-db> --user-id <usr_id> --expected-version <version>
  --request-id <unique-request>` uses existing lifecycle concurrency/idempotency
  and revokes sessions. It blocks access without claiming erasure.
- `request-closure --database <absolute-db> --user-id <usr_id>
  --expected-version <version> --request-id <unique-request>
  --cooling-days <approved-positive-days> --purge-days <approved-positive-days>`
  records the existing deletion lifecycle request and revokes access. There are
  deliberately **no retention defaults**. It returns `data_erased=false`. This
  milestone adds no scheduled purge executor and promises no completed erasure.

Use `status --database <absolute-db> --user-id <usr_id>` in the private stopped
operator session to obtain that account's lifecycle status and row version;
the response contains no email/profile or credentials. Do not dump the entire
user table into logs or chat.
Keep request/identity-verification receipts privately and use exact idempotent
retry if outcome is uncertain. The tool does not alter WorkOS account state or
send email. WorkOS-side access/data requests are separately scoped provider actions.
Restart and verify closed/suspended sessions cannot access private routes.

## Decisions that must precede candidate invitations

The owner/legal reviewer must choose and publish a feasible policy; this repository
does not supply legal conclusions or invent final periods:

| Decision | Technically feasible options and consequence |
| --- | --- |
| Operator/contact and access | Name the responsible operator and private support channel; restrict privileged access to that operator or explicitly approved support staff. More staff means broader private-data access requiring controls and disclosure. |
| Data location/processors | Approve proposed NYC3 host/bucket and WorkOS processing; alternatively select an available supported region before resource creation. Provider contractual terms and cross-border implications require owner/legal review. |
| Profile/draft/account retention | Keep data while active and perform a reviewed manual closure workflow, or define a shorter explicit retention policy and implement its executor before promising automatic deletion. Current closure records a request and blocks access; it is not physical purge. |
| Cooling/deletion deadlines | Choose positive cooling/purge parameters compatible with the existing account lifecycle. Record what happens to workflow/source bindings and immutable operational evidence before any purge. Do not advertise a deadline without an executable, tested procedure. |
| Backup retention and access | Manually retain a bounded number/size of encrypted snapshots for approved periods, or separately authorize lifecycle automation after testing. A retained backup still contains older data; destruction trades recovery history for removal. Never remove needed execution ledgers simply to satisfy an unreviewed cleanup. |
| Logs and invitation credentials | Approve archive/deletion periods and credential delivery/retirement method. Current app stops at 32 bounded log families; it does not silently delete them. Invitation expiry/revocation is not proof every privately delivered token copy was erased. |
| Disaster availability | Accept fail-closed service loss when no current authoritative ledger survives, or require a separately reviewed durable ledger custody/reconciliation solution and recovery targets before invitations. A stale backup must not resurrect revoked access or consumed operations. |

Before a real candidate joins, replace unresolved policy wording in `/privacy`
with approved contact/retention/processors text reflecting implemented behavior.
No checkbox or legal-certification claim is fabricated. Any new automated erasure,
export delivery, real invitation or provider-side deletion requires its own clear
scope; none has happened in this implementation phase.

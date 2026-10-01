# Candidate account and My Jobs V1

This integration lets public catalog visitors explicitly save a job, record that
they applied, hide a job, correct an Applied statement, and restore hidden jobs.
The existing My Jobs workspace renders All, Saved, Applied and Hidden views.
Anonymous reading and employer Apply remain available without an account.

## Accepted lineage and reuse

The backend starts from accepted live
`ee9b8b48695663a60c4bbd96d598d9e4ff7aceb4`, including the accepted DataForce and
Surge publication timing fix. The website starts from
`245f4243977ba0a64436253482ad8e65fbba5d39`, preserving contextual employer access
and the Work Smarter retirement. Neither repository's stale main is the release
base. The historical workflow-only reference is
`995dea0900a52bb456407b74d0108b5da609ef59`; only necessary workflow transitions
and presentation changes were selected, without merging its ranked branch.

Reused components include WorkOS AuthKit Hosted UI/Magic Auth and the installed
WorkOS Python SDK 10.2.0, PKCE and verified-provider completion, durable account
and browser-session services, the pipeline action/record/state/history engine,
discovery restoration, existing My Jobs cards/styles, the prepared anonymous
catalog reader, primary-database lifetime ownership, and native inventory and
backup/recovery mechanisms. No new matching or authentication engine is used.

## Public scope and sessions

The website exposes `/my-jobs` and only these candidate routes:

| Route | Methods | Purpose |
| --- | --- | --- |
| `/candidate/login` | GET, HEAD | Candidate benefit, support/policy links and email sign-in form |
| `/candidate/auth/start` | POST | CSRF-validated Hosted UI initiation |
| `/candidate/auth/callback` | GET | One-use verified WorkOS completion and session delivery |
| `/candidate/state` | GET | Private state for a bounded list of catalog identities |
| `/candidate/intent` | POST | Explicit tracking intent, authenticated or preserved through login |
| `/candidate/action` | POST | Authorized existing-history actions |
| `/candidate/resume` | GET, POST | Read continuation, then one-use CSRF-validated mutation |
| `/candidate/logout` | POST | Revoke the current candidate application session |
| `/my-jobs` | GET, HEAD | Actual personal workspace or candidate sign-in benefit |

The fixed upstream is `https://beta.wahojobs.com/_candidate`, protected with a
separate relay key. The anonymous `/_catalog` key cannot authorize candidate
operations. The backend separately authenticates and authorizes each account.
Its private beta gate also rejects candidate sessions copied into beta cookies.
No candidate route grants profile editing, Matches, ranking or other beta scope.

Production subjects are namespaced `production:<client_id>:<verified_user_id>`.
They never link by email to Staging, another Production client or NextAuth. Public
registration is enabled only at this verified Production entry. Staging's
invitation requirement remains. A minimal internal profile is created only when
the shared workflow needs it: no invented skills, experience, preferences,
matching run or required onboarding.

The five host-only Secure/HttpOnly cookies are
`__Host-wahojobs_candidate_{session,csrf,login_csrf,tx,context}`. The session has
one-hour idle and eight-hour absolute limits. Logout clears only those cookies
and revokes only the current candidate application session. It does not promise
global WorkOS logout or clear employer/other beta sessions. Tracking survives
logout and fresh verified sign-in.

Private responses use `private, no-store`, `noindex, nofollow` and no-referrer.
Only the five candidate cookies can traverse the narrow website gateway. It
rejects forged owner headers, unrestricted Authorization/cookie forwarding,
wrong hosts, unsupported methods/queries, oversized forms/responses, and unsafe
redirects. Public caches/HTML/sitemaps contain source content and static controls,
not emails, sessions, owners, tracking status or histories.

## Intent, identity and workflow

An anonymous explicit Save, Applied or Not interested action retains the exact
canonical job, selected source variant, filters/page and safe return path in a
bounded opaque ten-minute context. It is bound to the verified account after
login. A continuation GET renders the form but never changes tracking; the
following CSRF POST validates and consumes the intent. Cancellation, replay,
restart or expiry fails safely. Returning candidates bind their private record
version once; refreshing the continuation does not erase a stale-tab conflict.

The existing atomic action engine supplies ownership, record versions,
fingerprints, idempotency, Applied correction and restoration history. An initial
catalog action does not require a MatchRun. Save cannot silently downgrade
Applied. Canonical siblings do not create duplicate histories or replace the
applied-to source. Live source identity includes the source hash and immutable
posting identity; reidentified or expired source references cannot create new
tracking. Existing history remains recoverable when a source closes or its
verification expires. Unavailable references are labelled and do not fabricate
an active Apply destination. Applied correction restores the existing prior
state; it does not invent a Saved transition.

The actual public client waits for confirmed private state before reconciling
status. Hidden canonical siblings use the persisted selected variant for
restoration. Duplicate clicks are bounded, retries preserve their idempotency
key, and failures do not falsely confirm a write. Employer Apply and merely
opening a description are independent and never mark Applied.

## Protected configuration and rollout

No secrets belong in Git, chat, screenshots, public variables or ordinary logs.
The owner provision file is outside the checkout under
`%LOCALAPPDATA%\WahoJobs\candidate-production\candidate-v1.json`, with an
owner/SYSTEM-only ACL. The owner enters the application's Production client ID
and Production API key; initial `enabled` and `public_controls` remain false.

The strict protected document fields are `enabled`, `public_controls`,
`scope=candidate_my_jobs_v1`, `workos_environment=production`,
`workos_client_id`, `workos_api_key`, and a separate 64-hex `gateway_key`.
No key is printed by the setup or release report. Production source is
`/etc/wahojobs-beta/candidate-v1.json`; systemd LoadCredential installs its
0600 effective copy at `/run/wahojobs-beta/candidate-v1.json`.
`deploy/private-beta/80-candidate-my-jobs.conf` retains the authoritative beta
runtime and public reader arguments, adding only the candidate argument.
Native release/recovery pins admit only these fixed paths and an exact matching
credential set/copy. Invalid candidate initialization quarantines candidate
access and leaves the anonymous reader running.

Website server configuration uses `WAHOJOBS_CANDIDATE_ENABLED=1`,
`WAHOJOBS_CANDIDATE_KEY`, and `WAHOJOBS_CANDIDATE_CLIENT_ID`. Public navigation
uses the separate build-time `NEXT_PUBLIC_WAHOJOBS_CANDIDATE_ENABLED=1` flag.
Keep private routes enabled with backend `public_controls=false` and the public
navigation flag off until the controlled real journey passes at www. Only then
enable source-only catalog controls and public My Jobs navigation.

WorkOS Production dashboard configuration:

1. Authentication → Methods: Magic Auth enabled; this already selects email-code
   authentication. Do not enable email/password or social providers for V1.
2. Authentication → Features: preserve the default public Sign up setting and
   keep Waitlist disabled. The application Redirects' Sign-up URL is a different
   field, not the signup enable/disable switch.
3. Applications → selected Production application → Redirects: register exactly
   `https://www.wahojobs.com/candidate/auth/callback` as a Redirect URI and
   `https://www.wahojobs.com/jobs` as a Sign-out URI.
4. Branding: verify the Wahojobs name/logo. API keys: securely provision that
   Production application's client ID/key. Production keys are shown once.
5. Complete the real email-code flow with an explicitly designated owner/test
   identity. Do not send codes, keys or passwords in chat or email customers.

Official references: [environments](https://workos.com/docs/authkit/environments),
[Magic Auth](https://workos.com/docs/authkit/magic-auth),
[applications](https://workos.com/docs/authkit/applications),
[signup](https://workos.com/docs/authkit/invite-only-signup),
[waitlist](https://workos.com/docs/authkit/waitlist).
The owner personally supplied payment details to unlock Production; the agent
did not purchase a plan or supply billing details. WorkOS documents AuthKit's
free allowance up to one million monthly active users, followed by usage pricing.
Actual billing settings/usage remain owner-controlled and unverified here.

Before installation, recheck accepted backend/frontend drift, reserve Dandot's
deployment slot, verify current cold-backup capacity, and use the established
maintenance and database lifetime locks. Retain actual current budgets including
catalog preparation 180s, startup 240s, daily backup 180s, full backup 600s,
publication 600s and recovery 420s. Do not run source repair just to test this
feature, prune backups, replace the production database or revert these budgets.

Rollback disables the public navigation/controls and candidate route activation
while preserving `/jobs`, NextAuth and all candidate tables. A code/configuration
rollback never restores an older database over newly created account activity.
Preserve the existing current native archive and root-only configurations.

## Verification and outstanding live gates

Automated checks use synthetic identities and disposable databases. Candidate
coverage includes exact intent continuation, two subjects sharing an email,
Production client isolation, expired sessions, callback delivery compensation,
non-Magic/unverified provider rejection, source reidentification, stale writes,
duplicate/retry behavior, scoped logout/coexistence, restart/freshness history,
public-cache isolation and trapped zero matching/scoring/network calls. A native
protected-domain hash check includes candidate account/tracking tables during
inventory-only changes. Seven DOM tests execute the actual catalog client; they
are not real browser or live provider acceptance.

The one independent code review closed all findings. Local frontend tests, lint
and TypeScript passed. Backend affected-suite and native operating results are
recorded in the task's release-readiness report. Linux-specific native cases
skipped on Windows remain a Linux acceptance prerequisite.

At this documentation checkpoint no candidate Production deployment, live
owner authentication, desktop/mobile browser journey or screenshots are claimed.
Owner screenshots show Production Magic Auth enabled and the exact callback and
sign-out URIs saved; they do not prove the key or complete hosted signup journey.

The current public privacy policy was fetched directly. It allows voluntarily
provided personal data with consent, but says personally identifying information
is not transferred to outside parties (apart from its stated IP-location service
exception). That statement does not accurately describe sending a candidate's
email to WorkOS for authentication. It also does not describe optional candidate
identity, private tracking history or retention/deletion support. Report these
specific disclosure gaps before public advertising; this integration does not
claim compliance or rewrite the legal policy. Existing `/privacy-policy`, `/tos`
and `/contact` links are reused.

Production test path: anonymous `/jobs` with filters/page → explicit Save →
email-code signup → exact source/return → Saved → My Jobs → Applied → correction
→ Not interested → Undo/Hidden restoration → refresh → candidate logout →
verified login → retained history. Verify both desktop and mobile at the final
www origin, anonymous employer Apply and retained employer posting/draft detours.

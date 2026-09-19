# Candidate UX Cleanup V1 — owner review

Status: the integrated preview, including subsequent education/work-history changes and Browse Jobs V1, was manually tested and accepted by the owner on 19 September 2026. The owner subsequently authorized relevant commits and code-only deployment to the existing beta. See `candidate_ux_beta_promotion.md` for release status. Earlier implementation-stage restrictions and observations below are historical.

The latest feedback simplifies abilities and work history with examples and guided periods. See [the current handoff](candidate_ux_cleanup_work_followup.md). The owner acknowledged improvement in education and requested this further corrective iteration.

The owner requested a corrective education iteration after this initial handoff. See [the current correction handoff](candidate_ux_cleanup_education_followup.md) for Undo, degree display, completion-year fixes, current-preview match evidence and remaining qualification-comparison issue. The preview has not been accepted.

## Working context and boundaries

- Branch: `codex/candidate-ux-cleanup-v1`.
- Base HEAD: `d6ae22fab2bb9bd2a930249c3d051c123fc00aff`.
- Reported hosted release `ca5ca62107f76e98668a8b7d74fcbbc6056dbb0d` exists locally; the base differs only by closure documentation.
- Original checkout remains on `main` at `226183748acb251d21729ac596146cd7d56364d1`; its pre-existing untracked files were left alone.
- No applicable repository instruction file was found before implementation.
- Tests, captures and preview use disposable synthetic owners and storage. Sam is a generic fixture, not the owner's profile.
- No changes to active hosted beta, public site, authoritative user data, existing recovery/demo apps, public routing, indexing, WorkOS Production, invitations or production databases.
- No commit, merge, deployment, model call, inventory refresh, new matching logic, résumé import or source regeneration.

## Implemented

### Profile editing and presentation

My profile → Edit profile now opens the populated editor directly. Review changes saves a pending draft; one explicit Save changes confirms it and appends the immutable revision. Matching continues to use the confirmed profile until then.

The review shows Added/Removed facts and keeps the complete proposal in a disclosure. No-op reviews offer editing/profile navigation and cannot create a redundant confirmed revision. Empty visits do not create meaningful pending work.

A pending-draft notice appears only for a saved proposal that differs from the confirmed profile. Continue editing resumes that proposal. Discard invalidates only the pending draft and related confirmation artifacts using append-only tombstones; it preserves canonical revisions and historical checkpoints. Ownership, stale references and concurrent discard/save paths are checked. A newer draft cannot be discarded through an older tab, and discarded work cannot be resurrected by stale resume/redraft actions.

Correction no longer asks for a separate credential attestation in addition to final confirmation. Existing onboarding attestation remains in its separate manual-draft flow. Validation and final explicit accuracy confirmation remain.

The redundant remote preference is hidden in editing and profile/review summaries, while its stored value round-trips unchanged. Country permissions, geographic restrictions and meaningful work conditions remain visible. Dates are readable, education and credential status use natural wording, and identical work-interest/task lists appear once without merging their stored fields.

Compatibility follow-up: the legacy remote field still contributes to matcher profile text and the legacy work-preference shadow. The standalone preview script also retains its old remote label. This batch changes neither those matching inputs nor deeper filtering. Removing that legacy behavior would need a separate evidence-based change.

### Compensation

The repeated dollar-currency warning is removed from cards and prominent pre-application warnings. Confirmed denominations are shown only when the rate text or correctly bound source metadata identifies them. Unspecified currency retains its advertised amount/symbol and gets a small factual detail note. A denomination from another rate or mismatched source record is not borrowed. Geographic and other material eligibility warnings remain.

The Matches layout, ranking, admission, compensation records and explanation-generation behavior are unchanged.

### Activity and tracking correction

Your activity is collapsible, most recent first, with readable event descriptions and timestamps. Current status is shown directly. Internal normal visibility is no longer described as “Visible for consideration”; hiding is described as hiding from the candidate's matches.

The apparent duplicate Saved rows were distinct persisted events: creation of the user's job record (`user_created`) and the terminal save receipt (`product_noop_save`), often sharing a timestamp. They now read Added to My Jobs and Save confirmed. Records were not deleted or cosmetically deduplicated.

Mark as applied offers immediate Undo. Applied jobs with an auditable prior state also expose Change status → Restore previous status after reload. Correction restores the actual preceding workflow/provenance, including legacy unknown progress when valid, while preserving current reminders and hidden choice. It appends an audit correction and retains existing history. Ownership, expected version, idempotency, concurrent submissions, mirror failure rollback and reconciliation are covered.

An imported Applied status with no reliable prior action is not guessed back to Saved. The details explain when earlier tracking status cannot be restored.

External employer navigation remains a link and does not record an application. Tracking correction explicitly says it does not submit or withdraw an employer application.

## Validation and independent review

- Final targeted run: 93 tests passed, including cleanup, currency/card evidence, returning-user authentication and invitation regressions.
- Final profile run: 35 tests passed, including editor, review, preferences, draft resume and presentation.
- Earlier affected run had two remaining obsolete copy expectations; those were corrected and a 32-test follow-up passed. Historical logs retain those earlier failures rather than obscuring them.
- The new native-DOM test uses actual forms, shipped browser script and verified HTTPS requests through the product handler into isolated persistence. It covers direct editing, draft resume/discard, no-op/final confirmation, immediate and persistent correction, and external-link clicks without status mutation.
- Focused service regressions cover ownership, stale actions, duplicate/concurrent correction, unknown prior state, current reminder/hidden preservation, atomic rollback and reconciliation.
- Independent profile reviewer: no remaining blocking findings after fixes; five cleanup tests and education/constraint/credential review checks passed.
- Independent status/currency reviewer: no remaining blocking findings; 24 focused regressions and six source-identity mismatch probes passed.
- Browse assessor ran 46 catalog/company/detail tests plus 20 authenticated variant/detail/workflow tests and disposable geographic/filter probes.
- `git diff --check` passed.

Visual QA used actual rendered synthetic handler pages, captured for a separate loopback static viewer. Desktop review included profile, populated editor, review, Matches and expanded activity/status. Mobile review included editor, review and activity/status at 390 and 320 pixels; checked pages had no horizontal overflow. A long-profile editor fixture was also checked at 320 pixels. The Matches composition remains compact.

Limit: the interactive full-browser HTTPS preview reached a generated-localhost-certificate warning. Browser-control policy requires the owner to accept that warning personally; it was not bypassed. Native DOM + verified HTTPS tests passed, and the final preview's controlled login, profile and direct editor were independently verified over HTTPS with certificate and hostname validation. Static visual QA is not represented as a complete live-browser journey or hosted verification.

Logs and captured pages are in the sibling `candidate-ux-evidence` directory. The preview receipt and verification receipt contain the exact local fixture and reachability results.

## Owner review

Interactive preview: **https://localhost:8875/login?next=/account/profile**

This supersedes the earlier temporary development URL on port 8873. It uses only a generated local test account and fixture data. If a certificate warning appears, the owner must accept it personally to proceed. No real Google/WorkOS credentials are needed for the controlled local login.

1. Sign in through the controlled local login, then My profile → Edit profile.
2. Change a city or preference, Review changes, inspect Added/Removed and the complete proposal, then Save changes.
3. Make another change, Review changes, return to My profile, and try Continue editing / Discard changes.
4. Submit unchanged values and check No changes to save.
5. Open Matches and a job detail; Save, Mark as applied, then immediate Undo.
6. Mark as applied again, reload, expand Your activity and Change status, then Restore previous status. Try with a reminder set.
7. Review a listing with unspecified currency and one with explicit currency.

Read-only visual captures: **http://127.0.0.1:8874/**. These are snapshots with inert form state, useful for layout review; use port 8875 for functional review.

## Browse Jobs — assessment only

### What exists

`wahojobs/public_jobs_catalog.py` implements the existing `/jobs` catalog. Its integration is in `wahojobs/authenticated_profile_matches.py`; `wahojobs/remote_beta.py` exposes the route behind beta authentication. The candidate navigation omits it, but the empty Matches state links there.

The relevant historical catalog commits `3546667` and `8e8af48` are already ancestors of the reported hosted release. This is existing product code, not a fixture-only mockup or a feature missing from the release. That conclusion is based on release code, not a new live-hosted visit.

The legacy public site and the separate exact-route public-preview gateway/frozen projection described in `docs/production_origin_preview.md` are distinct from this authenticated catalog.

### What works

- Queries canonical opportunities beyond the personalized matcher shortlist, without silent profile restrictions.
- Reuses canonical details and exact posting identity for owner-scoped saved jobs.
- Keyword and country/location/work/field/language filters actually affect results.
- Clear resets to `/jobs`; page size 30 and query-preserving pagination are deterministic.
- Empty states and responsive styling exist.
- Brazil filtering includes worldwide, Americas and Brazil opportunities, while excluding US-only and unknown-location items.

### Concrete gaps

1. Location filtering occurs after selection of a global canonical representative. In a disposable US/Portugal variant probe, Portugal returned zero although that opportunity's Portugal detail was available.
2. Catalog cards lose filtered return context when opening details; the back link returns to Matches. A validated return-route mechanism already exists to reuse.
3. Worldwide wording is inconsistent: “Worldwide” and “Work from anywhere” returned zero, while “Remote worldwide” worked but was not suggested. The worldwide-only toggle disables location. Region filtering includes explicit region/worldwide records but not country-only records within the region; the intended policy needs clarification.
4. Keyword UI mentions skills, but the index omits required/preferred skill arrays.
5. A hidden `arrangement=Contract` query still affects results without a visible active filter and is dropped by normal form submission.
6. Facet counts are global rather than counts of remaining results; five always-visible filters make mobile lengthy. Arbitrary datalist input can yield opaque emptiness, and an out-of-range page returns a generic 404 without recovery navigation.

### Smallest next milestone

Reuse `/jobs` and add it to candidate navigation. Preserve catalog query/page context through details. Filter matching posting variants before canonical collapse. Normalize worldwide/country/region behavior and include skill arrays in keyword search. Keep keyword/location prominent, retain useful secondary filters behind a compact disclosure, and show active filters plus reset and useful empty/page recovery.

No catalog backend replacement, broad filtering framework, inventory refresh, public SEO rollout or Browse implementation belongs in this cleanup. All items above are recommendations, not implemented changes.

## Next decision

The owner's subsequent integrated-preview acceptance and beta-only promotion request supersede the earlier pending-review state. No additional feature or visual redesign is part of that promotion.

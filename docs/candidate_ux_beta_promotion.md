# Candidate UX Cleanup V1 and Browse Jobs V1 — beta promotion

The owner accepted the tested integrated preview on 19 September 2026 and authorized committing the approved implementation and deploying code only to the existing hosted beta. The five synthetic preview opportunities are not evidence of full hosted-catalog correctness.

## Candidate and scope

The release is prepared on `codex/candidate-ux-cleanup-v1`, directly from the accepted working tree based on `d6ae22fab2bb9bd2a930249c3d051c123fc00aff`. It includes all profile, education, abilities, optional work-history, tracking, compact Matches and Browse changes documented in the four milestone records. Previous independent reviews and their resolved findings remain applicable; no new visual or feature changes are included.

No migration, dependency change, authentication-policy change or storage replacement is required. Draft discard uses append-only markers in the existing private draft store when the owner explicitly discards a draft; deployment itself does not create or transform that store. Existing canonical profiles, revisions and source records retain their contracts. Synthetic data is confined to tests and the explicitly isolated preview launcher; the hosted entry point remains `scripts/private_beta_app.py` with the existing WorkOS configuration.

## Promotion gates

The immutable source archive must be generated from the actual committed revision, with commit, tree and SHA256 recorded outside the repository. Any subsequent documentation-only commit must be distinguished from that deployed source.

Before switching code, verify the actual hosted release and authoritative storage, run the pending POSIX listening-socket restart test on actual Linux, validate current storage against the unchanged supported schema, and take and verify a supported cold backup. Compare a fresh privacy-safe baseline covering inventory, account/identity, profiles/revisions/sources/drafts and workflow/reminder/history data. Earlier counts from the sign-in repair are historical and must not be used as expected current counts.

Use the existing private SSH handoff and pinned-host procedure. The prior helper discarded its credential and exited; private material is never committed or copied into release evidence. Only `wahojobs-beta.service` may be restarted. Config002 and the existing authoritative recovered storage stay selected. Rollback selects the previous immutable code with current storage in place; it never restores an older snapshot over intervening activity.

After deployment, record read-only real-inventory queries, canonical/variant counts, details/back context, navigation and authenticated page observations. Owner history must not receive disposable smoke-test writes. Clearly separate server-side checks from checks needing an owner's authenticated browser.

At this source checkpoint deployment has not yet occurred. The operational handoff will identify the exact hosted commit and observed results. Main/public-site changes, inventory refresh, external model/source calls, synthetic hosted data/authentication, WorkOS Production activation and invitations remain excluded. Privacy/retention, disaster recovery, hosted second-account isolation and invitation readiness remain separate gates.

## Hosted inventory performance gate

The initial candidate `ee3695c20100e781adddb65fd1fd6a2f35e25fbe` passed 32 isolated Ubuntu tests, including the immediate socket restart test, but its read-only catalog check timed out before any service stop or release switch. It was never deployed. The hosted inventory contains much larger groups of source variants than the synthetic preview.

The follow-up code shares identical scoped attribute projections within one inventory snapshot, avoids expanding detail-only provenance in catalog cards, checks a selected variant before evaluating unrelated rows, and evaluates facet predicates once per facet rather than once per option. Detail pages retain the complete source evidence; canonical/variant identity, geographic eligibility, conflict/unknown handling and matching rules are unchanged. Country identities use a bounded cache of pure label parsing only.

The read-only hosted probe of the corrected code loaded 491 eligible canonical opportunities and 5,624 eligible source variants in about 7.1 seconds on its first load; cached-inventory queries took under one second in that probe. All 17 pages, ten keyword/location queries and six detail/context links were checked. These observations describe the existing inventory at the probe time, not a guarantee of full-catalog correctness. The exact committed release must still pass the deployment guards and current-data comparison before promotion is reported complete.

## Completed beta promotion — 19 September 2026

The hosted release is **`ce9f318818843f0af591facc4b22af4cdb464243`**, with source tree `880902b3611151fd60266d9fc43ce7cf6affa5da` and exact git-archive SHA-256 `34ec384cc83342d1200377a71ce83fef5a7854270b280948e8f89ad3066add20`. The promotion completed at 23:38:42 UTC. This completion record is a subsequent documentation-only change; it is not the deployed source revision.

The final local suite passed **184 tests**. The immutable release then passed **87 tests on Ubuntu/Python 3.12**, with zero failures, errors or skips, including `test_immediate_socket_restart_after_served_response` and the catalog/detail regressions. The existing M011 storage schema was compatible without migration. Only `wahojobs-beta.service` was restarted. The supported cold snapshot `candidate-ux-browse-predeploy-001` was verified with 220 files before selecting the new release; the previous release remains available for code-only rollback.

The authoritative database remains `/var/lib/wahojobs-beta/rehearsal-recovered-001/product.sqlite3`, with its existing correction-draft sidecar and `config-002`. In-memory comparisons of every protected logical row and schema object before and after deployment matched, including accounts/identity bindings, profiles/sources/revisions/drafts, inventory, saved jobs, statuses and workflow history. Session usage bookkeeping was deliberately excluded from those comparisons. The four existing workflow records (two saved, two applied) had no invariant violations. No owner-history smoke-test writes were made.

| Hosted observation | Result |
| --- | ---: |
| Stored canonical opportunities | 879 |
| Stored source job records | 6,012 |
| Currently eligible canonical opportunities | 491 |
| Currently eligible source variants | 5,624 |
| Catalog pages | 17 (16 × 30, then 11) |
| Keyword: Python / Data / English | 29 / 70 / 42 |
| Location: Remote | 491 |
| Location: Brazil / Portugal / United States / Worldwide | 0 / 0 / 0 / 0 |

The country results reflect the existing explicit eligibility evidence under the current freshness/source rules. Remote status alone does not establish country eligibility. All page identities were unique and complete; ten query states and six variant/detail/back-context links were checked using the actual hosted database before and after deployment. Shared Browse navigation rendered correctly. These are bounded checks, not an assertion that every source record is correct.

The hosted entry point uses existing inventory and normal WorkOS Staging authentication. No preview database, synthetic authentication, fixtures, migration, inventory refresh, scraping, model call or new invitation entered the beta. Public configuration remained unchanged, and main stayed at `226183748acb251d21729ac596146cd7d56364d1` with no merge or push.

Browse URL: **https://beta.wahojobs.com/jobs**. An unauthenticated browser was confirmed to reach the normal sign-in page. Owner-authenticated visual verification of Browse, profile, Matches and My Jobs is pending ordinary owner sign-in; server-side probes did not fabricate an account session. Suggested review: Browse → search Python → open a result → return to results → My Jobs → My profile.

The private deployment handoff was closed and its in-memory key discarded. The privacy-safe deployment receipt has SHA-256 `6d5c3b16f8f2bac720a8a12d999462f680aea329c1b98015ddb060fc0d891ee6`. Deployment closes this promotion only; privacy/retention, disaster recovery, hosted second-account isolation, WorkOS Production and invitation readiness remain separate gates.

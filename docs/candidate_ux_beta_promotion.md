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

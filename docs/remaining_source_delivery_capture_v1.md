# Remaining Source Coverage V1: beta-host capture boundary

This runner is for the authorized DataAnnotation and DataForce delivery batch only.
It does not publish, activate a source, or change the nine-source daily operation.
The capture must run online from `wahojobs-private-beta-rehearsal-20260917` in a
prepared release archive under `/opt/wahojobs-beta/releases/<40-hex commit>`.
The `--code-commit` value names that archive. The runner verifies its path and
that the relevant files are root-owned and not group/other writable. It records
their SHA-256 values in each plan. **The path name alone does not prove Git
ancestry or archive contents.** Before capture, the operator must compare the
archive SHA-256 to the locally produced archive for the reviewed commit and
verify the hosted release/current state and any intervening accepted changes.
Stop if that verification cannot be made.

There is exactly one task ledger path:
`/var/lib/wahojobs-beta/remaining-source-coverage-v1/task-ledger`. The runner
has no ledger or expected-host command-line override. An exclusive Linux file
lock spans usage calculation, reservation, collection and final receipt. The
same ledger counts validation and commissioning attempts: DataAnnotation 32,
DataForce 100, aggregate 132, with 900 aggregate collection seconds. Each
redirect hop is an audited request; no automatic retry is allowed. An
unfinished run directory blocks further capture until its request journal has
been independently reconciled. Do not delete or replace that directory to
restore a budget.

Run one source at a time, outside application maintenance, from the verified
archive. The runner takes `--source`, `--phase` and `--code-commit`; its fixed
source URLs are `https://www.dataannotation.tech` and
`https://dataforcecommunity.transperfect.com/projects`. Raw bodies, request
ordinals, response metadata, parser hashes and timestamps stay in the private
journal below the task ledger. A successful read-only capture is a controlled
observation, not publication authority. The generic publication entry point
rejects it until a source-specific reviewed qualification path explicitly
admits the actual retained evidence.

Current preparation does not qualify DataForce detail records or uncaptured
DataAnnotation domains. Their record contracts and any source activation must
be based on the new beta-host bodies and an isolated lifecycle replay. The
prior local DataAnnotation collection failure remains failed; its offline
coding replay is not a fresh live observation.

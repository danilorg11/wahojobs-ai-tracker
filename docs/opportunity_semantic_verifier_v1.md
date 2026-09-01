# OE Semantic Verifier v1

Status: closed experimental development-regression implementation. It is not
imported by enrichment runtime, persistence, matching, semantic packet
construction, compatibility projection, or UI code. OE Semantic Authority
Boundary v1 gives every result, including challenge agreement, no eligibility
or exclusion authority.

## Exact-claim authority boundary

Verifier v1 classifies immutable, server-serialized claims. It does not ask the
provider to reconstruct qualifiers or semantic fields. The primary request
contains one complete atom or relation claim and only its server-authenticated
evidence. Its provider output contains exactly one closed decision:

- `entails`
- `contradicts`
- `not_established`

`entails` requires full support for the exact serialization. A missing,
ambiguous, over-specific, narrowed, or speculative material component is
`not_established` unless the evidence affirmatively contradicts it. Neither
verifier may create, repair, normalize, regroup, project, or authorize a claim.

The primary verifier uses `gpt-5.6-terra`, low reasoning, `store: false`, no
tools, and the strict decision-only schema. It verifies all 80 atom claims and
49 complete relation claims in the already-opened 16-case development set.

## Canonical immutable serialization

The atom serializer includes the immutable atom identity, subject, kind, typed
raw/source payload meaning, raw source values, polarity, and temporal meaning.
Normalized values and normalization status are excluded.

The relation serializer includes the immutable relation identity, modality,
complete DNF `all_of`/`any_of` structure, each referenced atom's complete exact
meaning, and every authenticated source branch with its representation status.
Relation evidence includes every authenticated evidence span needed by those
source branches, so a narrowed proposal is evaluated against the complete
source proposition rather than only the retained member.

Every input packet and result is hash-bound to role, claim identity, canonical
claim serialization, authenticated evidence identities and text, verifier
contract, prompt, schema, serializer, and model/configuration identities.

## Historical soft retention and challenge experiment

Primary abstention does not delete a grounded proposition. Primary decisions
only update semantic assurance within an isolated evaluation copy of the
unchanged staging ledger.

A second challenge is dispatched only for a required relation that already
satisfies the existing deterministic completeness and normalization policy and
whose complete relation plus every member atom received primary `entails`.
Preferred and descriptive relations are never challenged. The challenge uses
`gpt-5.6-sol`, low reasoning, `store: false`, no tools, and the same
decision-only schema. It receives the immutable complete candidate constraint
and its complete authenticated evidence, with no primary decision or rationale.

The experiment labeled a candidate as hard-authorized only when both independent
decisions were `entails`, in addition to every existing deterministic staging
requirement. That label remains available only for historical evaluation. It is
not product hard authority and is reduced to an observation with
`authority_effect: none` by the v1 semantic packet.

## Frozen reference and population

The direct human reference is frozen before provider calls. Atom labels retain
the existing 72 `entails`, 2 `contradicts`, and 6 `not_established` reviews.
The new explicit relation-level review contains 49 direct decisions: 33
`entails`, 2 `contradicts`, and 14 `not_established`. It includes narrowed
conjunction, incomplete-source, unsupported-member, and extra-speculative-member
controls. The complete-required hard-candidate universe contains 24 relations:
19 reference-supported candidates and 5 unsupported or incomplete controls.

The primary task population SHA-256 and hard-candidate-universe SHA-256 are
pinned by permanent tests. The evaluator refuses to overwrite its frozen
manifest, accounting journals, or live report.

## Durable accounting and execution

Primary Terra and challenge Sol phases use separate append-only, fsync-backed
JSONL journals. Every frozen task is registered, dispatch is persisted before
the request, and completion or categorized failure is persisted after it. The
journals retain task and request identities, status, returned model alias,
input/cache/output/reasoning tokens, latency, provider failure category, and
schema/semantic validation outcome. A stopped run can reconstruct exact work
already registered, dispatched, completed, failed, or left in flight without
using the application database.

Run permanent closure, freeze once, then evaluate once:

```text
python -m unittest tests.test_opportunity_semantic_verifier_v1 -v
python scripts/opportunity_semantic_verifier_v1_eval.py --freeze-manifest
python scripts/opportunity_semantic_verifier_v1_eval.py --evaluate
```

The evaluator performs no extraction, grouping, application-database, runtime,
or persistence operation.

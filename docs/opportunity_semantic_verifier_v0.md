# OE Semantic Verifier v0

Status: closed experimental development-regression implementation. It is not
imported by enrichment runtime, persistence, matching, semantic packet
construction, compatibility projection, or UI code. Its results are quality
observations only under OE Semantic Authority Boundary v1.

## Authority boundary

The verifier classifies one immutable claim per OpenAI Responses API call. Each
call contains only that claim and its server-authenticated contiguous evidence.
The request uses `gpt-5.6-terra`, `reasoning.effort: low`, `store: false`, no
tools, and a strict claim-type-specific structured-output schema.

The only decisions are the existing staging values:

- `entails`
- `contradicts`
- `not_established`

Atom verification covers kind/payload, polarity, and temporal meaning. Relation
verification covers the already-proposed DNF logic, modality, and completeness.
The verifier cannot add, delete, rewrite, normalize, regroup, project, assign
scope, choose compatibility fields, or grant eligibility or hard assurance.

Every provider result is server-bound to:

- immutable atom or relation identity;
- authenticated evidence-set identity;
- verifier contract version;
- prompt and schema identities;
- model/configuration identity;
- exact verifier input and provider output hashes.

## Provider response closure and accounting

Schema v2 has separate atom and relation response schemas. Atom output can
contain only `kind_payload`, `polarity`, and `temporal`; relation output can
contain only `relation_logic`, `modality`, and `completeness`. Both use exact
object keys and the three staging decisions. The provider does not emit a
separately mutable overall decision. The server deterministically derives it
with the frozen rule (any contradiction; otherwise all entail; otherwise not
established) and validates the resulting unchanged canonical local shape.

The evaluation runner writes an append-only, fsync-backed JSONL journal before
and after every request. It registers every frozen task, records dispatch before
network execution, and records either an evidence-free completed result or a
categorized failure with all provider usage, alias, latency, and schema outcome
available at that boundary. A stopped run can therefore reconstruct registered,
dispatched, completed, failed, and still-in-flight tasks without application
database access or another provider call.

## Staging behavior

Atom results are projected into the existing staging verification shape. A
relation `entails` result can only preserve an existing staging state. It cannot
upgrade an incomplete or invalid relation. `not_established` downgrades an
otherwise-complete relation to `grounded_incomplete`; `contradicts` downgrades it
to `invalid_proposal`. The existing finalizer admits only relations that remain
`complete_verified` after every deterministic and semantic requirement.

Unresolved source branches remain in the relation ledger. Raw Italian,
Indonesian, Physics, and chemistry values are sent only as source text or raw
source-branch values. Normalized values are never provider input.

## Frozen regression protocol

The population is the already-opened 16-case development regression set:

- 80 atom claims;
- 49 proposed relation claims;
- 77 authenticated source branches;
- 129 one-claim provider calls.

The atom reference is the frozen 72 `entails`, 2 `contradicts`, and 6
`not_established` human review. The qualifier reference records the eight
reviewed meaning failures as kind/payload failures while their polarity and
temporal qualifiers remain supported.

There is no separate independent human relation-annotation artifact. Before any
provider call, relation references are frozen from the human atom labels,
immutable raw relation proposals, authenticated source branches, and the
already-passed deterministic staging policy. Reports identify this provenance
explicitly.

Run permanent tests, freeze once, then evaluate once:

```text
python -m unittest tests.test_opportunity_semantic_verifier -v
python scripts/opportunity_semantic_verifier_eval.py --freeze-manifest
python scripts/opportunity_semantic_verifier_eval.py --evaluate
```

The evaluator refuses to overwrite the frozen manifest, accounting journal, or
live evaluation artifact. It performs no extraction, grouping, application
database, runtime, or persistence operation.

# OE Semantic Staging and Verification v0

Status: historical isolated offline architecture proof. It is not imported by
enrichment, persistence, matching, or candidate presentation. OE Semantic
Authority Boundary v1 supersedes its product-authority interpretation:
`hard_projection_authorized` is now an experimental artifact token with no
eligibility or exclusion effect.

## Flow

1. Authenticate the server-owned accepted-evidence bindings against the unchanged
   source packet.
2. Materialize every proposal into a provisional ledger. A proposal receives a
   stable ledger identity and immutable atom SHA-256; it is never deleted.
3. Extract bounded source values from authenticated quote spans. Normalization is
   server-owned and closed to `resolved`, `ambiguous`, or `unmapped`.
4. Run the deterministic lexical check only as an `entails`/`abstain` fast path.
   Abstention does not reject a proposition.
5. Apply a bounded semantic-verification decision to the immutable atom:
   `entails`, `contradicts`, or `not_established`. The decision cannot carry a
   replacement payload, normalization, group, modality, projection path,
   eligibility claim, or variant authority.
6. Construct relations over the complete provisional ledger and evidence-derived
   source branches. No atom is subtracted from a proposed AND/OR.
7. Assign exactly one relation state: `complete_verified`,
   `grounded_incomplete`, `invalid_proposal`, or `unrepresentable_relation`.
8. Historically admit only `complete_verified` relations to OE Semantic Contract
   v0. Admission requires semantic entailment, complete qualifiers, resolved
   normalization, exact source-branch coverage, an authoritative server-derived
   modality, and the frozen experimental `hard_projection_authorized` token bound
   to the atom hash. Boundary v1 gives that token no product authority.
9. Run the existing deterministic compatibility projector. Incomplete or invalid
   relations are absent from the projector input, while their atoms and branches
   remain in the staging ledger.

## Authority boundary

Evidence authentication proves provenance, alias identity, accepted authority,
exact quote linkage, hashes, contiguous spans, and bounds. It does not treat a
failed lexical paraphrase match as semantic contradiction.

The semantic verifier can classify only the unchanged atom, its polarity and
temporal qualifiers, against cited evidence. Relation construction, modality,
normalization, final-contract admission, and projection remain outside the
verifier.

Semantic retention and deterministic projection use different assurance levels.
An entailed atom may remain `semantic_verified` for semantic matching while an
unmapped value, missing qualifier, or incomplete relation prevents
`hard_projection_authorized` admission.

## Source values and logic

Raw values are exact substrings of authenticated quotes selected by bounded,
server-owned matchers. An unmapped raw value remains in its source-value record;
it is not replaced by a registry member. The regression fixture therefore retains
Italian, Indonesian, Physics, and chemistry even when the final v0 registry cannot
represent them.

Education alternatives are expanded into source-level branches from the bounded
level and field dimensions. A missing Physics or chemistry branch is recorded as
unresolved, which makes the relation incomplete. It cannot disappear or leave a
smaller OR/AND eligible for finalization.

## Role activity composition

The verified staging path composes the existing closed activity and artifact
vocabularies instead of relying on an exhaustive pair table. Both components must
be supported by evidence. This admits `fact_checking + ai_output` where factual
verification of model output is stated. `software_testing` additionally requires
evidence of software, an application, a platform feature, or a digital tool;
generic QA or fact-checking remains non-testing.

## Offline regression artifacts

- `tests/fixtures/opportunity_semantic_extraction_v0_fresh_canary.json`
  (`177574a22812a1f6122b3a03e640848fd6f211a9d19476ff416ed7330f6fbd38`)
- `tests/fixtures/opportunity_semantic_staging_v0_fresh_canary_raw.json`
  (`37d6e7bea857274477ad08a89fef81023b3c1502c5064242ba4fe34c5540daf4`)
- `tests/fixtures/opportunity_semantic_staging_v0_reviewed_canary.json`
  (explicit frozen 72 entailment, 2 contradiction, and 6 not-established labels)

Run the replay with:

```text
python scripts/opportunity_semantic_staging_replay.py
```

The replay reads only these fixtures. It has no provider, network, database, or
persistence path.

# OE Semantic Shadow Packet Integration v1

## Status and scope

`oe_semantic_shadow_packet_integration_v1` constructs and validates
`oe_semantic_matching_packet_v1` through a read-only Opportunity Enrichment
seam. The result is shadow/offline-only. No matching, ranking, deterministic
eligibility, lifecycle/trust, candidate UI, or persistence consumer is
authorized.

The single OE orchestration API is:

```python
construct_oe_semantic_matching_packet_v1_shadow(
    connection,
    canonical_opportunity_id,
    *,
    extraction_payload=None,
    grouping_payload=None,
    extraction_client=None,
)
```

Callers supply a canonical identity and either frozen semantic extraction
output or an explicitly configured shadow extraction client. The seam owns:

- accepted/LKG semantic-input loading and acceptance-integrity checks;
- the existing deterministic objective extraction call, retained separately;
- evidence packet and accepted-capture binding construction;
- semantic staging and relation construction;
- server-derived canonical and variant scope;
- native responsibility/candidate-profile provenance when a current automatic
  enrichment artifact is safely available;
- packet/version/provenance/authority identities;
- complete-packet validation and fail-closed result status.

## Authority and isolation

Every proposition, group, retained invalid proposal, and descriptive signal is
`semantic_non_exclusionary`. `required` remains a semantic modality only.
Packet construction does not import verifier-v0/v1. Frozen verifier observations
remain zero-authority compatibility observations in the underlying authority
boundary and cannot change the authority discriminator.

The packet records a closed `opportunity_scope` and per-item `applicability`:

- `canonical_applicability` is always `not_claimed`;
- variant applicability is `single_variant`, `variant_subset`,
  `all_known_variants`, or `unresolved`;
- variant references come only from authenticated server evidence blocks;
- observing the same proposition on all known variants does not promote it to a
  canonical hard fact;
- `canonical_fact_promotion_allowed` is always false.

The complete validator recomputes applicability from evidence relationships,
rejects unknown variant references, and requires both
`semantic_hard_exclusion_count` and `canonical_semantic_fact_claim_count` to be
zero.

The seam never reads semantic requirements, semantic location projections,
`legacy_patch`, or `variant_facts`. It does not create `GroundedFactV1` values
and does not use readiness labels as authority. Native opportunity
responsibilities and candidate-profile prose are admitted only from the current
automatic enrichment document with high-confidence accepted-body provenance;
otherwise they are omitted with an explicit availability reason.

## Fail-closed result

The result status is one of:

- `available`: a complete packet passed the single validation boundary;
- `unavailable`: required accepted evidence, extraction, provenance, or
  read-only source access was unavailable;
- `invalid`: an input or complete-packet validation invariant failed.

Unavailable or invalid results contain no partial packet. The result also
declares every runtime, matching, ranking, candidate UI, lifecycle/trust,
legacy-compatibility, and database-persistence authorization as false.

## Offline runner

`scripts/opportunity_semantic_shadow_packet.py` opens SQLite with
`mode=ro&immutable=1` and `PRAGMA query_only = ON`. It consumes frozen artifacts
and emits JSON to stdout or an explicitly named non-database output file.

Example:

```text
python scripts/opportunity_semantic_shadow_packet.py \
  --database data/wahojobs.sqlite \
  --canonical-id 1068 \
  --artifact tests/fixtures/opportunity_semantic_staging_v0_fresh_canary_raw.json \
  --case-id 1068
```

This fixture is regression coverage only. It is not fresh semantic benchmark
evidence, and its output must not be used to tune prompts or claim match quality.

## Current integration boundary

No module under `wahojobs/` imports the shadow seam. The only executable entry
point is the explicitly invoked offline script. The seam returns the existing
deterministic objective extractor's fact count and hash outside the semantic
packet and never invokes an eligibility consumer. Packet persistence is not
implemented.

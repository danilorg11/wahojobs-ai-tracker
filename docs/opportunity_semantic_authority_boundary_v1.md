# OE Semantic Authority Boundary v1

Status: implemented offline authority and packet contract. It is not integrated
with enrichment persistence, shortlist construction, matching, lifecycle,
candidate presentation, or UI behavior.

## Product decision

Every model-derived semantic opportunity proposition, group, responsibility,
and candidate-profile signal is non-exclusionary in this product version. Its
closed authority discriminator is:

```json
{
  "authority_policy_version": "oe_semantic_authority_boundary_v1",
  "authority_type": "semantic_non_exclusionary",
  "hard_eligibility_authorized": false,
  "deterministic_failure_authorized": false,
  "candidate_exclusion_authorized": false,
  "lifecycle_suppression_authorized": false,
  "profile_contradiction_authorized": false
}
```

The full envelope also contains closed allowed uses and forbidden effects.
Semantic output may support shortlist generation, semantic reranking, fit
assessment, explanations, evidence retrieval, preferred-fit signals, and later
match-time reasoning. It cannot independently produce deterministic eligibility
failure, a hard-gate failure, candidate exclusion, lifecycle suppression, or an
authoritative profile contradiction.

`required`, `verified`, `normalized`, `complete`, and high confidence describe
semantic meaning or quality. None changes the authority discriminator.

The only hard-authoritative type in this contract is
`deterministic_objective_hard_eligibility`. Constructing it requires all of:

- a separately named criterion from
  `matching_deterministic_eligibility_decision_v1`;
- an objective field path closed to that criterion;
- a known value at variant scope;
- non-empty high-confidence `deterministic_parse` or `source_explicit` evidence.

This is a discriminated union, not a naming convention. The common validator
returns hard-failure authority only for the valid deterministic member.

## Semantic matching packet

`oe_semantic_matching_packet_v1` is the native packet for future shortlist and
reranker integration. The builder is pure, offline, and verifier-independent.
The packet contains:

- semantic-input version and SHA-256;
- source-packet SHA-256;
- semantic-contract, staging, relation-builder, packet-builder, policy, and
  packet identities;
- the accepted evidence catalog with its original authority, text hash, and
  accepted-capture or reviewed-checkpoint provenance;
- immutable propositions with separate atom and meaning hashes;
- subject, closed kind, typed payload, polarity, and temporal meaning;
- a normalized value only when normalization is `resolved` and all qualifiers
  are complete;
- authenticated raw source-value records for resolved, ambiguous, and unmapped
  values;
- exact evidence source IDs, spans, and quotes;
- server-derived source-to-variant, source, and authority references;
- semantic support and completeness states;
- constraint groups with required, preferred, or descriptive modality;
- the server-derived modality alongside the unchanged proposed modality;
- exact bounded-DNF `any_of`/`all_of` structure and its unmodified raw proposal;
- complete, incomplete, invalid, or unrepresentable relation state;
- all source branches, including unresolved branches;
- retained structurally invalid proposals and unassigned propositions;
- native evidence-linked `responsibility` and `candidate_profile` descriptive
  signals, also carrying the non-exclusionary authority type;
- exact accounting, including a required zero
  `semantic_hard_exclusion_count`.

The packet contains no legacy requirement projection. Responsibilities and
candidate profile are native descriptive signals rather than manufactured
legacy fields.

### Required semantic propositions

A requirement remains represented by group modality:

```json
{
  "modality": "required",
  "logic": {
    "form": "bounded_dnf",
    "any_of": [
      {"all_of": ["advanced_degree"]},
      {"all_of": ["industry_standing"]}
    ]
  },
  "authority": {
    "authority_type": "semantic_non_exclusionary",
    "hard_eligibility_authorized": false,
    "candidate_exclusion_authorized": false
  }
}
```

`required` therefore survives for match-time semantic reasoning without being
interpreted as permission to fail eligibility. A preferred group uses the same
authority and differs only in semantic modality. Descriptive role activity,
responsibility, and candidate-profile signals also use the same authority.

### Incomplete and unresolved relations

The packet is built from the complete staging ledger, not only the final legacy
projection. `grounded_incomplete`, `invalid_proposal`, and
`unrepresentable_relation` records remain present with their raw group proposal,
reason codes, source branches, source values, and evidence.

An unresolved OR branch or member of an AND is never subtracted. For a
representable group, packet `logic.any_of` must equal `raw_proposal.any_of`
exactly. An unrepresentable raw proposal is retained with `logic: null`; it is
not repaired or flattened. Ambiguous or unmapped normalization emits
`normalized_value: null` while preserving authenticated raw values.

## Objective deterministic facts

Objective authority stays outside the semantic packet and outside semantic
policy. Boundary v1 pins the current matching foundation's three separately
authorized criteria:

| Deterministic criterion | Closed fact scope |
| --- | --- |
| `eligibility.location` | location scope and eligible countries, regions, or locations |
| `eligibility.required_languages` | required language facts |
| `eligibility.credentials_licenses` | objective credential or license facts |

The existing deterministic extractor also produces compensation, hours,
schedule, engagement, workplace, explicit education, and explicit experience
facts. They remain distinguishable as deterministic facts, but this authority
version does not silently add them to the current hard-eligibility criteria.
A future criterion or field requires an explicit version change.

## Compatibility and downstream hazards

`project_legacy_compatibility` remains available for diagnostics and historical
evaluation. Its result now carries this closed annotation:

```json
{
  "projection_role": "diagnostic_compatibility_only",
  "authority_type": "semantic_non_exclusionary",
  "hard_eligibility_authorized": false,
  "candidate_exclusion_authorized": false,
  "authority_effect": "none"
}
```

The frozen experimental label `hard_projection_authorized` remains readable in
staging/verifier artifacts. Boundary v1 treats it only as an experimental
observation with `authority_effect: none`.

The following existing interfaces are dangerous if they lose origin/authority
and consume semantic values as eligibility truth:

- semantic-contract `legacy_patch`, especially required skill, education,
  credential, license, experience, current-status, language, and location paths;
- merged enrichment `attributes.requirements`;
- merged eligible-country, region, location, and location-scope fields;
- enrichment `variant_facts` when origin and authority are not discriminated;
- matching `GroundedFactV1` with generic `automatic_enrichment` provenance;
- `OPPORTUNITY_ENRICHMENT_FACT_FIELD_PATHS_V1` and
  `REQUIREMENT_FACT_FIELD_PATHS_V1`, which identify shape, not eligibility
  authority;
- readiness/report interfaces that label a populated field “critical” or
  “objective” without checking fact origin.

No semantic packet is integrated into the matcher in this milestone. The
removal-capable typed projection independently requires human-override or
uniformly high-confidence deterministic/source-explicit evidence and therefore
cannot infer exclusion authority from a legacy field path. A later integration
must consume the native packet only on the non-exclusionary semantic side.

## Offline replay proof

`scripts/opportunity_semantic_authority_replay.py` reads only the reviewed gold
and already-opened 16-case fixtures. It performs no provider call, network call,
application-database read/write, persistence operation, or verifier experiment.

Reviewed gold results:

- 20 cases;
- 32 semantic propositions;
- 23 groups: 19 required, 2 preferred, and 2 descriptive;
- exact required/preferred/descriptive modality and DNF preservation;
- historical expected compatibility projection preserved;
- 0 semantic hard exclusions;
- 0 required-group hard failures;
- 0 compatibility projections with hard authority.

Opened 16-case architecture regression results:

- 16 cases;
- 80 semantic propositions;
- 49 groups: 29 required, 7 preferred, and 13 descriptive;
- 35 complete and 14 incomplete/invalid/unresolved groups;
- all 77 source branches retained, including 14 unresolved branches;
- all 249 raw source-value records retained;
- exact relation, OR, and AND preservation;
- 0 logic-weakening cases;
- 0 projections from incomplete relations;
- 0 existing unsafe compatibility hard gates;
- 0 semantic hard exclusions;
- 0 required-group hard failures;
- 0 unsupported/unresolved hard gates;
- 48 legacy `hard_projection_authorized` observations and 0 authority
  promotions;
- 80 proposition-level server-derived variant relationships.

A separate pending-state control constructs a valid 11-proposition packet
directly from authenticated staging input with no verifier result supplied; all
11 support states remain `pending` and the hard-exclusion count remains zero.

The replay also proves that the objective location fact envelope uses the
distinct deterministic hard-authority type and that the pinned criterion map is
identical to the current matching-foundation contract.

## Verifier closure

Verifier v0 and v1 remain experimental/evaluation code. They are not imported
by the packet builder and are not needed to construct or validate a packet.

Reusable components:

- immutable exact-claim serialization and hashes;
- accepted-evidence authentication and evidence-set identity;
- closed entail/contradict/not-established observations;
- staging ledger, raw proposal retention, normalization records, and source
  branch accounting;
- relation-level reviewed controls and frozen artifacts;
- durable experiment accounting and failure categorization for historical
  evaluation.

Experimental-only components:

- provider clients, prompts, schemas, model choices, token/cost accounting, and
  dispatch journals;
- v0/v1 evaluator and challenge orchestration;
- primary/challenge decision quality results;
- `hard_projection_authorized` admission tokens;
- any verifier-derived hard-candidate universe or compatibility projection.

No verifier decision, agreement, completeness score, confidence, or challenge
result can change packet authority.

## Completed shadow milestone and future integration

OE Semantic Authority Boundary v1 and OE Semantic Shadow Packet Integration v1
are complete. The explicit read-only shadow runner constructs and validates
`oe_semantic_matching_packet_v1`, but normal runtime, matching, deterministic
eligibility, lifecycle, persistence, and candidate UI paths neither construct
nor consume it.

The next future phase is a separately versioned semantic matching/reranking
integration. It may consume packets only for shortlist or reranking signals,
must preserve `semantic_non_exclusionary` authority, and must keep
`matching_deterministic_eligibility_decision_v1` disjoint.

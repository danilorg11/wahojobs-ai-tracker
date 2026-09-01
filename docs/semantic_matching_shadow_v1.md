# Semantic Matching Shadow v1

## Status and scope

`matching_semantic_shadow_seam_v1` is implemented as an offline, shadow-only
semantic comparison seam. It consumes native
`oe_semantic_matching_packet_v1` packets after deterministic eligibility and a
bounded shortlist have already been established. It does not alter the current
matcher, `/find-matches`, lifecycle/trust admission, profile persistence,
MatchRun persistence, candidate UI, infrastructure, or deployment behavior.

The implemented flow is:

```text
grounded canonical-profile matching projection
  -> existing deterministic hard-eligibility decision
  -> existing bounded shortlist survivor
  -> matching_semantic_shadow_request_v1
       + oe_semantic_matching_packet_v1 (when available)
  -> matching_semantic_shadow_local_evaluator_v1
  -> validated matching_semantic_shadow_result_v1
```

The local evaluator proves the architecture without an LLM or network call. It
uses exact normalized terms grounded in existing `GroundedFactV1` profile facts.
It is an evaluation control, not a production-quality semantic model.

## Versioned input contract

`matching_semantic_shadow_request_v1` contains:

- a request reference, profile revision reference, canonical-profile version,
  profile fingerprint, and taxonomy version;
- a maximum shortlist size of 32;
- existing score-free `GroundedFactV1` profile facts;
- bounded `matching_normalized_profile_semantic_signal_v1` comparison signals,
  each of which must reference and be textually grounded in one supplied profile
  fact;
- sorted `matching_semantic_shadow_candidate_v1` survivors.

Each survivor carries the existing `ShortlistCandidateV1` unchanged, including
its `DeterministicEligibilityDecisionV1`, inherited legacy rank, selected
variant reference, and `matching_semantic_packet_input_v1` availability state.

Packet input status is `available`, `unavailable`, or `invalid`. An available
packet must pass the complete existing packet validator, use
`oe_semantic_authority_boundary_v1`, have
`semantic_non_exclusionary` authority, name the same canonical opportunity, and
include the selected variant in its server-owned scope. A malformed or
scope-mismatched packet is reduced to an `invalid` no-packet state by the
fail-soft candidate adapter; the survivor itself is retained.

This request deliberately does not translate native semantic propositions into
legacy `attributes.requirements`, `GroundedFactV1` opportunity requirement
paths, score fields, or bucket labels. Those shapes would discard semantic
origin/authority and could recreate legacy matching truth.

## Deterministic eligibility boundary

Hard eligibility is complete and authoritative before this seam runs. The seam
accepts only `ShortlistCandidateV1` survivors, whose contract already rejects an
`ineligible` decision. It has no objective extractor, eligibility evaluator,
lifecycle/trust evaluator, exclusion disposition, or profile writer.

Result validation proves all of the following:

- output opportunity references equal the input survivor set exactly;
- every output item retains the same eligibility object and contract
  fingerprint as its input survivor;
- semantic exclusion count is zero;
- semantic negative-ranking-factor count is zero;
- semantic authority keeps deterministic failure, hard eligibility, candidate
  exclusion, lifecycle suppression, profile contradiction, and profile mutation
  unauthorized.

An upstream `unknown` deterministic eligibility survivor remains `unknown`; the
semantic seam cannot convert it to pass or fail.

## Semantic comparison and ordering

The local control evaluator compares a proposition only when:

- its accepted packet support state is `accepted_evidence_validated` or
  `supported`;
- its polarity is affirmed;
- its server-derived applicability includes the selected variant;
- all normalized typed-payload terms are present in one grounded profile signal
  of the same semantic kind.

The evaluator never interprets absence as contradiction. Proposition state is
therefore support or unknown for this milestone.

Constraint groups preserve the packet's exact bounded DNF:

```text
any_of(
  all_of(proposition, ...),
  all_of(proposition, ...),
  ...
)
```

A group has positive support only if one complete `all_of` branch is supported.
Some, but not all, members of an AND produce `partially_supported`; no supported
OR branch produces `unknown`. Both states have ranking effect `none`, never an
eligibility or exclusion effect.

Ordering is lexicographic over positive evidence only:

1. supported required groups;
2. supported preferred groups;
3. supported descriptive groups;
4. supported grounded unassigned propositions;
5. supported native responsibilities/candidate-profile signals;
6. inherited legacy rank as the stable tiebreaker.

There are no deductions, ratios, missing-data weights, coverage penalties,
thresholds, score bands, or legacy match buckets. A candidate can move down only
because another survivor has grounded positive support; no negative value is
assigned to the former candidate. Survivors with equal positive evidence retain
their existing relative order.

`required` and `preferred` remain semantic modalities under the same closed
non-exclusionary authority. Required support is useful positive evidence.
Unknown or partial required support is uncertainty, not failure. Preferred
support can break a relative tie between otherwise similarly supported
survivors, as demonstrated by the offline fixture.

Grounded propositions that were not representable in legacy enrichment fields
remain usable through the native packet. Packet `unassigned_proposition_ids` and
native descriptive signals also have explicit positive-only paths; neither is
routed through legacy compatibility projections.

## Missing and incomplete packets

An unavailable or invalid packet produces a ranked item with:

- the same opportunity and deterministic eligibility state;
- its inherited order unless another candidate has positive support;
- no semantic support factors;
- a structured `unavailable` or `invalid` coverage state;
- bounded candidate-safe uncertainty copy explaining that no semantic deduction
  was made.

For a valid but incomplete packet, the seam exposes `available_partial` and
specific bounded coverage reasons when it sees incomplete/unresolved groups,
unresolved propositions, retained invalid proposals, unassigned propositions,
unresolved variant coverage, or no semantic content. Coverage state is not a
sort key. Positively established complete branches may still help; missing or
unresolved material never hurts.

## Variant behavior

The selected job variant is explicit in every candidate input. Proposition,
group, and descriptive-signal applicability remains the server-derived packet
applicability. A proposition can support a selected variant only when that exact
variant appears in the proposition's applicability.

Facts tied to another variant become structured uncertainty for the selected
variant. They are not borrowed across variants, promoted to the canonical
opportunity, or interpreted as a mismatch. Output retains the packet policy that
`canonical_applicability` is `not_claimed` and
`canonical_fact_promotion_allowed` is false.

## Versioned output contract

`matching_semantic_shadow_result_v1` is bounded to the 32 input survivors and
contains a complete relative ranking. Each
`matching_semantic_shadow_item_v1` exposes:

- opportunity identity, selected variant, relative rank, and inherited legacy
  rank;
- the exact upstream deterministic eligibility decision;
- packet status, packet version/hash, coverage state, and partial-coverage flag;
- positive support counts, with an explicit empty negative-factor list;
- exact group logic plus per-branch supported, unknown, and
  variant-inapplicable proposition IDs;
- important profile fact references and accepted opportunity evidence-source
  references;
- concise grounded match explanations;
- bounded uncertainty/missing-information records;
- the closed semantic authority envelope.

Candidate-facing explanation and uncertainty strings are generated from a small
safe template set. Internal exception text, raw validation diagnostics, prompt
details, provider metadata, and developer trace data are not candidate-facing
copy.

The result also carries an invariant proof and an isolation block declaring
that runtime, `/find-matches`, candidate UI, lifecycle/trust, profile mutation,
database persistence, network, and model consumption are unauthorized.

### Provider-facing grounding closure

An authorized live evaluation uses
`wahojobs_semantic_shadow_grounding_request_v1` rather than exposing repository
reference serialization to the provider. Each request creates a closed,
request-local catalog:

- `J###` identifies one supplied opportunity for that request;
- `P###` identifies one atomic minimized profile fact or explicit grounding
  limitation;
- `O###` identifies one opportunity proposition, bounded relation/group,
  accepted evidence excerpt, or variant scope object.

The provider sees each opaque ID and its immutable structured meaning. Canonical
opportunity paths, proposition/group IDs, evidence IDs, source hashes, packet
hashes, and merge-back identities remain local. The strict response schema
enumerates the exact request-local IDs, and local validation independently
rejects invented IDs, serialized paths, duplicate IDs, and references belonging
to another opportunity.

Evidence-bearing output is structured rather than prose. The provider-visible
schema preserves the catalog role of every opportunity reference through
separate `proposition_reference_ids`, `group_reference_ids`,
`evidence_reference_ids`, and `scope_reference_ids` fields. Every material
finding also carries grounded `profile_reference_ids`, a closed relationship
type, a finding type, and a non-numeric ranking effect. At least one proposition
is mandatory, and a proposition in a bounded relation must carry exactly its
group references. Evidence excerpts and scope material may corroborate or
qualify a finding, but cannot substitute for proposition/group semantic
grounding or promote raw evidence into semantic truth. Domain context is
explicitly non-ranking; language or domain evidence cannot establish education;
and only matching semantic dimensions may form positive support. Free-form
evidentiary claims are not part of the output contract.

The strict output schema is mode-specific. `relative_reranking` retains closed
ordering groups over all request opportunities. In
`single_opportunity_assessment`, `relative_ordering_groups` is structurally
constrained to zero items, so a one-packet assessment cannot emit a relative
rank claim. The local validator independently enforces the same reference-role,
opportunity-ownership, relation-completeness, missingness, ranking-mode,
authority, and variant-scope rules.

Missingness states are permanently distinct: `not_grounded`, `not_specified`,
`unknown`, `unresolved`, and `unavailable`. None asserts factual absence or
negative fit. Missing profile entries cannot participate in findings. Incomplete
or unresolved opportunity material can appear only as partial alignment with a
matching structured, non-negative uncertainty. A survivor with no positive
support can only remain in the final equivalence group alongside all other
zero-support survivors; absence itself is never a negative sort factor.

## Offline evaluation

`scripts/semantic_matching_shadow_report.py --verify` reads only local fixtures.
Its primary architecture scenario combines a synthetic grounded profile with
three existing reviewed semantic cases:

- `advanced_degree_or_professional_standing` for exact OR and a grounded legacy-
  unprojected proposition;
- `required_capability_preferred_experience_control` for distinct required and
  preferred effects;
- `bilingual_all_required_control` for exact AND and partial support.

It also includes a missing-packet survivor. Results:

- 4 deterministic survivors in and 4 ranked shadow items out;
- 0 semantic exclusions;
- 0 negative semantic ranking factors;
- 2 positions changed because preferred positive support broke a tie;
- exact OR and AND structure preserved;
- partial required support retained with no ranking or exclusion effect;
- the missing-packet survivor retained;
- deterministic eligibility fingerprints unchanged;
- no runtime/UI import of the seam;
- 0 model, provider, network, database, runtime matcher, persistence, or UI
  calls.

The report remains anchored to the existing
`matching_foundation_evaluation_v2` fixture: 30 human-reviewed relevance cases,
9 reviewed/derived eligibility cases, and 3 human-reviewed opportunity-
relationship cases. Existing foundation and route regression suites remain the
authoritative compatibility checks; this milestone does not relabel or replace
them.

## Runtime and legacy isolation

No module under `wahojobs/` imports `wahojobs.matching.semantic_shadow`.
The only executable consumer is the explicitly invoked offline report script.
In particular, `wahojobs/authenticated_profile_matches.py` and
`scripts/local_product_app.py` contain no semantic-shadow or semantic-packet
consumer. No runtime/UI route can observe the shadow rank.

The compatibility proof requires the existing matching-foundation report,
matching contracts, semantic authority/packet tests, and complete
`test_find_matches_route_regression` suite to pass. The two `/find-matches`
implementation files must remain byte-identical to their baseline Git blobs.

## Smallest proposed live shadow evaluation

The proposed next evaluation is deliberately limited to three existing reviewed
benchmark profile IDs:

- `biology_or_medicine_academic`;
- `multilingual_translator`;
- `software_engineer`.

For each profile, freeze the top 12 deterministic eligibility survivors from
the current legacy order, including two missing-packet controls where available.
That caps the run at 36 profile-opportunity comparisons. Compare survivor-set
identity, legacy rank versus semantic shadow rank, reviewed relevance ordering,
pairwise rank changes, and coverage/uncertainty. Do not integrate or persist the
result.

The current matching-foundation snapshot reports 0 of 2,530 active canonicals
as mechanically complete or quality-approved for its older semantic-packet
readiness indicator. That does not invalidate the seam, which is closed over
validated native packets and frozen fixtures, but it is an operational data-
coverage constraint on a live sample. A future run must first perform a read-
only packet-availability audit and reduce the sample below 36 if necessary; it
must not manufacture coverage or drop missing-packet controls.

A meaningful semantic comparison beyond the exact local control would require
an external model to receive a minimized profile matching projection and native
opportunity packet data. The proposed payload would contain:

- normalized education, experience/domain signals, skills, languages,
  location/work-authorization constraints, and explicit preferences from the
  selected canonical-profile revision;
- profile fact references/provenance classes and a pseudonymous profile
  revision reference;
- deterministic eligibility status and inherited legacy rank;
- native opportunity propositions, required/preferred/descriptive DNF groups,
  variant applicability, accepted evidence excerpts/references, packet coverage,
  and authority metadata.

It would exclude name, email, account/session identifiers, raw resume/document
bytes, and unrelated profile fields. Even with that minimization, this is
candidate/profile data. Prior opportunity-evidence authorization does not cover
it. The live model evaluation is therefore not executed and requires explicit
authorization for this exact data class and bounded run.

## Current limitation, not an architecture blocker

The exact-term local evaluator establishes the safety and data-flow invariants
but cannot demonstrate real semantic-ranking quality. That quality question is
intentionally deferred to the authorized bounded evaluation above. No
architecture blocker was found for the shadow seam itself.

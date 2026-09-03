# Semantic ranker development v1

This is an isolated development method. It is not connected to `/find-matches`,
production matching, routing, authentication, databases, or deployment.

## Benchmark status

The 60 human judgments in the historically named
`prospective_semantic_ranking_benchmark_v1` were collected without semantic ranking
being shown to the reviewer. The complete ranker was not specified before label
freeze. The corpus is therefore development/calibration data, not a prospective
test of this implementation. Labels remain immutable at SHA-256
`f283abe2b3431d06f870e0877f6abc0ac9209cdb045b465198ad86ac5a27fdb6`.
A new v2 set is required after method freeze. No v2 population is created here.

## Reused foundation and new components

The September 1 commits `5cf9da285e55eb42cf3ff09693a2295e85c34c86` and
`1b00ede0a698ffc51014f05a9e192c54c9c860f6` supply the local shadow boundary and
provider opaque-reference grounding contract. The packet validator, authority
policy, evidence identities, typed proposition/group catalogs, and request-local
reference builder are reused unchanged.

The old local evaluator remains an architecture control; it is not the new
ranker. The old provider-output v1 contract remains unchanged and independently
tested. Its relative-ordering response is not misrepresented as an implemented
scorer. New `matching_semantic_fit_output_v1` deliberately adds evidence-supported
fit classes and diagnostic issues, using the same authenticated input catalogs.
It does not alter the semantics or authority of any stored packet.

The missing profile adapter, provider client/configuration, result interpretation,
full-list integration, failure policy, journal, and development evaluation are now
implemented in separate files. The evaluation module alone reads human labels.

## Complete runtime contract

1. **Profile adapter:** Copy an explicit allowlist of stated fields into typed
   atomic facts. Summary/constraints remain exact text. Combined degrees/domain
   entries become domain background, never inferred credentials. Missing fields
   become `not_specified`; display names, notes, labels, and metrics are excluded.
   No profile parsing API or learned adapter is used.
2. **Opportunity adapter:** Validate existing `oe_semantic_matching_packet_v1`.
   Preserve all reference meanings, quoted evidence, variants, incomplete groups,
   and unresolved proposition states. Use the existing opaque catalog builder.
   The existing listing title is context only, not independent fit proof. No newer
   page, body, extraction, verifier, or supplemental evidence is obtained.
3. **Provider request:** One batch per profile containing up to 32 admitted
   opportunities. Input order is SHA-256-seeded by ordinary opportunity identity,
   independent of legacy order. Opaque P/O/J IDs are request-local and merge back
   locally. Only non-empty valid packets are sent. Shared opportunities reuse the
   same packet; different profiles necessarily receive separate assessments.
4. **New development model choice:** One candidate, `gpt-5.6-terra`, reasoning
   `low`, `store:false`, maximum 9,000 output tokens, temperature omitted. This
   follows a working project structured-output convention, but is a new ranking
   choice—not retroactive use of the extraction authorization. No model sweep.
5. **Prompt/output:** Prompt/version is in `scripts/semantic_development_core.py`. The strict
   schema contains a closed object keyed by the exact supplied opportunity IDs.
   Each assessment contains fit, typed reference arrays, provisional/supported
   evidence state, a bounded reason, and bounded diagnostic issues. No provider
   numeric score, human relevance scale, final order, or eligibility field exists.
6. **Validation:** Exact object keys/types/enums/lengths; unique references;
   profile references must be grounded; no cross-opportunity references; every
   cited proposition needs its groups and an attached evidence quote. Pending or
   incomplete evidence cannot be promoted to supported. Conflicts need profile
   and opportunity citations; an unlisted language alone cannot prove conflict.
   This is structural/reference validation, not a claim of independent semantic
   entailment verification. The model can still make interpretive errors.
7. **Relevance:** `direct=3`, `adjacent=2`, `contextual=1`,
   `unestablished=0`, `explicit_conflict=0`. These are evidence-support classes,
   not human labels or probabilities. Primary work matters more than generic AI,
   English, remote, or writing overlap. Unsupported specialization is uncertainty
   about corresponding support, not an assertion the candidate lacks a skill.
   A provisional direct alignment does not assert complete requirement satisfaction.
8. **Empty/missing/invalid evidence:** Retain the candidate at its inherited
   legacy position. Do not call the provider for it. Sort only the other positions.
   This avoids silently dropping records or treating unavailable evidence as a
   negative grade. It can preserve a legacy error: that is an explicit limitation.
9. **Failure:** One HTTP attempt per profile, no automatic retries or repair calls.
   Transport, API, incomplete, refusal, schema, or reference-validation failure
   causes the entire profile's original legacy order to be returned. Raw output
   is retained even on validation failure. A pre-call marker prevents accidental
   duplicate execution after an uncertain interruption. No partial-response merge.
10. **Integration candidates:** `full` sorts evidence-covered candidates by
    descending fit value in their available slots. `ties_only` performs that same
    operation only inside contiguous equal-legacy-score blocks. Empty positions
    remain pinned in both. Eligibility decisions and population cannot change.
11. **Ties:** Equal semantic values preserve the inherited legacy total order;
    ordinary identity is a last deterministic safeguard. All final results are
    total orders. Diagnostic tie analysis separately reports unresolved equal
    semantic classes; it does not confuse legacy tie-breaking with semantic proof.
12. **Reproducibility:** Save prompt/config/code/request/schema hashes before
    calls; raw API responses, response IDs/models, usage, latency, and cost estimates;
    integrated outputs separately; exact output hashes. Replay from fixed responses
    is deterministic. Provider re-execution is not claimed bitwise deterministic.

All semantic authority remains `semantic_non_exclusionary`; no hard exclusion is
authorized, including for an explicit semantic conflict. Existing objective
eligibility runs before this layer and is not changed by it.

## Development comparison and selection

The same six responses feed two fixed integration candidates; there are no fitted
weights or score thresholds. The policy is recorded in the experiment manifest
before provider execution: prefer full only when pairwise correctness improves,
NDCG@3 does not decline, top-3/top-5 false positives do not increase, and top-3/top-5
Strong/Plausible counts do not decline. Otherwise try ties-only under the same
guardrails. If neither meets them, the simpler ties-only specification may still
be reviewed but is not a demonstrated quality improvement or production winner.

Metrics use all 60 final-ranking judgments, including fallback records. This is
an explicit development full-integration denominator, not the original non-empty
packet-only prospective quality denominator. Both legacy and semantic candidates
use the same implementation: exponential-gain NDCG@3/5/10; top-3/5 counts;
cross-label pair accuracy; Kendall tau-b preserving human ties. Final deterministic
ties do not exist; semantic-class equivalences are reported separately.

Report per-profile results, tie and non-tie changes, remaining inversions and
regressions, and leave-one-profile-out integration-selection diagnostics. These
are internal robustness checks; they cannot undo v1 development-data reuse.
There are only six profiles, one reviewer, one opportunity provider, shared
opportunities, and one stochastic model run. No significance or unbiased-effect
claim is justified.

## Execution and freeze boundary

Commands are `scripts/semantic_ranker_development.py prepare`, `execute`, `replay`,
and `scripts/evaluate_semantic_development.py baseline` / `evaluate`.
The current benchmark adapter is explicitly v1-shaped (6/60/52). A v2 adapter
would reuse the frozen runtime method rather than changing its interpretation.

Freeze the selected integration plus provider configuration, prompt, output-schema
builder, validation, profile and opportunity adapters, ordering seed, tie/fallback
policy, dependencies, implementation hashes, and tests before any v2 sampling.
Also freeze v2 sampling/evaluation rules separately before inspecting its outcomes.
The current development result requires review before that freeze. Nothing here
enables production integration or creates a future holdout.

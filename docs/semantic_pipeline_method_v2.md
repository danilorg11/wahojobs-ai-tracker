# Complete semantic pipeline freeze for a fresh prospective benchmark

Method identity: **wahojobs_semantic_pipeline_method_v2**. This is a method
version, not a sampled benchmark. Its canonical contract is
`docs/semantic_pipeline_method_v2.freeze.json`; the commit containing that
manifest is the new prospective provenance anchor.

## Historical status

Commit `391bb7a89fa10cb6bd82b7a41e0c56bcc558d783` and
`wahojobs_semantic_ranking_method_v1` remain an unchanged **downstream-only**
freeze. The previous readiness claim omitted upstream packet provisioning.
Its manifest and hashes are preserved, not retroactively reinterpreted.
Development benchmark v1 remains development/calibration data, not unbiased
effectiveness evidence. Its human labels are neither required nor read here.

The first prospective v2 sample was invalidated before provider execution,
human exposure or human labeling (all zero). It remains preserved and ineligible
for effectiveness evaluation. No replacement v2 was sampled or inspected while
defining this method. Neither its opportunity content nor v1 human outcomes
influenced the provisioning rules.

## Complete boundary and invocation

This opt-in utility is not imported by production matching or `/find-matches`.
It consumes an explicitly supplied, quiescent frozen canonical source snapshot;
normal eligibility and canonicalization are upstream, unchanged inputs, not new
semantic gates. No inventory sampling, crawling, current-page lookup or database
write exists in the semantic runtime.

1. Run `verify_semantic_pipeline_freeze.py` before preparation and execution.
2. `semantic_pipeline_evidence.capture_opportunity(snapshot, canonical_id)` reads
   one supplied opportunity through a read-only immutable SQLite connection.
   `load_semantic_input` selects active variants, or all available variants if
   none are active, excluding pending/simulation rows. The existing repository
   verifier authenticates accepted capture history and material hashes. The
   promoted v1 binder requires complete versioned accepted/LKG, healthy,
   promoted, nonsample, body-present capture coverage. Missing/invalid authority
   returns an unavailable source; it never changes eligibility or membership.
3. `seal_evidence` records canonical identity, exact normalized semantic input,
   exact `llm_source_packet`, accepted capture provenance and hashes. A trusted
   caller may supply these already-frozen server parts instead of reopening the
   database. Hashes authenticate against that trusted snapshot, not an untrusted
   caller's assertion. The snapshot must not have uncheckpointed WAL changes.
4. `semantic_pipeline_v2.prepare_provisioning(sources, cache_directory=...)`
   deduplicates shared canonical opportunities; conflicting duplicate evidence
   is rejected. It prepares one opportunity-only request per exact cache miss.
   Provider blinding uses the original allowlist/body text and neutral local
   variant aliases, never benchmark IDs, profile facts or ranking metadata.
5. Only with separate, destination-specific authorization,
   `execute_prepared('extraction', canonical_ref, item, ...)` transmits that exact
   prepared body. The authorization argument is a map of exact request IDs to
   body hashes for the explicit Responses endpoint; it does not imply permission.
6. `finish_provisioning(plan, results, cache_directory=...)` authenticates aliases
   and quote spans; runs the historical strict extraction validation, provisional
   staging and deterministic relation construction; builds and validates packets
   and checks evidence/source/capture provenance. Results retain every canonical
   ID, raw extraction, receipt, validation status and failure reason.
7. Only after provisioning is settled, `prepare_assessments(profile_batches,
   provisioned)` creates the unchanged per-profile assessment request. A batch
   contains only an opaque local profile key, allowlisted normal profile facts,
   and canonical candidate references/titles. Profile keys are not transmitted.
   There is one request per profile with at least one nonempty packet, at most
   32 covered candidates per batch as in the frozen downstream contract.
8. A separate explicit authorization permits `execute_prepared('assessment',
   profile_key, prepared, ...)`. No ranking call is needed for all-empty profiles.
9. `integrate_rankings(prepared, assessment_results, legacy_by_profile)` is the
   **only** pipeline stage receiving legacy scores/order. It delegates exactly
   to the historical frozen `ties_only` implementation, retaining all candidates.

Preparation and replay are offline and never implicitly execute a provider.
The stage functions accept ordinary production-shaped inputs, not benchmark
exports. Callers persist frozen input plans and returned artifacts separately
from source evidence. Synthetic tests exercise this whole sequence in a clean
repository projection with no exports, production DB or credentials.

## Reused extraction behavior

The September 1 generic infrastructure and September 2 v1 assembly are reused:
`opportunity_enrichment`, `opportunity_semantic_extraction`,
`opportunity_semantic_staging`, `opportunity_semantic_authority`, and their frozen
transitive dependencies. New generic orchestration removes the old harness's
fixed membership, 49-call authorization and three manually named replay cases.

Extraction uses OpenAI `https://api.openai.com/v1/responses`,
`gpt-5.6-terra`, reasoning `low`, `store:false`, 12000 maximum output tokens;
temperature is omitted. Prompt: `oe_semantic_extraction_v0_prompt_v3`.
Schema: `oe_semantic_extraction_v0_schema_v1`; strict Responses format name
`oe_semantic_extraction_v0`, with evidence-alias enums generated per request.
Timeout: (10, 120) seconds. There is no grouping-model call or verifier v0/v1.

The request builder is the historical client intercepted by an inert session
during preparation, not a separately reimplemented prompt/schema builder. New
transport orchestration disallows redirects/retries, records a durable attempt
before dispatch and requires a completed extraction response. No semantic
interpretation rule was changed.

Packet assembly is the exact v1 recipe: `validate_model_extraction`,
`stage_provisional_atoms`, `construct_relations(staging, None)`, server-derived
variant relationships, `build_semantic_matching_packet_from_staging`, then
packet/provenance validation. `semantic_grouping_version=None` and
`descriptive_signals=[]` are intentional. The generic shadow helper's optional
native descriptive signals/grouping are **not** substituted for this recipe.
Individual rejected proposals/unresolved source alternatives are handled exactly
by the existing validator/staging logic. Missing evidence is not a mismatch.

## Exact reuse, cache and provenance

The compatibility key hashes method/cache version, canonical identity, complete
frozen evidence hash, semantic-input version/hash, source-packet hash, accepted
bindings hash, endpoint, complete generation configuration, prompt/schema
versions and hashes, exact request-body hash, packet recipe version and its
direct implementation hashes. All transitive behavior is additionally immutable
under the method identity and checked by the freeze verifier before use.

Only that key's cache entry is considered: no directory-wide best-match search,
title similarity, stale opportunity reuse, manual aliasing or manual selection.
An entry must have matching identity, integrity and raw-output hashes plus a
completed provider receipt with exact request/configuration provenance. A hit
replays the original extraction through validation/assembly and retains the
receipt. A valid empty extraction is also a hit, never automatically repaired.
Missing, stale, incompatible or corrupt entries produce a cache miss. Existing
entries are never overwritten. Cache write failure retains the in-memory result.

Historical v1 outputs lack the new exact generic request/cache identity: they
are allowlisted **explicit offline replay fixtures**, not automatic production
cache hits. Generic provider aliases differ from the old benchmark-prefixed
aliases; original accepted evidence-block IDs and text remain unchanged. The
replay proves packet bytes and downstream request/ranking identity, not that a
fresh model call must emit identical extraction text. No outputs are regenerated.

Provider journals preserve exact request IDs/hashes, endpoint, stage, timestamps,
raw provider envelope, parsed output, usage/cost where available and integrity
hashes. Existing complete matching results replay locally. An uncertain attempt
or corrupt result never triggers an automatic resend. Missing authorization,
credentials or durable journal prevents dispatch. There is at most one HTTP
attempt for each authorized request; no retries, repair calls or alternatives.

## Downstream behavior and failures

Assessment remains `matching_semantic_development_v1`, prompt
`matching_semantic_development_prompt_v1`, schema `matching_semantic_fit_output_v1`:
same endpoint/model/reasoning/store setting, maximum output tokens **9000**,
temperature omitted, timeout (10, 180). Dynamic schema/reference validation,
profile adapter and packet adapter are unchanged. Provider ordering uses the
existing deterministic seed, independent of legacy ordering.

Fit precedence remains direct=3, adjacent=2, contextual=1,
unestablished=0, explicit_conflict=0. Reorder covered slots **only within
contiguous equal legacy-score groups**. Equal semantic values retain legacy
order; empty/unavailable/invalid packets retain their original positions.
Provider or semantic-assessment validation failure restores the whole profile's
legacy ranking. Extraction unavailability/failure/invalid packet produces that
opportunity's unavailable-packet fallback; no opportunity is silently dropped.
Insufficient profile facts/invalid caller populations fail input validation before
execution and require the caller to retain the original legacy result, not repair
or substitute an input. No eligibility rules change.

Authority is always `semantic_non_exclusionary`, with zero semantic hard
exclusions. Explicit conflict is a semantic fit class, not an eligibility gate.
No human labels, notes, metrics, expected rankings, strata or error analyses
enter either provider stage. Normal legacy inputs enter only local integration.

## Reproducibility and future holdout rule

`python -B scripts/verify_semantic_pipeline_freeze.py --replay-v1` rebuilds all
52 packets (49 nonempty, three empty), recreates all six assessment requests
exactly, validates the six saved assessment responses and retains all 60
judgments. The original ranking serialization must hash to
`ec012d94659d8abfa74a37aa50b00cffbca998c5fa03a31bca559ef62ec7a1e7`.
The verifier reads no human labels and writes no replay outputs. Generated
machine artifacts remain local, pinned by existing checksum references; they
are not copied into the freeze commit.

The manifest pins repository text after CRLF-to-LF normalization, explicit
component hashes, model configurations, prompts/schemas and runtime versions.
SQLite schema is pinned for source compatibility/synthetic tests. Broad imported
modules are pinned conservatively; unused overlay/import/migration functions
are never executed. No mutable overlay file is a runtime input. Provider model
aliases may evolve externally: deterministic reproduction is guaranteed from
frozen provider outputs, not repeated fresh model sampling.

From the new freeze commit until a fresh replacement v2 is fully human-reviewed
and unblinded, evidence selection/binding, extraction prompt/schema/config,
cache/reuse, packet construction, assessment prompt/schema/config, validation,
class interpretation, integration and all fallback behavior are immutable.
If any behavioral change is necessary after sampling, invalidate that sample,
make a new freeze commit, and only then generate a fresh universe. No replacement
sample is created by this freeze. This is readiness for prospective testing,
not proof of semantic effectiveness.

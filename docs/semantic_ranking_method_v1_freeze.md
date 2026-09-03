# Semantic ranking method v1: formal pre-v2 freeze

The canonical manifest is `docs/semantic_ranking_method_v1.freeze.json`.
Its enclosing commit is the formal methodological provenance anchor; the manifest
records the parent/source commit rather than a circular self-commit hash.

## Identity and unchanged behavior

Frozen method: `wahojobs_semantic_ranking_method_v1`.
Underlying implementation: `matching_semantic_development_v1`.
Selected integration: `ties_only`.

`scripts/frozen_semantic_ranking_v1.py` is a thin canonical entry point over the
unchanged audited `scripts/semantic_development_core.py`. Its request preparation,
provider body/client and validation are exact aliases. Its integration delegates
with `mode="ties_only"`; it does not expose the development full-reordering option.
No prompt, schema, fit class, weight, threshold, eligibility rule or scoring
interpretation changed during promotion.

Fit precedence is direct=3, adjacent=2, contextual=1, unestablished=0 and
explicit_conflict=0. Only covered positions within contiguous equal-legacy-score
groups are reordered. Equal semantic values keep legacy order. Empty/missing/invalid
packets retain their original positions; provider/validation failure restores the
whole profile's legacy order. All authority is `semantic_non_exclusionary`, with no
hard exclusions. Missing evidence is not proof of mismatch.

Provider configuration is OpenAI Responses `/v1/responses`, `gpt-5.6-terra`,
reasoning `low`, `store:false`, maximum output tokens 9,000. Temperature is omitted;
no seed or other generation setting is invented. The client makes one attempt,
does not follow redirects, and does not retry/repair. Future execution requires
separate authorization and must preserve the existing pre-dispatch journal and
stop-on-uncertain-attempt discipline. Freezing does not authorize provider calls.

## Boundary and dependencies

Runtime accepts literal profile facts, authenticated semantic packets/title context,
and the ordinary admitted legacy base order/scores used only by local integration.
It does not read benchmark exports, labels, notes, metrics, error analyses or expected
rankings. The v1-specific CLI is retained for reproducibility, not used as the v2
runtime loader. Future orchestration supplies the same inputs to the canonical API;
it must not add a new profile/evidence interpretation or change batching semantics.

The manifest pins the runtime source closure, including previously unlisted package
initializers, and Python/Unicode/HTTP-library versions. It references the dependency
lock at the immutable parent commit without unnecessarily freezing unrelated product
dependencies. File hashes use repository LF text bytes; only CRLF-to-LF normalization
is permitted, because this repository uses Git autocrlf on Windows. Original audited
file byte hashes are retained separately where applicable.

The strict output schema is request-specific because it enumerates the supplied
opaque IDs. Its builder source and canonical probe hash are frozen; v1's six exact
schema hashes are immutable references. Each future request must record its generated
schema hash. A changed population's IDs do not authorize a schema-builder change.

## Offline checks

From the repository root:

```text
python -B scripts/verify_semantic_method_freeze.py
python -B -m unittest discover -s tests -p test_frozen_semantic_ranking_v1.py
python -B scripts/verify_semantic_method_freeze.py --git-ref HEAD
```

For the existing local v1 artifacts only, add `--replay-v1` followed by the existing
`semantic_ranker_development_20260903/candidate_01` directory. Replay is in-memory,
checks input/raw hashes, reads no human labels or metrics, writes nothing, and must
reproduce `ec012d94659d8abfa74a37aa50b00cffbca998c5fa03a31bca559ef62ec7a1e7`.

The portable committed tests use existing contract fixtures, not v1 exports or a
v2 population. The existing artifact-dependent v1 development/evaluation tests stay
local and were also run for this freeze. Generated raw responses, labels, analysis
bundles, local UI files and databases are deliberately not added to the commit.

## V1 status and v2 immutability rule

V1 was human-labeled development/calibration data. Its judgments were collected
without semantic ranking being shown, but the complete method was developed after
label freeze. Its performance is not prospective or unbiased evidence. The accepted
limitations include partial packet coverage, coarse semantic classes and conservative
non-tied legacy preservation. No further v1 tuning is recommended before v2.

At this freeze, Benchmark v2 has not been sampled, generated, inspected or exposed.
This commit contains no v2 population, review UI or production matching integration.

From this freeze commit until v2 has been completely human-reviewed and unblinded,
the semantic behavior, prompt, schema builder, model/configuration, adapters,
integration and fallback behavior are immutable. The upstream packet contract and
normal eligibility/legacy boundaries may not be changed in a way that changes v2
inputs or ordering without invalidation. Freeze v2's sampling/evaluation protocol
before sampling, without using it to alter this method.

If any behaviorally relevant change becomes necessary, invalidate/discard the
in-progress v2 benchmark (whether review has not begun or is incomplete), create a
new freeze commit, then generate a fresh v2 universe from scratch. Never reuse the
old v2 outcomes to characterize the changed method as prospective. No deployment,
production matching integration or push is authorized by this freeze.

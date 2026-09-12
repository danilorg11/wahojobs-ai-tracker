# Selective professional-background preparation

This is a default-disabled, explicit **in-process** operator operation. It prepares occupational relevance only. It does not change scores, ranking, section policy, admission rules, source enrichment, profile fields or optional item-experience authority. The exploratory barrier remains. No automatic recognition quality or delivery improvement is claimed.

## Connected invocation

The supported composition accepts `professional_background_preparer` in `build_workos_authkit_staging_runtime(...)` and `_build_profile_integration(...)`. Both leave it `None` by default; environment flags alone cannot activate it. The returned runtime/profile integration exposes `prepare_professional_background(...)`. The browser integration passes its own authenticated profile service and read-only connection provider to the preparer, and uses **that preparer's evidence instance** for matching.

For a future explicitly authorized composition, construct the preparer with `configured_background_preparer(enabled=True, allow_real_requests=True, budget=budget, audit_sink=sink)`, then inject it when constructing the runtime. The factory uses the existing `configured_openai_client`, `OPENAI_API_KEY` and `WAHOJOBS_OPENAI_ENRICHMENT_MODEL` configuration. This task does not change a running composition, start a server or authorize that configuration.

Once the operator already holds the runtime and valid owner session credentials, the exact supported Python invocation is:

```python
selection = dict(
    profile_id=selected_profile_id,
    job_ids=[selected_exact_job_id],  # at most eight explicit distinct IDs
    authentication_input=authentication_input,
    session_token=session_token,
    csrf_secret=csrf_secret,
)
plan = runtime.prepare_professional_background(**selection)
# Inspect plan['items'], their model_input, source_binding, size and reservation.
result = runtime.prepare_professional_background(
    **selection, execute=True, authorized=True,
    expected_plan_id=plan['plan_id'],
)
```

The ordinary POST authentication/CSRF/profile authorization service resolves the owner and authoritative current confirmed profile. There is no new public endpoint. GET/HEAD matching and details only perform provider lookup. Selection reads the exact job IDs and validates their accepted captures, even if they have never been displayed or admitted. The dry-run fingerprint binds owner, profile ID/revision/content, exact source identity/capture/material, complete clause/context, modality, component/semantic version, recipe/model/basis. A different owner's session cannot execute a plan merely by supplying its ID.

The producer path is `runtime/profile integration → matching integration.prepare_professional_background → ProfessionalBackgroundPreparer → authorized profile service + load_card_sources/accepted_source_binding → prepare_card_evidence/build_request → configured client.generate_structured → validate_response → ProfessionalBackgroundEvidence.publish`. Matching and details independently rebuild the same committed requests and use `lookup`; the existing provider generation in `_recommendation_input_key` invalidates old match contexts on publication/replacement/eviction. The local model identity policy digest also participates in request binding, provider validity and match keys.

## Selection, reuse and failure

The dry-run reports `needs_preparation`, `reusable`, `skipped` or `limitation`, with reasons, bounded payloads, reservations and remaining accounting. It makes no client call. Missing authoritative role facts (including P01), unsupported clauses, unaccepted source evidence and existing deterministic occupational support are skipped. The v1 producer deliberately inherits the committed narrow span grammar; it is not a general professional classifier.

Valid partial, ambiguous, contradicted and no-support semantic results all reuse. An explicit `replace=True` permits a bounded replacement; improving a match is never a regeneration condition. Publishing increments the existing generation, so no cache clearing is required. A failed replacement retains any earlier valid result. Identity/version/context changes make prior results unusable. A new dry-run is required after changes; the producer rechecks authorization, facts and accepted evidence before each call and again after its response. The consumer's exact binding protects against subsequent changes as well; this is not an atomic database transaction across a network call.

Matching and authenticated details capture a provider generation token before reading comparison/cache state. Computation and storage reads remain tentative outside the lock. `consume_generation(token)` then uses the provider's existing in-process lock to check that generation and hold it through local rendering and match-context acceptance/registration. Publication uses the same lock. A replacement completed before this boundary causes an explicit HTTP 503 (`professional_evidence_changed_during_consumption` internally), without accepting mixed or superseded support or registering a fabricated empty success. There is one bounded attempt and no automatic recomputation loop. Unchanged generations reuse normally. A publication after the boundary waits until acceptance ends; a response already accepted is not promised to reflect later publications. No model/network call or database read holds this provider lock. This does not add cross-worker synchronization.

Execution requires the enabled injected preparer, a matching client/model/basis, `authorized=True`, the exact dry-run fingerprint and a `PreparationBudget`. Real execution additionally requires `allow_real_requests=True`, the configured real client, and positive explicitly supplied USD ceiling and input/output rates. The legacy client price table is **observability only**, not verified current pricing. Rates must be verified/authorized separately before a paid pilot.

One preparer allows at most eight lifetime attempts; the pilot caps this at six. Input is at most 24,000 serialized UTF-8 bytes for prompt/payload/schema, with a 1,024-token framing reservation; output is at most 2,048 tokens and 32,768 response bytes. Reserve one token per input byte plus framing and output limit, with no cache discount or refund for unknown usage. Also reserve cost using the explicitly authorized rates. Actual input/output/total tokens and legacy estimated cost come from the existing client accounting. Every attempt retains its reservation, including failures. Exhaustion blocks further calls. There is no retry, repair, scheduler or backfill. Reconstructing a preparer starts a new operator budget; it is not an account-wide spend ledger.

The bounded client path dispatches exactly once through the configured Requests session's standard `HTTPAdapter`, requiring zero transport retries and rejecting response hooks/custom authentication. It does not enter redirect-capable `Session.send`; 301/302/303/307/308 fail without following Location or resubmitting the body. A callback records the already-reserved physical attempt immediately before `adapter.send`. Timeout, HTTP failure, refusal and invalid output retain their request/token/cost reservations. Rejected transport configuration consumes its reservation but records zero physical dispatches. Unknown usage is explicitly marked; zero known usage is distinct. The ordinary unbounded shared enrichment client path retains its existing behavior.

Requested and transport-returned model names remain separate provenance fields. Exact identity is supported, plus only the local fixture pair `gpt-5-mini` → `gpt-5-mini-2025-08-07`, in budget class `gpt-5-mini`. This is not model discovery or a claim about the current alias target. Missing/malformed names and arbitrary prefix matches fail. The model's JSON content cannot authorize identity; the top-level transport response metadata is checked against the versioned local policy before publication and again on lookup. Policy changes invalidate plans/results/context reuse. A pair authorizing a different budget class is rejected; this mapping supplies no prices or permission to spend.

Missing/malformed/refused/incomplete/failed/oversized responses, invalid evidence references and stale identities cannot publish. Failures have bounded content-free process-local records. An optional synchronous audit sink receives the minimized request, unedited bounded response bytes, validated output/usage or failure; audit failure prevents publication. Oversized responses close the stream without claiming a complete raw response. No arbitrary exception text, secrets or refusal content enters operational failure records.

## Payload and authority

Only confirmed `experience.recent_roles`/`job_titles` values (maximum eight bounded labels) and the complete accepted source description reach the model. The full description is deliberately retained because applicable alternatives/qualifiers can occur outside the target block. The model receives the exact requirement quote, heading/modality, occupational span and opaque fact/request references. It does not receive owner/profile/source identifiers, names, contact fields, provenance records, general career years, domain-duration fields, raw resumes, private notes, unrelated profile fields, optional item details or historical judgments.

No source description is truncated. Oversized context or detectable contact/credential material, URLs or a known candidate name in needed text produces a preparation limitation. This reject-only gate uses the existing contact detector and known identity values; it is not a general named-entity recognizer. Arbitrary names embedded in free-text labels that are not recorded identity values cannot be comprehensively recognized by this deterministic gate. Review the inspectable payload for such material before authorizing a pair; do not execute if minimization cannot be established. No role equivalence lists are introduced. Source and candidate text are expressly untrusted data in the system prompt.

Output supplies only `supported_partial`, `not_established`, `ambiguous` or `contradicted` occupational relation, grounded fact IDs, exact source span and bounded rationale. It cannot establish professional competence, duration, responsibilities/depth, proficiency, complete eligibility, scores or admission. Existing deterministic contradictions and exact arithmetic retain precedence. Associated degree alternatives remain source-bound group decisions. Confirmation provenance means a declared fact, not independently verified competence.

## Offline validation and next pilot

`tests.professional_background_preparation_support` constructs the actual `_build_profile_integration` composition over disposable storage. It explicitly substitutes synthetic authenticated-state resolution and an offline HTTP session; accepted capture binding, producer, shared transport parsing, provider publication, matching, detail rendering and context invalidation execute production code. This is not a real-login test or classifier-quality evaluation. The original authentication regression selection remains intact.

`scripts.professional_background_pilot` supplies an executable seven-case pilot: P01/P02 on preserved source wording, unrelated biology practice, known two-year shortfall, exact five-year boundary, associated degree alternative and required-language conflict. P01 generates no request, so the proposed ceiling is **six requests and 150,000 reserved tokens**. Contrast-source edits are explicitly synthetic. `tests/fixtures/professional_background_frozen_source.json` copies only the preserved accepted source body/format/metadata and its origin receipt; synthetic candidate facts are generated locally. Expected quality hypotheses are written separately and never included in model input.

The script defaults to `--mode dry-run`. `--mode offline` uses labelled stub responses. A real run requires **all** of `--mode real --authorize-real-requests`, configured credentials/model, `--service-tier default`, `--request-limit 6 --token-limit 150000`, a positive `--usd-limit`, and verified positive `--input-usd-per-million` / `--output-usd-per-million`. The frozen authorization package supplies the reviewed bounds and rates; this documentation does not authorize dispatch. The required client configuration is the existing `OPENAI_API_KEY` and `WAHOJOBS_OPENAI_ENRICHMENT_MODEL`; credentials must remain outside artifacts and shell literals.

The pilot's `service_tier` argument (CLI `--service-tier`) accepts only `default`, which is also its default. It passes that explicit value to `configured_openai_client(enabled=True, service_tier='default')`. The client serializes the top-level Responses API field `service_tier: "default"`. Other client callers may use the optional `WAHOJOBS_OPENAI_ENRICHMENT_SERVICE_TIER=default` environment setting; an explicit argument takes precedence. Without either option, the shared client preserves its previous omitted-field behavior. Unsupported/empty values are rejected before dispatch. There is no arbitrary API-parameter passthrough or support for auto/flex/priority. A disabled factory still reads no credentials or tier configuration.

This selects Standard processing for the request; it neither verifies nor changes any API project's settings. The Standard pilot does not use a project-setting confirmation flag or fall back to omitted/auto processing.

Requested and returned service tiers are separate execution provenance in the attempt ledger and validation/failure artifacts. For an explicit Standard client, only the exact top-level response metadata string `default` passes, before structured output interpretation or publication. Generated text cannot supply this authority. Missing, malformed or different metadata retains the raw response and available usage, gives no usable support and halts later pilot cases with `unexecuted_service_tier`. A timeout/non-JSON response with no verifiable tier also halts. The first failure remains recorded, the physical slot and full reservation remain consumed, and its cost estimate is unknown when the actual tier is unresolved. This guard cannot undo already incurred charges. HTTP errors/refusals with verified Standard metadata remain failures with no fallback or retry; later distinct planned cases may proceed within their remaining limits. No automatic tier switching or repair is supported.

The external review receipt gives the exact approved Python 3.12.6 isolated offline invocation. For an authorized future real pilot, the supported module invocation (inside an approved runtime with this snapshot and the 21 locked dependencies on its isolated import path) is:

```text
-m scripts.professional_background_pilot --output <new-disposable-artifact-directory> --mode real --authorize-real-requests --service-tier default --request-limit 6 --token-limit 150000 --usd-limit <authorized-ceiling> --input-usd-per-million <verified-rate> --output-usd-per-million <verified-rate>
```

This is the implemented parser, not a request made by this task. The offline audit runner intentionally blocks external/model networking; a reviewed, explicitly authorized model-network execution boundary is an additional prerequisite for the real run. Do not bypass that runner's audit policy. No application activation is needed for the disposable pilot. It records dry-run selection, exact requests, unedited response bytes (including bounded invalid/refused responses), validation/failure/accounting, comparison components, scores/sections, subsequent lookup and hashed artifacts. Assess model-quality hypotheses separately from deterministic duration/proficiency/alternative safeguards. Conservative P02 output is allowed; no result is forced.

Each case executes once, then performs only a read-only inspection, including after failure. Successful conservative/no-support outputs can demonstrate reuse without dispatch. There is no second `execute()` to obtain a success. P01's missing-role skip is a selection outcome, never a quality observation. Later cases receive an explicit `unexecuted_budget` disposition when remaining request/token/cost reservations are insufficient. The ceiling is six physical requests, not six successful cases; unused reservations from failed/configuration-blocked attempts are not reassigned.

The output directory must be new. Each run contains `pilot-plan.json`, `pilot-ledger.json` and a separate directory per case; each reserved attempt has `attempt-NN-<request-id>/request.json`, `dispatch.json`, unedited `response.raw.json` when available, and `validated.json` or `failed.json`. All writes are exclusive, and attempt/destination collisions are checked before dispatch. Raw bounded responses are saved before semantic validation/publication; transport failures with no response retain failure evidence instead. Existing evidence is never overwritten. A later explicitly authorized retry requires a distinct run directory and budget. The ledger distinguishes planned cases, selection skips, read-only reuse, reserved and physical attempts, outcomes, known usage and retained reservations. No automatic resume/retry service exists.

The provider and attempt records are bounded and **process-local**. Restarting or using another worker/process loses them; no persistence or cross-worker synchronization is claimed. The pilot consumes each result in its same disposable process. A standalone CLI cannot prepare a separate live server's provider. A future live operator must use the retained runtime object's explicit method in that server process. Cross-process preparation would need an independently scoped persistence/lifecycle design; no migration is part of this change.

## Pilot model-identity halt

The preparer records `execution_failure: invalid_preparation_model_identity` only when the existing local model-identity validator fails. This structured code is independent of human/provider error messages and generated content. The pilot sets `halted_reason: pilot_response_model_identity_invalid` and marks remaining cases `unexecuted_model_identity`. A valid `default` service tier does not override this failure. The existing approved identity set is unchanged; valid conservative results remain reusable.

Bounded error-response metadata retains the unmodified model value, including on refusal, incomplete or invalid generated output, so those outcomes cannot conceal a missing/malformed/unapproved identity. Available usage and the raw response remain recorded. An unapproved identity has no approved-model cost estimate (`estimated_cost_usd: null` in the attempt ledger); full request/token/USD reservations remain consumed. There is no refund, alternate-model request or retry. Earlier successful artifacts/results are retained. If both tier and identity fail, the existing service-tier halt takes precedence.

| Outcome | Batch policy |
|---|---|
| Missing/malformed/unapproved returned model under the existing local policy | Halt; later cases `unexecuted_model_identity` |
| Unverified returned tier, including a transport failure with no tier | Existing halt; later cases `unexecuted_service_tier` |
| Refusal, incomplete, invalid semantic output or HTTP error with approved execution metadata | Finish the current case as failed; later planned cases may proceed within remaining bounds; never retry |
| Valid conservative/no-support output | Publish/reuse normally; no new request for reuse |
| Request/token/USD reservation exhausted | Existing `unexecuted_budget` disposition; no dispatch beyond the limit |
| Configuration/authorization error escaping the pilot operation | Operation aborts; no automatic relaunch |

This is a narrow model-identity halt, not a new general exception classification or retry framework. Other deterministic comparison, source/profile validity, transport and artifact policies remain unchanged.

The preparation v2 recipe changes only wire compatibility: `candidate_fact_ids`
no longer emits `uniqueItems`, which the actual Responses request rejected.
Local validation examines the original array and rejects repeated references
before publication; it never repairs or deduplicates output. Uniqueness applies
within each response, not across separate comparison components. Conservative
relations may still use empty evidence arrays; partial support still requires
at least one exact, supplied fact reference. The semantic version and substantive
instructions are unchanged; the recipe revision mechanically changes bound
request/plan IDs and corresponding singleton schema enums.

The [official Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs)
was inspected on 2026-09-12. This pilot uses strict closed objects with all fields
required, enums, integer spans and an array `maxItems` bound. Its one nested
object and six emitted variants were checked; there are no definitions or
composition branches. Rationale length bounds remain on the wire and locally;
the guide lists their exclusion for fine-tuned models, while this pilot uses the
base model. General JSON Schema validity is not API compatibility. These bounded
checks do not constitute an official validator or prove future remote acceptance.

The rejected real run remains one consumed physical attempt with unknown actual
usage/cost and retained USD 0.00518 / 6,384 reserved token units. A successor
freeze is not a budget reset or retry authorization. Executing P02 and the five
controls again would add six NEW attempts (seven cumulative), requiring separate
explicit authorization, a fresh non-overwriting destination and reconciliation
of that retained reservation. P01 remains a no-call selection outcome. No real
execution is authorized by this compatibility correction.

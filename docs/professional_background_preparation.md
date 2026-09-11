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

The script defaults to `--mode dry-run`. `--mode offline` uses labelled stub responses. A real run requires **all** of `--mode real --authorize-real-requests`, configured credentials/model, `--request-limit 6 --token-limit 150000`, a positive `--usd-limit`, and verified positive `--input-usd-per-million` / `--output-usd-per-million`. Values are intentionally not supplied here: current prices and a paid budget have not been authorized or verified. The required client configuration is the existing `OPENAI_API_KEY` and `WAHOJOBS_OPENAI_ENRICHMENT_MODEL`; credentials must remain outside artifacts and shell literals.

The external review receipt gives the exact approved Python 3.12.6 isolated offline invocation. For an authorized future real pilot, the supported module invocation (inside an approved runtime with this snapshot and the 21 locked dependencies on its isolated import path) is:

```text
-m scripts.professional_background_pilot --output <new-disposable-artifact-directory> --mode real --authorize-real-requests --request-limit 6 --token-limit 150000 --usd-limit <authorized-ceiling> --input-usd-per-million <verified-rate> --output-usd-per-million <verified-rate>
```

This is the implemented parser, not a request made by this task. The offline audit runner intentionally blocks external/model networking; a reviewed, explicitly authorized model-network execution boundary is an additional prerequisite for the real run. Do not bypass that runner's audit policy. No application activation is needed for the disposable pilot. It records dry-run selection, exact requests, unedited response bytes (including bounded invalid/refused responses), validation/failure/accounting, comparison components, scores/sections, subsequent lookup and hashed artifacts. Assess model-quality hypotheses separately from deterministic duration/proficiency/alternative safeguards. Conservative P02 output is allowed; no result is forced.

Each case executes once, then performs only a read-only inspection, including after failure. Successful conservative/no-support outputs can demonstrate reuse without dispatch. There is no second `execute()` to obtain a success. P01's missing-role skip is a selection outcome, never a quality observation. Later cases receive an explicit `unexecuted_budget` disposition when remaining request/token/cost reservations are insufficient. The ceiling is six physical requests, not six successful cases; unused reservations from failed/configuration-blocked attempts are not reassigned.

The output directory must be new. Each run contains `pilot-plan.json`, `pilot-ledger.json` and a separate directory per case; each reserved attempt has `attempt-NN-<request-id>/request.json`, `dispatch.json`, unedited `response.raw.json` when available, and `validated.json` or `failed.json`. All writes are exclusive, and attempt/destination collisions are checked before dispatch. Raw bounded responses are saved before semantic validation/publication; transport failures with no response retain failure evidence instead. Existing evidence is never overwritten. A later explicitly authorized retry requires a distinct run directory and budget. The ledger distinguishes planned cases, selection skips, read-only reuse, reserved and physical attempts, outcomes, known usage and retained reservations. No automatic resume/retry service exists.

The provider and attempt records are bounded and **process-local**. Restarting or using another worker/process loses them; no persistence or cross-worker synchronization is claimed. The pilot consumes each result in its same disposable process. A standalone CLI cannot prepare a separate live server's provider. A future live operator must use the retained runtime object's explicit method in that server process. Cross-process preparation would need an independently scoped persistence/lifecycle design; no migration is part of this change.

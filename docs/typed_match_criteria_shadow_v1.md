# Typed Match Criteria V1

`match_criteria_v1` is an internal, read-only projection of the optional
`canonical_profile_v2.preferences.preference_model`. It does not consume the
legacy preference mirror. When the authoritative model is absent, typed
preference enforcement is inactive and authenticated match presentation is
unchanged. The criterion diagnostics continue to run independently for the
existing eligibility bridge.

## Profile criteria

Each active criterion has a stable ID, a class (`eligibility`,
`strict_preference`, or `soft_preference`), a dimension, and either an
`any_of` accepted-value set or a structured compensation minimum. Empty lists
and lists containing the complete closed enum are unrestricted and therefore
do not produce active criteria.

The current preference contract contains no eligibility fields, so the
eligibility group is intentionally empty. It exists to keep future hard
eligibility separate and non-relaxable. All V1 accepted-choice dimensions are
soft preferences: employment relationship, workload, engagement term,
schedule facets, phone/voice modes, job interests, and accepted career levels.
A preferred compensation minimum is soft and a strict compensation minimum is
strict. Only failed soft criteria are marked potentially relaxable.

## Existing eligibility bridge

Opportunity-specific eligibility outcomes are translated from the existing
post-guardrail matcher record. They are not rebuilt from profile or opportunity
text. The profile-side `eligibility_criteria` collection remains empty; the
bridge adds four stable `eligibility` results to the shadow evaluation:

- `eligibility.required_languages` consumes the matcher's personalized
  language decision and its existing confirmed/unconfirmed title-language
  guardrails;
- `eligibility.location` consumes the stored-location eligibility status and
  existing location actionability guardrails;
- `eligibility.credentials_licenses` consumes explicit credential conflicts
  and already-produced credential fit evidence; an unconfirmed requirement
  remains unknown;
- `eligibility.professional_domain` consumes only the existing decisive
  finance/legal professional-domain hard gate.

Each result is `pass`, `fail`, `unknown`, or `not_applicable`, always has
`potentially_relaxable: false`, and carries only closed enums, booleans, and
bounded counts as context. It never copies location text, language names,
credential labels, job text, or profile evidence into the diagnostic.

The post-guardrail diagnostic sink receives a copy of every evaluated match
before deduplication and display limits. Sink failures are ignored. Opportunity
trust/freshness, affirmative-fit confidence, specialization caps, and ordinary
score penalties are not candidate eligibility and are not bridged. Structured
enrichment credentials/licenses are also not bridged yet because the current
matcher does not use them as authoritative gates.

## Opportunity projection

`opportunity_match_criteria_v1` represents each dimension as known values or
unknown with a stable reason. Structured enrichment is used only when the
field has high-confidence evidence or a current human override. A stale
override or an enrichment `unknown_fields` entry cannot become a known value.

The mixed enrichment `engagement_type` is split only where the meaning is
unambiguous:

- `full_time` and `part_time` establish workload only;
- `freelance` establishes `independent_contractor` only;
- `temporary` and `internship` establish engagement term only;
- `contract` remains unknown for relationship and term;
- `volunteer` is unsupported by the profile preference taxonomy.

`fixed` and `flexible` schedule types project to schedule flexibility.
Coordination and time windows remain unknown because enrichment has no typed
fields for them. Phone/voice acceptance also remains unknown: an
`audio_speech` role is not evidence that a job requires phone work. Job
interests and target career levels project only from exact shared taxonomy
values. The raw inventory fallback accepts only exact closed commitment and
source-category labels and performs no free-text classification.

## Compensation

Comparison is limited to disclosed structured pay with the same ISO currency
and the same `hour`, `month`, or `year` period. There is no FX conversion and
no period conversion. Project, task-like, asset, source-word, unsupported, and
undisclosed compensation is unknown/not comparable.

A strict minimum passes only when the opportunity's lower bound guarantees
the threshold. A proven upper bound below the threshold fails, and a
same-unit overlapping range fails as not guaranteed. For a preferred minimum,
a guaranteed lower bound passes, a proven upper bound below fails, and an
overlapping range remains unknown. This preserves the distinction between a
proven result and incomplete compensation evidence.

## Authenticated primary-result enforcement

The authenticated matches path completes the existing projection, scoring,
ranking and hard gates first. Only a profile with the authoritative preference
model activates enforcement. The integration obtains the full ranked pool that
already satisfies current presentation eligibility, then evaluates it with the
same typed projector, criterion evaluator, compensation comparator, and
eligibility bridge. Preference failures are removed without rescoring. The
existing display limit is applied afterward, so a lower-ranked surviving item
can fill a vacancy while the relative order of every survivor remains unchanged.

Strict preference `fail`, `unknown`, missing, and not-comparable results are
excluded. A known soft-preference `fail` is excluded, while soft `unknown` and
`not_applicable` results remain visible. Existing eligibility failures remain
non-relaxable and excluded by their authoritative matcher gate. Bounded
criterion outcomes and admission decisions stay in internal request/run state
for later counterfactual work and are not rendered to the candidate.

The all-inventory shadow diagnostic remains available to its optional internal
sink. Diagnostic sink failure cannot affect presentation. Enrichment lookup or
projection failure is treated as missing evidence: active strict criteria fail
closed, while active soft criteria fail open. Existing scoring, ranking,
thresholds, trust/freshness rules, matcher inputs, and public catalog behavior
remain unchanged.

## Single-criterion relaxation counterfactuals

The authenticated boundary also evaluates excluded members of that same full
ranked eligible pool for internal `single_criterion_relaxation_counterfactuals_v1`
diagnostics. This does not alter the primary list and is not rendered. A
scenario exists only when all bridged eligibility results are pass or not
applicable, every strict preference passes, exactly one soft preference is a
known failure, and every other soft result is non-blocking under the primary
policy. Missing or unknown eligibility authority produces no scenario.

For accepted-choice dimensions, the engine proposes adding an exact known
opportunity value to the current accepted set. For preferred compensation, it
can propose lowering the minimum only to a positive guaranteed lower bound
from disclosed pay in the same currency and period. It does not use `up_to`,
undisclosed, project/asset/source-word, incompatible-currency, or
incompatible-period pay, and strict compensation is never relaxable.

Every proposal is applied to a newly constructed `MatchCriteriaV1`, then the
ordinary criterion evaluator and primary admission policy are run again. The
scenario is retained only if the changed criterion passes and the whole
opportunity becomes admissible with no other criterion changed. Equivalent
changes aggregate stable internal opportunity references in the matcher's
existing rank order. Eligibility, hard exclusions, trust/freshness, scores,
thresholds, and ranking are not counterfactual inputs and cannot be suggested
as relaxations.

## Authenticated relaxation presentation

`/find-matches` renders only validated scenarios emitted by this engine. It
orders scenario summaries by unlock count, then the best existing opportunity
rank, without rescoring. Opening a native disclosure previews exactly the
scenario's proven opportunities in their existing order and keeps them
visually separate from primary matches. The page never renders criterion IDs
or internal reason codes.

The preview is read-only. Its explicit `Update my preferences` action links to
the existing profile flow; opening a disclosure does not submit a form or
change stored preferences. Pages with no proven scenario omit the section,
and additional scenarios beyond the first three stay behind an accessible
progressive disclosure.

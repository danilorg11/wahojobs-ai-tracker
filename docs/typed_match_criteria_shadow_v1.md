# Typed Match Criteria V1 (shadow)

`match_criteria_v1` is an internal, read-only projection of the optional
`canonical_profile_v2.preferences.preference_model`. It does not consume the
legacy preference mirror. When the authoritative model is absent, there are no
typed criteria and no shadow diagnostics.

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

## Shadow integration

The authenticated matches path completes the existing projection, scoring,
ranking, and visible context first. It then evaluates typed criteria and sends
bounded criterion outcomes to an optional internal sink. Projection, lookup,
or sink failure is fail-open relative to the existing matcher. Shadow outcomes
are not placed in the browser context and cannot filter, reorder, score,
suppress, or add visible matches. Existing profile writers, thresholds, and
matcher inputs remain unchanged.

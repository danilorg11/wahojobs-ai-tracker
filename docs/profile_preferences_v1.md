# Profile Preferences V1

`profile_preferences_v1` is an optional authoritative subdocument at
`canonical_profile_v2.preferences.preference_model`. It separates concepts
that legacy `employment_types` mixes together. New AI-assisted onboarding is
the first production writer. It validates the model on the server and writes it
only after explicit review confirmation. Manual profile writers do not emit
it, no existing profile is backfilled, and typed matching remains shadow-only.

## Exact contract

Every object is closed and every field below is required:

```json
{
  "schema_version": "profile_preferences_v1",
  "employment_relationships": [],
  "workloads": [],
  "engagement_terms": [],
  "schedule": {
    "flexibility_modes": [],
    "coordination_modes": [],
    "time_windows": []
  },
  "accepted_phone_voice_modes": [],
  "job_interests": [],
  "accepted_career_levels": [],
  "compensation": {
    "minimum_kind": "none",
    "amount": null,
    "currency": null,
    "period": null
  }
}
```

All arrays are sets of acceptable choices: they are unique and canonically
sorted, and an empty array means unrestricted. The closed values are:

- employment relationships: `employee`, `independent_contractor`;
- workloads: `full_time`, `part_time`;
- engagement terms: `permanent`, `fixed_term`, `temporary`, `seasonal`,
  `internship`;
- schedule flexibility: `fixed`, `flexible`;
- schedule coordination: `asynchronous`, `synchronous`;
- schedule windows: `business_hours`, `weekdays`, `evenings`, `weekends`;
- phone/voice acceptance: `phone`, `non_phone`;
- job interests: the shared closed `OCCUPATIONAL_FAMILIES` taxonomy;
- accepted career levels: `internship`, `entry`, `mid`, `senior`, `lead`,
  `principal`, `manager`, shared with opportunity enrichment.

`accepted_career_levels` describes target opportunities. It is not the
candidate's `experience.seniority` fact. Every accepted-choice dimension is a
soft preference in typed matching. Compensation alone carries an explicit
preferred/strict strength in V1.

Compensation uses `minimum_kind` values `none`, `preferred`, or `strict`.
`none` requires the other three fields to be null. `preferred` and `strict`
require a positive canonical decimal string with no exponent and at most two
decimal places (and at most 18 integral digits), a closed ISO 4217 currency
code, and period `hour`, `month`, or `year`. Project/task periods are
deliberately absent because V1 cannot compare them honestly.

Hybrid, onsite, and volunteer choices are not introduced. Existing `remote`
preference semantics remain in the surrounding legacy preference fields until
a separately approved workplace model is justified by product and inventory
requirements.

## Legacy adapters and compatibility

The pure legacy-to-new adapter maps only deterministic meanings:

- `full-time`/`part-time` become workloads;
- `freelance` becomes `independent_contractor`;
- `temporary`, `seasonal`, and `internship` become engagement terms;
- `entry-level` becomes accepted target level `entry`;
- schedule, synchronization, flexibility, and exact taxonomy interest codes
  map to their distinct dimensions;
- `non-phone required` becomes `non_phone`, while `phone acceptable` becomes
  both accepted modes.

Legacy `contract` is not guessed as either a relationship or a fixed term; the
adapter returns `legacy_contract_requires_confirmation`. Preferred phone modes,
untyped free-text job interests, and free-text compensation also return bounded
confirmation records without copying their values into diagnostics.

The pure new-to-legacy adapter produces a valid, intentionally lossy V1 mirror
for compatibility. AI-assisted onboarding derives the complete legacy mirror
from the validated model on the server; the browser cannot submit a second
legacy representation. The durable V2 writer rejects any mismatch between the
model and that mirror. Canonical V2's active matcher projection still removes
`preference_model`, so current matching sees only those established legacy
fields. Existing profiles retain their legacy fields unchanged.

The AI review renders each accepted-choice dimension as an independent
multi-select generated from this contract. Employee/freelance and
full-time/part-time can therefore coexist. Compensation exposes `none`,
`preferred`, and `strict` plus amount, currency, and hour/month/year period;
project/task periods and generic strictness controls for other dimensions are
not present. Legacy `contract` is never preselected or guessed: a user must
explicitly choose the typed relationship and/or fixed-term controls.

The next read-only layer projects this model and conservative opportunity
evidence into criterion-by-criterion internal diagnostics. Its contracts and
zero-visible-behavior boundary are documented in
[`typed_match_criteria_shadow_v1.md`](typed_match_criteria_shadow_v1.md).

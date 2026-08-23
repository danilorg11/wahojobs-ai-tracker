# Profile Preferences V1

`profile_preferences_v1` is an optional authoritative subdocument at
`canonical_profile_v2.preferences.preference_model`. It separates concepts
that legacy `employment_types` mixes together. This foundation is read-only in
the current product: no production writer emits it, no existing profile is
backfilled, and the current matcher does not consume it.

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
candidate's `experience.seniority` fact. Job interests are intrinsically soft;
V1 does not add a generic hard/preferred switch to other dimensions.

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
for compatibility. It is not wired into current writers or matching. Canonical
V2's active V1 review and matcher projections instead remove
`preference_model` and retain the existing legacy shadow fields unchanged. As a
result, adding the optional subdocument cannot change current match results.

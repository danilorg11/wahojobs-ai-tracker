# Profile Preferences V2

`profile_preferences_v2` is the authoritative preference subdocument written
by new AI-assisted onboarding inside
`canonical_profile_v2.preferences.preference_model`. Canonical V2 continues to
accept existing `profile_preferences_v1` documents without rewriting them.

Every object is closed. Every accepted-choice list is unique, canonically
sorted, and means unrestricted when empty:

```json
{
  "schema_version": "profile_preferences_v2",
  "employment_relationships": [],
  "workloads": [],
  "engagement_terms": [],
  "schedule": {
    "flexibility_modes": [],
    "coordination_modes": [],
    "working_days": [],
    "time_of_day": []
  },
  "accepted_phone_voice_modes": [],
  "job_interests": [],
  "accepted_career_levels": [],
  "compensation_expectations": []
}
```

The unchanged V1 choice sets continue to govern relationship, workload,
engagement term, flexibility, coordination, phone/voice, job interests, and
career levels. Working days are `weekdays` or `weekends`; time of day is
`business_hours` or `evenings`. They are independent dimensions.

Each compensation expectation has exactly `minimum_kind`, `amount`,
`currency`, and `period`. Minimum kind is `preferred` or `strict`; amount is a
positive canonical decimal string; currency uses the existing closed ISO 4217
set; and period is `hour`, `month`, or `year`. At most 12 expectations are
accepted and each currency/period pair may appear once. An empty list means no
minimum. No FX, period, project, or task conversion is introduced.

The V1-to-V2 adapter splits old schedule windows deterministically and turns a
set V1 compensation minimum into one expectation. Existing stored V1 models
remain V1. The legacy compatibility mirror is always derived on the server;
the browser cannot submit it independently.

Slice 6B projects this contract natively into the existing typed-criteria
engine. Each schedule axis has its own criterion, and each compensation
expectation has a stable currency/period-specific criterion. An opportunity is
compared only with the expectation whose currency and period both match; other
expectations are not applicable to that opportunity. No FX or period conversion
is performed.

Missing opportunity evidence remains `unknown`: it keeps a match for soft
preferences, excludes for an applicable strict compensation requirement, and
cannot prove a relaxation scenario. Current structured inventory reliably
supports schedule flexibility but not working days, time of day, or coordination,
so those dimensions remain unknown unless a future authoritative structured
source supplies them.

# Profile Preferences V2

`profile_preferences_v2` is the authoritative preference subdocument written
by AI-assisted onboarding and sealed manual confirmation inside
`canonical_profile_v2.preferences.preference_model`. Canonical V2 continues to
accept existing `profile_preferences_v1` documents without rewriting them.

## Private beta manual and legacy workload contract

The manual writer derives only supported deterministic values from the reviewed
legacy controls. It preserves every legacy value and free-text interest rather
than replacing them with the intentionally lossy typed mirror. Derived fields
bind to the confirmed original source or the complete reviewed correction source.
The existing AI-assisted writer retains its strict full-mirror equality check.
Typed corrections update changed mirrors only, preserving independent free text
and unchanged provenance. Draft saves do not change the confirmed profile.
An older profile acquires a model through correction only when the candidate
actually edits its preference values, reviews that proposal and confirms it.
An unchanged submission or an unrelated correction preserves the existing legacy
preference root and its provenance; it does not introduce new preference filters.

Older reviewed canonical V2 profiles without a typed model can supply exact
`part-time`/`full-time` values from confirmed legacy workload fields to the same
consumer, without writing a model or revision. This bounded read compatibility
requires explicit user-confirmation/correction provenance and is identified in
internal evaluation evidence as `confirmed_legacy_workload`. Other historical
free text does not silently become a new filter.

A soft workload mismatch remains a `fail` comparison but keeps an otherwise
eligible recommendation, with concise advice to check the schedule. It neither
changes scores nor produces a fictitious preference-relaxation unlock. A missing
workload is `unknown`; a source range such as 10–40 hours is not converted into
full-time/part-time authority. Other preference dimensions retain their existing
admission rules. Firm workload limits use the exact visible forms in confirmed
Firm constraints: `part-time only`/`only part-time work` or
`full-time only`/`only full-time work`. These create a strict workload criterion;
a conflict or missing comparable evidence excludes. Opposing firm limits are
rejected during review. No schema, numeric availability inference, or data
migration is introduced.

## Exact contract

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

For the AI-training-first V1 onboarding, new AI-assisted drafts keep
`job_interests` empty and do not present that taxonomy as a question. Empty
remains unrestricted. Candidate experience, skills, languages, education,
industries, and specialties supply relevance evidence for AI training and
evaluation opportunities; candidates do not have to enumerate every domain or
activity to avoid missing work. Existing V1/V2 profiles with confirmed
`job_interests` are neither migrated nor reinterpreted, and their existing
typed enforcement and relaxation behavior remains unchanged except for the
explicitly approved soft-workload rule above.

The AI intake editor presents each non-compensation soft dimension with an
explicit **No preference** or **I have preferences** state. No preference is
the default for a new draft and persists the existing empty/unrestricted list;
choosing preferences requires at least one allowlisted value. Returning to no
preference clears that dimension. This is presentation and form authority only:
the persisted V2 contract and matcher interpretation of an empty list are
unchanged. A single accessible Step 3 disclosure explains that preferences are
used when an opportunity provides comparable evidence, while missing evidence
is not treated as a conflict; background facts continue to drive AI-training
relevance without requiring candidate-maintained job taxonomies.

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

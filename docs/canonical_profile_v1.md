# Canonical Profile V1

Wahojobs uses `canonical_profile_v1` as the reviewed source of truth for local
Find Matches runs. Free text is a prefill mechanism: once the user confirms the
review form, matching uses a deterministic projection of the reviewed fields and
does not reintroduce facts from the original paragraph.

## Profile shape

The schema covers:

- identity and source inputs;
- country, region, city, work authorization, and geographic restrictions;
- languages, locale, proficiency, and whether proficiency was explicit;
- education level, degree, field, institution, and completion status;
- job titles, occupational families, domains, seniority, years, industries,
  specialties, and management or individual-contributor background;
- normalized and categorized skills;
- certifications, licenses, jurisdictions, and security clearances, including
  explicit credential absence;
- employment, schedule, phone, flexibility, availability, and task preferences;
- degree, experience, credential, domain, and accessibility constraints;
- per-field provenance and retained original text.

Material populated fields identify one of these provenance sources:

- `explicit_user_entry`
- `parsed_free_text`
- `resume_extraction`
- `external_import`
- `user_confirmation`
- `user_correction`

User confirmations and corrections are explicit and authoritative. The recursive
validator rejects unsupported fields, invalid nested types and enums, duplicate
language claims, malformed provenance, and unresolved credential, degree, domain,
experience, or location contradictions. Derived matcher signals and the legacy
matcher projection are rebuilt from the reviewed profile.

### Canonical boundaries

`location.country` and `location.residence` store canonical English ISO country
names (for example, `Brazil`, `United States`, and `Cote d'Ivoire`). Input and
review adapters accept unambiguous country names and ISO alpha-2 codes, then
normalize them before validation. The canonical validator rejects codes,
arbitrary text, empty values, and conflicting country/residence values.

Preference fields use documented canonical values. Phone preference is one of
`unknown`, `no preference`, `phone acceptable`, `phone preferred`,
`non-phone preferred`, or `non-phone required`. Synchronous preference is one
of `unknown`, `no preference`, `synchronous`, `asynchronous`, or `flexible`.
Employment, schedule, and availability values are likewise closed enums defined
by the schema module. Canonical validation does not coerce casing, numbers,
booleans, or unsupported labels.

`provenance.field_sources` is required for every populated material leaf. Paths
must resolve through the canonical document, including the exact index for array
items. Container paths, missing values, stale array indexes, schema/internal
fields, malformed paths, and duplicate paths are invalid. Review operations
rebuild this map server-side after accepted edits; clients do not author paths.

## Current persistence boundary

For this local milestone, a reviewed canonical profile is owned by its in-memory
`MatchRun`. Editing a result run updates the same run and recomputes matches from
the revised canonical profile.

The existing SQLite `user_profiles` table is intentionally unchanged. Its flat
columns cannot preserve the versioned document and field-level provenance safely.
A future account milestone should add a dedicated, explicit profile migration and
repository layer, then migrate temporary MatchRun ownership to an authenticated
profile owner. Normal runtime must not install that migration automatically.

Resume parsing, LinkedIn import, authentication, and public catalog browsing are
outside this milestone. They should produce proposed canonical fields with their
own provenance and still pass through user review before becoming authoritative.

## Coverage evaluation

Run the read-only persona evaluation with:

```bash
python scripts/profile_matching_coverage.py --stdout-only
```

It evaluates the reviewed persona suite against an immutable read-only SQLite
connection. Live coverage output is a generated local report, not a committed
fixture: the command writes nothing unless an explicit path is supplied, for
example `--out exports/product_readiness_profile_coverage.json`. The deterministic
persona definitions remain in `tests/fixtures/product_readiness_personas_v1.json`.
The report includes a sequential domain, language, location, credential,
freshness, and admission funnel. Each persona is explicitly classified as
`core`, `adjacent`, or `outside_initial_launch_scope`. Core and adjacent
personas must admit their deterministic strong-family contract opportunity,
rank it above the supplied fallback, reject prohibited families, and produce an
evidence-backed explanation. Outside-scope personas enforce the no-leak contract
without pretending that a missing live role is a matcher failure. The report
does not write profile or opportunity data.

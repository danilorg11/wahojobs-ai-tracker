# Canonical Profile V2

`canonical_profile_v2` is the proposed durable profile document for the next
persistent-profile migration. It is implemented as a pure domain contract and
compatibility layer only. No database migration, persistence service, browser
integration, or runtime import is active yet.

## Why V2 exists

Canonical Profile V1 remains the normalizer input and matcher compatibility
format. It cannot be stored under Migration 004 unchanged because V1 uses
dynamic object keys for field-source paths, professional-domain years, and an
optional language-proficiency map. Migration 004 deliberately permits only
controlled lowercase ASCII structural keys and rejects raw/evidence fields.

V2 keeps that durable privacy boundary. Dynamic paths, languages, locales,
domains, and skills are values inside bounded records, never object keys.

## Root contract

A V2 document has exactly these root fields:

- `schema_version`, exactly `canonical_profile_v2`;
- `identity`;
- `languages`;
- `location`;
- `education`;
- `credentials`;
- `experience`;
- `skills`;
- `preferences`;
- `constraints`;
- `derived_matcher_signals`;
- `provenance`.

`identity` contains only a persistent `prf_` profile ID and a display name. A
legacy profile/archetype ID is not ownership identity. Matcher projection must
receive a separate ephemeral matcher ID. Ephemeral matcher IDs are bounded
ASCII runtime identifiers. They cannot equal, contain, or use the resource-ID
shape of a durable profile, revision, source, account, principal, alias,
binding, session, invitation, or account-lifecycle record. A complete durable
resource-ID shape is rejected anywhere inside an ephemeral matcher ID without
requiring token boundaries. Projection recursively scans the complete V1 result
and rejects both the current persistent profile ID and any complete durable
resource-ID shape before returning it.

V2 does not contain `matcher_compatible_profile`, original About You text,
source copies, evidence, evidence snippets, fixture/case/input IDs, account or
principal identity, provider identity, email, session data, tokens, or
authorization data. Exact confirmed input belongs in immutable revision source
records once a future service exists.

## Dynamic structures

`experience.years_by_domain` is an ordered array of `{domain, years}` records.
Domains are trimmed NFC display values, compared with collapsed-whitespace
Unicode case-folding. Values are unique, sorted, finite, between zero and 80,
and have at most two decimal places.

Language proficiency remains authoritative in `languages[]`. Each language
record contains `language`, `proficiency`, `locale`, and confidence, plus
optional explicitness and controlled provenance. Records are deterministic and
unique by normalized language and locale. While V1 is the compatibility format,
V2 also rejects two locale variants of the same base language because V1 cannot
represent them without silently merging a claim.

Skill entries and derived matcher signals are bounded canonical arrays with
fixed keys. A signal contains exactly `reason`, `keywords`, `points`, and
`confidence`. Its reason is a lowercase ASCII identifier of at most 64
characters. It has at most 32 unique, canonically ordered keywords of at most
128 characters each, and integer points from 1 through 100. Signal reasons are
unique, so duplicate or conflicting records for one reason are rejected.
Confidence retains the existing V1 enum (`unknown`, `low`, `medium`, or `high`)
so conversion does not invent a new confidence model. Evidence, snippets, raw
content, source identity, and arbitrary signal metadata are not accepted.

## Provenance paths

`provenance.field_sources` is an ordered array. Each record contains:

```json
{
  "explicit": true,
  "field_path": "languages[0].language",
  "path_version": "canonical_profile_v2_path_v1",
  "source_kind": "user_confirmation",
  "source_ordinals": [1]
}
```

Source ordinals are sorted, unique integers from 1 through 16, with at most 16
ordinals per field. Every populated
material V2 leaf has exactly one field-source record. Identity, provenance,
derived signals, and removed raw/evidence content cannot be provenance targets.

Paths reference the exact V2 document. They begin under `languages`,
`location`, `education`, `credentials`, `experience`, `skills`, `preferences`,
or `constraints`. Object segments use lowercase ASCII names, indexes are
zero-based from 0 through 255, total length is at most 256, and traversal has at
most 12 steps. Wildcards, JSONPath/SQL syntax, recursive or parent traversal,
quotes, whitespace, controls, malformed indexes, missing paths, and container
endpoints are rejected. Reconciliation must later verify path existence.

## Normalization and serialization

Canonicalization precedes validation and serialization. Structured Canonical Profile V2 values normalize Unicode to NFC; trim leading and trailing permitted Unicode space separators; collapse repeated permitted Unicode space separators to one ASCII space; and reject tab U+0009, LF U+000A, CR U+000D, all other C0 controls, DEL U+007F, and C1 controls before normalization. Meaningful display case is preserved, while case-folding is used only for ordering and collision detection. NFC/NFD and approved whitespace variants therefore serialize identically; intentionally different display case remains different. The contract does not transliterate or remove accents. The separate persistent-profile source-text contract may allow tab, LF, and CR; source text remains outside structured V2 values and is unaffected by this policy.

Finite numbers use their normalized durable value. Booleans are never accepted
as numbers. Positive zero, negative zero, and integral zero serialize as the
same integer. Integral floats normalize to integers where the field semantics
allow them. Domain-year decimals retain at most two places; NaN and infinities
are rejected. The bounded numeric fields prevent platform-dependent exponent
output.

Semantically unordered arrays are sorted by their documented canonical keys.
When language, skill, domain-year, or other material arrays move, canonicalization
also remaps their field-source paths. Object keys are sorted during JSON
serialization. Serialization always uses the canonical defensive copy, never
the caller's original structure.

One immutable limits definition is authoritative for validation, conversion,
serialization, tests, and this document. The implementation key is included so
tests can compare the complete contract without relying on prose aliases.

| Implementation key | Value |
| --- | ---: |
| `document_bytes` | 131072 |
| `document_nodes` | 4096 |
| `document_depth` | 12 |
| `object_children` | 256 |
| `list_children` | 256 |
| `structural_key_length` | 64 |
| `scalar_string_length` | 4096 |
| `dynamic_label_length` | 128 |
| `display_name_length` | 160 |
| `languages` | 32 |
| `domain_year_records` | 64 |
| `skill_records` | 96 |
| `derived_signals` | 64 |
| `signal_keywords` | 32 |
| `signal_keyword_length` | 128 |
| `signal_reason_length` | 64 |
| `signal_points_absolute` | 100 |
| `field_source_records` | 256 |
| `field_path_length` | 256 |
| `field_path_steps` | 12 |
| `source_ordinal_value` | 16 |
| `source_ordinals_per_field` | 16 |
| `string_list_items` | 128 |
| `decimal_places` | 2 |
| `matcher_profile_id_length` | 128 |

Every collection-specific limit is subordinate to the global structural limit.
Canonical serialization uses UTF-8, sorted object keys, compact separators,
canonical array order, no non-finite numbers, and no platform newlines.

Raw JSON parsing is duplicate-aware. Duplicate keys at any depth, NaN,
Infinity, malformed JSON, JSON5 syntax, malformed Unicode, and non-object roots
are rejected. An already-parsed Python dictionary cannot reveal duplicate keys
that its parser already discarded.

Every public V2 boundary performs an iterative structural preflight before
copying, normalization, recursive privacy scanning, or serialization. Excessive
depth, node count, width, scalar size, and cyclic Python containers produce
bounded redacted domain errors. Parser recursion and malformed internal nesting
cannot escape as raw runtime errors. Unexpected validation failures retain
private exception chaining for diagnostics but expose only a sanitized reason
code.

## Compatibility conversion

V1-to-V2 conversion validates V1, deep-copies it, receives the new persistent
profile ID explicitly, converts dynamic maps to records, resolves provenance
through a pure source-ordinal resolver, and removes raw/evidence and legacy
matcher data. Unknown fields, ambiguous source mapping, and normalized
collisions fail with bounded redacted reason codes.

V2-to-V1 projection validates V2 and receives an ephemeral matcher ID. It
reconstructs only the valid runtime V1 document and a derived matcher block.
The matcher ID is validated against the durable identity before projection, and
the complete projection is checked again for persistent identity leakage.
Language proficiency is derived from `languages[]`. `years_by_domain` is empty
in the V1 projection because current matching does not consume it and restoring
the dynamic map would reintroduce V1 path and Unicode limitations. Durable V2
retains the complete domain-year records.

Round trips are semantically, not byte, equivalent. Schema version, identity,
evidence removal, source-input removal, provenance representation, map/array
representation, matcher-block derivation, and deterministic ordering are
intentional differences. Matching labels and product sections must remain
unchanged.

## Future Migration 005 boundary

Migration 005 is not part of this milestone. It must be separately reviewed and
installed before V2 or `confirmed_lifecycle_action` can be persisted.

No database repository, reconciliation service, CLI, browser integration,
MatchRun integration, authentication path, authorization path, or normal
runtime import exists for V2 in this milestone.

The future lifecycle source is controlled JSON with source type
`confirmed_lifecycle_action`, format `application/json`, and schema
`confirmed_lifecycle_action_v1`. It has exactly `action` and `schema_version`.
Allowed actions are `archive`, `reactivate`, and `deletion_request`. It contains
no free text, profile input, identity, or arbitrary metadata, and its action must
agree with the future revision kind.

The canonical archive payload is:

```json
{"action":"archive","schema_version":"confirmed_lifecycle_action_v1"}
```

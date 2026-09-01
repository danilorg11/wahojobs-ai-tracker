# Opportunity Enrichment vNext

Opportunity Enrichment vNext is a versioned hybrid derivation over accepted
opportunity evidence. It produces matching-grade canonical fields only when the
support is complete across the canonical's live variants, while retaining
variant-only facts for later variant-aware matching and explanations.

## Contracts and identity

- Enrichment document: `opportunity_enrichment_v5`
- Semantic input: `opportunity_semantic_input_v3`
- Extractor: `hybrid_evidence_vnext_v4`
- Derivation recipe: `opportunity_enrichment_derivation_v8`
- Structured prompt: `opportunity_semantic_vnext_v4`
- Semantic acceptance guards: `opportunity_llm_acceptance_guards_v9`

The semantic-input hash contains canonical data, every selected variant, and
accepted rich source material. An identical healthy recapture may advance the
append-only accepted-capture pointer without changing the semantic identity;
the stable accepted material hashes remain in the identity. The derivation
fingerprint separately captures schema, taxonomy, extractor, prompt/model, and
acceptance-guard versions.

An enrichment or LLM attempt is therefore reusable only for the same semantic
input and recipe identity. Tracking does not discover migrations by scanning a
company. It enriches only canonicals touched by a source promotion,
reactivation, removal, or resulting canonical move. Recipe-only migrations are
explicit operations.

## Hybrid authority boundary

Deterministic extraction handles facts with an explicit, safely normalizable
surface form:

- accepted listing location and language variants;
- explicit compensation amounts, periods, and disclosure;
- explicit weekly hours and fixed/flexible schedule statements;
- labeled engagement and workplace modes;
- hard minimum or explicitly preferred education levels;
- numeric minimum years of required experience;
- existing title/category taxonomy classifications.

Ambiguous dollar signs remain currency-unknown. Descriptive degree or
experience mentions are not converted into requirements.

Structured LLM extraction handles facts that require language understanding:

- responsibilities and candidate profile;
- required versus preferred skills;
- education alternatives and nuanced education modality;
- credentials and licenses, split into required and preferred;
- qualitative and numeric required/preferred experience;
- explicitly required current professional or participation status, including
  required present access to an asset or resource;
- professional domains, work activities, and specializations;
- location, language, schedule, hours, duration, and compensation facts that
  are explicit but outside the deterministic grammar;
- concise grounded summaries and material caveats.

The model returns facts plus supplied evidence aliases. It cannot choose scope:
the runtime derives variant scope from the cited blocks, validates the closed
schema and evidence references, and never accepts an eligibility judgment.
Under OE Semantic Authority Boundary v1, model-derived facts are
`semantic_non_exclusionary`; candidate eligibility may compare only separately
authorized deterministic/objective facts. Required semantic modality does not
grant hard-gate authority.

Sensitive facts also pass field-specific authority guards. Page-language and
office metadata cannot establish candidate language or location eligibility;
a bare currency symbol cannot establish an ISO currency; taxonomy activities
must cite evidence of the activity itself; and experience and modality are
normalized into their required or preferred fields. Every cited alias for
these facts must support the accepted value.

Same-language `single` and `all_required` evidence is reconciled by semantic
meaning rather than extractor origin. A one-language requirement normalizes to
`single`; a multi-language conjunction is `all_required` only when every named
language has direct all-required support. Other mode mixtures remain
`ambiguous`, so they cannot invalidate or silently strengthen the packet.
Absent and specific locales for the same language collapse to the one supported
specific locale; distinct locales remain separate. Dialect expertise and
preferred native fluency do not become a required language gate without
separate required-capability evidence.
Mixed qualifications such as background-or-interest stay intact in the
capability representation rather than making experience mandatory. Explicit
current-status and asset-access requirements have their own field instead of
being represented as a skill or past experience. Every accepted experience
proposition must itself state history, practice, tenure, or experience.

Professional-domain and activity guards retain only cited body segments that
express the domain expertise or underlying action. Generic QA/fact-checking is
not software testing without a software artifact, and a tool or format does not
establish a professional domain. Direct AI evaluation, operations, annotation,
and writing/resource-generation actions remain admissible.

The current matching-v1 fact projection remains frozen. The additive
preferred/qualitative requirement paths are measured by the readiness harness
but do not silently enter matching; consuming them requires a future explicit
matching-contract version. The current removal-capable typed projection accepts
only human overrides or uniformly high-confidence `deterministic_parse` /
`source_explicit` evidence; model-derived facts remain unavailable for
admission exclusion. No semantic packet enters ranking, deterministic
eligibility, lifecycle, or candidate UI behavior in this milestone.

## Evidence and knowledge state

Each `variant_facts` entry contains:

- a supported field path and typed value;
- `known_value` or `known_empty`;
- one or more stable `variant_refs`;
- evidence-block, source, accepted-authority, basis, confidence, and exact
  evidence-text references.

Accepted body facts carry the accepted capture and source-content hash.
Accepted listing facts carry the accepted capture and semantic-source hash.
Silence produces no fact and remains in `unknown_fields`. A list is
known-empty only when explicit cited evidence says the requirement is absent.

## Variant reduction

Facts are first stored at variant scope. A field is copied to the canonical
attributes only when every selected live variant is known for that field and
all variants have the same scalar or atomic-list value set. Partial coverage or
conflict stays in `variant_facts` and the canonical field remains unknown. This
prevents one location's pay, language, schedule, or eligibility rule from being
promoted to the whole canonical.

Canonical projection is recomputed when semantic facts are added. A prior
deterministic projection does not automatically win a disagreement: conflicting
facts remain variant-scoped and the canonical field returns to unknown unless
the evidence resolves to one conflict-free value.

The readiness harness reports mechanically complete packets separately from
quality-approved packets. Mechanical population alone is not scale readiness;
the quality-ready count requires an explicit semantic review approval.

## Canary result

`scripts/opportunity_enrichment_canary.py` is immutable/query-only and performs
no enrichment persistence or LLM calls. On the stable first 32 active Meridial
canonicals with accepted bodies (109 accepted body variants), deterministic
vNext produced the following; all 32 packets satisfy the bounded LLM source
threshold:

| Body-grounded canonical field | vNext coverage |
| --- | ---: |
| Compensation disclosed / period | 32 / 32 |
| Compensation amount min/max | 31 / 32 |
| Workplace mode | 25 / 32 |
| Location scope | 23 / 32 |
| Engagement type | 22 / 32 |
| Preferred education level | 11 / 32 |
| Schedule type | 9 / 32 |
| Weekly hours min/max | 4 / 32 |
| Required years of experience minimum | 3 / 32 |
| Responsibilities / candidate profile / skills | 0 / 32 |

The one compensation amount not promoted is an intentional cross-variant
conflict; all 32 have variant-supported amounts. Minimum semantic-packet
readiness remains 0/32 until the semantic pass is piloted. Structured
requirement signal is 24/32 from the deterministic and accepted listing facts.

The frozen v4/v2/v3 dry-run evidence is retained as
`exports/opportunity_enrichment_v4_historical_canary.json`; it is historical,
not a canary for the current v5/v4/v8 package.
The preregistration fixture retains the artifact's original filename and hash
as immutable historical metadata; that value is not a live path reference.

## Completed semantic boundary and shadow seam

OE Semantic Authority Boundary v1 and OE Semantic Shadow Packet Integration v1
are complete. `oe_semantic_matching_packet_v1` is available only through the
explicit read-only shadow runner and remains disconnected from normal matching,
deterministic eligibility, lifecycle, persistence, and candidate UI paths.

The next future phase is an explicit semantic matching/reranking integration.
That phase may use semantic packets for shortlist or reranking signals, but must
preserve their `semantic_non_exclusionary` authority and keep deterministic hard
eligibility separate.

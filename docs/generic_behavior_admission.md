# Source-clause materiality and admission

The exception is narrow: an otherwise supported task match is not demoted only
because an **unassessed** qualification clause has a current source-linked
`generic_behavior_only` classification. The clause remains unassessed, keeps its
required/preferred/unspecified modality, and stays in the existing source
disclosure. It contributes no score, task support or candidate competence.

## Existing enrichment path

`opportunity_enrichment.llm_source_packet` uses the existing `_source_text`,
`_blocks` and `_lines` boundaries to supply whole accepted qualification clauses
to the existing structured enrichment client. This adds no behavioral keyword
parser. A bounded catalog (64 clauses / 8,000 quote characters) has server-derived
aliases, exact quotes, section/line references, variant/source identities,
accepted-capture references and material hashes. Oversized clauses are omitted,
never truncated into apparently generic fragments.

Prompt `opportunity_semantic_vnext_v5` asks for source-only classifications:
`generic_behavior_only`, `specific_or_mixed`, or `ambiguous`. The model cannot
choose a partial span, a candidate, or a replacement quote. Any concrete skill,
proficiency, experience, credential, equipment, eligibility or procedural
condition makes a whole mixed clause non-exempt. Writing, listening/audio,
style-guide and interface skills are specifically outside the generic boundary.

The optional `clause_materiality` annotation is persisted inside the existing
enrichment JSON, through its normal validation, merge, run provenance and
failure/reuse paths. Old payloads/documents without it remain valid. There is
no database-schema change or inventory backfill. The acceptance-guard recipe is
versioned (`opportunity_llm_acceptance_guards_v10`); no existing record acquires
an annotation automatically.

`load_card_sources` reads annotations only for its existing bounded source pool.
For nonempty annotations it reuses accepted-source integrity, semantic-input and
derivation freshness checks. Only current automatic model enrichment can supply
the annotation; no override or arbitrary source metadata creates it. A current
catalog must reproduce the stored clause and exact variant binding. Old recipes,
changed content, missing provenance, invalid/ambiguous classifications and
unaccepted source content grant no exception. Healthy identical recapture can
change the accepted-capture binding without changing semantic input identity.
The old annotation then becomes unusable until ordinary enrichment validates
new output against the current accepted clauses. There is no reference relinking
or promise of uninterrupted main admission during that uncertainty.

## Capture-binding lifecycle

`classify_enrichment_freshness` shares `annotation_freshness` with the admission
source loader. It reports `clause_binding_status` separately from source-input
and derivation status, with `clause_evidence_binding_changed` for stale/invalid
annotations. Semantic hashes still exclude capture references; unrelated semantic
outputs and legacy documents with absent/empty annotations retain their existing
reuse behavior.

The ordinary `enrich_canonical_opportunity` entry point accounts for this status
before both `already_enriched` and prior-attempt suppression. An explicitly
requested, otherwise eligible model enrichment may regenerate a stale binding.
Repair attempts record the current accepted-clause fingerprint in the existing
run-diagnostics JSON, without a schema change. Attempts against capture A cannot
suppress a first repair at B; a failed repair at unchanged B still returns
`already_attempted` on repetition. A successful B replacement returns
`already_enriched` thereafter. Failed output retains previous success under the
existing protection, but the stale annotation remains unusable for admission.
Nothing edits the old references into apparently current evidence.

The existing single-canonical command (`scripts/enrich_opportunities.py`,
`--canonical-id ID --llm`) delegates directly to this entry point. Selected/all
helpers also share it when called with a model client. Their selection and
scheduling are unchanged: tracking still selects semantic changes, so a capture-
only confirmation is not automatically scheduled. A deterministic/no-model call
does not regenerate or erase the old annotation. No automatic backfill, retry
loop, scheduler or live operation is introduced.

## Admission consumer

`apply_task_condition_review` consumes this annotation only after existing
task-fit, location, language and trust guards. An exception requires an
`unassessed` / `unresolved` comparison without conflicting modality or a concrete
comparison message. Recognized education/tools/background/workload comparisons
and contradictions are never exempted by this annotation. Generic questions are
retained in `non_decisive_source_questions` with classification provenance;
remaining material questions still control the existing conditional/exclusion
path. Optional profile-item details remain explanation-only.

The admission reuse key advances to version 5. Existing database commit/input
invalidation covers newly persisted enrichment. Nothing runs a model at match
time, changes availability, or promotes an unrelated opportunity through task
wording alone. Numeric scoring and preferred-condition handling are unchanged.

## Offline validation and limits

The synthetic integration tests use explicitly labelled semantic-output fixtures
through ordinary accepted-source ingestion and `enrich_canonical_opportunity`.
They exercise authenticated matching, old-run invalidation and exact-variant
details. They demonstrate the intended generic-only conditional-to-main change,
continued uncertainty for the five specific clauses in the public Alignerr
wording, and retained material unknown/conflict gates. No personal profile is a
test fixture; the Alignerr clause test uses an anonymous synthetic profile.

The disposable lifecycle controls exercise accepted A, identical accepted B,
conservative admission while A is stale, validated regeneration at B, and
idempotent repetition. A held non-authoritative observation does not change the
accepted binding or trigger generation. An obsolete model alias fails normal
validation, cannot restore admission and is not repeatedly attempted at unchanged
evidence. Missing legacy annotations do not initiate a backfill.

These tests validate source binding and consumption, **not automatic semantic
classification quality**. Structural validation cannot establish that a model
correctly classified arbitrary prose. A model incorrectly labelling an otherwise
unrecognized mixed/technical clause as exclusively generic remains a semantic
quality risk; there is deliberately no keyword fallback pretending to prove
meaning. Later enrichment/output validation is needed before claiming automatic
coverage. No live output was generated or inventory rewritten for this change.

## Explaining conditional placement

Display preparation may receive an explicit conditional-placement flag from the
existing selected list, or from an exact-variant detail's established conditional
membership. Only the `accepted_task_source_conditions` route with an uncertain
`accepted_task_conditions` review can produce “Why this is a possibility”. The
display copies the existing decisive `source_task_fit.conditions`; it does not
reclassify clauses or infer causes from headings. Each condition must still match
the current packet's exact source reference and comparison status. Missing or
mismatched evidence omits the new explanation rather than inventing a cause.

The summary describes these questions as contributing reasons, not an exhaustive
account of conditional placement. Other aspects may still need review; resolving
the listed questions alone is not a promise of main admission. Existing specialist
uncertainty can coexist with the final source-condition review without being
aggregated or reinterpreted by this display-only explanation.

Generic non-decisive questions and preferred clauses remain in the original
employer wording, outside the causal list. Existing source-bound language support
can be acknowledged as component support only; it does not satisfy a combined
writing or other qualification. Main, conflicting, other-route and unestablished
detail membership do not receive this conditional-placement explanation.

The shared renderer is used by the conditional-card disclosure and exact-variant
details. Admission's default evidence preparation remains unchanged; the new
packet field is display-only. No score, selection, availability, action permission
or form/link binding is changed.

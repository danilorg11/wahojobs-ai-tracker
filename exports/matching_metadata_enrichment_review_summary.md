# Matching Metadata Enrichment Review Summary

This is a review-only artifact. No fixture snapshots, matcher behavior, database rows, labels, sections, or product UI were changed.

## Counts

- Total human-reviewed cases inspected: **30**
- Cases included in enrichment review batch: **22**
- Counts by fidelity classification: ambiguous_identity=1, fixture_missing_structured_metadata=20, product_policy_not_represented=1
- Counts by primary issue: dataannotation_bilingual_evergreen_archetype_gap=3, legal_ip_metadata_gap=1, location_actionability_metadata_gap=1, medical_science_metadata_gap=2, missing_structured_metadata=13, professional_domain_gate_not_represented=1, unsafe_meridial_pci_title_variants=1
- Missing field counts: canonical_opportunity_id=22, department=22, expertise=22, external_id=22, language=22, language_locale=22, required_languages=22, source_category=22, source_hash=22, url=22
- Exact stable live row availability: no=22
- Cases with unsafe title/similar candidates only: 1
- Cases requiring human approval before enrichment: 22
- Cases where matching changes should be blocked until metadata review: 22

## Baseline Check

- Human-reviewed baseline remains: labels 9/30, sections 17/30, full 8/30

## Focused Case Classifications

- `portuguese_english_reviewer_010`: `fixture_missing_structured_metadata` / `dataannotation_bilingual_evergreen_archetype_gap` - Bilingual DataAnnotation evergreen archetype is human-labeled as useful, but the snapshot still carries live-posting/api-feed defaults and lacks structured language/taxonomy fields.
- `multilingual_translator_010`: `fixture_missing_structured_metadata` / `dataannotation_bilingual_evergreen_archetype_gap` - Bilingual DataAnnotation evergreen archetype is relevant to the profile, but the snapshot needs human-approved fixture-only archetype metadata.
- `beginner_bilingual_no_degree_005`: `fixture_missing_structured_metadata` / `dataannotation_bilingual_evergreen_archetype_gap` - Broad bilingual evergreen application is plausible for this beginner profile, but structured language/taxonomy fields are missing.
- `lawyer_002`: `fixture_missing_structured_metadata` / `legal_ip_metadata_gap` - The human label treats IP Expert as legal-domain work, but the fixture snapshot lacks legal/IP structured category fields. The title is context only and must not be auto-converted into legal taxonomy.
- `biology_or_medicine_academic_003`: `fixture_missing_structured_metadata` / `medical_science_metadata_gap` - The human label treats Microbiology Specialist as direct science-domain work, but the snapshot lacks structured science/medical metadata.
- `biology_or_medicine_academic_007`: `fixture_missing_structured_metadata` / `medical_science_metadata_gap` - The human label treats Academic Dermatologist as medical-domain work, but the snapshot lacks structured medical/credential metadata.
- `phd_history_researcher_011`: `product_policy_not_represented` / `professional_domain_gate_not_represented` - The human label excludes a medical-specialty role for a humanities profile. That product policy is not represented by current structured fixture fields, so matching changes should wait for explicit metadata or gate design.
- `generalist_no_degree_013`: `ambiguous_identity` / `unsafe_meridial_pci_title_variants` - Meridial PCI appears to have multiple title-only live candidates. The review batch must not select a representative row without human-approved stable identity.
- `phd_history_researcher_013`: `fixture_missing_structured_metadata` / `location_actionability_metadata_gap` - The title mentions Morocco, but the snapshot lacks structured applicant location requirements. The title-location clue is diagnostic only.

## Reviewer Notes

- Title/source live candidates are diagnostics only and were not applied as fixture metadata.
- Proposed metadata fields are intentionally blank for human editing.
- Use review decisions: `leave_unchanged`, `approve_metadata_update`, `needs_more_research`, `ambiguous_keep_fixture_only`, `tie_to_stable_row`, `remove_or_replace_case_later`.
- Do not tune matching behavior from these cases until sparse or ambiguous fixture metadata has been reviewed.

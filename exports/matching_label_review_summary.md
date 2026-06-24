# Matching Label Review Summary

Generated: 2026-06-24T00:04:53.001592+00:00

This enrichment preserves the existing review batch and adds review context only.

## Preservation Check

- Input batch: `exports\matching_label_review.csv`
- Rows before: **30**
- Rows after: **30**
- Ordered `case_id` list identical: **yes**
- Pre-existing reference, matcher-output, rationale, question, and human-review values preserved: **yes**
- No cases were added or removed.
- `review_priority`, expected labels/sections, review-required flags, label source, regression rules, scores, matcher labels, sections, gates, reasons, rationale, questions, and existing human-review values were not changed.

## Context Added

- Cases with usable job URL: **8**
- Cases with unavailable or ambiguous URL/context resolution: **1**
- Ambiguous cases with candidate rows: **1**
- Cases with explicit location restrictions: **3**
- Restricted cases where profile location is unknown: **3**
- Profiles with unknown location: **9**

## Cases By Profile

- `beginner_bilingual_no_degree`: 8
- `phd_history_researcher`: 5
- `biology_or_medicine_academic`: 4
- `lawyer`: 3
- `generalist_no_degree`: 3
- `multilingual_translator`: 3
- `english_teacher_remote`: 2
- `portuguese_english_reviewer`: 1
- `software_engineer`: 1

## Job Context Resolution

- `partial`: 21
- `resolved`: 8
- `ambiguous`: 1

## Ambiguous Job Candidates

- `generalist_no_degree_013`: Meridial - Pavement Condition Index (PCI) Survey & Annotation Specialist - Freelance AI Trainer Project (8 candidate rows)
  - Location: World Wide - Remote; URL: https://job-boards.eu.greenhouse.io/agency/jobs/4775898101; IDs: job_id=132; external_id=4775898101; canonical=1032; hash=6f4272ef0e52a6537d6b5e7d2e8f287d8e60f12575fc64c456125aaa1f91b62b
  - Location: Poland; URL: https://job-boards.eu.greenhouse.io/agency/jobs/4781816101; IDs: job_id=133; external_id=4781816101; canonical=1032; hash=aee39f3f046427d78d8678c2612d9320e3e6d5172f4d9f1a7a2998c8c4e5a913
  - Location: Hungary; URL: https://job-boards.eu.greenhouse.io/agency/jobs/4781817101; IDs: job_id=134; external_id=4781817101; canonical=1032; hash=2642e6ad9709df8484bfff334df04beb89617e36ef16f1a47dff7df071a9b6ce
  - Location: Serbia; URL: https://job-boards.eu.greenhouse.io/agency/jobs/4781818101; IDs: job_id=135; external_id=4781818101; canonical=1032; hash=90ff2eb137177d424487398fba5b32504d19a56967c627c5d9b57fd3d36d23be
  - Location: Greece; URL: https://job-boards.eu.greenhouse.io/agency/jobs/4781822101; IDs: job_id=136; external_id=4781822101; canonical=1032; hash=f0b0c3d9e84538c47d9033b0c80e504df428361be04d5e5aae4241ef54d3b1b1
  - Location: Croatia; URL: https://job-boards.eu.greenhouse.io/agency/jobs/4781823101; IDs: job_id=137; external_id=4781823101; canonical=1032; hash=757ba72051907fe71a63019f3d781fdad12a22f5be00000f0d7410d53af0deab
  - Location: Czechia; URL: https://job-boards.eu.greenhouse.io/agency/jobs/4781824101; IDs: job_id=138; external_id=4781824101; canonical=1032; hash=e8f10d81c2a733b78583da806f0be4e6e1633c1089e39ff8d52e1ed8e38e2688
  - Location: Slovenia; URL: https://job-boards.eu.greenhouse.io/agency/jobs/4781825101; IDs: job_id=139; external_id=4781825101; canonical=1032; hash=74faf61f09b2304036d2a41d7287a3822b5466d58247c667f7db6e4666e5f772

## Location Scope

- `unknown`: 27
- `onsite_or_hybrid_restricted`: 3

## Location Eligibility

- `unknown`: 30

## Restricted Cases With Unknown Profile Location

- `beginner_bilingual_no_degree_006`: Meridial - Social Media Annotation - Freelance AI Trainer Project (United States of America)
- `generalist_no_degree_007`: Meridial - Social Media Annotation - Freelance AI Trainer Project (United States of America)
- `phd_history_researcher_014`: Welocalize - Remote Internet Search Quality Rater - English (United States) (United States)

## Review Notes

- Missing profile location remains `unknown`; no profile locations were invented.
- Speaking a language was not treated as evidence of residence.
- `Remote` alone was not treated as worldwide availability.
- This did not change production matching behavior, scores, gates, planner behavior, UI behavior, crawlers, schema, product-state data, live market estimates, sample profiles, or golden-set labels.

## Human Review Instructions

1. Open the HTML file for context.
2. Edit only the human columns in the CSV.
3. Use labels: `strong`, `plausible`, `weak`, `false_positive`.
4. Use sections: `do_these_first`, `best_matches`, `also_worth_reviewing`, `explore_only`, `exclude`.
5. Leave a row blank if no decision is approved yet.

Dry run before applying approved decisions:

```bash
python scripts/matching_label_review.py apply --input exports/matching_label_review.csv --dry-run
```

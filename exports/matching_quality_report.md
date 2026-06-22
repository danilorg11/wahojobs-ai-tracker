# Matching Quality Benchmark Baseline

Generated: 2026-06-22T01:11:06.531516+00:00

## Scope

This report evaluates the current production profile matcher against a draft fixture pool. It does not change scoring, thresholds, planner logic, crawlers, schema, product-state data, or live market estimates.

All fixture labels are `codex_draft` labels. They are proposed baseline judgments for review, not final human-approved truth.

Precision, recall, and false-positive metrics below are fixture-pool metrics only. They are not universal production accuracy estimates.

## Fixture Summary

- Fixture: `tests/fixtures/matching_golden_set.json`
- Total cases: 160
- Headline metric cases: 151
- Review-required cases excluded from headline metrics: 9
- DB resolution: live_db_url=89, fixture_snapshot=55, live_db_title=16
- Label distribution, all cases: strong=61, false_positive=58, plausible=30, weak=11
- Label distribution, headline cases: strong=59, false_positive=58, plausible=26, weak=8

## Fixture-Pool Metrics By Profile

| Profile | Cases | Relevant P@4 | Relevant P@10 | Strict P@4 | Strict P@10 | FP@10 | Relevant Recall | Hard Regressions |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| beginner_bilingual_no_degree | 15 | 75% | 50% | 50% | 20% | 50% | 86% | 7 |
| biology_or_medicine_academic | 16 | 100% | 100% | 100% | 80% | 0% | 91% | 0 |
| english_teacher_remote | 16 | 100% | 70% | 50% | 50% | 30% | 88% | 5 |
| finance_professional | 15 | 100% | 100% | 100% | 100% | 0% | 100% | 1 |
| generalist_no_degree | 15 | 100% | 70% | 75% | 30% | 20% | 88% | 6 |
| lawyer | 16 | 100% | 90% | 100% | 80% | 0% | 90% | 5 |
| multilingual_translator | 13 | 100% | 70% | 100% | 60% | 30% | 100% | 3 |
| phd_history_researcher | 15 | 50% | 40% | 0% | 10% | 20% | 80% | 5 |
| portuguese_english_reviewer | 15 | 100% | 90% | 100% | 70% | 10% | 100% | 6 |
| software_engineer | 15 | 100% | 90% | 100% | 70% | 10% | 100% | 5 |

## Hard Regression Failures

### beginner_bilingual_no_degree - beginner_bilingual_no_degree_010

- Opportunity: Welocalize - Alpheratz Project - Catalan Translation Quality Rater
- Expected: `false_positive` / `exclude`
- Current: score 34, `Strong`, `do_these_first`
- Rationale: Catalan is unsupported.
- Regression rule: `unsupported_explicit_language`
- Failure patterns: `visible_false_positive`, `exclude_case_visible`, `unsupported_language`, `unsupported_explicit_language`
- Positive contributions: +10 Language/translation signal: translation, +8 AI evaluation/training signal: rater, +7 Search/research quality signal: quality, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Language/translation signal, AI evaluation/training signal, Search/research quality signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: unsupported explicit language: catalan

### beginner_bilingual_no_degree - beginner_bilingual_no_degree_011

- Opportunity: Welocalize - Alpheratz Project - Czech Translation Quality Rater
- Expected: `false_positive` / `exclude`
- Current: score 34, `Strong`, `do_these_first`
- Rationale: Czech is unsupported.
- Regression rule: `unsupported_explicit_language`
- Failure patterns: `visible_false_positive`, `exclude_case_visible`, `unsupported_language`, `unsupported_explicit_language`
- Positive contributions: +10 Language/translation signal: translation, +8 AI evaluation/training signal: rater, +7 Search/research quality signal: quality, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Language/translation signal, AI evaluation/training signal, Search/research quality signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: unsupported explicit language: czech

### beginner_bilingual_no_degree - beginner_bilingual_no_degree_012

- Opportunity: Welocalize - Alpheratz Project - Danish Translation Quality Rater
- Expected: `false_positive` / `exclude`
- Current: score 34, `Strong`, `do_these_first`
- Rationale: Danish is unsupported.
- Regression rule: `unsupported_explicit_language`
- Failure patterns: `visible_false_positive`, `exclude_case_visible`, `unsupported_language`, `unsupported_explicit_language`
- Positive contributions: +10 Language/translation signal: translation, +8 AI evaluation/training signal: rater, +7 Search/research quality signal: quality, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Language/translation signal, AI evaluation/training signal, Search/research quality signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: unsupported explicit language: danish

### beginner_bilingual_no_degree - beginner_bilingual_no_degree_013

- Opportunity: Welocalize - Alpheratz Project - Dutch Translation Quality Rater
- Expected: `false_positive` / `exclude`
- Current: score 34, `Strong`, `do_these_first`
- Rationale: Dutch is unsupported.
- Regression rule: `unsupported_explicit_language`
- Failure patterns: `visible_false_positive`, `exclude_case_visible`, `unsupported_language`, `unsupported_explicit_language`
- Positive contributions: +10 Language/translation signal: translation, +8 AI evaluation/training signal: rater, +7 Search/research quality signal: quality, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Language/translation signal, AI evaluation/training signal, Search/research quality signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: unsupported explicit language: dutch

### beginner_bilingual_no_degree - beginner_bilingual_no_degree_014

- Opportunity: Welocalize - Alpheratz Project - Korean Translation Quality Rater
- Expected: `false_positive` / `exclude`
- Current: score 34, `Strong`, `do_these_first`
- Rationale: Korean is unsupported.
- Regression rule: `unsupported_explicit_language`
- Failure patterns: `visible_false_positive`, `exclude_case_visible`, `unsupported_language`, `unsupported_explicit_language`
- Positive contributions: +10 Language/translation signal: translation, +8 AI evaluation/training signal: rater, +7 Search/research quality signal: quality, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Language/translation signal, AI evaluation/training signal, Search/research quality signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: unsupported explicit language: korean

### beginner_bilingual_no_degree - beginner_bilingual_no_degree_009

- Opportunity: Outlier - Kiswahili Freelance Writer
- Expected: `false_positive` / `exclude`
- Current: score 9, `Possible`, `explore_only`
- Rationale: Kiswahili is unsupported for English-Spanish bilingual profile.
- Regression rule: `unsupported_explicit_language`
- Failure patterns: `unsupported_language`, `unsupported_explicit_language`
- Positive contributions: +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Remote/flexible signal, Live/countable opportunity
- Contradictions: unsupported explicit language: kiswahili

### beginner_bilingual_no_degree - beginner_bilingual_no_degree_015

- Opportunity: Turing - Senior Python Developer
- Expected: `false_positive` / `exclude`
- Current: score 9, `Possible`, `explore_only`
- Rationale: Senior software role is outside beginner no-degree profile.
- Regression rule: `technical_mismatch`
- Failure patterns: `professional_domain_mismatch`, `credential_or_specialty_mismatch`, `technical_mismatch`
- Positive contributions: +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Remote/flexible signal, Live/countable opportunity
- Contradictions: specialist credential/domain mismatch

### english_teacher_remote - english_teacher_remote_011

- Opportunity: Welocalize - Alpheratz Project - Catalan Translation Quality Rater
- Expected: `false_positive` / `exclude`
- Current: score 27, `Medium`, `best_matches`
- Rationale: Catalan is unsupported.
- Regression rule: `unsupported_explicit_language`
- Failure patterns: `visible_false_positive`, `exclude_case_visible`, `unsupported_language`, `unsupported_explicit_language`
- Positive contributions: +10 Language/translation signal: translation, +8 AI evaluation/training signal: rater, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Language/translation signal, AI evaluation/training signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: unsupported explicit language: catalan

### english_teacher_remote - english_teacher_remote_012

- Opportunity: Welocalize - Alpheratz Project - Czech Translation Quality Rater
- Expected: `false_positive` / `exclude`
- Current: score 27, `Medium`, `best_matches`
- Rationale: Czech is unsupported.
- Regression rule: `unsupported_explicit_language`
- Failure patterns: `visible_false_positive`, `exclude_case_visible`, `unsupported_language`, `unsupported_explicit_language`
- Positive contributions: +10 Language/translation signal: translation, +8 AI evaluation/training signal: rater, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Language/translation signal, AI evaluation/training signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: unsupported explicit language: czech

### english_teacher_remote - english_teacher_remote_013

- Opportunity: Outlier - Polish STEM Writing Expertise Sought for AI Training
- Expected: `false_positive` / `exclude`
- Current: score 26, `Medium`, `best_matches`
- Rationale: Polish and STEM requirements are unsupported.
- Regression rule: `unsupported_explicit_language`
- Failure patterns: `visible_false_positive`, `exclude_case_visible`, `generic_evaluation_evidence`, `unsupported_language`, `unsupported_explicit_language`
- Positive contributions: +9 Teaching/writing/review signal: writing, +8 AI evaluation/training signal: ai training, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Teaching/writing/review signal, AI evaluation/training signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: unsupported explicit language: polish

### english_teacher_remote - english_teacher_remote_009

- Opportunity: OneForma - HT (Human Translation) and MTPE (Machine Translation Post Editing) - Chinese (Simplified) - English (United States)
- Expected: `false_positive` / `exclude`
- Current: score 25, `Medium`, `best_matches`
- Rationale: Requires Chinese-English translation, unsupported by profile.
- Regression rule: `unsupported_explicit_language`
- Failure patterns: `visible_false_positive`, `exclude_case_visible`, `unsupported_language`, `unsupported_explicit_language`
- Positive contributions: +10 Language/translation signal: translation, +6 English language signal: english, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Language/translation signal, English language signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: unsupported explicit language: chinese

### english_teacher_remote - english_teacher_remote_010

- Opportunity: OneForma - HT (Human Translation) and MTPE (Machine Translation Post Editing) - English (United States) - Norwegian (Norway)
- Expected: `false_positive` / `exclude`
- Current: score 25, `Medium`, `best_matches`
- Rationale: Requires Norwegian translation, unsupported by profile.
- Regression rule: `unsupported_explicit_language`
- Failure patterns: `visible_false_positive`, `exclude_case_visible`, `unsupported_language`, `unsupported_explicit_language`
- Positive contributions: +10 Language/translation signal: translation, +6 English language signal: english, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Language/translation signal, English language signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: unsupported explicit language: norwegian

### finance_professional - finance_professional_012

- Opportunity: Mercor - Legal Expert Specialist
- Expected: `false_positive` / `exclude`
- Current: score 16, `Possible`, `explore_only`
- Rationale: Legal expert role is outside finance unless finance law is explicit.
- Regression rule: `professional_domain_mismatch`
- Failure patterns: `professional_domain_mismatch`
- Positive contributions: +7 Expert review signal: expert, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Expert review signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: -

### generalist_no_degree - generalist_no_degree_009

- Opportunity: Mindrift - Freelance Mathematics Expert - AI Trainer
- Expected: `false_positive` / `exclude`
- Current: score 25, `Medium`, `best_matches`
- Rationale: Requires math expertise not present in the no-degree generalist profile.
- Regression rule: `generalist_specialist_domain_mismatch`
- Failure patterns: `visible_false_positive`, `exclude_case_visible`, `generalist_specialist_domain_mismatch`
- Positive contributions: +9 Generalist AI-work signal: ai trainer, +7 Writing/review signal: writer, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Generalist AI-work signal, Writing/review signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: -

### generalist_no_degree - generalist_no_degree_010

- Opportunity: Mindrift - Material Science Expert with Python - Freelance AI Trainer
- Expected: `false_positive` / `exclude`
- Current: score 25, `Medium`, `best_matches`
- Rationale: Combines science and Python expertise, not a generalist no-degree fit.
- Regression rule: `generalist_specialist_domain_mismatch`
- Failure patterns: `visible_false_positive`, `exclude_case_visible`, `professional_domain_mismatch`, `credential_or_specialty_mismatch`, `generalist_specialist_domain_mismatch`
- Positive contributions: +9 Generalist AI-work signal: ai trainer, +7 Writing/review signal: writer, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Generalist AI-work signal, Writing/review signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: specialist credential/domain mismatch

### generalist_no_degree - generalist_no_degree_011

- Opportunity: Mindrift - Direct Endorsement (DE) Underwriter - Freelance AI Trainer
- Expected: `false_positive` / `exclude`
- Current: score 25, `Medium`, `best_matches`
- Rationale: Mortgage underwriting is a specialist credentialed domain, not a generalist fit.
- Regression rule: `generalist_specialist_domain_mismatch`
- Failure patterns: `visible_false_positive`, `exclude_case_visible`, `generalist_specialist_domain_mismatch`
- Positive contributions: +9 Generalist AI-work signal: ai trainer, +7 Writing/review signal: writer, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Generalist AI-work signal, Writing/review signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: -

### generalist_no_degree - generalist_no_degree_016

- Opportunity: Turing - Senior Python Developer
- Expected: `false_positive` / `exclude`
- Current: score 9, `Possible`, `explore_only`
- Rationale: Senior coding role is outside a non-coding generalist profile.
- Regression rule: `technical_mismatch`
- Failure patterns: `professional_domain_mismatch`, `credential_or_specialty_mismatch`, `technical_mismatch`
- Positive contributions: +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Remote/flexible signal, Live/countable opportunity
- Contradictions: specialist credential/domain mismatch

### generalist_no_degree - generalist_no_degree_014

- Opportunity: micro1 - Legal Expert
- Expected: `false_positive` / `exclude`
- Current: score 6, `Possible`, `explore_only`
- Rationale: Legal expertise is a specialist domain outside this profile.
- Regression rule: `generalist_specialist_domain_mismatch`
- Failure patterns: `professional_domain_mismatch`, `credential_or_specialty_mismatch`, `generalist_specialist_domain_mismatch`
- Positive contributions: +9 Generalist AI-work signal: generalist, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -12 Possible requirement mismatch: legal
- User-facing reasons: Generalist AI-work signal, Remote/flexible signal, Live/countable opportunity, Possible requirement mismatch; review carefully
- Contradictions: specialist credential/domain mismatch

### generalist_no_degree - generalist_no_degree_015

- Opportunity: micro1 - Finance Expert
- Expected: `false_positive` / `exclude`
- Current: score 0, `Possible`, `explore_only`
- Rationale: Finance expertise is a specialist domain outside this profile.
- Regression rule: `generalist_specialist_domain_mismatch`
- Failure patterns: `professional_domain_mismatch`, `credential_or_specialty_mismatch`, `generalist_specialist_domain_mismatch`
- Positive contributions: +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -12 Possible requirement mismatch: finance
- User-facing reasons: Remote/flexible signal, Live/countable opportunity, Possible requirement mismatch; review carefully
- Contradictions: specialist credential/domain mismatch

### lawyer - lawyer_012

- Opportunity: Mercor - Corporate Finance Expert
- Expected: `false_positive` / `exclude`
- Current: score 16, `Possible`, `explore_only`
- Rationale: Finance role is not legal unless legal requirements are explicit.
- Regression rule: `professional_domain_mismatch`
- Failure patterns: `professional_domain_mismatch`
- Positive contributions: +7 Expert review signal: expert, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Expert review signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: -

### lawyer - lawyer_014

- Opportunity: Mindrift - Freelance Mathematics Expert - AI Trainer
- Expected: `false_positive` / `exclude`
- Current: score 16, `Possible`, `explore_only`
- Rationale: Mathematics expert role is outside legal domain.
- Regression rule: `professional_domain_mismatch`
- Failure patterns: `professional_domain_mismatch`
- Positive contributions: +7 Expert review signal: expert, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Expert review signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: -

### lawyer - lawyer_011

- Opportunity: micro1 - Software Engineer - AI Quality & Testing
- Expected: `false_positive` / `exclude`
- Current: score 9, `Possible`, `explore_only`
- Rationale: Software engineering role is outside legal expertise.
- Regression rule: `technical_mismatch`
- Failure patterns: `technical_mismatch`
- Positive contributions: +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Remote/flexible signal, Live/countable opportunity
- Contradictions: -

### lawyer - lawyer_013

- Opportunity: Mercor - Biology Research Scientist
- Expected: `false_positive` / `exclude`
- Current: score 9, `Possible`, `explore_only`
- Rationale: Biology role is outside legal domain.
- Regression rule: `professional_domain_mismatch`
- Failure patterns: `professional_domain_mismatch`
- Positive contributions: +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Remote/flexible signal, Live/countable opportunity
- Contradictions: -

### lawyer - lawyer_016

- Opportunity: Turing - AI Evaluation Engineer (Python / Java / Web)
- Expected: `false_positive` / `exclude`
- Current: score 9, `Possible`, `explore_only`
- Rationale: Technical engineering role is not a legal match.
- Regression rule: `technical_mismatch`
- Failure patterns: `technical_mismatch`
- Positive contributions: +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Remote/flexible signal, Live/countable opportunity
- Contradictions: -

### multilingual_translator - multilingual_translator_013

- Opportunity: OneForma - HT (Human Translation) and MTPE (Machine Translation Post Editing) - English (United States) - Norwegian (Norway)
- Expected: `false_positive` / `exclude`
- Current: score 27, `Medium`, `best_matches`
- Rationale: Norwegian is outside listed languages.
- Regression rule: `-`
- Failure patterns: `visible_false_positive`, `exclude_case_visible`, `unsupported_language`
- Positive contributions: +12 Translation/localization match: translation, +6 English language signal: english, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Translation/localization match, English language signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: unsupported explicit language: norwegian

### multilingual_translator - multilingual_translator_011

- Opportunity: Welocalize - Alpheratz Project - Danish Translation Quality Rater
- Expected: `false_positive` / `exclude`
- Current: score 21, `Possible`, `also_worth_reviewing`
- Rationale: Danish is outside listed languages.
- Regression rule: `-`
- Failure patterns: `visible_false_positive`, `exclude_case_visible`, `unsupported_language`
- Positive contributions: +12 Translation/localization match: translation, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Translation/localization match, Remote/flexible signal, Live/countable opportunity
- Contradictions: unsupported explicit language: danish

### multilingual_translator - multilingual_translator_012

- Opportunity: Welocalize - Alpheratz Project - Korean Translation Quality Rater
- Expected: `false_positive` / `exclude`
- Current: score 21, `Possible`, `also_worth_reviewing`
- Rationale: Korean is outside listed languages.
- Regression rule: `-`
- Failure patterns: `visible_false_positive`, `exclude_case_visible`, `unsupported_language`
- Positive contributions: +12 Translation/localization match: translation, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Translation/localization match, Remote/flexible signal, Live/countable opportunity
- Contradictions: unsupported explicit language: korean

### phd_history_researcher - phd_history_researcher_008

- Opportunity: Turing - Software Engineer - AI Research & Evaluation (US-based)
- Expected: `false_positive` / `exclude`
- Current: score 34, `Strong`, `do_these_first`
- Rationale: Current matcher overweights research/evaluation, but this is a software role.
- Regression rule: `history_software_research_false_positive`
- Failure patterns: `visible_false_positive`, `exclude_case_visible`, `search_inside_research`, `generic_evaluation_evidence`, `professional_domain_mismatch`, `history_software_research_false_positive`
- Positive contributions: +10 Research/humanities signal: research, +8 AI evaluation/training signal: evaluation, +7 Search/research quality signal: search, research, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Research/humanities signal, AI evaluation/training signal, Search/research quality signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: domain mismatch for humanities research profile

### phd_history_researcher - phd_history_researcher_009

- Opportunity: Mercor - Biology & Biophysics Researchers (India, Part-time)
- Expected: `false_positive` / `exclude`
- Current: score 26, `Medium`, `best_matches`
- Rationale: Research term matches, but biology/biophysics is outside humanities history.
- Regression rule: `history_science_research_false_positive`
- Failure patterns: `visible_false_positive`, `exclude_case_visible`, `search_inside_research`, `professional_domain_mismatch`, `history_science_research_false_positive`
- Positive contributions: +10 Research/humanities signal: research, +7 Search/research quality signal: search, research, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Research/humanities signal, Search/research quality signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: domain mismatch for humanities research profile

### phd_history_researcher - phd_history_researcher_010

- Opportunity: Mercor - Human Baseliner for Open-Ended ML Research Tasks
- Expected: `weak` / `explore_only`
- Current: score 26, `Medium`, `best_matches`
- Rationale: Generic ML research task may be adjacent but is not clearly humanities work.
- Regression rule: `generic_research_overpromotion`
- Failure patterns: `search_inside_research`, `generic_research_overpromotion`
- Positive contributions: +10 Research/humanities signal: research, +7 Search/research quality signal: search, research, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Research/humanities signal, Search/research quality signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: -

### phd_history_researcher - phd_history_researcher_011

- Opportunity: Turing - Academic Dermatologist
- Expected: `false_positive` / `exclude`
- Current: score 19, `Possible`, `also_worth_reviewing`
- Rationale: Academic title should not override medical-specialty mismatch.
- Regression rule: `history_medical_false_positive`
- Failure patterns: `visible_false_positive`, `exclude_case_visible`, `professional_domain_mismatch`, `history_medical_false_positive`
- Positive contributions: +10 Research/humanities signal: academic, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Research/humanities signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: domain mismatch for humanities research profile

### phd_history_researcher - phd_history_researcher_012

- Opportunity: OneForma - Project Spring - Healthcare Data Improvement Project - English (Ireland)
- Expected: `false_positive` / `exclude`
- Current: score 15, `Possible`, `explore_only`
- Rationale: Healthcare data project is outside history/humanities despite English/research signals.
- Regression rule: `history_healthcare_false_positive`
- Failure patterns: `history_healthcare_false_positive`
- Positive contributions: +6 English language signal: english, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: English language signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: -

### portuguese_english_reviewer - portuguese_english_reviewer_011

- Opportunity: Welocalize - Alpheratz Project - Czech Translation Quality Rater
- Expected: `false_positive` / `exclude`
- Current: score 33, `Medium`, `do_these_first`
- Rationale: Czech is not an explicit supported language for this profile.
- Regression rule: `unsupported_explicit_language`
- Failure patterns: `visible_false_positive`, `exclude_case_visible`, `unsupported_language`, `unsupported_explicit_language`
- Positive contributions: +8 Language review signal: translation, +8 AI evaluation/reviewer signal: rater, quality, +8 Search quality signal: quality rater, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Language review signal, AI evaluation/reviewer signal, Search quality signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: unsupported explicit language: czech

### portuguese_english_reviewer - portuguese_english_reviewer_012

- Opportunity: Welocalize - Alpheratz Project - Danish Translation Quality Rater
- Expected: `false_positive` / `exclude`
- Current: score 33, `Medium`, `do_these_first`
- Rationale: Danish is not an explicit supported language for this profile.
- Regression rule: `unsupported_explicit_language`
- Failure patterns: `visible_false_positive`, `exclude_case_visible`, `unsupported_language`, `unsupported_explicit_language`
- Positive contributions: +8 Language review signal: translation, +8 AI evaluation/reviewer signal: rater, quality, +8 Search quality signal: quality rater, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Language review signal, AI evaluation/reviewer signal, Search quality signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: unsupported explicit language: danish

### portuguese_english_reviewer - portuguese_english_reviewer_013

- Opportunity: Welocalize - Alpheratz Project - Korean Translation Quality Rater
- Expected: `false_positive` / `exclude`
- Current: score 33, `Medium`, `do_these_first`
- Rationale: Korean is not an explicit supported language for this profile.
- Regression rule: `unsupported_explicit_language`
- Failure patterns: `visible_false_positive`, `exclude_case_visible`, `unsupported_language`, `unsupported_explicit_language`
- Positive contributions: +8 Language review signal: translation, +8 AI evaluation/reviewer signal: rater, quality, +8 Search quality signal: quality rater, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Language review signal, AI evaluation/reviewer signal, Search quality signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: unsupported explicit language: korean

### portuguese_english_reviewer - portuguese_english_reviewer_014

- Opportunity: OneForma - HT (Human Translation) and MTPE (Machine Translation Post Editing) - English (United States) - Norwegian (Norway)
- Expected: `false_positive` / `exclude`
- Current: score 23, `Possible`, `best_matches`
- Rationale: Norwegian translation should not rank for a Portuguese-English profile.
- Regression rule: `unsupported_explicit_language`
- Failure patterns: `visible_false_positive`, `exclude_case_visible`, `unsupported_language`, `unsupported_explicit_language`
- Positive contributions: +8 Language review signal: translation, +6 English language signal: english, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Language review signal, English language signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: unsupported explicit language: norwegian

### portuguese_english_reviewer - portuguese_english_reviewer_015

- Opportunity: Mercor - AI Safety Experts - English & Marathi
- Expected: `false_positive` / `exclude`
- Current: score 15, `Possible`, `explore_only`
- Rationale: Marathi requirement is unsupported despite English appearing in the title.
- Regression rule: `unsupported_explicit_language`
- Failure patterns: `unsupported_explicit_language`
- Positive contributions: +6 English language signal: english, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: English language signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: -

### portuguese_english_reviewer - portuguese_english_reviewer_016

- Opportunity: Turing - Senior Python Developer
- Expected: `false_positive` / `exclude`
- Current: score 9, `Possible`, `explore_only`
- Rationale: Coding role is outside Portuguese/English review work.
- Regression rule: `technical_mismatch`
- Failure patterns: `technical_mismatch`
- Positive contributions: +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Remote/flexible signal, Live/countable opportunity
- Contradictions: -

### software_engineer - software_engineer_011

- Opportunity: Mercor - AI Safety Experts - English & Marathi
- Expected: `false_positive` / `exclude`
- Current: score 27, `Medium`, `best_matches`
- Rationale: Language/safety role is not primarily software engineering.
- Regression rule: `technical_mismatch`
- Failure patterns: `visible_false_positive`, `exclude_case_visible`, `technical_mismatch`
- Positive contributions: +12 Coding/technical task match: software, +6 English language signal: english, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Coding/technical task match, English language signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: -

### software_engineer - software_engineer_012

- Opportunity: Mercor - Legal Expert Specialist
- Expected: `false_positive` / `exclude`
- Current: score 9, `Possible`, `explore_only`
- Rationale: Legal domain mismatch for software profile.
- Regression rule: `professional_domain_mismatch`
- Failure patterns: `professional_domain_mismatch`
- Positive contributions: +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Remote/flexible signal, Live/countable opportunity
- Contradictions: -

### software_engineer - software_engineer_013

- Opportunity: Mercor - Corporate Finance Expert
- Expected: `false_positive` / `exclude`
- Current: score 9, `Possible`, `explore_only`
- Rationale: Finance domain mismatch for software profile.
- Regression rule: `professional_domain_mismatch`
- Failure patterns: `professional_domain_mismatch`
- Positive contributions: +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Remote/flexible signal, Live/countable opportunity
- Contradictions: -

### software_engineer - software_engineer_014

- Opportunity: Mercor - Biology Research Scientist
- Expected: `false_positive` / `exclude`
- Current: score 9, `Possible`, `explore_only`
- Rationale: Science domain mismatch for software profile unless coding is explicit.
- Regression rule: `professional_domain_mismatch`
- Failure patterns: `professional_domain_mismatch`
- Positive contributions: +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Remote/flexible signal, Live/countable opportunity
- Contradictions: -

### software_engineer - software_engineer_015

- Opportunity: Welocalize - Alpheratz Project - Portuguese (Portugal) Translation Quality Rater
- Expected: `false_positive` / `exclude`
- Current: score 9, `Possible`, `explore_only`
- Rationale: Portuguese translation role is not a software match.
- Regression rule: `unsupported_explicit_language`
- Failure patterns: `unsupported_explicit_language`
- Positive contributions: +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Remote/flexible signal, Live/countable opportunity
- Contradictions: -


## Section Suitability Overpromotions

| Profile | Case | Expected Section | Current Section | Score | Label |
|---|---|---|---|---:|---|
| beginner_bilingual_no_degree | Welocalize - Alpheratz Project - Spanish (US) Translation Quality Reviewer | best_matches | do_these_first | 41 | Strong |
| beginner_bilingual_no_degree | Welocalize - Alpheratz Project - English (UK) Translation Quality Rater | also_worth_reviewing | do_these_first | 40 | Strong |
| beginner_bilingual_no_degree | Welocalize - Alpheratz Project - Spanish (US) Translation Quality Rater | best_matches | do_these_first | 40 | Strong |
| beginner_bilingual_no_degree | Welocalize - Alpheratz Project - Catalan Translation Quality Rater | exclude | do_these_first | 34 | Strong |
| beginner_bilingual_no_degree | Welocalize - Alpheratz Project - Czech Translation Quality Rater | exclude | do_these_first | 34 | Strong |
| beginner_bilingual_no_degree | Welocalize - Alpheratz Project - Danish Translation Quality Rater | exclude | do_these_first | 34 | Strong |
| beginner_bilingual_no_degree | Welocalize - Alpheratz Project - Dutch Translation Quality Rater | exclude | do_these_first | 34 | Strong |
| beginner_bilingual_no_degree | Welocalize - Alpheratz Project - Korean Translation Quality Rater | exclude | do_these_first | 34 | Strong |
| beginner_bilingual_no_degree | Outlier - English Writing and Content Reviewing Expertise Sought for AI Training | also_worth_reviewing | do_these_first | 32 | Medium |
| beginner_bilingual_no_degree | OneForma - HT (Human Translation) and MTPE (Machine Translation Post Editing) - English (United States) - Spanish (Chile) | also_worth_reviewing | do_these_first | 31 | Medium |
| beginner_bilingual_no_degree | Meridial - Search Engine Evaluation Specialist - Freelance AI Trainer Project | also_worth_reviewing | best_matches | 24 | Medium |
| beginner_bilingual_no_degree | Outlier - Kiswahili Freelance Writer | exclude | explore_only | 9 | Possible |
| beginner_bilingual_no_degree | Turing - Senior Python Developer | exclude | explore_only | 9 | Possible |
| beginner_bilingual_no_degree | Mercor - Legal Expert Specialist | exclude | explore_only | 9 | Possible |
| biology_or_medicine_academic | Mercor - Clinical / biomedical / pharma Evaluator | best_matches | do_these_first | 37 | Strong |
| biology_or_medicine_academic | Mercor - Biology Expert with Python Experience | best_matches | do_these_first | 34 | Strong |
| biology_or_medicine_academic | Mercor - Biology Research Scientist | best_matches | do_these_first | 30 | Medium |
| biology_or_medicine_academic | micro1 - Biology Expert | best_matches | do_these_first | 30 | Medium |
| biology_or_medicine_academic | DataAnnotation - Medicine Expert / Medical AI Trainer | also_worth_reviewing | best_matches | 29 | Medium |
| biology_or_medicine_academic | DataAnnotation - Biology Expert / AI Biology Trainer | also_worth_reviewing | best_matches | 29 | Medium |
| biology_or_medicine_academic | Outlier - English Writing and Content Reviewing Expertise Sought for AI Training | explore_only | best_matches | 22 | Possible |
| biology_or_medicine_academic | Mercor - Legal Expert Specialist | exclude | explore_only | 16 | Possible |
| biology_or_medicine_academic | Mercor - Corporate Finance Expert | exclude | explore_only | 16 | Possible |
| biology_or_medicine_academic | Turing - Senior Python Developer | exclude | explore_only | 13 | Possible |
| biology_or_medicine_academic | Welocalize - Alpheratz Project - Portuguese (Portugal) Translation Quality Rater | exclude | explore_only | 9 | Possible |
| english_teacher_remote | Welocalize - Alpheratz Project - English (UK) Translation Quality Rater | also_worth_reviewing | do_these_first | 33 | Medium |
| english_teacher_remote | Turing - Audio/Voice/Annotation Trainer - English Language | also_worth_reviewing | do_these_first | 33 | Medium |
| english_teacher_remote | Alignerr - English Writing Generalist | best_matches | do_these_first | 31 | Medium |
| english_teacher_remote | Alignerr - English Writing Generalist - Advanced | best_matches | do_these_first | 31 | Medium |
| english_teacher_remote | Alignerr - English Writing Generalist - Quality Review | best_matches | do_these_first | 31 | Medium |
| english_teacher_remote | Welocalize - Alpheratz Project - Catalan Translation Quality Rater | exclude | best_matches | 27 | Medium |
| english_teacher_remote | Welocalize - Alpheratz Project - Czech Translation Quality Rater | exclude | best_matches | 27 | Medium |
| english_teacher_remote | Outlier - Polish STEM Writing Expertise Sought for AI Training | exclude | best_matches | 26 | Medium |
| english_teacher_remote | OneForma - HT (Human Translation) and MTPE (Machine Translation Post Editing) - Chinese (Simplified) - English (United States) | exclude | best_matches | 25 | Medium |
| english_teacher_remote | OneForma - HT (Human Translation) and MTPE (Machine Translation Post Editing) - English (United States) - Norwegian (Norway) | exclude | best_matches | 25 | Medium |
| english_teacher_remote | Turing - Senior Python Developer | exclude | explore_only | 9 | Possible |
| english_teacher_remote | Mercor - Corporate Finance Expert | exclude | explore_only | 9 | Possible |
| english_teacher_remote | Mercor - Biology Research Scientist | exclude | explore_only | 9 | Possible |
| finance_professional | Alignerr - Accounting & Finance Expert | best_matches | do_these_first | 37 | Strong |
| finance_professional | Alignerr - Finance & Accounting Expert | best_matches | do_these_first | 37 | Strong |
| finance_professional | Mercor - General finance / accounting Evaluator | best_matches | do_these_first | 35 | Strong |
| finance_professional | Meridial - Accounting Specialist - Freelance AI Trainer Project | best_matches | do_these_first | 35 | Strong |
| finance_professional | micro1 - Accountant | best_matches | do_these_first | 35 | Strong |
| finance_professional | micro1 - Tax Professional (EA / CPA) | best_matches | do_these_first | 35 | Strong |
| finance_professional | Alignerr - Financial Analyst AI Researcher - Remote | best_matches | do_these_first | 30 | Medium |
| finance_professional | Mercor - Corporate Finance Expert | best_matches | do_these_first | 30 | Medium |
| finance_professional | Mercor - Investment Banking Expert | best_matches | do_these_first | 30 | Medium |
| finance_professional | DataAnnotation - Finance Expert / Finance AI Trainer | also_worth_reviewing | best_matches | 29 | Medium |
| finance_professional | Mercor - Legal Expert Specialist | exclude | explore_only | 16 | Possible |
| finance_professional | Mercor - Biology Research Scientist | exclude | explore_only | 9 | Possible |
| finance_professional | Turing - Senior Python Developer | exclude | explore_only | 9 | Possible |
| finance_professional | Welocalize - Alpheratz Project - Portuguese (Portugal) Translation Quality Rater | exclude | explore_only | 9 | Possible |
| generalist_no_degree | Alignerr - English Writing Generalist - Advanced | also_worth_reviewing | best_matches | 26 | Medium |
| generalist_no_degree | Alignerr - English Writing Generalist - Quality Review | also_worth_reviewing | best_matches | 26 | Medium |
| generalist_no_degree | Meridial - Search Engine Evaluation Specialist - Freelance AI Trainer Project | also_worth_reviewing | best_matches | 25 | Medium |
| generalist_no_degree | Meridial - Social Media Annotation - Freelance AI Trainer Project | also_worth_reviewing | best_matches | 25 | Medium |
| generalist_no_degree | Mindrift - Freelance Mathematics Expert - AI Trainer | exclude | best_matches | 25 | Medium |
| generalist_no_degree | Mindrift - Material Science Expert with Python - Freelance AI Trainer | exclude | best_matches | 25 | Medium |
| generalist_no_degree | Mindrift - Direct Endorsement (DE) Underwriter - Freelance AI Trainer | exclude | best_matches | 25 | Medium |
| generalist_no_degree | Mindrift - Auto Claims Examiner - Freelance AI Trainer | explore_only | best_matches | 25 | Medium |
| generalist_no_degree | Turing - Senior Python Developer | exclude | explore_only | 9 | Possible |
| generalist_no_degree | micro1 - Legal Expert | exclude | explore_only | 6 | Possible |
| generalist_no_degree | micro1 - Finance Expert | exclude | explore_only | 0 | Possible |
| lawyer | Mercor - IP Law Expert | best_matches | do_these_first | 30 | Medium |
| lawyer | Mercor - Legal Expert - Employment/Labor Law | best_matches | do_these_first | 30 | Medium |
| lawyer | Mercor - Legal Expert Specialist | best_matches | do_these_first | 30 | Medium |
| lawyer | Mercor - Legal Expert - Transactional/Corporate | best_matches | do_these_first | 30 | Medium |
| lawyer | Mercor - Litigation Expert | best_matches | do_these_first | 30 | Medium |
| lawyer | Mercor - Regulatory Law Expert | best_matches | do_these_first | 30 | Medium |
| lawyer | Turing - Contract Law Expert | best_matches | do_these_first | 30 | Medium |
| lawyer | DataAnnotation - Law Expert / Legal AI Trainer | also_worth_reviewing | best_matches | 29 | Medium |
| lawyer | Outlier - English Writing and Content Reviewing Expertise Sought for AI Training | explore_only | best_matches | 22 | Possible |
| lawyer | Mercor - Corporate Finance Expert | exclude | explore_only | 16 | Possible |
| lawyer | Mindrift - Freelance Mathematics Expert - AI Trainer | exclude | explore_only | 16 | Possible |
| lawyer | micro1 - Software Engineer - AI Quality & Testing | exclude | explore_only | 9 | Possible |
| lawyer | Mercor - Biology Research Scientist | exclude | explore_only | 9 | Possible |
| lawyer | Turing - AI Evaluation Engineer (Python / Java / Web) | exclude | explore_only | 9 | Possible |
| multilingual_translator | OneForma - HT (Human Translation) and MTPE (Machine Translation Post Editing) - English (United States) - French (France) | best_matches | do_these_first | 41 | Strong |
| multilingual_translator | Alignerr - Portuguese Localization Expert | best_matches | do_these_first | 35 | Strong |
| multilingual_translator | Alignerr - Spanish Localization Expert | best_matches | do_these_first | 35 | Strong |
| multilingual_translator | Meridial - French Language Specialist - Freelance AI Trainer Project | best_matches | do_these_first | 35 | Strong |
| multilingual_translator | Welocalize - Alpheratz Project - Portuguese (Portugal) Translation Quality Rater | best_matches | do_these_first | 35 | Strong |
| multilingual_translator | OneForma - Adaptation - Portuguese (Brazil) - Portuguese (Portugal) | also_worth_reviewing | do_these_first | 35 | Strong |
| multilingual_translator | OneForma - HT (Human Translation) and MTPE (Machine Translation Post Editing) - English (United States) - Norwegian (Norway) | exclude | best_matches | 27 | Medium |
| multilingual_translator | Welocalize - Alpheratz Project - Danish Translation Quality Rater | exclude | also_worth_reviewing | 21 | Possible |
| multilingual_translator | Welocalize - Alpheratz Project - Korean Translation Quality Rater | exclude | also_worth_reviewing | 21 | Possible |
| multilingual_translator | Turing - Senior Python Developer | exclude | explore_only | 9 | Possible |
| multilingual_translator | Mercor - Legal Expert Specialist | exclude | explore_only | 9 | Possible |
| multilingual_translator | Mercor - Corporate Finance Expert | exclude | explore_only | 9 | Possible |
| phd_history_researcher | Mercor - Market Research Methodologist - Report Quality & Insights Evaluation Expert | explore_only | do_these_first | 34 | Strong |
| phd_history_researcher | Turing - Software Engineer - AI Research & Evaluation (US-based) | exclude | do_these_first | 34 | Strong |
| phd_history_researcher | Outlier - English Writing and Content Reviewing Expertise Sought for AI Training | also_worth_reviewing | do_these_first | 32 | Medium |
| phd_history_researcher | Welocalize - Alpheratz Project - English (UK) Translation Quality Rater | also_worth_reviewing | do_these_first | 30 | Medium |
| phd_history_researcher | Welocalize - French Search & Data Labelling Rater - Morocco | explore_only | do_these_first | 30 | Medium |
| phd_history_researcher | Welocalize - Remote Internet Search Quality Rater - English (United States) | explore_only | do_these_first | 30 | Medium |
| phd_history_researcher | OneForma - Education - Pronunciation Evaluation - English (United Kingdom) | also_worth_reviewing | best_matches | 27 | Medium |
| phd_history_researcher | Mercor - Biology & Biophysics Researchers (India, Part-time) | exclude | best_matches | 26 | Medium |
| phd_history_researcher | Mercor - Human Baseliner for Open-Ended ML Research Tasks | explore_only | best_matches | 26 | Medium |
| phd_history_researcher | Turing - Academic Dermatologist | exclude | also_worth_reviewing | 19 | Possible |
| phd_history_researcher | OneForma - Project Spring - Healthcare Data Improvement Project - English (Ireland) | exclude | explore_only | 15 | Possible |
| phd_history_researcher | Mercor - Corporate Finance Expert | exclude | explore_only | 9 | Possible |
| phd_history_researcher | Turing - Senior Python Developer | exclude | explore_only | 9 | Possible |
| portuguese_english_reviewer | Welocalize - Alpheratz Project - Portuguese (Portugal) Translation Quality Reviewer | best_matches | do_these_first | 43 | Strong |
| portuguese_english_reviewer | OneForma - HT (Human Translation) and MTPE (Machine Translation Post Editing) - English (United States) - Portuguese (Portugal) | best_matches | do_these_first | 41 | Strong |
| portuguese_english_reviewer | Meridial - Language Alignment & Resource Partner (Portuguese) - Freelance AI Trainer Project | best_matches | do_these_first | 35 | Strong |
| portuguese_english_reviewer | Meridial - Portuguese Language Data Contributor (Multimodal) - Freelance AI Trainer Project | also_worth_reviewing | do_these_first | 35 | Strong |
| portuguese_english_reviewer | Meridial - Portuguese Voice Actor - Freelance AI Trainer Project | also_worth_reviewing | do_these_first | 35 | Strong |
| portuguese_english_reviewer | Outlier - Portuguese (Brazil) Freelance Writer | best_matches | do_these_first | 35 | Strong |
| portuguese_english_reviewer | Turing - AI Quality Analyst - Portuguese (Portugal) | best_matches | do_these_first | 35 | Strong |
| portuguese_english_reviewer | Welocalize - Alpheratz Project - Czech Translation Quality Rater | exclude | do_these_first | 33 | Medium |
| portuguese_english_reviewer | Welocalize - Alpheratz Project - Danish Translation Quality Rater | exclude | do_these_first | 33 | Medium |
| portuguese_english_reviewer | Welocalize - Alpheratz Project - Korean Translation Quality Rater | exclude | do_these_first | 33 | Medium |
| portuguese_english_reviewer | OneForma - HT (Human Translation) and MTPE (Machine Translation Post Editing) - English (United States) - Norwegian (Norway) | exclude | best_matches | 23 | Possible |
| portuguese_english_reviewer | Mercor - AI Safety Experts - English & Marathi | exclude | explore_only | 15 | Possible |
| portuguese_english_reviewer | Turing - Senior Python Developer | exclude | explore_only | 9 | Possible |
| software_engineer | Turing - Python + Full-Stack (JS) Developer | best_matches | do_these_first | 31 | Medium |
| software_engineer | Turing - Senior Backend Engineer (Python/FastAPI) - AI Evaluation (US-based) | best_matches | do_these_first | 31 | Medium |
| software_engineer | Turing - Senior Python Developer | best_matches | do_these_first | 31 | Medium |
| software_engineer | micro1 - QA Automation Engineer | also_worth_reviewing | best_matches | 29 | Medium |
| software_engineer | Mercor - AI Safety Experts - English & Marathi | exclude | best_matches | 27 | Medium |
| software_engineer | Mercor - Legal Expert Specialist | exclude | explore_only | 9 | Possible |
| software_engineer | Mercor - Corporate Finance Expert | exclude | explore_only | 9 | Possible |
| software_engineer | Mercor - Biology Research Scientist | exclude | explore_only | 9 | Possible |
| software_engineer | Welocalize - Alpheratz Project - Portuguese (Portugal) Translation Quality Rater | exclude | explore_only | 9 | Possible |

## Recurring Failure Patterns

- `visible_false_positive`: 24
- `exclude_case_visible`: 24
- `professional_domain_mismatch`: 19
- `unsupported_language`: 18
- `unsupported_explicit_language`: 17
- `generic_evaluation_evidence`: 8
- `credential_or_specialty_mismatch`: 6
- `technical_mismatch`: 6
- `generalist_specialist_domain_mismatch`: 5
- `search_inside_research`: 5
- `history_software_research_false_positive`: 1
- `history_science_research_false_positive`: 1
- `generic_research_overpromotion`: 1
- `history_medical_false_positive`: 1
- `history_healthcare_false_positive`: 1

## Live Snapshot For Future Review

These are the current top 10 live matches by benchmark profile. They are not labeled truth rows unless they also appear in the fixture.

### beginner_bilingual_no_degree

| Rank | Source | Title | Score | Label | Reasons | Signals | Contradictions |
|---:|---|---|---:|---|---|---|---|
| 1 | Welocalize | Alpheratz Project - English (United Kingdom) Translation Quality Reviewer | 41 | Strong | Teaching/writing/review signal; Language/translation signal; Search/research quality signal; English language signal | languages: english | - |
| 2 | Welocalize | Alpheratz Project - Spanish (United States) Translation Quality Reviewer | 41 | Strong | Teaching/writing/review signal; Language/translation signal; Search/research quality signal; Spanish language signal | languages: spanish | - |
| 3 | Welocalize | Alpheratz Project - Spanish (United States) Translation Quality Rater | 40 | Strong | Language/translation signal; AI evaluation/training signal; Search/research quality signal; Spanish language signal | languages: spanish | - |
| 4 | OneForma | Acceptability and Preference: Translation Raters - English (United States) – Spanish (Chile) | 39 | Strong | Language/translation signal; AI evaluation/training signal; English language signal; Spanish language signal | languages: english, spanish | - |
| 5 | Outlier | Kiswahili Writing Expertise for AI Training | 36 | Strong | Teaching/writing/review signal; Language/translation signal; AI evaluation/training signal; Remote/flexible signal | languages: kiswahili | unsupported explicit language: kiswahili |
| 6 | Welocalize | Alpheratz Project - Catalan (Spain) Translation Quality Reviewer | 35 | Strong | Teaching/writing/review signal; Language/translation signal; Search/research quality signal; Remote/flexible signal | languages: catalan | unsupported explicit language: catalan |
| 7 | Welocalize | Alpheratz Project - Czech (Czech Republic) Translation Quality Reviewer | 35 | Strong | Teaching/writing/review signal; Language/translation signal; Search/research quality signal; Remote/flexible signal | languages: czech | unsupported explicit language: czech |
| 8 | Welocalize | Alpheratz Project - Danish (Denmark) Translation Quality Reviewer | 35 | Strong | Teaching/writing/review signal; Language/translation signal; Search/research quality signal; Remote/flexible signal | languages: danish | unsupported explicit language: danish |
| 9 | Welocalize | Alpheratz Project - Dutch (Netherlands) Translation Quality Reviewer | 35 | Strong | Teaching/writing/review signal; Language/translation signal; Search/research quality signal; Remote/flexible signal | languages: dutch | unsupported explicit language: dutch |
| 10 | Welocalize | Alpheratz Project - French (Canada) Translation Quality Reviewer | 35 | Strong | Teaching/writing/review signal; Language/translation signal; Search/research quality signal; Remote/flexible signal | languages: french | unsupported explicit language: french |

### biology_or_medicine_academic

| Rank | Source | Title | Score | Label | Reasons | Signals | Contradictions |
|---:|---|---|---:|---|---|---|---|
| 1 | Mercor | Clinical Research Experts (Chemistry and Biology) | 44 | Strong | Biology/life sciences match; Medicine/clinical match; Academic/expert signal; Remote/flexible signal | domains: science/medical; contains both search and research | - |
| 2 | Handshake AI | Biomedical Engineering Expert | 38 | Strong | Biology/life sciences match; Medicine/clinical match; Academic/expert signal; Public inventory opportunity, report separately | domains: science/medical | - |
| 3 | Mercor | Clinical / biomedical / pharma Evaluator | 37 | Strong | Biology/life sciences match; Medicine/clinical match; Remote/flexible signal; Live/countable opportunity | domains: science/medical | - |
| 4 | Meridial | Microbiology Specialist - Freelance AI Trainer Project | 37 | Strong | Biology/life sciences match; Medicine/clinical match; Remote/flexible signal; Live/countable opportunity | - | - |
| 5 | Meridial | Synthetic Biology Specialist - Freelance AI Trainer Project | 37 | Strong | Biology/life sciences match; Medicine/clinical match; Remote/flexible signal; Live/countable opportunity | domains: science/medical | - |
| 6 | Mindrift | Biology & Python Expert - Freelance AI Trainer | 34 | Strong | Biology/life sciences match; Academic/expert signal; Python + science task match; Remote/flexible signal | domains: technical, science/medical | - |
| 7 | Mindrift | Biology Expert with Python Experience - AI Projects on Mindrift | 34 | Strong | Biology/life sciences match; Academic/expert signal; Python + science task match; Remote/flexible signal | domains: technical, science/medical | - |
| 8 | Mercor | Biology Research Scientist (BA, MS, PhD's) | 30 | Medium | Biology/life sciences match; Academic/expert signal; Remote/flexible signal; Live/countable opportunity | domains: science/medical; contains both search and research | - |
| 9 | Mercor | Pharmacokinetics & Systems Biology Expert | 30 | Medium | Biology/life sciences match; Academic/expert signal; Remote/flexible signal; Live/countable opportunity | domains: science/medical | - |
| 10 | Turing | Academic Dermatologist – Senior Clinical Reviewer | 30 | Medium | Medicine/clinical match; Academic/expert signal; Remote/flexible signal; Live/countable opportunity | domains: science/medical | - |

### english_teacher_remote

| Rank | Source | Title | Score | Label | Reasons | Signals | Contradictions |
|---:|---|---|---:|---|---|---|---|
| 1 | Outlier | English Writing and Content Reviewing Expertise Sought for AI Training | 44 | Strong | Teaching/writing/review signal; English writing/content review signal; AI evaluation/training signal; English language signal | languages: english | - |
| 2 | Outlier | English Writing and Content Reviewing Expertise Sought for AI Training | 44 | Strong | Teaching/writing/review signal; English writing/content review signal; AI evaluation/training signal; English language signal | languages: english | - |
| 3 | OneForma | LELI – Review & Refine Machine Translations in Your Language - Chinese Simplified → English (US) | 34 | Strong | Teaching/writing/review signal; Language/translation signal; English language signal; Remote/flexible signal | languages: chinese, english | unsupported explicit language: chinese |
| 4 | Welocalize | Alpheratz Project - English (United Kingdom) Translation Quality Reviewer | 34 | Strong | Teaching/writing/review signal; Language/translation signal; English language signal; Remote/flexible signal | languages: english | - |
| 5 | OneForma | Acceptability and Preference: Translation Raters - English (US) to Norwegian (Norway) | 33 | Medium | Language/translation signal; AI evaluation/training signal; English language signal; Remote/flexible signal | languages: english, norwegian | unsupported explicit language: norwegian |
| 6 | Turing | Audio/Voice/Annotation Trainer - English (US) Language | 33 | Medium | Language/translation signal; AI evaluation/training signal; English language signal; Remote/flexible signal | languages: english | - |
| 7 | Alignerr | English Writing Generalist | 31 | Medium | Teaching/writing/review signal; English writing/content review signal; English language signal; Live/countable opportunity | languages: english | - |
| 8 | Alignerr | English Writing Generalist – Advanced | 31 | Medium | Teaching/writing/review signal; English writing/content review signal; English language signal; Live/countable opportunity | languages: english | - |
| 9 | Alignerr | English Writing Generalist – Quality Review | 31 | Medium | Teaching/writing/review signal; English writing/content review signal; English language signal; Live/countable opportunity | languages: english | - |
| 10 | Welocalize | English (United States) <> French (France) Lyric Translation Reviewer | 29 | Medium | Teaching/writing/review signal; Language/translation signal; English language signal; Live/countable opportunity | languages: english, french | unsupported explicit language: french |

### finance_professional

| Rank | Source | Title | Score | Label | Reasons | Signals | Contradictions |
|---:|---|---|---:|---|---|---|---|
| 1 | micro1 | US Tax Law Analyst (EA/CA) | 42 | Strong | Finance domain match; Accounting domain match; Expert review signal; Remote/flexible signal | domains: legal, finance | - |
| 2 | DataAnnotation | Accounting Expert / Accounting AI Trainer | 41 | Strong | Finance domain match; Accounting domain match; Expert review signal; Remote/flexible signal | domains: finance | - |
| 3 | Alignerr | Accounting & Finance Expert | 37 | Strong | Finance domain match; Accounting domain match; Expert review signal; Live/countable opportunity | domains: finance | - |
| 4 | Alignerr | Finance & Accounting Expert | 37 | Strong | Finance domain match; Accounting domain match; Expert review signal; Live/countable opportunity | domains: finance | - |
| 5 | Mercor | General finance / accounting Evaluator | 35 | Strong | Finance domain match; Accounting domain match; Remote/flexible signal; Live/countable opportunity | domains: finance | - |
| 6 | Meridial | Accounting Specialist - Freelance AI Trainer Project | 35 | Strong | Finance domain match; Accounting domain match; Remote/flexible signal; Live/countable opportunity | domains: finance | - |
| 7 | micro1 | Accountant | 35 | Strong | Finance domain match; Accounting domain match; Remote/flexible signal; Live/countable opportunity | domains: finance | - |
| 8 | micro1 | Tax Professional (EA / CPA) | 35 | Strong | Finance domain match; Accounting domain match; Remote/flexible signal; Live/countable opportunity | domains: finance | - |
| 9 | Alignerr | Finance & Accounting Specialist | 30 | Medium | Finance domain match; Accounting domain match; Live/countable opportunity | domains: finance | - |
| 10 | Alignerr | Financial Analyst AI Researcher - Remote | 30 | Medium | Finance domain match; Expert review signal; Remote/flexible signal; Live/countable opportunity | domains: finance; contains both search and research | - |

### generalist_no_degree

| Rank | Source | Title | Score | Label | Reasons | Signals | Contradictions |
|---:|---|---|---:|---|---|---|---|
| 1 | Outlier | English Writing and Content Reviewing Expertise Sought for AI Training | 31 | Medium | Generalist AI-work signal; Writing/review signal; English language signal; Remote/flexible signal | languages: english | - |
| 2 | Outlier | English Writing and Content Reviewing Expertise Sought for AI Training | 31 | Medium | Generalist AI-work signal; Writing/review signal; English language signal; Remote/flexible signal | languages: english | - |
| 3 | Welocalize | Remote Internet Search Quality Rater - English (United States) | 29 | Medium | Research/search evaluation signal; Data annotation signal; English language signal; Remote/flexible signal | languages: english | - |
| 4 | Alignerr | English Writing Generalist | 26 | Medium | Generalist AI-work signal; Writing/review signal; English language signal; Live/countable opportunity | languages: english | - |
| 5 | Alignerr | English Writing Generalist – Advanced | 26 | Medium | Generalist AI-work signal; Writing/review signal; English language signal; Live/countable opportunity | languages: english | - |
| 6 | Alignerr | English Writing Generalist – Quality Review | 26 | Medium | Generalist AI-work signal; Writing/review signal; English language signal; Live/countable opportunity | languages: english | - |
| 7 | Meridial | Pavement Condition Index (PCI) Survey & Annotation Specialist – Freelance AI Trainer Project | 25 | Medium | Generalist AI-work signal; Data annotation signal; Remote/flexible signal; Live/countable opportunity | - | - |
| 8 | Meridial | Search Engine Evaluation Specialist - Freelance AI Trainer Project | 25 | Medium | Generalist AI-work signal; Research/search evaluation signal; Remote/flexible signal; Live/countable opportunity | - | - |
| 9 | Meridial | Social Media Annotation - Freelance AI Trainer Project | 25 | Medium | Generalist AI-work signal; Data annotation signal; Remote/flexible signal; Live/countable opportunity | - | - |
| 10 | Mindrift | Auto Claims Examiner - Freelance AI Trainer | 25 | Medium | Generalist AI-work signal; Writing/review signal; Remote/flexible signal; Live/countable opportunity | - | - |

### lawyer

| Rank | Source | Title | Score | Label | Reasons | Signals | Contradictions |
|---:|---|---|---:|---|---|---|---|
| 1 | Mercor | Attorney / Legal Expert (Real Estate / Energy) - AI Training Project | 30 | Medium | Legal domain match; Expert review signal; Remote/flexible signal; Live/countable opportunity | domains: legal | - |
| 2 | Mercor | IP Expert — Patent / Trademark | 30 | Medium | Legal domain match; Expert review signal; Remote/flexible signal; Live/countable opportunity | domains: legal | - |
| 3 | Mercor | IP Law Expert | 30 | Medium | Legal domain match; Expert review signal; Remote/flexible signal; Live/countable opportunity | domains: legal | - |
| 4 | Mercor | Legal Expert — Employment / Labor | 30 | Medium | Legal domain match; Expert review signal; Remote/flexible signal; Live/countable opportunity | domains: legal | - |
| 5 | Mercor | Legal Expert — Specialist (Real Estate, Tax, Bankruptcy, Estates) | 30 | Medium | Legal domain match; Expert review signal; Remote/flexible signal; Live/countable opportunity | domains: legal, finance | - |
| 6 | Mercor | Legal Expert — Transactional / Corporate | 30 | Medium | Legal domain match; Expert review signal; Remote/flexible signal; Live/countable opportunity | domains: legal | - |
| 7 | Mercor | Legal Technology Expert | 30 | Medium | Legal domain match; Expert review signal; Remote/flexible signal; Live/countable opportunity | domains: legal | - |
| 8 | Mercor | Litigation Expert | 30 | Medium | Legal domain match; Expert review signal; Remote/flexible signal; Live/countable opportunity | domains: legal | - |
| 9 | Mercor | Regulatory Compliance & Risk Management Expert | 30 | Medium | Legal domain match; Expert review signal; Remote/flexible signal; Live/countable opportunity | domains: legal | - |
| 10 | Mercor | Regulatory Law Expert | 30 | Medium | Legal domain match; Expert review signal; Remote/flexible signal; Live/countable opportunity | domains: legal | - |

### multilingual_translator

| Rank | Source | Title | Score | Label | Reasons | Signals | Contradictions |
|---:|---|---|---:|---|---|---|---|
| 1 | OneForma | LELI – Review & Refine Machine Translations in Your Language - French (Belgium) | 47 | Strong | Language/linguistics match; Translation/localization match; French language match; French language signal | languages: french | - |
| 2 | Alignerr | Portuguese Localization Specialist | 42 | Strong | Language/linguistics match; Translation/localization match; Portuguese language match; Portuguese language signal | languages: portuguese | - |
| 3 | Alignerr | Spanish Localization Specialist | 42 | Strong | Language/linguistics match; Translation/localization match; Spanish language match; Spanish language signal | languages: spanish | - |
| 4 | OneForma | Acceptability and Preference: Translation Raters - English (United States) – French (Belgium) | 41 | Strong | Translation/localization match; French language match; English language signal; French language signal | languages: english, french | - |
| 5 | OneForma | HT (Human Translation) and MTPE (Machine Translation Post Editing) - English (United States) - French (Switzerland) | 41 | Strong | Translation/localization match; French language match; English language signal; French language signal | languages: english, french | - |
| 6 | Welocalize | English (United States) <> French (France) Lyric Translation Reviewer | 36 | Strong | Translation/localization match; French language match; English language signal; French language signal | languages: english, french | - |
| 7 | Mercor | Internet-Native Bilingual Evaluator Expert (Spanish – Spain) | 35 | Strong | Language/linguistics match; Spanish language match; Spanish language signal; Remote/flexible signal | languages: spanish | - |
| 8 | Mercor | Spanish (Mexico) Audio Generalist Evaluator Expert | 35 | Strong | Language/linguistics match; Spanish language match; Spanish language signal; Remote/flexible signal | languages: spanish | - |
| 9 | Mercor | Spanish (Spain) Audio Generalist Evaluator Expert | 35 | Strong | Language/linguistics match; Spanish language match; Spanish language signal; Remote/flexible signal | languages: spanish | - |
| 10 | Mercor | Voice Actor: CX Agent Voice Cloning (Canadian French) | 35 | Strong | Language/linguistics match; French language match; French language signal; Remote/flexible signal | languages: french | - |

### phd_history_researcher

| Rank | Source | Title | Score | Label | Reasons | Signals | Contradictions |
|---:|---|---|---:|---|---|---|---|
| 1 | Mercor | Market Research Methodologist – Report Quality & Insights Evaluation Expert | 34 | Strong | Research/humanities signal; AI evaluation/training signal; Search/research quality signal; Remote/flexible signal | domains: science/medical; contains both search and research | domain mismatch for humanities research profile |
| 2 | Turing | Software Engineer – AI Research & Evaluation (US-based) | 34 | Strong | Research/humanities signal; AI evaluation/training signal; Search/research quality signal; Remote/flexible signal | domains: technical; contains both search and research | domain mismatch for humanities research profile |
| 3 | Outlier | English Writing and Content Reviewing Expertise Sought for AI Training | 32 | Medium | Teaching/writing/review signal; AI evaluation/training signal; English language signal; Remote/flexible signal | languages: english | - |
| 4 | Outlier | English Writing and Content Reviewing Expertise Sought for AI Training | 32 | Medium | Teaching/writing/review signal; AI evaluation/training signal; English language signal; Remote/flexible signal | languages: english | - |
| 5 | Welocalize | Alpheratz Project - English (United Kingdom) Translation Quality Reviewer | 31 | Medium | Teaching/writing/review signal; Search/research quality signal; English language signal; Remote/flexible signal | languages: english | - |
| 6 | Welocalize | Alpheratz Project - French (Canada) Translation Quality Reviewer | 31 | Medium | Teaching/writing/review signal; Search/research quality signal; French language signal; Remote/flexible signal | languages: french | - |
| 7 | OneForma | Project Spring – Help Build High-Quality Multilingual Healthcare Data - English - Ireland | 30 | Medium | AI evaluation/training signal; Search/research quality signal; English language signal; Remote/flexible signal | languages: english | - |
| 8 | Welocalize | Alpheratz Project - French (Canada) Translation Quality Rater | 30 | Medium | AI evaluation/training signal; Search/research quality signal; French language signal; Remote/flexible signal | languages: french | - |
| 9 | Welocalize | French Search & Data Labelling Rater - Project Andromeda Titawin | 30 | Medium | AI evaluation/training signal; Search/research quality signal; French language signal; Remote/flexible signal | languages: french | - |
| 10 | Welocalize | Remote Internet Search Quality Rater - English (United States) | 30 | Medium | AI evaluation/training signal; Search/research quality signal; English language signal; Remote/flexible signal | languages: english | - |

### portuguese_english_reviewer

| Rank | Source | Title | Score | Label | Reasons | Signals | Contradictions |
|---:|---|---|---:|---|---|---|---|
| 1 | Welocalize | Alpheratz Project - Portuguese (Portugal) Translation Quality Rater | 51 | Strong | Portuguese language match; Language review signal; AI evaluation/reviewer signal; Search quality signal | languages: portuguese | - |
| 2 | Meridial | Portuguese Audio Evaluations Specialist - Freelance AI Trainer Project | 43 | Strong | Portuguese language match; Language review signal; AI evaluation/reviewer signal; Portuguese language signal | languages: portuguese | - |
| 3 | Welocalize | Alpheratz Project - Portuguese (Portugal) Translation Quality Reviewer | 43 | Strong | Portuguese language match; Language review signal; AI evaluation/reviewer signal; Portuguese language signal | languages: portuguese | - |
| 4 | OneForma | HT (Human Translation) and MTPE (Machine Translation Post Editing) - English (United States) - Portuguese (Portugal) | 41 | Strong | Portuguese language match; Language review signal; Portuguese language signal; English language signal | languages: english, portuguese | - |
| 5 | Meridial | Language Alignment & Resource Partner (Portuguese) - Freelance AI Trainer Project | 35 | Strong | Portuguese language match; Language review signal; Portuguese language signal; Remote/flexible signal | languages: portuguese | - |
| 6 | Meridial | Portuguese Language Data Contributor (Multimodal) – Freelance AI Trainer Project | 35 | Strong | Portuguese language match; Language review signal; Portuguese language signal; Remote/flexible signal | languages: portuguese | - |
| 7 | Meridial | Portuguese Language Specialist (Brazil) - Freelance AI Trainer Project | 35 | Strong | Portuguese language match; Language review signal; Portuguese language signal; Remote/flexible signal | languages: portuguese | - |
| 8 | Meridial | Portuguese Language Specialist (Portugal) - Freelance AI Trainer Project | 35 | Strong | Portuguese language match; Language review signal; Portuguese language signal; Remote/flexible signal | languages: portuguese | - |
| 9 | Meridial | Portuguese Voice Actor - Freelance AI Trainer Project | 35 | Strong | Portuguese language match; Language review signal; Portuguese language signal; Remote/flexible signal | languages: portuguese | - |
| 10 | OneForma | Adaptation - Portuguese (Brazil) - Portuguese (Portugal) | 35 | Strong | Portuguese language match; Language review signal; Portuguese language signal; Remote/flexible signal | languages: portuguese | - |

### software_engineer

| Rank | Source | Title | Score | Label | Reasons | Signals | Contradictions |
|---:|---|---|---:|---|---|---|---|
| 1 | micro1 | Software Engineer - AI Quality & Testing | 37 | Strong | Coding/technical task match; Technical review/evaluation signal; Benchmark/model-evaluation signal; Remote/flexible signal | domains: technical | - |
| 2 | Meridial | Python Coding Specialist - Freelance AI Trainer Project | 31 | Medium | Coding/technical task match; Python match; Remote/flexible signal; Live/countable opportunity | domains: technical | - |
| 3 | Turing | Python + Full-Stack (JS) Developer | 31 | Medium | Coding/technical task match; Python match; Remote/flexible signal; Live/countable opportunity | domains: technical | - |
| 4 | Turing | Scientific Coding - Biology and Python | 31 | Medium | Coding/technical task match; Python match; Remote/flexible signal; Live/countable opportunity | domains: technical, science/medical | - |
| 5 | Turing | Senior Backend Engineer (Python/FastAPI) – AI Evaluation (US-based) | 31 | Medium | Coding/technical task match; Python match; Remote/flexible signal; Live/countable opportunity | domains: technical | - |
| 6 | Turing | Senior Python Developer | 31 | Medium | Coding/technical task match; Python match; Remote/flexible signal; Live/countable opportunity | domains: technical | - |
| 7 | Turing | Senior Python Engineer – LLM Evaluation (US-based) | 31 | Medium | Coding/technical task match; Python match; Remote/flexible signal; Live/countable opportunity | domains: technical | - |
| 8 | Turing | Senior Software Engineer – Python (LLM Evaluation & Repository Validation) | 31 | Medium | Coding/technical task match; Python match; Remote/flexible signal; Live/countable opportunity | domains: technical | - |
| 9 | micro1 | Fullstack Engineer (Python+React) | 31 | Medium | Coding/technical task match; Python match; Remote/flexible signal; Live/countable opportunity | domains: technical | - |
| 10 | micro1 | Python Developer | 31 | Medium | Coding/technical task match; Python match; Remote/flexible signal; Live/countable opportunity | domains: technical | - |

## Recommended Next Deterministic Gate Work

- Add language eligibility gates so unsupported explicit-language roles do not rank for narrow language profiles.
- Separate `search` from `research` evidence so humanities research profiles are not promoted into search-rater roles by substring matches.
- Add broad domain mismatch gates for software, legal, finance, science/medical, and generalist profiles.
- Keep this benchmark as a baseline before changing production scoring.

## Methodology Notes

- `strong` and `plausible` count as relevant for fixture-pool relevant precision/recall.
- Only `strong` counts as relevant for strict fixture-pool precision.
- `review_required: true` cases are visible in review files but excluded from headline metrics.
- Stored fixture snapshots are used only when the live DB row cannot be resolved.

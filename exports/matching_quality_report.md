# Matching Quality Benchmark Baseline

Generated: 2026-06-22T02:50:18.610874+00:00

## Scope

This report evaluates the current production profile matcher against a draft fixture pool. It does not change scoring, thresholds, planner logic, crawlers, schema, product-state data, or live market estimates.

All fixture labels are `codex_draft` labels. They are proposed baseline judgments for review, not final human-approved truth.

Precision, recall, and false-positive metrics below are fixture-pool metrics only. They are not universal production accuracy estimates.

## Fixture Summary

- Fixture: `tests/fixtures/matching_golden_set.json`
- Baseline matcher commit: `0613bc12fb91b447ad5241f96ed7a8a619266724`
- Total cases: 160
- Headline metric cases: 151
- Review-required cases excluded from headline metrics: 9
- DB resolution: live_db_url=89, fixture_snapshot=55, live_db_title=16
- Label distribution, all cases: strong=61, false_positive=58, plausible=30, weak=11
- Label distribution, headline cases: strong=59, false_positive=58, plausible=26, weak=8

## Apples-To-Apples Matcher Comparison

Previous and current matchers are evaluated against the same fixture pool with the same metric definitions. Surfacing metrics account for personalized-section eligibility; classification metrics use raw scores.

| Metric | Previous HEAD | Current Working Tree |
|---|---:|---:|
| Surfacing false positives | 24 | 0 |
| Classification false positives | 20 | 6 |
| Visible false negatives | 6 | 7 |
| Explore-only false positives | 34 | 58 |

### Precision Before / After By Profile

| Profile | P@4 Previous | P@4 Current | P@10 Previous | P@10 Current | Strict P@4 Previous | Strict P@4 Current | Strict P@10 Previous | Strict P@10 Current |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| beginner_bilingual_no_degree | 75% | 50% | 50% | 50% | 50% | 25% | 20% | 20% |
| biology_or_medicine_academic | 100% | 100% | 100% | 100% | 100% | 100% | 80% | 80% |
| english_teacher_remote | 100% | 100% | 70% | 70% | 50% | 50% | 50% | 50% |
| finance_professional | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| generalist_no_degree | 100% | 100% | 70% | 80% | 75% | 75% | 30% | 30% |
| lawyer | 100% | 100% | 90% | 100% | 100% | 100% | 80% | 90% |
| multilingual_translator | 100% | 100% | 70% | 70% | 100% | 100% | 60% | 60% |
| phd_history_researcher | 50% | 50% | 40% | 50% | 0% | 0% | 10% | 10% |
| portuguese_english_reviewer | 100% | 100% | 90% | 90% | 100% | 100% | 70% | 70% |
| software_engineer | 100% | 100% | 90% | 90% | 100% | 100% | 70% | 70% |

## Fixture-Pool Metrics By Profile

| Profile | Cases | Relevant P@4 | Relevant P@10 | Strict P@4 | Strict P@10 | FP@10 | Relevant Recall | Surfacing FP | Classification FP | Visible FN |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| beginner_bilingual_no_degree | 15 | 50% | 50% | 25% | 20% | 50% | 86% | 0 | 5 | 1 |
| biology_or_medicine_academic | 16 | 100% | 100% | 100% | 80% | 0% | 82% | 0 | 0 | 2 |
| english_teacher_remote | 16 | 100% | 70% | 50% | 50% | 30% | 88% | 0 | 1 | 1 |
| finance_professional | 15 | 100% | 100% | 100% | 100% | 0% | 100% | 0 | 0 | 0 |
| generalist_no_degree | 15 | 100% | 80% | 75% | 30% | 10% | 88% | 0 | 0 | 1 |
| lawyer | 16 | 100% | 100% | 100% | 90% | 0% | 90% | 0 | 0 | 1 |
| multilingual_translator | 13 | 100% | 70% | 100% | 60% | 30% | 100% | 0 | 0 | 0 |
| phd_history_researcher | 15 | 50% | 50% | 0% | 10% | 10% | 80% | 0 | 0 | 1 |
| portuguese_english_reviewer | 15 | 100% | 90% | 100% | 70% | 10% | 100% | 0 | 0 | 0 |
| software_engineer | 15 | 100% | 90% | 100% | 70% | 10% | 100% | 0 | 0 | 0 |

## Surfacing False Positives

No false-positive fixture cases reached personalized sections.

## Classification False Positives

### beginner_bilingual_no_degree - beginner_bilingual_no_degree_010

- Opportunity: Welocalize - Alpheratz Project - Catalan Translation Quality Rater
- Expected: `false_positive` / `exclude`
- Current: score 34, `Strong`, `explore_only`
- Personalized eligibility: no (Explicit language requirement is not listed on profile.)
- Rationale: Catalan is unsupported.
- Regression rule: `unsupported_explicit_language`
- Failure patterns: `unsupported_language`, `unsupported_explicit_language`
- Positive contributions: +10 Language/translation signal: translation, +8 AI evaluation/training signal: rater, +7 Search/research quality signal: quality, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Language/translation signal, AI evaluation/training signal, Search/research quality signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: unsupported explicit language: catalan

### beginner_bilingual_no_degree - beginner_bilingual_no_degree_011

- Opportunity: Welocalize - Alpheratz Project - Czech Translation Quality Rater
- Expected: `false_positive` / `exclude`
- Current: score 34, `Strong`, `explore_only`
- Personalized eligibility: no (Explicit language requirement is not listed on profile.)
- Rationale: Czech is unsupported.
- Regression rule: `unsupported_explicit_language`
- Failure patterns: `unsupported_language`, `unsupported_explicit_language`
- Positive contributions: +10 Language/translation signal: translation, +8 AI evaluation/training signal: rater, +7 Search/research quality signal: quality, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Language/translation signal, AI evaluation/training signal, Search/research quality signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: unsupported explicit language: czech

### beginner_bilingual_no_degree - beginner_bilingual_no_degree_012

- Opportunity: Welocalize - Alpheratz Project - Danish Translation Quality Rater
- Expected: `false_positive` / `exclude`
- Current: score 34, `Strong`, `explore_only`
- Personalized eligibility: no (Explicit language requirement is not listed on profile.)
- Rationale: Danish is unsupported.
- Regression rule: `unsupported_explicit_language`
- Failure patterns: `unsupported_language`, `unsupported_explicit_language`
- Positive contributions: +10 Language/translation signal: translation, +8 AI evaluation/training signal: rater, +7 Search/research quality signal: quality, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Language/translation signal, AI evaluation/training signal, Search/research quality signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: unsupported explicit language: danish

### beginner_bilingual_no_degree - beginner_bilingual_no_degree_013

- Opportunity: Welocalize - Alpheratz Project - Dutch Translation Quality Rater
- Expected: `false_positive` / `exclude`
- Current: score 34, `Strong`, `explore_only`
- Personalized eligibility: no (Explicit language requirement is not listed on profile.)
- Rationale: Dutch is unsupported.
- Regression rule: `unsupported_explicit_language`
- Failure patterns: `unsupported_language`, `unsupported_explicit_language`
- Positive contributions: +10 Language/translation signal: translation, +8 AI evaluation/training signal: rater, +7 Search/research quality signal: quality, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Language/translation signal, AI evaluation/training signal, Search/research quality signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: unsupported explicit language: dutch

### beginner_bilingual_no_degree - beginner_bilingual_no_degree_014

- Opportunity: Welocalize - Alpheratz Project - Korean Translation Quality Rater
- Expected: `false_positive` / `exclude`
- Current: score 34, `Strong`, `explore_only`
- Personalized eligibility: no (Explicit language requirement is not listed on profile.)
- Rationale: Korean is unsupported.
- Regression rule: `unsupported_explicit_language`
- Failure patterns: `unsupported_language`, `unsupported_explicit_language`
- Positive contributions: +10 Language/translation signal: translation, +8 AI evaluation/training signal: rater, +7 Search/research quality signal: quality, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Language/translation signal, AI evaluation/training signal, Search/research quality signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: unsupported explicit language: korean

### english_teacher_remote - english_teacher_remote_013

- Opportunity: Outlier - Polish STEM Writing Expertise Sought for AI Training
- Expected: `false_positive` / `exclude`
- Current: score 26, `Medium`, `explore_only`
- Personalized eligibility: no (Explicit language requirement is not listed on profile.)
- Rationale: Polish and STEM requirements are unsupported.
- Regression rule: `unsupported_explicit_language`
- Failure patterns: `generic_evaluation_evidence`, `unsupported_language`, `unsupported_explicit_language`
- Positive contributions: +9 Teaching/writing/review signal: writing, +8 AI evaluation/training signal: ai training, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Teaching/writing/review signal, AI evaluation/training signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: unsupported explicit language: polish


## Visible False Negatives

### beginner_bilingual_no_degree - beginner_bilingual_no_degree_006

- Opportunity: Meridial - Social Media Annotation - Freelance AI Trainer Project
- Expected: `plausible` / `also_worth_reviewing`
- Current: score 17, `Possible`, `explore_only`
- Personalized eligibility: yes (No explicit language requirement detected.)
- Rationale: Annotation work could fit a beginner profile.
- Regression rule: `-`
- Failure patterns: -
- Positive contributions: +8 AI evaluation/training signal: annotation, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: AI evaluation/training signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: -

### biology_or_medicine_academic - biology_or_medicine_academic_003

- Opportunity: Mercor - Microbiology Specialist
- Expected: `strong` / `best_matches`
- Current: score 9, `Possible`, `explore_only`
- Personalized eligibility: yes (No explicit language requirement detected.)
- Rationale: Microbiology is a direct science match.
- Regression rule: `-`
- Failure patterns: -
- Positive contributions: +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Remote/flexible signal, Live/countable opportunity
- Contradictions: -

### biology_or_medicine_academic - biology_or_medicine_academic_007

- Opportunity: Turing - Academic Dermatologist
- Expected: `strong` / `best_matches`
- Current: score 16, `Possible`, `explore_only`
- Personalized eligibility: yes (No explicit language requirement detected.)
- Rationale: Medical academic domain is relevant.
- Regression rule: `-`
- Failure patterns: -
- Positive contributions: +7 Academic/expert signal: academic, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Academic/expert signal, Remote/flexible signal, Live/countable opportunity
- Contradictions: -

### english_teacher_remote - english_teacher_remote_008

- Opportunity: DataAnnotation - Generalist AI Trainer
- Expected: `plausible` / `also_worth_reviewing`
- Current: score 8, `Possible`, `explore_only`
- Personalized eligibility: yes (No explicit language requirement detected.)
- Rationale: Evergreen generalist application can fit English teaching skills.
- Regression rule: `-`
- Failure patterns: -
- Positive contributions: +5 Remote/flexible signal, +2 Reported separately opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Remote/flexible signal, Evergreen application, useful but not counted in live estimate
- Contradictions: -

### generalist_no_degree - generalist_no_degree_008

- Opportunity: DataAnnotation - Generalist AI Trainer
- Expected: `plausible` / `also_worth_reviewing`
- Current: score 17, `Possible`, `explore_only`
- Personalized eligibility: yes (No explicit language requirement detected.)
- Rationale: Evergreen generalist application is useful but not a live task feed.
- Regression rule: `-`
- Failure patterns: -
- Positive contributions: +9 Generalist AI-work signal: generalist, ai trainer, +5 Remote/flexible signal, +2 Reported separately opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Generalist AI-work signal, Remote/flexible signal, Evergreen application, useful but not counted in live estimate
- Contradictions: -

### lawyer - lawyer_002

- Opportunity: Mercor - IP Expert
- Expected: `strong` / `best_matches`
- Current: score 6, `Possible`, `explore_only`
- Personalized eligibility: yes (No explicit language requirement detected.)
- Rationale: Intellectual property expertise is legal-domain work.
- Regression rule: `-`
- Failure patterns: -
- Positive contributions: +7 Expert review signal: expert, +5 Remote/flexible signal, +3 Live/countable opportunity, +1 Non-experimental source
- Penalties: -10 Match is based mostly on generic AI-work terms
- User-facing reasons: Expert review signal, Remote/flexible signal, Live/countable opportunity, Match is based mostly on generic AI-work terms
- Contradictions: -

### phd_history_researcher - phd_history_researcher_005

- Opportunity: DataAnnotation - Generalist AI Trainer
- Expected: `plausible` / `also_worth_reviewing`
- Current: score 8, `Possible`, `explore_only`
- Personalized eligibility: yes (No explicit language requirement detected.)
- Rationale: Evergreen generalist AI training may fit academic writing/research skills.
- Regression rule: `-`
- Failure patterns: -
- Positive contributions: +5 Remote/flexible signal, +2 Reported separately opportunity, +1 Non-experimental source
- Penalties: -
- User-facing reasons: Remote/flexible signal, Evergreen application, useful but not counted in live estimate
- Contradictions: -


## Language Eligibility Diagnostics

- Recognized canonical languages: afrikaans, albanian, amharic, arabic, armenian, azerbaijani, bengali, bosnian, bulgarian, burmese, catalan, chinese, croatian, czech, danish, dutch, english, estonian, finnish, french, georgian, german, greek, gujarati, hebrew, hindi, hungarian, icelandic, indonesian, italian, japanese, kannada, kazakh, khmer, kinyarwanda, kiswahili, korean, lao, latvian, lithuanian, macedonian, malay, malayalam, marathi, mongolian, nepali, norwegian, pashto, persian, polish, portuguese, punjabi, romanian, russian, serbian, sinhala, slovak, slovenian, spanish, swedish, tagalog, tamil, telugu, thai, turkish, ukrainian, urdu, uzbek, vietnamese, welsh
- Explicit languages observed in active opportunities: english=394, french=202, german=188, spanish=159, chinese=144, arabic=119, italian=104, portuguese=96, korean=75, japanese=71, hindi=66, dutch=58, swedish=45, vietnamese=44, russian=40, finnish=35, danish=32, thai=31, indonesian=30, hebrew=29, turkish=26, bengali=25, norwegian=25, polish=25, tamil=24, marathi=23, czech=22, telugu=20, malay=20, kannada=19, urdu=18, malayalam=17, ukrainian=17, catalan=16, hungarian=14, romanian=14, tagalog=13, greek=13, gujarati=13, bulgarian=13, slovak=13, punjabi=12, estonian=12, icelandic=12, slovenian=12, croatian=11, latvian=8, persian=8, lithuanian=7, serbian=7, uzbek=7, welsh=7, armenian=6, khmer=5, afrikaans=4, albanian=4, azerbaijani=4, nepali=4, lao=3, burmese=3, amharic=3, georgian=3, kazakh=3, kinyarwanda=3, sinhala=3, mongolian=3, kiswahili=3, bosnian=2, macedonian=2, pashto=2
- Profile/opportunity pairs excluded by explicit-language eligibility: 17106
- Potential unrecognized language tokens: none detected by the heuristic.
- Affected profiles: biology_or_medicine_academic=1793, english_teacher_remote=1793, finance_professional=1793, generalist_no_degree=1793, lawyer=1793, software_engineer=1793, portuguese_english_reviewer=1712, beginner_bilingual_no_degree=1648, phd_history_researcher=1609, multilingual_translator=1379

| Profile | Source | Title | Detected Languages | Reason |
|---|---|---|---|---|
| beginner_bilingual_no_degree | Alignerr | AI Language Expert - Chinese | chinese | Explicit language requirement is not listed on profile. |
| biology_or_medicine_academic | Alignerr | AI Language Expert - Chinese | chinese | Explicit language requirement is not listed on profile. |
| english_teacher_remote | Alignerr | AI Language Expert - Chinese | chinese | Explicit language requirement is not listed on profile. |
| finance_professional | Alignerr | AI Language Expert - Chinese | chinese | Explicit language requirement is not listed on profile. |
| generalist_no_degree | Alignerr | AI Language Expert - Chinese | chinese | Explicit language requirement is not listed on profile. |
| lawyer | Alignerr | AI Language Expert - Chinese | chinese | Explicit language requirement is not listed on profile. |
| multilingual_translator | Alignerr | AI Language Expert - Chinese | chinese | Explicit language requirement is not listed on profile. |
| phd_history_researcher | Alignerr | AI Language Expert - Chinese | chinese | Explicit language requirement is not listed on profile. |
| portuguese_english_reviewer | Alignerr | AI Language Expert - Chinese | chinese | Explicit language requirement is not listed on profile. |
| software_engineer | Alignerr | AI Language Expert - Chinese | chinese | Explicit language requirement is not listed on profile. |
| beginner_bilingual_no_degree | Alignerr | AI Language Expert - Chinese | chinese | Explicit language requirement is not listed on profile. |
| biology_or_medicine_academic | Alignerr | AI Language Expert - Chinese | chinese | Explicit language requirement is not listed on profile. |
| english_teacher_remote | Alignerr | AI Language Expert - Chinese | chinese | Explicit language requirement is not listed on profile. |
| finance_professional | Alignerr | AI Language Expert - Chinese | chinese | Explicit language requirement is not listed on profile. |
| generalist_no_degree | Alignerr | AI Language Expert - Chinese | chinese | Explicit language requirement is not listed on profile. |
| lawyer | Alignerr | AI Language Expert - Chinese | chinese | Explicit language requirement is not listed on profile. |
| multilingual_translator | Alignerr | AI Language Expert - Chinese | chinese | Explicit language requirement is not listed on profile. |
| phd_history_researcher | Alignerr | AI Language Expert - Chinese | chinese | Explicit language requirement is not listed on profile. |
| portuguese_english_reviewer | Alignerr | AI Language Expert - Chinese | chinese | Explicit language requirement is not listed on profile. |
| software_engineer | Alignerr | AI Language Expert - Chinese | chinese | Explicit language requirement is not listed on profile. |

## Section Suitability Overpromotions

| Profile | Case | Expected Section | Current Section | Score | Label |
|---|---|---|---|---:|---|
| beginner_bilingual_no_degree | Welocalize - Alpheratz Project - English (UK) Translation Quality Rater | also_worth_reviewing | do_these_first | 40 | Strong |
| beginner_bilingual_no_degree | Welocalize - Alpheratz Project - Spanish (US) Translation Quality Rater | best_matches | do_these_first | 40 | Strong |
| beginner_bilingual_no_degree | Welocalize - Alpheratz Project - Spanish (US) Translation Quality Reviewer | best_matches | do_these_first | 32 | Medium |
| beginner_bilingual_no_degree | Outlier - English Writing and Content Reviewing Expertise Sought for AI Training | also_worth_reviewing | do_these_first | 32 | Medium |
| beginner_bilingual_no_degree | OneForma - HT (Human Translation) and MTPE (Machine Translation Post Editing) - English (United States) - Spanish (Chile) | also_worth_reviewing | do_these_first | 31 | Medium |
| beginner_bilingual_no_degree | Meridial - Search Engine Evaluation Specialist - Freelance AI Trainer Project | also_worth_reviewing | best_matches | 24 | Medium |
| biology_or_medicine_academic | Mercor - Clinical / biomedical / pharma Evaluator | best_matches | do_these_first | 37 | Strong |
| biology_or_medicine_academic | Mercor - Biology Expert with Python Experience | best_matches | do_these_first | 34 | Strong |
| biology_or_medicine_academic | Mercor - Biology Research Scientist | best_matches | do_these_first | 30 | Medium |
| biology_or_medicine_academic | micro1 - Biology Expert | best_matches | do_these_first | 30 | Medium |
| biology_or_medicine_academic | DataAnnotation - Medicine Expert / Medical AI Trainer | also_worth_reviewing | best_matches | 29 | Medium |
| biology_or_medicine_academic | DataAnnotation - Biology Expert / AI Biology Trainer | also_worth_reviewing | best_matches | 29 | Medium |
| english_teacher_remote | Welocalize - Alpheratz Project - English (UK) Translation Quality Rater | also_worth_reviewing | do_these_first | 33 | Medium |
| english_teacher_remote | Turing - Audio/Voice/Annotation Trainer - English Language | also_worth_reviewing | do_these_first | 33 | Medium |
| english_teacher_remote | Alignerr - English Writing Generalist | best_matches | do_these_first | 31 | Medium |
| english_teacher_remote | Alignerr - English Writing Generalist - Advanced | best_matches | do_these_first | 31 | Medium |
| english_teacher_remote | Alignerr - English Writing Generalist - Quality Review | best_matches | do_these_first | 31 | Medium |
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
| generalist_no_degree | Alignerr - English Writing Generalist - Advanced | also_worth_reviewing | best_matches | 26 | Medium |
| generalist_no_degree | Alignerr - English Writing Generalist - Quality Review | also_worth_reviewing | best_matches | 26 | Medium |
| generalist_no_degree | Meridial - Search Engine Evaluation Specialist - Freelance AI Trainer Project | also_worth_reviewing | best_matches | 25 | Medium |
| generalist_no_degree | Meridial - Social Media Annotation - Freelance AI Trainer Project | also_worth_reviewing | best_matches | 25 | Medium |
| generalist_no_degree | Mindrift - Auto Claims Examiner - Freelance AI Trainer | explore_only | best_matches | 25 | Medium |
| lawyer | Mercor - IP Law Expert | best_matches | do_these_first | 30 | Medium |
| lawyer | Mercor - Legal Expert - Employment/Labor Law | best_matches | do_these_first | 30 | Medium |
| lawyer | Mercor - Legal Expert Specialist | best_matches | do_these_first | 30 | Medium |
| lawyer | Mercor - Legal Expert - Transactional/Corporate | best_matches | do_these_first | 30 | Medium |
| lawyer | Mercor - Litigation Expert | best_matches | do_these_first | 30 | Medium |
| lawyer | Mercor - Regulatory Law Expert | best_matches | do_these_first | 30 | Medium |
| lawyer | Turing - Contract Law Expert | best_matches | do_these_first | 30 | Medium |
| lawyer | DataAnnotation - Law Expert / Legal AI Trainer | also_worth_reviewing | best_matches | 29 | Medium |
| multilingual_translator | OneForma - HT (Human Translation) and MTPE (Machine Translation Post Editing) - English (United States) - French (France) | best_matches | do_these_first | 41 | Strong |
| multilingual_translator | Alignerr - Portuguese Localization Expert | best_matches | do_these_first | 35 | Strong |
| multilingual_translator | Alignerr - Spanish Localization Expert | best_matches | do_these_first | 35 | Strong |
| multilingual_translator | Meridial - French Language Specialist - Freelance AI Trainer Project | best_matches | do_these_first | 35 | Strong |
| multilingual_translator | Welocalize - Alpheratz Project - Portuguese (Portugal) Translation Quality Rater | best_matches | do_these_first | 35 | Strong |
| multilingual_translator | OneForma - Adaptation - Portuguese (Brazil) - Portuguese (Portugal) | also_worth_reviewing | do_these_first | 35 | Strong |
| phd_history_researcher | Outlier - English Writing and Content Reviewing Expertise Sought for AI Training | also_worth_reviewing | do_these_first | 32 | Medium |
| phd_history_researcher | Welocalize - Alpheratz Project - English (UK) Translation Quality Rater | also_worth_reviewing | do_these_first | 30 | Medium |
| phd_history_researcher | Welocalize - French Search & Data Labelling Rater - Morocco | explore_only | do_these_first | 30 | Medium |
| phd_history_researcher | Welocalize - Remote Internet Search Quality Rater - English (United States) | explore_only | do_these_first | 30 | Medium |
| phd_history_researcher | OneForma - Education - Pronunciation Evaluation - English (United Kingdom) | also_worth_reviewing | best_matches | 27 | Medium |
| phd_history_researcher | Mercor - Market Research Methodologist - Report Quality & Insights Evaluation Expert | explore_only | best_matches | 24 | Medium |
| portuguese_english_reviewer | Welocalize - Alpheratz Project - Portuguese (Portugal) Translation Quality Reviewer | best_matches | do_these_first | 43 | Strong |
| portuguese_english_reviewer | OneForma - HT (Human Translation) and MTPE (Machine Translation Post Editing) - English (United States) - Portuguese (Portugal) | best_matches | do_these_first | 41 | Strong |
| portuguese_english_reviewer | Meridial - Language Alignment & Resource Partner (Portuguese) - Freelance AI Trainer Project | best_matches | do_these_first | 35 | Strong |
| portuguese_english_reviewer | Meridial - Portuguese Language Data Contributor (Multimodal) - Freelance AI Trainer Project | also_worth_reviewing | do_these_first | 35 | Strong |
| portuguese_english_reviewer | Meridial - Portuguese Voice Actor - Freelance AI Trainer Project | also_worth_reviewing | do_these_first | 35 | Strong |
| portuguese_english_reviewer | Outlier - Portuguese (Brazil) Freelance Writer | best_matches | do_these_first | 35 | Strong |
| portuguese_english_reviewer | Turing - AI Quality Analyst - Portuguese (Portugal) | best_matches | do_these_first | 35 | Strong |
| software_engineer | Turing - Python + Full-Stack (JS) Developer | best_matches | do_these_first | 31 | Medium |
| software_engineer | Turing - Senior Backend Engineer (Python/FastAPI) - AI Evaluation (US-based) | best_matches | do_these_first | 31 | Medium |
| software_engineer | Turing - Senior Python Developer | best_matches | do_these_first | 31 | Medium |
| software_engineer | micro1 - QA Automation Engineer | also_worth_reviewing | best_matches | 29 | Medium |

## Recurring Failure Patterns

- `generic_evaluation_evidence`: 7
- `unsupported_language`: 6
- `unsupported_explicit_language`: 6
- `search_inside_research`: 2
- `professional_domain_mismatch`: 1

## Live Snapshot For Future Review

These are the current top 10 live matches by benchmark profile. They are not labeled truth rows unless they also appear in the fixture.

### beginner_bilingual_no_degree

| Rank | Source | Title | Score | Label | Reasons | Signals | Contradictions |
|---:|---|---|---:|---|---|---|---|
| 1 | Welocalize | Alpheratz Project - Spanish (United States) Translation Quality Rater | 40 | Strong | Language/translation signal; AI evaluation/training signal; Search/research quality signal; Spanish language signal | languages: spanish | - |
| 2 | Outlier | Kiswahili Writing Expertise for AI Training | 36 | Strong | Teaching/writing/review signal; Language/translation signal; AI evaluation/training signal; Remote/flexible signal | languages: kiswahili | unsupported explicit language: kiswahili |
| 3 | OneForma | LELI – Review & Refine Machine Translations in Your Language - English (Australia) | 34 | Strong | Teaching/writing/review signal; Language/translation signal; English language signal; Remote/flexible signal | languages: english | - |
| 4 | Welocalize | Alpheratz Project - Catalan (Spain) Translation Quality Rater | 34 | Strong | Language/translation signal; AI evaluation/training signal; Search/research quality signal; Remote/flexible signal | languages: catalan | unsupported explicit language: catalan |
| 5 | Welocalize | Alpheratz Project - Czech (Czech Republic) Translation Quality Rater | 34 | Strong | Language/translation signal; AI evaluation/training signal; Search/research quality signal; Remote/flexible signal | languages: czech | unsupported explicit language: czech |
| 6 | Welocalize | Alpheratz Project - Danish (Denmark) Translation Quality Rater | 34 | Strong | Language/translation signal; AI evaluation/training signal; Search/research quality signal; Remote/flexible signal | languages: danish | unsupported explicit language: danish |
| 7 | Welocalize | Alpheratz Project - Dutch (Netherlands) Translation Quality Rater | 34 | Strong | Language/translation signal; AI evaluation/training signal; Search/research quality signal; Remote/flexible signal | languages: dutch | unsupported explicit language: dutch |
| 8 | Welocalize | Alpheratz Project - French (Canada) Translation Quality Rater | 34 | Strong | Language/translation signal; AI evaluation/training signal; Search/research quality signal; Remote/flexible signal | languages: french | unsupported explicit language: french |
| 9 | Welocalize | Alpheratz Project - Hebrew (Israel) Translation Quality Rater | 34 | Strong | Language/translation signal; AI evaluation/training signal; Search/research quality signal; Remote/flexible signal | languages: hebrew | unsupported explicit language: hebrew |
| 10 | Welocalize | Alpheratz Project - Korean (Korea) Translation Quality Rater | 34 | Strong | Language/translation signal; AI evaluation/training signal; Search/research quality signal; Remote/flexible signal | languages: korean | unsupported explicit language: korean |

### biology_or_medicine_academic

| Rank | Source | Title | Score | Label | Reasons | Signals | Contradictions |
|---:|---|---|---:|---|---|---|---|
| 1 | Mercor | Clinical / biomedical / pharma Evaluator | 37 | Strong | Biology/life sciences match; Medicine/clinical match; Remote/flexible signal; Live/countable opportunity | domains: science/medical | - |
| 2 | Mercor | Clinical Research Experts (Chemistry and Biology) | 37 | Strong | Biology/life sciences match; Medicine/clinical match; Remote/flexible signal; Live/countable opportunity | domains: science/medical; contains both search and research | - |
| 3 | Meridial | Synthetic Biology Specialist - Freelance AI Trainer Project | 37 | Strong | Biology/life sciences match; Medicine/clinical match; Remote/flexible signal; Live/countable opportunity | domains: science/medical | - |
| 4 | Mindrift | Biology & Python Expert - Freelance AI Trainer | 34 | Strong | Biology/life sciences match; Academic/expert signal; Python + science task match; Remote/flexible signal | domains: technical, science/medical | - |
| 5 | Mindrift | Biology Expert with Python Experience - AI Projects on Mindrift | 34 | Strong | Biology/life sciences match; Academic/expert signal; Python + science task match; Remote/flexible signal | domains: technical, science/medical | - |
| 6 | Mercor | Biology Research Scientist (BA, MS, PhD's) | 30 | Medium | Biology/life sciences match; Academic/expert signal; Remote/flexible signal; Live/countable opportunity | domains: science/medical; contains both search and research | - |
| 7 | Mercor | Pharmacokinetics & Systems Biology Expert | 30 | Medium | Biology/life sciences match; Academic/expert signal; Remote/flexible signal; Live/countable opportunity | domains: science/medical | - |
| 8 | Turing | Academic Dermatologist – Senior Clinical Reviewer | 30 | Medium | Medicine/clinical match; Academic/expert signal; Remote/flexible signal; Live/countable opportunity | domains: science/medical | - |
| 9 | Turing | Biology Expert | 30 | Medium | Biology/life sciences match; Academic/expert signal; Remote/flexible signal; Live/countable opportunity | domains: science/medical | - |
| 10 | Turing | Biology Expert (PhD/Master’s) | 30 | Medium | Biology/life sciences match; Academic/expert signal; Remote/flexible signal; Live/countable opportunity | domains: science/medical | - |

### english_teacher_remote

| Rank | Source | Title | Score | Label | Reasons | Signals | Contradictions |
|---:|---|---|---:|---|---|---|---|
| 1 | Outlier | English Writing and Content Reviewing Expertise Sought for AI Training | 44 | Strong | Teaching/writing/review signal; English writing/content review signal; AI evaluation/training signal; English language signal | languages: english | - |
| 2 | Outlier | English Writing and Content Reviewing Expertise Sought for AI Training | 44 | Strong | Teaching/writing/review signal; English writing/content review signal; AI evaluation/training signal; English language signal | languages: english | - |
| 3 | OneForma | LELI – Review & Refine Machine Translations in Your Language - English (Australia) | 34 | Strong | Teaching/writing/review signal; Language/translation signal; English language signal; Remote/flexible signal | languages: english | - |
| 4 | Turing | Audio/Voice/Annotation Trainer - English (US) Language | 33 | Medium | Language/translation signal; AI evaluation/training signal; English language signal; Remote/flexible signal | languages: english | - |
| 5 | Alignerr | English Writing Generalist | 31 | Medium | Teaching/writing/review signal; English writing/content review signal; English language signal; Live/countable opportunity | languages: english | - |
| 6 | Alignerr | English Writing Generalist – Advanced | 31 | Medium | Teaching/writing/review signal; English writing/content review signal; English language signal; Live/countable opportunity | languages: english | - |
| 7 | Alignerr | English Writing Generalist – Quality Review | 31 | Medium | Teaching/writing/review signal; English writing/content review signal; English language signal; Live/countable opportunity | languages: english | - |
| 8 | Outlier | Polish Freelance STEM Writing | 28 | Medium | Teaching/writing/review signal; Language/translation signal; Remote/flexible signal; Live/countable opportunity | languages: polish | unsupported explicit language: polish |
| 9 | OneForma | Education Pronunciation Evaluation - English (United Kingdom) | 27 | Medium | Teaching/writing/review signal; AI evaluation/training signal; English language signal; Live/countable opportunity | languages: english | - |
| 10 | Turing | Audio/Voice/Annotation Trainer - Arabic Language | 27 | Medium | Language/translation signal; AI evaluation/training signal; Remote/flexible signal; Live/countable opportunity | languages: arabic | unsupported explicit language: arabic |

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
| 1 | Outlier | English Writing and Content Reviewing Expertise Sought for AI Training | 32 | Medium | Teaching/writing/review signal; AI evaluation/training signal; English language signal; Remote/flexible signal | languages: english | - |
| 2 | Outlier | English Writing and Content Reviewing Expertise Sought for AI Training | 32 | Medium | Teaching/writing/review signal; AI evaluation/training signal; English language signal; Remote/flexible signal | languages: english | - |
| 3 | Welocalize | Alpheratz Project - French (Canada) Translation Quality Rater | 30 | Medium | AI evaluation/training signal; Search/research quality signal; French language signal; Remote/flexible signal | languages: french | - |
| 4 | Welocalize | French Search & Data Labelling Rater - Project Andromeda Titawin | 30 | Medium | AI evaluation/training signal; Search/research quality signal; French language signal; Remote/flexible signal | languages: french | - |
| 5 | Welocalize | Remote Internet Search Quality Rater - English (United States) | 30 | Medium | AI evaluation/training signal; Search/research quality signal; English language signal; Remote/flexible signal | languages: english | - |
| 6 | Mercor | Keystone Education Expert | 28 | Medium | Teaching/writing/review signal; Research/humanities signal; Remote/flexible signal; Live/countable opportunity | - | - |
| 7 | OneForma | Education Pronunciation Evaluation - English (United Kingdom) | 27 | Medium | Teaching/writing/review signal; AI evaluation/training signal; English language signal; Live/countable opportunity | languages: english | - |
| 8 | Alignerr | English Writing Generalist – Quality Review | 26 | Medium | Teaching/writing/review signal; Search/research quality signal; English language signal; Live/countable opportunity | languages: english | - |
| 9 | Outlier | Kiswahili Writing Expertise for AI Training | 26 | Medium | Teaching/writing/review signal; AI evaluation/training signal; Remote/flexible signal; Live/countable opportunity | languages: kiswahili | unsupported explicit language: kiswahili |
| 10 | Welocalize | Alpha Telescopii - Entertainment Media Content Rater | 26 | Medium | Teaching/writing/review signal; AI evaluation/training signal; Remote/flexible signal; Live/countable opportunity | - | - |

### portuguese_english_reviewer

| Rank | Source | Title | Score | Label | Reasons | Signals | Contradictions |
|---:|---|---|---:|---|---|---|---|
| 1 | Welocalize | Alpheratz Project - Portuguese (Portugal) Translation Quality Rater | 51 | Strong | Portuguese language match; Language review signal; AI evaluation/reviewer signal; Search quality signal | languages: portuguese | - |
| 2 | Welocalize | Alpheratz Project - Portuguese (Portugal) Translation Quality Reviewer | 43 | Strong | Portuguese language match; Language review signal; AI evaluation/reviewer signal; Portuguese language signal | languages: portuguese | - |
| 3 | OneForma | HT (Human Translation) and MTPE (Machine Translation Post Editing) - English (United States) - Portuguese (Portugal) | 41 | Strong | Portuguese language match; Language review signal; English language signal; Portuguese language signal | languages: english, portuguese | - |
| 4 | Meridial | Language Alignment & Resource Partner (Portuguese) - Freelance AI Trainer Project | 35 | Strong | Portuguese language match; Language review signal; Portuguese language signal; Remote/flexible signal | languages: portuguese | - |
| 5 | Meridial | Portuguese Audio Evaluations Specialist - Freelance AI Trainer Project | 35 | Strong | Portuguese language match; Language review signal; Portuguese language signal; Remote/flexible signal | languages: portuguese | - |
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

- Human-review the highest-impact `codex_draft` labels before treating precision as product truth.
- Investigate visible false negatives caused by sparse fixture snapshots or missing live metadata.
- Review the history/humanities profile separately; do not inflate generic writing/search roles just to fill recommendation slots.
- Keep surfacing and classification metrics separate before tuning additional deterministic gates.

## Methodology Notes

- `strong` and `plausible` count as relevant for fixture-pool relevant precision/recall.
- Only `strong` counts as relevant for strict fixture-pool precision.
- `review_required: true` cases are visible in review files but excluded from headline metrics.
- Stored fixture snapshots are used only when the live DB row cannot be resolved.

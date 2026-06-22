# Matching Golden Set Human Review Draft

Generated: 2026-06-22T02:50:18.610874+00:00

All labels in this file are `codex_draft` labels. They are intended for human review and correction before being treated as product truth.

Review-required cases are excluded from headline precision, recall, and false-positive metrics in `matching_quality_report.md`.

## beginner_bilingual_no_degree

| Case | Source / Title | Expected | Expected Section | Review Required | Regression Rule | Current Score / Label | Rationale |
|---|---|---|---|---|---|---|---|
| beginner_bilingual_no_degree_001 | Welocalize - Alpheratz Project - English (UK) Translation Quality Rater | plausible | also_worth_reviewing | no | - | 40 / Strong | English language quality role may fit if no degree required. |
| beginner_bilingual_no_degree_002 | Welocalize - Alpheratz Project - Spanish (US) Translation Quality Reviewer | strong | best_matches | no | - | 32 / Medium | Spanish-English review work is directly relevant. |
| beginner_bilingual_no_degree_003 | Welocalize - Alpheratz Project - Spanish (US) Translation Quality Rater | strong | best_matches | no | - | 40 / Strong | Spanish-English rating work is directly relevant. |
| beginner_bilingual_no_degree_004 | OneForma - HT (Human Translation) and MTPE (Machine Translation Post Editing) - English (United States) - Spanish (Chile) | plausible | also_worth_reviewing | no | - | 31 / Medium | English-Spanish translation/post-editing is relevant, though experience may matter. |
| beginner_bilingual_no_degree_005 | DataAnnotation - Bilingual AI Trainer | plausible | also_worth_reviewing | yes | - | 18 / Possible | Evergreen bilingual application is relevant but not live inventory. |
| beginner_bilingual_no_degree_006 | Meridial - Social Media Annotation - Freelance AI Trainer Project | plausible | also_worth_reviewing | no | - | 17 / Possible | Annotation work could fit a beginner profile. |
| beginner_bilingual_no_degree_007 | Meridial - Search Engine Evaluation Specialist - Freelance AI Trainer Project | plausible | also_worth_reviewing | no | - | 24 / Medium | Search evaluation can fit beginner remote work. |
| beginner_bilingual_no_degree_008 | Outlier - English Writing and Content Reviewing Expertise Sought for AI Training | plausible | also_worth_reviewing | no | - | 32 / Medium | English content review can fit if writing skills are sufficient. |
| beginner_bilingual_no_degree_009 | Outlier - Kiswahili Freelance Writer | false_positive | exclude | no | unsupported_explicit_language | 9 / Possible | Kiswahili is unsupported for English-Spanish bilingual profile. |
| beginner_bilingual_no_degree_010 | Welocalize - Alpheratz Project - Catalan Translation Quality Rater | false_positive | exclude | no | unsupported_explicit_language | 34 / Strong | Catalan is unsupported. |
| beginner_bilingual_no_degree_011 | Welocalize - Alpheratz Project - Czech Translation Quality Rater | false_positive | exclude | no | unsupported_explicit_language | 34 / Strong | Czech is unsupported. |
| beginner_bilingual_no_degree_012 | Welocalize - Alpheratz Project - Danish Translation Quality Rater | false_positive | exclude | no | unsupported_explicit_language | 34 / Strong | Danish is unsupported. |
| beginner_bilingual_no_degree_013 | Welocalize - Alpheratz Project - Dutch Translation Quality Rater | false_positive | exclude | no | unsupported_explicit_language | 34 / Strong | Dutch is unsupported. |
| beginner_bilingual_no_degree_014 | Welocalize - Alpheratz Project - Korean Translation Quality Rater | false_positive | exclude | no | unsupported_explicit_language | 34 / Strong | Korean is unsupported. |
| beginner_bilingual_no_degree_015 | Turing - Senior Python Developer | false_positive | exclude | no | technical_mismatch | 0 / Possible | Senior software role is outside beginner no-degree profile. |
| beginner_bilingual_no_degree_016 | Mercor - Legal Expert Specialist | false_positive | exclude | no | - | 0 / Possible | Legal expert role is outside beginner no-degree profile. |

## biology_or_medicine_academic

| Case | Source / Title | Expected | Expected Section | Review Required | Regression Rule | Current Score / Label | Rationale |
|---|---|---|---|---|---|---|---|
| biology_or_medicine_academic_001 | Mercor - Clinical Research Experts (Chemistry and Biology) | strong | do_these_first | no | - | 37 / Strong | Clinical research with biology/chemistry is directly relevant. |
| biology_or_medicine_academic_002 | Mercor - Clinical / biomedical / pharma Evaluator | strong | best_matches | no | - | 37 / Strong | Biomedical/pharma evaluation is directly relevant. |
| biology_or_medicine_academic_003 | Mercor - Microbiology Specialist | strong | best_matches | no | - | 9 / Possible | Microbiology is a direct science match. |
| biology_or_medicine_academic_004 | Mercor - Synthetic Biology Specialist | strong | best_matches | no | - | 23 / Possible | Synthetic biology is directly relevant. |
| biology_or_medicine_academic_005 | Mercor - Biology Expert with Python Experience | strong | best_matches | no | - | 34 / Strong | Biology expertise is relevant; Python may be a bonus/constraint. |
| biology_or_medicine_academic_006 | Mercor - Biology Research Scientist | strong | best_matches | no | - | 30 / Medium | Biology research scientist role is directly relevant. |
| biology_or_medicine_academic_007 | Turing - Academic Dermatologist | strong | best_matches | no | - | 16 / Possible | Medical academic domain is relevant. |
| biology_or_medicine_academic_008 | Mercor - Medicine Physician | strong | best_matches | no | - | 23 / Possible | Medicine physician role is relevant if credentials match. |
| biology_or_medicine_academic_009 | micro1 - Biology Expert | strong | best_matches | no | - | 30 / Medium | Biology expert role is a direct match. |
| biology_or_medicine_academic_010 | DataAnnotation - Medicine Expert / Medical AI Trainer | plausible | also_worth_reviewing | no | - | 29 / Medium | Evergreen medical application is relevant but not live inventory. |
| biology_or_medicine_academic_011 | DataAnnotation - Biology Expert / AI Biology Trainer | plausible | also_worth_reviewing | no | - | 29 / Medium | Evergreen biology application is relevant but not live inventory. |
| biology_or_medicine_academic_012 | Mercor - Legal Expert Specialist | false_positive | exclude | no | - | 0 / Possible | Legal role is outside biology/medicine. |
| biology_or_medicine_academic_013 | Mercor - Corporate Finance Expert | false_positive | exclude | no | - | 0 / Possible | Finance role is outside biology/medicine. |
| biology_or_medicine_academic_014 | Turing - Senior Python Developer | false_positive | exclude | no | - | 0 / Possible | Software engineering role is outside biology/medicine unless science coding is explicit. |
| biology_or_medicine_academic_015 | Welocalize - Alpheratz Project - Portuguese (Portugal) Translation Quality Rater | false_positive | exclude | no | - | 0 / Possible | Translation role is outside biology/medicine. |
| biology_or_medicine_academic_016 | Outlier - English Writing and Content Reviewing Expertise Sought for AI Training | weak | explore_only | no | - | 15 / Possible | Writing review may be useful but lacks science/medical specificity. |

## english_teacher_remote

| Case | Source / Title | Expected | Expected Section | Review Required | Regression Rule | Current Score / Label | Rationale |
|---|---|---|---|---|---|---|---|
| english_teacher_remote_001 | Outlier - English Writing and Content Reviewing Expertise Sought for AI Training | strong | do_these_first | no | - | 44 / Strong | English writing/content review is a strong remote teacher fit. |
| english_teacher_remote_002 | Alignerr - English Writing Generalist | strong | best_matches | no | - | 31 / Medium | English writing generalist role is a strong fit. |
| english_teacher_remote_003 | Alignerr - English Writing Generalist - Advanced | strong | best_matches | no | - | 31 / Medium | Advanced English writing role is relevant if experience is sufficient. |
| english_teacher_remote_004 | Alignerr - English Writing Generalist - Quality Review | strong | best_matches | no | - | 31 / Medium | English quality review aligns with teaching and feedback skills. |
| english_teacher_remote_005 | Welocalize - Alpheratz Project - English (UK) Translation Quality Rater | plausible | also_worth_reviewing | no | - | 33 / Medium | English language quality role is plausible, though locale may matter. |
| english_teacher_remote_006 | Turing - Audio/Voice/Annotation Trainer - English Language | plausible | also_worth_reviewing | no | - | 33 / Medium | English audio/annotation work is adjacent to teaching and review. |
| english_teacher_remote_007 | OneForma - Education - Pronunciation Evaluation - English (United Kingdom) | strong | best_matches | no | - | 27 / Medium | Education pronunciation evaluation is a strong teacher-language fit. |
| english_teacher_remote_008 | DataAnnotation - Generalist AI Trainer | plausible | also_worth_reviewing | no | - | 8 / Possible | Evergreen generalist application can fit English teaching skills. |
| english_teacher_remote_009 | OneForma - HT (Human Translation) and MTPE (Machine Translation Post Editing) - Chinese (Simplified) - English (United States) | false_positive | exclude | no | unsupported_explicit_language | 19 / Possible | Requires Chinese-English translation, unsupported by profile. |
| english_teacher_remote_010 | OneForma - HT (Human Translation) and MTPE (Machine Translation Post Editing) - English (United States) - Norwegian (Norway) | false_positive | exclude | no | unsupported_explicit_language | 19 / Possible | Requires Norwegian translation, unsupported by profile. |
| english_teacher_remote_011 | Welocalize - Alpheratz Project - Catalan Translation Quality Rater | false_positive | exclude | no | unsupported_explicit_language | 17 / Possible | Catalan is unsupported. |
| english_teacher_remote_012 | Welocalize - Alpheratz Project - Czech Translation Quality Rater | false_positive | exclude | no | unsupported_explicit_language | 17 / Possible | Czech is unsupported. |
| english_teacher_remote_013 | Outlier - Polish STEM Writing Expertise Sought for AI Training | false_positive | exclude | no | unsupported_explicit_language | 26 / Medium | Polish and STEM requirements are unsupported. |
| english_teacher_remote_014 | Turing - Senior Python Developer | false_positive | exclude | no | - | 0 / Possible | Software engineering role is outside English teaching profile. |
| english_teacher_remote_015 | Mercor - Corporate Finance Expert | false_positive | exclude | no | - | 0 / Possible | Finance expert role is outside English teaching profile. |
| english_teacher_remote_016 | Mercor - Biology Research Scientist | false_positive | exclude | no | - | 0 / Possible | Biology research role is outside English teaching profile. |

## finance_professional

| Case | Source / Title | Expected | Expected Section | Review Required | Regression Rule | Current Score / Label | Rationale |
|---|---|---|---|---|---|---|---|
| finance_professional_001 | micro1 - US Tax Law Analyst (EA/CA) | strong | do_these_first | no | - | 42 / Strong | Tax analysis is a strong finance/accounting match. |
| finance_professional_002 | Alignerr - Accounting & Finance Expert | strong | best_matches | no | - | 37 / Strong | Accounting and finance expert work is direct fit. |
| finance_professional_003 | Alignerr - Finance & Accounting Expert | strong | best_matches | no | - | 37 / Strong | Finance/accounting expert work is direct fit. |
| finance_professional_004 | Mercor - General finance / accounting Evaluator | strong | best_matches | no | - | 35 / Strong | Finance/accounting evaluator is a direct match. |
| finance_professional_005 | Meridial - Accounting Specialist - Freelance AI Trainer Project | strong | best_matches | no | - | 35 / Strong | Accounting specialist AI trainer work is relevant. |
| finance_professional_006 | micro1 - Accountant | strong | best_matches | no | - | 35 / Strong | Accountant role is directly relevant. |
| finance_professional_007 | micro1 - Tax Professional (EA / CPA) | strong | best_matches | no | - | 35 / Strong | Tax professional role is directly relevant. |
| finance_professional_008 | Alignerr - Financial Analyst AI Researcher - Remote | strong | best_matches | no | - | 30 / Medium | Financial analyst AI research is relevant to finance. |
| finance_professional_009 | Mercor - Corporate Finance Expert | strong | best_matches | no | - | 30 / Medium | Corporate finance expert role is relevant. |
| finance_professional_010 | Mercor - Investment Banking Expert | strong | best_matches | no | - | 30 / Medium | Investment banking expert role is relevant. |
| finance_professional_011 | DataAnnotation - Finance Expert / Finance AI Trainer | plausible | also_worth_reviewing | no | - | 29 / Medium | Evergreen finance application is useful but not live inventory. |
| finance_professional_012 | Mercor - Legal Expert Specialist | false_positive | exclude | no | professional_domain_mismatch | 0 / Possible | Legal expert role is outside finance unless finance law is explicit. |
| finance_professional_013 | Mercor - Biology Research Scientist | false_positive | exclude | no | - | 0 / Possible | Biology research is outside finance. |
| finance_professional_014 | Turing - Senior Python Developer | false_positive | exclude | no | - | 0 / Possible | Software engineering role is outside finance. |
| finance_professional_015 | Welocalize - Alpheratz Project - Portuguese (Portugal) Translation Quality Rater | false_positive | exclude | no | - | 0 / Possible | Portuguese translation role is not finance. |
| finance_professional_016 | Mindrift - Freelance Mathematics Expert - AI Trainer | weak | explore_only | yes | - | 0 / Possible | Math is adjacent to finance but not enough without finance context. |

## generalist_no_degree

| Case | Source / Title | Expected | Expected Section | Review Required | Regression Rule | Current Score / Label | Rationale |
|---|---|---|---|---|---|---|---|
| generalist_no_degree_001 | Outlier - English Writing and Content Reviewing Expertise Sought for AI Training | strong | do_these_first | no | - | 31 / Medium | Broad English writing and content review work is a good no-degree remote AI-work fit. |
| generalist_no_degree_002 | Welocalize - Remote Internet Search Quality Rater - English (United States) | strong | best_matches | no | - | 29 / Medium | Search quality rater work is plausible generalist remote work with English requirements. |
| generalist_no_degree_003 | Alignerr - English Writing Generalist | strong | best_matches | no | - | 26 / Medium | English writing generalist work fits the profile and does not require a specialist domain in the title. |
| generalist_no_degree_004 | Alignerr - English Writing Generalist - Advanced | plausible | also_worth_reviewing | no | - | 26 / Medium | Still relevant English writing work, but advanced label may require stronger experience. |
| generalist_no_degree_005 | Alignerr - English Writing Generalist - Quality Review | plausible | also_worth_reviewing | no | - | 26 / Medium | Quality review is adjacent to generalist content review. |
| generalist_no_degree_006 | Meridial - Search Engine Evaluation Specialist - Freelance AI Trainer Project | plausible | also_worth_reviewing | no | - | 25 / Medium | Search evaluation may fit a generalist worker but should not outrank clear English writing roles. |
| generalist_no_degree_007 | Meridial - Social Media Annotation - Freelance AI Trainer Project | plausible | also_worth_reviewing | no | - | 25 / Medium | Social media annotation is plausible generalist work. |
| generalist_no_degree_008 | DataAnnotation - Generalist AI Trainer | plausible | also_worth_reviewing | no | - | 17 / Possible | Evergreen generalist application is useful but not a live task feed. |
| generalist_no_degree_009 | Mindrift - Freelance Mathematics Expert - AI Trainer | false_positive | exclude | no | generalist_specialist_domain_mismatch | 0 / Possible | Requires math expertise not present in the no-degree generalist profile. |
| generalist_no_degree_010 | Mindrift - Material Science Expert with Python - Freelance AI Trainer | false_positive | exclude | no | generalist_specialist_domain_mismatch | 0 / Possible | Combines science and Python expertise, not a generalist no-degree fit. |
| generalist_no_degree_011 | Mindrift - Direct Endorsement (DE) Underwriter - Freelance AI Trainer | false_positive | exclude | no | generalist_specialist_domain_mismatch | 0 / Possible | Mortgage underwriting is a specialist credentialed domain, not a generalist fit. |
| generalist_no_degree_012 | Mindrift - Auto Claims Examiner - Freelance AI Trainer | weak | explore_only | no | - | 25 / Medium | Could be administrative review work, but the domain is specialized. |
| generalist_no_degree_013 | Meridial - Pavement Condition Index (PCI) Survey & Annotation Specialist - Freelance AI Trainer Project | weak | explore_only | yes | - | 25 / Medium | Annotation is relevant, but pavement condition survey expertise is specialized. |
| generalist_no_degree_014 | micro1 - Legal Expert | false_positive | exclude | no | generalist_specialist_domain_mismatch | 0 / Possible | Legal expertise is a specialist domain outside this profile. |
| generalist_no_degree_015 | micro1 - Finance Expert | false_positive | exclude | no | generalist_specialist_domain_mismatch | 0 / Possible | Finance expertise is a specialist domain outside this profile. |
| generalist_no_degree_016 | Turing - Senior Python Developer | false_positive | exclude | no | technical_mismatch | 0 / Possible | Senior coding role is outside a non-coding generalist profile. |

## lawyer

| Case | Source / Title | Expected | Expected Section | Review Required | Regression Rule | Current Score / Label | Rationale |
|---|---|---|---|---|---|---|---|
| lawyer_001 | Mercor - Attorney / Legal Expert (Real Estate/Energy) | strong | do_these_first | no | - | 30 / Medium | Attorney/legal expert work is directly relevant. |
| lawyer_002 | Mercor - IP Expert | strong | best_matches | no | - | 6 / Possible | Intellectual property expertise is legal-domain work. |
| lawyer_003 | Mercor - IP Law Expert | strong | best_matches | no | - | 30 / Medium | IP law is a direct legal match. |
| lawyer_004 | Mercor - Legal Expert - Employment/Labor Law | strong | best_matches | no | - | 30 / Medium | Employment/labor law is a direct legal match. |
| lawyer_005 | Mercor - Legal Expert Specialist | strong | best_matches | no | - | 30 / Medium | General legal expert role is relevant. |
| lawyer_006 | Mercor - Legal Expert - Transactional/Corporate | strong | best_matches | no | - | 30 / Medium | Transactional/corporate legal work is relevant. |
| lawyer_007 | Mercor - Litigation Expert | strong | best_matches | no | - | 30 / Medium | Litigation is a direct legal specialization. |
| lawyer_008 | Mercor - Regulatory Law Expert | strong | best_matches | no | - | 30 / Medium | Regulatory law is a direct legal specialization. |
| lawyer_009 | Turing - Contract Law Expert | strong | best_matches | no | - | 30 / Medium | Contract law expert work is directly relevant. |
| lawyer_010 | DataAnnotation - Law Expert / Legal AI Trainer | plausible | also_worth_reviewing | no | - | 29 / Medium | Evergreen legal application is relevant but not live inventory. |
| lawyer_011 | micro1 - Software Engineer - AI Quality & Testing | false_positive | exclude | no | technical_mismatch | 0 / Possible | Software engineering role is outside legal expertise. |
| lawyer_012 | Mercor - Corporate Finance Expert | false_positive | exclude | no | professional_domain_mismatch | 0 / Possible | Finance role is not legal unless legal requirements are explicit. |
| lawyer_013 | Mercor - Biology Research Scientist | false_positive | exclude | no | professional_domain_mismatch | 0 / Possible | Biology role is outside legal domain. |
| lawyer_014 | Mindrift - Freelance Mathematics Expert - AI Trainer | false_positive | exclude | no | professional_domain_mismatch | 0 / Possible | Mathematics expert role is outside legal domain. |
| lawyer_015 | Outlier - English Writing and Content Reviewing Expertise Sought for AI Training | weak | explore_only | no | - | 5 / Possible | Writing review is adjacent but not a legal opportunity. |
| lawyer_016 | Turing - AI Evaluation Engineer (Python / Java / Web) | false_positive | exclude | no | technical_mismatch | 0 / Possible | Technical engineering role is not a legal match. |

## multilingual_translator

| Case | Source / Title | Expected | Expected Section | Review Required | Regression Rule | Current Score / Label | Rationale |
|---|---|---|---|---|---|---|---|
| multilingual_translator_001 | OneForma - LELI - Review & Refine Machine Translations in Your Language - French (France) | strong | do_these_first | no | - | 47 / Strong | French machine translation review is directly relevant. |
| multilingual_translator_002 | Alignerr - Portuguese Localization Expert | strong | best_matches | no | - | 35 / Strong | Portuguese localization is a direct language-work match. |
| multilingual_translator_003 | Alignerr - Spanish Localization Expert | strong | best_matches | no | - | 35 / Strong | Spanish localization is a direct language-work match. |
| multilingual_translator_004 | OneForma - HT (Human Translation) and MTPE (Machine Translation Post Editing) - English (United States) - French (France) | strong | best_matches | no | - | 41 / Strong | English-French translation/post-editing is directly relevant. |
| multilingual_translator_005 | Mercor - French Voice/Audio AI Data Roles | strong | best_matches | yes | - | 23 / Possible | French voice/audio AI data role is relevant language work. |
| multilingual_translator_006 | Mercor - Spanish Voice/Audio AI Data Roles | strong | best_matches | yes | - | 23 / Possible | Spanish voice/audio AI data role is relevant language work. |
| multilingual_translator_007 | Meridial - French Language Specialist - Freelance AI Trainer Project | strong | best_matches | no | - | 35 / Strong | French language specialist role is relevant. |
| multilingual_translator_008 | Welocalize - Alpheratz Project - Portuguese (Portugal) Translation Quality Rater | strong | best_matches | no | - | 35 / Strong | Portuguese translation quality role is relevant. |
| multilingual_translator_009 | OneForma - Adaptation - Portuguese (Brazil) - Portuguese (Portugal) | plausible | also_worth_reviewing | no | - | 35 / Strong | Portuguese variant adaptation is relevant but variant-specific. |
| multilingual_translator_010 | DataAnnotation - Bilingual AI Trainer | plausible | also_worth_reviewing | yes | - | 8 / Possible | Evergreen bilingual application is relevant but not live inventory. |
| multilingual_translator_011 | Welocalize - Alpheratz Project - Danish Translation Quality Rater | false_positive | exclude | no | - | 11 / Possible | Danish is outside listed languages. |
| multilingual_translator_012 | Welocalize - Alpheratz Project - Korean Translation Quality Rater | false_positive | exclude | no | - | 11 / Possible | Korean is outside listed languages. |
| multilingual_translator_013 | OneForma - HT (Human Translation) and MTPE (Machine Translation Post Editing) - English (United States) - Norwegian (Norway) | false_positive | exclude | no | - | 21 / Possible | Norwegian is outside listed languages. |
| multilingual_translator_014 | Turing - Senior Python Developer | false_positive | exclude | no | - | 0 / Possible | Software engineering role is outside translator profile. |
| multilingual_translator_015 | Mercor - Legal Expert Specialist | false_positive | exclude | no | - | 0 / Possible | Legal expert role is outside translation profile. |
| multilingual_translator_016 | Mercor - Corporate Finance Expert | false_positive | exclude | no | - | 0 / Possible | Finance expert role is outside translation profile. |

## phd_history_researcher

| Case | Source / Title | Expected | Expected Section | Review Required | Regression Rule | Current Score / Label | Rationale |
|---|---|---|---|---|---|---|---|
| phd_history_researcher_001 | Mercor - Keystone Education Expert | strong | best_matches | no | - | 28 / Medium | Education expertise is plausible for an academic humanities researcher. |
| phd_history_researcher_002 | OneForma - Education - Pronunciation Evaluation - English (United Kingdom) | plausible | also_worth_reviewing | no | - | 27 / Medium | Education/language evaluation is adjacent to academic review work. |
| phd_history_researcher_003 | Outlier - English Writing and Content Reviewing Expertise Sought for AI Training | plausible | also_worth_reviewing | no | - | 32 / Medium | English writing/content review can fit academic writing skills. |
| phd_history_researcher_004 | Welocalize - Alpheratz Project - English (UK) Translation Quality Rater | plausible | also_worth_reviewing | no | - | 30 / Medium | English quality review is adjacent but not history-specific. |
| phd_history_researcher_005 | DataAnnotation - Generalist AI Trainer | plausible | also_worth_reviewing | no | - | 8 / Possible | Evergreen generalist AI training may fit academic writing/research skills. |
| phd_history_researcher_006 | DataAnnotation - Law Expert / Legal AI Trainer | weak | explore_only | yes | - | 0 / Possible | Humanities researcher may review legal prose only with legal credentials; not a strong fit. |
| phd_history_researcher_007 | Mercor - Market Research Methodologist - Report Quality & Insights Evaluation Expert | weak | explore_only | no | - | 24 / Medium | Research/report quality is adjacent, but market-research methodology is not history-specific. |
| phd_history_researcher_008 | Turing - Software Engineer - AI Research & Evaluation (US-based) | false_positive | exclude | no | history_software_research_false_positive | 0 / Possible | Current matcher overweights research/evaluation, but this is a software role. |
| phd_history_researcher_009 | Mercor - Biology & Biophysics Researchers (India, Part-time) | false_positive | exclude | no | history_science_research_false_positive | 0 / Possible | Research term matches, but biology/biophysics is outside humanities history. |
| phd_history_researcher_010 | Mercor - Human Baseliner for Open-Ended ML Research Tasks | weak | explore_only | no | generic_research_overpromotion | 16 / Possible | Generic ML research task may be adjacent but is not clearly humanities work. |
| phd_history_researcher_011 | Turing - Academic Dermatologist | false_positive | exclude | no | history_medical_false_positive | 0 / Possible | Academic title should not override medical-specialty mismatch. |
| phd_history_researcher_012 | OneForma - Project Spring - Healthcare Data Improvement Project - English (Ireland) | false_positive | exclude | no | history_healthcare_false_positive | 0 / Possible | Healthcare data project is outside history/humanities despite English/research signals. |
| phd_history_researcher_013 | Welocalize - French Search & Data Labelling Rater - Morocco | weak | explore_only | no | - | 30 / Medium | French/search rater may be a language-adjacent role but not a strong history match. |
| phd_history_researcher_014 | Welocalize - Remote Internet Search Quality Rater - English (United States) | weak | explore_only | no | - | 30 / Medium | Search quality is adjacent but should not be a top action for history research profile. |
| phd_history_researcher_015 | Mercor - Corporate Finance Expert | false_positive | exclude | no | - | 0 / Possible | Finance role is outside humanities research. |
| phd_history_researcher_016 | Turing - Senior Python Developer | false_positive | exclude | no | - | 0 / Possible | Software role is outside humanities research. |

## portuguese_english_reviewer

| Case | Source / Title | Expected | Expected Section | Review Required | Regression Rule | Current Score / Label | Rationale |
|---|---|---|---|---|---|---|---|
| portuguese_english_reviewer_001 | Welocalize - Alpheratz Project - Portuguese (Portugal) Translation Quality Rater | strong | do_these_first | no | - | 51 / Strong | Direct Portuguese translation quality rater fit. |
| portuguese_english_reviewer_002 | Meridial - Portuguese Audio Evaluations Specialist - Freelance AI Trainer Project | strong | do_these_first | no | - | 35 / Strong | Portuguese audio evaluation is a direct language-review fit. |
| portuguese_english_reviewer_003 | Welocalize - Alpheratz Project - Portuguese (Portugal) Translation Quality Reviewer | strong | best_matches | no | - | 43 / Strong | Direct Portuguese translation quality reviewer fit. |
| portuguese_english_reviewer_004 | OneForma - HT (Human Translation) and MTPE (Machine Translation Post Editing) - English (United States) - Portuguese (Portugal) | strong | best_matches | no | - | 41 / Strong | Explicit English to Portuguese translation/post-editing match. |
| portuguese_english_reviewer_005 | Meridial - Language Alignment & Resource Partner (Portuguese) - Freelance AI Trainer Project | strong | best_matches | no | - | 35 / Strong | Portuguese language alignment is directly relevant. |
| portuguese_english_reviewer_006 | Meridial - Portuguese Language Data Contributor (Multimodal) - Freelance AI Trainer Project | plausible | also_worth_reviewing | no | - | 35 / Strong | Portuguese language data work is relevant but may be more data collection than review. |
| portuguese_english_reviewer_007 | Meridial - Portuguese Voice Actor - Freelance AI Trainer Project | plausible | also_worth_reviewing | no | - | 35 / Strong | Portuguese voice work is relevant but depends on comfort with recording. |
| portuguese_english_reviewer_008 | Outlier - Portuguese (Brazil) Freelance Writer | strong | best_matches | no | - | 35 / Strong | Portuguese writing is a direct fit. |
| portuguese_english_reviewer_009 | Turing - AI Quality Analyst - Portuguese (Portugal) | strong | best_matches | no | - | 35 / Strong | Portuguese AI quality analysis is directly relevant. |
| portuguese_english_reviewer_010 | DataAnnotation - Bilingual AI Trainer | plausible | also_worth_reviewing | yes | - | 8 / Possible | Evergreen bilingual application is relevant but not a live posting. |
| portuguese_english_reviewer_011 | Welocalize - Alpheratz Project - Czech Translation Quality Rater | false_positive | exclude | no | unsupported_explicit_language | 23 / Possible | Czech is not an explicit supported language for this profile. |
| portuguese_english_reviewer_012 | Welocalize - Alpheratz Project - Danish Translation Quality Rater | false_positive | exclude | no | unsupported_explicit_language | 23 / Possible | Danish is not an explicit supported language for this profile. |
| portuguese_english_reviewer_013 | Welocalize - Alpheratz Project - Korean Translation Quality Rater | false_positive | exclude | no | unsupported_explicit_language | 23 / Possible | Korean is not an explicit supported language for this profile. |
| portuguese_english_reviewer_014 | OneForma - HT (Human Translation) and MTPE (Machine Translation Post Editing) - English (United States) - Norwegian (Norway) | false_positive | exclude | no | unsupported_explicit_language | 17 / Possible | Norwegian translation should not rank for a Portuguese-English profile. |
| portuguese_english_reviewer_015 | Mercor - AI Safety Experts - English & Marathi | false_positive | exclude | no | unsupported_explicit_language | 0 / Possible | Marathi requirement is unsupported despite English appearing in the title. |
| portuguese_english_reviewer_016 | Turing - Senior Python Developer | false_positive | exclude | no | technical_mismatch | 0 / Possible | Coding role is outside Portuguese/English review work. |

## software_engineer

| Case | Source / Title | Expected | Expected Section | Review Required | Regression Rule | Current Score / Label | Rationale |
|---|---|---|---|---|---|---|---|
| software_engineer_001 | micro1 - Software Engineer - AI Quality & Testing | strong | do_these_first | no | - | 37 / Strong | Direct software/AI quality testing role. |
| software_engineer_002 | Meridial - Python Coding Specialist - Freelance AI Trainer Project | strong | do_these_first | no | - | 31 / Medium | Python coding specialist role is a direct match. |
| software_engineer_003 | Turing - Python + Full-Stack (JS) Developer | strong | best_matches | no | - | 31 / Medium | Python/full-stack role is a clear software fit. |
| software_engineer_004 | Turing - Senior Backend Engineer (Python/FastAPI) - AI Evaluation (US-based) | strong | best_matches | no | - | 31 / Medium | Backend Python/FastAPI and AI evaluation match the profile. |
| software_engineer_005 | Turing - Senior Python Developer | strong | best_matches | no | - | 31 / Medium | Direct senior Python opportunity. |
| software_engineer_006 | Turing - Software Engineer - AI Code Evaluation & Benchmarking (SWE-Bench) | strong | best_matches | no | - | 29 / Medium | Software code evaluation/benchmarking is highly relevant. |
| software_engineer_007 | micro1 - QA Automation Engineer | plausible | also_worth_reviewing | no | - | 29 / Medium | QA automation is relevant software evaluation work. |
| software_engineer_008 | Alignerr - Backend Python Developer | strong | best_matches | no | - | 26 / Medium | Backend Python developer role is directly relevant. |
| software_engineer_009 | Turing - Scientific Coding - Biology and Python | plausible | also_worth_reviewing | yes | - | 31 / Medium | Python coding is relevant, but biology domain may matter. |
| software_engineer_010 | DataAnnotation - AI Coding Specialist / Coding Expert | plausible | also_worth_reviewing | no | - | 20 / Possible | Evergreen coding application is relevant but not a live feed. |
| software_engineer_011 | Mercor - AI Safety Experts - English & Marathi | false_positive | exclude | no | technical_mismatch | 21 / Possible | Language/safety role is not primarily software engineering. |
| software_engineer_012 | Mercor - Legal Expert Specialist | false_positive | exclude | no | professional_domain_mismatch | 0 / Possible | Legal domain mismatch for software profile. |
| software_engineer_013 | Mercor - Corporate Finance Expert | false_positive | exclude | no | professional_domain_mismatch | 0 / Possible | Finance domain mismatch for software profile. |
| software_engineer_014 | Mercor - Biology Research Scientist | false_positive | exclude | no | professional_domain_mismatch | 0 / Possible | Science domain mismatch for software profile unless coding is explicit. |
| software_engineer_015 | Welocalize - Alpheratz Project - Portuguese (Portugal) Translation Quality Rater | false_positive | exclude | no | unsupported_explicit_language | 0 / Possible | Portuguese translation role is not a software match. |
| software_engineer_016 | Outlier - English Writing and Content Reviewing Expertise Sought for AI Training | weak | explore_only | no | - | 5 / Possible | AI training writing could be adjacent but is not a strong software role. |


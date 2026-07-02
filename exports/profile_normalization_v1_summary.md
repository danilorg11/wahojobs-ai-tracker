# Profile Normalization Suite V1

This read-only suite defines expected `canonical_profile_v1` outputs for raw user profile inputs. It is not a parser and does not call external services.

## Summary

- Total cases: 25
- Archetypes: beginner_bilingual_no_degree: 3, biology_or_medicine_academic: 3, english_teacher_remote: 2, finance_professional: 2, generalist_no_degree: 2, lawyer: 2, multilingual_translator: 3, phd_history_researcher: 2, portuguese_english_reviewer: 3, software_engineer: 3
- Input styles: long_paragraph: 3, messy_sparse_input: 4, resume_or_linkedin_style: 9, short_paragraph: 9
- Normalization focus: bilingual_pool: 3, constraints: 2, credentials: 4, domain_specialty: 12, education: 8, employment_type: 4, generalist_annotation: 1, jurisdiction: 1, language_proficiency: 1, languages: 9, license_absence: 4, locale: 1, location: 2, messy_input: 4, missing_ambiguous: 4, phone_preference: 3, professional_domain_guardrail: 6, remote_preference: 7, seniority: 2, skills: 4, writing_review: 1, years_experience: 8

## Cases

| Case | Archetype | Input style | Languages | Location | Credentials | Missing / ambiguous | Focus |
|---|---|---|---|---|---|---|---|
| pnv1_beginner_bilingual_short | beginner_bilingual_no_degree | short_paragraph | English (fluent), Spanish (conversational) | explicit | status: unknown | certifications, licenses, location, total_years | languages, education, remote_preference, bilingual_pool, professional_domain_guardrail |
| pnv1_beginner_bilingual_resume | beginner_bilingual_no_degree | resume_or_linkedin_style | English (fluent), Spanish (native) | explicit | status: unknown | certifications, licenses, location, total_years | languages, phone_preference, education, remote_preference |
| pnv1_beginner_bilingual_messy | beginner_bilingual_no_degree | messy_sparse_input | English, Spanish | explicit | status: unknown | certifications, licenses, location, total_years, language proficiency, availability | messy_input, languages, missing_ambiguous, remote_preference |
| pnv1_portuguese_short | portuguese_english_reviewer | short_paragraph | Portuguese (native) [Brazil], English (advanced) | Brazil | status: unknown | certifications, licenses, total_years | languages, location, remote_preference, bilingual_pool |
| pnv1_portuguese_long | portuguese_english_reviewer | long_paragraph | Portuguese (native) [Brazil], English (professional) | explicit | status: unknown | certifications, licenses, location, total_years | languages, phone_preference, domain_specialty |
| pnv1_portuguese_resume | portuguese_english_reviewer | resume_or_linkedin_style | Portuguese (professional) [BR], English (professional) | Sao Paulo, Sao Paulo, Brazil | status: unknown | certifications, licenses | languages, location, years_experience, employment_type |
| pnv1_multilingual_short | multilingual_translator | short_paragraph | English (professional), Spanish (professional), Portuguese (professional), French (professional) | explicit | status: unknown | certifications, licenses, location, total_years | languages, domain_specialty, bilingual_pool |
| pnv1_multilingual_resume | multilingual_translator | resume_or_linkedin_style | Spanish (professional), English (professional), Portuguese (professional) [BR], French (reading) | explicit | status: unknown | certifications, licenses, location | languages, locale, years_experience, domain_specialty |
| pnv1_multilingual_messy | multilingual_translator | messy_sparse_input | Spanish, Portuguese, French, English | explicit | status: unknown | certifications, licenses, location, total_years, language proficiency, locale | messy_input, languages, missing_ambiguous |
| pnv1_english_teacher_short | english_teacher_remote | short_paragraph | English (native) | explicit | status: unknown | certifications, licenses, location, total_years | education, skills, remote_preference, employment_type |
| pnv1_english_teacher_resume | english_teacher_remote | resume_or_linkedin_style | English (professional) | explicit | status: unknown | certifications, licenses, location | education, years_experience, employment_type, writing_review |
| pnv1_software_short | software_engineer | short_paragraph | English | explicit | status: unknown | certifications, licenses, location, total_years | skills, professional_domain_guardrail, employment_type |
| pnv1_software_resume | software_engineer | resume_or_linkedin_style | English | explicit | status: unknown | certifications, licenses, location | skills, seniority, years_experience, domain_specialty |
| pnv1_software_long | software_engineer | long_paragraph | English | explicit | status: absent | certifications, licenses, location, total_years | professional_domain_guardrail, license_absence, skills |
| pnv1_biology_long | biology_or_medicine_academic | long_paragraph | English | explicit | status: absent | certifications, licenses, location, total_years | credentials, license_absence, professional_domain_guardrail, domain_specialty |
| pnv1_biology_resume | biology_or_medicine_academic | resume_or_linkedin_style | English | explicit | status: unknown | certifications, licenses, location | education, years_experience, license_absence, domain_specialty |
| pnv1_biology_messy | biology_or_medicine_academic | messy_sparse_input | English | explicit | status: absent | certifications, licenses, location, total_years, years of experience, specialty depth | messy_input, license_absence, missing_ambiguous |
| pnv1_lawyer_short | lawyer | short_paragraph | English | explicit | status: unknown | certifications, licenses, location, total_years | credentials, domain_specialty, remote_preference |
| pnv1_lawyer_resume | lawyer | resume_or_linkedin_style | English | explicit | licenses: attorney; jurisdictions: California; status: explicit | certifications, location | credentials, jurisdiction, years_experience, domain_specialty |
| pnv1_finance_short | finance_professional | short_paragraph | English | explicit | status: unknown | certifications, licenses, location, total_years | domain_specialty, professional_domain_guardrail |
| pnv1_finance_resume | finance_professional | resume_or_linkedin_style | English | explicit | certifications: CFA Level II candidate; status: explicit | licenses, location | credentials, seniority, years_experience, domain_specialty |
| pnv1_history_short | phd_history_researcher | short_paragraph | English, French (reading) | explicit | status: unknown | certifications, licenses, location, total_years | education, domain_specialty, professional_domain_guardrail |
| pnv1_history_resume | phd_history_researcher | resume_or_linkedin_style | English (professional), French (reading) | explicit | status: unknown | certifications, licenses, location | education, language_proficiency, years_experience, domain_specialty |
| pnv1_generalist_short | generalist_no_degree | short_paragraph | English (intermediate) | explicit | status: unknown | certifications, licenses, location, total_years | education, remote_preference, constraints, generalist_annotation |
| pnv1_generalist_messy | generalist_no_degree | messy_sparse_input | English | explicit | status: unknown | certifications, licenses, location, total_years, language proficiency, availability | messy_input, phone_preference, constraints, missing_ambiguous |

## Notes

- Preserve explicit facts from resumes, LinkedIn-style profiles, paragraphs, and messy sparse self-descriptions.
- Use `unknown`, `absent`, missing fields, and ambiguous fields instead of inventing credentials, licenses, locations, years, or languages.
- This suite is a contract for future deterministic or reviewed extraction; it does not implement extraction.

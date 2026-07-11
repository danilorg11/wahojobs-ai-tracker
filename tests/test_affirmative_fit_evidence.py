import re
import unittest

import scripts.local_product_app as app
import scripts.profile_to_matches_preview as preview
from wahojobs.matching.fit_evidence import CONFLICTING, SUPPORTED, UNCERTAIN


def profile(*, languages=(), skills=(), domains=(), summary="", country="", education="not_specified"):
    return {
        "languages": list(languages),
        "skills": list(skills),
        "degrees_or_domains": list(domains),
        "summary": summary,
        "notes": "",
        "country": country,
        "education_level": education,
        "target_opportunity_types": [],
        "work_preferences": ["remote"],
        "constraints": [],
    }


def projected(title, candidate_profile, *, section="best_matches", location="Remote"):
    profile_languages = preview.matcher.profile_language_set(candidate_profile)
    matched_languages = preview.supported_profile_languages_in_title(
        preview.normalize_text(title), profile_languages
    )
    row = {
        "title": title,
        "canonical_title": title,
        "source_category": "",
        "department": "",
        "expertise": "",
        "description": "",
        "location": location,
    }
    match = {
        "source": "Fixture",
        "source_slug": "fixture",
        "display_title": title,
        "title": title,
        "url": "https://example.test/" + re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-"),
        "location": location,
        "expertise": "",
        "score": 24,
        "match_strength": "Medium",
        "raw_product_section": section,
        "effective_product_section": section,
        "eligible_for_personalized": True,
        "unsupported_languages": [],
        "detected_languages": matched_languages,
        "matched_languages": matched_languages,
        "language_requirement_mode": "single" if len(matched_languages) == 1 else "ambiguous",
        "reasons": ["Remote/flexible signal", "Live/countable opportunity"],
    }
    return preview.apply_preview_guardrails(candidate_profile, row, match)


class AffirmativeFitEvidenceTests(unittest.TestCase):
    def test_constructs_supported_uncertain_and_conflicting_statuses(self):
        supported = projected("Python Specialist", profile(skills=("Python",), domains=("software engineering",)))
        uncertain = projected("Orbital Mapping Specialist", profile(summary="I want remote AI work."))
        conflicting = projected("French Language Data Contributor", profile(languages=("English",)))
        conflicting["eligible_for_personalized"] = False
        conflicting["unsupported_languages"] = ["french"]
        conflicting["detected_languages"] = ["french"]
        conflicting = preview.apply_preview_guardrails(
            profile(languages=("English",)),
            {"title": conflicting["display_title"], "location": "Remote"},
            conflicting,
        )

        self.assertEqual(supported["affirmative_fit_status"], SUPPORTED)
        self.assertEqual(uncertain["affirmative_fit_status"], UNCERTAIN)
        self.assertEqual(conflicting["affirmative_fit_status"], CONFLICTING)

    def test_exact_structured_evidence_and_reviewed_adjacencies_are_recorded(self):
        software = profile(
            skills=("Python", "TypeScript", "React", "apis"),
            domains=("software engineering",),
            summary="Senior software engineer with API and data platform experience.",
        )
        python = projected("Python Specialist", software)
        frontend = projected("Frontend Engineer", software)
        backend = projected("Backend Engineer", software)

        self.assertEqual(python["affirmative_fit_status"], SUPPORTED)
        self.assertIn("Python", python["affirmative_fit"]["satisfied_groups"])
        self.assertIn("React + TypeScript -> Frontend Development", frontend["affirmative_fit"]["adjacencies_used"])
        self.assertIn("API/data-platform experience -> Backend Development", backend["affirmative_fit"]["adjacencies_used"])

    def test_broad_generic_signals_do_not_establish_role_specific_fit(self):
        generic = profile(
            languages=("English",),
            domains=("generalist",),
            summary="I want remote AI training and evaluation work.",
        )
        for title in (
            "English Writing Generalist",
            "English Voice Actor",
            "English Voice Coach",
            "Audio Engineer (Native English Language)",
            "Business Owners",
            "Business and Management Specialist",
            "Crowd Workers - Accents/Dialects",
        ):
            with self.subTest(title=title):
                result = projected(title, generic)
                self.assertNotEqual(result["affirmative_fit_status"], SUPPORTED)
                self.assertFalse(result["primary_recommendation_eligible"])

    def test_unmodeled_modifier_defaults_to_uncertain(self):
        result = projected("Orbital Mapping Specialist", profile(summary="I want AI evaluation work."))
        self.assertEqual(result["affirmative_fit_status"], UNCERTAIN)
        self.assertIn("Title-defining role or specialization", result["affirmative_fit"]["unmodeled_requirements"])

    def test_all_of_any_of_and_ambiguous_slash_requirements(self):
        software = profile(
            skills=("Python", "TypeScript"),
            domains=("software engineering",),
            summary="Software engineer with Python, TypeScript, and data platform experience.",
        )
        all_of = projected("Biologist with Python Experience", software)
        any_of = projected("SWE Infrastructure Specialist (JS/TS/Python)", software)
        biology = projected(
            "Biology / Environmental Science Evaluator",
            profile(domains=("biology",), summary="Biology researcher."),
        )

        self.assertEqual(all_of["affirmative_fit_status"], UNCERTAIN)
        self.assertEqual(any_of["affirmative_fit_status"], SUPPORTED)
        self.assertEqual(biology["affirmative_fit_status"], UNCERTAIN)
        self.assertTrue(
            any("Ambiguous slash requirement" in item for item in biology["affirmative_fit"]["unmodeled_requirements"])
        )

    def test_ai_interest_does_not_satisfy_ml_engineering(self):
        result = projected(
            "AI/ML Engineer",
            profile(domains=("generalist",), summary="Interested in AI training and evaluation."),
        )
        self.assertEqual(result["affirmative_fit_status"], UNCERTAIN)
        self.assertIn("Machine Learning Engineering", result["affirmative_fit"]["missing_requirements"])

    def test_generic_software_does_not_satisfy_other_engineering_mobile_or_sre(self):
        software = profile(
            skills=("Python", "TypeScript", "React"),
            domains=("software engineering",),
            summary="Senior software engineer.",
        )
        for title in ("Mechanical Engineer", "Mobile App Developer", "Site Reliability Engineer"):
            with self.subTest(title=title):
                result = projected(title, software)
                self.assertEqual(result["affirmative_fit_status"], UNCERTAIN)
                self.assertFalse(result["primary_recommendation_eligible"])

    def test_broad_biology_does_not_satisfy_adjacent_professions(self):
        biology = profile(
            domains=("biology", "microbiology"),
            skills=("research",),
            summary="PhD biology researcher with microbiology experience.",
            education="doctorate",
        )
        for title in (
            "Biology & Biophysics Researchers",
            "Chemist Talent Network",
            "Child Family School Social Workers",
            "Clinical / Biomedical / Pharma Evaluator",
        ):
            with self.subTest(title=title):
                result = projected(title, biology)
                self.assertNotEqual(result["affirmative_fit_status"], SUPPORTED)

    def test_india_parenthetical_with_qualifiers_is_actionable_only_for_india(self):
        title = "Biology & Biophysics Researchers (India, Part-time)"
        unknown = projected(title, profile(domains=("biology", "biophysics")))
        india = projected(title, profile(domains=("biology", "biophysics"), country="India"))
        us = projected(title, profile(domains=("biology", "biophysics"), country="United States"))

        self.assertIn("India eligibility", unknown["affirmative_fit"]["missing_requirements"])
        self.assertEqual(unknown["affirmative_fit_status"], UNCERTAIN)
        self.assertIn("Profile location satisfies India eligibility", india["affirmative_fit"]["location_and_locale_evidence"])
        self.assertEqual(india["affirmative_fit_status"], SUPPORTED)
        self.assertEqual(us["affirmative_fit_status"], CONFLICTING)

    def test_us_based_title_needs_confirmed_compatible_location(self):
        title = "Senior JavaScript/React Engineer - AI Training (US-based)"
        software = {
            "skills": ("JavaScript", "React", "TypeScript"),
            "domains": ("software engineering",),
            "summary": "Senior software engineer with React and TypeScript experience.",
        }
        unknown = projected(title, profile(**software))
        us = projected(title, profile(**software, country="United States"))
        india = projected(title, profile(**software, country="India"))

        self.assertEqual(unknown["affirmative_fit_status"], UNCERTAIN)
        self.assertIn("United States eligibility", unknown["affirmative_fit"]["missing_requirements"])
        self.assertEqual(us["affirmative_fit_status"], SUPPORTED)
        self.assertEqual(india["affirmative_fit_status"], CONFLICTING)

    def test_safe_fallback_and_natural_also_require_supported_fit(self):
        candidate = profile(
            languages=("English",),
            summary="I speak English and want remote AI data tasks.",
        )
        supported = projected("English Language Data Contributor", candidate, section="also_worth_reviewing")
        uncertain = projected("English Writing Generalist", candidate, section="also_worth_reviewing")
        promoted = preview.ensure_safe_do_these_first([uncertain, supported], candidate)
        sections = {item["display_title"]: item["preview_section"] for item in promoted}

        self.assertEqual(sections["English Language Data Contributor"], "do_these_first")
        self.assertEqual(sections["English Writing Generalist"], "also_worth_reviewing")
        self.assertTrue(supported["primary_recommendation_eligible"])
        self.assertFalse(uncertain["primary_recommendation_eligible"])

    def test_ineligible_do_first_row_does_not_block_supported_fallback(self):
        candidate = profile(
            languages=("English",),
            summary="I speak English and want remote AI data tasks.",
        )
        uncertain = projected("English Writing Generalist", candidate, section="do_these_first")
        supported = projected("English Language Data Contributor", candidate, section="best_matches")

        promoted = preview.ensure_safe_do_these_first([uncertain, supported], candidate)
        sections = {item["display_title"]: item["preview_section"] for item in promoted}
        self.assertEqual(sections["English Writing Generalist"], "do_these_first")
        self.assertFalse(uncertain["primary_recommendation_eligible"])
        self.assertEqual(sections["English Language Data Contributor"], "do_these_first")

    def test_grounded_why_fit_copy_comes_from_affirmative_evidence(self):
        result = projected(
            "Frontend Engineer",
            profile(
                skills=("React", "TypeScript"),
                domains=("software engineering",),
                summary="Software engineer with React and TypeScript experience.",
            ),
        )
        self.assertEqual(
            preview.user_fit_reason(result),
            "Your React and TypeScript experience aligns with this frontend role.",
        )
        self.assertNotIn("remote", preview.user_fit_reason(result).lower())

    def test_biology_research_copy_mentions_phd_only_when_profile_has_one(self):
        no_degree = projected(
            "Biology Research Scientist",
            profile(
                domains=("biology",),
                skills=("research",),
                summary="Biology researcher with academic research experience.",
            ),
        )
        doctorate = projected(
            "Biology Research Scientist",
            profile(
                domains=("biology",),
                skills=("research",),
                summary="PhD biology researcher with academic research experience.",
                education="doctorate",
            ),
        )

        self.assertNotIn("PhD", preview.user_fit_reason(no_degree))
        self.assertIn("PhD", preview.user_fit_reason(doctorate))

    def test_computational_biology_copy_uses_only_title_defining_evidence(self):
        candidate = profile(
            domains=("biology", "computational biology"),
            skills=("research",),
            summary="PhD computational biology researcher.",
            education="doctorate",
        )
        expert = projected("Computational Biology Expert", candidate)
        research = projected("Research Quality Specialist - Computational Biology", candidate)

        self.assertEqual(
            preview.user_fit_reason(expert),
            "Your computational biology background aligns with this opportunity.",
        )
        self.assertIn("research background", preview.user_fit_reason(research))

    def test_confirmed_degree_alternative_is_recorded_as_satisfied_evidence(self):
        result = projected(
            "Biology Expert (PhD/Master's)",
            profile(
                domains=("biology",),
                summary="PhD biology researcher.",
                education="doctorate",
            ),
        )
        degree_evidence = [
            item
            for item in result["affirmative_fit"]["supported_evidence"]
            if item["source"] == "credential"
        ]
        self.assertEqual(result["affirmative_fit_status"], SUPPORTED)
        self.assertEqual(degree_evidence[0]["profile_evidence"], "PhD or doctorate")

    def test_unknown_or_absent_degree_does_not_satisfy_degree_alternatives(self):
        unknown = projected(
            "Biology Expert (PhD/Master's)",
            profile(domains=("biology",), summary="Biology researcher."),
        )
        absent = projected(
            "Biology Expert (PhD/Master's)",
            profile(domains=("biology",), summary="Biology researcher.", education="no_degree"),
        )
        self.assertEqual(unknown["affirmative_fit_status"], UNCERTAIN)
        self.assertEqual(absent["affirmative_fit_status"], CONFLICTING)

    def test_bilingual_crowd_work_cites_both_profile_languages(self):
        result = projected(
            "Crowd Workers - Bilingual",
            profile(
                languages=("English", "Spanish"),
                summary="I speak English and Spanish and want remote AI data tasks.",
            ),
        )
        self.assertEqual(result["affirmative_fit_status"], SUPPORTED)
        self.assertIn("English and Spanish", preview.user_fit_reason(result))

    def test_uncertain_rows_remain_in_demo_qa_but_not_primary_list(self):
        context = {"matches": {section: [] for section in preview.SECTION_ORDER}}
        uncertain = projected("English Writing Generalist", profile(languages=("English",)))
        context["matches"][uncertain["preview_section"]].append(uncertain)

        self.assertEqual(app.build_ranked_presentation_matches(context), [])
        qa = app.render_affirmative_fit_qa(context)
        self.assertIn("English Writing Generalist", qa)
        self.assertIn("uncertain", qa)


if __name__ == "__main__":
    unittest.main()

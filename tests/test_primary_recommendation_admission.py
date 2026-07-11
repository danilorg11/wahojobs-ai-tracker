import unittest

import scripts.local_product_app as app
import scripts.profile_to_matches_preview as preview


def language_profile(*, locale_keys=()):
    return {
        "languages": ["English", "Spanish"],
        "language_locale_keys": list(locale_keys),
        "skills": ["review"],
        "degrees_or_domains": ["language"],
        "summary": "I speak English and Spanish.",
    }


def projected(title, profile):
    profile_languages = preview.matcher.profile_language_set(profile)
    matched_languages = preview.supported_profile_languages_in_title(
        preview.normalize_text(title),
        profile_languages,
    )
    row = {
        "title": title,
        "canonical_title": title,
        "source_category": "",
        "department": "",
        "expertise": "Language",
        "description": "",
        "location": "Remote",
    }
    match = {
        "source": "Fixture",
        "display_title": title,
        "title": title,
        "url": "https://example.test/role",
        "location": "Remote",
        "expertise": "Language",
        "score": 24,
        "match_strength": "Medium",
        "raw_product_section": "best_matches",
        "effective_product_section": "best_matches",
        "eligible_for_personalized": True,
        "unsupported_languages": [],
        "detected_languages": matched_languages,
        "matched_languages": matched_languages,
        "language_requirement_mode": "single" if len(matched_languages) == 1 else "ambiguous",
        "reasons": ["Language match"],
    }
    return preview.apply_preview_guardrails(profile, row, match)


def ranked(matches):
    context = {"matches": {section: [] for section in preview.SECTION_ORDER}}
    for match in matches:
        context["matches"][match["preview_section"]].append(match)
    return app.build_ranked_presentation_matches(context)


class PrimaryRecommendationAdmissionTests(unittest.TestCase):
    def test_broad_languages_do_not_admit_unconfirmed_locale_variants(self):
        matches = [
            projected(title, language_profile())
            for title in (
                "English (Australia) Audio Generalist Evaluator Expert",
                "English (New Zealand) Audio Generalist Evaluator Expert",
                "English (US) Audio Generalist Evaluator Expert",
                "Spanish (Mexico) Audio Generalist Evaluator Expert",
            )
        ]

        self.assertTrue(all(not match["primary_recommendation_eligible"] for match in matches))
        self.assertTrue(
            all(match["actionability_cap_reasons"] == ["unconfirmed_language_locale"] for match in matches)
        )
        self.assertEqual(ranked(matches), [])

    def test_confirmed_english_us_admits_only_us_locale(self):
        profile = language_profile(locale_keys=("english:united states",))
        matches = [
            projected(title, profile)
            for title in (
                "English (US) Language Data Contributor",
                "English (Australia) Language Data Contributor",
                "English (New Zealand) Language Data Contributor",
            )
        ]

        self.assertTrue(matches[0]["primary_recommendation_eligible"])
        self.assertFalse(matches[1]["primary_recommendation_eligible"])
        self.assertFalse(matches[2]["primary_recommendation_eligible"])
        self.assertEqual([match["display_title"] for match in ranked(matches)], [matches[0]["display_title"]])

    def test_confirmed_spanish_mexico_admits_only_mexico_locale(self):
        profile = language_profile(locale_keys=("spanish:mexico",))
        mexico = projected("Spanish (Mexico) Language Data Contributor", profile)
        spain = projected("Spanish (Spain) Language Data Contributor", profile)

        self.assertTrue(mexico["primary_recommendation_eligible"])
        self.assertFalse(spain["primary_recommendation_eligible"])
        self.assertEqual([match["display_title"] for match in ranked([mexico, spain])], [mexico["display_title"]])

    def test_specialization_cap_provenance_excludes_primary_without_mutating_matcher_fields(self):
        profile = {
            "languages": [],
            "skills": ["Python"],
            "degrees_or_domains": ["software engineering"],
            "summary": "Python software engineer.",
        }
        java = projected("Java Coding Specialist", profile)

        self.assertFalse(java["primary_recommendation_eligible"])
        self.assertEqual(java["actionability_cap_reasons"], ["unsupported_specialization"])
        self.assertEqual(java["score"], 24)
        self.assertEqual(java["match_strength"], "Medium")
        self.assertEqual(java["raw_product_section"], "best_matches")
        self.assertEqual(java["effective_product_section"], "best_matches")
        self.assertEqual(java["preview_section"], "also_worth_reviewing")
        self.assertEqual(ranked([java]), [])

    def test_natural_also_worth_reviewing_remains_admissible(self):
        match = projected("English Language Data Contributor", language_profile())
        match["effective_product_section"] = "also_worth_reviewing"
        match["preview_section"] = "also_worth_reviewing"

        self.assertTrue(match["primary_recommendation_eligible"])
        result = ranked([match])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["presentation_rank"], 1)

    def test_explicit_degree_incompatibility_is_omitted_without_changing_bucket(self):
        profile = language_profile()
        profile["education_level"] = "no_degree"
        profile["constraints"] = ["no college degree"]
        phd = projected("STEM PhD Qualifier Contributor", profile)

        self.assertEqual(phd["preview_section"], "best_matches")
        self.assertFalse(phd["primary_recommendation_eligible"])
        self.assertIn("explicit_credential_incompatibility", phd["actionability_cap_reasons"])
        self.assertEqual(ranked([phd]), [])


if __name__ == "__main__":
    unittest.main()

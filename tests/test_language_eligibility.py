import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from wahojobs.classification import MARKET_COUNT_POLICY_COUNT_LIVE, SOURCE_TIER_CORE
from wahojobs.matching.languages import (
    REQUIREMENT_ALL_REQUIRED,
    REQUIREMENT_ANY_SUPPORTED,
    REQUIREMENT_SINGLE,
    detect_explicit_languages,
    language_eligibility,
    normalize_language_name,
)
from profile_match_digest import rank_opportunities
from product_demo_report import build_explore_market


def profile(profile_id="test", languages=None):
    return {
        "profile_id": profile_id,
        "display_name": "Test Profile",
        "summary": "Test language profile",
        "education_level": "not_specified",
        "degrees_or_domains": ["language"],
        "languages": languages or ["English"],
        "skills": ["translation", "review"],
        "work_preferences": ["remote"],
        "constraints": [],
        "target_opportunity_types": ["language review"],
        "notes": "",
        "avoid_keywords": [],
        "signals": [("Language work signal", ["language", "translation", "bilingual"], 20)],
    }


def row(title, job_id=1):
    return {
        "job_id": job_id,
        "title": title,
        "location": "Remote",
        "url": f"https://example.com/{job_id}",
        "department": "Language",
        "expertise": "Language",
        "commitment": "Freelance",
        "opportunity_kind": "live_posting",
        "availability_basis": "api_feed",
        "include_in_live_market_estimate": 1,
        "canonical_opportunity_id": None,
        "source": "Example",
        "source_slug": "example",
        "source_tier": SOURCE_TIER_CORE,
        "inventory_model": "live_feed",
        "market_count_policy": MARKET_COUNT_POLICY_COUNT_LIVE,
        "canonical_title": None,
        "source_category": "Language",
    }


class LanguageEligibilityTests(unittest.TestCase):
    def test_locale_variants_match_base_language(self):
        self.assertTrue(language_eligibility(profile(languages=["Portuguese"]), "Portuguese (Brazil) reviewer").eligible_for_personalized)
        self.assertTrue(language_eligibility(profile(languages=["Portuguese"]), "Portuguese (Portugal) reviewer").eligible_for_personalized)
        self.assertTrue(language_eligibility(profile(languages=["Spanish"]), "Spanish (Mexico) reviewer").eligible_for_personalized)
        self.assertTrue(language_eligibility(profile(languages=["French"]), "French (Canada) reviewer").eligible_for_personalized)

    def test_aliases_normalize_consistently(self):
        self.assertEqual(normalize_language_name("Mandarin"), "chinese")
        self.assertEqual(normalize_language_name("Chinese"), "chinese")
        self.assertEqual(normalize_language_name("Kiswahili"), "kiswahili")
        self.assertEqual(normalize_language_name("Swahili"), "kiswahili")

    def test_language_pairs_require_both_languages(self):
        french_only = language_eligibility(profile(languages=["French"]), "French to English translation reviewer")
        self.assertEqual(french_only.requirement_mode, REQUIREMENT_ALL_REQUIRED)
        self.assertFalse(french_only.eligible_for_personalized)

        both = language_eligibility(profile(languages=["French", "English"]), "French to English translation reviewer")
        self.assertTrue(both.eligible_for_personalized)

        portuguese_only = language_eligibility(profile(languages=["Portuguese"]), "Portuguese-English bilingual rater")
        self.assertEqual(portuguese_only.requirement_mode, REQUIREMENT_ALL_REQUIRED)
        self.assertFalse(portuguese_only.eligible_for_personalized)

        english_only_translation_pair = language_eligibility(
            profile(languages=["English"]),
            "HT and MTPE - Chinese (Simplified) - English (United States)",
        )
        self.assertEqual(english_only_translation_pair.requirement_mode, REQUIREMENT_ALL_REQUIRED)
        self.assertFalse(english_only_translation_pair.eligible_for_personalized)

    def test_alternatives_accept_any_supported_language(self):
        gujarati = language_eligibility(profile(languages=["Gujarati"]), "Bengali or Gujarati language reviewer")
        self.assertEqual(gujarati.requirement_mode, REQUIREMENT_ANY_SUPPORTED)
        self.assertTrue(gujarati.eligible_for_personalized)

        english_only = language_eligibility(profile(languages=["English"]), "Bengali or Gujarati language reviewer")
        self.assertFalse(english_only.eligible_for_personalized)

        french = language_eligibility(profile(languages=["French"]), "English, French, or German AI writing campaign")
        self.assertEqual(french.requirement_mode, REQUIREMENT_ANY_SUPPORTED)
        self.assertTrue(french.eligible_for_personalized)

    def test_country_and_multilingual_are_not_language_requirements(self):
        self.assertEqual(detect_explicit_languages("Brazil-based AI trainer"), set())
        self.assertEqual(detect_explicit_languages("US-based AI trainer"), set())

        multilingual_profile = profile(languages=["Multilingual"])
        thai = language_eligibility(multilingual_profile, "Thai language reviewer")
        self.assertEqual(thai.requirement_mode, REQUIREMENT_SINGLE)
        self.assertFalse(thai.eligible_for_personalized)

    def test_unsupported_languages_do_not_surface_in_personalized_rankings(self):
        english_profile = profile(languages=["English"])
        rows = [row("Bengali or Gujarati translation reviewer")]
        self.assertEqual(rank_opportunities(english_profile, rows, False, 10), [])

    def test_explore_market_keeps_unsupported_languages_as_broader_market(self):
        english_profile = profile(languages=["English"])
        market = build_explore_market(
            english_profile,
            [row("Bengali or Gujarati translation reviewer")],
            [],
            [],
            {"by_key": {}, "by_source_title": {}, "by_source_near_title": {}},
        )
        self.assertEqual(market["strong_fit"], [])
        self.assertEqual(market["possible_fit"], [])
        self.assertEqual(len(market["broader_market"]), 1)


if __name__ == "__main__":
    unittest.main()

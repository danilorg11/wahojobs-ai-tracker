import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import matching_quality_report as benchmark
from profile_match_digest import score_opportunity
from wahojobs.classification import (
    INVENTORY_MODEL_EVERGREEN_APPLICATION,
    MARKET_COUNT_POLICY_REPORT_SEPARATELY,
    OPPORTUNITY_KIND_EVERGREEN_APPLICATION,
    SOURCE_TIER_CORE,
)


def profile(**overrides):
    payload = {
        "profile_id": "profile",
        "display_name": "Profile",
        "summary": "",
        "education_level": "not_specified",
        "degrees_or_domains": ["generalist"],
        "languages": ["English"],
        "skills": ["web research", "review"],
        "work_preferences": ["remote"],
        "constraints": [],
        "target_opportunity_types": ["AI training", "data annotation"],
        "notes": "",
        "avoid_keywords": [],
        "signals": [],
        "location": "",
        "country": "",
        "residence": "",
        "city": "",
        "region": "",
    }
    payload.update(overrides)
    return payload


def evergreen_row(**overrides):
    payload = {
        "job_id": 1,
        "title": "Evergreen Application",
        "canonical_title": None,
        "source": "Example",
        "source_slug": "example",
        "source_tier": SOURCE_TIER_CORE,
        "location": "Remote",
        "url": "https://example.test/apply",
        "department": "Generalist",
        "expertise": "Generalist",
        "source_category": "Generalist",
        "commitment": "",
        "opportunity_kind": OPPORTUNITY_KIND_EVERGREEN_APPLICATION,
        "availability_basis": "evergreen_page",
        "inventory_model": INVENTORY_MODEL_EVERGREEN_APPLICATION,
        "market_count_policy": MARKET_COUNT_POLICY_REPORT_SEPARATELY,
        "include_in_live_market_estimate": 0,
        "canonical_opportunity_id": None,
        "language": None,
        "language_locale": None,
        "required_languages": None,
    }
    payload.update(overrides)
    return payload


def project(scored):
    raw_label = benchmark.match_strength_from_score(scored["score"])
    return benchmark.project_benchmark_prediction(
        scored["score"],
        raw_label,
        scored["raw_product_section"],
        scored,
        scored["effective_product_section"],
    )


class EvergreenLabelCalibrationTests(unittest.TestCase):
    def test_generalist_evergreen_floors_to_plausible_without_section_promotion(self):
        scored = score_opportunity(profile(), evergreen_row())
        projection = project(scored)

        self.assertEqual(scored["score"], 8)
        self.assertEqual(projection.raw_match_label, "Possible")
        self.assertEqual(projection.evaluation_label, "plausible")
        self.assertEqual(scored["effective_product_section"], "also_worth_reviewing")
        self.assertEqual(projection.evaluation_section, "also_worth_reviewing")
        self.assertTrue(scored["evergreen_label_floor_applied"])

    def test_bilingual_evergreen_floors_for_bilingual_profile_without_required_language_pair(self):
        scored = score_opportunity(
            profile(
                profile_id="translator",
                degrees_or_domains=["language", "translation"],
                languages=["English", "Portuguese"],
                skills=["translation", "language review"],
                target_opportunity_types=["language review"],
            ),
            evergreen_row(
                source_category="Bilingual",
                expertise="Bilingual",
                department="Bilingual",
            ),
        )
        projection = project(scored)

        self.assertTrue(scored["eligible_for_personalized"])
        self.assertEqual(scored["language_requirement_mode"], "none")
        self.assertEqual(projection.evaluation_label, "plausible")
        self.assertEqual(projection.evaluation_section, "also_worth_reviewing")
        self.assertTrue(scored["evergreen_label_floor_applied"])
        self.assertEqual(scored["required_languages"] if "required_languages" in scored else None, None)

    def test_unsupported_required_language_blocks_evergreen_label_floor(self):
        scored = score_opportunity(
            profile(
                degrees_or_domains=["language"],
                languages=["English"],
                skills=["translation"],
                target_opportunity_types=["language review"],
            ),
            evergreen_row(
                source_category="Bilingual",
                expertise="Bilingual",
                department="Catalan",
            ),
        )
        projection = project(scored)

        self.assertFalse(scored["eligible_for_personalized"])
        self.assertFalse(scored["evergreen_label_floor_applied"])
        self.assertEqual(projection.evaluation_label, "false_positive")
        self.assertEqual(projection.evaluation_section, "exclude")

    def test_specialized_evergreen_domain_page_does_not_get_broad_label_floor(self):
        scored = score_opportunity(
            profile(),
            evergreen_row(
                title="Medicine Expert / Medical AI Trainer",
                source_category="Medicine",
                expertise="Medicine",
                department="Medicine",
            ),
        )
        projection = project(scored)

        self.assertFalse(scored["evergreen_applicability_qualifies"])
        self.assertFalse(scored["evergreen_label_floor_applied"])
        self.assertEqual(projection.evaluation_label, "weak")

    def test_location_incompatible_evergreen_does_not_get_label_floor(self):
        scored = score_opportunity(
            profile(country="Brazil"),
            evergreen_row(location="Remote - United States"),
        )
        projection = project(scored)

        self.assertFalse(scored["evergreen_label_floor_applied"])
        self.assertEqual(scored["location_eligibility_status"], "incompatible")
        self.assertEqual(projection.evaluation_label, "weak")


if __name__ == "__main__":
    unittest.main()

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
    MARKET_COUNT_POLICY_COUNT_LIVE,
    MARKET_COUNT_POLICY_REPORT_SEPARATELY,
    SOURCE_TIER_CORE,
)


def profile(**overrides):
    payload = {
        "profile_id": "profile",
        "display_name": "Profile",
        "summary": "",
        "education_level": "not_specified",
        "degrees_or_domains": [],
        "languages": ["English"],
        "skills": [],
        "work_preferences": ["remote"],
        "constraints": [],
        "target_opportunity_types": [],
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


def row(**overrides):
    payload = {
        "job_id": 1,
        "title": "Opportunity",
        "canonical_title": None,
        "source": "Example",
        "source_slug": "example",
        "source_tier": SOURCE_TIER_CORE,
        "location": "Remote",
        "url": "https://example.test/jobs/1",
        "department": "General",
        "expertise": "General",
        "source_category": "General",
        "commitment": "",
        "opportunity_kind": "live_posting",
        "availability_basis": "api_feed",
        "inventory_model": "live_feed",
        "market_count_policy": MARKET_COUNT_POLICY_COUNT_LIVE,
        "include_in_live_market_estimate": 1,
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


class DirectDomainLabelCalibrationTests(unittest.TestCase):
    def test_law_metadata_floors_direct_legal_match_to_strong(self):
        scored = score_opportunity(
            profile(
                degrees_or_domains=["law", "legal"],
                skills=["legal reasoning", "contracts", "expert review"],
                target_opportunity_types=["legal AI training", "expert review"],
                signals=[
                    ("Legal domain match", ["legal", "law", "lawyer", "attorney"], 14),
                    ("Expert review signal", ["expert", "review", "reasoning"], 7),
                ],
            ),
            row(
                title="IP Expert",
                source_category="Law",
                expertise="Intellectual Property",
                department="Law",
            ),
        )
        projection = project(scored)

        self.assertEqual(scored["score"], 30)
        self.assertEqual(projection.raw_match_label, "Medium")
        self.assertEqual(projection.evaluation_label, "strong")
        self.assertEqual(projection.evaluation_section, "best_matches")
        self.assertTrue(scored["direct_domain_label_floor_applied"])
        self.assertTrue(scored["specialized_actionability_cap_applied"])

    def test_science_medical_metadata_floors_domain_match_to_strong(self):
        cases = [
            ("Microbiology Specialist", "Sciences/Medical", "Microbiology", "Sciences/Medical", 23, "best_matches"),
            ("Academic Dermatologist", "Healthcare & Medical", "Dermatology", "Healthcare & Medical", 30, "best_matches"),
            ("Medicine Physician", "Healthcare & Medical", "Medicine / Physician", "Healthcare & Medical", 23, "best_matches"),
        ]
        for title, source_category, expertise, department, expected_score, expected_section in cases:
            with self.subTest(title=title):
                scored = score_opportunity(
                    profile(
                        degrees_or_domains=["biology", "medicine", "life sciences"],
                        skills=["scientific writing", "medical review", "biology"],
                        target_opportunity_types=["STEM expert AI training", "medical AI review"],
                        signals=[
                            ("Biology/life sciences match", ["biology", "life science", "genetics", "biomedical"], 14),
                            ("Medicine/clinical match", ["medicine", "medical", "clinical", "healthcare"], 14),
                            ("Academic/expert signal", ["phd", "academic", "expert", "scientist"], 7),
                        ],
                    ),
                    row(
                        title=title,
                        source_category=source_category,
                        expertise=expertise,
                        department=department,
                    ),
                )
                projection = project(scored)

                self.assertEqual(scored["score"], expected_score)
                self.assertEqual(projection.evaluation_label, "strong")
                self.assertEqual(projection.evaluation_section, expected_section)
                self.assertTrue(scored["direct_domain_label_floor_applied"])

    def test_generic_software_profile_does_not_get_science_domain_floor(self):
        scored = score_opportunity(
            profile(
                degrees_or_domains=["software", "coding", "python"],
                skills=["python", "software engineering", "code review", "testing"],
                target_opportunity_types=["coding evaluation", "technical review"],
                signals=[
                    ("Coding/technical task match", ["coding", "software", "developer", "programming"], 12),
                    ("Python match", ["python"], 10),
                ],
            ),
            row(
                title="Scientific Coding - Biology and Python",
                source_category="Science",
                expertise="Science",
                department="Science",
            ),
        )
        projection = project(scored)

        self.assertEqual(scored["score"], 31)
        self.assertEqual(projection.evaluation_label, "plausible")
        self.assertEqual(projection.evaluation_section, "also_worth_reviewing")
        self.assertFalse(scored["direct_domain_label_floor_applied"])

    def test_humanities_profile_does_not_get_medical_domain_floor(self):
        scored = score_opportunity(
            profile(
                degrees_or_domains=["history", "humanities"],
                skills=["academic research", "source evaluation"],
                target_opportunity_types=["research evaluation"],
                signals=[
                    ("Research/humanities signal", ["history", "historian", "research", "humanities", "academic"], 10),
                ],
            ),
            row(
                title="Academic Dermatologist",
                source_category="Healthcare & Medical",
                expertise="Dermatology",
                department="Healthcare & Medical",
            ),
        )
        projection = project(scored)

        self.assertEqual(scored["score"], 0)
        self.assertEqual(projection.evaluation_label, "false_positive")
        self.assertEqual(projection.evaluation_section, "exclude")
        self.assertFalse(scored["direct_domain_label_floor_applied"])
        self.assertTrue(scored["professional_domain_hard_gate_applied"])

    def test_broad_evergreen_application_does_not_get_domain_floor(self):
        scored = score_opportunity(
            profile(
                degrees_or_domains=["law", "legal"],
                skills=["legal reasoning", "contracts"],
                target_opportunity_types=["legal AI training"],
                signals=[("Legal domain match", ["legal", "law", "lawyer", "attorney"], 14)],
            ),
            row(
                title="Law Expert / Legal AI Trainer",
                source_category="Law",
                expertise="Law",
                department="Law",
                opportunity_kind="evergreen_application",
                availability_basis="evergreen_page",
                inventory_model=INVENTORY_MODEL_EVERGREEN_APPLICATION,
                market_count_policy=MARKET_COUNT_POLICY_REPORT_SEPARATELY,
                include_in_live_market_estimate=0,
            ),
        )
        projection = project(scored)

        self.assertEqual(projection.evaluation_label, "weak")
        self.assertFalse(scored["direct_domain_label_floor_applied"])


if __name__ == "__main__":
    unittest.main()

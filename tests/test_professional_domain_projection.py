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
    OPPORTUNITY_KIND_EVERGREEN_APPLICATION,
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


def humanities_profile():
    return profile(
        degrees_or_domains=["history", "humanities"],
        skills=["academic research", "source evaluation"],
        target_opportunity_types=["research evaluation"],
        signals=[
            ("Research/humanities signal", ["history", "historian", "research", "humanities", "academic"], 10),
        ],
    )


def science_profile():
    return profile(
        degrees_or_domains=["biology", "medicine", "life sciences"],
        skills=["scientific writing", "medical review", "biology"],
        target_opportunity_types=["STEM expert AI training", "medical AI review"],
        signals=[
            ("Biology/life sciences match", ["biology", "life science", "genetics", "biomedical"], 14),
            ("Medicine/clinical match", ["medicine", "medical", "clinical", "healthcare"], 14),
            ("Academic/expert signal", ["phd", "academic", "expert", "scientist"], 7),
        ],
    )


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


class ProfessionalDomainProjectionTests(unittest.TestCase):
    def test_humanities_profile_medical_specialist_projects_to_false_positive(self):
        scored = score_opportunity(
            humanities_profile(),
            row(
                title="Academic Dermatologist",
                source_category="Healthcare & Medical",
                expertise="Dermatology",
                department="Healthcare & Medical",
            ),
        )
        projection = project(scored)

        self.assertEqual(scored["score"], 0)
        self.assertTrue(scored["professional_domain_hard_gate_applied"])
        self.assertEqual(projection.evaluation_label, "false_positive")
        self.assertEqual(projection.evaluation_section, "exclude")
        self.assertEqual(projection.hard_gate_type, "professional_domain")

    def test_science_profile_medical_specialist_remains_visible(self):
        scored = score_opportunity(
            science_profile(),
            row(
                title="Academic Dermatologist",
                source_category="Healthcare & Medical",
                expertise="Dermatology",
                department="Healthcare & Medical",
            ),
        )
        projection = project(scored)

        self.assertFalse(scored["professional_domain_hard_gate_applied"])
        self.assertEqual(projection.evaluation_label, "strong")
        self.assertEqual(projection.evaluation_section, "best_matches")

    def test_generic_academic_research_role_is_not_excluded(self):
        scored = score_opportunity(
            humanities_profile(),
            row(
                title="Academic Research Evaluator",
                source_category="Research",
                expertise="Research",
                department="Research",
            ),
        )
        projection = project(scored)

        self.assertFalse(scored["professional_domain_hard_gate_applied"])
        self.assertNotEqual(projection.evaluation_label, "false_positive")
        self.assertNotEqual(projection.evaluation_section, "exclude")

    def test_search_rater_and_data_labeling_are_not_domain_excluded(self):
        scored = score_opportunity(
            humanities_profile(),
            row(
                title="Remote Internet Search Quality Rater",
                source_category="Data Validation",
                expertise="Data Validation",
                department="Data Validation",
            ),
        )
        projection = project(scored)

        self.assertFalse(scored["professional_domain_hard_gate_applied"])
        self.assertNotEqual(projection.evaluation_label, "false_positive")
        self.assertNotEqual(projection.evaluation_section, "exclude")

    def test_evergreen_applications_are_not_domain_excluded(self):
        scored = score_opportunity(
            humanities_profile(),
            row(
                title="Medicine Expert / Medical AI Trainer",
                source_category="Medicine",
                expertise="Medicine",
                department="Medicine",
                opportunity_kind=OPPORTUNITY_KIND_EVERGREEN_APPLICATION,
                availability_basis="evergreen_page",
                inventory_model=INVENTORY_MODEL_EVERGREEN_APPLICATION,
                market_count_policy=MARKET_COUNT_POLICY_REPORT_SEPARATELY,
                include_in_live_market_estimate=0,
            ),
        )
        projection = project(scored)

        self.assertFalse(scored["professional_domain_hard_gate_applied"])
        self.assertNotEqual(projection.evaluation_label, "false_positive")
        self.assertNotEqual(projection.evaluation_section, "exclude")


if __name__ == "__main__":
    unittest.main()

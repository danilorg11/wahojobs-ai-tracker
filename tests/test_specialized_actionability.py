import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from profile_match_digest import score_opportunity
from wahojobs.classification import MARKET_COUNT_POLICY_COUNT_LIVE, SOURCE_TIER_CORE


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


class SpecializedActionabilityTests(unittest.TestCase):
    def test_specialized_legal_role_caps_to_best_matches_without_subtype(self):
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

        self.assertEqual(scored["score"], 30)
        self.assertEqual(scored["raw_product_section"], "do_these_first")
        self.assertEqual(scored["effective_product_section"], "best_matches")
        self.assertTrue(scored["specialized_actionability_cap_applied"])

    def test_explicit_legal_subtype_is_not_capped(self):
        scored = score_opportunity(
            profile(
                degrees_or_domains=["law", "legal"],
                skills=["intellectual property", "legal reasoning", "expert review"],
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

        self.assertEqual(scored["score"], 30)
        self.assertEqual(scored["raw_product_section"], "do_these_first")
        self.assertEqual(scored["effective_product_section"], "do_these_first")
        self.assertFalse(scored["specialized_actionability_cap_applied"])

    def test_credential_sensitive_medical_role_caps_to_best_matches(self):
        scored = score_opportunity(
            profile(
                degrees_or_domains=["biology", "medicine", "life sciences"],
                skills=["scientific writing", "medical review", "biology"],
                target_opportunity_types=["STEM expert AI training", "medical AI review"],
                signals=[
                    ("Medicine/clinical match", ["medicine", "medical", "clinical", "healthcare"], 14),
                    ("Academic/expert signal", ["phd", "academic", "expert", "scientist"], 7),
                ],
            ),
            row(
                title="Academic Dermatologist",
                source_category="Healthcare & Medical",
                expertise="Dermatology",
                department="Healthcare & Medical",
            ),
        )

        self.assertEqual(scored["score"], 30)
        self.assertEqual(scored["raw_product_section"], "do_these_first")
        self.assertEqual(scored["effective_product_section"], "best_matches")
        self.assertTrue(scored["specialized_actionability_cap_applied"])

    def test_voice_audio_language_role_caps_to_best_matches(self):
        scored = score_opportunity(
            profile(
                degrees_or_domains=["language", "translation", "linguistics"],
                languages=["English", "French"],
                skills=["translation", "localization", "language review", "linguistics"],
                target_opportunity_types=["language review", "translation evaluation"],
                signals=[
                    ("Language/linguistics match", ["language", "linguistic", "linguistics"], 12),
                    ("French language match", ["french"], 8),
                ],
            ),
            row(
                title="French Voice/Audio AI Data Roles",
                source_category="Language Data",
                expertise="French Voice/Audio AI Data",
                department="Language/Audio",
                language="French",
                required_languages="French",
            ),
        )

        self.assertEqual(scored["score"], 35)
        self.assertEqual(scored["raw_product_section"], "do_these_first")
        self.assertEqual(scored["effective_product_section"], "best_matches")
        self.assertTrue(scored["specialized_actionability_cap_applied"])

    def test_cross_domain_scientific_coding_caps_to_also_worth_reviewing(self):
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

        self.assertEqual(scored["score"], 31)
        self.assertEqual(scored["raw_product_section"], "do_these_first")
        self.assertEqual(scored["effective_product_section"], "also_worth_reviewing")
        self.assertTrue(scored["specialized_actionability_cap_applied"])

    def test_clear_broad_direct_match_is_not_capped(self):
        scored = score_opportunity(
            profile(
                degrees_or_domains=["English", "education", "teaching"],
                skills=["teaching", "writing feedback", "content review", "grammar"],
                target_opportunity_types=["AI writing evaluation", "AI training", "content review"],
                signals=[
                    ("Teaching/writing/review signal", ["teacher", "teaching", "education", "writing", "review", "content"], 9),
                    ("English writing/content review signal", ["english writing", "content reviewing"], 12),
                ],
            ),
            row(
                title="English Writing and Content Reviewing Expertise Sought for AI Training",
                source_category="Generalist",
                expertise="Generalist",
                department="Generalist",
            ),
        )

        self.assertEqual(scored["score"], 36)
        self.assertEqual(scored["raw_product_section"], "do_these_first")
        self.assertEqual(scored["effective_product_section"], "do_these_first")
        self.assertFalse(scored["specialized_actionability_cap_applied"])


if __name__ == "__main__":
    unittest.main()

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import matching_quality_report as benchmark
from product_demo_report import match_strength_from_score
from profile_match_digest import product_section_for_score, score_opportunity
from wahojobs.classification import (
    INVENTORY_MODEL_EVERGREEN_APPLICATION,
    INVENTORY_MODEL_LIVE_FEED,
    MARKET_COUNT_POLICY_COUNT_LIVE,
    MARKET_COUNT_POLICY_REPORT_SEPARATELY,
    OPPORTUNITY_KIND_EVERGREEN_APPLICATION,
    OPPORTUNITY_KIND_LIVE_POSTING,
    SOURCE_TIER_CORE,
)
from wahojobs.db.connection import get_connection
from wahojobs.matching.evergreen import EVERGREEN_REASON


def profile(**overrides):
    payload = {
        "profile_id": "generalist",
        "display_name": "Generalist",
        "summary": "Wants broad remote AI training work",
        "education_level": "not_specified",
        "degrees_or_domains": ["generalist"],
        "languages": ["English"],
        "skills": ["web research", "review"],
        "work_preferences": ["remote", "flexible"],
        "constraints": [],
        "target_opportunity_types": ["AI training", "data annotation"],
        "notes": "",
        "avoid_keywords": [],
        "signals": [("Generalist AI-work signal", ["generalist"], 9)],
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
        "title": "Broad Application Pool",
        "location": "Remote",
        "url": "https://example.com/application",
        "department": "Generalist",
        "expertise": "Generalist",
        "commitment": None,
        "opportunity_kind": OPPORTUNITY_KIND_EVERGREEN_APPLICATION,
        "availability_basis": "evergreen_page",
        "include_in_live_market_estimate": 0,
        "canonical_opportunity_id": None,
        "source": "Example",
        "source_slug": "example",
        "source_tier": SOURCE_TIER_CORE,
        "inventory_model": INVENTORY_MODEL_EVERGREEN_APPLICATION,
        "market_count_policy": MARKET_COUNT_POLICY_REPORT_SEPARATELY,
        "canonical_title": None,
        "source_category": "Generalist",
    }
    payload.update(overrides)
    return payload


def projected(scored):
    raw_label = match_strength_from_score(scored["score"])
    return benchmark.project_benchmark_prediction(
        scored["score"],
        raw_label,
        scored["raw_product_section"],
        scored,
        scored["effective_product_section"],
    )


def committed_human_reviewed_raw_predictions():
    temp_root = Path(tempfile.mkdtemp(prefix="wahojobs_evergreen_test_"))
    try:
        for rel in ("scripts/profile_match_digest.py", "scripts/matching_quality_report.py"):
            content = subprocess.check_output(["git", "show", f"HEAD:{rel}"], cwd=ROOT).decode("utf-8")
            target = temp_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        fixture = benchmark.load_fixture()
        profiles = benchmark.load_benchmark_profiles(fixture)
        profiles_path = temp_root / "profiles.json"
        profiles_path.write_text(json.dumps(profiles), encoding="utf-8")

        code = r"""
import json
import sys
from pathlib import Path

temp_root = Path(sys.argv[1])
root = Path(sys.argv[2])
profiles_path = Path(sys.argv[3])
sys.path.insert(0, str(temp_root / "scripts"))
sys.path.insert(1, str(root / "scripts"))
sys.path.insert(2, str(root))

import matching_quality_report as benchmark
from product_demo_report import match_strength_from_score
from wahojobs.db.connection import get_connection

benchmark.FIXTURE_PATH = root / "tests" / "fixtures" / "matching_golden_set.json"
fixture = benchmark.load_fixture()
profiles = json.loads(profiles_path.read_text(encoding="utf-8"))
with get_connection() as conn:
    rows = [benchmark.row_to_dict(row) for row in benchmark.matcher.get_active_rows(conn)]

result = {}
for case in fixture["cases"]:
    if case.get("label_source") != "human_reviewed":
        continue
    item = benchmark.evaluate_case(case, profiles[case["profile_id"]], rows, benchmark.matcher)
    result[case["case_id"]] = {
        "score": item.score,
        "raw_match_label": item.raw_match_label,
        "raw_section": item.raw_section,
    }
print(json.dumps(result, sort_keys=True))
"""
        output = subprocess.check_output(
            [sys.executable, "-c", code, str(temp_root), str(ROOT), str(profiles_path)],
            cwd=ROOT,
        ).decode("utf-8")
        return json.loads(output)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


class EvergreenActionabilityTests(unittest.TestCase):
    def test_broad_generalist_evergreen_floor_preserves_score_and_label(self):
        scored = score_opportunity(profile(), row())

        self.assertEqual(scored["score"], 17)
        self.assertEqual(match_strength_from_score(scored["score"]), "Possible")
        self.assertEqual(scored["raw_product_section"], "explore_only")
        self.assertEqual(scored["evergreen_adjusted_section"], "also_worth_reviewing")
        self.assertEqual(scored["effective_product_section"], "also_worth_reviewing")
        self.assertTrue(scored["evergreen_floor_applied"])
        self.assertTrue(scored["evergreen_visible_reason_added"])
        self.assertEqual(scored["reasons"].count(EVERGREEN_REASON), 1)

    def test_broad_bilingual_evergreen_with_supported_required_languages_gets_floor(self):
        scored = score_opportunity(
            profile(
                profile_id="translator",
                degrees_or_domains=["language"],
                languages=["English", "Portuguese"],
                skills=["translation"],
                target_opportunity_types=["language review"],
                signals=[],
            ),
            row(
                location="",
                source_category="Language",
                expertise="Language",
                department="Portuguese English translation",
            ),
        )

        self.assertTrue(scored["eligible_for_personalized"])
        self.assertEqual(scored["language_requirement_mode"], "all_required")
        self.assertEqual(scored["evergreen_opportunity_kind"], "broad_language")
        self.assertTrue(scored["evergreen_floor_applied"])
        self.assertEqual(scored["effective_product_section"], "also_worth_reviewing")

    def test_bilingual_evergreen_with_unsupported_language_keeps_hard_gate(self):
        scored = score_opportunity(
            profile(languages=["English"], degrees_or_domains=["language"], skills=["translation"], signals=[]),
            row(source_category="Bilingual", expertise="Bilingual", department="Catalan"),
        )
        projection = projected(scored)

        self.assertFalse(scored["eligible_for_personalized"])
        self.assertFalse(scored["evergreen_floor_applied"])
        self.assertEqual(projection.evaluation_label, "false_positive")
        self.assertEqual(projection.evaluation_section, "exclude")
        self.assertEqual(projection.hard_gate_type, "language")

    def test_ambiguous_language_evergreen_is_not_affirmative_compatibility(self):
        scored = score_opportunity(
            profile(languages=["English"], degrees_or_domains=["language"], skills=["translation"], signals=[]),
            row(source_category="Language", expertise="Language", department="English, French, German"),
        )

        self.assertTrue(scored["eligible_for_personalized"])
        self.assertEqual(scored["language_requirement_mode"], "ambiguous")
        self.assertFalse(scored["evergreen_floor_applied"])
        self.assertEqual(scored["effective_product_section"], "explore_only")

    def test_non_evergreen_generalist_opportunity_gets_no_floor(self):
        scored = score_opportunity(
            profile(signals=[]),
            row(
                inventory_model=INVENTORY_MODEL_LIVE_FEED,
                market_count_policy=MARKET_COUNT_POLICY_COUNT_LIVE,
                opportunity_kind=OPPORTUNITY_KIND_LIVE_POSTING,
                include_in_live_market_estimate=1,
            ),
        )

        self.assertFalse(scored["evergreen_floor_applied"])
        self.assertEqual(scored["evergreen_adjusted_section"], scored["raw_product_section"])

    def test_specialist_evergreen_gets_no_floor_solely_because_it_is_evergreen(self):
        scored = score_opportunity(
            profile(),
            row(source_category="Medical Experts", expertise="Medical Experts", department="Medical Experts"),
        )

        self.assertEqual(scored["evergreen_opportunity_kind"], "specialist")
        self.assertFalse(scored["evergreen_floor_applied"])
        self.assertEqual(scored["effective_product_section"], scored["raw_product_section"])

    def test_evergreen_already_at_floor_gets_no_new_visible_reason(self):
        scored = score_opportunity(
            profile(signals=[("Generalist AI-work signal", ["generalist"], 10)]),
            row(),
        )

        self.assertEqual(scored["raw_product_section"], "also_worth_reviewing")
        self.assertEqual(scored["evergreen_adjusted_section"], "also_worth_reviewing")
        self.assertFalse(scored["evergreen_floor_applied"])
        self.assertFalse(scored["evergreen_visible_reason_added"])
        self.assertNotIn(EVERGREEN_REASON, scored["reasons"])

    def test_evergreen_already_best_match_gets_no_visible_reason(self):
        scored = score_opportunity(
            profile(signals=[("Generalist AI-work signal", ["generalist"], 20)]),
            row(),
        )

        self.assertEqual(scored["raw_product_section"], "best_matches")
        self.assertEqual(scored["evergreen_adjusted_section"], "best_matches")
        self.assertFalse(scored["evergreen_floor_applied"])
        self.assertNotIn(EVERGREEN_REASON, scored["reasons"])

    def test_evergreen_floor_loses_to_location_actionability_cap(self):
        scored = score_opportunity(profile(), row(location="United States"))
        projection = projected(scored)

        self.assertEqual(scored["score"], 12)
        self.assertEqual(match_strength_from_score(scored["score"]), "Possible")
        self.assertEqual(scored["raw_product_section"], "explore_only")
        self.assertEqual(scored["evergreen_adjusted_section"], "also_worth_reviewing")
        self.assertEqual(scored["effective_product_section"], "explore_only")
        self.assertTrue(scored["evergreen_floor_applied"])
        self.assertTrue(scored["location_actionability_cap_applied"])
        self.assertFalse(scored["evergreen_visible_reason_added"])
        self.assertNotIn(EVERGREEN_REASON, scored["reasons"])
        self.assertIn("Location eligibility could not be confirmed", " ".join(scored["reasons"]))
        self.assertEqual(projection.evaluation_label, "weak")
        self.assertEqual(projection.evaluation_section, "explore_only")
        self.assertEqual(projection.hard_gate_type, "")

    def test_missing_inventory_classification_gets_no_floor(self):
        scored = score_opportunity(profile(), row(inventory_model="unknown"))

        self.assertFalse(scored["evergreen_floor_applied"])
        self.assertEqual(scored["effective_product_section"], scored["raw_product_section"])

    def test_raw_section_thresholds_are_unchanged(self):
        cases = [
            (17, "explore_only"),
            (18, "also_worth_reviewing"),
            (21, "also_worth_reviewing"),
            (22, "best_matches"),
            (29, "best_matches"),
            (30, "do_these_first"),
        ]
        for score, expected in cases:
            with self.subTest(score=score):
                self.assertEqual(product_section_for_score(score), expected)

    def test_human_reviewed_raw_predictions_match_committed_head(self):
        committed = committed_human_reviewed_raw_predictions()
        fixture = benchmark.load_fixture()
        profiles = benchmark.load_benchmark_profiles(fixture)
        with get_connection() as conn:
            rows = [benchmark.row_to_dict(row) for row in benchmark.matcher.get_active_rows(conn)]

        for case in fixture["cases"]:
            if case.get("label_source") != "human_reviewed":
                continue
            current = benchmark.evaluate_case(case, profiles[case["profile_id"]], rows, benchmark.matcher)
            self.assertEqual(
                {
                    "score": current.score,
                    "raw_match_label": current.raw_match_label,
                    "raw_section": current.raw_section,
                },
                committed[case["case_id"]],
                case["case_id"],
            )


if __name__ == "__main__":
    unittest.main()

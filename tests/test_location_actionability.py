import sys
import json
import shutil
import subprocess
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
from profile_match_digest import product_section_for_score, rank_opportunities, score_opportunity
from product_demo_report import build_explore_market
from wahojobs.classification import MARKET_COUNT_POLICY_COUNT_LIVE, SOURCE_TIER_CORE
from wahojobs.db.connection import get_connection
from wahojobs.matching.locations import LOCATION_UNKNOWN


def profile(**overrides):
    payload = {
        "profile_id": "generalist",
        "display_name": "Generalist",
        "summary": "Remote data annotation profile",
        "education_level": "not_specified",
        "degrees_or_domains": ["generalist"],
        "languages": ["English"],
        "skills": ["annotation", "review"],
        "work_preferences": ["remote", "flexible"],
        "constraints": [],
        "target_opportunity_types": ["data annotation"],
        "notes": "",
        "avoid_keywords": [],
        "signals": [("Annotation signal", ["annotation"], 30)],
        "location": "",
        "country": "",
        "residence": "",
        "city": "",
        "region": "",
    }
    payload.update(overrides)
    return payload


def row(title="Social Media Annotation", location="Remote", job_id=1):
    return {
        "job_id": job_id,
        "title": title,
        "location": location,
        "url": f"https://example.com/{job_id}",
        "department": "Data Annotation",
        "expertise": "Data Annotation",
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
        "source_category": "Data Annotation",
    }


def project(scored):
    raw_label = benchmark.match_strength_from_score(scored["score"])
    return benchmark.project_benchmark_prediction(
        scored["score"],
        raw_label,
        scored["raw_product_section"],
        scored,
        scored["effective_product_section"],
    )


def committed_human_reviewed_predictions():
    temp_root = Path(tempfile.mkdtemp(prefix="wahojobs_matcher_test_"))
    try:
        for rel in (
            "scripts/profile_match_digest.py",
            "scripts/matching_quality_report.py",
            "scripts/product_demo_report.py",
        ):
            content = subprocess.check_output(["git", "show", f"a9d85ff:{rel}"], cwd=ROOT).decode("utf-8")
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
        "label": item.evaluation_label,
        "section": item.evaluation_section,
        "raw_match_label": item.raw_match_label,
        "raw_section": item.raw_section,
        "score": item.score,
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


class LocationActionabilityTests(unittest.TestCase):
    def test_raw_section_threshold_boundaries_match_committed_baseline(self):
        cases = [
            (17, "explore_only"),
            (18, "also_worth_reviewing"),
            (21, "also_worth_reviewing"),
            (22, "best_matches"),
            (23, "best_matches"),
            (29, "best_matches"),
            (30, "do_these_first"),
        ]
        for score, expected in cases:
            with self.subTest(score=score):
                self.assertEqual(product_section_for_score(score), expected)

    def test_human_reviewed_raw_predictions_match_committed_a9d85ff(self):
        committed = committed_human_reviewed_predictions()
        fixture = benchmark.load_fixture()
        profiles = benchmark.load_benchmark_profiles(fixture)
        with get_connection() as conn:
            rows = [benchmark.row_to_dict(row) for row in benchmark.matcher.get_active_rows(conn)]

        for case in fixture["cases"]:
            if case.get("label_source") != "human_reviewed":
                continue
            current = benchmark.evaluate_case(case, profiles[case["profile_id"]], rows, benchmark.matcher)
            label = "false_positive" if current.hard_gate_type else benchmark.label_from_raw_match_label(current.raw_match_label)
            section = "exclude" if current.hard_gate_type else current.raw_section
            expected = committed[case["case_id"]]
            self.assertEqual(
                {
                    "label": label,
                    "section": section,
                    "raw_match_label": current.raw_match_label,
                    "raw_section": current.raw_section,
                    "score": current.score,
                },
                expected,
                case["case_id"],
            )

    def test_explicit_restriction_unknown_profile_caps_section_without_score_penalty(self):
        scored = score_opportunity(profile(), row(location="United States"))

        self.assertEqual(scored["score"], 39)
        self.assertEqual(scored["raw_product_section"], "do_these_first")
        self.assertEqual(scored["effective_product_section"], "explore_only")
        self.assertEqual(scored["location_eligibility_status"], LOCATION_UNKNOWN)
        self.assertEqual(scored["location_restriction_type"], "concrete")
        self.assertEqual(scored["profile_location_status"], "unknown")
        self.assertTrue(scored["location_actionability_cap_required"])
        self.assertTrue(scored["location_actionability_cap_applied"])
        self.assertIn("Location eligibility could not be confirmed", scored["location_eligibility_reason"])
        self.assertIn(scored["location_eligibility_reason"], scored["reasons"])

    def test_matching_known_profile_location_is_not_capped(self):
        scored = score_opportunity(profile(country="United States"), row(location="United States"))

        self.assertEqual(scored["score"], 39)
        self.assertEqual(scored["raw_product_section"], "do_these_first")
        self.assertEqual(scored["effective_product_section"], "do_these_first")
        self.assertEqual(scored["location_eligibility_status"], "eligible")
        self.assertEqual(scored["location_restriction_type"], "concrete")
        self.assertFalse(scored["location_actionability_cap_required"])
        self.assertFalse(scored["location_actionability_cap_applied"])
        self.assertNotIn(scored["location_eligibility_reason"], scored["reasons"])

    def test_selected_locations_is_opaque_unknown_and_never_incompatible_by_itself(self):
        scored = score_opportunity(profile(country="United States"), row(location="Selected Locations"))

        self.assertEqual(scored["location_restriction_type"], "opaque")
        self.assertEqual(scored["location_eligibility_status"], "unknown")
        self.assertTrue(scored["location_actionability_cap_required"])
        self.assertTrue(scored["location_actionability_cap_applied"])
        self.assertEqual(scored["effective_product_section"], "explore_only")

    def test_onsite_or_hybrid_without_allowed_location_is_opaque(self):
        for location in ("hybrid", "On site"):
            with self.subTest(location=location):
                scored = score_opportunity(profile(country="United States"), row(location=location))
                self.assertEqual(scored["location_restriction_type"], "opaque")
                self.assertEqual(scored["location_eligibility_status"], "unknown")
                self.assertTrue(scored["location_actionability_cap_required"])

    def test_worldwide_opportunity_is_not_capped_with_unknown_profile_location(self):
        scored = score_opportunity(profile(), row(location="Worldwide"))

        self.assertEqual(scored["raw_product_section"], "do_these_first")
        self.assertEqual(scored["effective_product_section"], "do_these_first")
        self.assertEqual(scored["location_eligibility_status"], "not_applicable")
        self.assertEqual(scored["location_restriction_type"], "none")
        self.assertFalse(scored["location_actionability_cap_applied"])
        self.assertNotIn(scored["location_eligibility_reason"], scored["reasons"])

    def test_missing_profile_location_without_explicit_restriction_is_not_capped(self):
        scored = score_opportunity(profile(), row(location="Remote"))

        self.assertEqual(scored["raw_product_section"], "do_these_first")
        self.assertEqual(scored["effective_product_section"], "do_these_first")
        self.assertFalse(scored["location_actionability_cap_applied"])
        self.assertNotIn(scored["location_eligibility_reason"], scored["reasons"])

    def test_blank_location_has_no_cap_or_uncertainty_reason(self):
        for location in ("", None):
            with self.subTest(location=location):
                scored = score_opportunity(profile(), row(location=location))
                self.assertEqual(scored["location_restriction_type"], "none")
                self.assertFalse(scored["location_actionability_cap_required"])
                self.assertFalse(scored["location_actionability_cap_applied"])
                self.assertNotIn(scored["location_eligibility_reason"], scored["reasons"])

    def test_already_explore_only_restricted_location_has_no_visible_reason(self):
        scored = score_opportunity(
            profile(signals=[("Weak signal", ["unlikely-keyword"], 1)]),
            row(title="Unrelated Role", location="United States"),
        )

        self.assertEqual(scored["raw_product_section"], "explore_only")
        self.assertEqual(scored["effective_product_section"], "explore_only")
        self.assertTrue(scored["location_actionability_cap_required"])
        self.assertFalse(scored["location_actionability_cap_applied"])
        self.assertEqual(scored["location_eligibility_status"], "unknown")
        self.assertNotIn(scored["location_eligibility_reason"], scored["reasons"])

    def test_language_information_does_not_establish_profile_location(self):
        scored = score_opportunity(
            profile(languages=["Portuguese"]),
            row(title="Portuguese Annotation", location="Brazil"),
        )

        self.assertEqual(scored["profile_location_status"], "unknown")
        self.assertEqual(scored["location_eligibility_status"], "unknown")
        self.assertTrue(scored["location_actionability_cap_required"])
        self.assertTrue(scored["location_actionability_cap_applied"])

    def test_unknown_location_cap_does_not_project_to_false_positive(self):
        scored = score_opportunity(profile(), row(location="United States"))
        projection = project(scored)

        self.assertEqual(projection.raw_score, scored["score"])
        self.assertEqual(projection.raw_match_label, "Strong")
        self.assertEqual(projection.raw_section, "do_these_first")
        self.assertEqual(projection.evaluation_label, "strong")
        self.assertEqual(projection.evaluation_section, "explore_only")
        self.assertEqual(projection.hard_gate_type, "")

    def test_existing_decisive_language_hard_gate_is_unchanged(self):
        scored = score_opportunity(
            profile(languages=["English"]),
            row(title="Catalan Translation Annotation", location="Remote"),
        )
        projection = project(scored)

        self.assertEqual(projection.evaluation_label, "false_positive")
        self.assertEqual(projection.evaluation_section, "exclude")
        self.assertEqual(projection.hard_gate_type, "language")

    def test_eligible_case_mapping_is_unchanged(self):
        scored = score_opportunity(profile(), row(location="Remote"))
        projection = project(scored)

        self.assertEqual(projection.raw_score, scored["score"])
        self.assertEqual(projection.raw_section, scored["raw_product_section"])
        self.assertEqual(projection.evaluation_section, scored["effective_product_section"])
        self.assertEqual(projection.evaluation_label, "strong")

    def test_personalized_rankings_skip_capped_rows_but_explore_keeps_them_broad(self):
        capped = row(location="United States")
        self.assertEqual(rank_opportunities(profile(), [capped], False, 10), [])

        market = build_explore_market(
            profile(),
            [capped],
            [],
            [],
            {"by_key": {}, "by_source_title": {}, "by_source_near_title": {}},
        )
        self.assertEqual(market["strong_fit"], [])
        self.assertEqual(market["possible_fit"], [])
        self.assertEqual(len(market["broader_market"]), 1)


if __name__ == "__main__":
    unittest.main()

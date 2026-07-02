import json
import subprocess
import sys
import unittest
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import matching_quality_report as benchmark
import profile_match_digest as matcher
import profile_normalization_eval as normalization_eval
from wahojobs.profiles.canonical import validate_canonical_profile
from wahojobs.profiles.normalizer import (
    BaselineHeuristicProfileNormalizer,
    FixtureExpectedProfileNormalizer,
    compare_canonical_profiles,
)


FIXTURE_PATH = ROOT / "tests" / "fixtures" / "profile_normalization_v1.json"


def load_suite():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class ProfileNormalizerInterfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.suite = load_suite()
        cls.cases = cls.suite["cases"]

    def test_fixture_normalizer_returns_exact_expected_profiles(self):
        normalizer = FixtureExpectedProfileNormalizer(self.suite)
        evaluation = normalization_eval.evaluate_suite(self.suite, normalizer)

        self.assertEqual(evaluation["valid_outputs"], len(self.cases))
        self.assertEqual(evaluation["exact_matches"], len(self.cases))
        self.assertEqual(evaluation["field_match_rate"], 1.0)

        for case in self.cases:
            with self.subTest(case_id=case["case_id"]):
                result = normalizer.normalize(
                    case["raw_input"],
                    case["input_style"],
                    {"case_id": case["case_id"], "archetype_id": case["archetype_id"]},
                )
                self.assertEqual(result.canonical_profile, case["expected_canonical_profile"])
                self.assertTrue(validate_canonical_profile(result.canonical_profile))
                self.assertEqual(result.extraction_quality, "control")

    def test_baseline_normalizer_returns_valid_profiles_for_suite(self):
        normalizer = BaselineHeuristicProfileNormalizer()
        evaluation = normalization_eval.evaluate_suite(self.suite, normalizer)

        self.assertEqual(evaluation["valid_outputs"], len(self.cases))
        self.assertLess(evaluation["exact_matches"], len(self.cases))
        self.assertGreater(evaluation["field_match_rate"], 0.25)

    def test_baseline_does_not_invent_credentials_or_years_when_absent(self):
        normalizer = BaselineHeuristicProfileNormalizer()
        result = normalizer.normalize(
            "I can write short reviews and do remote online research.",
            "short_paragraph",
            {"profile_id": "simple_writer"},
        )
        canonical = result.canonical_profile

        self.assertEqual(canonical["credentials"]["certifications"], [])
        self.assertEqual(canonical["credentials"]["licenses"], [])
        self.assertIsNone(canonical["experience"]["total_years"])
        self.assertIn("certifications", canonical["provenance"]["missing_fields"])
        self.assertIn("licenses", canonical["provenance"]["missing_fields"])
        self.assertIn("total_years", canonical["provenance"]["missing_fields"])

    def test_baseline_captures_obvious_language_mentions(self):
        normalizer = BaselineHeuristicProfileNormalizer()
        result = normalizer.normalize(
            "Portuguese native speaker with advanced English and conversational Spanish.",
            "short_paragraph",
            {"profile_id": "language_case"},
        )
        languages = {
            item["language"]: item["proficiency"]
            for item in result.canonical_profile["languages"]
        }

        self.assertEqual(languages["Portuguese"], "native")
        self.assertEqual(languages["English"], "advanced")
        self.assertEqual(languages["Spanish"], "conversational")

    def test_baseline_captures_obvious_preferences_and_constraints(self):
        normalizer = BaselineHeuristicProfileNormalizer()
        result = normalizer.normalize(
            "No degree. Need remote flexible work. No phone calls preferred; not coding.",
            "messy_sparse_input",
            {"profile_id": "constraints_case"},
        )
        canonical = result.canonical_profile

        self.assertEqual(canonical["education"]["education_level"], "no_degree")
        self.assertTrue(canonical["preferences"]["remote"])
        self.assertTrue(canonical["preferences"]["flexible"])
        self.assertEqual(canonical["preferences"]["phone_preference"], "non-phone preferred")
        self.assertIn("no college degree", canonical["constraints"]["hard_constraints"])
        self.assertIn("no phone calls preferred", canonical["constraints"]["soft_preferences"])
        self.assertIn("coding", canonical["constraints"]["avoid_keywords"])

    def test_compare_helper_identifies_missing_language_credential_and_location(self):
        expected = deepcopy(self.cases[0]["expected_canonical_profile"])
        actual = deepcopy(expected)
        actual["languages"] = []
        actual["location"]["country"] = ""
        actual["credentials"]["credential_status"] = "unknown"
        expected["location"]["country"] = "Brazil"
        expected["credentials"]["licenses"] = ["example license"]

        comparison = compare_canonical_profiles(expected, actual)

        self.assertFalse(comparison["exact_match"])
        self.assertIn("languages", comparison["missing_critical_fields"])
        self.assertIn("location.country", comparison["missing_critical_fields"])
        self.assertIn("credentials.licenses", comparison["missing_critical_fields"])

    def test_cli_fixture_mode_reports_perfect_control(self):
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(ROOT / "scripts" / "profile_normalization_eval.py"),
                "--suite",
                str(FIXTURE_PATH),
                "--normalizer",
                "fixture",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn("Normalizer: fixture", result.stdout)
        self.assertIn(f"Valid canonical_profile_v1 outputs: {len(self.cases)}/{len(self.cases)}", result.stdout)
        self.assertIn(f"Exact canonical matches: {len(self.cases)}/{len(self.cases)}", result.stdout)

    def test_cli_baseline_mode_runs_and_reports_valid_nonperfect_output(self):
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(ROOT / "scripts" / "profile_normalization_eval.py"),
                "--suite",
                str(FIXTURE_PATH),
                "--normalizer",
                "baseline",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn("Normalizer: baseline", result.stdout)
        self.assertIn(f"Valid canonical_profile_v1 outputs: {len(self.cases)}/{len(self.cases)}", result.stdout)
        self.assertNotIn(f"Exact canonical matches: {len(self.cases)}/{len(self.cases)}", result.stdout)

    def test_current_matcher_benchmark_remains_unchanged(self):
        fixture = benchmark.load_fixture()
        profiles = benchmark.load_benchmark_profiles(fixture)
        rows = benchmark.load_benchmark_db_rows()
        evaluated = [
            benchmark.evaluate_case(case, profiles[case["profile_id"]], rows, matcher)
            for case in fixture["cases"]
            if case.get("label_source") == "human_reviewed"
        ]
        metrics = benchmark.human_reviewed_agreement(evaluated)

        self.assertEqual(
            (
                metrics["label_agreement"],
                metrics["section_agreement"],
                metrics["full_agreement"],
                metrics["total"],
            ),
            (26, 29, 26, 30),
        )


if __name__ == "__main__":
    unittest.main()

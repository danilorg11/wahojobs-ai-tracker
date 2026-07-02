import json
import sys
import unittest
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import matching_quality_report as benchmark
import profile_match_digest as matcher
from wahojobs.profiles.canonical import (
    SCHEMA_VERSION,
    canonical_to_matcher_profile,
    validate_canonical_profile,
)


FIXTURE_PATH = ROOT / "tests" / "fixtures" / "profile_normalization_v1.json"
ALLOWED_INPUT_STYLES = {
    "short_paragraph",
    "long_paragraph",
    "resume_or_linkedin_style",
    "messy_sparse_input",
}
MATCHER_REQUIRED_FIELDS = {
    "profile_id",
    "display_name",
    "summary",
    "education_level",
    "degrees_or_domains",
    "languages",
    "skills",
    "work_preferences",
    "constraints",
    "target_opportunity_types",
    "notes",
    "signals",
    "avoid_keywords",
    "location",
    "country",
    "residence",
    "city",
    "region",
}


def load_suite():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class ProfileNormalizationSuiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.suite = load_suite()
        cls.cases = cls.suite["cases"]

    def test_suite_shape_and_case_ids(self):
        self.assertEqual(self.suite["schema_version"], "profile_normalization_suite_v1")
        self.assertGreaterEqual(len(self.cases), 24)
        self.assertLessEqual(len(self.cases), 30)

        case_ids = [case["case_id"] for case in self.cases]
        self.assertEqual(len(case_ids), len(set(case_ids)))
        for case_id in case_ids:
            self.assertTrue(case_id.startswith("pnv1_"))

    def test_required_case_fields_and_input_styles(self):
        required = {
            "case_id",
            "archetype_id",
            "input_style",
            "raw_input",
            "expected_canonical_profile",
            "review_notes",
            "normalization_focus",
        }
        styles = Counter()
        archetypes = Counter()
        for case in self.cases:
            with self.subTest(case_id=case.get("case_id")):
                self.assertTrue(required <= set(case))
                self.assertIn(case["input_style"], ALLOWED_INPUT_STYLES)
                self.assertIsInstance(case["raw_input"], str)
                self.assertTrue(case["raw_input"].strip())
                self.assertIsInstance(case["review_notes"], str)
                self.assertTrue(case["review_notes"].strip())
                self.assertIsInstance(case["normalization_focus"], list)
                self.assertTrue(case["normalization_focus"])
                styles[case["input_style"]] += 1
                archetypes[case["archetype_id"]] += 1

        self.assertGreaterEqual(len(archetypes), 8)
        self.assertGreaterEqual(styles["short_paragraph"], 1)
        self.assertGreaterEqual(styles["long_paragraph"], 1)
        self.assertGreaterEqual(styles["resume_or_linkedin_style"], 1)
        self.assertGreaterEqual(styles["messy_sparse_input"], 1)

    def test_expected_canonical_profiles_validate(self):
        for case in self.cases:
            with self.subTest(case_id=case["case_id"]):
                canonical = case["expected_canonical_profile"]
                self.assertEqual(canonical["schema_version"], SCHEMA_VERSION)
                self.assertTrue(validate_canonical_profile(canonical))

    def test_language_entries_are_structured_objects(self):
        for case in self.cases:
            with self.subTest(case_id=case["case_id"]):
                languages = case["expected_canonical_profile"]["languages"]
                self.assertIsInstance(languages, list)
                self.assertTrue(languages)
                for language in languages:
                    self.assertIsInstance(language, dict)
                    self.assertIsInstance(language.get("language"), str)
                    self.assertIn("proficiency", language)
                    self.assertIn("locale", language)
                    self.assertIn("evidence", language)
                    self.assertIn("confidence", language)

    def test_round_trip_to_matcher_profile_has_required_fields(self):
        for case in self.cases:
            with self.subTest(case_id=case["case_id"]):
                profile = canonical_to_matcher_profile(case["expected_canonical_profile"])
                self.assertTrue(MATCHER_REQUIRED_FIELDS <= set(profile))
                self.assertIsInstance(profile["signals"], list)
                self.assertTrue(profile["signals"])
                for reason, keywords, points in profile["signals"]:
                    self.assertIsInstance(reason, str)
                    self.assertIsInstance(keywords, list)
                    self.assertIsInstance(points, int)

    def test_guardrail_cases_do_not_invent_licenses_or_years(self):
        for case in self.cases:
            if "license_absence" not in case["normalization_focus"]:
                continue
            with self.subTest(case_id=case["case_id"]):
                canonical = case["expected_canonical_profile"]
                self.assertEqual(canonical["credentials"]["licenses"], [])
                self.assertIn(canonical["credentials"]["credential_status"], {"absent", "unknown"})
                if "years_experience" not in case["normalization_focus"]:
                    self.assertIsNone(canonical["experience"]["total_years"])

    def test_messy_sparse_cases_record_missing_or_ambiguous_fields(self):
        messy_cases = [case for case in self.cases if case["input_style"] == "messy_sparse_input"]
        self.assertGreaterEqual(len(messy_cases), 1)
        for case in messy_cases:
            with self.subTest(case_id=case["case_id"]):
                provenance = case["expected_canonical_profile"]["provenance"]
                combined = set(provenance["missing_fields"]) | set(provenance["ambiguous_fields"])
                self.assertTrue(combined)

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

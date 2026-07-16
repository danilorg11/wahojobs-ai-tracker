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
from wahojobs.profiles.canonical import complete_trusted_fixture_provenance, validate_canonical_profile
from wahojobs.profiles.normalizer import (
    BaselineHeuristicProfileNormalizer,
    FixtureExpectedProfileNormalizer,
    compare_canonical_profiles,
)


FIXTURE_PATH = ROOT / "tests" / "fixtures" / "profile_normalization_v1.json"


def load_suite():
    suite = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    for case in suite["cases"]:
        case["expected_canonical_profile"] = complete_trusted_fixture_provenance(
            case["expected_canonical_profile"]
        )
    return suite


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
        self.assertEqual(
            evaluation["critical_field_safety"]["blocking_false_positive_count"],
            0,
        )

    def test_critical_field_safety_separates_false_positive_and_uncertain_claims(self):
        expected = BaselineHeuristicProfileNormalizer().normalize(
            "I live in Brazil and I am fluent in English.",
            "short_paragraph",
            {"profile_id": "critical_expected"},
        ).canonical_profile
        uncertain = deepcopy(expected)
        uncertain["languages"][0]["proficiency"] = "unknown"
        findings = normalization_eval.critical_field_findings(
            "critical_uncertain", expected, uncertain
        )
        self.assertNotIn("false_positive", {item["kind"] for item in findings})
        self.assertIn("false_negative", {item["kind"] for item in findings})
        self.assertIn("uncertain", {item["kind"] for item in findings})

        unsupported = deepcopy(expected)
        unsupported["credentials"]["credential_status"] = "explicit"
        unsupported["credentials"]["licenses"] = ["medical license"]
        findings = normalization_eval.critical_field_findings(
            "critical_unsupported", expected, unsupported
        )
        blocking = [item for item in findings if item["blocking"]]
        self.assertEqual([(item["field"], item["kind"]) for item in blocking], [("credentials", "false_positive")])

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

    def test_remote_preference_does_not_become_explicit_location(self):
        normalizer = BaselineHeuristicProfileNormalizer()
        result = normalizer.normalize(
            "I speak English and Spanish, no college degree, looking for remote beginner AI data tasks.",
            "short_paragraph",
            {"profile_id": "remote_case"},
        )
        canonical = result.canonical_profile

        self.assertEqual(canonical["location"]["country"], "")
        self.assertEqual(canonical["location"]["remote_eligibility"], "unknown")
        self.assertTrue(canonical["preferences"]["remote"])
        self.assertIn("location", canonical["provenance"]["missing_fields"])

    def test_explicit_us_residence_sets_location(self):
        normalizer = BaselineHeuristicProfileNormalizer()
        examples = (
            "I live in the US and want remote AI data work.",
            "I live in the United States and want remote AI data work.",
            "Based in the US. English native, Spanish fluent.",
            "Located in the United States; available for remote AI work.",
            "My location is US. I prefer flexible work.",
            "Location: United States. I can do annotation tasks.",
        )

        for text in examples:
            with self.subTest(text=text):
                result = normalizer.normalize(text, "short_paragraph", {"profile_id": "us_case"})
                canonical = result.canonical_profile
                self.assertEqual(canonical["location"]["country"], "United States")
                self.assertEqual(canonical["location"]["residence"], "United States")
                self.assertNotIn("location", canonical["provenance"]["missing_fields"])

    def test_ambiguous_us_market_mentions_do_not_set_location(self):
        normalizer = BaselineHeuristicProfileNormalizer()
        examples = (
            "Interested in US jobs and English language data tasks.",
            "Experience in US English data and annotation QA.",
            "Worked on projects in the United States market.",
            "I reviewed US market research and want remote work.",
        )

        for text in examples:
            with self.subTest(text=text):
                result = normalizer.normalize(text, "short_paragraph", {"profile_id": "us_market_case"})
                canonical = result.canonical_profile
                self.assertEqual(canonical["location"]["country"], "")
                self.assertIn("location", canonical["provenance"]["missing_fields"])

    def test_later_language_proficiency_statements_update_languages(self):
        normalizer = BaselineHeuristicProfileNormalizer()
        result = normalizer.normalize(
            "I speak English and Spanish for AI data work. Later note: English native and Spanish fluent.",
            "short_paragraph",
            {"profile_id": "later_language_proficiency"},
        )
        languages = {
            item["language"]: item["proficiency"]
            for item in result.canonical_profile["languages"]
        }

        self.assertEqual(languages["English"], "native")
        self.assertEqual(languages["Spanish"], "fluent")
        self.assertNotIn("language proficiency", result.canonical_profile["provenance"]["ambiguous_fields"])

    def test_natural_language_proficiency_phrases_are_explicit(self):
        canonical = BaselineHeuristicProfileNormalizer().normalize(
            "Portuguese is my native language and I am fluent in English.",
            "short_paragraph",
            {"profile_id": "natural_language_proficiency"},
        ).canonical_profile
        languages = {
            item["language"]: item["proficiency"]
            for item in canonical["languages"]
        }

        self.assertEqual(languages, {"Portuguese": "native", "English": "fluent"})
        self.assertTrue(
            canonical["provenance"]["field_sources"]["languages[0].proficiency"]["explicit"]
        )
        self.assertTrue(
            canonical["provenance"]["field_sources"]["languages[1].proficiency"]["explicit"]
        )

    def test_combined_degree_and_professional_credential_absence_is_prefilled(self):
        canonical = BaselineHeuristicProfileNormalizer().normalize(
            "I do not have a university degree or professional credentials.",
            "short_paragraph",
            {"profile_id": "combined_absence"},
        ).canonical_profile

        self.assertEqual(canonical["education"]["education_level"], "no_degree")
        self.assertEqual(canonical["credentials"]["credential_status"], "absent")
        self.assertIn("no college degree", canonical["constraints"]["hard_constraints"])
        self.assertIn(
            "no professional license or certification",
            canonical["constraints"]["hard_constraints"],
        )
        self.assertNotIn("licenses", canonical["provenance"]["missing_fields"])
        self.assertNotIn("certifications", canonical["provenance"]["missing_fields"])

    def test_generic_professional_license_absence_removes_credential_missing_fields(self):
        normalizer = BaselineHeuristicProfileNormalizer()
        result = normalizer.normalize(
            "I do not hold any professional license or certification. I can review remote AI tasks.",
            "short_paragraph",
            {"profile_id": "no_professional_license"},
        )
        canonical = result.canonical_profile

        self.assertEqual(canonical["credentials"]["credential_status"], "absent")
        self.assertIn("no professional license or certification", canonical["constraints"]["hard_constraints"])
        self.assertNotIn("licenses", canonical["provenance"]["missing_fields"])
        self.assertNotIn("certifications", canonical["provenance"]["missing_fields"])

    def test_negative_biology_medical_credentials_do_not_become_positive_domains(self):
        normalizer = BaselineHeuristicProfileNormalizer()
        result = normalizer.normalize(
            "Python backend engineer. I don't have biology or medical credentials, but I can evaluate coding tasks.",
            "long_paragraph",
            {"profile_id": "software_negative_credentials"},
        )
        canonical = result.canonical_profile

        self.assertIn("software engineering", canonical["education"]["fields_or_domains"])
        self.assertNotIn("biology", canonical["education"]["fields_or_domains"])
        self.assertNotIn("medicine", canonical["education"]["fields_or_domains"])
        self.assertEqual(canonical["credentials"]["credential_status"], "absent")
        self.assertIn("no biology or medical credentials", canonical["constraints"]["hard_constraints"])

    def test_negated_medical_license_does_not_create_medicine_domain(self):
        canonical = BaselineHeuristicProfileNormalizer().normalize(
            "I am a software engineer. I have no medical license.",
            "short_paragraph",
            {"profile_id": "software_no_medical_license"},
        ).canonical_profile

        self.assertEqual(canonical["education"]["fields_or_domains"], ["software engineering"])
        self.assertEqual(canonical["credentials"]["credential_status"], "absent")
        self.assertIn("no medical license", canonical["constraints"]["hard_constraints"])

    def test_negated_medical_credentials_do_not_create_medicine_domain(self):
        canonical = BaselineHeuristicProfileNormalizer().normalize(
            "I have no medical credentials.",
            "short_paragraph",
            {"profile_id": "no_medical_credentials"},
        ).canonical_profile

        self.assertNotIn("medicine", canonical["education"]["fields_or_domains"])
        self.assertEqual(canonical["credentials"]["credential_status"], "absent")

    def test_negated_biology_and_medical_credentials_create_neither_domain(self):
        canonical = BaselineHeuristicProfileNormalizer().normalize(
            "I have no biology or medical credentials.",
            "short_paragraph",
            {"profile_id": "no_biology_or_medical_credentials"},
        ).canonical_profile

        self.assertNotIn("biology", canonical["education"]["fields_or_domains"])
        self.assertNotIn("medicine", canonical["education"]["fields_or_domains"])

    def test_positive_biology_with_negated_medical_license_retains_only_biology(self):
        canonical = BaselineHeuristicProfileNormalizer().normalize(
            "I have a PhD in biology. I have no medical license.",
            "long_paragraph",
            {"profile_id": "biology_no_medical_license"},
        ).canonical_profile

        self.assertIn("biology", canonical["education"]["fields_or_domains"])
        self.assertNotIn("medicine", canonical["education"]["fields_or_domains"])
        self.assertEqual(canonical["credentials"]["credential_status"], "absent")

    def test_positive_medical_research_survives_separate_license_negation(self):
        canonical = BaselineHeuristicProfileNormalizer().normalize(
            "I am a medical researcher, but I do not have a medical license.",
            "long_paragraph",
            {"profile_id": "medical_researcher_no_license"},
        ).canonical_profile

        self.assertIn("medicine", canonical["education"]["fields_or_domains"])
        self.assertEqual(canonical["credentials"]["credential_status"], "absent")
        self.assertIn("no medical license", canonical["constraints"]["hard_constraints"])

    def test_positive_medical_experience_remains_medicine(self):
        canonical = BaselineHeuristicProfileNormalizer().normalize(
            "I have professional experience in medicine.",
            "short_paragraph",
            {"profile_id": "medical_experience"},
        ).canonical_profile

        self.assertIn("medicine", canonical["education"]["fields_or_domains"])

    def test_no_experience_in_medicine_does_not_create_medicine_domain(self):
        canonical = BaselineHeuristicProfileNormalizer().normalize(
            "I have no experience in medicine.",
            "short_paragraph",
            {"profile_id": "no_medical_experience"},
        ).canonical_profile

        self.assertNotIn("medicine", canonical["education"]["fields_or_domains"])

    def test_software_domain_is_not_contaminated_by_negative_science_credentials(self):
        canonical = BaselineHeuristicProfileNormalizer().normalize(
            "Software engineer with Python. I have no biology or medical credentials.",
            "long_paragraph",
            {"profile_id": "software_negative_science_credentials"},
        ).canonical_profile

        self.assertEqual(canonical["education"]["fields_or_domains"], ["software engineering"])
        self.assertEqual(canonical["credentials"]["credential_status"], "absent")

    def test_nonaffirmative_context_does_not_create_professional_domains(self):
        cases = (
            ("I am interested in law without being a lawyer.", {"legal"}),
            ("I would like to work with legal AI.", {"legal"}),
            ("I might pursue a medical license.", {"medicine"}),
            ("I worked with healthcare content but I am not a clinician.", {"medicine"}),
            ("I used Python in a finance project without finance expertise.", {"finance"}),
            ("I studied some biology but do not have a biology degree.", {"biology"}),
            ("I am not a certified accountant.", {"finance"}),
            ("I do contract work.", {"legal"}),
            ("I am legal to work in Brazil.", {"legal"}),
        )
        for raw_input, forbidden in cases:
            with self.subTest(raw_input=raw_input):
                canonical = BaselineHeuristicProfileNormalizer().normalize(
                    raw_input,
                    "short_paragraph",
                    {"profile_id": "nonaffirmative"},
                ).canonical_profile
                domains = set(canonical["experience"]["professional_domains"])
                self.assertTrue(domains.isdisjoint(forbidden), domains)

        interested_writer = BaselineHeuristicProfileNormalizer().normalize(
            "I would like to do writing and editing work, but I have no writing experience.",
            "short_paragraph",
            {"profile_id": "writing_interest"},
        ).canonical_profile
        self.assertNotIn("writing", interested_writer["skills"]["normalized"])
        self.assertNotIn("editing", interested_writer["skills"]["normalized"])

    def test_work_authorization_language_never_becomes_legal_fit_evidence(self):
        cases = (
            ("I am legally allowed to work in Brazil.", "explicit"),
            ("I am legally authorized to work in the US.", "explicit"),
            ("I am authorized to work in Canada.", "explicit"),
            ("I am eligible to work in Canada.", "explicit"),
            ("I have the right to work in Canada.", "explicit"),
            ("I have work authorization.", "explicit"),
            ("My visa allows me to work.", "explicit"),
            ("I have a work permit.", "explicit"),
            ("I need work authorization.", "required"),
        )
        for raw_input, expected_authorization in cases:
            with self.subTest(raw_input=raw_input):
                canonical = BaselineHeuristicProfileNormalizer().normalize(
                    raw_input,
                    "short_paragraph",
                    {"profile_id": "work_authorization"},
                ).canonical_profile
                self.assertNotIn("legal", canonical["experience"]["professional_domains"])
                self.assertNotIn(
                    "legal AI training",
                    canonical["preferences"]["target_opportunity_types"],
                )
                self.assertNotIn(
                    "legal AI training",
                    canonical["derived_matcher_signals"]["derived_target_work_types"],
                )
                self.assertEqual(
                    canonical["location"]["work_authorization"],
                    expected_authorization,
                )

        for raw_input in ("I do contract work.", "I prepare legally compliant documentation."):
            with self.subTest(raw_input=raw_input):
                canonical = BaselineHeuristicProfileNormalizer().normalize(
                    raw_input,
                    "short_paragraph",
                    {"profile_id": "not_legal_work"},
                ).canonical_profile
                self.assertNotIn("legal", canonical["experience"]["professional_domains"])
                self.assertNotIn(
                    "legal AI training",
                    canonical["preferences"]["target_opportunity_types"],
                )

    def test_legal_interest_remains_a_preference_without_becoming_a_qualification(self):
        canonical = BaselineHeuristicProfileNormalizer().normalize(
            "I am interested in legal AI, but I am not a lawyer.",
            "short_paragraph",
            {"profile_id": "legal_interest"},
        ).canonical_profile

        self.assertNotIn("legal", canonical["experience"]["professional_domains"])
        self.assertIn("legal AI training", canonical["preferences"]["target_opportunity_types"])

    def test_positive_legal_professional_controls_remain_qualifications(self):
        cases = (
            "I am a lawyer.",
            "I have a law degree.",
            "I worked as legal counsel.",
            "I am a paralegal.",
        )
        for raw_input in cases:
            with self.subTest(raw_input=raw_input):
                canonical = BaselineHeuristicProfileNormalizer().normalize(
                    raw_input,
                    "short_paragraph",
                    {"profile_id": "legal_positive"},
                ).canonical_profile
                self.assertIn("legal", canonical["experience"]["professional_domains"])
                self.assertIn(
                    "legal AI training",
                    canonical["preferences"]["target_opportunity_types"],
                )

    def test_affirmative_professional_controls_remain_qualifications(self):
        cases = (
            ("I am a licensed lawyer.", "legal", "attorney license"),
            ("I worked as a financial analyst for five years.", "finance", None),
            ("I have a master's degree in Biology.", "biology", None),
            ("I am a registered nurse.", "medicine", "registered nurse license"),
            ("I have professional experience in medicine.", "medicine", None),
        )
        for raw_input, domain, license_name in cases:
            with self.subTest(raw_input=raw_input):
                canonical = BaselineHeuristicProfileNormalizer().normalize(
                    raw_input,
                    "short_paragraph",
                    {"profile_id": "affirmative"},
                ).canonical_profile
                self.assertIn(domain, canonical["experience"]["professional_domains"])
                if license_name:
                    self.assertIn(license_name, canonical["credentials"]["licenses"])
                if "master's degree" in raw_input.casefold():
                    self.assertEqual(canonical["education"]["education_level"], "master")
                    self.assertEqual(canonical["education"]["degrees"], ["Master's degree"])

    def test_not_licensed_physician_records_license_absence(self):
        normalizer = BaselineHeuristicProfileNormalizer()
        result = normalizer.normalize(
            "Biology researcher interested in medicine AI review, but I am not a licensed physician.",
            "long_paragraph",
            {"profile_id": "biology_no_physician_license"},
        )
        canonical = result.canonical_profile

        self.assertIn("biology", canonical["education"]["fields_or_domains"])
        self.assertNotIn("medicine", canonical["education"]["fields_or_domains"])
        self.assertEqual(canonical["credentials"]["credential_status"], "absent")
        self.assertIn("no medical license", canonical["constraints"]["hard_constraints"])

    def test_biology_research_extracts_microbiology_and_writing_context(self):
        normalizer = BaselineHeuristicProfileNormalizer()
        result = normalizer.normalize(
            "PhD microbiologist with biology research, academic writing, and scientific writing experience. Not a licensed physician.",
            "long_paragraph",
            {"profile_id": "microbiology_researcher"},
        )
        canonical = result.canonical_profile

        self.assertIn("biology", canonical["education"]["fields_or_domains"])
        self.assertIn("microbiology", canonical["education"]["fields_or_domains"])
        self.assertIn("microbiology", canonical["experience"]["specialties"])
        self.assertIn("academic writing", canonical["experience"]["specialties"])
        self.assertIn("academic writing", canonical["skills"]["normalized"])
        self.assertIn("scientific writing", canonical["skills"]["normalized"])
        self.assertIn(
            "Writing/review skill signal",
            {signal["reason"] for signal in canonical["derived_matcher_signals"]["signals"]},
        )
        self.assertEqual(canonical["credentials"]["credential_status"], "absent")

    def test_compare_helper_identifies_missing_language_credential_and_location(self):
        expected = deepcopy(self.cases[0]["expected_canonical_profile"])
        actual = deepcopy(expected)
        actual["languages"] = []
        actual["location"]["country"] = ""
        actual["credentials"]["credential_status"] = "unknown"
        expected["location"]["country"] = "Brazil"
        expected["credentials"]["licenses"] = ["example license"]
        expected["provenance"].pop("field_sources")
        actual["provenance"].pop("field_sources")
        expected = complete_trusted_fixture_provenance(expected)
        actual = complete_trusted_fixture_provenance(actual)

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

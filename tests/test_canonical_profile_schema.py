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
import profile_match_digest as matcher
from wahojobs.profiles.canonical import (
    SCHEMA_VERSION,
    canonical_profile_debug_summary,
    canonical_to_matcher_profile,
    matcher_profile_to_canonical,
    validate_canonical_profile,
)


class CanonicalProfileSchemaTests(unittest.TestCase):
    def test_built_in_profiles_convert_to_canonical_v1(self):
        profiles, _source = matcher.load_profiles(None)

        self.assertGreater(len(profiles), 0)
        for profile in profiles:
            with self.subTest(profile_id=profile["profile_id"]):
                canonical = matcher_profile_to_canonical(profile)
                self.assertEqual(canonical["schema_version"], SCHEMA_VERSION)
                self.assertEqual(canonical["identity"]["profile_id"], profile["profile_id"])
                self.assertEqual(canonical["identity"]["display_name"], profile["display_name"])
                self.assertTrue(validate_canonical_profile(canonical))

    def test_sample_profiles_convert_to_canonical_v1(self):
        profiles, _source = matcher.load_profiles(ROOT / "profiles" / "sample_profiles.json")

        self.assertGreater(len(profiles), 0)
        for profile in profiles:
            with self.subTest(profile_id=profile["profile_id"]):
                canonical = matcher_profile_to_canonical(profile, extracted_from="sample_profiles_json")
                self.assertEqual(canonical["provenance"]["extracted_from"], "sample_profiles_json")
                self.assertTrue(validate_canonical_profile(canonical))

    def test_languages_are_structured_entries(self):
        profile = matcher.normalize_profile(
            {
                "profile_id": "language_profile",
                "display_name": "Language Profile",
                "education_level": "not_specified",
                "degrees_or_domains": ["language"],
                "languages": ["Portuguese", "English"],
                "skills": ["translation"],
                "work_preferences": ["remote"],
                "constraints": [],
                "target_opportunity_types": ["language review"],
                "notes": "",
            }
        )
        canonical = matcher_profile_to_canonical(profile)

        self.assertEqual(
            canonical["languages"],
            [
                {
                    "language": "Portuguese",
                    "proficiency": "unknown",
                    "locale": "",
                    "evidence": [],
                    "confidence": "unknown",
                },
                {
                    "language": "English",
                    "proficiency": "unknown",
                    "locale": "",
                    "evidence": [],
                    "confidence": "unknown",
                },
            ],
        )

    def test_location_fields_are_preserved_when_present(self):
        profile = matcher.normalize_profile(
            {
                "profile_id": "located_profile",
                "display_name": "Located Profile",
                "education_level": "not_specified",
                "degrees_or_domains": ["generalist"],
                "languages": ["English"],
                "skills": ["review"],
                "work_preferences": ["remote", "flexible"],
                "constraints": [],
                "target_opportunity_types": ["AI training"],
                "notes": "",
                "country": "Brazil",
                "region": "Sao Paulo",
                "city": "Sao Paulo",
                "residence": "Brazil",
            }
        )
        canonical = matcher_profile_to_canonical(profile)
        round_trip = canonical_to_matcher_profile(canonical)

        self.assertEqual(canonical["location"]["country"], "Brazil")
        self.assertEqual(canonical["location"]["region"], "Sao Paulo")
        self.assertEqual(canonical["location"]["city"], "Sao Paulo")
        self.assertEqual(canonical["location"]["residence"], "Brazil")
        self.assertEqual(round_trip["country"], "Brazil")
        self.assertEqual(round_trip["city"], "Sao Paulo")

    def test_current_matcher_fields_are_preserved(self):
        profile = matcher.load_profiles(None)[0][0]
        canonical = matcher_profile_to_canonical(profile)
        round_trip = canonical_to_matcher_profile(canonical)

        for field in (
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
            "avoid_keywords",
            "location",
            "country",
            "residence",
            "city",
            "region",
        ):
            self.assertEqual(round_trip[field], profile[field])
        self.assertEqual(round_trip["signals"], profile["signals"])
        self.assertEqual(canonical["education"]["fields_or_domains"], profile["degrees_or_domains"])
        self.assertEqual(canonical["skills"]["normalized"], profile["skills"])
        self.assertEqual(canonical["preferences"]["work_preferences"], profile["work_preferences"])
        self.assertEqual(canonical["preferences"]["target_opportunity_types"], profile["target_opportunity_types"])
        self.assertEqual(canonical["constraints"]["hard_constraints"], profile["constraints"])
        self.assertEqual(canonical["constraints"]["avoid_keywords"], profile["avoid_keywords"])

    def test_missing_rich_fields_are_unknown_or_absent_without_invention(self):
        profile = matcher.load_profiles(None)[0][0]
        canonical = matcher_profile_to_canonical(profile)
        summary = canonical_profile_debug_summary(canonical)

        self.assertEqual(canonical["credentials"]["certifications"], [])
        self.assertEqual(canonical["credentials"]["licenses"], [])
        self.assertEqual(canonical["credentials"]["credential_status"], "unknown")
        self.assertEqual(canonical["experience"]["seniority"], "unknown")
        self.assertIsNone(canonical["experience"]["total_years"])
        self.assertIn("location", canonical["provenance"]["missing_fields"])
        self.assertFalse(summary["has_credentials"])

    def test_provenance_and_evidence_are_optional_but_supported(self):
        profile = matcher.load_profiles(None)[0][0]
        canonical = matcher_profile_to_canonical(
            profile,
            source_inputs=[{"type": "paragraph", "source_id": "sample"}],
            extracted_from="short_paragraph",
        )

        self.assertEqual(canonical["identity"]["source_inputs"], [{"type": "paragraph", "source_id": "sample"}])
        self.assertEqual(canonical["provenance"]["extracted_from"], "short_paragraph")
        self.assertEqual(canonical["provenance"]["evidence_snippets"], [])
        self.assertEqual(canonical["provenance"]["confidence"], "unknown")

    def test_canonical_round_trip_preserves_human_reviewed_benchmark_predictions(self):
        fixture = benchmark.load_fixture()
        profiles = benchmark.load_benchmark_profiles(fixture)
        rows = benchmark.load_benchmark_db_rows()
        original_items = []
        round_trip_items = []

        for case in fixture["cases"]:
            if case.get("label_source") != "human_reviewed":
                continue
            profile = profiles[case["profile_id"]]
            round_trip_profile = canonical_to_matcher_profile(matcher_profile_to_canonical(profile))
            original = benchmark.evaluate_case(case, profile, rows, matcher)
            round_trip = benchmark.evaluate_case(case, round_trip_profile, rows, matcher)
            original_items.append(original)
            round_trip_items.append(round_trip)

            self.assertEqual(round_trip.score, original.score)
            self.assertEqual(round_trip.raw_match_label, original.raw_match_label)
            self.assertEqual(round_trip.raw_section, original.raw_section)
            self.assertEqual(round_trip.evaluation_label, original.evaluation_label)
            self.assertEqual(round_trip.evaluation_section, original.evaluation_section)

        metrics = benchmark.human_reviewed_agreement(round_trip_items)
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

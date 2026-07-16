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
from wahojobs.profiles.canonical import (
    AVAILABILITY_STATUSES,
    EMPLOYMENT_TYPES,
    PHONE_PREFERENCES,
    PROFILE_SOURCE_PARSED_TEXT,
    SCHEDULE_PREFERENCES,
    SCHEMA_VERSION,
    SYNCHRONOUS_PREFERENCES,
    canonical_field_path_value,
    canonical_profile_fingerprint,
    canonical_profile_debug_summary,
    canonical_to_matcher_profile,
    field_sources_for_profile,
    matcher_profile_to_canonical,
    validate_canonical_profile,
)
from wahojobs.profiles.countries import normalize_country
from wahojobs.profiles.review import apply_reviewed_profile


class CanonicalProfileSchemaTests(unittest.TestCase):
    def canonical_fixture(self):
        profile = matcher.load_profiles(None)[0][0]
        return matcher_profile_to_canonical(profile)

    def assert_invalid(self, mutate, expected):
        canonical = deepcopy(self.canonical_fixture())
        mutate(canonical)
        with self.assertRaisesRegex(ValueError, expected):
            validate_canonical_profile(canonical)

    def refresh_provenance(self, canonical):
        canonical["provenance"]["field_sources"] = field_sources_for_profile(
            canonical,
            PROFILE_SOURCE_PARSED_TEXT,
            explicit=False,
        )
        return canonical

    def test_recursive_contract_rejects_unknown_fields_and_wrong_types(self):
        cases = (
            (lambda value: value.__setitem__("unexpected", True), "unexpected is not supported"),
            (lambda value: value["credentials"].__setitem__("unexpected", "x"), "credentials.unexpected is not supported"),
            (lambda value: value["location"].__setitem__("country", ["Brazil"]), "location.country must be a string"),
            (lambda value: value["preferences"].__setitem__("remote", "yes"), "preferences.remote must be boolean"),
            (lambda value: value["experience"].__setitem__("total_years", True), "experience.total_years must be null or an integer"),
            (lambda value: value["education"].__setitem__("graduation_years", [True]), r"graduation_years\[0\] must be an integer"),
            (lambda value: value["credentials"].__setitem__("credential_status", "licensed"), "credential_status is not supported"),
            (lambda value: value["languages"][0].__setitem__("unexpected", "x"), r"languages\[0\].unexpected is not supported"),
            (lambda value: value["provenance"].__setitem__("field_sources", {"location.country": {"source": "client", "explicit": True}}), "source is invalid"),
            (lambda value: value["matcher_compatible_profile"].__setitem__("signals", [["Reason", ["keyword"], True]]), r"signals\[0\]\[2\] must be an integer"),
            (lambda value: value["identity"].__setitem__("profile_id", " profile "), "profile_id must not have leading or trailing whitespace"),
            (lambda value: value.__setitem__("schema_version", "canonical_profile_v999"), "schema_version must be"),
        )
        for mutate, expected in cases:
            with self.subTest(expected=expected):
                self.assert_invalid(mutate, expected)

    def test_contract_rejects_profile_contradictions(self):
        def credential_conflict(value):
            value["credentials"]["credential_status"] = "absent"
            value["credentials"]["licenses"] = ["medical license"]

        def degree_conflict(value):
            value["education"]["education_level"] = "no_degree"
            value["education"]["degrees"] = ["BA"]

        def no_degree_constraint_conflict(value):
            value["education"]["education_level"] = "bachelor"
            value["education"]["degrees"] = ["BA"]
            value["constraints"]["hard_constraints"] = ["no college degree"]

        def language_conflict(value):
            value["languages"] = [
                {"language": "Spanish", "proficiency": "fluent", "locale": "", "evidence": [], "confidence": "high"},
                {"language": "Spanish", "proficiency": "basic", "locale": "", "evidence": [], "confidence": "high"},
            ]

        def domain_conflict(value):
            value["experience"]["professional_domains"] = ["finance"]
            value["constraints"]["excluded_domains"] = ["Finance"]

        def license_conflict(value):
            value["credentials"]["credential_status"] = "explicit"
            value["credentials"]["licenses"] = ["medical license"]
            value["constraints"]["hard_constraints"] = ["no medical license"]

        def seniority_conflict(value):
            value["experience"]["total_years"] = 0
            value["experience"]["seniority"] = "senior"

        def location_conflict(value):
            value["location"]["country"] = "Brazil"
            value["location"]["residence"] = "United States"

        for mutate, expected in (
            (credential_conflict, "credentials cannot be listed"),
            (degree_conflict, "degrees cannot be listed"),
            (no_degree_constraint_conflict, "no-degree constraint"),
            (language_conflict, "language proficiency must be resolved explicitly"),
            (domain_conflict, "excluded domains conflict"),
            (license_conflict, "no-license constraint"),
            (seniority_conflict, "zero experience conflicts"),
            (location_conflict, "location.country conflicts"),
        ):
            with self.subTest(expected=expected):
                self.assert_invalid(mutate, expected)

    def test_unrelated_license_absence_does_not_conflict_with_confirmed_license(self):
        canonical = self.canonical_fixture()
        canonical["credentials"]["credential_status"] = "explicit"
        canonical["credentials"]["licenses"] = ["attorney license"]
        canonical["constraints"]["hard_constraints"] = ["no medical license"]
        self.refresh_provenance(canonical)

        self.assertTrue(validate_canonical_profile(canonical))

    def test_country_contract_normalizes_at_input_boundary_and_rejects_unknown_canonical_values(self):
        aliases = {
            "Brazil": "Brazil",
            "BR": "Brazil",
            "United States": "United States",
            "US": "United States",
            "United Kingdom": "United Kingdom",
            "Canada": "Canada",
            "India": "India",
            "M\u00e9xico": "Mexico",
            "Brasil": "Brazil",
            "\u65e5\u672c": "Japan",
        }
        for supplied, expected in aliases.items():
            with self.subTest(supplied=supplied):
                self.assertEqual(normalize_country(supplied), expected)

        for invalid in ("Narnia", "", "   ", 12, True):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    normalize_country(invalid)

        canonical = self.canonical_fixture()
        canonical["location"]["country"] = "Narnia"
        canonical["location"]["residence"] = "Narnia"
        self.refresh_provenance(canonical)
        with self.assertRaisesRegex(ValueError, "canonical country name"):
            validate_canonical_profile(canonical)

        canonical["location"]["country"] = "BR"
        canonical["location"]["residence"] = "BR"
        self.refresh_provenance(canonical)
        with self.assertRaisesRegex(ValueError, "canonical country name"):
            validate_canonical_profile(canonical)

    def test_review_boundary_canonicalizes_country_codes_and_names(self):
        canonical = self.canonical_fixture()
        reviewed = apply_reviewed_profile(
            canonical,
            {
                "country": "BR",
                "languages": [],
                "education_level": "not_specified",
                "credential_status": "unknown",
            },
        )
        self.assertEqual(reviewed["location"]["country"], "Brazil")
        self.assertEqual(reviewed["location"]["residence"], "Brazil")
        self.assertTrue(validate_canonical_profile(reviewed))

    def test_preference_enums_are_strict_and_supported_values_validate(self):
        canonical = self.canonical_fixture()
        canonical["preferences"].update(
            {
                "employment_types": sorted(EMPLOYMENT_TYPES),
                "synchronous_preference": "asynchronous",
                "phone_preference": "non-phone required",
                "schedule": ["asynchronous", "weekdays"],
                "availability": "immediate",
                "work_preferences": ["remote", *sorted(EMPLOYMENT_TYPES)],
                "remote": True,
                "flexible": True,
            }
        )
        self.refresh_provenance(canonical)
        self.assertTrue(validate_canonical_profile(canonical))
        self.assertIn("unknown", PHONE_PREFERENCES)
        self.assertIn("flexible", SYNCHRONOUS_PREFERENCES)
        self.assertIn("weekdays", SCHEDULE_PREFERENCES)
        self.assertIn("available", AVAILABILITY_STATUSES)

        for field, value in (
            ("phone_preference", "telepathy"),
            ("synchronous_preference", "whenever"),
            ("availability", "perhaps"),
        ):
            broken = deepcopy(canonical)
            broken["preferences"][field] = value
            self.refresh_provenance(broken)
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, f"preferences.{field} is not supported"):
                    validate_canonical_profile(broken)

        broken = deepcopy(canonical)
        broken["preferences"]["employment_types"] = ["gig-ish"]
        self.refresh_provenance(broken)
        with self.assertRaisesRegex(ValueError, "employment_types.*not supported"):
            validate_canonical_profile(broken)

        scalar_enums = {
            "phone_preference": PHONE_PREFERENCES,
            "synchronous_preference": SYNCHRONOUS_PREFERENCES,
            "availability": AVAILABILITY_STATUSES,
        }
        for field, supported_values in scalar_enums.items():
            for value in sorted(supported_values):
                supported = deepcopy(canonical)
                supported["preferences"][field] = value
                supported["preferences"]["schedule"] = []
                self.refresh_provenance(supported)
                with self.subTest(field=field, supported=value):
                    self.assertTrue(validate_canonical_profile(supported))
            for value in ("", "Wrong Case", 7, True):
                unsupported = deepcopy(canonical)
                unsupported["preferences"][field] = value
                self.refresh_provenance(unsupported)
                with self.subTest(field=field, unsupported=value):
                    with self.assertRaises(ValueError):
                        validate_canonical_profile(unsupported)

    def test_preference_conflicts_are_rejected(self):
        cases = (
            ("synchronous_preference", "synchronous", ["asynchronous"]),
            ("synchronous_preference", "asynchronous", ["synchronous"]),
        )
        for field, value, schedule in cases:
            canonical = self.canonical_fixture()
            canonical["preferences"][field] = value
            canonical["preferences"]["schedule"] = schedule
            self.refresh_provenance(canonical)
            with self.subTest(value=value, schedule=schedule):
                with self.assertRaisesRegex(ValueError, "conflicts"):
                    validate_canonical_profile(canonical)

        for field, preference in (("remote", "remote"), ("flexible", "flexible")):
            canonical = self.canonical_fixture()
            canonical["preferences"][field] = False
            canonical["preferences"]["work_preferences"] = [preference]
            self.refresh_provenance(canonical)
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "conflicts"):
                    validate_canonical_profile(canonical)

    def test_provenance_paths_resolve_to_present_material_leaves(self):
        canonical = self.canonical_fixture()
        valid_paths = [
            "location.remote_eligibility",
            "languages[0].language",
            "languages[0].proficiency",
            "education.education_level",
            "credentials.credential_status",
            "experience.professional_domains[0]",
            "skills.normalized[0]",
            "preferences.remote",
            "constraints.hard_constraints[0]",
        ]
        for path in valid_paths:
            with self.subTest(path=path):
                self.assertIsNotNone(canonical_field_path_value(canonical, path))

        invalid_paths = (
            "does.not.exist",
            "languages[99].language",
            "languages",
            "schema_version",
            "location..country",
            "location.country.",
            "matcher_compatible_profile.summary",
            "location.city",
        )
        for path in invalid_paths:
            broken = deepcopy(canonical)
            broken["provenance"]["field_sources"][path] = {
                "source": PROFILE_SOURCE_PARSED_TEXT,
                "explicit": False,
            }
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "provenance.field_sources"):
                    validate_canonical_profile(broken)

        missing = deepcopy(canonical)
        missing["provenance"]["field_sources"].pop("languages[0].language")
        with self.assertRaisesRegex(ValueError, "missing material field languages"):
            validate_canonical_profile(missing)

    def test_provenance_remains_deterministic_after_array_deletion(self):
        canonical = self.canonical_fixture()
        canonical["languages"].pop()
        self.refresh_provenance(canonical)
        first = canonical_profile_fingerprint(canonical)
        second = canonical_profile_fingerprint(deepcopy(canonical))
        self.assertEqual(first, second)
        self.assertNotIn("languages[1].language", canonical["provenance"]["field_sources"])
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

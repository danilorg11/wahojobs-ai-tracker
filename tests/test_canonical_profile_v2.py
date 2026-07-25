import hashlib
import json
import re
import subprocess
import sys
import unittest
import unicodedata
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
    canonical_to_matcher_profile,
    complete_trusted_fixture_provenance,
    validate_canonical_profile,
)
from wahojobs.profiles.canonical_v2 import (
    CANONICAL_PROFILE_V2_LIMITS,
    CanonicalProfileV2Error,
    FIELD_PATH_VERSION,
    MAX_DOMAIN_YEARS,
    MAX_FIELD_SOURCES,
    MAX_LANGUAGES,
    MAX_SIGNALS,
    MAX_SIGNAL_KEYWORDS,
    MAX_SKILL_ENTRIES,
    SCHEMA_VERSION,
    STRUCTURED_WHITESPACE_POLICY,
    canonical_profile_v2_json_bytes,
    convert_v1_to_v2,
    normalize_comparison_label,
    parse_canonical_profile_v2_json,
    parse_field_path,
    project_v2_to_matcher_v1,
    resolve_field_path,
    validate_ephemeral_matcher_profile_id,
    validate_canonical_profile_v2,
)


SUITE_PATH = ROOT / "tests" / "fixtures" / "profile_normalization_v1.json"
DB_PATH = ROOT / "data" / "wahojobs.sqlite"
MATERIAL_ROOTS = (
    "languages",
    "location",
    "education",
    "credentials",
    "experience",
    "skills",
    "preferences",
    "constraints",
)


def load_cases():
    suite = json.loads(SUITE_PATH.read_text(encoding="utf-8"))
    return [
        {
            **case,
            "expected_canonical_profile": complete_trusted_fixture_provenance(
                case["expected_canonical_profile"]
            ),
        }
        for case in suite["cases"]
    ]


def ordinal_resolver(_path, _source_kind, _explicit):
    return [1]


def persistent_id(index=1):
    return f"prf_{index:032x}"


def contains_text_fragment(value, fragment):
    target = fragment.casefold()
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str) and target in item.casefold():
            return True
        if isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False


def rebuild_field_sources(profile):
    paths = []

    def visit(value, path):
        if isinstance(value, dict):
            for key, child in value.items():
                visit(child, f"{path}.{key}" if path else key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")
        elif value not in (None, ""):
            paths.append(path)

    for root in MATERIAL_ROOTS:
        visit(profile[root], root)
    template = profile["provenance"]["field_sources"][0]
    profile["provenance"]["field_sources"] = [
        {
            "field_path": path,
            "path_version": FIELD_PATH_VERSION,
            "source_ordinals": [1],
            "source_kind": template["source_kind"],
            "explicit": True,
        }
        for path in sorted(paths, key=lambda value: (value.casefold(), value))
    ]
    return profile


class CanonicalProfileV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = load_cases()

    def convert_case(self, index=0):
        case = self.cases[index]
        return convert_v1_to_v2(
            case["expected_canonical_profile"],
            persistent_profile_id=persistent_id(index + 1),
            source_ordinal_resolver=ordinal_resolver,
        )

    def assert_rejected(self, value, reason=None):
        with self.assertRaises(CanonicalProfileV2Error) as raised:
            validate_canonical_profile_v2(value)
        if reason:
            self.assertIn(reason, raised.exception.reason_codes)
        return raised.exception

    def test_all_25_fixtures_convert_validate_and_project(self):
        self.assertEqual(len(self.cases), 25)
        for index, case in enumerate(self.cases, start=1):
            with self.subTest(case_id=case["case_id"]):
                v1 = case["expected_canonical_profile"]
                validate_canonical_profile(v1)
                v2 = convert_v1_to_v2(
                    v1,
                    persistent_profile_id=persistent_id(index),
                    source_ordinal_resolver=ordinal_resolver,
                )
                self.assertEqual(validate_canonical_profile_v2(v2), v2)
                projected = project_v2_to_matcher_v1(
                    v2,
                    matcher_profile_id=case["archetype_id"],
                )
                self.assertTrue(validate_canonical_profile(projected))
                self.assertEqual(projected["identity"]["profile_id"], case["archetype_id"])
                self.assertNotEqual(
                    projected["identity"]["profile_id"],
                    v2["identity"]["profile_id"],
                )

    def test_exact_root_identity_and_privacy_contract(self):
        v2 = self.convert_case()
        self.assertEqual(
            set(v2),
            {
                "schema_version",
                "identity",
                "languages",
                "location",
                "education",
                "credentials",
                "experience",
                "skills",
                "preferences",
                "constraints",
                "derived_matcher_signals",
                "provenance",
            },
        )
        self.assertEqual(v2["schema_version"], SCHEMA_VERSION)
        self.assertEqual(set(v2["identity"]), {"profile_id", "display_name"})
        serialized = canonical_profile_v2_json_bytes(v2).decode("utf-8")
        for prohibited in (
            "matcher_compatible_profile",
            "original_text",
            "evidence_snippets",
            '"evidence"',
            "source_inputs",
            "case_id",
            "account_id",
            "principal_id",
            "session_id",
            "email",
        ):
            self.assertNotIn(prohibited, serialized)

    def test_nonempty_v1_raw_and_evidence_fields_are_removed_not_duplicated(self):
        v1 = deepcopy(self.cases[0]["expected_canonical_profile"])
        private = "private source fragment that must not survive"
        v1["languages"][0]["evidence"] = [private]
        v1["skills"]["entries"][0]["evidence"] = [private]
        v1["derived_matcher_signals"]["signals"][0]["evidence"] = [private]
        v1["provenance"]["original_text"] = private
        v1["provenance"]["evidence_snippets"] = [private]
        v1["provenance"].pop("field_sources")
        v1 = complete_trusted_fixture_provenance(v1)
        validate_canonical_profile(v1)

        v2 = convert_v1_to_v2(
            v1,
            persistent_profile_id=persistent_id(),
            source_ordinal_resolver=ordinal_resolver,
        )
        self.assertNotIn(private, canonical_profile_v2_json_bytes(v2).decode("utf-8"))

    def test_unknown_v1_extension_is_rejected_before_conversion(self):
        v1 = deepcopy(self.cases[0]["expected_canonical_profile"])
        v1["unsupported_extension"] = "private value"
        with self.assertRaises(CanonicalProfileV2Error) as raised:
            convert_v1_to_v2(
                v1,
                persistent_profile_id=persistent_id(),
                source_ordinal_resolver=ordinal_resolver,
            )
        self.assertIn("invalid_v1_input", raised.exception.reason_codes)
        self.assertNotIn("private value", str(raised.exception))

    def test_unknown_root_and_nested_fields_are_rejected(self):
        v2 = self.convert_case()
        v2["extra"] = "value"
        self.assert_rejected(v2, "invalid_root_fields")
        v2 = self.convert_case()
        v2["location"]["extra"] = "value"
        self.assert_rejected(v2, "invalid_location_fields")

    def test_prohibited_privacy_keys_are_rejected_at_every_depth(self):
        mutations = (
            lambda value: value.update({"raw_input": "private"}),
            lambda value: value["location"].update({"account_id": "private"}),
            lambda value: value["languages"][0].update({"evidence": ["private"]}),
            lambda value: value["skills"]["entries"][0].update({"original_text": "private"}),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                v2 = self.convert_case()
                mutate(v2)
                self.assert_rejected(v2)

    def test_structural_key_grammar_and_document_limits(self):
        v2 = self.convert_case()
        v2["Bad-Key"] = 1
        self.assert_rejected(v2, "invalid_structural_key")
        v2 = self.convert_case()
        v2["constraints"]["hard_constraints"] = ["x"] * 257
        self.assert_rejected(v2, "list_too_large")
        v2 = self.convert_case()
        v2["location"]["region"] = "x" * 4097
        self.assert_rejected(v2, "scalar_too_large")

    def test_dynamic_array_limits_are_enforced(self):
        mutations = (
            lambda value: value.update(
                languages=[deepcopy(value["languages"][0])] * (MAX_LANGUAGES + 1)
            ),
            lambda value: value["experience"].update(
                years_by_domain=[
                    {"domain": f"domain {index}", "years": 1}
                    for index in range(MAX_DOMAIN_YEARS + 1)
                ]
            ),
            lambda value: value["skills"].update(
                entries=[
                    {"skill": f"skill {index}", "confidence": "unknown"}
                    for index in range(MAX_SKILL_ENTRIES + 1)
                ]
            ),
            lambda value: value["derived_matcher_signals"].update(
                signals=[
                    {
                        "reason": f"reason_{index}",
                        "keywords": [f"keyword {index}"],
                        "points": 1,
                        "confidence": "unknown",
                    }
                    for index in range(MAX_SIGNALS + 1)
                ]
            ),
            lambda value: value["provenance"].update(
                field_sources=[deepcopy(value["provenance"]["field_sources"][0])]
                * (MAX_FIELD_SOURCES + 1)
            ),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                v2 = self.convert_case()
                mutate(v2)
                self.assert_rejected(v2)

    def test_nfc_controls_nulls_enums_and_item_shapes_are_rejected(self):
        mutations = (
            lambda value: value["location"].update(region="bad\x01control"),
            lambda value: value["languages"][0].update(proficiency="expert"),
            lambda value: value["languages"][0].update(locale=None),
            lambda value: value["skills"]["entries"][0].pop("confidence"),
            lambda value: value["derived_matcher_signals"]["signals"][0].update(points=True),
            lambda value: value["experience"]["years_by_domain"].append({"domain": "extra"}),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                v2 = self.convert_case()
                mutate(v2)
                self.assert_rejected(v2)

    def test_persistent_profile_id_contract(self):
        v1 = self.cases[0]["expected_canonical_profile"]
        for invalid in (
            "beginner_bilingual_no_degree",
            "prf_" + "0" * 32,
            "prf_" + "a" * 32,
            "prf_" + "A" * 32,
            "prf_1234",
        ):
            with self.subTest(value=invalid):
                with self.assertRaises(CanonicalProfileV2Error) as raised:
                    convert_v1_to_v2(
                        v1,
                        persistent_profile_id=invalid,
                        source_ordinal_resolver=ordinal_resolver,
                    )
                self.assertNotIn(invalid, str(raised.exception))

    def test_ephemeral_matcher_identity_excludes_all_durable_resource_families(self):
        v2 = self.convert_case()
        durable_id = v2["identity"]["profile_id"]
        valid = (
            "beginner_bilingual_no_degree",
            "fixture:software_engineer",
            "123e4567-e89b-12d3-a456-426614174000",
        )
        for matcher_id in valid:
            with self.subTest(matcher_id=matcher_id):
                self.assertEqual(
                    validate_ephemeral_matcher_profile_id(
                        matcher_id,
                        persistent_profile_id=durable_id,
                    ),
                    matcher_id,
                )
                project_v2_to_matcher_v1(v2, matcher_profile_id=matcher_id)

        durable_prefixes = (
            "prf",
            "pvr",
            "pfs",
            "usr",
            "auth",
            "ses",
            "inv",
            "rot",
            "cns",
            "del",
            "life",
            "prn",
            "loa",
            "pab",
            "obe",
        )
        invalid = [
            durable_id,
            f"runtime-{durable_id}-preview",
            f"runtime-{durable_id.upper()}-preview",
            "bad\nidentifier",
            "x" * 129,
            " leading",
        ]
        invalid.extend(f"{prefix}_{'1' * 32}" for prefix in durable_prefixes)
        invalid.extend(f"{prefix.upper()}_{'A' * 32}" for prefix in durable_prefixes)
        invalid.extend(f"{prefix}_not_a_resource_id" for prefix in durable_prefixes)
        for matcher_id in invalid:
            with self.subTest(matcher_id=matcher_id[:16]):
                with self.assertRaises(CanonicalProfileV2Error) as raised:
                    project_v2_to_matcher_v1(v2, matcher_profile_id=matcher_id)
                self.assertIn("invalid_matcher_profile_id", raised.exception.reason_codes)
                self.assertNotIn(matcher_id, str(raised.exception))

    def test_ephemeral_matcher_identity_rejects_embedded_durable_ids_without_boundaries(self):
        v2 = self.convert_case()
        durable_id = v2["identity"]["profile_id"]
        durable_prefixes = (
            "prf",
            "pvr",
            "pfs",
            "usr",
            "auth",
            "ses",
            "inv",
            "rot",
            "cns",
            "del",
            "life",
            "prn",
            "loa",
            "pab",
            "obe",
        )
        embedded = [
            "fixtureprn_" + "1" * 32,
            "abcprf_" + "2" * 32,
            "pvr_" + "3" * 32 + "runtime",
            "prefix_pfs_" + "4" * 32 + "_suffix",
            "abcPRN_" + "A" * 32 + "xyz",
            "firstusr_" + "5" * 32 + "secondses_" + "6" * 32,
        ]
        embedded.extend(
            f"fixture{prefix}_{index:032x}runtime"
            for index, prefix in enumerate(durable_prefixes, start=17)
        )
        for matcher_id in embedded:
            with self.subTest(matcher_id=matcher_id[:20]):
                with self.assertRaises(CanonicalProfileV2Error) as raised:
                    validate_ephemeral_matcher_profile_id(
                        matcher_id,
                        persistent_profile_id=durable_id,
                    )
                self.assertEqual(
                    raised.exception.reason_codes,
                    ("invalid_matcher_profile_id",),
                )
                self.assertNotIn(matcher_id, str(raised.exception))

        near_misses = (
            "fixtureprn_1234",
            "fixtureprn_" + "g" * 32,
            "abcprf_" + "1" * 31 + "xyz",
            "fixture_profile",
            "123e4567-e89b-12d3-a456-426614174000",
        )
        for matcher_id in near_misses:
            with self.subTest(near_miss=matcher_id):
                self.assertEqual(
                    validate_ephemeral_matcher_profile_id(
                        matcher_id,
                        persistent_profile_id=durable_id,
                    ),
                    matcher_id,
                )

    def test_projection_recursively_rejects_any_embedded_durable_resource_id(self):
        v2 = self.convert_case()
        embedded = "Reference abcprn_" + "7" * 32 + "xyz"
        v2["constraints"]["soft_preferences"] = [embedded]
        rebuild_field_sources(v2)
        with self.assertRaises(CanonicalProfileV2Error) as raised:
            project_v2_to_matcher_v1(v2, matcher_profile_id="fixture_profile")
        self.assertEqual(raised.exception.reason_codes, ("persistent_identity_leak",))
        self.assertNotIn(embedded, str(raised.exception))

    def test_projection_rejects_persistent_identity_in_any_projected_field(self):
        v2 = self.convert_case()
        durable_id = v2["identity"]["profile_id"]
        v2["identity"]["display_name"] = f"Profile {durable_id}"
        with self.assertRaises(CanonicalProfileV2Error) as raised:
            project_v2_to_matcher_v1(v2, matcher_profile_id="fixture_profile")
        self.assertIn("persistent_identity_leak", raised.exception.reason_codes)
        self.assertNotIn(durable_id, str(raised.exception))

    def test_every_fixture_projection_is_recursively_free_of_persistent_identity(self):
        for index, case in enumerate(self.cases, start=1):
            with self.subTest(case_id=case["case_id"]):
                v2 = convert_v1_to_v2(
                    case["expected_canonical_profile"],
                    persistent_profile_id=persistent_id(index),
                    source_ordinal_resolver=ordinal_resolver,
                )
                projected = project_v2_to_matcher_v1(
                    v2,
                    matcher_profile_id=case["archetype_id"],
                )
                self.assertFalse(
                    contains_text_fragment(projected, v2["identity"]["profile_id"])
                )

    def test_all_fixture_field_source_paths_are_explicitly_handled(self):
        old_total = 0
        resolver_calls = 0
        removed_total = 0
        new_total = 0

        def resolver(path, _source, _explicit):
            nonlocal resolver_calls
            resolver_calls += 1
            return [1]

        for index, case in enumerate(self.cases, start=1):
            v1 = case["expected_canonical_profile"]
            old_paths = list(v1["provenance"]["field_sources"])
            old_total += len(old_paths)
            removed_total += sum(
                path.startswith("identity.")
                or ".evidence[" in path
                or path.endswith(".evidence")
                for path in old_paths
            )
            v2 = convert_v1_to_v2(
                v1,
                persistent_profile_id=persistent_id(index),
                source_ordinal_resolver=resolver,
            )
            new_total += len(v2["provenance"]["field_sources"])
        self.assertEqual(old_total, 1130)
        self.assertEqual(resolver_calls + removed_total, old_total)
        self.assertEqual(new_total, 1060)

    def test_field_sources_use_ordered_records_and_complete_coverage(self):
        v2 = self.convert_case()
        records = v2["provenance"]["field_sources"]
        self.assertEqual(
            [record["field_path"] for record in records],
            sorted(record["field_path"] for record in records),
        )
        for record in records:
            self.assertEqual(
                set(record),
                {
                    "field_path",
                    "path_version",
                    "source_ordinals",
                    "source_kind",
                    "explicit",
                },
            )
            self.assertEqual(record["path_version"], FIELD_PATH_VERSION)
            self.assertEqual(record["source_ordinals"], [1])
            resolve_field_path(v2, record["field_path"])

    def test_source_resolver_failures_and_ambiguous_ordinals_are_rejected(self):
        v1 = self.cases[0]["expected_canonical_profile"]
        resolvers = (
            lambda _p, _s, _e: [],
            lambda _p, _s, _e: [1, 1],
            lambda _p, _s, _e: [0],
            lambda _p, _s, _e: [17],
            lambda _p, _s, _e: "1",
        )
        for resolver in resolvers:
            with self.subTest(resolver=resolver):
                with self.assertRaises(CanonicalProfileV2Error):
                    convert_v1_to_v2(
                        v1,
                        persistent_profile_id=persistent_id(),
                        source_ordinal_resolver=resolver,
                    )

    def test_stored_source_ordinals_must_be_sorted_unique_lists(self):
        v2 = self.convert_case()
        v2["provenance"]["field_sources"][0]["source_ordinals"] = [2, 1]
        validated = validate_canonical_profile_v2(v2)
        self.assertEqual(
            validated["provenance"]["field_sources"][0]["source_ordinals"],
            [1, 2],
        )
        for ordinals in ([1, 1], [], [0], [17], [True]):
            with self.subTest(ordinals=ordinals):
                v2 = self.convert_case()
                v2["provenance"]["field_sources"][0]["source_ordinals"] = ordinals
                self.assert_rejected(v2, "invalid_source_ordinals")

    def test_years_by_domain_records_cover_all_fixture_values(self):
        expected = []
        actual = []
        for index, case in enumerate(self.cases, start=1):
            expected.extend(case["expected_canonical_profile"]["experience"]["years_by_domain"].items())
            actual.extend(
                (item["domain"], item["years"])
                for item in convert_v1_to_v2(
                    case["expected_canonical_profile"],
                    persistent_profile_id=persistent_id(index),
                    source_ordinal_resolver=ordinal_resolver,
                )["experience"]["years_by_domain"]
            )
        self.assertEqual(len(expected), 8)
        self.assertCountEqual(actual, expected)

    def test_domain_year_provenance_covers_both_label_and_value(self):
        index, case = next(
            (index, case)
            for index, case in enumerate(self.cases, start=1)
            if case["expected_canonical_profile"]["experience"]["years_by_domain"]
        )
        v2 = convert_v1_to_v2(
            case["expected_canonical_profile"],
            persistent_profile_id=persistent_id(index),
            source_ordinal_resolver=ordinal_resolver,
        )
        paths = {record["field_path"] for record in v2["provenance"]["field_sources"]}
        for domain_index in range(len(v2["experience"]["years_by_domain"])):
            self.assertIn(f"experience.years_by_domain[{domain_index}].domain", paths)
            self.assertIn(f"experience.years_by_domain[{domain_index}].years", paths)

    def test_domain_year_bounds_precision_order_and_duplicate_rules(self):
        for years in (-1, 81, True, None, float("inf"), 1.001):
            with self.subTest(years=years):
                v2 = self.convert_case()
                v2["experience"]["years_by_domain"] = [{"domain": "Writing", "years": years}]
                self.assert_rejected(v2)

        v2 = self.convert_case()
        v2["experience"]["years_by_domain"] = [
            {"domain": "software engineering", "years": 7},
            {"domain": "Software  Engineering", "years": 6},
        ]
        self.assert_rejected(v2, "duplicate_domain")

    def test_unicode_case_and_whitespace_comparison_normalization(self):
        self.assertEqual(normalize_comparison_label(" Café  Data "), "café data")
        self.assertEqual(
            normalize_comparison_label(unicodedata.normalize("NFD", "Café")),
            normalize_comparison_label("Café"),
        )
        with self.assertRaises(CanonicalProfileV2Error) as raised:
            normalize_comparison_label("bad\nlabel")
        self.assertIn("prohibited_control_character", raised.exception.reason_codes)

    def test_structured_whitespace_policy_matches_documentation_and_validation(self):
        documentation = (ROOT / "docs" / "canonical_profile_v2.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(STRUCTURED_WHITESPACE_POLICY, documentation)

        for value in (
            "  Cafe   Reviewer  ",
            "Cafe\u2003\u2003Reviewer",
            "Cafe\u00a0\u00a0Reviewer",
            "Cafe\u202f\u202fReviewer",
        ):
            with self.subTest(value=repr(value)):
                self.assertEqual(normalize_comparison_label(value), "cafe reviewer")

        for value in (
            "Cafe\tReviewer",
            "Cafe\nReviewer",
            "Cafe\rReviewer",
            "Cafe\x00Reviewer",
            "Cafe\x1fReviewer",
            "Cafe\x7fReviewer",
            "Cafe\x85Reviewer",
            "Cafe\x9fReviewer",
        ):
            with self.subTest(value=repr(value)):
                with self.assertRaises(CanonicalProfileV2Error) as raised:
                    normalize_comparison_label(value)
                self.assertIn(
                    "prohibited_control_character",
                    raised.exception.reason_codes,
                )

    def test_language_order_and_duplicate_contract(self):
        v2 = self.convert_case()
        reordered = deepcopy(v2)
        reordered["languages"] = list(reversed(reordered["languages"]))
        self.assertEqual(
            canonical_profile_v2_json_bytes(reordered),
            canonical_profile_v2_json_bytes(v2),
        )

        v2 = self.convert_case()
        duplicate = deepcopy(v2["languages"][0])
        duplicate["language"] = duplicate["language"].swapcase()
        v2["languages"].append(duplicate)
        v2["languages"].sort(
            key=lambda item: (
                normalize_comparison_label(item["language"]),
                normalize_comparison_label(item["locale"]),
                item["language"],
                item["locale"],
            )
        )
        self.assert_rejected(v2, "duplicate_language_locale")

    def test_same_base_language_multiple_locales_is_rejected_at_v1_boundary(self):
        v2 = self.convert_case()
        locale_variant = deepcopy(v2["languages"][0])
        locale_variant["locale"] = "Canada"
        v2["languages"].append(locale_variant)
        v2["languages"].sort(
            key=lambda item: (
                normalize_comparison_label(item["language"]),
                normalize_comparison_label(item["locale"]),
                item["language"],
                item["locale"],
            )
        )
        self.assert_rejected(v2, "v1_language_projection_collision")

    def test_skill_and_signal_ordering_and_duplicate_rules(self):
        v2 = self.convert_case()
        reordered = deepcopy(v2)
        reordered["skills"]["entries"] = list(reversed(reordered["skills"]["entries"]))
        self.assertEqual(
            canonical_profile_v2_json_bytes(reordered),
            canonical_profile_v2_json_bytes(v2),
        )

        duplicate = self.convert_case()
        duplicate["skills"]["entries"].append(
            deepcopy(duplicate["skills"]["entries"][0])
        )
        self.assert_rejected(duplicate, "duplicate_skill")

        reordered = self.convert_case()
        reordered["derived_matcher_signals"]["signals"] = list(
            reversed(reordered["derived_matcher_signals"]["signals"])
        )
        self.assertEqual(
            canonical_profile_v2_json_bytes(reordered),
            canonical_profile_v2_json_bytes(v2),
        )

    def test_derived_signal_contract_rejects_duplicates_evidence_and_bad_values(self):
        v2 = self.convert_case()
        signal = deepcopy(v2["derived_matcher_signals"]["signals"][0])

        duplicate = deepcopy(v2)
        duplicate["derived_matcher_signals"]["signals"].append(deepcopy(signal))
        self.assert_rejected(duplicate, "duplicate_signal_reason")

        conflicting = deepcopy(v2)
        changed = deepcopy(signal)
        changed["points"] = min(changed["points"] + 1, 100)
        conflicting["derived_matcher_signals"]["signals"].append(changed)
        self.assert_rejected(conflicting, "duplicate_signal_reason")

        for extra_field in ("evidence", "evidence_snippet", "raw_content", "metadata"):
            with self.subTest(extra_field=extra_field):
                candidate = deepcopy(v2)
                candidate["derived_matcher_signals"]["signals"][0][extra_field] = []
                error = self.assert_rejected(candidate)
                self.assertTrue(
                    set(error.reason_codes)
                    & {"invalid_signal_item", "prohibited_structural_key"}
                )

        for reason in ("", "Display Reason", "bad-reason", "évidence"):
            with self.subTest(reason=reason):
                candidate = deepcopy(v2)
                candidate["derived_matcher_signals"]["signals"][0]["reason"] = reason
                self.assert_rejected(candidate, "invalid_signal_reason")

        for points in (True, 0, -1, 101, 1.5, float("inf")):
            with self.subTest(points=points):
                candidate = deepcopy(v2)
                candidate["derived_matcher_signals"]["signals"][0]["points"] = points
                self.assert_rejected(candidate)

        candidate = deepcopy(v2)
        candidate["derived_matcher_signals"]["signals"][0]["confidence"] = "certain"
        self.assert_rejected(candidate, "invalid_confidence")

    def test_signal_keyword_collisions_and_boundaries(self):
        v2 = self.convert_case()
        signal = v2["derived_matcher_signals"]["signals"][0]
        signal["keywords"] = [f"keyword {index:02d}" for index in range(MAX_SIGNAL_KEYWORDS)]
        validated = validate_canonical_profile_v2(v2)
        self.assertEqual(
            len(validated["derived_matcher_signals"]["signals"][0]["keywords"]),
            MAX_SIGNAL_KEYWORDS,
        )

        too_many = deepcopy(v2)
        too_many["derived_matcher_signals"]["signals"][0]["keywords"].append(
            "keyword overflow"
        )
        self.assert_rejected(too_many, "invalid_string_list")

        for keywords in (
            ["Data", "data"],
            ["Café", unicodedata.normalize("NFD", "Café")],
            ["data value", " data\u00a0 value "],
        ):
            with self.subTest(keywords=keywords):
                candidate = self.convert_case()
                candidate["derived_matcher_signals"]["signals"][0]["keywords"] = keywords
                self.assert_rejected(candidate, "duplicate_string_value")

        candidate = self.convert_case()
        candidate["derived_matcher_signals"]["signals"][0]["keywords"] = ["x" * 129]
        self.assert_rejected(candidate, "invalid_string_list")

    def test_authoritative_limits_are_consistent_and_reachable(self):
        with self.assertRaises(TypeError):
            CANONICAL_PROFILE_V2_LIMITS["languages"] = 99
        for local_name in (
            "languages",
            "domain_year_records",
            "skill_records",
            "derived_signals",
            "signal_keywords",
            "field_source_records",
            "source_ordinals_per_field",
            "string_list_items",
        ):
            with self.subTest(limit=local_name):
                self.assertLessEqual(
                    CANONICAL_PROFILE_V2_LIMITS[local_name],
                    CANONICAL_PROFILE_V2_LIMITS["list_children"],
                )
        self.assertEqual(MAX_FIELD_SOURCES, 256)
        self.assertEqual(MAX_SKILL_ENTRIES, 96)

        documentation = (ROOT / "docs" / "canonical_profile_v2.md").read_text(
            encoding="utf-8"
        )
        documented_limits = {
            key: int(value)
            for key, value in re.findall(
                r"^\| `([a-z0-9_]+)` \| ([0-9]+) \|$",
                documentation,
                flags=re.MULTILINE,
            )
        }
        self.assertEqual(documented_limits, dict(CANONICAL_PROFILE_V2_LIMITS))

        languages = self.convert_case()
        template_language = deepcopy(languages["languages"][0])
        languages["languages"] = [
            dict(template_language, language=f"Language {index:02d}", locale="")
            for index in range(MAX_LANGUAGES)
        ]
        rebuild_field_sources(languages)
        self.assertEqual(len(validate_canonical_profile_v2(languages)["languages"]), 32)

        domain_years = self.convert_case()
        domain_years["experience"]["years_by_domain"] = [
            {"domain": f"Domain {index:02d}", "years": 1}
            for index in range(MAX_DOMAIN_YEARS)
        ]
        rebuild_field_sources(domain_years)
        self.assertEqual(
            len(validate_canonical_profile_v2(domain_years)["experience"]["years_by_domain"]),
            64,
        )

        skills = self.convert_case()
        skills["skills"]["entries"] = [
            {"skill": f"Skill {index:02d}", "confidence": "unknown"}
            for index in range(MAX_SKILL_ENTRIES)
        ]
        rebuild_field_sources(skills)
        self.assertEqual(
            len(validate_canonical_profile_v2(skills)["skills"]["entries"]),
            MAX_SKILL_ENTRIES,
        )

        signals = self.convert_case()
        signals["derived_matcher_signals"]["signals"] = [
            {
                "reason": f"signal_{index:02d}",
                "keywords": [f"keyword {index:02d}"],
                "points": 1,
                "confidence": "unknown",
            }
            for index in range(MAX_SIGNALS)
        ]
        self.assertEqual(
            len(validate_canonical_profile_v2(signals)["derived_matcher_signals"]["signals"]),
            MAX_SIGNALS,
        )

        field_sources = deepcopy(skills)
        rebuild_field_sources(field_sources)
        missing = MAX_FIELD_SOURCES - len(field_sources["provenance"]["field_sources"])
        self.assertGreaterEqual(missing, 0)
        field_sources["constraints"]["soft_preferences"] = [
            f"Preference {index:02d}" for index in range(missing)
        ]
        rebuild_field_sources(field_sources)
        self.assertEqual(len(field_sources["provenance"]["field_sources"]), MAX_FIELD_SOURCES)
        self.assertEqual(
            len(validate_canonical_profile_v2(field_sources)["provenance"]["field_sources"]),
            MAX_FIELD_SOURCES,
        )

        ordinal_boundary = self.convert_case()
        ordinal_boundary["provenance"]["field_sources"][0]["source_ordinals"] = list(
            range(1, 17)
        )
        self.assertEqual(
            validate_canonical_profile_v2(ordinal_boundary)["provenance"]["field_sources"][0][
                "source_ordinals"
            ],
            list(range(1, 17)),
        )

    def test_field_path_parser_accepts_material_paths(self):
        v2 = self.convert_case()
        valid = (
            "languages[0].language",
            "experience.professional_domains[0]",
            "location.remote_eligibility",
        )
        for path in valid:
            with self.subTest(path=path):
                self.assertTrue(parse_field_path(path))
                self.assertIsNotNone(resolve_field_path(v2, path))

    def test_field_path_parser_rejects_operators_and_unsafe_paths(self):
        invalid = (
            "$.location.country",
            "languages[*].language",
            "languages..language",
            "languages[0]..language",
            "languages[-1].language",
            "languages[01].language",
            "languages[256].language",
            "languages[0].language ",
            "languages[0].'language'",
            "identity.profile_id",
            "provenance.reviewed",
            "derived_matcher_signals.signals[0].reason",
            "location",
            "location.missing",
        )
        for path in invalid:
            with self.subTest(path=path):
                with self.assertRaises(CanonicalProfileV2Error):
                    resolve_field_path(self.convert_case(), path)

    def test_field_path_depth_and_length_are_bounded(self):
        deep = ".".join(["location"] + ["field"] * 12)
        with self.assertRaises(CanonicalProfileV2Error):
            parse_field_path(deep)
        with self.assertRaises(CanonicalProfileV2Error):
            parse_field_path("location." + "x" * 250)

    def test_raw_json_duplicate_keys_are_rejected_at_every_depth(self):
        samples = (
            '{"schema_version":"canonical_profile_v2","schema_version":"canonical_profile_v2"}',
            '{"outer":{"value":1,"value":1}}',
            '{"items":[{"value":1,"value":2}]}',
        )
        for sample in samples:
            with self.subTest(sample=sample):
                with self.assertRaises(CanonicalProfileV2Error) as raised:
                    parse_canonical_profile_v2_json(sample)
                self.assertIn("duplicate_json_key", raised.exception.reason_codes)

    def test_raw_json_rejects_nonfinite_json5_malformed_and_nonobject_values(self):
        samples = ("NaN", "Infinity", "-Infinity", "[]", "null", "{bad: 1}", '{"x": /* no */ 1}')
        for sample in samples:
            with self.subTest(sample=sample):
                with self.assertRaises(CanonicalProfileV2Error):
                    parse_canonical_profile_v2_json(sample)

    def test_deep_and_cyclic_inputs_have_bounded_sanitized_failures(self):
        def nested(levels, alternating=False):
            value = {}
            for index in range(levels):
                if alternating and index % 2:
                    value = [value]
                else:
                    value = {"x": value}
            return value

        exact = nested(CANONICAL_PROFILE_V2_LIMITS["document_depth"])
        with self.assertRaises(CanonicalProfileV2Error) as raised:
            validate_canonical_profile_v2(exact)
        self.assertNotIn("document_too_deep", raised.exception.reason_codes)

        for value in (
            nested(CANONICAL_PROFILE_V2_LIMITS["document_depth"] + 1),
            nested(500),
            nested(1500, alternating=True),
        ):
            for boundary in (
                validate_canonical_profile_v2,
                canonical_profile_v2_json_bytes,
                lambda item: project_v2_to_matcher_v1(
                    item,
                    matcher_profile_id="fixture_profile",
                ),
                lambda item: resolve_field_path(item, "location.country"),
                lambda item: convert_v1_to_v2(
                    item,
                    persistent_profile_id=persistent_id(),
                    source_ordinal_resolver=ordinal_resolver,
                ),
            ):
                with self.subTest(boundary=boundary, size=len(str(type(value)))):
                    with self.assertRaises(CanonicalProfileV2Error) as failure:
                        boundary(value)
                    self.assertIn("document_too_deep", failure.exception.reason_codes)
                    self.assertNotIsInstance(failure.exception, RecursionError)
                    self.assertLess(len(str(failure.exception)), 1024)

        cyclic_dict = {}
        cyclic_dict["x"] = cyclic_dict
        cyclic_list = []
        cyclic_list.append(cyclic_list)
        left = {}
        right = {"left": left}
        left["right"] = right
        for value in (cyclic_dict, cyclic_list, left):
            with self.subTest(kind=type(value).__name__):
                with self.assertRaises(CanonicalProfileV2Error) as failure:
                    validate_canonical_profile_v2(value)
                self.assertIn("cyclic_structure", failure.exception.reason_codes)

    def test_deep_raw_json_never_exposes_parser_runtime_errors(self):
        raw = '{"x":' * 1500 + "0" + "}" * 1500
        malformed = raw[:-100]
        for payload in (raw, malformed):
            with self.subTest(length=len(payload)):
                with self.assertRaises(CanonicalProfileV2Error) as failure:
                    parse_canonical_profile_v2_json(payload)
                self.assertTrue(
                    set(failure.exception.reason_codes)
                    & {"document_too_deep", "invalid_json"}
                )
                self.assertNotIn(payload[:40], str(failure.exception))

    def test_deterministic_serialization_round_trip(self):
        v2 = self.convert_case()
        encoded = canonical_profile_v2_json_bytes(v2)
        self.assertEqual(encoded, canonical_profile_v2_json_bytes(deepcopy(v2)))
        self.assertNotIn(b"\r", encoded)
        self.assertNotIn(b"\n", encoded)
        self.assertEqual(parse_canonical_profile_v2_json(encoded), v2)
        self.assertEqual(encoded, canonical_profile_v2_json_bytes(json.loads(encoded)))

    def test_canonical_serialization_has_cross_process_known_answer(self):
        expected = "c1f74046b7a22a1deb8b27fcb33fec2d63c0e74048cdf326f046ff58ab970a89"
        local = hashlib.sha256(
            canonical_profile_v2_json_bytes(self.convert_case())
        ).hexdigest()
        self.assertEqual(local, expected)
        script = """
import hashlib
from tests.test_canonical_profile_v2 import CanonicalProfileV2Tests
from wahojobs.profiles.canonical_v2 import canonical_profile_v2_json_bytes
CanonicalProfileV2Tests.setUpClass()
case = CanonicalProfileV2Tests('runTest')
print(hashlib.sha256(canonical_profile_v2_json_bytes(case.convert_case())).hexdigest())
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout.strip(), expected)

    def test_semantically_equivalent_values_have_identical_canonical_bytes(self):
        base = self.convert_case()

        unicode_a = deepcopy(base)
        unicode_a["identity"]["display_name"] = "  Café\u00a0  Reviewer  "
        unicode_b = deepcopy(base)
        unicode_b["identity"]["display_name"] = unicodedata.normalize(
            "NFD", "Café Reviewer"
        )
        self.assertEqual(
            canonical_profile_v2_json_bytes(unicode_a),
            canonical_profile_v2_json_bytes(unicode_b),
        )

        dictionaries = {key: value for key, value in reversed(tuple(base.items()))}
        self.assertEqual(
            canonical_profile_v2_json_bytes(base),
            canonical_profile_v2_json_bytes(dictionaries),
        )

        ordered = deepcopy(base)
        if len(ordered["constraints"]["avoid_keywords"]) < 2:
            ordered["constraints"]["avoid_keywords"] = ["Alpha", "Beta"]
            rebuild_field_sources(ordered)
        reversed_values = deepcopy(ordered)
        reversed_values["constraints"]["avoid_keywords"] = list(
            reversed(reversed_values["constraints"]["avoid_keywords"])
        )
        self.assertEqual(
            canonical_profile_v2_json_bytes(ordered),
            canonical_profile_v2_json_bytes(reversed_values),
        )

        with_years = next(
            index
            for index, case in enumerate(self.cases)
            if case["expected_canonical_profile"]["experience"]["years_by_domain"]
        )
        integer = self.convert_case(with_years)
        integer["experience"]["years_by_domain"][0]["years"] = 0
        positive_zero = deepcopy(integer)
        positive_zero["experience"]["years_by_domain"][0]["years"] = 0.0
        negative_zero = deepcopy(integer)
        negative_zero["experience"]["years_by_domain"][0]["years"] = -0.0
        self.assertEqual(
            canonical_profile_v2_json_bytes(integer),
            canonical_profile_v2_json_bytes(positive_zero),
        )
        self.assertEqual(
            canonical_profile_v2_json_bytes(integer),
            canonical_profile_v2_json_bytes(negative_zero),
        )

        decimal_a = self.convert_case(with_years)
        decimal_a["experience"]["years_by_domain"][0]["years"] = 1.5
        decimal_b = deepcopy(decimal_a)
        decimal_b["experience"]["years_by_domain"][0]["years"] = float("1.50")
        self.assertEqual(
            canonical_profile_v2_json_bytes(decimal_a),
            canonical_profile_v2_json_bytes(decimal_b),
        )

    def test_genuinely_different_display_case_has_different_canonical_bytes(self):
        lower = self.convert_case()
        lower["identity"]["display_name"] = "Profile Name"
        upper = deepcopy(lower)
        upper["identity"]["display_name"] = "PROFILE NAME"
        self.assertNotEqual(
            canonical_profile_v2_json_bytes(lower),
            canonical_profile_v2_json_bytes(upper),
        )

    def test_conversion_and_projection_do_not_mutate_callers(self):
        v1 = deepcopy(self.cases[0]["expected_canonical_profile"])
        before = deepcopy(v1)
        v2 = convert_v1_to_v2(
            v1,
            persistent_profile_id=persistent_id(),
            source_ordinal_resolver=ordinal_resolver,
        )
        self.assertEqual(v1, before)
        v2_before = deepcopy(v2)
        project_v2_to_matcher_v1(v2, matcher_profile_id="ephemeral_matcher")
        self.assertEqual(v2, v2_before)

    def test_repeated_conversion_and_projection_are_deterministic(self):
        v1 = self.cases[0]["expected_canonical_profile"]
        first = convert_v1_to_v2(
            v1,
            persistent_profile_id=persistent_id(),
            source_ordinal_resolver=ordinal_resolver,
        )
        second = convert_v1_to_v2(
            v1,
            persistent_profile_id=persistent_id(),
            source_ordinal_resolver=ordinal_resolver,
        )
        self.assertEqual(first, second)
        self.assertEqual(
            project_v2_to_matcher_v1(first, matcher_profile_id="ephemeral"),
            project_v2_to_matcher_v1(second, matcher_profile_id="ephemeral"),
        )

    def test_projection_derives_language_proficiency_and_never_leaks_persistent_id(self):
        v2 = self.convert_case()
        projected = project_v2_to_matcher_v1(v2, matcher_profile_id="ephemeral_profile")
        matcher_profile = canonical_to_matcher_profile(projected)
        expected = {item["language"]: item["proficiency"] for item in v2["languages"]}
        self.assertEqual(matcher_profile["language_proficiency"], expected)
        self.assertEqual(matcher_profile["profile_id"], "ephemeral_profile")
        self.assertNotIn(v2["identity"]["profile_id"], json.dumps(projected))
        with self.assertRaises(CanonicalProfileV2Error):
            project_v2_to_matcher_v1(v2, matcher_profile_id=persistent_id(2))

    def test_projection_uses_empty_runtime_year_map_without_semantic_loss_to_matcher(self):
        case = next(
            case
            for case in self.cases
            if case["expected_canonical_profile"]["experience"]["years_by_domain"]
        )
        v2 = convert_v1_to_v2(
            case["expected_canonical_profile"],
            persistent_profile_id=persistent_id(),
            source_ordinal_resolver=ordinal_resolver,
        )
        self.assertTrue(v2["experience"]["years_by_domain"])
        projected = project_v2_to_matcher_v1(v2, matcher_profile_id=case["archetype_id"])
        self.assertEqual(projected["experience"]["years_by_domain"], {})

    def test_all_fixture_match_labels_and_sections_are_semantically_equivalent(self):
        golden = benchmark.load_fixture()
        rows = [benchmark.build_snapshot_row(case) for case in golden["cases"]]
        comparisons = 0
        changes = []
        for index, case in enumerate(self.cases, start=1):
            v1 = case["expected_canonical_profile"]
            original_profile = canonical_to_matcher_profile(v1)
            v2 = convert_v1_to_v2(
                v1,
                persistent_profile_id=persistent_id(index),
                source_ordinal_resolver=ordinal_resolver,
            )
            projected_profile = canonical_to_matcher_profile(
                project_v2_to_matcher_v1(v2, matcher_profile_id=case["archetype_id"])
            )
            for opportunity_case, row in zip(golden["cases"], rows):
                original = matcher.score_opportunity(original_profile, row)
                projected = matcher.score_opportunity(projected_profile, row)
                original_result = (
                    benchmark.match_strength_from_score(original["score"]),
                    original["effective_product_section"],
                )
                projected_result = (
                    benchmark.match_strength_from_score(projected["score"]),
                    projected["effective_product_section"],
                )
                comparisons += 1
                if original_result != projected_result:
                    changes.append((case["case_id"], opportunity_case["case_id"]))
            original_top = matcher.rank_opportunities(
                original_profile,
                rows,
                False,
                10,
                min_score=-999,
                require_personalized_eligible=False,
            )
            projected_top = matcher.rank_opportunities(
                projected_profile,
                rows,
                False,
                10,
                min_score=-999,
                require_personalized_eligible=False,
            )
            self.assertEqual(
                [item["job_id"] for item in projected_top],
                [item["job_id"] for item in original_top],
            )
        self.assertEqual(comparisons, 4000)
        self.assertEqual(changes, [])

    def test_human_reviewed_benchmark_remains_26_29_26(self):
        fixture = benchmark.load_fixture()
        profiles = benchmark.load_benchmark_profiles(fixture)
        evaluated = [
            benchmark.evaluate_case(case, profiles[case["profile_id"]], [], matcher)
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

    def test_errors_are_bounded_and_redacted(self):
        private = "private-person@example.invalid bearer-secret-value"
        v2 = self.convert_case()
        v2["location"]["region"] = private
        v2["raw_input"] = private
        error = self.assert_rejected(v2)
        for rendered in (str(error), repr(error), json.dumps(error.as_dict())):
            self.assertNotIn(private, rendered)
            self.assertNotIn("example.invalid", rendered)
            self.assertLess(len(rendered), 1024)

    def test_module_import_has_no_database_network_or_file_write_side_effect(self):
        script = r'''
import builtins
import pathlib
import socket
import sqlite3

def blocked(*args, **kwargs):
    raise RuntimeError("side effect")

sqlite3.connect = blocked
socket.socket = blocked
original_open = builtins.open
def guarded_open(file, mode="r", *args, **kwargs):
    if any(flag in mode for flag in ("w", "a", "x", "+")):
        raise RuntimeError("file write")
    return original_open(file, mode, *args, **kwargs)
builtins.open = guarded_open
from wahojobs.profiles import canonical_v2
print(canonical_v2.SCHEMA_VERSION)
'''
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout.strip(), SCHEMA_VERSION)

    def test_only_dormant_migration_modules_import_or_name_v2(self):
        references = []
        for root in (ROOT / "wahojobs", ROOT / "scripts"):
            for path in root.rglob("*.py"):
                if path.name == "canonical_v2.py":
                    continue
                text = path.read_text(encoding="utf-8")
                if "canonical_v2" in text:
                    references.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(len(references), len(set(references)))
        self.assertEqual(
            references,
            [
                "wahojobs/google_oidc_authorization_transaction_schema.py",
                "wahojobs/persistent_profiles.py",
                "wahojobs/persistent_profiles_reconciliation.py",
                "wahojobs/persistent_profiles_repository.py",
                "wahojobs/persistent_profile_canonical_v2_schema.py",
                "wahojobs/persistent_profile_schema.py",
                "scripts/persistent_profile_canonical_v2_migration.py",
            ],
        )

    def test_workspace_database_is_not_accessed_or_changed(self):
        before = (
            DB_PATH.stat().st_size,
            DB_PATH.stat().st_mtime_ns,
            hashlib.sha256(DB_PATH.read_bytes()).hexdigest(),
        )
        self.convert_case()
        after = (
            DB_PATH.stat().st_size,
            DB_PATH.stat().st_mtime_ns,
            hashlib.sha256(DB_PATH.read_bytes()).hexdigest(),
        )
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()

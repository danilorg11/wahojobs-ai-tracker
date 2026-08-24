import hashlib
import unittest
from copy import deepcopy

from tests.test_canonical_profile_v2 import (
    load_cases,
    ordinal_resolver,
    persistent_id,
    rebuild_field_sources,
)
from wahojobs.matching.taxonomy import CAREER_LEVELS, OCCUPATIONAL_FAMILIES
from wahojobs.opportunity_enrichment import SENIORITY_VALUES
from wahojobs.profiles.canonical import (
    PROFILE_SOURCE_USER_CONFIRMATION,
    field_sources_for_profile,
    validate_preferences,
)
from wahojobs.profiles.canonical_v2 import (
    CanonicalProfileV2Error,
    add_user_confirmed_preference_model_v1,
    add_user_confirmed_preference_model_v2,
    canonical_profile_v2_json_bytes,
    convert_v1_to_v2,
    parse_canonical_profile_v2_json,
    project_v2_to_matcher_v1,
    project_v2_to_review_v1,
    validate_canonical_profile_v2,
)
from wahojobs.profiles.preference_model import (
    ACCEPTED_CAREER_LEVELS,
    COMPENSATION_MINIMUM_KINDS,
    COMPENSATION_PERIODS,
    EMPLOYMENT_RELATIONSHIPS,
    ENGAGEMENT_TERMS,
    ISO_4217_CURRENCIES,
    JOB_INTEREST_CODES,
    PHONE_VOICE_MODES,
    MAX_COMPENSATION_EXPECTATIONS,
    ProfilePreferenceModelError,
    SCHEDULE_COORDINATION_MODES,
    SCHEDULE_FLEXIBILITY_MODES,
    SCHEDULE_TIME_WINDOWS,
    SCHEDULE_TIME_OF_DAY,
    SCHEDULE_WORKING_DAYS,
    SOFT_PREFERENCE_DIMENSIONS,
    WORKLOADS,
    canonicalize_profile_preferences_v1,
    canonicalize_profile_preferences_v2,
    empty_profile_preferences_v1,
    empty_profile_preferences_v2,
    legacy_preferences_to_preference_draft,
    preference_model_to_legacy_preferences,
    profile_preferences_v1_to_v2,
    profile_preferences_v2_to_v1_matcher_compat,
    profile_preference_control_catalog_v1,
    profile_preference_control_catalog_v2,
)


BASELINE_V2_SHA256 = "c1f74046b7a22a1deb8b27fcb33fec2d63c0e74048cdf326f046ff58ab970a89"


def with_preference_model(profile, model):
    candidate = deepcopy(profile)
    candidate["preferences"]["preference_model"] = deepcopy(model)
    rebuild_field_sources(candidate)
    candidate["provenance"]["field_sources"] = [
        item
        for item in candidate["provenance"]["field_sources"]
        if item["field_path"] != "preferences.preference_model.schema_version"
    ]
    return candidate


class ProfilePreferenceModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v1 = load_cases()[0]["expected_canonical_profile"]

    def base_v2(self):
        return convert_v1_to_v2(
            self.v1,
            persistent_profile_id=persistent_id(1),
            source_ordinal_resolver=ordinal_resolver,
        )

    def populated_model(self):
        model = empty_profile_preferences_v1()
        model.update(
            {
                "employment_relationships": ["independent_contractor", "employee"],
                "workloads": ["part_time", "full_time"],
                "engagement_terms": ["temporary", "fixed_term"],
                "accepted_phone_voice_modes": ["phone", "non_phone"],
                "job_interests": ["search_evaluation", "customer_support"],
                "accepted_career_levels": ["senior", "entry"],
            }
        )
        model["schedule"] = {
            "flexibility_modes": ["flexible", "fixed"],
            "coordination_modes": ["synchronous", "asynchronous"],
            "time_windows": ["weekends", "business_hours"],
        }
        model["compensation"] = {
            "minimum_kind": "preferred",
            "amount": "00025.00",
            "currency": "brl",
            "period": "hour",
        }
        return model

    def populated_model_v2(self):
        model = profile_preferences_v1_to_v2(self.populated_model())
        model["compensation_expectations"].append(
            {
                "minimum_kind": "strict",
                "amount": "90000",
                "currency": "USD",
                "period": "year",
            }
        )
        return model

    def test_v2_contract_splits_schedule_and_supports_multiple_compensation_units(self):
        model = empty_profile_preferences_v2()
        model["schedule"] = {
            "flexibility_modes": ["flexible"],
            "coordination_modes": ["asynchronous"],
            "working_days": ["weekends", "weekdays"],
            "time_of_day": ["evenings", "business_hours"],
        }
        model["compensation_expectations"] = [
            {
                "minimum_kind": "strict",
                "amount": "090000.00",
                "currency": "usd",
                "period": "year",
            },
            {
                "minimum_kind": "preferred",
                "amount": "30.00",
                "currency": "usd",
                "period": "hour",
            },
        ]
        canonical = canonicalize_profile_preferences_v2(model)
        self.assertEqual(canonical["schema_version"], "profile_preferences_v2")
        self.assertEqual(canonical["schedule"]["working_days"], ["weekdays", "weekends"])
        self.assertEqual(canonical["schedule"]["time_of_day"], ["business_hours", "evenings"])
        self.assertEqual(
            [(item["amount"], item["currency"], item["period"], item["minimum_kind"]) for item in canonical["compensation_expectations"]],
            [("30", "USD", "hour", "preferred"), ("90000", "USD", "year", "strict")],
        )

    def test_v2_rejects_duplicate_units_invalid_items_and_oversize_lists(self):
        duplicate = empty_profile_preferences_v2()
        duplicate["compensation_expectations"] = [
            {"minimum_kind": "preferred", "amount": "30", "currency": "USD", "period": "hour"},
            {"minimum_kind": "strict", "amount": "35", "currency": "usd", "period": "hour"},
        ]
        invalid_kind = empty_profile_preferences_v2()
        invalid_kind["compensation_expectations"] = [
            {"minimum_kind": "none", "amount": "30", "currency": "USD", "period": "hour"}
        ]
        invalid_currency_type = empty_profile_preferences_v2()
        invalid_currency_type["compensation_expectations"] = [
            {"minimum_kind": "preferred", "amount": "30", "currency": ["USD"], "period": "hour"}
        ]
        invalid_period = empty_profile_preferences_v2()
        invalid_period["compensation_expectations"] = [
            {"minimum_kind": "preferred", "amount": "30", "currency": "USD", "period": "project"}
        ]
        oversize = empty_profile_preferences_v2()
        oversize["compensation_expectations"] = [
            {"minimum_kind": "preferred", "amount": str(index + 1), "currency": currency, "period": "hour"}
            for index, currency in enumerate(sorted(ISO_4217_CURRENCIES)[: MAX_COMPENSATION_EXPECTATIONS + 1])
        ]
        for value, code in (
            (duplicate, "duplicate_compensation_currency_period"),
            (invalid_kind, "invalid_compensation_minimum_kind"),
            (invalid_currency_type, "invalid_compensation_currency"),
            (invalid_period, "invalid_compensation_period"),
            (oversize, "invalid_compensation_expectations"),
        ):
            with self.subTest(code=code):
                with self.assertRaises(ProfilePreferenceModelError) as context:
                    canonicalize_profile_preferences_v2(value)
                self.assertIn(code, context.exception.reason_codes)

    def test_v1_lifts_losslessly_and_v2_legacy_projection_is_deterministic(self):
        v1 = canonicalize_profile_preferences_v1(self.populated_model())
        v2 = profile_preferences_v1_to_v2(v1)
        self.assertEqual(v2["schedule"]["working_days"], ["weekends"])
        self.assertEqual(v2["schedule"]["time_of_day"], ["business_hours"])
        self.assertEqual(len(v2["compensation_expectations"]), 1)
        self.assertEqual(profile_preferences_v2_to_v1_matcher_compat(v2), v1)
        multi = canonicalize_profile_preferences_v2(self.populated_model_v2())
        compat = profile_preferences_v2_to_v1_matcher_compat(multi)
        self.assertEqual(compat["compensation"]["minimum_kind"], "none")
        legacy = preference_model_to_legacy_preferences(multi)
        self.assertIn("preferred minimum: BRL 25 per hour", legacy["rate_pay_preference"])
        self.assertIn("strict minimum: USD 90000 per year", legacy["rate_pay_preference"])

    def test_v2_catalog_is_exact_and_keeps_schedule_axes_independent(self):
        catalog = profile_preference_control_catalog_v2()
        expected = {
            "schedule.flexibility_modes": SCHEDULE_FLEXIBILITY_MODES,
            "schedule.coordination_modes": SCHEDULE_COORDINATION_MODES,
            "schedule.working_days": SCHEDULE_WORKING_DAYS,
            "schedule.time_of_day": SCHEDULE_TIME_OF_DAY,
        }
        by_id = {item["id"]: item for item in catalog["dimensions"]}
        for identifier, allowed in expected.items():
            self.assertEqual(
                {item["code"] for item in by_id[identifier]["choices"]},
                allowed,
            )
        self.assertEqual(
            catalog["compensation"]["maximum_expectations"],
            MAX_COMPENSATION_EXPECTATIONS,
        )

    def test_empty_contract_is_exact_and_means_unrestricted(self):
        model = canonicalize_profile_preferences_v1(empty_profile_preferences_v1())
        self.assertEqual(model["schema_version"], "profile_preferences_v1")
        self.assertEqual(model["employment_relationships"], [])
        self.assertEqual(model["workloads"], [])
        self.assertEqual(model["engagement_terms"], [])
        self.assertEqual(model["accepted_phone_voice_modes"], [])
        self.assertEqual(model["job_interests"], [])
        self.assertEqual(model["accepted_career_levels"], [])
        self.assertTrue(all(value == [] for value in model["schedule"].values()))
        self.assertEqual(
            model["compensation"],
            {"minimum_kind": "none", "amount": None, "currency": None, "period": None},
        )

    def test_canonicalization_sorts_dimensions_and_normalizes_compensation(self):
        model = canonicalize_profile_preferences_v1(self.populated_model())
        self.assertEqual(model["employment_relationships"], ["employee", "independent_contractor"])
        self.assertEqual(model["workloads"], ["full_time", "part_time"])
        self.assertEqual(model["schedule"]["time_windows"], ["business_hours", "weekends"])
        self.assertEqual(model["job_interests"], ["customer_support", "search_evaluation"])
        self.assertEqual(model["compensation"]["amount"], "25")
        self.assertEqual(model["compensation"]["currency"], "BRL")

    def test_contract_rejects_extra_fields_invalid_enums_and_noncanonical_money(self):
        invalid_models = []

        extra = empty_profile_preferences_v1()
        extra["workplace_modes"] = ["hybrid"]
        invalid_models.append(extra)

        duplicate = empty_profile_preferences_v1()
        duplicate["workloads"] = ["full_time", "full_time"]
        invalid_models.append(duplicate)

        volunteer = empty_profile_preferences_v1()
        volunteer["employment_relationships"] = ["volunteer"]
        invalid_models.append(volunteer)

        project_period = empty_profile_preferences_v1()
        project_period["compensation"] = {
            "minimum_kind": "strict",
            "amount": "50",
            "currency": "USD",
            "period": "project",
        }
        invalid_models.append(project_period)

        fake_currency = empty_profile_preferences_v1()
        fake_currency["compensation"] = {
            "minimum_kind": "strict",
            "amount": "50",
            "currency": "ZZZ",
            "period": "month",
        }
        invalid_models.append(fake_currency)

        zero = empty_profile_preferences_v1()
        zero["compensation"] = {
            "minimum_kind": "preferred",
            "amount": "0",
            "currency": "USD",
            "period": "year",
        }
        invalid_models.append(zero)

        incomplete_none = empty_profile_preferences_v1()
        incomplete_none["compensation"]["amount"] = "20"
        invalid_models.append(incomplete_none)

        for model in invalid_models:
            with self.subTest(model=model):
                with self.assertRaises(ProfilePreferenceModelError):
                    canonicalize_profile_preferences_v1(model)

    def test_shared_job_and_target_career_vocabularies_stay_coherent(self):
        self.assertEqual(JOB_INTEREST_CODES, OCCUPATIONAL_FAMILIES)
        self.assertEqual(ACCEPTED_CAREER_LEVELS, CAREER_LEVELS)
        self.assertEqual(SENIORITY_VALUES, ACCEPTED_CAREER_LEVELS | {"unknown"})
        self.assertNotIn("unknown", ACCEPTED_CAREER_LEVELS)
        self.assertEqual(SOFT_PREFERENCE_DIMENSIONS, {"job_interests"})

    def test_onboarding_control_catalog_is_derived_from_every_contract_enum(self):
        catalog = profile_preference_control_catalog_v1()
        self.assertEqual(catalog["schema_version"], "profile_preferences_v1")
        expected = {
            "employment_relationships": EMPLOYMENT_RELATIONSHIPS,
            "workloads": WORKLOADS,
            "engagement_terms": ENGAGEMENT_TERMS,
            "schedule.flexibility_modes": SCHEDULE_FLEXIBILITY_MODES,
            "schedule.coordination_modes": SCHEDULE_COORDINATION_MODES,
            "schedule.time_windows": SCHEDULE_TIME_WINDOWS,
            "accepted_phone_voice_modes": PHONE_VOICE_MODES,
            "job_interests": JOB_INTEREST_CODES,
            "accepted_career_levels": ACCEPTED_CAREER_LEVELS,
        }
        self.assertEqual(
            {item["id"] for item in catalog["dimensions"]},
            set(expected),
        )
        for dimension in catalog["dimensions"]:
            with self.subTest(dimension=dimension["id"]):
                self.assertEqual(
                    {choice["code"] for choice in dimension["choices"]},
                    expected[dimension["id"]],
                )
                self.assertTrue(
                    all(choice["label"] and choice["description"] for choice in dimension["choices"])
                )
        self.assertEqual(
            {item["code"] for item in catalog["compensation"]["minimum_kinds"]},
            COMPENSATION_MINIMUM_KINDS,
        )
        self.assertEqual(
            {item["code"] for item in catalog["compensation"]["periods"]},
            COMPENSATION_PERIODS,
        )
        self.assertEqual(
            set(catalog["compensation"]["currencies"]),
            ISO_4217_CURRENCIES,
        )

    def test_existing_v2_writer_output_is_byte_for_byte_unchanged(self):
        profile = self.base_v2()
        self.assertNotIn("preference_model", profile["preferences"])
        self.assertEqual(
            hashlib.sha256(canonical_profile_v2_json_bytes(profile)).hexdigest(),
            BASELINE_V2_SHA256,
        )

    def test_user_confirmed_writer_requires_exact_legacy_projection(self):
        model = canonicalize_profile_preferences_v1(self.populated_model())
        v1 = deepcopy(self.v1)
        v1["preferences"] = preference_model_to_legacy_preferences(model)
        v1["provenance"]["field_sources"] = {}
        v1["provenance"]["field_sources"] = field_sources_for_profile(
            v1,
            PROFILE_SOURCE_USER_CONFIRMATION,
            explicit=True,
        )
        base = convert_v1_to_v2(
            v1,
            persistent_profile_id=persistent_id(3),
            source_ordinal_resolver=ordinal_resolver,
        )
        written = add_user_confirmed_preference_model_v1(
            base,
            model,
            source_ordinal_resolver=ordinal_resolver,
        )
        self.assertEqual(written["preferences"]["preference_model"], model)
        self.assertEqual(project_v2_to_matcher_v1(written, matcher_profile_id="writer"), project_v2_to_matcher_v1(base, matcher_profile_id="writer"))

        divergent = self.base_v2()
        with self.assertRaises(CanonicalProfileV2Error) as context:
            add_user_confirmed_preference_model_v1(
                divergent,
                model,
                source_ordinal_resolver=ordinal_resolver,
            )
        self.assertIn("preference_legacy_projection_mismatch", context.exception.reason_codes)

    def test_v2_writer_is_additive_and_legacy_matcher_projection_stays_identical(self):
        model = canonicalize_profile_preferences_v2(self.populated_model_v2())
        v1 = deepcopy(self.v1)
        v1["preferences"] = preference_model_to_legacy_preferences(model)
        v1["provenance"]["field_sources"] = field_sources_for_profile(
            v1,
            PROFILE_SOURCE_USER_CONFIRMATION,
            explicit=True,
        )
        base = convert_v1_to_v2(
            v1,
            persistent_profile_id=persistent_id(4),
            source_ordinal_resolver=ordinal_resolver,
        )
        baseline = project_v2_to_matcher_v1(base, matcher_profile_id="v2-writer")
        written = add_user_confirmed_preference_model_v2(
            base,
            model,
            source_ordinal_resolver=ordinal_resolver,
        )
        self.assertEqual(written["preferences"]["preference_model"], model)
        self.assertEqual(
            project_v2_to_matcher_v1(written, matcher_profile_id="v2-writer"),
            baseline,
        )
        parsed = parse_canonical_profile_v2_json(
            canonical_profile_v2_json_bytes(written)
        )
        self.assertEqual(parsed, written)

        invalid = deepcopy(model)
        invalid["compensation_expectations"][0]["period"] = "project"
        with self.assertRaises(CanonicalProfileV2Error) as context:
            add_user_confirmed_preference_model_v2(
                base,
                invalid,
                source_ordinal_resolver=ordinal_resolver,
            )
        self.assertIn("invalid_compensation_period", context.exception.reason_codes)

    def test_v2_reads_and_canonicalizes_optional_preference_model(self):
        candidate = with_preference_model(self.base_v2(), self.populated_model())
        validated = validate_canonical_profile_v2(candidate)
        model = validated["preferences"]["preference_model"]
        self.assertEqual(model["employment_relationships"], ["employee", "independent_contractor"])
        self.assertEqual(model["compensation"]["amount"], "25")
        parsed = parse_canonical_profile_v2_json(canonical_profile_v2_json_bytes(validated))
        self.assertEqual(parsed, validated)

    def test_v2_rejects_invalid_optional_preference_model_fail_closed(self):
        model = empty_profile_preferences_v1()
        model["compensation"] = {
            "minimum_kind": "strict",
            "amount": "25",
            "currency": "ZZZ",
            "period": "hour",
        }
        candidate = with_preference_model(self.base_v2(), model)
        with self.assertRaises(CanonicalProfileV2Error) as context:
            validate_canonical_profile_v2(candidate)
        self.assertIn("invalid_compensation_currency", context.exception.reason_codes)

    def test_current_matcher_and_review_projections_ignore_nested_authority(self):
        baseline = self.base_v2()
        baseline_matcher = project_v2_to_matcher_v1(
            baseline, matcher_profile_id="preference-foundation"
        )
        candidate = with_preference_model(baseline, self.populated_model())
        projected = project_v2_to_matcher_v1(
            candidate, matcher_profile_id="preference-foundation"
        )
        self.assertEqual(projected, baseline_matcher)
        self.assertNotIn("preference_model", projected["preferences"])
        self.assertNotIn(
            "preference_model",
            project_v2_to_review_v1(candidate)["preferences"],
        )

    def test_legacy_draft_is_conservative_and_reports_contract_ambiguity(self):
        legacy = {
            "remote": True,
            "flexible": True,
            "employment_types": [
                "full-time",
                "freelance",
                "contract",
                "temporary",
                "entry-level",
            ],
            "synchronous_preference": "asynchronous",
            "phone_preference": "non-phone preferred",
            "schedule": ["weekdays", "evenings"],
            "availability": "available",
            "rate_pay_preference": "private free-text expectation",
            "target_opportunity_types": ["customer_support", "untyped private label"],
            "preferred_task_types": ["data_annotation"],
            "work_preferences": ["remote", "flexible", "contract"],
        }
        draft = legacy_preferences_to_preference_draft(legacy)
        model = draft["preference_model"]
        self.assertEqual(model["employment_relationships"], ["independent_contractor"])
        self.assertNotIn("fixed_term", model["engagement_terms"])
        self.assertEqual(model["workloads"], ["full_time"])
        self.assertEqual(model["accepted_career_levels"], ["entry"])
        self.assertEqual(model["job_interests"], ["customer_support", "data_annotation"])
        self.assertEqual(model["compensation"]["minimum_kind"], "none")
        codes = {item["code"] for item in draft["ambiguities"]}
        self.assertEqual(
            codes,
            {
                "legacy_compensation_requires_confirmation",
                "legacy_contract_requires_confirmation",
                "legacy_job_interest_requires_confirmation",
                "legacy_phone_strength_requires_confirmation",
            },
        )
        self.assertNotIn("private free-text expectation", repr(draft))
        self.assertNotIn("untyped private label", repr(draft))

    def test_new_to_legacy_projection_is_pure_valid_and_lossy_by_design(self):
        model = canonicalize_profile_preferences_v1(self.populated_model())
        projected = preference_model_to_legacy_preferences(
            model,
            legacy_base={"remote": True, "availability": "available"},
        )
        errors = []
        validate_preferences(projected, errors)
        self.assertEqual(errors, [])
        self.assertTrue(projected["remote"])
        self.assertIn("freelance", projected["employment_types"])
        self.assertIn("contract", projected["employment_types"])
        self.assertNotIn("employee", projected["employment_types"])
        self.assertEqual(projected["phone_preference"], "phone acceptable")
        self.assertEqual(projected["target_opportunity_types"], ["customer_support", "search_evaluation"])
        self.assertEqual(projected["rate_pay_preference"], "preferred minimum: BRL 25 per hour")
        self.assertNotIn("preference_model", projected)

    def test_legacy_work_preferences_map_only_deterministic_dimensions(self):
        draft = legacy_preferences_to_preference_draft(
            {
                "flexible": True,
                "work_preferences": [
                    "part-time",
                    "seasonal",
                    "entry-level",
                    "flexible",
                ]
            }
        )
        model = draft["preference_model"]
        self.assertEqual(model["workloads"], ["part_time"])
        self.assertEqual(model["engagement_terms"], ["seasonal"])
        self.assertEqual(model["accepted_career_levels"], ["entry"])
        self.assertEqual(model["schedule"]["flexibility_modes"], ["flexible"])
        self.assertEqual(draft["ambiguities"], [])


if __name__ == "__main__":
    unittest.main()

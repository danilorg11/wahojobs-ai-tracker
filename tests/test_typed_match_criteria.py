import unittest
from copy import deepcopy
from dataclasses import replace

from scripts import profile_match_digest as matcher
from tests.test_canonical_profile_v2 import load_cases, ordinal_resolver, persistent_id
from tests.test_profile_preference_model import with_preference_model
from wahojobs.matching.typed_criteria import (
    CriterionOutcomeV1,
    MatchCriteriaV1,
    OpportunityCompensationV1,
    OpportunityCriteriaV1,
    OpportunityDimensionV1,
    ProfileCriterionV1,
    aggregate_single_criterion_relaxations_v1,
    compare_compensation_criterion,
    evaluate_match_criteria_shadow,
    evaluate_primary_preference_admission_v1,
    evaluate_single_criterion_relaxations_v1,
    match_criteria_v1_from_profile,
    project_opportunity_criteria_v1,
    run_typed_match_criteria_shadow,
)
from wahojobs.opportunity_enrichment import add_evidence, blank_document
from wahojobs.profiles.canonical import canonical_to_matcher_profile
from wahojobs.profiles.canonical_v2 import (
    convert_v1_to_v2,
    project_v2_to_matcher_v1,
    validate_canonical_profile_v2,
)
from wahojobs.profiles.preference_model import (
    empty_profile_preferences_v1,
    empty_profile_preferences_v2,
    profile_preferences_v1_to_v2,
)


def profile_model(*, minimum_kind="preferred", amount="30"):
    model = empty_profile_preferences_v1()
    model["employment_relationships"] = ["employee"]
    model["workloads"] = ["full_time"]
    model["engagement_terms"] = ["fixed_term"]
    model["schedule"] = {
        "flexibility_modes": ["fixed"],
        "coordination_modes": ["asynchronous"],
        "time_windows": ["weekdays"],
    }
    model["accepted_phone_voice_modes"] = ["non_phone"]
    model["job_interests"] = ["data_annotation"]
    model["accepted_career_levels"] = ["entry"]
    model["compensation"] = {
        "minimum_kind": minimum_kind,
        "amount": amount if minimum_kind != "none" else None,
        "currency": "USD" if minimum_kind != "none" else None,
        "period": "hour" if minimum_kind != "none" else None,
    }
    return model


def profile_v2(*, minimum_kind="preferred", amount="30"):
    fixture = load_cases()[0]["expected_canonical_profile"]
    base = convert_v1_to_v2(
        fixture,
        persistent_profile_id=persistent_id(1),
        source_ordinal_resolver=ordinal_resolver,
    )
    return validate_canonical_profile_v2(
        with_preference_model(
            base,
            profile_model(minimum_kind=minimum_kind, amount=amount),
        )
    )


def enrichment(
    *,
    engagement_type="full_time",
    schedule_type="fixed",
    role_family="data_annotation",
    work_activities=("audio_speech", "data_annotation"),
    seniority="entry",
    disclosed=True,
    currency="USD",
    amount_min=25,
    amount_max=40,
    period="hour",
    amount_type="range",
    stale_fields=(),
    evidence_basis="deterministic_parse",
    evidence_confidence="high",
    field_sources=None,
):
    document = blank_document()
    role = document["attributes"]["role"]
    role["role_family"] = role_family
    role["work_activities"] = sorted(work_activities)
    role["seniority"] = seniority
    arrangement = document["attributes"]["work_arrangement"]
    arrangement["engagement_type"] = engagement_type
    arrangement["schedule_type"] = schedule_type
    compensation = document["attributes"]["compensation"]
    compensation.update(
        {
            "disclosed": disclosed,
            "currency": currency,
            "amount_min": amount_min,
            "amount_max": amount_max,
            "period": period,
            "amount_type": amount_type,
        }
    )
    paths = [
        "attributes.role.role_family",
        "attributes.role.work_activities",
        "attributes.role.seniority",
        "attributes.work_arrangement.engagement_type",
        "attributes.work_arrangement.schedule_type",
        "attributes.compensation.disclosed",
    ]
    if disclosed is True:
        paths.extend(
            [
                "attributes.compensation.currency",
                "attributes.compensation.amount_min",
                "attributes.compensation.amount_max",
                "attributes.compensation.period",
                "attributes.compensation.amount_type",
            ]
        )
    for path in paths:
        add_evidence(
            document["field_evidence"],
            path,
            "synthetic_fixture",
            "synthetic evidence",
            evidence_basis,
            evidence_confidence,
        )
    document["field_evidence"] = sorted(
        document["field_evidence"],
        key=lambda item: (
            item["field_path"],
            item["source_ref"],
            item["evidence_text"],
        ),
    )
    return {
        "document": document,
        "field_sources": dict(field_sources or {}),
        "stale_override_fields": list(stale_fields),
    }


def compensation_criterion(*, criterion_class="strict_preference", amount="30"):
    return ProfileCriterionV1(
        criterion_id="preferences.compensation.minimum",
        criterion_class=criterion_class,
        dimension="compensation_minimum",
        operator="minimum",
        minimum_amount=amount,
        currency="USD",
        period="hour",
    )


def opportunity_compensation(
    *,
    disclosed=True,
    currency="USD",
    minimum="30",
    maximum="30",
    period="hour",
    amount_type="exact",
    status="known",
    reason="compensation_structured",
):
    return OpportunityCompensationV1(
        status=status,
        disclosed=disclosed,
        currency=currency,
        amount_min=minimum,
        amount_max=maximum,
        period=period,
        amount_type=amount_type,
        reason_code=reason,
    )


def matcher_row(*, job_id=7002, title="Synthetic Data Annotation Role"):
    return {
        "job_id": job_id,
        "title": title,
        "canonical_title": None,
        "source": "Synthetic Inventory",
        "source_slug": "synthetic",
        "source_tier": "core",
        "location": "Remote",
        "url": "https://jobs.example.test/synthetic-role",
        "department": "Data",
        "expertise": "Data",
        "source_category": "data_annotation",
        "commitment": "Full-time",
        "opportunity_kind": "live_posting",
        "availability_basis": "api_feed",
        "inventory_model": "live_feed",
        "market_count_policy": "count_live",
        "include_in_live_market_estimate": 1,
        "canonical_opportunity_id": job_id,
        "canonical_is_active": True,
        "job_is_active": True,
        "job_last_seen_at": "2026-08-22T00:00:00+00:00",
        "latest_successful_source_run_at": "2026-08-22T00:00:00+00:00",
        "source_run_started_at": "2026-08-22T00:00:00+00:00",
        "source_run_id": job_id,
        "source_run_qualifies": True,
        "language": None,
        "language_locale": None,
        "required_languages": None,
    }


def nonblocking_eligibility(*, failing_id=None, unknown_id=None):
    specifications = (
        ("eligibility.required_languages", "required_language_eligibility"),
        ("eligibility.location", "location_eligibility"),
        ("eligibility.credentials_licenses", "credential_eligibility"),
        ("eligibility.professional_domain", "professional_domain_eligibility"),
    )
    return tuple(
        CriterionOutcomeV1(
            criterion_id=criterion_id,
            criterion_class="eligibility",
            dimension=dimension,
            outcome=(
                "fail"
                if criterion_id == failing_id
                else "unknown"
                if criterion_id == unknown_id
                else "not_applicable"
            ),
            reason_code="synthetic_eligibility",
            potentially_relaxable=False,
        )
        for criterion_id, dimension in specifications
    )


def criteria_with_model(model):
    return match_criteria_v1_from_profile(
        validate_canonical_profile_v2(
            with_preference_model(profile_v2(minimum_kind="none"), model)
        )
    )


def opportunity_for_dimension(dimension, value, *, known=True):
    unknown = OpportunityDimensionV1("unknown", (), "synthetic_unknown")
    dimensions = {
        "employment_relationships": unknown,
        "workloads": unknown,
        "engagement_terms": unknown,
        "schedule_flexibility_modes": unknown,
        "schedule_coordination_modes": unknown,
        "schedule_time_windows": unknown,
        "schedule_working_days": unknown,
        "schedule_time_of_day": unknown,
        "phone_voice_modes": unknown,
        "job_interests": unknown,
        "career_levels": unknown,
    }
    fields = {
        "employment_relationship": "employment_relationships",
        "workload": "workloads",
        "engagement_term": "engagement_terms",
        "schedule_flexibility": "schedule_flexibility_modes",
        "schedule_coordination": "schedule_coordination_modes",
        "schedule_time_window": "schedule_time_windows",
        "schedule_working_day": "schedule_working_days",
        "schedule_time_of_day": "schedule_time_of_day",
        "phone_voice": "phone_voice_modes",
        "job_interest": "job_interests",
        "career_level": "career_levels",
    }
    dimensions[fields[dimension]] = (
        OpportunityDimensionV1("known", (value,), "synthetic_known")
        if known
        else unknown
    )
    return OpportunityCriteriaV1(
        **dimensions,
        compensation=OpportunityCompensationV1(
            status="unknown",
            disclosed=None,
            currency=None,
            amount_min=None,
            amount_max=None,
            period=None,
            amount_type="unknown",
            reason_code="synthetic_unknown",
        ),
    )


class TypedMatchCriteriaTests(unittest.TestCase):
    def test_v2_empty_job_interests_is_unrestricted_for_ai_training_work(self):
        criteria = criteria_with_model(empty_profile_preferences_v2())
        self.assertFalse(
            any(
                item.dimension == "job_interest"
                for item in criteria.soft_preference_criteria
            )
        )

        opportunity = opportunity_for_dimension(
            "job_interest",
            "data_annotation",
        )
        evaluation = evaluate_match_criteria_shadow(criteria, opportunity)
        admission = evaluate_primary_preference_admission_v1(
            criteria,
            evaluation.outcomes,
        )
        self.assertEqual(admission.status, "keep")
        self.assertEqual(
            evaluate_single_criterion_relaxations_v1(
                criteria,
                opportunity,
                nonblocking_eligibility(),
                opportunity_reference="canonical:1",
                original_rank=1,
            ),
            (),
        )

    def test_v2_single_expectation_uses_native_basis_and_split_schedule_criteria(self):
        v1_profile = profile_v2()
        v1_criteria = match_criteria_v1_from_profile(v1_profile)
        model_v2 = profile_preferences_v1_to_v2(profile_model())
        v2_profile = validate_canonical_profile_v2(
            with_preference_model(
                v1_profile,
                model_v2,
            )
        )
        v2_criteria = match_criteria_v1_from_profile(v2_profile)
        self.assertEqual(
            {
                item.criterion_id
                for item in v2_criteria.soft_preference_criteria
                if item.dimension == "compensation_minimum"
            },
            {"preferences.compensation_expectations.USD.hour.minimum"},
        )
        self.assertTrue(
            any(
                item.dimension == "schedule_working_day"
                for item in v2_criteria.soft_preference_criteria
            )
        )
        self.assertFalse(
            any(
                item.dimension == "schedule_time_window"
                for item in v2_criteria.soft_preference_criteria
            )
        )
        self.assertEqual(
            {
                item.dimension
                for item in v1_criteria.soft_preference_criteria
                if item.dimension not in {"schedule_time_window", "compensation_minimum"}
            },
            {
                item.dimension
                for item in v2_criteria.soft_preference_criteria
                if item.dimension not in {"schedule_working_day", "compensation_minimum"}
            },
        )

    def test_v2_multiple_compensation_expectations_project_independently(self):
        model = profile_preferences_v1_to_v2(profile_model())
        model["compensation_expectations"].append(
            {
                "minimum_kind": "strict",
                "amount": "90000",
                "currency": "USD",
                "period": "year",
            }
        )
        base = profile_v2(minimum_kind="none")
        criteria = match_criteria_v1_from_profile(
            validate_canonical_profile_v2(with_preference_model(base, model))
        )
        compensation = {
            item.criterion_id: item
            for item in (
                *criteria.strict_preference_criteria,
                *criteria.soft_preference_criteria,
            )
            if item.dimension == "compensation_minimum"
        }
        self.assertEqual(
            set(compensation),
            {
                "preferences.compensation_expectations.USD.hour.minimum",
                "preferences.compensation_expectations.USD.year.minimum",
            },
        )
        self.assertEqual(
            compensation["preferences.compensation_expectations.USD.hour.minimum"].criterion_class,
            "soft_preference",
        )
        self.assertEqual(
            compensation["preferences.compensation_expectations.USD.year.minimum"].criterion_class,
            "strict_preference",
        )
        self.assertTrue(
            any(
                item.dimension == "schedule_working_day"
                for item in criteria.soft_preference_criteria
            )
        )

    def test_v2_compensation_applies_only_the_matching_currency_period_basis(self):
        model = empty_profile_preferences_v2()
        model["compensation_expectations"] = [
            {
                "minimum_kind": "preferred",
                "amount": "30",
                "currency": "USD",
                "period": "hour",
            },
            {
                "minimum_kind": "strict",
                "amount": "90000",
                "currency": "USD",
                "period": "year",
            },
            {
                "minimum_kind": "preferred",
                "amount": "25",
                "currency": "EUR",
                "period": "hour",
            },
        ]
        criteria = criteria_with_model(model)

        def evaluate(**kwargs):
            opportunity = project_opportunity_criteria_v1(
                effective_enrichment=enrichment(
                    amount_type="exact",
                    **kwargs,
                )
            )
            outcomes = evaluate_match_criteria_shadow(criteria, opportunity).outcomes
            return (
                {item.criterion_id: item for item in outcomes},
                evaluate_primary_preference_admission_v1(criteria, outcomes),
            )

        hourly, hourly_admission = evaluate(
            currency="USD", amount_min=22, amount_max=22, period="hour"
        )
        self.assertEqual(
            hourly["preferences.compensation_expectations.USD.hour.minimum"].outcome,
            "fail",
        )
        self.assertEqual(
            hourly["preferences.compensation_expectations.USD.year.minimum"].outcome,
            "not_applicable",
        )
        self.assertEqual(
            hourly["preferences.compensation_expectations.EUR.hour.minimum"].outcome,
            "not_applicable",
        )
        self.assertEqual(hourly_admission.status, "exclude")

        yearly, yearly_admission = evaluate(
            currency="USD", amount_min=95000, amount_max=95000, period="year"
        )
        self.assertEqual(
            yearly["preferences.compensation_expectations.USD.year.minimum"].outcome,
            "pass",
        )
        self.assertEqual(
            yearly["preferences.compensation_expectations.USD.hour.minimum"].outcome,
            "not_applicable",
        )
        self.assertEqual(yearly_admission.status, "keep")

        euros, euros_admission = evaluate(
            currency="EUR", amount_min=26, amount_max=26, period="hour"
        )
        self.assertEqual(
            euros["preferences.compensation_expectations.EUR.hour.minimum"].outcome,
            "pass",
        )
        self.assertEqual(euros_admission.status, "keep")

    def test_v2_incompatible_basis_is_ignored_but_unknown_strict_pay_excludes(self):
        model = empty_profile_preferences_v2()
        model["compensation_expectations"] = [
            {
                "minimum_kind": "strict",
                "amount": "30",
                "currency": "USD",
                "period": "hour",
            }
        ]
        criteria = criteria_with_model(model)
        incompatible = project_opportunity_criteria_v1(
            effective_enrichment=enrichment(
                currency="EUR",
                amount_min=5000,
                amount_max=5000,
                period="month",
                amount_type="exact",
            )
        )
        outcomes = evaluate_match_criteria_shadow(criteria, incompatible).outcomes
        self.assertEqual(outcomes[0].outcome, "not_applicable")
        self.assertEqual(
            outcomes[0].reason_code,
            "compensation_expectation_basis_not_applicable",
        )
        self.assertEqual(
            evaluate_primary_preference_admission_v1(criteria, outcomes).status,
            "keep",
        )

        undisclosed = replace(
            opportunity_for_dimension("workload", "full_time"),
            compensation=opportunity_compensation(
                disclosed=False,
                currency=None,
                minimum=None,
                maximum=None,
                period=None,
                amount_type="unknown",
            ),
        )
        unknown_outcomes = evaluate_match_criteria_shadow(
            criteria,
            undisclosed,
        ).outcomes
        self.assertEqual(unknown_outcomes[0].outcome, "unknown")
        self.assertEqual(
            evaluate_primary_preference_admission_v1(
                criteria,
                unknown_outcomes,
            ).status,
            "exclude",
        )

    def test_v2_schedule_dimensions_evaluate_independently_and_unknown_keeps(self):
        model = empty_profile_preferences_v2()
        model["schedule"]["working_days"] = ["weekdays"]
        model["schedule"]["time_of_day"] = ["business_hours"]
        criteria = criteria_with_model(model)

        weekend = opportunity_for_dimension(
            "schedule_working_day",
            "weekends",
        )
        weekend_outcomes = {
            item.criterion_id: item
            for item in evaluate_match_criteria_shadow(criteria, weekend).outcomes
        }
        self.assertEqual(
            weekend_outcomes["preferences.schedule.working_days"].outcome,
            "fail",
        )
        self.assertEqual(
            weekend_outcomes["preferences.schedule.time_of_day"].outcome,
            "unknown",
        )
        self.assertEqual(
            evaluate_primary_preference_admission_v1(
                criteria,
                tuple(weekend_outcomes.values()),
            ).status,
            "exclude",
        )

        weekdays = opportunity_for_dimension(
            "schedule_working_day",
            "weekdays",
        )
        weekday_outcomes = evaluate_match_criteria_shadow(criteria, weekdays).outcomes
        self.assertEqual(
            evaluate_primary_preference_admission_v1(
                criteria,
                weekday_outcomes,
            ).status,
            "keep",
        )
        projected = project_opportunity_criteria_v1(
            effective_enrichment=enrichment(schedule_type="fixed")
        )
        self.assertEqual(projected.schedule_working_days.status, "unknown")
        self.assertEqual(projected.schedule_time_of_day.status, "unknown")
        self.assertEqual(projected.schedule_coordination_modes.status, "unknown")
        self.assertEqual(projected.schedule_flexibility_modes.status, "known")

    def test_v2_schedule_and_compensation_relaxations_require_known_evidence(self):
        schedule_model = empty_profile_preferences_v2()
        schedule_model["schedule"]["working_days"] = ["weekdays"]
        schedule_criteria = criteria_with_model(schedule_model)
        schedule_candidates = evaluate_single_criterion_relaxations_v1(
            schedule_criteria,
            opportunity_for_dimension("schedule_working_day", "weekends"),
            nonblocking_eligibility(),
            opportunity_reference="canonical:601",
            original_rank=4,
        )
        self.assertEqual(len(schedule_candidates), 1)
        self.assertEqual(
            schedule_candidates[0].criterion_id,
            "preferences.schedule.working_days",
        )
        self.assertEqual(
            schedule_candidates[0].relaxation_type,
            "allow_schedule_working_day",
        )
        self.assertEqual(
            evaluate_single_criterion_relaxations_v1(
                schedule_criteria,
                opportunity_for_dimension(
                    "schedule_working_day",
                    "weekends",
                    known=False,
                ),
                nonblocking_eligibility(),
                opportunity_reference="canonical:602",
                original_rank=5,
            ),
            (),
        )

        compensation_model = empty_profile_preferences_v2()
        compensation_model["compensation_expectations"] = [
            {
                "minimum_kind": "preferred",
                "amount": "25",
                "currency": "USD",
                "period": "hour",
            },
            {
                "minimum_kind": "strict",
                "amount": "90000",
                "currency": "USD",
                "period": "year",
            },
        ]
        compensation_criteria = criteria_with_model(compensation_model)
        comparable = project_opportunity_criteria_v1(
            effective_enrichment=enrichment(
                amount_min=22,
                amount_max=22,
                amount_type="exact",
                currency="USD",
                period="hour",
            )
        )
        candidates = evaluate_single_criterion_relaxations_v1(
            compensation_criteria,
            comparable,
            nonblocking_eligibility(),
            opportunity_reference="canonical:603",
            original_rank=6,
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(
            candidates[0].criterion_id,
            "preferences.compensation_expectations.USD.hour.minimum",
        )
        self.assertEqual(candidates[0].proposed_minimum_amount, "22")

        unknown_pay = replace(
            opportunity_for_dimension("workload", "full_time"),
            compensation=opportunity_compensation(
                disclosed=False,
                currency=None,
                minimum=None,
                maximum=None,
                period=None,
                amount_type="unknown",
            ),
        )
        self.assertEqual(
            evaluate_single_criterion_relaxations_v1(
                compensation_criteria,
                unknown_pay,
                nonblocking_eligibility(),
                opportunity_reference="canonical:604",
                original_rank=7,
            ),
            (),
        )

    def test_profile_criteria_preserve_eligibility_strict_and_soft_groups(self):
        criteria = match_criteria_v1_from_profile(
            profile_v2(minimum_kind="strict")
        )
        self.assertEqual(criteria.source_status, "present")
        self.assertEqual(criteria.eligibility_criteria, ())
        self.assertEqual(
            {item.criterion_id for item in criteria.strict_preference_criteria},
            {
                "preferences.compensation.minimum",
            },
        )
        self.assertEqual(
            {item.criterion_id for item in criteria.soft_preference_criteria},
            {
                "preferences.accepted_career_levels",
                "preferences.accepted_phone_voice_modes",
                "preferences.employment_relationships",
                "preferences.engagement_terms",
                "preferences.job_interests",
                "preferences.schedule.coordination_modes",
                "preferences.schedule.flexibility_modes",
                "preferences.schedule.time_windows",
                "preferences.workloads",
            },
        )

        preferred = match_criteria_v1_from_profile(
            profile_v2(minimum_kind="preferred")
        )
        compensation = next(
            item
            for item in preferred.soft_preference_criteria
            if item.dimension == "compensation_minimum"
        )
        self.assertEqual(compensation.criterion_class, "soft_preference")
        self.assertEqual(preferred.strict_preference_criteria, ())

    def test_profile_without_authoritative_model_has_no_typed_criteria(self):
        fixture = load_cases()[0]["expected_canonical_profile"]
        existing = convert_v1_to_v2(
            fixture,
            persistent_profile_id=persistent_id(1),
            source_ordinal_resolver=ordinal_resolver,
        )
        criteria = match_criteria_v1_from_profile(existing)
        self.assertEqual(criteria.source_status, "absent")
        self.assertEqual(criteria.all_criteria(), ())

    def test_opportunity_projection_splits_only_honest_engagement_dimensions(self):
        cases = (
            (
                "full_time",
                ("unknown", ()),
                ("known", ("full_time",)),
                ("unknown", ()),
            ),
            (
                "freelance",
                ("known", ("independent_contractor",)),
                ("unknown", ()),
                ("unknown", ()),
            ),
            (
                "temporary",
                ("unknown", ()),
                ("unknown", ()),
                ("known", ("temporary",)),
            ),
            (
                "contract",
                ("unknown", ()),
                ("unknown", ()),
                ("unknown", ()),
            ),
        )
        for engagement_type, relationship, workload, term in cases:
            with self.subTest(engagement_type=engagement_type):
                projected = project_opportunity_criteria_v1(
                    effective_enrichment=enrichment(
                        engagement_type=engagement_type
                    )
                )
                self.assertEqual(
                    (
                        projected.employment_relationships.status,
                        projected.employment_relationships.values,
                    ),
                    relationship,
                )
                self.assertEqual(
                    (projected.workloads.status, projected.workloads.values),
                    workload,
                )
                self.assertEqual(
                    (
                        projected.engagement_terms.status,
                        projected.engagement_terms.values,
                    ),
                    term,
                )
                if engagement_type == "contract":
                    self.assertEqual(
                        projected.employment_relationships.reason_code,
                        "engagement_type_contract_ambiguous",
                    )

    def test_structured_projection_uses_reliable_schedule_taxonomy_and_pay(self):
        projected = project_opportunity_criteria_v1(
            effective_enrichment=enrichment()
        )
        self.assertEqual(projected.schedule_flexibility_modes.values, ("fixed",))
        self.assertEqual(projected.schedule_coordination_modes.status, "unknown")
        self.assertEqual(projected.schedule_time_windows.status, "unknown")
        self.assertEqual(projected.phone_voice_modes.status, "unknown")
        self.assertEqual(
            projected.phone_voice_modes.reason_code,
            "phone_voice_not_structured",
        )
        self.assertEqual(projected.job_interests.values, ("data_annotation",))
        self.assertEqual(projected.career_levels.values, ("entry",))
        self.assertEqual(projected.compensation.currency, "USD")
        self.assertEqual(projected.compensation.amount_min, "25")
        self.assertEqual(projected.compensation.amount_max, "40")

    def test_unknown_and_stale_enrichment_evidence_is_not_promoted(self):
        projected = project_opportunity_criteria_v1(
            effective_enrichment=enrichment(
                stale_fields=("attributes.work_arrangement.schedule_type",)
            )
        )
        self.assertEqual(projected.schedule_flexibility_modes.status, "unknown")
        self.assertEqual(
            projected.schedule_flexibility_modes.reason_code,
            "schedule_flexibility_unknown",
        )

    def test_llm_evidence_cannot_exclude_schedule_or_compensation(self):
        criteria = match_criteria_v1_from_profile(profile_v2())
        opportunity = project_opportunity_criteria_v1(
            effective_enrichment=enrichment(
                schedule_type="flexible",
                amount_min=20,
                amount_max=20,
                amount_type="exact",
                evidence_basis="llm_source_evidence",
                evidence_confidence="high",
            )
        )
        outcomes = {
            item.criterion_id: item
            for item in evaluate_match_criteria_shadow(
                criteria,
                opportunity,
            ).outcomes
        }

        self.assertEqual(opportunity.schedule_flexibility_modes.status, "unknown")
        self.assertEqual(opportunity.compensation.status, "unknown")
        self.assertEqual(
            outcomes["preferences.schedule.flexibility_modes"].outcome,
            "unknown",
        )
        self.assertEqual(
            outcomes["preferences.compensation.minimum"].outcome,
            "unknown",
        )
        self.assertEqual(
            evaluate_primary_preference_admission_v1(
                criteria,
                tuple(outcomes.values()),
            ).status,
            "keep",
        )

    def test_objective_and_human_override_typed_facts_remain_authoritative(self):
        criteria = match_criteria_v1_from_profile(profile_v2())
        schedule_path = "attributes.work_arrangement.schedule_type"
        cases = (
            ("deterministic_parse", {}, "automatic deterministic parse"),
            ("source_explicit", {}, "source-explicit evidence"),
            (
                "llm_source_evidence",
                {schedule_path: "human_override"},
                "human override",
            ),
        )

        for evidence_basis, field_sources, label in cases:
            with self.subTest(authority=label):
                opportunity = project_opportunity_criteria_v1(
                    effective_enrichment=enrichment(
                        schedule_type="flexible",
                        amount_min=40,
                        amount_max=40,
                        amount_type="exact",
                        evidence_basis=evidence_basis,
                        field_sources=field_sources,
                    )
                )
                outcomes = evaluate_match_criteria_shadow(
                    criteria,
                    opportunity,
                ).outcomes
                schedule = next(
                    item
                    for item in outcomes
                    if item.criterion_id
                    == "preferences.schedule.flexibility_modes"
                )
                self.assertEqual(
                    opportunity.schedule_flexibility_modes.values,
                    ("flexible",),
                )
                self.assertEqual(schedule.outcome, "fail")
                self.assertEqual(
                    evaluate_primary_preference_admission_v1(
                        criteria,
                        outcomes,
                    ).status,
                    "exclude",
                )

    def test_shadow_outcomes_are_stable_and_class_controlled(self):
        criteria = match_criteria_v1_from_profile(profile_v2())
        opportunity = project_opportunity_criteria_v1(
            effective_enrichment=enrichment()
        )
        evaluation = evaluate_match_criteria_shadow(criteria, opportunity)
        outcomes = {item.criterion_id: item for item in evaluation.outcomes}

        self.assertEqual(
            outcomes["preferences.workloads"].outcome,
            "pass",
        )
        self.assertEqual(
            outcomes["preferences.employment_relationships"].outcome,
            "unknown",
        )
        phone = outcomes["preferences.accepted_phone_voice_modes"]
        self.assertEqual(
            (phone.outcome, phone.reason_code),
            ("unknown", "phone_voice_not_structured"),
        )
        self.assertFalse(phone.potentially_relaxable)
        compensation = outcomes["preferences.compensation.minimum"]
        self.assertEqual(
            (compensation.outcome, compensation.reason_code),
            ("unknown", "compensation_preferred_range_overlap"),
        )
        self.assertFalse(compensation.potentially_relaxable)

    def test_eligibility_and_strict_failures_never_relax_but_soft_failures_may(self):
        criteria = MatchCriteriaV1(
            source_status="present",
            eligibility_criteria=(
                ProfileCriterionV1(
                    criterion_id="eligibility.relationship",
                    criterion_class="eligibility",
                    dimension="employment_relationship",
                    operator="any_of",
                    accepted_values=("employee",),
                ),
            ),
            strict_preference_criteria=(
                ProfileCriterionV1(
                    criterion_id="strict.relationship",
                    criterion_class="strict_preference",
                    dimension="employment_relationship",
                    operator="any_of",
                    accepted_values=("employee",),
                ),
            ),
            soft_preference_criteria=(
                ProfileCriterionV1(
                    criterion_id="soft.relationship",
                    criterion_class="soft_preference",
                    dimension="employment_relationship",
                    operator="any_of",
                    accepted_values=("employee",),
                ),
            ),
        )
        opportunity = project_opportunity_criteria_v1(
            effective_enrichment=enrichment(engagement_type="freelance")
        )
        outcomes = evaluate_match_criteria_shadow(criteria, opportunity).outcomes
        self.assertEqual([item.outcome for item in outcomes], ["fail"] * 3)
        self.assertEqual(
            {
                item.criterion_class: item.potentially_relaxable
                for item in outcomes
            },
            {
                "eligibility": False,
                "strict_preference": False,
                "soft_preference": True,
            },
        )

    def test_primary_admission_applies_strict_soft_and_eligibility_policy(self):
        strict = compensation_criterion()
        soft = ProfileCriterionV1(
            criterion_id="preferences.employment_relationships",
            criterion_class="soft_preference",
            dimension="employment_relationship",
            operator="any_of",
            accepted_values=("employee",),
        )
        criteria = MatchCriteriaV1(
            source_status="present",
            eligibility_criteria=(),
            strict_preference_criteria=(strict,),
            soft_preference_criteria=(soft,),
        )

        def result(criterion, outcome):
            return CriterionOutcomeV1(
                criterion_id=criterion.criterion_id,
                criterion_class=criterion.criterion_class,
                dimension=criterion.dimension,
                outcome=outcome,
                reason_code="synthetic_result",
                potentially_relaxable=(
                    criterion.criterion_class == "soft_preference"
                    and outcome == "fail"
                ),
            )

        strict_pass = result(strict, "pass")
        soft_pass = result(soft, "pass")
        for soft_outcome in ("pass", "unknown", "not_applicable"):
            with self.subTest(soft_outcome=soft_outcome):
                admission = evaluate_primary_preference_admission_v1(
                    criteria,
                    (strict_pass, result(soft, soft_outcome)),
                )
                self.assertEqual(admission.status, "keep")

        soft_failure = evaluate_primary_preference_admission_v1(
            criteria,
            (strict_pass, result(soft, "fail")),
        )
        self.assertEqual(soft_failure.status, "exclude")
        self.assertEqual(
            soft_failure.exclusion_criterion_ids,
            ("preferences.employment_relationships",),
        )

        for strict_outcome in ("fail", "unknown", "not_applicable"):
            with self.subTest(strict_outcome=strict_outcome):
                admission = evaluate_primary_preference_admission_v1(
                    criteria,
                    (result(strict, strict_outcome), soft_pass),
                )
                self.assertEqual(admission.status, "exclude")
                self.assertEqual(
                    admission.exclusion_criterion_ids,
                    ("preferences.compensation.minimum",),
                )
        missing_strict = evaluate_primary_preference_admission_v1(
            criteria,
            (soft_pass,),
        )
        self.assertEqual(missing_strict.status, "exclude")
        missing_soft = evaluate_primary_preference_admission_v1(
            criteria,
            (strict_pass,),
        )
        self.assertEqual(missing_soft.status, "keep")

        eligibility_failure = CriterionOutcomeV1(
            criterion_id="eligibility.location",
            criterion_class="eligibility",
            dimension="location_eligibility",
            outcome="fail",
            reason_code="location_eligibility_incompatible",
            potentially_relaxable=False,
        )
        blocked = evaluate_primary_preference_admission_v1(
            criteria,
            (strict_pass, soft_pass, eligibility_failure),
        )
        self.assertEqual(blocked.status, "exclude")
        self.assertEqual(
            blocked.exclusion_criterion_ids,
            ("eligibility.location",),
        )

    def test_multiple_accepted_choices_and_dimensions_evaluate_independently(self):
        model = empty_profile_preferences_v1()
        model["employment_relationships"] = ["employee"]
        model["workloads"] = ["full_time"]
        model["engagement_terms"] = ["fixed_term", "temporary"]
        criteria = match_criteria_v1_from_profile(
            validate_canonical_profile_v2(
                with_preference_model(
                    profile_v2(minimum_kind="none"),
                    model,
                )
            )
        )

        known = lambda *values: OpportunityDimensionV1(
            "known",
            tuple(sorted(values)),
            "synthetic_known",
        )
        unknown = OpportunityDimensionV1(
            "unknown",
            (),
            "synthetic_unknown",
        )
        opportunity = OpportunityCriteriaV1(
            employment_relationships=known("independent_contractor"),
            workloads=known("full_time"),
            engagement_terms=known("temporary"),
            schedule_flexibility_modes=unknown,
            schedule_coordination_modes=unknown,
            schedule_time_windows=unknown,
            schedule_working_days=unknown,
            schedule_time_of_day=unknown,
            phone_voice_modes=unknown,
            job_interests=unknown,
            career_levels=unknown,
            compensation=OpportunityCompensationV1(
                status="unknown",
                disclosed=None,
                currency=None,
                amount_min=None,
                amount_max=None,
                period=None,
                amount_type="unknown",
                reason_code="synthetic_unknown",
            ),
        )
        outcomes = {
            item.criterion_id: item
            for item in evaluate_match_criteria_shadow(
                criteria,
                opportunity,
            ).outcomes
        }

        self.assertEqual(
            outcomes["preferences.employment_relationships"].outcome,
            "fail",
        )
        self.assertEqual(
            outcomes["preferences.workloads"].outcome,
            "pass",
        )
        self.assertEqual(
            outcomes["preferences.engagement_terms"].outcome,
            "pass",
        )

    def test_compensation_comparator_is_conservative_and_non_converting(self):
        strict = compensation_criterion()
        preferred = compensation_criterion(criterion_class="soft_preference")
        cases = (
            (strict, opportunity_compensation(minimum="35", maximum="35"), "pass", "compensation_strict_minimum_guaranteed", False),
            (strict, opportunity_compensation(minimum="20", maximum="40", amount_type="range"), "fail", "compensation_strict_minimum_not_guaranteed", False),
            (strict, opportunity_compensation(minimum="20", maximum="20", amount_type="up_to"), "fail", "compensation_below_strict_minimum", False),
            (preferred, opportunity_compensation(minimum="20", maximum="20"), "fail", "compensation_below_preferred_minimum", True),
            (preferred, opportunity_compensation(minimum="20", maximum="40", amount_type="range"), "unknown", "compensation_preferred_range_overlap", False),
            (strict, opportunity_compensation(minimum="20", maximum="40", amount_type="exact"), "unknown", "compensation_amount_inconsistent", False),
            (strict, opportunity_compensation(currency="BRL"), "unknown", "compensation_currency_mismatch", False),
            (strict, opportunity_compensation(period="month"), "unknown", "compensation_period_mismatch", False),
            (strict, opportunity_compensation(period="project"), "unknown", "compensation_period_unsupported", False),
            (strict, opportunity_compensation(disclosed=False, currency=None, minimum=None, maximum=None, period=None, amount_type="unknown"), "unknown", "compensation_undisclosed", False),
        )
        for criterion, opportunity, outcome, reason, relaxable in cases:
            with self.subTest(reason=reason):
                result = compare_compensation_criterion(criterion, opportunity)
                self.assertEqual(
                    (result.outcome, result.reason_code, result.potentially_relaxable),
                    (outcome, reason, relaxable),
                )

        not_applicable = compare_compensation_criterion(
            None,
            opportunity_compensation(),
        )
        self.assertEqual(
            (not_applicable.outcome, not_applicable.reason_code),
            ("not_applicable", "compensation_minimum_not_set"),
        )

    def test_every_supported_choice_relaxation_is_proven_by_reevaluation(self):
        cases = (
            ("preferences.employment_relationships", "employment_relationship", "employee", "independent_contractor", "add_employment_relationship"),
            ("preferences.workloads", "workload", "full_time", "part_time", "add_workload"),
            ("preferences.engagement_terms", "engagement_term", "permanent", "temporary", "add_engagement_term"),
            ("preferences.schedule.flexibility_modes", "schedule_flexibility", "fixed", "flexible", "allow_schedule_flexibility"),
            ("preferences.schedule.coordination_modes", "schedule_coordination", "asynchronous", "synchronous", "allow_schedule_coordination"),
            ("preferences.schedule.time_windows", "schedule_time_window", "weekdays", "weekends", "allow_schedule_time_window"),
            ("preferences.schedule.working_days", "schedule_working_day", "weekdays", "weekends", "allow_schedule_working_day"),
            ("preferences.schedule.time_of_day", "schedule_time_of_day", "business_hours", "evenings", "allow_schedule_time_of_day"),
            ("preferences.accepted_phone_voice_modes", "phone_voice", "non_phone", "phone", "allow_phone_voice_mode"),
            ("preferences.job_interests", "job_interest", "data_annotation", "customer_support", "broaden_job_interests"),
            ("preferences.accepted_career_levels", "career_level", "entry", "mid", "add_accepted_career_level"),
        )
        for criterion_id, dimension, current, proposed, relaxation_type in cases:
            with self.subTest(dimension=dimension):
                criterion = ProfileCriterionV1(
                    criterion_id=criterion_id,
                    criterion_class="soft_preference",
                    dimension=dimension,
                    operator="any_of",
                    accepted_values=(current,),
                )
                criteria = MatchCriteriaV1(
                    source_status="present",
                    eligibility_criteria=(),
                    strict_preference_criteria=(),
                    soft_preference_criteria=(criterion,),
                )
                candidates = evaluate_single_criterion_relaxations_v1(
                    criteria,
                    opportunity_for_dimension(dimension, proposed),
                    nonblocking_eligibility(),
                    opportunity_reference="canonical:101",
                    original_rank=7,
                )

                self.assertEqual(len(candidates), 1)
                candidate = candidates[0]
                self.assertEqual(candidate.relaxation_type, relaxation_type)
                self.assertEqual(candidate.current_accepted_values, (current,))
                self.assertEqual(candidate.proposed_value, proposed)
                self.assertEqual(
                    (candidate.blocking_reason_code, candidate.counterfactual_reason_code),
                    ("accepted_value_absent", "accepted_value_present"),
                )

    def test_relaxation_requires_one_soft_failure_and_nonblocking_eligibility(self):
        model = empty_profile_preferences_v1()
        model["workloads"] = ["full_time"]
        model["job_interests"] = ["data_annotation"]
        two_failure_criteria = criteria_with_model(model)
        two_failure_opportunity = project_opportunity_criteria_v1(
            effective_enrichment=enrichment(
                engagement_type="part_time",
                role_family="customer_support",
                work_activities=(),
            )
        )
        self.assertEqual(
            evaluate_single_criterion_relaxations_v1(
                two_failure_criteria,
                two_failure_opportunity,
                nonblocking_eligibility(),
                opportunity_reference="canonical:201",
                original_rank=1,
            ),
            (),
        )

        model = empty_profile_preferences_v1()
        model["employment_relationships"] = ["employee"]
        model["workloads"] = ["full_time"]
        one_failure_criteria = criteria_with_model(model)
        one_failure_opportunity = project_opportunity_criteria_v1(
            effective_enrichment=enrichment(engagement_type="part_time")
        )
        self.assertEqual(
            len(
                evaluate_single_criterion_relaxations_v1(
                    one_failure_criteria,
                    one_failure_opportunity,
                    nonblocking_eligibility(),
                    opportunity_reference="canonical:202",
                    original_rank=2,
                )
            ),
            1,
        )
        for eligibility in (
            nonblocking_eligibility(failing_id="eligibility.location"),
            nonblocking_eligibility(unknown_id="eligibility.location"),
            nonblocking_eligibility()[:-1],
        ):
            with self.subTest(eligibility=eligibility):
                self.assertEqual(
                    evaluate_single_criterion_relaxations_v1(
                        one_failure_criteria,
                        one_failure_opportunity,
                        eligibility,
                        opportunity_reference="canonical:202",
                        original_rank=2,
                    ),
                    (),
                )

    def test_strict_failure_and_unknown_soft_evidence_never_create_relaxations(self):
        strict_model = empty_profile_preferences_v1()
        strict_model["workloads"] = ["full_time"]
        strict_model["compensation"] = {
            "minimum_kind": "strict",
            "amount": "30",
            "currency": "USD",
            "period": "hour",
        }
        strict_and_soft_failure = project_opportunity_criteria_v1(
            effective_enrichment=enrichment(
                engagement_type="part_time",
                amount_min=22,
                amount_max=22,
                amount_type="exact",
            )
        )
        self.assertEqual(
            evaluate_single_criterion_relaxations_v1(
                criteria_with_model(strict_model),
                strict_and_soft_failure,
                nonblocking_eligibility(),
                opportunity_reference="canonical:301",
                original_rank=3,
            ),
            (),
        )
        strict_only_model = empty_profile_preferences_v1()
        strict_only_model["compensation"] = strict_model["compensation"]
        self.assertEqual(
            evaluate_single_criterion_relaxations_v1(
                criteria_with_model(strict_only_model),
                strict_and_soft_failure,
                nonblocking_eligibility(),
                opportunity_reference="canonical:303",
                original_rank=3,
            ),
            (),
        )

        relationship = ProfileCriterionV1(
            criterion_id="preferences.employment_relationships",
            criterion_class="soft_preference",
            dimension="employment_relationship",
            operator="any_of",
            accepted_values=("employee",),
        )
        unknown_criteria = MatchCriteriaV1(
            source_status="present",
            eligibility_criteria=(),
            strict_preference_criteria=(),
            soft_preference_criteria=(relationship,),
        )
        self.assertEqual(
            evaluate_single_criterion_relaxations_v1(
                unknown_criteria,
                opportunity_for_dimension(
                    "employment_relationship",
                    "independent_contractor",
                    known=False,
                ),
                nonblocking_eligibility(),
                opportunity_reference="canonical:302",
                original_rank=4,
            ),
            (),
        )

    def test_preferred_compensation_relaxes_only_to_a_guaranteed_comparable_floor(self):
        model = empty_profile_preferences_v1()
        model["compensation"] = {
            "minimum_kind": "preferred",
            "amount": "25",
            "currency": "USD",
            "period": "hour",
        }
        criteria = criteria_with_model(model)
        exact = project_opportunity_criteria_v1(
            effective_enrichment=enrichment(
                amount_min=22,
                amount_max=22,
                amount_type="exact",
            )
        )
        candidate = evaluate_single_criterion_relaxations_v1(
            criteria,
            exact,
            nonblocking_eligibility(),
            opportunity_reference="canonical:401",
            original_rank=5,
        )[0]
        self.assertEqual(candidate.relaxation_type, "lower_preferred_compensation_minimum")
        self.assertEqual(
            (
                candidate.current_minimum_amount,
                candidate.proposed_minimum_amount,
                candidate.currency,
                candidate.period,
            ),
            ("25", "22", "USD", "hour"),
        )

        excluded = (
            enrichment(
                disclosed=False,
                currency=None,
                amount_min=None,
                amount_max=None,
                period="unknown",
                amount_type="unknown",
            ),
            enrichment(amount_min=22, amount_max=22, period="project", amount_type="exact"),
            enrichment(amount_min=22, amount_max=22, currency="BRL", amount_type="exact"),
            enrichment(amount_min=None, amount_max=22, amount_type="up_to"),
        )
        for index, fixture in enumerate(excluded, start=1):
            with self.subTest(case=index):
                opportunity = project_opportunity_criteria_v1(
                    effective_enrichment=fixture
                )
                self.assertEqual(
                    evaluate_single_criterion_relaxations_v1(
                        criteria,
                        opportunity,
                        nonblocking_eligibility(),
                        opportunity_reference=f"canonical:{410 + index}",
                        original_rank=5 + index,
                    ),
                    (),
                )

    def test_equivalent_relaxations_aggregate_in_existing_rank_order(self):
        criterion = ProfileCriterionV1(
            criterion_id="preferences.workloads",
            criterion_class="soft_preference",
            dimension="workload",
            operator="any_of",
            accepted_values=("full_time",),
        )
        criteria = MatchCriteriaV1(
            source_status="present",
            eligibility_criteria=(),
            strict_preference_criteria=(),
            soft_preference_criteria=(criterion,),
        )
        opportunity = opportunity_for_dimension("workload", "part_time")
        later = evaluate_single_criterion_relaxations_v1(
            criteria,
            opportunity,
            nonblocking_eligibility(),
            opportunity_reference="canonical:502",
            original_rank=9,
        )[0]
        earlier = evaluate_single_criterion_relaxations_v1(
            criteria,
            opportunity,
            nonblocking_eligibility(),
            opportunity_reference="canonical:501",
            original_rank=2,
        )[0]
        scenario = aggregate_single_criterion_relaxations_v1(
            (later, earlier, later)
        )[0]
        diagnostic = scenario.as_dict()

        self.assertEqual(diagnostic["unlock_count"], 2)
        self.assertEqual(
            diagnostic["unlocked_opportunities"],
            [
                {"opportunity_reference": "canonical:501", "original_rank": 2},
                {"opportunity_reference": "canonical:502", "original_rank": 9},
            ],
        )
        self.assertEqual(diagnostic["current"], {"accepted_values": ["full_time"]})
        self.assertEqual(diagnostic["proposed"], {"add_value": "part_time"})

    def test_legacy_criteria_never_produce_relaxation_scenarios(self):
        absent = MatchCriteriaV1(
            source_status="absent",
            eligibility_criteria=(),
            strict_preference_criteria=(),
            soft_preference_criteria=(),
        )
        self.assertEqual(
            evaluate_single_criterion_relaxations_v1(
                absent,
                opportunity_for_dimension("workload", "part_time"),
                nonblocking_eligibility(),
                opportunity_reference="canonical:601",
                original_rank=1,
            ),
            (),
        )

    def test_shadow_runner_is_deterministic_non_mutating_and_matcher_inert(self):
        authoritative = profile_v2()
        row = matcher_row()
        rows = [row]
        effective = {7002: enrichment()}
        before_profile = deepcopy(authoritative)
        before_rows = deepcopy(rows)
        before_effective = deepcopy(effective)
        projected = project_v2_to_matcher_v1(
            authoritative,
            matcher_profile_id="typed-shadow-regression",
        )
        matcher_profile = canonical_to_matcher_profile(projected)
        visible_before = matcher.score_opportunity(matcher_profile, row)
        captured = []

        first = run_typed_match_criteria_shadow(
            authoritative,
            rows,
            effective,
            diagnostic_sink=captured.append,
        )
        second = run_typed_match_criteria_shadow(
            authoritative,
            rows,
            effective,
            diagnostic_sink=lambda _record: (_ for _ in ()).throw(RuntimeError()),
        )
        visible_after = matcher.score_opportunity(matcher_profile, row)

        self.assertEqual(first, second)
        self.assertEqual(captured, list(first))
        self.assertEqual(authoritative, before_profile)
        self.assertEqual(rows, before_rows)
        self.assertEqual(effective, before_effective)
        self.assertEqual(visible_after, visible_before)
        self.assertNotIn("30", repr(first))


if __name__ == "__main__":
    unittest.main()

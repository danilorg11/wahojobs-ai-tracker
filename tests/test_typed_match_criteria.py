import unittest
from copy import deepcopy

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
    compare_compensation_criterion,
    evaluate_match_criteria_shadow,
    evaluate_primary_preference_admission_v1,
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
from wahojobs.profiles.preference_model import empty_profile_preferences_v1


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
            "deterministic_parse",
            "high",
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
        "field_sources": {},
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


class TypedMatchCriteriaTests(unittest.TestCase):
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

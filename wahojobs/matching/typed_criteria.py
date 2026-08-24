"""Typed profile/opportunity criteria, evaluation, and admission policy.

This module is pure apart from an optional caller-supplied diagnostic sink. It
does not score, reorder, persist, render, or mutate opportunity collections.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import math

from wahojobs.matching.taxonomy import CAREER_LEVELS
from wahojobs.opportunity_enrichment import (
    COMPENSATION_AMOUNT_TYPES as OPPORTUNITY_COMPENSATION_AMOUNT_TYPES,
    COMPENSATION_PERIODS as OPPORTUNITY_COMPENSATION_PERIODS,
    validate_enrichment_document,
)
from wahojobs.profiles.canonical_v2 import validate_canonical_profile_v2
from wahojobs.profiles.preference_model import (
    ACCEPTED_CAREER_LEVELS,
    COMPENSATION_PERIODS,
    EMPLOYMENT_RELATIONSHIPS,
    ENGAGEMENT_TERMS,
    ISO_4217_CURRENCIES,
    JOB_INTEREST_CODES,
    PHONE_VOICE_MODES,
    SCHEDULE_COORDINATION_MODES,
    SCHEDULE_FLEXIBILITY_MODES,
    SCHEDULE_TIME_WINDOWS,
    WORKLOADS,
    V2_SCHEMA_VERSION as PROFILE_PREFERENCES_V2_SCHEMA_VERSION,
    profile_preferences_v2_to_v1_matcher_compat,
)


MATCH_CRITERIA_SCHEMA_VERSION = "match_criteria_v1"
OPPORTUNITY_CRITERIA_SCHEMA_VERSION = "opportunity_match_criteria_v1"
SHADOW_EVALUATION_SCHEMA_VERSION = "match_criteria_shadow_evaluation_v1"
SHADOW_DIAGNOSTIC_SCHEMA_VERSION = "match_criteria_shadow_diagnostic_v1"
PRIMARY_PREFERENCE_ADMISSION_SCHEMA_VERSION = (
    "typed_preference_primary_admission_v1"
)
SINGLE_CRITERION_RELAXATION_SCHEMA_VERSION = (
    "single_criterion_relaxation_counterfactuals_v1"
)

CRITERION_CLASSES = frozenset(
    {"eligibility", "strict_preference", "soft_preference"}
)
CRITERION_OUTCOMES = frozenset({"pass", "fail", "unknown", "not_applicable"})
DIMENSION_STATUSES = frozenset({"known", "unknown"})
CRITERION_OPERATORS = frozenset({"any_of", "minimum"})

_ELIGIBILITY_CONTEXT_SPEC = {
    "authority": frozenset(
        {
            "matcher_language_eligibility",
            "matcher_location_eligibility",
            "preview_credential_guardrail",
            "preview_professional_domain_gate",
        }
    ),
    "requirement_mode": frozenset(
        {"none", "single", "all_required", "any_supported", "ambiguous", "unknown"}
    ),
    "detected_count": int,
    "matched_count": int,
    "unsupported_count": int,
    "gate_code": frozenset(
        {
            "none",
            "personalized_eligibility_failed",
            "unsupported_title_language_or_dialect",
            "unconfirmed_language_requirement",
            "unconfirmed_language_locale",
            "location_actionability_cap",
            "unconfirmed_location_restriction",
            "incompatible_location",
            "explicit_credential_incompatibility",
            "absent_science_credentials",
            "medical_credential_requirement",
            "professional_domain_hard_gate",
        }
    ),
    "authority_status": frozenset(
        {"eligible", "incompatible", "unknown", "not_applicable"}
    ),
    "profile_location_status": frozenset({"known", "unknown"}),
    "restriction_type": frozenset({"none", "concrete", "opaque"}),
    "job_location_scope": frozenset(
        {
            "remote_worldwide",
            "remote_restricted",
            "onsite_or_hybrid_restricted",
            "unknown",
        }
    ),
    "job_remote_status": frozenset({"remote", "hybrid", "onsite", "unknown"}),
    "actionability_cap_required": bool,
    "actionability_cap_applied": bool,
    "requirement_present": bool,
    "supported_requirement_count": int,
    "hard_gate_applied": bool,
    "role_domain_count": int,
    "matched_domain_count": int,
    "missing_essential_count": int,
}

_LANGUAGE_FAIL_GATES = (
    "personalized_eligibility_failed",
    "unsupported_title_language_or_dialect",
)
_LANGUAGE_UNKNOWN_GATES = (
    "unconfirmed_language_requirement",
    "unconfirmed_language_locale",
)
_LOCATION_GATES = (
    "incompatible_location",
    "location_actionability_cap",
    "unconfirmed_location_restriction",
)
_CREDENTIAL_FAIL_GATES = ("explicit_credential_incompatibility",)
_CREDENTIAL_UNKNOWN_GATES = (
    "absent_science_credentials",
    "medical_credential_requirement",
)
_DECISIVE_PROFESSIONAL_DOMAINS = frozenset({"finance", "legal"})

DIMENSION_ALLOWED_VALUES = {
    "employment_relationship": EMPLOYMENT_RELATIONSHIPS,
    "workload": WORKLOADS,
    "engagement_term": ENGAGEMENT_TERMS,
    "schedule_flexibility": SCHEDULE_FLEXIBILITY_MODES,
    "schedule_coordination": SCHEDULE_COORDINATION_MODES,
    "schedule_time_window": SCHEDULE_TIME_WINDOWS,
    "phone_voice": PHONE_VOICE_MODES,
    "job_interest": JOB_INTEREST_CODES,
    "career_level": ACCEPTED_CAREER_LEVELS,
}

_PROFILE_LIST_CRITERIA = (
    (
        "preferences.employment_relationships",
        "employment_relationship",
        ("employment_relationships",),
    ),
    (
        "preferences.workloads",
        "workload",
        ("workloads",),
    ),
    (
        "preferences.engagement_terms",
        "engagement_term",
        ("engagement_terms",),
    ),
    (
        "preferences.schedule.flexibility_modes",
        "schedule_flexibility",
        ("schedule", "flexibility_modes"),
    ),
    (
        "preferences.schedule.coordination_modes",
        "schedule_coordination",
        ("schedule", "coordination_modes"),
    ),
    (
        "preferences.schedule.time_windows",
        "schedule_time_window",
        ("schedule", "time_windows"),
    ),
    (
        "preferences.accepted_phone_voice_modes",
        "phone_voice",
        ("accepted_phone_voice_modes",),
    ),
    (
        "preferences.job_interests",
        "job_interest",
        ("job_interests",),
    ),
    (
        "preferences.accepted_career_levels",
        "career_level",
        ("accepted_career_levels",),
    ),
)

_OPPORTUNITY_DIMENSION_FIELDS = {
    "employment_relationship": "employment_relationships",
    "workload": "workloads",
    "engagement_term": "engagement_terms",
    "schedule_flexibility": "schedule_flexibility_modes",
    "schedule_coordination": "schedule_coordination_modes",
    "schedule_time_window": "schedule_time_windows",
    "phone_voice": "phone_voice_modes",
    "job_interest": "job_interests",
    "career_level": "career_levels",
}

_CRITERION_IDS_BY_DIMENSION = {
    dimension: criterion_id
    for criterion_id, dimension, _path in _PROFILE_LIST_CRITERIA
}
_CRITERION_IDS_BY_DIMENSION["compensation_minimum"] = (
    "preferences.compensation.minimum"
)

_RELAXATION_TYPES_BY_DIMENSION = {
    "employment_relationship": "add_employment_relationship",
    "workload": "add_workload",
    "engagement_term": "add_engagement_term",
    "schedule_flexibility": "allow_schedule_flexibility",
    "schedule_coordination": "allow_schedule_coordination",
    "schedule_time_window": "allow_schedule_time_window",
    "phone_voice": "allow_phone_voice_mode",
    "job_interest": "broaden_job_interests",
    "career_level": "add_accepted_career_level",
    "compensation_minimum": "lower_preferred_compensation_minimum",
}

_EXISTING_ELIGIBILITY_CRITERION_IDS = frozenset(
    {
        "eligibility.required_languages",
        "eligibility.location",
        "eligibility.credentials_licenses",
        "eligibility.professional_domain",
    }
)


class TypedCriteriaError(ValueError):
    """A bounded typed-criteria failure that never includes profile/job values."""

    def __init__(self, *reason_codes: str):
        self.reason_codes = tuple(sorted(set(reason_codes or ("invalid_criteria",))))[:32]
        super().__init__(
            "typed_match_criteria rejected; reason_codes="
            + ",".join(self.reason_codes)
        )


@dataclass(frozen=True, slots=True, repr=False)
class ProfileCriterionV1:
    criterion_id: str
    criterion_class: str
    dimension: str
    operator: str
    accepted_values: tuple[str, ...] = ()
    minimum_amount: str | None = None
    currency: str | None = None
    period: str | None = None

    def __post_init__(self):
        if (
            type(self.criterion_id) is not str
            or not self.criterion_id
            or self.criterion_class not in CRITERION_CLASSES
            or self.operator not in CRITERION_OPERATORS
        ):
            raise TypedCriteriaError("invalid_profile_criterion")
        if self.operator == "any_of":
            allowed = DIMENSION_ALLOWED_VALUES.get(self.dimension)
            if (
                allowed is None
                or not self.accepted_values
                or any(type(value) is not str for value in self.accepted_values)
                or tuple(sorted(self.accepted_values)) != self.accepted_values
                or len(self.accepted_values) != len(set(self.accepted_values))
                or any(value not in allowed for value in self.accepted_values)
                or any(
                    value is not None
                    for value in (self.minimum_amount, self.currency, self.period)
                )
            ):
                raise TypedCriteriaError("invalid_profile_criterion")
        elif (
            self.dimension != "compensation_minimum"
            or self.accepted_values
            or _positive_decimal(self.minimum_amount) is None
            or self.currency not in ISO_4217_CURRENCIES
            or self.period not in COMPENSATION_PERIODS
        ):
            raise TypedCriteriaError("invalid_profile_criterion")

    def __repr__(self):
        return (
            "ProfileCriterionV1("
            f"criterion_id={self.criterion_id!r}, "
            f"criterion_class={self.criterion_class!r}, "
            f"dimension={self.dimension!r}, values=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class MatchCriteriaV1:
    source_status: str
    eligibility_criteria: tuple[ProfileCriterionV1, ...]
    strict_preference_criteria: tuple[ProfileCriterionV1, ...]
    soft_preference_criteria: tuple[ProfileCriterionV1, ...]
    schema_version: str = MATCH_CRITERIA_SCHEMA_VERSION

    def __post_init__(self):
        if (
            self.schema_version != MATCH_CRITERIA_SCHEMA_VERSION
            or self.source_status not in {"present", "absent"}
        ):
            raise TypedCriteriaError("invalid_match_criteria")
        groups = (
            ("eligibility", self.eligibility_criteria),
            ("strict_preference", self.strict_preference_criteria),
            ("soft_preference", self.soft_preference_criteria),
        )
        ids = []
        for expected_class, criteria in groups:
            if type(criteria) is not tuple:
                raise TypedCriteriaError("invalid_match_criteria")
            for criterion in criteria:
                if (
                    type(criterion) is not ProfileCriterionV1
                    or criterion.criterion_class != expected_class
                ):
                    raise TypedCriteriaError("invalid_match_criteria")
                ids.append(criterion.criterion_id)
            if [item.criterion_id for item in criteria] != sorted(
                item.criterion_id for item in criteria
            ):
                raise TypedCriteriaError("invalid_match_criteria")
        if len(ids) != len(set(ids)):
            raise TypedCriteriaError("invalid_match_criteria")
        if self.source_status == "absent" and ids:
            raise TypedCriteriaError("invalid_match_criteria")

    def all_criteria(self) -> tuple[ProfileCriterionV1, ...]:
        return (
            self.eligibility_criteria
            + self.strict_preference_criteria
            + self.soft_preference_criteria
        )

    def __repr__(self):
        return (
            "MatchCriteriaV1("
            f"source_status={self.source_status!r}, "
            f"criterion_count={len(self.all_criteria())}, values=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class OpportunityDimensionV1:
    status: str
    values: tuple[str, ...]
    reason_code: str

    def __post_init__(self):
        if (
            self.status not in DIMENSION_STATUSES
            or type(self.values) is not tuple
            or any(type(value) is not str for value in self.values)
            or tuple(sorted(self.values)) != self.values
            or len(self.values) != len(set(self.values))
            or type(self.reason_code) is not str
            or not self.reason_code
            or (self.status == "known" and not self.values)
            or (self.status == "unknown" and self.values)
        ):
            raise TypedCriteriaError("invalid_opportunity_dimension")


@dataclass(frozen=True, slots=True, repr=False)
class OpportunityCompensationV1:
    status: str
    disclosed: bool | None
    currency: str | None
    amount_min: str | None
    amount_max: str | None
    period: str | None
    amount_type: str
    reason_code: str

    def __post_init__(self):
        if (
            self.status not in DIMENSION_STATUSES
            or (
                self.disclosed is not None
                and type(self.disclosed) is not bool
            )
            or self.amount_type not in OPPORTUNITY_COMPENSATION_AMOUNT_TYPES
            or type(self.reason_code) is not str
            or not self.reason_code
            or (
                self.currency is not None
                and (
                    type(self.currency) is not str
                    or self.currency != self.currency.upper()
                )
            )
            or (
                self.period is not None
                and self.period not in OPPORTUNITY_COMPENSATION_PERIODS
            )
            or (self.amount_min is not None and _nonnegative_decimal(self.amount_min) is None)
            or (self.amount_max is not None and _nonnegative_decimal(self.amount_max) is None)
        ):
            raise TypedCriteriaError("invalid_opportunity_compensation")
        if self.status == "unknown" and any(
            value is not None
            for value in (
                self.disclosed,
                self.currency,
                self.amount_min,
                self.amount_max,
                self.period,
            )
        ):
            raise TypedCriteriaError("invalid_opportunity_compensation")
        if self.status == "unknown" and self.amount_type != "unknown":
            raise TypedCriteriaError("invalid_opportunity_compensation")
        if self.status == "known" and self.disclosed is None:
            raise TypedCriteriaError("invalid_opportunity_compensation")
        if self.disclosed is False and (
            any(
                value is not None
                for value in (
                    self.currency,
                    self.amount_min,
                    self.amount_max,
                    self.period,
                )
            )
            or self.amount_type != "unknown"
        ):
            raise TypedCriteriaError("invalid_opportunity_compensation")
        minimum = _nonnegative_decimal(self.amount_min)
        maximum = _nonnegative_decimal(self.amount_max)
        if minimum is not None and maximum is not None and minimum > maximum:
            raise TypedCriteriaError("invalid_opportunity_compensation")

    def __repr__(self):
        return (
            "OpportunityCompensationV1("
            f"status={self.status!r}, disclosed={self.disclosed!r}, "
            "values=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class OpportunityCriteriaV1:
    employment_relationships: OpportunityDimensionV1
    workloads: OpportunityDimensionV1
    engagement_terms: OpportunityDimensionV1
    schedule_flexibility_modes: OpportunityDimensionV1
    schedule_coordination_modes: OpportunityDimensionV1
    schedule_time_windows: OpportunityDimensionV1
    phone_voice_modes: OpportunityDimensionV1
    job_interests: OpportunityDimensionV1
    career_levels: OpportunityDimensionV1
    compensation: OpportunityCompensationV1
    schema_version: str = OPPORTUNITY_CRITERIA_SCHEMA_VERSION

    def __post_init__(self):
        if self.schema_version != OPPORTUNITY_CRITERIA_SCHEMA_VERSION:
            raise TypedCriteriaError("invalid_opportunity_criteria")
        for dimension, field in _OPPORTUNITY_DIMENSION_FIELDS.items():
            projected = getattr(self, field)
            if (
                type(projected) is not OpportunityDimensionV1
                or any(
                    value not in DIMENSION_ALLOWED_VALUES[dimension]
                    for value in projected.values
                )
            ):
                raise TypedCriteriaError("invalid_opportunity_criteria")
        if type(self.compensation) is not OpportunityCompensationV1:
            raise TypedCriteriaError("invalid_opportunity_criteria")

    def __repr__(self):
        return "OpportunityCriteriaV1(values=<redacted>)"


@dataclass(frozen=True, slots=True)
class CriterionOutcomeV1:
    criterion_id: str
    criterion_class: str
    dimension: str
    outcome: str
    reason_code: str
    potentially_relaxable: bool
    context: tuple[tuple[str, str | int | bool], ...] = ()

    def __post_init__(self):
        if (
            type(self.criterion_id) is not str
            or not self.criterion_id
            or self.criterion_class not in CRITERION_CLASSES
            or type(self.dimension) is not str
            or not self.dimension
            or self.outcome not in CRITERION_OUTCOMES
            or type(self.reason_code) is not str
            or not self.reason_code
            or type(self.potentially_relaxable) is not bool
            or self.potentially_relaxable
            != (self.criterion_class == "soft_preference" and self.outcome == "fail")
            or type(self.context) is not tuple
            or (self.context and self.criterion_class != "eligibility")
        ):
            raise TypedCriteriaError("invalid_criterion_outcome")
        context_keys = []
        for item in self.context:
            if type(item) is not tuple or len(item) != 2 or type(item[0]) is not str:
                raise TypedCriteriaError("invalid_criterion_outcome")
            key, value = item
            spec = _ELIGIBILITY_CONTEXT_SPEC.get(key)
            if spec is None:
                raise TypedCriteriaError("invalid_criterion_outcome")
            if type(spec) is type and type(value) is not spec:
                raise TypedCriteriaError("invalid_criterion_outcome")
            if type(spec) is frozenset and value not in spec:
                raise TypedCriteriaError("invalid_criterion_outcome")
            if type(value) is int and not 0 <= value <= 64:
                raise TypedCriteriaError("invalid_criterion_outcome")
            context_keys.append(key)
        if context_keys != sorted(context_keys) or len(context_keys) != len(set(context_keys)):
            raise TypedCriteriaError("invalid_criterion_outcome")

    def as_dict(self) -> dict:
        result = {
            "criterion_id": self.criterion_id,
            "criterion_class": self.criterion_class,
            "dimension": self.dimension,
            "outcome": self.outcome,
            "reason_code": self.reason_code,
            "potentially_relaxable": self.potentially_relaxable,
        }
        if self.context:
            result["context"] = dict(self.context)
        return result


@dataclass(frozen=True, slots=True)
class ShadowCriteriaEvaluationV1:
    outcomes: tuple[CriterionOutcomeV1, ...]
    schema_version: str = SHADOW_EVALUATION_SCHEMA_VERSION

    def __post_init__(self):
        if (
            self.schema_version != SHADOW_EVALUATION_SCHEMA_VERSION
            or type(self.outcomes) is not tuple
            or any(type(item) is not CriterionOutcomeV1 for item in self.outcomes)
            or [item.criterion_id for item in self.outcomes]
            != sorted(item.criterion_id for item in self.outcomes)
            or len({item.criterion_id for item in self.outcomes}) != len(self.outcomes)
        ):
            raise TypedCriteriaError("invalid_shadow_evaluation")

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "outcomes": [item.as_dict() for item in self.outcomes],
        }


@dataclass(frozen=True, slots=True)
class PrimaryPreferenceAdmissionV1:
    """A removal-only admission decision over existing typed outcomes."""

    status: str
    exclusion_criterion_ids: tuple[str, ...]
    schema_version: str = PRIMARY_PREFERENCE_ADMISSION_SCHEMA_VERSION

    def __post_init__(self):
        if (
            self.schema_version != PRIMARY_PREFERENCE_ADMISSION_SCHEMA_VERSION
            or self.status not in {"keep", "exclude"}
            or type(self.exclusion_criterion_ids) is not tuple
            or any(
                type(criterion_id) is not str or not criterion_id
                for criterion_id in self.exclusion_criterion_ids
            )
            or tuple(sorted(self.exclusion_criterion_ids))
            != self.exclusion_criterion_ids
            or len(self.exclusion_criterion_ids)
            != len(set(self.exclusion_criterion_ids))
            or (self.status == "keep") != (not self.exclusion_criterion_ids)
        ):
            raise TypedCriteriaError("invalid_primary_preference_admission")

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "exclusion_criterion_ids": list(self.exclusion_criterion_ids),
        }


@dataclass(frozen=True, slots=True, repr=False)
class SingleCriterionRelaxationCandidateV1:
    """One proven one-change unlock for one already-ranked opportunity."""

    criterion_id: str
    dimension: str
    relaxation_type: str
    opportunity_reference: str
    original_rank: int
    blocking_reason_code: str
    counterfactual_reason_code: str
    current_accepted_values: tuple[str, ...] = ()
    proposed_value: str | None = None
    current_minimum_amount: str | None = None
    proposed_minimum_amount: str | None = None
    currency: str | None = None
    period: str | None = None

    def __post_init__(self):
        if (
            type(self.criterion_id) is not str
            or not self.criterion_id
            or len(self.criterion_id) > 128
            or self.dimension not in _RELAXATION_TYPES_BY_DIMENSION
            or self.criterion_id != _CRITERION_IDS_BY_DIMENSION[self.dimension]
            or self.relaxation_type
            != _RELAXATION_TYPES_BY_DIMENSION[self.dimension]
            or not _valid_opportunity_reference(self.opportunity_reference)
            or type(self.original_rank) is not int
            or not 1 <= self.original_rank <= 1_000_000
            or type(self.blocking_reason_code) is not str
            or not self.blocking_reason_code
            or len(self.blocking_reason_code) > 128
            or type(self.counterfactual_reason_code) is not str
            or not self.counterfactual_reason_code
            or len(self.counterfactual_reason_code) > 128
        ):
            raise TypedCriteriaError("invalid_relaxation_candidate")
        if self.dimension == "compensation_minimum":
            current = _positive_decimal(self.current_minimum_amount)
            proposed = _positive_decimal(self.proposed_minimum_amount)
            if (
                self.current_accepted_values
                or self.proposed_value is not None
                or current is None
                or proposed is None
                or proposed >= current
                or self.currency not in ISO_4217_CURRENCIES
                or self.period not in COMPENSATION_PERIODS
                or self.blocking_reason_code
                != "compensation_below_preferred_minimum"
                or self.counterfactual_reason_code
                != "compensation_preferred_minimum_guaranteed"
            ):
                raise TypedCriteriaError("invalid_relaxation_candidate")
        else:
            allowed = DIMENSION_ALLOWED_VALUES[self.dimension]
            if (
                not self.current_accepted_values
                or tuple(sorted(self.current_accepted_values))
                != self.current_accepted_values
                or len(self.current_accepted_values)
                != len(set(self.current_accepted_values))
                or any(value not in allowed for value in self.current_accepted_values)
                or self.proposed_value not in allowed
                or self.proposed_value in self.current_accepted_values
                or any(
                    value is not None
                    for value in (
                        self.current_minimum_amount,
                        self.proposed_minimum_amount,
                        self.currency,
                        self.period,
                    )
                )
                or self.blocking_reason_code != "accepted_value_absent"
                or self.counterfactual_reason_code != "accepted_value_present"
            ):
                raise TypedCriteriaError("invalid_relaxation_candidate")

    def aggregation_key(self) -> tuple:
        return (
            self.criterion_id,
            self.dimension,
            self.relaxation_type,
            self.current_accepted_values,
            self.proposed_value,
            self.current_minimum_amount,
            self.proposed_minimum_amount,
            self.currency,
            self.period,
            self.blocking_reason_code,
            self.counterfactual_reason_code,
        )

    def __repr__(self):
        return (
            "SingleCriterionRelaxationCandidateV1("
            f"criterion_id={self.criterion_id!r}, "
            f"dimension={self.dimension!r}, "
            f"opportunity_reference={self.opportunity_reference!r}, "
            f"original_rank={self.original_rank}, change=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class SingleCriterionRelaxationScenarioV1:
    """Equivalent one-change unlocks aggregated in matcher rank order."""

    scenario_id: str
    criterion_id: str
    dimension: str
    relaxation_type: str
    blocking_reason_code: str
    counterfactual_reason_code: str
    unlocked_opportunities: tuple[tuple[str, int], ...]
    current_accepted_values: tuple[str, ...] = ()
    proposed_value: str | None = None
    current_minimum_amount: str | None = None
    proposed_minimum_amount: str | None = None
    currency: str | None = None
    period: str | None = None
    schema_version: str = SINGLE_CRITERION_RELAXATION_SCHEMA_VERSION

    def __post_init__(self):
        if (
            self.schema_version != SINGLE_CRITERION_RELAXATION_SCHEMA_VERSION
            or type(self.scenario_id) is not str
            or len(self.scenario_id) > 256
            or self.scenario_id
            != _relaxation_scenario_id(
                self.criterion_id,
                self.proposed_value,
                self.proposed_minimum_amount,
                self.currency,
                self.period,
            )
            or type(self.unlocked_opportunities) is not tuple
            or not self.unlocked_opportunities
            or len(self.unlocked_opportunities) > 10_000
        ):
            raise TypedCriteriaError("invalid_relaxation_scenario")
        references = []
        ranks = []
        for item in self.unlocked_opportunities:
            if (
                type(item) is not tuple
                or len(item) != 2
                or not _valid_opportunity_reference(item[0])
                or type(item[1]) is not int
                or not 1 <= item[1] <= 1_000_000
            ):
                raise TypedCriteriaError("invalid_relaxation_scenario")
            references.append(item[0])
            ranks.append(item[1])
        if (
            len(references) != len(set(references))
            or len(ranks) != len(set(ranks))
            or ranks != sorted(ranks)
        ):
            raise TypedCriteriaError("invalid_relaxation_scenario")
        # Reuse the candidate's closed change-shape validation.
        SingleCriterionRelaxationCandidateV1(
            criterion_id=self.criterion_id,
            dimension=self.dimension,
            relaxation_type=self.relaxation_type,
            opportunity_reference=references[0],
            original_rank=ranks[0],
            blocking_reason_code=self.blocking_reason_code,
            counterfactual_reason_code=self.counterfactual_reason_code,
            current_accepted_values=self.current_accepted_values,
            proposed_value=self.proposed_value,
            current_minimum_amount=self.current_minimum_amount,
            proposed_minimum_amount=self.proposed_minimum_amount,
            currency=self.currency,
            period=self.period,
        )

    def as_dict(self) -> dict:
        if self.dimension == "compensation_minimum":
            current = {
                "minimum_amount": self.current_minimum_amount,
                "currency": self.currency,
                "period": self.period,
            }
            proposed = {
                "minimum_amount": self.proposed_minimum_amount,
                "currency": self.currency,
                "period": self.period,
            }
        else:
            current = {"accepted_values": list(self.current_accepted_values)}
            proposed = {"add_value": self.proposed_value}
        return {
            "schema_version": self.schema_version,
            "scenario_id": self.scenario_id,
            "criterion_id": self.criterion_id,
            "dimension": self.dimension,
            "relaxation_type": self.relaxation_type,
            "current": current,
            "proposed": proposed,
            "blocking_reason_code": self.blocking_reason_code,
            "counterfactual_reason_code": self.counterfactual_reason_code,
            "unlock_count": len(self.unlocked_opportunities),
            "unlocked_opportunities": [
                {
                    "opportunity_reference": reference,
                    "original_rank": rank,
                }
                for reference, rank in self.unlocked_opportunities
            ],
        }

    def __repr__(self):
        return (
            "SingleCriterionRelaxationScenarioV1("
            f"scenario_id={self.scenario_id!r}, "
            f"unlock_count={len(self.unlocked_opportunities)}, "
            "change=<redacted>)"
        )


def match_criteria_v1_from_profile(profile_v2: dict) -> MatchCriteriaV1:
    """Build typed criteria from optional authoritative profile preferences."""
    profile = validate_canonical_profile_v2(profile_v2)
    model = profile["preferences"].get("preference_model")
    if model is None:
        return MatchCriteriaV1(
            source_status="absent",
            eligibility_criteria=(),
            strict_preference_criteria=(),
            soft_preference_criteria=(),
        )

    # Slice 6A keeps the established V1 matcher contract. Split V2 schedule
    # fields map exactly. Multiple compensation expectations are intentionally
    # non-enforcing until Slice 6B can evaluate each item independently.
    if model.get("schema_version") == PROFILE_PREFERENCES_V2_SCHEMA_VERSION:
        model = profile_preferences_v2_to_v1_matcher_compat(model)

    strict: list[ProfileCriterionV1] = []
    soft: list[ProfileCriterionV1] = []
    for criterion_id, dimension, path in _PROFILE_LIST_CRITERIA:
        values = _nested(model, path)
        allowed = DIMENSION_ALLOWED_VALUES[dimension]
        if not values or set(values) == set(allowed):
            continue
        criterion = ProfileCriterionV1(
            criterion_id=criterion_id,
            criterion_class="soft_preference",
            dimension=dimension,
            operator="any_of",
            accepted_values=tuple(values),
        )
        soft.append(criterion)

    compensation = model["compensation"]
    if compensation["minimum_kind"] in {"preferred", "strict"}:
        criterion_class = (
            "strict_preference"
            if compensation["minimum_kind"] == "strict"
            else "soft_preference"
        )
        criterion = ProfileCriterionV1(
            criterion_id="preferences.compensation.minimum",
            criterion_class=criterion_class,
            dimension="compensation_minimum",
            operator="minimum",
            minimum_amount=compensation["amount"],
            currency=compensation["currency"],
            period=compensation["period"],
        )
        (strict if criterion_class == "strict_preference" else soft).append(
            criterion
        )

    return MatchCriteriaV1(
        source_status="present",
        eligibility_criteria=(),
        strict_preference_criteria=tuple(sorted(strict, key=lambda item: item.criterion_id)),
        soft_preference_criteria=tuple(sorted(soft, key=lambda item: item.criterion_id)),
    )


def project_opportunity_criteria_v1(
    *,
    effective_enrichment: dict | None = None,
    inventory_row=None,
) -> OpportunityCriteriaV1:
    """Project only reliable structured opportunity facts; never parse free text."""
    if effective_enrichment is None:
        return _project_inventory_row(inventory_row)
    if type(effective_enrichment) is not dict:
        raise TypedCriteriaError("invalid_opportunity_enrichment")
    document = effective_enrichment.get("document")
    if type(document) is not dict:
        raise TypedCriteriaError("invalid_opportunity_enrichment")
    try:
        validate_enrichment_document(deepcopy(document))
    except Exception as exc:
        raise TypedCriteriaError("invalid_opportunity_enrichment") from exc

    field_sources = effective_enrichment.get("field_sources") or {}
    stale_fields = set(effective_enrichment.get("stale_override_fields") or [])
    if type(field_sources) is not dict or any(
        value not in {"automatic", "human_override"}
        for value in field_sources.values()
    ):
        raise TypedCriteriaError("invalid_opportunity_enrichment")
    context = _EvidenceContext(document, field_sources, stale_fields)
    attributes = document["attributes"]
    role = attributes["role"]
    arrangement = attributes["work_arrangement"]

    engagement = (
        arrangement["engagement_type"]
        if context.reliable("attributes.work_arrangement.engagement_type")
        else "unknown"
    )
    relationships = _unknown_dimension("employment_relationship_unknown")
    workloads = _unknown_dimension("workload_unknown")
    terms = _unknown_dimension("engagement_term_unknown")
    if engagement == "freelance":
        relationships = _known_dimension(
            ("independent_contractor",), "engagement_type_freelance"
        )
    elif engagement == "full_time":
        workloads = _known_dimension(("full_time",), "engagement_type_full_time")
    elif engagement == "part_time":
        workloads = _known_dimension(("part_time",), "engagement_type_part_time")
    elif engagement in {"temporary", "internship"}:
        terms = _known_dimension((engagement,), f"engagement_type_{engagement}")
    elif engagement == "contract":
        reason = "engagement_type_contract_ambiguous"
        relationships = _unknown_dimension(reason)
        terms = _unknown_dimension(reason)
    elif engagement == "volunteer":
        reason = "engagement_type_unsupported"
        relationships = _unknown_dimension(reason)
        workloads = _unknown_dimension(reason)
        terms = _unknown_dimension(reason)

    schedule_type = (
        arrangement["schedule_type"]
        if context.reliable("attributes.work_arrangement.schedule_type")
        else "unknown"
    )
    schedule_flexibility = (
        _known_dimension((schedule_type,), f"schedule_type_{schedule_type}")
        if schedule_type in SCHEDULE_FLEXIBILITY_MODES
        else _unknown_dimension("schedule_flexibility_unknown")
    )

    interests = set()
    role_family_path = "attributes.role.role_family"
    if context.reliable(role_family_path) and role["role_family"] in JOB_INTEREST_CODES:
        interests.add(role["role_family"])
    activities_path = "attributes.role.work_activities"
    if context.reliable(activities_path):
        interests.update(set(role["work_activities"]) & set(JOB_INTEREST_CODES))
    job_interests = (
        _known_dimension(tuple(sorted(interests)), "structured_job_taxonomy")
        if interests
        else _unknown_dimension("job_interest_unknown")
    )

    seniority_path = "attributes.role.seniority"
    career_levels = (
        _known_dimension((role["seniority"],), "structured_career_level")
        if context.reliable(seniority_path) and role["seniority"] in CAREER_LEVELS
        else _unknown_dimension("career_level_unknown")
    )

    return OpportunityCriteriaV1(
        employment_relationships=relationships,
        workloads=workloads,
        engagement_terms=terms,
        schedule_flexibility_modes=schedule_flexibility,
        schedule_coordination_modes=_unknown_dimension(
            "schedule_coordination_not_structured"
        ),
        schedule_time_windows=_unknown_dimension(
            "schedule_time_window_not_structured"
        ),
        phone_voice_modes=_unknown_dimension("phone_voice_not_structured"),
        job_interests=job_interests,
        career_levels=career_levels,
        compensation=_project_compensation(attributes["compensation"], context),
    )


def evaluate_match_criteria_shadow(
    criteria: MatchCriteriaV1,
    opportunity: OpportunityCriteriaV1,
) -> ShadowCriteriaEvaluationV1:
    """Evaluate each active criterion without feeding results to matching."""
    if type(criteria) is not MatchCriteriaV1 or type(opportunity) is not OpportunityCriteriaV1:
        raise TypedCriteriaError("invalid_shadow_evaluation_input")
    outcomes = []
    for criterion in criteria.all_criteria():
        if criterion.operator == "minimum":
            result = compare_compensation_criterion(
                criterion,
                opportunity.compensation,
            )
        else:
            dimension = getattr(
                opportunity,
                _OPPORTUNITY_DIMENSION_FIELDS[criterion.dimension],
            )
            if dimension.status == "unknown":
                result = _outcome(
                    criterion,
                    "unknown",
                    dimension.reason_code,
                )
            elif set(criterion.accepted_values).intersection(dimension.values):
                result = _outcome(criterion, "pass", "accepted_value_present")
            else:
                result = _outcome(criterion, "fail", "accepted_value_absent")
        outcomes.append(result)
    return ShadowCriteriaEvaluationV1(
        outcomes=tuple(sorted(outcomes, key=lambda item: item.criterion_id))
    )


def evaluate_primary_preference_admission_v1(
    criteria: MatchCriteriaV1,
    outcomes: tuple[CriterionOutcomeV1, ...],
) -> PrimaryPreferenceAdmissionV1:
    """Apply V1 admission policy without scoring, ranking, or adding matches.

    A missing strict result is treated like unknown and therefore excludes.
    A missing soft result is treated like missing opportunity evidence and
    therefore keeps the match. Existing eligibility failures are translated
    by the bridge and can never be relaxed here.
    """
    if (
        type(criteria) is not MatchCriteriaV1
        or type(outcomes) is not tuple
        or any(type(item) is not CriterionOutcomeV1 for item in outcomes)
        or len({item.criterion_id for item in outcomes}) != len(outcomes)
    ):
        raise TypedCriteriaError("invalid_primary_preference_admission_input")
    if criteria.source_status == "absent":
        return PrimaryPreferenceAdmissionV1("keep", ())

    outcome_by_id = {item.criterion_id: item for item in outcomes}
    exclusions = {
        item.criterion_id
        for item in outcomes
        if item.criterion_class == "eligibility" and item.outcome == "fail"
    }
    for criterion in criteria.strict_preference_criteria:
        result = outcome_by_id.get(criterion.criterion_id)
        if (
            result is None
            or result.criterion_class != "strict_preference"
            or result.outcome != "pass"
        ):
            exclusions.add(criterion.criterion_id)
    for criterion in criteria.soft_preference_criteria:
        result = outcome_by_id.get(criterion.criterion_id)
        if (
            result is not None
            and result.criterion_class == "soft_preference"
            and result.outcome == "fail"
        ):
            exclusions.add(criterion.criterion_id)

    exclusion_ids = tuple(sorted(exclusions))
    return PrimaryPreferenceAdmissionV1(
        "exclude" if exclusion_ids else "keep",
        exclusion_ids,
    )


def evaluate_single_criterion_relaxations_v1(
    criteria: MatchCriteriaV1,
    opportunity: OpportunityCriteriaV1,
    eligibility_outcomes: tuple[CriterionOutcomeV1, ...],
    *,
    opportunity_reference: str,
    original_rank: int,
) -> tuple[SingleCriterionRelaxationCandidateV1, ...]:
    """Prove every one-soft-criterion change that admits one opportunity.

    Unknown eligibility, a non-passing strict criterion, multiple soft failures,
    or unknown evidence returns no proposal. Each returned proposal is rebuilt as
    typed criteria and evaluated through the normal admission policy.
    """
    if (
        type(criteria) is not MatchCriteriaV1
        or type(opportunity) is not OpportunityCriteriaV1
        or type(eligibility_outcomes) is not tuple
        or any(type(item) is not CriterionOutcomeV1 for item in eligibility_outcomes)
        or any(item.criterion_class != "eligibility" for item in eligibility_outcomes)
        or len({item.criterion_id for item in eligibility_outcomes})
        != len(eligibility_outcomes)
        or not _valid_opportunity_reference(opportunity_reference)
        or type(original_rank) is not int
        or not 1 <= original_rank <= 1_000_000
    ):
        raise TypedCriteriaError("invalid_relaxation_evaluation_input")
    if criteria.source_status != "present":
        return ()

    eligibility_by_id = {
        item.criterion_id: item for item in eligibility_outcomes
    }
    if (
        not _EXISTING_ELIGIBILITY_CRITERION_IDS.issubset(eligibility_by_id)
        or any(
            item.outcome not in {"pass", "not_applicable"}
            for item in eligibility_outcomes
        )
    ):
        return ()

    current_evaluation = evaluate_match_criteria_shadow(criteria, opportunity)
    preference_by_id = {
        item.criterion_id: item for item in current_evaluation.outcomes
    }
    if any(
        preference_by_id.get(criterion.criterion_id) is None
        or preference_by_id[criterion.criterion_id].outcome != "pass"
        for criterion in criteria.strict_preference_criteria
    ):
        return ()

    soft_failures = tuple(
        preference_by_id[criterion.criterion_id]
        for criterion in criteria.soft_preference_criteria
        if preference_by_id[criterion.criterion_id].outcome == "fail"
    )
    if len(soft_failures) != 1:
        return ()
    failure = soft_failures[0]
    if not failure.potentially_relaxable:
        return ()

    current_outcomes = tuple(
        sorted(
            current_evaluation.outcomes + eligibility_outcomes,
            key=lambda item: item.criterion_id,
        )
    )
    current_admission = evaluate_primary_preference_admission_v1(
        criteria,
        current_outcomes,
    )
    if current_admission.exclusion_criterion_ids != (failure.criterion_id,):
        return ()

    failing_criterion = next(
        criterion
        for criterion in criteria.soft_preference_criteria
        if criterion.criterion_id == failure.criterion_id
    )
    proposed_criteria = _single_criterion_proposals(
        failing_criterion,
        opportunity,
        failure,
    )
    candidates = []
    for proposed in proposed_criteria:
        counterfactual = _replace_soft_criterion(criteria, proposed)
        counterfactual_evaluation = evaluate_match_criteria_shadow(
            counterfactual,
            opportunity,
        )
        counterfactual_by_id = {
            item.criterion_id: item
            for item in counterfactual_evaluation.outcomes
        }
        changed_result = counterfactual_by_id.get(failure.criterion_id)
        if changed_result is None or changed_result.outcome != "pass":
            continue
        if any(
            before.criterion_id != failure.criterion_id
            and counterfactual_by_id.get(before.criterion_id) != before
            for before in current_evaluation.outcomes
        ):
            continue
        counterfactual_outcomes = tuple(
            sorted(
                counterfactual_evaluation.outcomes + eligibility_outcomes,
                key=lambda item: item.criterion_id,
            )
        )
        if evaluate_primary_preference_admission_v1(
            counterfactual,
            counterfactual_outcomes,
        ).status != "keep":
            continue
        candidates.append(
            _relaxation_candidate(
                failing_criterion,
                proposed,
                failure,
                changed_result,
                opportunity_reference,
                original_rank,
            )
        )
    return tuple(
        sorted(
            candidates,
            key=lambda item: (
                item.criterion_id,
                item.proposed_value or "",
                item.proposed_minimum_amount or "",
            ),
        )
    )


def aggregate_single_criterion_relaxations_v1(
    candidates: tuple[SingleCriterionRelaxationCandidateV1, ...],
) -> tuple[SingleCriterionRelaxationScenarioV1, ...]:
    """Aggregate equivalent proven changes while retaining matcher rank."""
    if type(candidates) is not tuple or any(
        type(item) is not SingleCriterionRelaxationCandidateV1
        for item in candidates
    ):
        raise TypedCriteriaError("invalid_relaxation_aggregation_input")
    grouped = {}
    for candidate in candidates:
        opportunities = grouped.setdefault(candidate.aggregation_key(), {})
        previous = opportunities.get(candidate.opportunity_reference)
        if previous is None or candidate.original_rank < previous.original_rank:
            opportunities[candidate.opportunity_reference] = candidate

    scenarios = []
    for opportunities in grouped.values():
        ranked = tuple(
            sorted(
                opportunities.values(),
                key=lambda item: (item.original_rank, item.opportunity_reference),
            )
        )
        first = ranked[0]
        scenarios.append(
            SingleCriterionRelaxationScenarioV1(
                scenario_id=_relaxation_scenario_id(
                    first.criterion_id,
                    first.proposed_value,
                    first.proposed_minimum_amount,
                    first.currency,
                    first.period,
                ),
                criterion_id=first.criterion_id,
                dimension=first.dimension,
                relaxation_type=first.relaxation_type,
                blocking_reason_code=first.blocking_reason_code,
                counterfactual_reason_code=first.counterfactual_reason_code,
                unlocked_opportunities=tuple(
                    (item.opportunity_reference, item.original_rank)
                    for item in ranked
                ),
                current_accepted_values=first.current_accepted_values,
                proposed_value=first.proposed_value,
                current_minimum_amount=first.current_minimum_amount,
                proposed_minimum_amount=first.proposed_minimum_amount,
                currency=first.currency,
                period=first.period,
            )
        )
    return tuple(
        sorted(
            scenarios,
            key=lambda item: (
                item.unlocked_opportunities[0][1],
                item.scenario_id,
            ),
        )
    )


def _single_criterion_proposals(criterion, opportunity, failure):
    if criterion.operator == "any_of":
        dimension = getattr(
            opportunity,
            _OPPORTUNITY_DIMENSION_FIELDS[criterion.dimension],
        )
        if (
            dimension.status != "known"
            or failure.reason_code != "accepted_value_absent"
        ):
            return ()
        return tuple(
            ProfileCriterionV1(
                criterion_id=criterion.criterion_id,
                criterion_class="soft_preference",
                dimension=criterion.dimension,
                operator="any_of",
                accepted_values=tuple(
                    sorted(set(criterion.accepted_values) | {value})
                ),
            )
            for value in dimension.values
            if value not in criterion.accepted_values
        )

    compensation = opportunity.compensation
    if (
        criterion.dimension != "compensation_minimum"
        or criterion.criterion_class != "soft_preference"
        or failure.reason_code != "compensation_below_preferred_minimum"
        or compensation.status != "known"
        or compensation.disclosed is not True
        or compensation.currency != criterion.currency
        or compensation.period != criterion.period
        or compensation.period not in COMPENSATION_PERIODS
        or compensation.amount_type not in {"exact", "range", "from"}
    ):
        return ()
    if compensation.amount_type == "exact":
        minimum = _nonnegative_decimal(compensation.amount_min)
        maximum = _nonnegative_decimal(compensation.amount_max)
        if minimum is not None and maximum is not None and minimum != maximum:
            return ()
        exact = compensation.amount_min if minimum is not None else compensation.amount_max
        lower_bound = _canonical_positive_decimal(exact)
    else:
        lower_bound = _canonical_positive_decimal(compensation.amount_min)
    current = _positive_decimal(criterion.minimum_amount)
    proposed = _positive_decimal(lower_bound)
    if current is None or proposed is None or proposed >= current:
        return ()
    if compensation.amount_type == "range":
        maximum = _nonnegative_decimal(compensation.amount_max)
        if maximum is None or proposed > maximum:
            return ()
    return (
        ProfileCriterionV1(
            criterion_id=criterion.criterion_id,
            criterion_class="soft_preference",
            dimension="compensation_minimum",
            operator="minimum",
            minimum_amount=lower_bound,
            currency=criterion.currency,
            period=criterion.period,
        ),
    )


def _replace_soft_criterion(criteria, proposed):
    return MatchCriteriaV1(
        source_status="present",
        eligibility_criteria=criteria.eligibility_criteria,
        strict_preference_criteria=criteria.strict_preference_criteria,
        soft_preference_criteria=tuple(
            proposed if item.criterion_id == proposed.criterion_id else item
            for item in criteria.soft_preference_criteria
        ),
    )


def _relaxation_candidate(
    current,
    proposed,
    failure,
    counterfactual_result,
    opportunity_reference,
    original_rank,
):
    common = {
        "criterion_id": current.criterion_id,
        "dimension": current.dimension,
        "relaxation_type": _RELAXATION_TYPES_BY_DIMENSION[current.dimension],
        "opportunity_reference": opportunity_reference,
        "original_rank": original_rank,
        "blocking_reason_code": failure.reason_code,
        "counterfactual_reason_code": counterfactual_result.reason_code,
    }
    if current.operator == "any_of":
        added = tuple(
            sorted(set(proposed.accepted_values) - set(current.accepted_values))
        )
        if len(added) != 1:
            raise TypedCriteriaError("invalid_relaxation_proposal")
        return SingleCriterionRelaxationCandidateV1(
            **common,
            current_accepted_values=current.accepted_values,
            proposed_value=added[0],
        )
    return SingleCriterionRelaxationCandidateV1(
        **common,
        current_minimum_amount=current.minimum_amount,
        proposed_minimum_amount=proposed.minimum_amount,
        currency=current.currency,
        period=current.period,
    )


def bridge_existing_matcher_eligibility(
    authoritative_match: dict,
) -> tuple[CriterionOutcomeV1, ...]:
    """Translate existing matcher/guardrail decisions without re-evaluating them."""
    if type(authoritative_match) is not dict:
        raise TypedCriteriaError("invalid_eligibility_authority")
    outcomes = (
        _bridge_language_eligibility(authoritative_match),
        _bridge_location_eligibility(authoritative_match),
        _bridge_credential_eligibility(authoritative_match),
        _bridge_professional_domain_eligibility(authoritative_match),
    )
    return tuple(sorted(outcomes, key=lambda item: item.criterion_id))


def _bridge_language_eligibility(match):
    cap_reasons = _recognized_cap_reasons(match)
    mode = _closed_value(
        match.get("language_requirement_mode"),
        _ELIGIBILITY_CONTEXT_SPEC["requirement_mode"],
        "unknown",
    )
    fail_gate = _first_gate(cap_reasons, _LANGUAGE_FAIL_GATES)
    unknown_gate = _first_gate(cap_reasons, _LANGUAGE_UNKNOWN_GATES)
    eligible = match.get("eligible_for_personalized")
    if fail_gate or eligible is False:
        outcome = "fail"
        reason = "required_language_incompatible"
    elif unknown_gate or type(eligible) is not bool or mode == "unknown":
        outcome = "unknown"
        reason = "required_language_unconfirmed"
    elif mode == "none":
        outcome = "not_applicable"
        reason = "required_language_not_applicable"
    else:
        outcome = "pass"
        reason = "required_language_satisfied"
    return _eligibility_outcome(
        "eligibility.required_languages",
        "required_language_eligibility",
        outcome,
        reason,
        {
            "authority": "matcher_language_eligibility",
            "requirement_mode": mode,
            "detected_count": _bounded_count(match.get("detected_languages")),
            "matched_count": _bounded_count(match.get("matched_languages")),
            "unsupported_count": _bounded_count(match.get("unsupported_languages")),
            "gate_code": fail_gate or unknown_gate or "none",
        },
    )


def _bridge_location_eligibility(match):
    cap_reasons = _recognized_cap_reasons(match)
    status = _closed_value(
        match.get("location_eligibility_status"),
        _ELIGIBILITY_CONTEXT_SPEC["authority_status"],
        "unknown",
    )
    gate = _first_gate(cap_reasons, _LOCATION_GATES)
    if gate == "incompatible_location" or status == "incompatible":
        outcome, reason = "fail", "location_eligibility_incompatible"
    elif gate:
        outcome, reason = "unknown", "location_eligibility_unconfirmed"
    else:
        outcome, reason = {
            "eligible": ("pass", "location_eligibility_satisfied"),
            "unknown": ("unknown", "location_eligibility_unconfirmed"),
            "not_applicable": (
                "not_applicable",
                "location_eligibility_not_applicable",
            ),
        }[status]
    return _eligibility_outcome(
        "eligibility.location",
        "location_eligibility",
        outcome,
        reason,
        {
            "authority": "matcher_location_eligibility",
            "authority_status": status,
            "profile_location_status": _closed_value(
                match.get("profile_location_status"),
                _ELIGIBILITY_CONTEXT_SPEC["profile_location_status"],
                "unknown",
            ),
            "restriction_type": _closed_value(
                match.get("location_restriction_type"),
                _ELIGIBILITY_CONTEXT_SPEC["restriction_type"],
                "none",
            ),
            "job_location_scope": _closed_value(
                match.get("job_location_scope"),
                _ELIGIBILITY_CONTEXT_SPEC["job_location_scope"],
                "unknown",
            ),
            "job_remote_status": _closed_value(
                match.get("job_remote_status"),
                _ELIGIBILITY_CONTEXT_SPEC["job_remote_status"],
                "unknown",
            ),
            "actionability_cap_required": (
                match.get("location_actionability_cap_required") is True
            ),
            "actionability_cap_applied": (
                match.get("location_actionability_cap_applied") is True
            ),
            "gate_code": gate or "none",
        },
    )


def _bridge_credential_eligibility(match):
    cap_reasons = _recognized_cap_reasons(match)
    fail_gate = _first_gate(cap_reasons, _CREDENTIAL_FAIL_GATES)
    unknown_gate = _first_gate(cap_reasons, _CREDENTIAL_UNKNOWN_GATES)
    affirmative = match.get("affirmative_fit")
    evidence = (
        affirmative.get("supported_evidence")
        if type(affirmative) is dict
        else None
    )
    supported_count = sum(
        1
        for item in (evidence if type(evidence) in {list, tuple} else ())
        if type(item) is dict and item.get("source") == "credential"
    )
    required_groups = (
        affirmative.get("required_groups")
        if type(affirmative) is dict
        else None
    )
    affirmative_requirement = any(
        type(item) is dict and item.get("source") == "title_credential"
        for item in (
            required_groups if type(required_groups) in {list, tuple} else ()
        )
    )
    requirement_present = bool(match.get("preview_credential_requirement")) or bool(
        fail_gate or unknown_gate or affirmative_requirement or supported_count
    )
    if fail_gate:
        outcome = "fail"
        reason = "credential_requirement_incompatible"
    elif supported_count:
        outcome = "pass"
        reason = "credential_requirement_satisfied"
    elif requirement_present:
        outcome = "unknown"
        reason = "credential_requirement_unconfirmed"
    else:
        outcome = "not_applicable"
        reason = "credential_requirement_not_applicable"
    return _eligibility_outcome(
        "eligibility.credentials_licenses",
        "credential_eligibility",
        outcome,
        reason,
        {
            "authority": "preview_credential_guardrail",
            "requirement_present": requirement_present,
            "supported_requirement_count": min(supported_count, 64),
            "gate_code": fail_gate or unknown_gate or "none",
        },
    )


def _bridge_professional_domain_eligibility(match):
    role_domains = _closed_domain_set(match.get("core_role_domains"))
    matched_domains = _closed_domain_set(match.get("matched_core_domains"))
    missing_domains = _closed_domain_set(match.get("missing_essential_domains"))
    decisive_role = role_domains & _DECISIVE_PROFESSIONAL_DOMAINS
    decisive_matched = matched_domains & _DECISIVE_PROFESSIONAL_DOMAINS
    decisive_missing = missing_domains & _DECISIVE_PROFESSIONAL_DOMAINS
    hard_gate = match.get("professional_domain_hard_gate_applied") is True
    if hard_gate:
        outcome = "fail"
        reason = "professional_domain_incompatible"
    elif decisive_role and not decisive_missing:
        outcome = "pass"
        reason = "professional_domain_satisfied"
    elif decisive_role:
        outcome = "unknown"
        reason = "professional_domain_authority_incomplete"
    else:
        outcome = "not_applicable"
        reason = "professional_domain_not_applicable"
    return _eligibility_outcome(
        "eligibility.professional_domain",
        "professional_domain_eligibility",
        outcome,
        reason,
        {
            "authority": "preview_professional_domain_gate",
            "hard_gate_applied": hard_gate,
            "role_domain_count": len(decisive_role),
            "matched_domain_count": len(decisive_matched),
            "missing_essential_count": len(decisive_missing),
            "gate_code": "professional_domain_hard_gate" if hard_gate else "none",
        },
    )


def compare_compensation_criterion(
    criterion: ProfileCriterionV1 | None,
    opportunity: OpportunityCompensationV1,
) -> CriterionOutcomeV1:
    """Compare only a guaranteed like-currency, like-period minimum."""
    if criterion is None:
        return CriterionOutcomeV1(
            criterion_id="preferences.compensation.minimum",
            criterion_class="soft_preference",
            dimension="compensation_minimum",
            outcome="not_applicable",
            reason_code="compensation_minimum_not_set",
            potentially_relaxable=False,
        )
    if (
        type(criterion) is not ProfileCriterionV1
        or criterion.operator != "minimum"
        or type(opportunity) is not OpportunityCompensationV1
    ):
        raise TypedCriteriaError("invalid_compensation_comparison")
    if opportunity.status == "unknown":
        return _outcome(criterion, "unknown", opportunity.reason_code)
    if opportunity.disclosed is False:
        return _outcome(criterion, "unknown", "compensation_undisclosed")
    if opportunity.disclosed is not True:
        return _outcome(criterion, "unknown", "compensation_disclosure_unknown")
    if opportunity.period not in COMPENSATION_PERIODS:
        return _outcome(criterion, "unknown", "compensation_period_unsupported")
    if opportunity.currency not in ISO_4217_CURRENCIES:
        return _outcome(criterion, "unknown", "compensation_currency_unknown")
    if opportunity.currency != criterion.currency:
        return _outcome(criterion, "unknown", "compensation_currency_mismatch")
    if opportunity.period != criterion.period:
        return _outcome(criterion, "unknown", "compensation_period_mismatch")

    threshold = _positive_decimal(criterion.minimum_amount)
    if opportunity.amount_type not in {"exact", "range", "from", "up_to"}:
        return _outcome(criterion, "unknown", "compensation_amount_type_unknown")
    if threshold is None:
        return _outcome(criterion, "unknown", "compensation_amount_incomplete")

    minimum = _nonnegative_decimal(opportunity.amount_min)
    maximum = _nonnegative_decimal(opportunity.amount_max)
    lower_bound = None
    upper_bound = None
    if opportunity.amount_type == "exact":
        if minimum is not None and maximum is not None and minimum != maximum:
            return _outcome(
                criterion,
                "unknown",
                "compensation_amount_inconsistent",
            )
        exact = minimum if minimum is not None else maximum
        if exact is None:
            return _outcome(criterion, "unknown", "compensation_amount_incomplete")
        lower_bound = exact
        upper_bound = exact
    elif opportunity.amount_type == "range":
        if minimum is None or maximum is None:
            return _outcome(criterion, "unknown", "compensation_amount_incomplete")
        lower_bound = minimum
        upper_bound = maximum
    elif opportunity.amount_type == "from":
        if minimum is None:
            return _outcome(criterion, "unknown", "compensation_amount_incomplete")
        lower_bound = minimum
        upper_bound = maximum
    elif opportunity.amount_type == "up_to":
        if maximum is None:
            return _outcome(criterion, "unknown", "compensation_amount_incomplete")
        upper_bound = maximum

    if lower_bound is not None and lower_bound >= threshold:
        reason = (
            "compensation_strict_minimum_guaranteed"
            if criterion.criterion_class == "strict_preference"
            else "compensation_preferred_minimum_guaranteed"
        )
        return _outcome(criterion, "pass", reason)
    if upper_bound is not None and upper_bound < threshold:
        reason = (
            "compensation_below_strict_minimum"
            if criterion.criterion_class == "strict_preference"
            else "compensation_below_preferred_minimum"
        )
        return _outcome(criterion, "fail", reason)
    if criterion.criterion_class == "strict_preference":
        return _outcome(
            criterion,
            "fail",
            "compensation_strict_minimum_not_guaranteed",
        )
    return _outcome(
        criterion,
        "unknown",
        "compensation_preferred_range_overlap",
    )


def run_typed_match_criteria_shadow(
    profile_v2: dict,
    inventory_rows,
    effective_enrichments: dict[int, dict] | None = None,
    *,
    authoritative_matches: list[dict] | None = None,
    diagnostic_sink=None,
) -> tuple[dict, ...]:
    """Produce bounded diagnostics without mutating matcher inputs or outputs."""
    criteria = match_criteria_v1_from_profile(profile_v2)
    if type(inventory_rows) is not list or (
        effective_enrichments is not None and type(effective_enrichments) is not dict
    ) or (
        authoritative_matches is not None and type(authoritative_matches) is not list
    ):
        raise TypedCriteriaError("invalid_shadow_inventory")
    if diagnostic_sink is not None and not callable(diagnostic_sink):
        raise TypedCriteriaError("invalid_shadow_diagnostic_sink")
    effective_enrichments = effective_enrichments or {}
    authority_by_job_id = _authoritative_match_index(authoritative_matches or [])
    preference_criteria = criteria.all_criteria()
    if not preference_criteria and not authority_by_job_id:
        return ()
    records = []
    for row in inventory_rows:
        outcomes = []
        canonical_id = _row_value(row, "canonical_opportunity_id")
        effective = (
            effective_enrichments.get(canonical_id)
            if type(canonical_id) is int
            else None
        )
        if preference_criteria:
            try:
                opportunity = project_opportunity_criteria_v1(
                    effective_enrichment=effective,
                    inventory_row=row,
                )
                outcomes.extend(
                    evaluate_match_criteria_shadow(criteria, opportunity).outcomes
                )
            except Exception:
                outcomes.extend(
                    _outcome(
                        criterion,
                        "unknown",
                        "opportunity_projection_failed",
                    )
                    for criterion in preference_criteria
                )
        job_id = _row_value(row, "job_id")
        authoritative_match = (
            authority_by_job_id.get(job_id) if type(job_id) is int else None
        )
        if authoritative_match is not None:
            try:
                outcomes.extend(
                    bridge_existing_matcher_eligibility(authoritative_match)
                )
            except Exception:
                outcomes.extend(_unavailable_eligibility_outcomes())
        if not outcomes:
            continue
        evaluation = ShadowCriteriaEvaluationV1(
            outcomes=tuple(sorted(outcomes, key=lambda item: item.criterion_id))
        )
        record = {
            "schema_version": SHADOW_DIAGNOSTIC_SCHEMA_VERSION,
            "opportunity_reference": _opportunity_reference(row),
            "criteria_source_status": criteria.source_status,
            "outcomes": [item.as_dict() for item in evaluation.outcomes],
        }
        records.append(record)
        if diagnostic_sink is not None:
            try:
                diagnostic_sink(deepcopy(record))
            except Exception:
                pass
    return tuple(deepcopy(records))


class _EvidenceContext:
    def __init__(self, document, field_sources, stale_fields):
        self.document = document
        self.field_sources = field_sources
        self.stale_fields = stale_fields
        self.unknown_fields = set(document.get("unknown_fields") or [])
        self.high_evidence = {
            item["field_path"]
            for item in document.get("field_evidence") or []
            if item.get("confidence") == "high"
        }

    def reliable(self, path):
        if path in self.unknown_fields or path in self.stale_fields:
            return False
        return (
            self.field_sources.get(path) == "human_override"
            or path in self.high_evidence
        )


def _project_inventory_row(row) -> OpportunityCriteriaV1:
    relationship = _unknown_dimension("employment_relationship_unknown")
    workload = _unknown_dimension("workload_unknown")
    term = _unknown_dimension("engagement_term_unknown")
    commitment = str(_row_value(row, "commitment") or "").strip().casefold()
    normalized = commitment.replace("-", "_").replace(" ", "_")
    if normalized == "freelance":
        relationship = _known_dimension(
            ("independent_contractor",), "inventory_commitment_freelance"
        )
    elif normalized in WORKLOADS:
        workload = _known_dimension((normalized,), f"inventory_commitment_{normalized}")
    elif normalized in {"temporary", "internship"}:
        term = _known_dimension((normalized,), f"inventory_commitment_{normalized}")
    elif normalized == "contract":
        reason = "inventory_contract_ambiguous"
        relationship = _unknown_dimension(reason)
        term = _unknown_dimension(reason)

    category = str(_row_value(row, "source_category") or "").strip()
    interests = (
        _known_dimension((category,), "inventory_taxonomy_exact")
        if category in JOB_INTEREST_CODES
        else _unknown_dimension("job_interest_unknown")
    )
    return OpportunityCriteriaV1(
        employment_relationships=relationship,
        workloads=workload,
        engagement_terms=term,
        schedule_flexibility_modes=_unknown_dimension("schedule_flexibility_unknown"),
        schedule_coordination_modes=_unknown_dimension(
            "schedule_coordination_not_structured"
        ),
        schedule_time_windows=_unknown_dimension(
            "schedule_time_window_not_structured"
        ),
        phone_voice_modes=_unknown_dimension("phone_voice_unknown"),
        job_interests=interests,
        career_levels=_unknown_dimension("career_level_unknown"),
        compensation=_unknown_compensation("compensation_not_structured"),
    )


def _project_compensation(value, context):
    paths = {
        field: f"attributes.compensation.{field}"
        for field in (
            "disclosed",
            "currency",
            "amount_min",
            "amount_max",
            "period",
            "amount_type",
        )
    }
    if not context.reliable(paths["disclosed"]):
        return _unknown_compensation("compensation_disclosure_unknown")
    disclosed = value["disclosed"]
    if disclosed is False:
        return OpportunityCompensationV1(
            status="known",
            disclosed=False,
            currency=None,
            amount_min=None,
            amount_max=None,
            period=None,
            amount_type="unknown",
            reason_code="compensation_undisclosed",
        )
    if disclosed is not True:
        return _unknown_compensation("compensation_disclosure_unknown")

    currency = (
        str(value["currency"]).upper()
        if context.reliable(paths["currency"]) and value["currency"]
        else None
    )
    period = value["period"] if context.reliable(paths["period"]) else None
    amount_type = (
        value["amount_type"]
        if context.reliable(paths["amount_type"])
        else "unknown"
    )
    minimum = (
        _decimal_from_number(value["amount_min"])
        if context.reliable(paths["amount_min"])
        else None
    )
    maximum = (
        _decimal_from_number(value["amount_max"])
        if context.reliable(paths["amount_max"])
        else None
    )
    return OpportunityCompensationV1(
        status="known",
        disclosed=True,
        currency=currency,
        amount_min=minimum,
        amount_max=maximum,
        period=period,
        amount_type=amount_type,
        reason_code="compensation_structured",
    )


def _outcome(criterion, outcome, reason_code):
    return CriterionOutcomeV1(
        criterion_id=criterion.criterion_id,
        criterion_class=criterion.criterion_class,
        dimension=criterion.dimension,
        outcome=outcome,
        reason_code=reason_code,
        potentially_relaxable=(
            criterion.criterion_class == "soft_preference" and outcome == "fail"
        ),
    )


def _eligibility_outcome(criterion_id, dimension, outcome, reason_code, context):
    return CriterionOutcomeV1(
        criterion_id=criterion_id,
        criterion_class="eligibility",
        dimension=dimension,
        outcome=outcome,
        reason_code=reason_code,
        potentially_relaxable=False,
        context=tuple(sorted(context.items())),
    )


def _unavailable_eligibility_outcomes():
    specifications = (
        (
            "eligibility.required_languages",
            "required_language_eligibility",
            "matcher_language_eligibility",
        ),
        (
            "eligibility.location",
            "location_eligibility",
            "matcher_location_eligibility",
        ),
        (
            "eligibility.credentials_licenses",
            "credential_eligibility",
            "preview_credential_guardrail",
        ),
        (
            "eligibility.professional_domain",
            "professional_domain_eligibility",
            "preview_professional_domain_gate",
        ),
    )
    return tuple(
        _eligibility_outcome(
            criterion_id,
            dimension,
            "unknown",
            "eligibility_authority_unavailable",
            {"authority": authority},
        )
        for criterion_id, dimension, authority in specifications
    )


def _authoritative_match_index(matches):
    index = {}
    duplicates = set()
    for match in matches:
        if type(match) is not dict:
            raise TypedCriteriaError("invalid_eligibility_authority")
        job_id = match.get("job_id")
        if type(job_id) is not int or job_id < 0:
            continue
        if job_id in index:
            duplicates.add(job_id)
            continue
        index[job_id] = match
    for job_id in duplicates:
        index.pop(job_id, None)
    return index


def _recognized_cap_reasons(match):
    values = match.get("actionability_cap_reasons")
    allowed = _ELIGIBILITY_CONTEXT_SPEC["gate_code"] - {"none"}
    if type(values) not in {list, tuple}:
        return frozenset()
    return frozenset(value for value in values if value in allowed)


def _first_gate(actual, ordered):
    return next((gate for gate in ordered if gate in actual), None)


def _closed_value(value, allowed, default):
    return value if type(value) is str and value in allowed else default


def _closed_domain_set(value):
    if type(value) not in {list, tuple, set, frozenset}:
        return frozenset()
    return frozenset(
        item for item in value if type(item) is str and item in _DECISIVE_PROFESSIONAL_DOMAINS
    )


def _bounded_count(value):
    if type(value) not in {list, tuple, set, frozenset}:
        return 0
    return min(len(value), 64)


def _known_dimension(values, reason):
    return OpportunityDimensionV1("known", tuple(sorted(set(values))), reason)


def _unknown_dimension(reason):
    return OpportunityDimensionV1("unknown", (), reason)


def _unknown_compensation(reason):
    return OpportunityCompensationV1(
        status="unknown",
        disclosed=None,
        currency=None,
        amount_min=None,
        amount_max=None,
        period=None,
        amount_type="unknown",
        reason_code=reason,
    )


def _positive_decimal(value):
    decimal = _decimal(value)
    return decimal if decimal is not None and decimal > 0 else None


def _nonnegative_decimal(value):
    decimal = _decimal(value)
    return decimal if decimal is not None and decimal >= 0 else None


def _decimal(value):
    if type(value) is not str:
        return None
    try:
        result = Decimal(value)
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def _decimal_from_number(value):
    if type(value) not in {int, float} or (type(value) is float and not math.isfinite(value)):
        return None
    try:
        decimal = Decimal(str(value))
    except InvalidOperation:
        return None
    if not decimal.is_finite() or decimal < 0:
        return None
    return format(decimal.normalize(), "f")


def _canonical_positive_decimal(value):
    decimal = _positive_decimal(value)
    return format(decimal.normalize(), "f") if decimal is not None else None


def _valid_opportunity_reference(value):
    if type(value) is not str or len(value) > 64 or ":" not in value:
        return False
    kind, identifier = value.split(":", 1)
    return (
        kind in {"canonical", "job"}
        and len(identifier) <= 20
        and identifier.isascii()
        and identifier.isdigit()
        and int(identifier) > 0
    )


def _relaxation_scenario_id(
    criterion_id,
    proposed_value,
    proposed_minimum_amount,
    currency,
    period,
):
    if proposed_value is not None:
        return f"{criterion_id}|add|{proposed_value}"
    return (
        f"{criterion_id}|lower|{proposed_minimum_amount}|{currency}|{period}"
    )


def _nested(value, path):
    result = value
    for key in path:
        result = result[key]
    return result


def _row_value(row, key):
    if hasattr(row, "get"):
        return row.get(key)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _opportunity_reference(row):
    canonical = _row_value(row, "canonical_opportunity_id")
    if type(canonical) is int and canonical >= 0:
        return f"canonical:{canonical}"
    job = _row_value(row, "job_id")
    if type(job) is int and job >= 0:
        return f"job:{job}"
    return "unidentified"

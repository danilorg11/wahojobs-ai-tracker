"""Versioned, score-free contracts for the next matching architecture.

These contracts are intentionally inert.  They validate and serialize matching
artifacts, but they do not retrieve, rank, persist, render, or mutate anything.
The future semantic reranker is constrained to selecting grounded evidence from
the request rather than inventing profile or opportunity facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
import math
import re
from types import MappingProxyType

from wahojobs.matching.typed_criteria import CriterionOutcomeV1


DETERMINISTIC_ELIGIBILITY_DECISION_SCHEMA_VERSION = (
    "matching_deterministic_eligibility_decision_v1"
)
SHORTLIST_CANDIDATE_SCHEMA_VERSION = "matching_shortlist_candidate_v1"
GROUNDED_FACT_SCHEMA_VERSION = "matching_grounded_fact_v1"
SEMANTIC_RERANK_REQUEST_SCHEMA_VERSION = "matching_semantic_rerank_request_v1"
SEMANTIC_RERANK_RESULT_SCHEMA_VERSION = "matching_semantic_rerank_result_v1"
MATCH_RUN_SNAPSHOT_SCHEMA_VERSION = "matching_match_run_snapshot_v1"
MATCH_RUN_RESULT_SCHEMA_VERSION = "matching_match_run_result_v1"
MATCH_RUN_CANDIDATE_DISPOSITION_SCHEMA_VERSION = (
    "matching_match_run_candidate_disposition_v1"
)
OPPORTUNITY_RELATIONSHIP_SCHEMA_VERSION = (
    "matching_deterministic_opportunity_relationship_v1"
)

ELIGIBILITY_STATUSES = frozenset({"eligible", "ineligible", "unknown"})
VARIANT_DISPOSITIONS = frozenset(
    {"singleton", "canonical_representative", "material_variant"}
)
FACT_PROVENANCE = frozenset(
    {
        "user_confirmed",
        "profile_inferred",
        "automatic_enrichment",
        "human_override",
        "source_explicit",
    }
)
EVIDENCE_RELATIONS = frozenset({"supports", "adjacent", "tension"})
REQUIREMENT_STATUSES = frozenset({"met", "unmet", "unknown", "not_applicable"})
OPPORTUNITY_RELATIONSHIPS = frozenset(
    {
        "exact_duplicate",
        "eligibility_variant",
        "material_variant",
        "unresolved_related",
        "related_not_duplicate",
        "distinct",
    }
)
MATCH_RUN_STATUSES = frozenset({"completed", "failed"})
MATCH_RUN_MODES = frozenset(
    {"semantic_rerank", "deterministic_fallback", "no_candidates", "none"}
)
MATCH_RUN_CANDIDATE_DISPOSITIONS = frozenset(
    {"ranked", "not_ranked", "not_evaluated"}
)
REQUIREMENT_SET_STATUSES = frozenset({"checked", "no_structured_requirements"})
SUPPORTED_PROFILE_CONTRACT_VERSIONS_V1 = frozenset({"canonical_profile_v2"})

# This is deliberately a closed, version-owned projection of the existing
# deterministic bridge.  Adding another objective authority requires a new
# matching-contract version; relevance heuristics such as professional-domain
# fit cannot silently become hard eligibility criteria.
DETERMINISTIC_ELIGIBILITY_CRITERIA_V1 = MappingProxyType(
    {
        "eligibility.credentials_licenses": "credential_eligibility",
        "eligibility.location": "location_eligibility",
        "eligibility.required_languages": "required_language_eligibility",
    }
)

# These paths are the bounded semantic packet accepted by v1.  They are pinned
# here rather than imported from a mutable enrichment constant so changes to an
# upstream schema cannot alter this contract without an explicit version bump.
PROFILE_FACT_FIELD_PATHS_V1 = frozenset(
    {
        "profile.constraints",
        "profile.credentials",
        "profile.derived_matcher_signals",
        "profile.education",
        "profile.experience",
        "profile.identity",
        "profile.languages",
        "profile.location",
        "profile.preferences",
        "profile.skills",
    }
)
OPPORTUNITY_SOURCE_FACT_FIELD_PATHS_V1 = frozenset(
    {
        "source.availability_bases",
        "source.canonical_title",
        "source.company_name",
        "source.company_slug",
        "source.opportunity_kinds",
        "source.source_category",
        "source_content.excerpt",
        "variant.commitment",
        "variant.department",
        "variant.expertise",
        "variant.location",
        "variant.title",
        "variant.url",
    }
)
OPPORTUNITY_ENRICHMENT_FACT_FIELD_PATHS_V1 = frozenset(
    {
        "attributes.application.application_url",
        "attributes.application.assessment_required",
        "attributes.application.deadline",
        "attributes.application.login_required",
        "attributes.application.portfolio_or_sample_required",
        "attributes.compensation.amount_max",
        "attributes.compensation.amount_min",
        "attributes.compensation.amount_type",
        "attributes.compensation.currency",
        "attributes.compensation.disclosed",
        "attributes.compensation.notes",
        "attributes.compensation.period",
        "attributes.content.benefits",
        "attributes.content.candidate_profile",
        "attributes.content.caveats",
        "attributes.content.quick_take",
        "attributes.content.responsibilities",
        "attributes.requirements.credentials",
        "attributes.requirements.education.accepted_alternatives",
        "attributes.requirements.education.minimum_level",
        "attributes.requirements.languages",
        "attributes.requirements.licenses",
        "attributes.requirements.skills_preferred",
        "attributes.requirements.skills_required",
        "attributes.requirements.years_experience_min",
        "attributes.role.professional_domains",
        "attributes.role.role_family",
        "attributes.role.seniority",
        "attributes.role.specializations",
        "attributes.role.work_activities",
        "attributes.work_arrangement.duration",
        "attributes.work_arrangement.eligible_countries",
        "attributes.work_arrangement.eligible_locations",
        "attributes.work_arrangement.eligible_regions",
        "attributes.work_arrangement.engagement_type",
        "attributes.work_arrangement.hours_per_week_max",
        "attributes.work_arrangement.hours_per_week_min",
        "attributes.work_arrangement.location_scope",
        "attributes.work_arrangement.schedule_type",
        "attributes.work_arrangement.workplace_mode",
    }
)
OPPORTUNITY_FACT_FIELD_PATHS_V1 = (
    OPPORTUNITY_SOURCE_FACT_FIELD_PATHS_V1
    | OPPORTUNITY_ENRICHMENT_FACT_FIELD_PATHS_V1
)
REQUIREMENT_FACT_FIELD_PATHS_V1 = frozenset(
    {
        "attributes.requirements.credentials",
        "attributes.requirements.education.accepted_alternatives",
        "attributes.requirements.education.minimum_level",
        "attributes.requirements.languages",
        "attributes.requirements.licenses",
        "attributes.requirements.skills_required",
        "attributes.requirements.years_experience_min",
        "attributes.work_arrangement.eligible_countries",
        "attributes.work_arrangement.eligible_locations",
        "attributes.work_arrangement.eligible_regions",
        "attributes.work_arrangement.location_scope",
    }
)

_REFERENCE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}:[^\s]{1,256}$")
_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,95}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_MODEL_JSON_BYTES = 1024 * 1024


class MatchingContractError(ValueError):
    """A bounded validation failure that does not echo candidate data."""

    def __init__(self, *reason_codes: str):
        self.reason_codes = tuple(sorted(set(reason_codes or ("invalid_contract",))))[:32]
        super().__init__(
            "matching contract rejected; reason_codes=" + ",".join(self.reason_codes)
        )


def canonical_json(value) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def contract_fingerprint(value) -> str:
    if hasattr(value, "as_dict"):
        value = value.as_dict()
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _valid_reference(value) -> bool:
    return type(value) is str and _REFERENCE_RE.fullmatch(value) is not None


def _valid_token(value) -> bool:
    return type(value) is str and _TOKEN_RE.fullmatch(value) is not None


def _valid_sha256(value) -> bool:
    return type(value) is str and _SHA256_RE.fullmatch(value) is not None


def _valid_positive_int(value) -> bool:
    return type(value) is int and value > 0


def _valid_optional_positive_int(value) -> bool:
    return value is None or _valid_positive_int(value)


def _is_sorted_unique_strings(values) -> bool:
    return (
        type(values) is tuple
        and all(type(item) is str and item for item in values)
        and values == tuple(sorted(values))
        and len(values) == len(set(values))
    )


def _valid_iso_datetime(value) -> bool:
    if type(value) is not str or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _valid_json_value(value) -> bool:
    if not _is_strict_json_value(value):
        return False
    try:
        serialized = canonical_json(value)
    except (TypeError, ValueError):
        return False
    return len(serialized.encode("utf-8")) <= 4096


def _is_strict_json_value(value) -> bool:
    if value is None or type(value) in {bool, str, int}:
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(_is_strict_json_value(item) for item in value)
    if type(value) is dict:
        return all(
            type(key) is str and _is_strict_json_value(item)
            for key, item in value.items()
        )
    return False


def _has_reference_namespace(value: str, namespace: str) -> bool:
    return _valid_reference(value) and value.startswith(namespace + ":")


def _aggregate_eligibility(outcomes: tuple[CriterionOutcomeV1, ...]) -> str:
    values = {item.outcome for item in outcomes}
    if "fail" in values:
        return "ineligible"
    if "unknown" in values:
        return "unknown"
    return "eligible"


@dataclass(frozen=True, slots=True)
class DeterministicEligibilityDecisionV1:
    """Aggregate only objective eligibility outcomes; never soft relevance."""

    opportunity_ref: str
    policy_version: str
    status: str
    outcomes: tuple[CriterionOutcomeV1, ...]
    schema_version: str = DETERMINISTIC_ELIGIBILITY_DECISION_SCHEMA_VERSION

    def __post_init__(self):
        if (
            type(self.outcomes) is not tuple
            or not self.outcomes
            or any(type(item) is not CriterionOutcomeV1 for item in self.outcomes)
        ):
            raise MatchingContractError("invalid_eligibility_decision")
        criterion_ids = tuple(item.criterion_id for item in self.outcomes)
        expected_criteria = DETERMINISTIC_ELIGIBILITY_CRITERIA_V1
        invalid = (
            self.schema_version
            != DETERMINISTIC_ELIGIBILITY_DECISION_SCHEMA_VERSION
            or not _valid_reference(self.opportunity_ref)
            or not _valid_token(self.policy_version)
            or self.status not in ELIGIBILITY_STATUSES
            or any(
                item.criterion_class != "eligibility"
                or expected_criteria.get(item.criterion_id) != item.dimension
                for item in self.outcomes
            )
            or criterion_ids != tuple(sorted(criterion_ids))
            or len(criterion_ids) != len(set(criterion_ids))
            or frozenset(criterion_ids) != frozenset(expected_criteria)
            or self.status != _aggregate_eligibility(self.outcomes)
        )
        if invalid:
            raise MatchingContractError("invalid_eligibility_decision")

    @classmethod
    def from_outcomes(
        cls,
        *,
        opportunity_ref: str,
        policy_version: str,
        outcomes: tuple[CriterionOutcomeV1, ...],
    ) -> "DeterministicEligibilityDecisionV1":
        if (
            type(outcomes) is not tuple
            or not outcomes
            or any(type(item) is not CriterionOutcomeV1 for item in outcomes)
        ):
            raise MatchingContractError("invalid_eligibility_decision")
        return cls(
            opportunity_ref=opportunity_ref,
            policy_version=policy_version,
            status=_aggregate_eligibility(outcomes),
            outcomes=outcomes,
        )

    @property
    def unresolved_criterion_ids(self) -> tuple[str, ...]:
        return tuple(item.criterion_id for item in self.outcomes if item.outcome == "unknown")

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "opportunity_ref": self.opportunity_ref,
            "policy_version": self.policy_version,
            "status": self.status,
            "outcomes": [item.as_dict() for item in self.outcomes],
        }


@dataclass(frozen=True, slots=True)
class ShortlistCandidateV1:
    """One admitted candidate before semantic reranking, with no score or band."""

    opportunity_ref: str
    canonical_opportunity_id: int | None
    selected_job_id: int
    variant_group_ref: str
    variant_disposition: str
    enrichment_fingerprint: str
    eligibility: DeterministicEligibilityDecisionV1
    retrieval_channels: tuple[str, ...]
    missing_semantic_fields: tuple[str, ...] = ()
    schema_version: str = SHORTLIST_CANDIDATE_SCHEMA_VERSION

    def __post_init__(self):
        invalid = (
            self.schema_version != SHORTLIST_CANDIDATE_SCHEMA_VERSION
            or not _valid_reference(self.opportunity_ref)
            or not _valid_optional_positive_int(self.canonical_opportunity_id)
            or not _valid_positive_int(self.selected_job_id)
            or not _valid_reference(self.variant_group_ref)
            or self.variant_disposition not in VARIANT_DISPOSITIONS
            or not _valid_sha256(self.enrichment_fingerprint)
            or type(self.eligibility) is not DeterministicEligibilityDecisionV1
            or self.eligibility.opportunity_ref != self.opportunity_ref
            or self.eligibility.status == "ineligible"
            or not _is_sorted_unique_strings(self.retrieval_channels)
            or not self.retrieval_channels
            or any(not _valid_token(item) for item in self.retrieval_channels)
            or not _is_sorted_unique_strings(self.missing_semantic_fields)
            or any(
                item not in OPPORTUNITY_ENRICHMENT_FACT_FIELD_PATHS_V1
                for item in self.missing_semantic_fields
            )
        )
        if invalid:
            raise MatchingContractError("invalid_shortlist_candidate")

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "opportunity_ref": self.opportunity_ref,
            "canonical_opportunity_id": self.canonical_opportunity_id,
            "selected_job_id": self.selected_job_id,
            "variant_group_ref": self.variant_group_ref,
            "variant_disposition": self.variant_disposition,
            "enrichment_fingerprint": self.enrichment_fingerprint,
            "eligibility": self.eligibility.as_dict(),
            "retrieval_channels": list(self.retrieval_channels),
            "missing_semantic_fields": list(self.missing_semantic_fields),
        }


@dataclass(frozen=True, slots=True)
class GroundedFactV1:
    """A bounded fact projected from an existing profile or enrichment document."""

    fact_ref: str
    field_path: str
    value: object
    provenance: str
    source_refs: tuple[str, ...]
    schema_version: str = GROUNDED_FACT_SCHEMA_VERSION

    def __post_init__(self):
        invalid = (
            self.schema_version != GROUNDED_FACT_SCHEMA_VERSION
            or not _valid_reference(self.fact_ref)
            or self.field_path
            not in (PROFILE_FACT_FIELD_PATHS_V1 | OPPORTUNITY_FACT_FIELD_PATHS_V1)
            or not _valid_json_value(self.value)
            or self.provenance not in FACT_PROVENANCE
            or not _is_sorted_unique_strings(self.source_refs)
            or not self.source_refs
            or any(not _valid_reference(item) for item in self.source_refs)
        )
        if invalid:
            raise MatchingContractError("invalid_grounded_fact")

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "fact_ref": self.fact_ref,
            "field_path": self.field_path,
            "value": self.value,
            "provenance": self.provenance,
            "source_refs": list(self.source_refs),
        }


@dataclass(frozen=True, slots=True)
class SemanticRerankCandidateInputV1:
    shortlist_candidate: ShortlistCandidateV1
    opportunity_facts: tuple[GroundedFactV1, ...]

    def __post_init__(self):
        if (
            type(self.shortlist_candidate) is not ShortlistCandidateV1
            or type(self.opportunity_facts) is not tuple
            or not self.opportunity_facts
            or any(type(item) is not GroundedFactV1 for item in self.opportunity_facts)
        ):
            raise MatchingContractError("invalid_semantic_candidate_input")
        fact_refs = tuple(item.fact_ref for item in self.opportunity_facts)
        if (
            fact_refs != tuple(sorted(fact_refs))
            or len(fact_refs) != len(set(fact_refs))
        ):
            raise MatchingContractError("invalid_semantic_candidate_input")

    @property
    def opportunity_ref(self) -> str:
        return self.shortlist_candidate.opportunity_ref

    def as_dict(self) -> dict:
        return {
            "shortlist_candidate": self.shortlist_candidate.as_dict(),
            "opportunity_facts": [item.as_dict() for item in self.opportunity_facts],
        }


@dataclass(frozen=True, slots=True)
class SemanticRerankRequestV1:
    request_ref: str
    profile_revision_ref: str
    profile_contract_version: str
    profile_fingerprint: str
    taxonomy_version: str
    profile_facts: tuple[GroundedFactV1, ...]
    candidates: tuple[SemanticRerankCandidateInputV1, ...]
    schema_version: str = SEMANTIC_RERANK_REQUEST_SCHEMA_VERSION

    def __post_init__(self):
        if (
            type(self.profile_facts) is not tuple
            or not self.profile_facts
            or any(type(item) is not GroundedFactV1 for item in self.profile_facts)
            or type(self.candidates) is not tuple
            or not self.candidates
            or any(
                type(item) is not SemanticRerankCandidateInputV1
                for item in self.candidates
            )
        ):
            raise MatchingContractError("invalid_semantic_rerank_request")
        profile_fact_refs = tuple(item.fact_ref for item in self.profile_facts)
        candidate_refs = tuple(item.opportunity_ref for item in self.candidates)
        all_fact_refs = set(profile_fact_refs)
        for candidate in self.candidates:
            all_fact_refs.update(item.fact_ref for item in candidate.opportunity_facts)
        if (
            self.schema_version != SEMANTIC_RERANK_REQUEST_SCHEMA_VERSION
            or not _valid_reference(self.request_ref)
            or not _valid_reference(self.profile_revision_ref)
            or self.profile_contract_version
            not in SUPPORTED_PROFILE_CONTRACT_VERSIONS_V1
            or not _valid_sha256(self.profile_fingerprint)
            or not _valid_token(self.taxonomy_version)
            or any(
                not _has_reference_namespace(item.fact_ref, "profile_fact")
                or item.field_path not in PROFILE_FACT_FIELD_PATHS_V1
                or self.profile_revision_ref not in item.source_refs
                for item in self.profile_facts
            )
            or profile_fact_refs != tuple(sorted(profile_fact_refs))
            or len(profile_fact_refs) != len(set(profile_fact_refs))
            or candidate_refs != tuple(sorted(candidate_refs))
            or len(candidate_refs) != len(set(candidate_refs))
            or len(all_fact_refs)
            != len(profile_fact_refs)
            + sum(len(item.opportunity_facts) for item in self.candidates)
            or any(
                not _has_reference_namespace(fact.fact_ref, "opportunity_fact")
                or fact.field_path not in OPPORTUNITY_FACT_FIELD_PATHS_V1
                or candidate.opportunity_ref not in fact.source_refs
                for candidate in self.candidates
                for fact in candidate.opportunity_facts
            )
        ):
            raise MatchingContractError("invalid_semantic_rerank_request")

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "request_ref": self.request_ref,
            "profile_revision_ref": self.profile_revision_ref,
            "profile_contract_version": self.profile_contract_version,
            "profile_fingerprint": self.profile_fingerprint,
            "taxonomy_version": self.taxonomy_version,
            "profile_facts": [item.as_dict() for item in self.profile_facts],
            "candidates": [item.as_dict() for item in self.candidates],
        }


@dataclass(frozen=True, slots=True)
class EvidenceLinkV1:
    profile_fact_ref: str
    opportunity_fact_ref: str
    relation: str
    reason_code: str

    def __post_init__(self):
        if (
            not _valid_reference(self.profile_fact_ref)
            or not _valid_reference(self.opportunity_fact_ref)
            or self.relation not in EVIDENCE_RELATIONS
            or not _valid_token(self.reason_code)
        ):
            raise MatchingContractError("invalid_evidence_link")

    def as_dict(self) -> dict:
        return {
            "profile_fact_ref": self.profile_fact_ref,
            "opportunity_fact_ref": self.opportunity_fact_ref,
            "relation": self.relation,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class RequirementCheckV1:
    opportunity_fact_ref: str
    status: str
    profile_fact_refs: tuple[str, ...]
    reason_code: str

    def __post_init__(self):
        if (
            not _valid_reference(self.opportunity_fact_ref)
            or self.status not in REQUIREMENT_STATUSES
            or not _is_sorted_unique_strings(self.profile_fact_refs)
            or any(not _valid_reference(item) for item in self.profile_fact_refs)
            or (self.status == "met" and not self.profile_fact_refs)
            or not _valid_token(self.reason_code)
        ):
            raise MatchingContractError("invalid_requirement_check")

    def as_dict(self) -> dict:
        return {
            "opportunity_fact_ref": self.opportunity_fact_ref,
            "status": self.status,
            "profile_fact_refs": list(self.profile_fact_refs),
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class SemanticRerankItemV1:
    opportunity_ref: str
    rank: int
    evidence_links: tuple[EvidenceLinkV1, ...]
    requirement_set_status: str
    requirement_checks: tuple[RequirementCheckV1, ...]
    caveat_fact_refs: tuple[str, ...]
    reason_codes: tuple[str, ...]

    def __post_init__(self):
        evidence_keys = tuple(
            (
                item.profile_fact_ref,
                item.opportunity_fact_ref,
                item.relation,
                item.reason_code,
            )
            for item in self.evidence_links
            if type(item) is EvidenceLinkV1
        )
        requirement_refs = tuple(
            item.opportunity_fact_ref
            for item in self.requirement_checks
            if type(item) is RequirementCheckV1
        )
        if (
            not _valid_reference(self.opportunity_ref)
            or not _valid_positive_int(self.rank)
            or type(self.evidence_links) is not tuple
            or not self.evidence_links
            or any(type(item) is not EvidenceLinkV1 for item in self.evidence_links)
            or evidence_keys != tuple(sorted(evidence_keys))
            or len(evidence_keys) != len(set(evidence_keys))
            or self.requirement_set_status not in REQUIREMENT_SET_STATUSES
            or type(self.requirement_checks) is not tuple
            or any(type(item) is not RequirementCheckV1 for item in self.requirement_checks)
            or requirement_refs != tuple(sorted(requirement_refs))
            or len(requirement_refs) != len(set(requirement_refs))
            or (
                self.requirement_set_status == "checked"
                and not self.requirement_checks
            )
            or (
                self.requirement_set_status == "no_structured_requirements"
                and self.requirement_checks
            )
            or not _is_sorted_unique_strings(self.caveat_fact_refs)
            or any(not _valid_reference(item) for item in self.caveat_fact_refs)
            or not _is_sorted_unique_strings(self.reason_codes)
            or not self.reason_codes
            or any(not _valid_token(item) for item in self.reason_codes)
        ):
            raise MatchingContractError("invalid_semantic_rerank_item")

    def as_dict(self) -> dict:
        return {
            "opportunity_ref": self.opportunity_ref,
            "rank": self.rank,
            "evidence_links": [item.as_dict() for item in self.evidence_links],
            "requirement_set_status": self.requirement_set_status,
            "requirement_checks": [item.as_dict() for item in self.requirement_checks],
            "caveat_fact_refs": list(self.caveat_fact_refs),
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True, slots=True)
class OpportunityRelationshipV1:
    identity_policy_version: str
    left_opportunity_ref: str
    right_opportunity_ref: str
    relationship: str
    distinguishing_field_paths: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    reason_code: str
    schema_version: str = OPPORTUNITY_RELATIONSHIP_SCHEMA_VERSION

    def __post_init__(self):
        if (
            self.schema_version != OPPORTUNITY_RELATIONSHIP_SCHEMA_VERSION
            or not _valid_token(self.identity_policy_version)
            or not _valid_reference(self.left_opportunity_ref)
            or not _valid_reference(self.right_opportunity_ref)
            or self.left_opportunity_ref >= self.right_opportunity_ref
            or self.relationship not in OPPORTUNITY_RELATIONSHIPS
            or not _is_sorted_unique_strings(self.distinguishing_field_paths)
            or any(
                item not in OPPORTUNITY_FACT_FIELD_PATHS_V1
                for item in self.distinguishing_field_paths
            )
            or not _is_sorted_unique_strings(self.evidence_refs)
            or not self.evidence_refs
            or any(not _valid_reference(item) for item in self.evidence_refs)
            or not _valid_token(self.reason_code)
        ):
            raise MatchingContractError("invalid_opportunity_relationship")

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "identity_policy_version": self.identity_policy_version,
            "left_opportunity_ref": self.left_opportunity_ref,
            "right_opportunity_ref": self.right_opportunity_ref,
            "relationship": self.relationship,
            "distinguishing_field_paths": list(self.distinguishing_field_paths),
            "evidence_refs": list(self.evidence_refs),
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class SemanticRerankResultV1:
    request_ref: str
    producer_version: str
    items: tuple[SemanticRerankItemV1, ...]
    schema_version: str = SEMANTIC_RERANK_RESULT_SCHEMA_VERSION

    def __post_init__(self):
        if (
            type(self.items) is not tuple
            or not self.items
            or any(type(item) is not SemanticRerankItemV1 for item in self.items)
        ):
            raise MatchingContractError("invalid_semantic_rerank_result")
        item_refs = tuple(item.opportunity_ref for item in self.items)
        if (
            self.schema_version != SEMANTIC_RERANK_RESULT_SCHEMA_VERSION
            or not _valid_reference(self.request_ref)
            or not _valid_token(self.producer_version)
            or len(item_refs) != len(set(item_refs))
            or tuple(item.rank for item in self.items)
            != tuple(range(1, len(self.items) + 1))
        ):
            raise MatchingContractError("invalid_semantic_rerank_result")

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "request_ref": self.request_ref,
            "producer_version": self.producer_version,
            "items": [item.as_dict() for item in self.items],
        }


def validate_semantic_rerank_result(
    request: SemanticRerankRequestV1,
    result: SemanticRerankResultV1,
) -> None:
    """Reject omissions, additions, or evidence references absent from the input."""

    if (
        type(request) is not SemanticRerankRequestV1
        or type(result) is not SemanticRerankResultV1
        or result.request_ref != request.request_ref
    ):
        raise MatchingContractError("semantic_request_result_mismatch")

    candidate_index = {item.opportunity_ref: item for item in request.candidates}
    expected_refs = set(candidate_index)
    result_refs = {item.opportunity_ref for item in result.items}
    if result_refs != expected_refs:
        raise MatchingContractError("semantic_candidate_set_mismatch")

    profile_fact_refs = {item.fact_ref for item in request.profile_facts}
    for item in result.items:
        opportunity_facts = candidate_index[item.opportunity_ref].opportunity_facts
        opportunity_fact_refs = {fact.fact_ref for fact in opportunity_facts}
        requirement_fact_refs = {
            fact.fact_ref
            for fact in opportunity_facts
            if fact.field_path in REQUIREMENT_FACT_FIELD_PATHS_V1
        }
        caveat_fact_refs = {
            fact.fact_ref
            for fact in opportunity_facts
            if fact.field_path == "attributes.content.caveats"
        }
        for evidence in item.evidence_links:
            if (
                evidence.profile_fact_ref not in profile_fact_refs
                or evidence.opportunity_fact_ref not in opportunity_fact_refs
            ):
                raise MatchingContractError("semantic_evidence_reference_unresolved")
        for check in item.requirement_checks:
            if (
                check.opportunity_fact_ref not in opportunity_fact_refs
                or not set(check.profile_fact_refs).issubset(profile_fact_refs)
            ):
                raise MatchingContractError("semantic_requirement_reference_unresolved")
        if not set(item.caveat_fact_refs).issubset(opportunity_fact_refs):
            raise MatchingContractError("semantic_caveat_reference_unresolved")
        checked_requirement_refs = {
            check.opportunity_fact_ref for check in item.requirement_checks
        }
        expected_requirement_status = (
            "checked" if requirement_fact_refs else "no_structured_requirements"
        )
        if (
            item.requirement_set_status != expected_requirement_status
            or checked_requirement_refs != requirement_fact_refs
        ):
            raise MatchingContractError("semantic_requirements_incomplete")
        if set(item.caveat_fact_refs) != caveat_fact_refs:
            raise MatchingContractError("semantic_caveats_incomplete")


def parse_semantic_rerank_result_json(
    raw_json: str | bytes,
    request: SemanticRerankRequestV1,
) -> SemanticRerankResultV1:
    """Parse an untrusted model response through an exact, closed JSON boundary."""

    if type(request) is not SemanticRerankRequestV1:
        raise MatchingContractError("invalid_semantic_rerank_request")
    if type(raw_json) is bytes:
        try:
            raw_json = raw_json.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise MatchingContractError("invalid_semantic_rerank_json") from exc
    if (
        type(raw_json) is not str
        or not raw_json
        or len(raw_json.encode("utf-8")) > _MAX_MODEL_JSON_BYTES
    ):
        raise MatchingContractError("invalid_semantic_rerank_json")
    try:
        payload = json.loads(
            raw_json,
            object_pairs_hook=_closed_json_object,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MatchingContractError("invalid_semantic_rerank_json") from exc
    try:
        result = _semantic_rerank_result_from_json(payload)
        validate_semantic_rerank_result(request, result)
    except MatchingContractError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise MatchingContractError("invalid_semantic_rerank_json") from exc
    return result


def _closed_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_json_constant(_value):
    raise ValueError("nonstandard_json_number")


def _exact_json_object(value, keys: frozenset[str], reason_code: str) -> dict:
    if type(value) is not dict or frozenset(value) != keys:
        raise MatchingContractError(reason_code)
    return value


def _json_tuple(value, reason_code: str) -> tuple:
    if type(value) is not list:
        raise MatchingContractError(reason_code)
    return tuple(value)


def _json_string_tuple(value, reason_code: str) -> tuple[str, ...]:
    values = _json_tuple(value, reason_code)
    if any(type(item) is not str for item in values):
        raise MatchingContractError(reason_code)
    return values


def _semantic_rerank_result_from_json(payload) -> SemanticRerankResultV1:
    payload = _exact_json_object(
        payload,
        frozenset({"schema_version", "request_ref", "producer_version", "items"}),
        "invalid_semantic_rerank_json",
    )
    items = tuple(
        _semantic_rerank_item_from_json(item)
        for item in _json_tuple(payload["items"], "invalid_semantic_rerank_json")
    )
    return SemanticRerankResultV1(
        schema_version=payload["schema_version"],
        request_ref=payload["request_ref"],
        producer_version=payload["producer_version"],
        items=items,
    )


def _semantic_rerank_item_from_json(payload) -> SemanticRerankItemV1:
    payload = _exact_json_object(
        payload,
        frozenset(
            {
                "opportunity_ref",
                "rank",
                "evidence_links",
                "requirement_set_status",
                "requirement_checks",
                "caveat_fact_refs",
                "reason_codes",
            }
        ),
        "invalid_semantic_rerank_item_json",
    )
    return SemanticRerankItemV1(
        opportunity_ref=payload["opportunity_ref"],
        rank=payload["rank"],
        evidence_links=tuple(
            _evidence_link_from_json(item)
            for item in _json_tuple(
                payload["evidence_links"], "invalid_semantic_rerank_item_json"
            )
        ),
        requirement_set_status=payload["requirement_set_status"],
        requirement_checks=tuple(
            _requirement_check_from_json(item)
            for item in _json_tuple(
                payload["requirement_checks"], "invalid_semantic_rerank_item_json"
            )
        ),
        caveat_fact_refs=_json_string_tuple(
            payload["caveat_fact_refs"], "invalid_semantic_rerank_item_json"
        ),
        reason_codes=_json_string_tuple(
            payload["reason_codes"], "invalid_semantic_rerank_item_json"
        ),
    )


def _evidence_link_from_json(payload) -> EvidenceLinkV1:
    payload = _exact_json_object(
        payload,
        frozenset(
            {
                "profile_fact_ref",
                "opportunity_fact_ref",
                "relation",
                "reason_code",
            }
        ),
        "invalid_evidence_link_json",
    )
    return EvidenceLinkV1(**payload)


def _requirement_check_from_json(payload) -> RequirementCheckV1:
    payload = _exact_json_object(
        payload,
        frozenset(
            {
                "opportunity_fact_ref",
                "status",
                "profile_fact_refs",
                "reason_code",
            }
        ),
        "invalid_requirement_check_json",
    )
    return RequirementCheckV1(
        opportunity_fact_ref=payload["opportunity_fact_ref"],
        status=payload["status"],
        profile_fact_refs=_json_string_tuple(
            payload["profile_fact_refs"], "invalid_requirement_check_json"
        ),
        reason_code=payload["reason_code"],
    )


@dataclass(frozen=True, slots=True)
class MatchRunSnapshotV1:
    run_ref: str
    created_at: str
    profile_revision_ref: str
    profile_fingerprint: str
    inventory_as_of: str
    inventory_fingerprint: str
    eligibility_policy_version: str
    identity_policy_version: str
    shortlist_policy_version: str
    taxonomy_version: str
    candidates: tuple[ShortlistCandidateV1, ...]
    schema_version: str = MATCH_RUN_SNAPSHOT_SCHEMA_VERSION

    def __post_init__(self):
        if (
            type(self.candidates) is not tuple
            or any(type(item) is not ShortlistCandidateV1 for item in self.candidates)
        ):
            raise MatchingContractError("invalid_match_run_snapshot")
        candidate_refs = tuple(item.opportunity_ref for item in self.candidates)
        if (
            self.schema_version != MATCH_RUN_SNAPSHOT_SCHEMA_VERSION
            or not _valid_reference(self.run_ref)
            or not _valid_iso_datetime(self.created_at)
            or not _valid_reference(self.profile_revision_ref)
            or not _valid_sha256(self.profile_fingerprint)
            or not _valid_iso_datetime(self.inventory_as_of)
            or not _valid_sha256(self.inventory_fingerprint)
            or not _valid_token(self.eligibility_policy_version)
            or not _valid_token(self.identity_policy_version)
            or not _valid_token(self.shortlist_policy_version)
            or not _valid_token(self.taxonomy_version)
            or candidate_refs != tuple(sorted(candidate_refs))
            or len(candidate_refs) != len(set(candidate_refs))
        ):
            raise MatchingContractError("invalid_match_run_snapshot")

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "run_ref": self.run_ref,
            "created_at": self.created_at,
            "profile_revision_ref": self.profile_revision_ref,
            "profile_fingerprint": self.profile_fingerprint,
            "inventory_as_of": self.inventory_as_of,
            "inventory_fingerprint": self.inventory_fingerprint,
            "eligibility_policy_version": self.eligibility_policy_version,
            "identity_policy_version": self.identity_policy_version,
            "shortlist_policy_version": self.shortlist_policy_version,
            "taxonomy_version": self.taxonomy_version,
            "candidates": [item.as_dict() for item in self.candidates],
        }

    @property
    def fingerprint(self) -> str:
        return contract_fingerprint(self)


@dataclass(frozen=True, slots=True)
class MatchRunCandidateDispositionV1:
    """Explicit terminal handling for one candidate in a MatchRun."""

    opportunity_ref: str
    disposition: str
    reason_codes: tuple[str, ...]
    schema_version: str = MATCH_RUN_CANDIDATE_DISPOSITION_SCHEMA_VERSION

    def __post_init__(self):
        if (
            self.schema_version != MATCH_RUN_CANDIDATE_DISPOSITION_SCHEMA_VERSION
            or not _valid_reference(self.opportunity_ref)
            or self.disposition not in MATCH_RUN_CANDIDATE_DISPOSITIONS
            or not _is_sorted_unique_strings(self.reason_codes)
            or not self.reason_codes
            or any(not _valid_token(item) for item in self.reason_codes)
        ):
            raise MatchingContractError("invalid_match_run_candidate_disposition")

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "opportunity_ref": self.opportunity_ref,
            "disposition": self.disposition,
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True, slots=True)
class MatchRunResultV1:
    run_ref: str
    snapshot_fingerprint: str
    completed_at: str
    status: str
    mode: str
    producer_version: str
    items: tuple[SemanticRerankItemV1, ...]
    candidate_dispositions: tuple[MatchRunCandidateDispositionV1, ...]
    schema_version: str = MATCH_RUN_RESULT_SCHEMA_VERSION

    def __post_init__(self):
        if (
            type(self.items) is not tuple
            or any(type(item) is not SemanticRerankItemV1 for item in self.items)
            or type(self.candidate_dispositions) is not tuple
            or any(
                type(item) is not MatchRunCandidateDispositionV1
                for item in self.candidate_dispositions
            )
        ):
            raise MatchingContractError("invalid_match_run_result")
        item_refs = tuple(item.opportunity_ref for item in self.items)
        disposition_refs = tuple(
            item.opportunity_ref for item in self.candidate_dispositions
        )
        ranked_refs = {
            item.opportunity_ref
            for item in self.candidate_dispositions
            if item.disposition == "ranked"
        }
        if (
            self.schema_version != MATCH_RUN_RESULT_SCHEMA_VERSION
            or not _valid_reference(self.run_ref)
            or not _valid_sha256(self.snapshot_fingerprint)
            or not _valid_iso_datetime(self.completed_at)
            or self.status not in MATCH_RUN_STATUSES
            or self.mode not in MATCH_RUN_MODES
            or not _valid_token(self.producer_version)
            or len(item_refs) != len(set(item_refs))
            or tuple(item.rank for item in self.items)
            != tuple(range(1, len(self.items) + 1))
            or disposition_refs != tuple(sorted(disposition_refs))
            or len(disposition_refs) != len(set(disposition_refs))
            or ranked_refs != set(item_refs)
            or (
                self.status == "failed"
                and (
                    self.mode != "none"
                    or self.items
                    or any(
                        item.disposition != "not_evaluated"
                        for item in self.candidate_dispositions
                    )
                )
            )
            or (
                self.status == "completed"
                and (
                    self.mode == "none"
                    or any(
                        item.disposition == "not_evaluated"
                        for item in self.candidate_dispositions
                    )
                )
            )
            or (
                self.mode == "no_candidates"
                and (self.items or self.candidate_dispositions)
            )
        ):
            raise MatchingContractError("invalid_match_run_result")

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "run_ref": self.run_ref,
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "completed_at": self.completed_at,
            "status": self.status,
            "mode": self.mode,
            "producer_version": self.producer_version,
            "items": [item.as_dict() for item in self.items],
            "candidate_dispositions": [
                item.as_dict() for item in self.candidate_dispositions
            ],
        }


def validate_match_run_result(
    snapshot: MatchRunSnapshotV1,
    result: MatchRunResultV1,
) -> None:
    if (
        type(snapshot) is not MatchRunSnapshotV1
        or type(result) is not MatchRunResultV1
        or snapshot.run_ref != result.run_ref
        or snapshot.fingerprint != result.snapshot_fingerprint
    ):
        raise MatchingContractError("match_run_snapshot_result_mismatch")
    candidate_refs = {item.opportunity_ref for item in snapshot.candidates}
    disposition_refs = {
        item.opportunity_ref for item in result.candidate_dispositions
    }
    if disposition_refs != candidate_refs:
        raise MatchingContractError("match_run_candidate_dispositions_incomplete")
    if (result.mode == "no_candidates") != (
        result.status == "completed" and not candidate_refs
    ):
        raise MatchingContractError("match_run_no_candidates_mismatch")

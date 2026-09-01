"""Shadow-only semantic matching over native OE semantic packets.

This module is intentionally disconnected from the product matcher and UI.  It
accepts deterministic shortlist survivors from ``foundation_contracts`` and
native ``oe_semantic_matching_packet_v1`` packets.  Semantic evidence can only
add positive ordering support; it cannot remove a survivor, change an
eligibility decision, suppress lifecycle/trust state, or mutate profile truth.

The local evaluator is an architecture/evaluation control, not a production
semantic model.  It performs exact matching between grounded normalized
profile signals and typed packet proposition values while preserving packet
DNF and variant scope.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, replace
import re
import unicodedata

from wahojobs.matching.foundation_contracts import (
    DeterministicEligibilityDecisionV1,
    GroundedFactV1,
    ShortlistCandidateV1,
    contract_fingerprint,
)
from wahojobs.opportunity_semantic_authority import (
    AUTHORITY_POLICY_VERSION,
    SEMANTIC_AUTHORITY_TYPE,
    SEMANTIC_MATCHING_PACKET_VERSION,
    OpportunitySemanticAuthorityError,
    semantic_non_exclusionary_authority,
    validate_semantic_matching_packet,
)
from wahojobs.opportunity_semantic_contract import ATOM_KINDS, MODALITIES


SEMANTIC_MATCHING_SHADOW_SEAM_VERSION = "matching_semantic_shadow_seam_v1"
SEMANTIC_MATCHING_SHADOW_REQUEST_SCHEMA_VERSION = (
    "matching_semantic_shadow_request_v1"
)
SEMANTIC_MATCHING_SHADOW_CANDIDATE_SCHEMA_VERSION = (
    "matching_semantic_shadow_candidate_v1"
)
SEMANTIC_PACKET_INPUT_SCHEMA_VERSION = "matching_semantic_packet_input_v1"
NORMALIZED_PROFILE_SIGNAL_SCHEMA_VERSION = (
    "matching_normalized_profile_semantic_signal_v1"
)
SEMANTIC_BRANCH_ASSESSMENT_SCHEMA_VERSION = (
    "matching_semantic_branch_assessment_v1"
)
SEMANTIC_GROUP_ASSESSMENT_SCHEMA_VERSION = (
    "matching_semantic_group_assessment_v1"
)
SEMANTIC_SUPPORTING_EVIDENCE_SCHEMA_VERSION = (
    "matching_semantic_supporting_evidence_v1"
)
SEMANTIC_UNCERTAINTY_SCHEMA_VERSION = "matching_semantic_uncertainty_v1"
SEMANTIC_MATCHING_SHADOW_ITEM_SCHEMA_VERSION = (
    "matching_semantic_shadow_item_v1"
)
SEMANTIC_MATCHING_SHADOW_RESULT_SCHEMA_VERSION = (
    "matching_semantic_shadow_result_v1"
)
LOCAL_SHADOW_EVALUATOR_VERSION = "matching_semantic_shadow_local_evaluator_v1"
SHADOW_EXECUTION_MODE = "shadow_offline_local_only"

MAX_SHADOW_SHORTLIST_SIZE_V1 = 32
MAX_PROFILE_FACTS_V1 = 128
MAX_PROFILE_SIGNALS_V1 = 128
MAX_PROFILE_TERMS_PER_SIGNAL_V1 = 24
MAX_SUPPORTING_EVIDENCE_PER_ITEM_V1 = 16
MAX_UNCERTAINTIES_PER_ITEM_V1 = 16
MAX_CANDIDATE_COPY_CHARS_V1 = 240

PACKET_INPUT_STATUSES = frozenset({"available", "unavailable", "invalid"})
PACKET_COVERAGE_STATES = frozenset(
    {"available_complete", "available_partial", "unavailable", "invalid"}
)
GROUP_ASSESSMENT_STATUSES = frozenset(
    {"supported", "partially_supported", "unknown"}
)
BRANCH_ASSESSMENT_STATUSES = GROUP_ASSESSMENT_STATUSES
SUPPORT_TYPES = frozenset(
    {
        "required_group",
        "preferred_group",
        "descriptive_group",
        "unassigned_proposition",
        "native_descriptive_signal",
    }
)
PROFILE_SIGNAL_KINDS = frozenset({*ATOM_KINDS, "descriptive"})

_REFERENCE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}:[^\s]{1,256}$")
_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,95}$")
_SEMANTIC_TERM_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,95}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SemanticMatchingShadowContractError(ValueError):
    """A bounded shadow-contract error that never echoes profile data."""

    def __init__(self, *reason_codes: str):
        self.reason_codes = tuple(
            sorted(set(reason_codes or ("invalid_semantic_shadow_contract",)))
        )[:32]
        super().__init__(
            "semantic matching shadow contract rejected; reason_codes="
            + ",".join(self.reason_codes)
        )


def _valid_reference(value) -> bool:
    return type(value) is str and _REFERENCE_RE.fullmatch(value) is not None


def _valid_token(value) -> bool:
    return type(value) is str and _TOKEN_RE.fullmatch(value) is not None


def _valid_sha256(value) -> bool:
    return type(value) is str and _SHA256_RE.fullmatch(value) is not None


def _sorted_unique_strings(values, *, allow_empty: bool) -> bool:
    return (
        type(values) is tuple
        and (allow_empty or bool(values))
        and all(type(item) is str and item for item in values)
        and values == tuple(sorted(values))
        and len(values) == len(set(values))
    )


def _bounded_candidate_copy(value) -> bool:
    return (
        type(value) is str
        and bool(value)
        and len(value) <= MAX_CANDIDATE_COPY_CHARS_V1
        and "\x00" not in value
    )


def normalize_semantic_term(value: str) -> str:
    """Normalize one exact comparison term for the local control evaluator."""

    if type(value) is not str:
        return ""
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
    return normalized[:96]


def _terms_from_value(value) -> set[str]:
    terms: set[str] = set()
    if type(value) is str:
        normalized = normalize_semantic_term(value)
        if normalized:
            terms.add(normalized)
        return terms
    if type(value) in {int, float} and type(value) is not bool:
        normalized = normalize_semantic_term(str(value))
        if normalized:
            terms.add(normalized)
        return terms
    if type(value) in {list, tuple}:
        for item in value:
            terms.update(_terms_from_value(item))
        return terms
    if type(value) is dict:
        for item in value.values():
            terms.update(_terms_from_value(item))
    return terms


def _words_from_text(value: str) -> set[str]:
    if type(value) is not str:
        return set()
    normalized = normalize_semantic_term(value)
    words = {item for item in normalized.split("_") if item}
    if normalized:
        words.add(normalized)
    return words


@dataclass(frozen=True, slots=True)
class NormalizedProfileSemanticSignalV1:
    """A comparison-only signal grounded in one immutable profile fact."""

    signal_ref: str
    profile_fact_ref: str
    semantic_kind: str
    semantic_terms: tuple[str, ...]
    schema_version: str = NORMALIZED_PROFILE_SIGNAL_SCHEMA_VERSION

    def __post_init__(self):
        if (
            self.schema_version != NORMALIZED_PROFILE_SIGNAL_SCHEMA_VERSION
            or not _valid_reference(self.signal_ref)
            or not self.signal_ref.startswith("profile_signal:")
            or not _valid_reference(self.profile_fact_ref)
            or not self.profile_fact_ref.startswith("profile_fact:")
            or self.semantic_kind not in PROFILE_SIGNAL_KINDS
            or not _sorted_unique_strings(self.semantic_terms, allow_empty=False)
            or len(self.semantic_terms) > MAX_PROFILE_TERMS_PER_SIGNAL_V1
            or any(
                _SEMANTIC_TERM_RE.fullmatch(item) is None
                or normalize_semantic_term(item) != item
                for item in self.semantic_terms
            )
        ):
            raise SemanticMatchingShadowContractError(
                "invalid_normalized_profile_signal"
            )

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "signal_ref": self.signal_ref,
            "profile_fact_ref": self.profile_fact_ref,
            "semantic_kind": self.semantic_kind,
            "semantic_terms": list(self.semantic_terms),
        }


@dataclass(frozen=True, slots=True)
class SemanticPacketInputV1:
    """Validated packet availability without allowing a partial invalid packet."""

    status: str
    packet: dict | None
    reason_codes: tuple[str, ...]
    schema_version: str = SEMANTIC_PACKET_INPUT_SCHEMA_VERSION

    def __post_init__(self):
        invalid = (
            self.schema_version != SEMANTIC_PACKET_INPUT_SCHEMA_VERSION
            or self.status not in PACKET_INPUT_STATUSES
            or not _sorted_unique_strings(
                self.reason_codes, allow_empty=self.status == "available"
            )
            or any(not _valid_token(item) for item in self.reason_codes)
            or (self.status == "available") != (type(self.packet) is dict)
            or (self.status == "available" and bool(self.reason_codes))
        )
        if invalid:
            raise SemanticMatchingShadowContractError("invalid_semantic_packet_input")
        if self.status == "available":
            try:
                validate_semantic_matching_packet(copy.deepcopy(self.packet))
            except (OpportunitySemanticAuthorityError, KeyError, TypeError) as exc:
                raise SemanticMatchingShadowContractError(
                    "invalid_semantic_packet_input"
                ) from exc

    @classmethod
    def available(cls, packet: dict) -> "SemanticPacketInputV1":
        return cls(status="available", packet=copy.deepcopy(packet), reason_codes=())

    @classmethod
    def unavailable(
        cls, reason_code: str = "semantic_packet_unavailable"
    ) -> "SemanticPacketInputV1":
        return cls(status="unavailable", packet=None, reason_codes=(reason_code,))

    @classmethod
    def invalid(
        cls, reason_code: str = "semantic_packet_invalid"
    ) -> "SemanticPacketInputV1":
        return cls(status="invalid", packet=None, reason_codes=(reason_code,))

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "packet": copy.deepcopy(self.packet),
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True, slots=True)
class SemanticMatchingShadowCandidateV1:
    """One deterministic survivor plus its native packet availability."""

    shortlist_candidate: ShortlistCandidateV1
    legacy_rank: int
    selected_variant_ref: str
    semantic_packet: SemanticPacketInputV1
    schema_version: str = SEMANTIC_MATCHING_SHADOW_CANDIDATE_SCHEMA_VERSION

    def __post_init__(self):
        invalid = (
            self.schema_version
            != SEMANTIC_MATCHING_SHADOW_CANDIDATE_SCHEMA_VERSION
            or type(self.shortlist_candidate) is not ShortlistCandidateV1
            or self.shortlist_candidate.eligibility.status == "ineligible"
            or type(self.legacy_rank) is not int
            or self.legacy_rank <= 0
            or not _valid_reference(self.selected_variant_ref)
            or type(self.semantic_packet) is not SemanticPacketInputV1
        )
        if invalid:
            raise SemanticMatchingShadowContractError(
                "invalid_semantic_shadow_candidate"
            )
        if self.semantic_packet.status == "available":
            packet = self.semantic_packet.packet
            scope = packet["opportunity_scope"]
            if (
                packet["packet_version"] != SEMANTIC_MATCHING_PACKET_VERSION
                or packet["authority_policy_version"] != AUTHORITY_POLICY_VERSION
                or packet["authority"]["authority_type"]
                != SEMANTIC_AUTHORITY_TYPE
                or scope["canonical_ref"]
                != self.shortlist_candidate.opportunity_ref
                or self.selected_variant_ref not in scope["known_variant_refs"]
            ):
                raise SemanticMatchingShadowContractError(
                    "semantic_packet_candidate_scope_mismatch"
                )

    @property
    def opportunity_ref(self) -> str:
        return self.shortlist_candidate.opportunity_ref

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "shortlist_candidate": self.shortlist_candidate.as_dict(),
            "legacy_rank": self.legacy_rank,
            "selected_variant_ref": self.selected_variant_ref,
            "semantic_packet": self.semantic_packet.as_dict(),
        }


def semantic_shadow_candidate_from_packet_v1(
    *,
    shortlist_candidate: ShortlistCandidateV1,
    legacy_rank: int,
    selected_variant_ref: str,
    packet: dict | None,
    unavailable_reason_code: str = "semantic_packet_unavailable",
) -> SemanticMatchingShadowCandidateV1:
    """Adapt one raw packet fail-soft while preserving its survivor."""

    if packet is None:
        packet_input = SemanticPacketInputV1.unavailable(unavailable_reason_code)
    else:
        try:
            packet_input = SemanticPacketInputV1.available(packet)
            candidate = SemanticMatchingShadowCandidateV1(
                shortlist_candidate=shortlist_candidate,
                legacy_rank=legacy_rank,
                selected_variant_ref=selected_variant_ref,
                semantic_packet=packet_input,
            )
            return candidate
        except SemanticMatchingShadowContractError:
            packet_input = SemanticPacketInputV1.invalid()
    return SemanticMatchingShadowCandidateV1(
        shortlist_candidate=shortlist_candidate,
        legacy_rank=legacy_rank,
        selected_variant_ref=selected_variant_ref,
        semantic_packet=packet_input,
    )


@dataclass(frozen=True, slots=True)
class SemanticMatchingShadowRequestV1:
    """Bounded native-packet request after deterministic shortlist admission."""

    request_ref: str
    profile_revision_ref: str
    profile_contract_version: str
    profile_fingerprint: str
    taxonomy_version: str
    shortlist_limit: int
    profile_facts: tuple[GroundedFactV1, ...]
    profile_signals: tuple[NormalizedProfileSemanticSignalV1, ...]
    candidates: tuple[SemanticMatchingShadowCandidateV1, ...]
    schema_version: str = SEMANTIC_MATCHING_SHADOW_REQUEST_SCHEMA_VERSION

    def __post_init__(self):
        fact_refs = tuple(
            item.fact_ref for item in self.profile_facts if type(item) is GroundedFactV1
        )
        signal_refs = tuple(
            item.signal_ref
            for item in self.profile_signals
            if type(item) is NormalizedProfileSemanticSignalV1
        )
        candidate_refs = tuple(
            item.opportunity_ref
            for item in self.candidates
            if type(item) is SemanticMatchingShadowCandidateV1
        )
        invalid = (
            self.schema_version != SEMANTIC_MATCHING_SHADOW_REQUEST_SCHEMA_VERSION
            or not _valid_reference(self.request_ref)
            or not _valid_reference(self.profile_revision_ref)
            or self.profile_contract_version != "canonical_profile_v2"
            or not _valid_sha256(self.profile_fingerprint)
            or not _valid_token(self.taxonomy_version)
            or type(self.shortlist_limit) is not int
            or not 1 <= self.shortlist_limit <= MAX_SHADOW_SHORTLIST_SIZE_V1
            or type(self.profile_facts) is not tuple
            or not self.profile_facts
            or len(self.profile_facts) > MAX_PROFILE_FACTS_V1
            or any(type(item) is not GroundedFactV1 for item in self.profile_facts)
            or fact_refs != tuple(sorted(fact_refs))
            or len(fact_refs) != len(set(fact_refs))
            or type(self.profile_signals) is not tuple
            or len(self.profile_signals) > MAX_PROFILE_SIGNALS_V1
            or any(
                type(item) is not NormalizedProfileSemanticSignalV1
                for item in self.profile_signals
            )
            or signal_refs != tuple(sorted(signal_refs))
            or len(signal_refs) != len(set(signal_refs))
            or type(self.candidates) is not tuple
            or len(self.candidates) > self.shortlist_limit
            or any(
                type(item) is not SemanticMatchingShadowCandidateV1
                for item in self.candidates
            )
            or candidate_refs != tuple(sorted(candidate_refs))
            or len(candidate_refs) != len(set(candidate_refs))
            or sorted(item.legacy_rank for item in self.candidates)
            != list(range(1, len(self.candidates) + 1))
        )
        if invalid:
            raise SemanticMatchingShadowContractError(
                "invalid_semantic_shadow_request"
            )

        facts_by_ref = {item.fact_ref: item for item in self.profile_facts}
        for signal in self.profile_signals:
            fact = facts_by_ref.get(signal.profile_fact_ref)
            if fact is None or not set(signal.semantic_terms).issubset(
                _terms_from_value(fact.value)
            ):
                raise SemanticMatchingShadowContractError(
                    "profile_signal_not_grounded_in_profile_fact"
                )

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "request_ref": self.request_ref,
            "profile_revision_ref": self.profile_revision_ref,
            "profile_contract_version": self.profile_contract_version,
            "profile_fingerprint": self.profile_fingerprint,
            "taxonomy_version": self.taxonomy_version,
            "shortlist_limit": self.shortlist_limit,
            "profile_facts": [item.as_dict() for item in self.profile_facts],
            "profile_signals": [item.as_dict() for item in self.profile_signals],
            "candidates": [item.as_dict() for item in self.candidates],
        }


@dataclass(frozen=True, slots=True)
class SemanticBranchAssessmentV1:
    branch_index: int
    all_of: tuple[str, ...]
    supported_proposition_ids: tuple[str, ...]
    unknown_proposition_ids: tuple[str, ...]
    variant_inapplicable_proposition_ids: tuple[str, ...]
    status: str
    schema_version: str = SEMANTIC_BRANCH_ASSESSMENT_SCHEMA_VERSION

    def __post_init__(self):
        all_ids = set(self.all_of)
        classifications = (
            set(self.supported_proposition_ids)
            | set(self.unknown_proposition_ids)
            | set(self.variant_inapplicable_proposition_ids)
        )
        if (
            self.schema_version != SEMANTIC_BRANCH_ASSESSMENT_SCHEMA_VERSION
            or type(self.branch_index) is not int
            or self.branch_index <= 0
            or not _sorted_unique_strings(tuple(sorted(self.all_of)), allow_empty=False)
            or len(self.all_of) != len(all_ids)
            or not _sorted_unique_strings(
                self.supported_proposition_ids, allow_empty=True
            )
            or not _sorted_unique_strings(self.unknown_proposition_ids, allow_empty=True)
            or not _sorted_unique_strings(
                self.variant_inapplicable_proposition_ids, allow_empty=True
            )
            or classifications != all_ids
            or sum(
                len(value)
                for value in (
                    self.supported_proposition_ids,
                    self.unknown_proposition_ids,
                    self.variant_inapplicable_proposition_ids,
                )
            )
            != len(all_ids)
            or self.status not in BRANCH_ASSESSMENT_STATUSES
            or (self.status == "supported")
            != (set(self.supported_proposition_ids) == all_ids)
            or (self.status == "partially_supported")
            != (bool(self.supported_proposition_ids) and self.status != "supported")
        ):
            raise SemanticMatchingShadowContractError(
                "invalid_semantic_branch_assessment"
            )

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "branch_index": self.branch_index,
            "all_of": list(self.all_of),
            "supported_proposition_ids": list(self.supported_proposition_ids),
            "unknown_proposition_ids": list(self.unknown_proposition_ids),
            "variant_inapplicable_proposition_ids": list(
                self.variant_inapplicable_proposition_ids
            ),
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class SemanticGroupAssessmentV1:
    group_id: str
    modality: str | None
    relation_state: str
    logic: dict | None
    branches: tuple[SemanticBranchAssessmentV1, ...]
    status: str
    ranking_effect: str
    authority_type: str = SEMANTIC_AUTHORITY_TYPE
    candidate_exclusion_authorized: bool = False
    schema_version: str = SEMANTIC_GROUP_ASSESSMENT_SCHEMA_VERSION

    def __post_init__(self):
        supported = any(item.status == "supported" for item in self.branches)
        partial = any(item.status == "partially_supported" for item in self.branches)
        expected_status = "supported" if supported else (
            "partially_supported" if partial else "unknown"
        )
        invalid_logic = False
        if self.logic is None:
            invalid_logic = bool(self.branches)
        elif (
            type(self.logic) is not dict
            or set(self.logic) != {"form", "any_of"}
            or self.logic.get("form") != "bounded_dnf"
            or type(self.logic.get("any_of")) is not list
            or len(self.logic["any_of"]) != len(self.branches)
            or any(
                type(item) is not dict
                or set(item) != {"all_of"}
                or type(item["all_of"]) is not list
                or item["all_of"] != list(branch.all_of)
                for item, branch in zip(self.logic["any_of"], self.branches)
            )
        ):
            invalid_logic = True
        if (
            self.schema_version != SEMANTIC_GROUP_ASSESSMENT_SCHEMA_VERSION
            or not _valid_token(self.group_id)
            or self.modality not in ({None} | set(MODALITIES))
            or not _valid_token(self.relation_state)
            or type(self.branches) is not tuple
            or any(type(item) is not SemanticBranchAssessmentV1 for item in self.branches)
            or invalid_logic
            or self.status not in GROUP_ASSESSMENT_STATUSES
            or self.status != expected_status
            or self.ranking_effect
            != ("positive_support" if self.status == "supported" else "none")
            or self.authority_type != SEMANTIC_AUTHORITY_TYPE
            or self.candidate_exclusion_authorized is not False
        ):
            raise SemanticMatchingShadowContractError(
                "invalid_semantic_group_assessment"
            )

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "group_id": self.group_id,
            "modality": self.modality,
            "relation_state": self.relation_state,
            "logic": copy.deepcopy(self.logic),
            "branches": [item.as_dict() for item in self.branches],
            "status": self.status,
            "ranking_effect": self.ranking_effect,
            "authority_type": self.authority_type,
            "candidate_exclusion_authorized": self.candidate_exclusion_authorized,
        }


@dataclass(frozen=True, slots=True)
class SemanticSupportingEvidenceV1:
    support_type: str
    semantic_item_ids: tuple[str, ...]
    profile_fact_refs: tuple[str, ...]
    opportunity_evidence_source_ids: tuple[str, ...]
    candidate_explanation: str
    ranking_effect: str = "positive_support"
    authority_type: str = SEMANTIC_AUTHORITY_TYPE
    schema_version: str = SEMANTIC_SUPPORTING_EVIDENCE_SCHEMA_VERSION

    def __post_init__(self):
        if (
            self.schema_version != SEMANTIC_SUPPORTING_EVIDENCE_SCHEMA_VERSION
            or self.support_type not in SUPPORT_TYPES
            or not _sorted_unique_strings(self.semantic_item_ids, allow_empty=False)
            or not _sorted_unique_strings(self.profile_fact_refs, allow_empty=False)
            or any(not _valid_reference(item) for item in self.profile_fact_refs)
            or not _sorted_unique_strings(
                self.opportunity_evidence_source_ids, allow_empty=False
            )
            or not _bounded_candidate_copy(self.candidate_explanation)
            or self.ranking_effect != "positive_support"
            or self.authority_type != SEMANTIC_AUTHORITY_TYPE
        ):
            raise SemanticMatchingShadowContractError(
                "invalid_semantic_supporting_evidence"
            )

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "support_type": self.support_type,
            "semantic_item_ids": list(self.semantic_item_ids),
            "profile_fact_refs": list(self.profile_fact_refs),
            "opportunity_evidence_source_ids": list(
                self.opportunity_evidence_source_ids
            ),
            "candidate_explanation": self.candidate_explanation,
            "ranking_effect": self.ranking_effect,
            "authority_type": self.authority_type,
        }


@dataclass(frozen=True, slots=True)
class SemanticUncertaintyV1:
    reason_code: str
    semantic_item_ids: tuple[str, ...]
    candidate_note: str
    ranking_effect: str = "none"
    schema_version: str = SEMANTIC_UNCERTAINTY_SCHEMA_VERSION

    def __post_init__(self):
        if (
            self.schema_version != SEMANTIC_UNCERTAINTY_SCHEMA_VERSION
            or not _valid_token(self.reason_code)
            or not _sorted_unique_strings(self.semantic_item_ids, allow_empty=True)
            or not _bounded_candidate_copy(self.candidate_note)
            or self.ranking_effect != "none"
        ):
            raise SemanticMatchingShadowContractError(
                "invalid_semantic_uncertainty"
            )

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "reason_code": self.reason_code,
            "semantic_item_ids": list(self.semantic_item_ids),
            "candidate_note": self.candidate_note,
            "ranking_effect": self.ranking_effect,
        }


@dataclass(frozen=True, slots=True)
class SemanticMatchingShadowItemV1:
    opportunity_ref: str
    relative_rank: int
    legacy_rank: int
    selected_variant_ref: str
    deterministic_eligibility: DeterministicEligibilityDecisionV1
    semantic_packet_status: str
    semantic_packet_version: str | None
    semantic_packet_sha256: str | None
    packet_coverage_state: str
    packet_coverage_partial: bool
    supported_required_group_count: int
    supported_preferred_group_count: int
    supported_descriptive_group_count: int
    supported_unassigned_proposition_count: int
    supported_native_signal_count: int
    group_assessments: tuple[SemanticGroupAssessmentV1, ...]
    supporting_evidence: tuple[SemanticSupportingEvidenceV1, ...]
    uncertainties: tuple[SemanticUncertaintyV1, ...]
    grounded_match_explanations: tuple[str, ...]
    semantic_authority: dict
    schema_version: str = SEMANTIC_MATCHING_SHADOW_ITEM_SCHEMA_VERSION

    def __post_init__(self):
        counts = (
            self.supported_required_group_count,
            self.supported_preferred_group_count,
            self.supported_descriptive_group_count,
            self.supported_unassigned_proposition_count,
            self.supported_native_signal_count,
        )
        if (
            self.schema_version != SEMANTIC_MATCHING_SHADOW_ITEM_SCHEMA_VERSION
            or not _valid_reference(self.opportunity_ref)
            or type(self.relative_rank) is not int
            or self.relative_rank <= 0
            or type(self.legacy_rank) is not int
            or self.legacy_rank <= 0
            or not _valid_reference(self.selected_variant_ref)
            or type(self.deterministic_eligibility)
            is not DeterministicEligibilityDecisionV1
            or self.semantic_packet_status not in PACKET_INPUT_STATUSES
            or self.packet_coverage_state not in PACKET_COVERAGE_STATES
            or self.packet_coverage_partial
            is not (self.packet_coverage_state == "available_partial")
            or (self.semantic_packet_status == "available")
            != (self.semantic_packet_version == SEMANTIC_MATCHING_PACKET_VERSION)
            or (self.semantic_packet_status == "available")
            != _valid_sha256(self.semantic_packet_sha256)
            or self.packet_coverage_state.startswith("available_")
            != (self.semantic_packet_status == "available")
            or type(self.group_assessments) is not tuple
            or any(
                type(item) is not SemanticGroupAssessmentV1
                for item in self.group_assessments
            )
            or tuple(item.group_id for item in self.group_assessments)
            != tuple(sorted(item.group_id for item in self.group_assessments))
            or len({item.group_id for item in self.group_assessments})
            != len(self.group_assessments)
            or any(type(value) is not int or value < 0 for value in counts)
            or type(self.supporting_evidence) is not tuple
            or len(self.supporting_evidence) > MAX_SUPPORTING_EVIDENCE_PER_ITEM_V1
            or any(
                type(item) is not SemanticSupportingEvidenceV1
                for item in self.supporting_evidence
            )
            or type(self.uncertainties) is not tuple
            or len(self.uncertainties) > MAX_UNCERTAINTIES_PER_ITEM_V1
            or any(type(item) is not SemanticUncertaintyV1 for item in self.uncertainties)
            or not _sorted_unique_strings(
                self.grounded_match_explanations, allow_empty=True
            )
            or any(
                not _bounded_candidate_copy(item)
                for item in self.grounded_match_explanations
            )
            or self.grounded_match_explanations
            != tuple(sorted({item.candidate_explanation for item in self.supporting_evidence}))
            or self.semantic_authority != _shadow_semantic_authority()
        ):
            raise SemanticMatchingShadowContractError(
                "invalid_semantic_shadow_item"
            )

    @property
    def positive_support_key(self) -> tuple[int, int, int, int, int]:
        return (
            self.supported_required_group_count,
            self.supported_preferred_group_count,
            self.supported_descriptive_group_count,
            self.supported_unassigned_proposition_count,
            self.supported_native_signal_count,
        )

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "opportunity_ref": self.opportunity_ref,
            "relative_rank": self.relative_rank,
            "legacy_rank": self.legacy_rank,
            "selected_variant_ref": self.selected_variant_ref,
            "deterministic_eligibility": self.deterministic_eligibility.as_dict(),
            "semantic_packet": {
                "status": self.semantic_packet_status,
                "packet_version": self.semantic_packet_version,
                "packet_sha256": self.semantic_packet_sha256,
                "coverage_state": self.packet_coverage_state,
                "coverage_partial": self.packet_coverage_partial,
            },
            "positive_support": {
                "required_groups": self.supported_required_group_count,
                "preferred_groups": self.supported_preferred_group_count,
                "descriptive_groups": self.supported_descriptive_group_count,
                "unassigned_propositions": self.supported_unassigned_proposition_count,
                "native_descriptive_signals": self.supported_native_signal_count,
                "negative_factors": [],
            },
            "group_assessments": [item.as_dict() for item in self.group_assessments],
            "supporting_evidence": [item.as_dict() for item in self.supporting_evidence],
            "uncertainties": [item.as_dict() for item in self.uncertainties],
            "grounded_match_explanations": list(self.grounded_match_explanations),
            "semantic_authority": copy.deepcopy(self.semantic_authority),
        }


@dataclass(frozen=True, slots=True)
class SemanticMatchingShadowResultV1:
    request_ref: str
    producer_version: str
    items: tuple[SemanticMatchingShadowItemV1, ...]
    semantic_authority: dict
    invariant_proof: dict
    isolation: dict
    schema_version: str = SEMANTIC_MATCHING_SHADOW_RESULT_SCHEMA_VERSION
    seam_version: str = SEMANTIC_MATCHING_SHADOW_SEAM_VERSION
    execution_mode: str = SHADOW_EXECUTION_MODE

    def __post_init__(self):
        item_refs = tuple(item.opportunity_ref for item in self.items)
        if (
            self.schema_version != SEMANTIC_MATCHING_SHADOW_RESULT_SCHEMA_VERSION
            or self.seam_version != SEMANTIC_MATCHING_SHADOW_SEAM_VERSION
            or self.execution_mode != SHADOW_EXECUTION_MODE
            or not _valid_reference(self.request_ref)
            or self.producer_version != LOCAL_SHADOW_EVALUATOR_VERSION
            or type(self.items) is not tuple
            or len(self.items) > MAX_SHADOW_SHORTLIST_SIZE_V1
            or any(type(item) is not SemanticMatchingShadowItemV1 for item in self.items)
            or len(item_refs) != len(set(item_refs))
            or tuple(item.relative_rank for item in self.items)
            != tuple(range(1, len(self.items) + 1))
            or self.semantic_authority != _shadow_semantic_authority()
            or self.invariant_proof != _result_invariant_proof(len(self.items))
            or self.isolation != _shadow_isolation()
        ):
            raise SemanticMatchingShadowContractError(
                "invalid_semantic_shadow_result"
            )

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "seam_version": self.seam_version,
            "execution_mode": self.execution_mode,
            "request_ref": self.request_ref,
            "producer_version": self.producer_version,
            "semantic_authority": copy.deepcopy(self.semantic_authority),
            "items": [item.as_dict() for item in self.items],
            "invariant_proof": copy.deepcopy(self.invariant_proof),
            "isolation": copy.deepcopy(self.isolation),
        }


def _shadow_semantic_authority() -> dict:
    authority = semantic_non_exclusionary_authority()
    return {
        "authority_policy_version": AUTHORITY_POLICY_VERSION,
        **authority,
        "profile_mutation_authorized": False,
    }


def _shadow_isolation() -> dict:
    return {
        "shadow_only": True,
        "runtime_consumption_authorized": False,
        "find_matches_consumption_authorized": False,
        "candidate_ui_consumption_authorized": False,
        "lifecycle_or_trust_effect_authorized": False,
        "profile_mutation_authorized": False,
        "database_persistence_authorized": False,
        "network_or_model_call_authorized": False,
    }


def _result_invariant_proof(survivor_count: int) -> dict:
    return {
        "input_survivor_count": survivor_count,
        "output_ranked_count": survivor_count,
        "eligibility_survivor_set_preserved": True,
        "deterministic_eligibility_unchanged": True,
        "semantic_exclusion_count": 0,
        "semantic_negative_ranking_factor_count": 0,
    }


def _profile_signal_index(request: SemanticMatchingShadowRequestV1) -> dict[str, tuple]:
    by_kind: dict[str, list[NormalizedProfileSemanticSignalV1]] = {}
    for signal in request.profile_signals:
        by_kind.setdefault(signal.semantic_kind, []).append(signal)
    return {key: tuple(value) for key, value in by_kind.items()}


def _selected_variant_applies(item: dict, selected_variant_ref: str) -> bool:
    applicability = item.get("applicability") or {}
    return selected_variant_ref in (applicability.get("variant_refs") or [])


def _matched_profile_fact_refs(
    proposition: dict,
    profile_signals_by_kind: dict[str, tuple],
) -> tuple[str, ...]:
    meaning = proposition.get("meaning") or {}
    if meaning.get("polarity") != "affirmed":
        return ()
    status = proposition.get("status") or {}
    if status.get("semantic_support") not in {
        "accepted_evidence_validated",
        "supported",
    }:
        return ()
    proposition_terms = _terms_from_value(meaning.get("typed_payload"))
    if not proposition_terms:
        return ()
    matched = {
        signal.profile_fact_ref
        for signal in profile_signals_by_kind.get(meaning.get("kind"), ())
        if proposition_terms.issubset(set(signal.semantic_terms))
    }
    return tuple(sorted(matched))


def _evidence_source_ids(propositions: tuple[dict, ...]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                evidence["source_id"]
                for proposition in propositions
                for evidence in proposition.get("evidence") or []
                if type(evidence) is dict
                and type(evidence.get("source_id")) is str
                and evidence["source_id"]
            }
        )
    )


def _support_explanation(support_type: str) -> str:
    return {
        "required_group": (
            "Confirmed profile evidence supports one complete branch of a stated "
            "required semantic condition."
        ),
        "preferred_group": (
            "Confirmed profile evidence supports one complete branch of a stated "
            "preferred semantic condition."
        ),
        "descriptive_group": (
            "Confirmed profile evidence aligns with a grounded description of the work."
        ),
        "unassigned_proposition": (
            "Confirmed profile evidence aligns with a grounded opportunity proposition "
            "that is not represented in legacy matching fields."
        ),
        "native_descriptive_signal": (
            "Confirmed profile evidence aligns with a grounded responsibility or "
            "candidate-profile signal."
        ),
    }[support_type]


def _coverage_uncertainty(reason_code: str, semantic_item_ids=()) -> SemanticUncertaintyV1:
    note = {
        "semantic_packet_unavailable": (
            "Semantic opportunity details are unavailable, so this opportunity remains "
            "ranked from its existing position without a semantic deduction."
        ),
        "semantic_packet_invalid": (
            "Semantic opportunity details could not be validated, so this opportunity "
            "remains ranked without a semantic deduction."
        ),
        "semantic_packet_has_incomplete_relations": (
            "Some semantic conditions are incomplete or unresolved; they do not count "
            "against this opportunity."
        ),
        "semantic_packet_has_unresolved_propositions": (
            "Some grounded opportunity propositions are unresolved; they do not count "
            "against this opportunity."
        ),
        "semantic_packet_has_unassigned_propositions": (
            "Some grounded propositions are not assigned to a complete condition."
        ),
        "semantic_packet_has_retained_invalid_proposals": (
            "Some source-grounded proposals could not be represented as complete "
            "conditions and remain non-exclusionary."
        ),
        "semantic_packet_has_unresolved_variant_coverage": (
            "Some semantic facts are not established for the selected opportunity "
            "variant and do not count against it."
        ),
        "semantic_packet_has_no_semantic_content": (
            "No semantic opportunity content is available, so the existing ordering is "
            "preserved for this opportunity."
        ),
        "semantic_group_not_established": (
            "The profile does not establish a complete semantic branch; this remains "
            "unknown rather than a mismatch."
        ),
    }.get(
        reason_code,
        "Semantic information is incomplete and does not count against this opportunity.",
    )
    return SemanticUncertaintyV1(
        reason_code=reason_code,
        semantic_item_ids=tuple(sorted(set(semantic_item_ids))),
        candidate_note=note,
    )


def _assess_group(
    group: dict,
    *,
    proposition_index: dict[str, dict],
    selected_variant_ref: str,
    profile_signals_by_kind: dict[str, tuple],
) -> tuple[SemanticGroupAssessmentV1, tuple[str, ...], tuple[dict, ...]]:
    logic = group.get("logic")
    branches = []
    matching_facts: dict[str, tuple[str, ...]] = {}
    if logic is not None:
        for branch_index, raw_branch in enumerate(logic["any_of"], start=1):
            all_of = tuple(raw_branch["all_of"])
            supported = []
            unknown = []
            inapplicable = []
            for proposition_id in all_of:
                proposition = proposition_index.get(proposition_id)
                if proposition is None:
                    unknown.append(proposition_id)
                    continue
                if not _selected_variant_applies(proposition, selected_variant_ref):
                    inapplicable.append(proposition_id)
                    continue
                profile_refs = _matched_profile_fact_refs(
                    proposition, profile_signals_by_kind
                )
                if profile_refs:
                    supported.append(proposition_id)
                    matching_facts[proposition_id] = profile_refs
                else:
                    unknown.append(proposition_id)
            status = (
                "supported"
                if len(supported) == len(all_of)
                else ("partially_supported" if supported else "unknown")
            )
            branches.append(
                SemanticBranchAssessmentV1(
                    branch_index=branch_index,
                    all_of=all_of,
                    supported_proposition_ids=tuple(sorted(supported)),
                    unknown_proposition_ids=tuple(sorted(unknown)),
                    variant_inapplicable_proposition_ids=tuple(sorted(inapplicable)),
                    status=status,
                )
            )
    supported_branch = next(
        (item for item in branches if item.status == "supported"), None
    )
    group_status = (
        "supported"
        if supported_branch is not None
        else (
            "partially_supported"
            if any(item.status == "partially_supported" for item in branches)
            else "unknown"
        )
    )
    assessment = SemanticGroupAssessmentV1(
        group_id=group["group_id"],
        modality=group["modality"],
        relation_state=group["relation_state"],
        logic=copy.deepcopy(logic),
        branches=tuple(branches),
        status=group_status,
        ranking_effect="positive_support" if group_status == "supported" else "none",
    )
    if supported_branch is None:
        return assessment, (), ()
    proposition_ids = tuple(sorted(supported_branch.all_of))
    profile_refs = tuple(
        sorted(
            {
                profile_ref
                for proposition_id in proposition_ids
                for profile_ref in matching_facts[proposition_id]
            }
        )
    )
    propositions = tuple(proposition_index[item] for item in proposition_ids)
    return assessment, profile_refs, propositions


def _packet_coverage_reasons(packet: dict, selected_variant_ref: str) -> tuple[str, ...]:
    reasons = set()
    accounting = packet["accounting"]
    if accounting["incomplete_or_unresolved_group_count"]:
        reasons.add("semantic_packet_has_incomplete_relations")
    if any(
        proposition["status"]["completeness"] != "complete"
        or proposition["normalization"]["status"] != "resolved"
        or proposition["normalization"]["qualifier_complete"] is not True
        for proposition in packet["propositions"]
    ):
        reasons.add("semantic_packet_has_unresolved_propositions")
    if packet["unassigned_proposition_ids"]:
        reasons.add("semantic_packet_has_unassigned_propositions")
    if packet["retained_invalid_proposals"]:
        reasons.add("semantic_packet_has_retained_invalid_proposals")
    semantic_items = [
        *packet["propositions"],
        *packet["groups"],
        *packet["descriptive_signals"],
        *packet["retained_invalid_proposals"],
    ]
    if any(
        not _selected_variant_applies(item, selected_variant_ref)
        for item in semantic_items
    ):
        reasons.add("semantic_packet_has_unresolved_variant_coverage")
    if not packet["propositions"] and not packet["descriptive_signals"]:
        reasons.add("semantic_packet_has_no_semantic_content")
    return tuple(sorted(reasons))


def _evaluate_available_candidate(
    candidate: SemanticMatchingShadowCandidateV1,
    profile_signals_by_kind: dict[str, tuple],
) -> SemanticMatchingShadowItemV1:
    packet = candidate.semantic_packet.packet
    proposition_index = {
        item["proposition_id"]: item for item in packet["propositions"]
    }
    assessments = []
    evidence = []
    uncertainties = []
    supported_by_modality = {modality: 0 for modality in MODALITIES}

    for group in packet["groups"]:
        assessment, profile_refs, propositions = _assess_group(
            group,
            proposition_index=proposition_index,
            selected_variant_ref=candidate.selected_variant_ref,
            profile_signals_by_kind=profile_signals_by_kind,
        )
        assessments.append(assessment)
        if assessment.status == "supported":
            if group["modality"] not in MODALITIES:
                raise SemanticMatchingShadowContractError(
                    "semantic_group_support_missing_modality"
                )
            supported_by_modality[group["modality"]] += 1
            support_type = f"{group['modality']}_group"
            evidence.append(
                SemanticSupportingEvidenceV1(
                    support_type=support_type,
                    semantic_item_ids=(group["group_id"],),
                    profile_fact_refs=profile_refs,
                    opportunity_evidence_source_ids=_evidence_source_ids(propositions),
                    candidate_explanation=_support_explanation(support_type),
                )
            )
        elif group["modality"] in {"required", "preferred"}:
            uncertainties.append(
                _coverage_uncertainty(
                    "semantic_group_not_established", (group["group_id"],)
                )
            )

    supported_unassigned = 0
    for proposition_id in packet["unassigned_proposition_ids"]:
        proposition = proposition_index.get(proposition_id)
        if proposition is None or not _selected_variant_applies(
            proposition, candidate.selected_variant_ref
        ):
            continue
        profile_refs = _matched_profile_fact_refs(
            proposition, profile_signals_by_kind
        )
        if not profile_refs:
            continue
        supported_unassigned += 1
        evidence.append(
            SemanticSupportingEvidenceV1(
                support_type="unassigned_proposition",
                semantic_item_ids=(proposition_id,),
                profile_fact_refs=profile_refs,
                opportunity_evidence_source_ids=_evidence_source_ids((proposition,)),
                candidate_explanation=_support_explanation(
                    "unassigned_proposition"
                ),
            )
        )

    supported_native = 0
    descriptive_profile_signals = profile_signals_by_kind.get("descriptive", ())
    for signal in packet["descriptive_signals"]:
        if not _selected_variant_applies(signal, candidate.selected_variant_ref):
            continue
        opportunity_terms = _words_from_text(signal["value"])
        profile_refs = tuple(
            sorted(
                {
                    item.profile_fact_ref
                    for item in descriptive_profile_signals
                    if set(item.semantic_terms).issubset(opportunity_terms)
                }
            )
        )
        if not profile_refs:
            continue
        supported_native += 1
        evidence.append(
            SemanticSupportingEvidenceV1(
                support_type="native_descriptive_signal",
                semantic_item_ids=(signal["signal_id"],),
                profile_fact_refs=profile_refs,
                opportunity_evidence_source_ids=tuple(signal["evidence_source_ids"]),
                candidate_explanation=_support_explanation(
                    "native_descriptive_signal"
                ),
            )
        )

    coverage_reasons = _packet_coverage_reasons(
        packet, candidate.selected_variant_ref
    )
    uncertainties.extend(_coverage_uncertainty(item) for item in coverage_reasons)
    evidence = evidence[:MAX_SUPPORTING_EVIDENCE_PER_ITEM_V1]
    unique_uncertainties = {}
    for item in uncertainties:
        key = (item.reason_code, item.semantic_item_ids)
        unique_uncertainties[key] = item
    uncertainties = [
        unique_uncertainties[key]
        for key in sorted(unique_uncertainties)
    ][:MAX_UNCERTAINTIES_PER_ITEM_V1]
    coverage_state = (
        "available_partial" if coverage_reasons else "available_complete"
    )
    return SemanticMatchingShadowItemV1(
        opportunity_ref=candidate.opportunity_ref,
        relative_rank=candidate.legacy_rank,
        legacy_rank=candidate.legacy_rank,
        selected_variant_ref=candidate.selected_variant_ref,
        deterministic_eligibility=candidate.shortlist_candidate.eligibility,
        semantic_packet_status="available",
        semantic_packet_version=packet["packet_version"],
        semantic_packet_sha256=packet["packet_sha256"],
        packet_coverage_state=coverage_state,
        packet_coverage_partial=coverage_state == "available_partial",
        supported_required_group_count=supported_by_modality["required"],
        supported_preferred_group_count=supported_by_modality["preferred"],
        supported_descriptive_group_count=supported_by_modality["descriptive"],
        supported_unassigned_proposition_count=supported_unassigned,
        supported_native_signal_count=supported_native,
        group_assessments=tuple(sorted(assessments, key=lambda item: item.group_id)),
        supporting_evidence=tuple(evidence),
        uncertainties=tuple(uncertainties),
        grounded_match_explanations=tuple(
            sorted({item.candidate_explanation for item in evidence})
        ),
        semantic_authority=_shadow_semantic_authority(),
    )


def _evaluate_missing_candidate(
    candidate: SemanticMatchingShadowCandidateV1,
) -> SemanticMatchingShadowItemV1:
    status = candidate.semantic_packet.status
    reason_codes = candidate.semantic_packet.reason_codes
    uncertainties = tuple(
        _coverage_uncertainty(
            reason_code
            if reason_code in {"semantic_packet_unavailable", "semantic_packet_invalid"}
            else status == "invalid"
            and "semantic_packet_invalid"
            or "semantic_packet_unavailable"
        )
        for reason_code in reason_codes[:MAX_UNCERTAINTIES_PER_ITEM_V1]
    )
    return SemanticMatchingShadowItemV1(
        opportunity_ref=candidate.opportunity_ref,
        relative_rank=candidate.legacy_rank,
        legacy_rank=candidate.legacy_rank,
        selected_variant_ref=candidate.selected_variant_ref,
        deterministic_eligibility=candidate.shortlist_candidate.eligibility,
        semantic_packet_status=status,
        semantic_packet_version=None,
        semantic_packet_sha256=None,
        packet_coverage_state=status,
        packet_coverage_partial=False,
        supported_required_group_count=0,
        supported_preferred_group_count=0,
        supported_descriptive_group_count=0,
        supported_unassigned_proposition_count=0,
        supported_native_signal_count=0,
        group_assessments=(),
        supporting_evidence=(),
        uncertainties=uncertainties,
        grounded_match_explanations=(),
        semantic_authority=_shadow_semantic_authority(),
    )


def run_semantic_matching_shadow_v1(
    request: SemanticMatchingShadowRequestV1,
) -> SemanticMatchingShadowResultV1:
    """Rank every survivor with positive-only local semantic support.

    The candidate set is closed before this function runs.  Packet coverage,
    unknown group states, and variant-inapplicable facts are not sort keys.
    Therefore missing semantic information cannot create a negative adjustment.
    """

    if type(request) is not SemanticMatchingShadowRequestV1:
        raise SemanticMatchingShadowContractError(
            "invalid_semantic_shadow_request"
        )
    profile_signals_by_kind = _profile_signal_index(request)
    evaluated = []
    for candidate in request.candidates:
        if candidate.semantic_packet.status == "available":
            # Packet dictionaries come from an external contract and remain
            # mutable Python objects even inside a frozen dataclass. Re-adapt
            # and revalidate at the execution boundary so post-construction
            # mutation becomes a neutral invalid-packet state, never omission.
            execution_candidate = semantic_shadow_candidate_from_packet_v1(
                shortlist_candidate=candidate.shortlist_candidate,
                legacy_rank=candidate.legacy_rank,
                selected_variant_ref=candidate.selected_variant_ref,
                packet=candidate.semantic_packet.packet,
            )
            if execution_candidate.semantic_packet.status == "available":
                item = _evaluate_available_candidate(
                    execution_candidate, profile_signals_by_kind
                )
            else:
                item = _evaluate_missing_candidate(execution_candidate)
        else:
            item = _evaluate_missing_candidate(candidate)
        evaluated.append(item)

    evaluated.sort(
        key=lambda item: (
            *(-value for value in item.positive_support_key),
            item.legacy_rank,
            item.opportunity_ref,
        )
    )
    ranked = tuple(
        replace(item, relative_rank=rank)
        for rank, item in enumerate(evaluated, start=1)
    )
    result = SemanticMatchingShadowResultV1(
        request_ref=request.request_ref,
        producer_version=LOCAL_SHADOW_EVALUATOR_VERSION,
        items=ranked,
        semantic_authority=_shadow_semantic_authority(),
        invariant_proof=_result_invariant_proof(len(request.candidates)),
        isolation=_shadow_isolation(),
    )
    validate_semantic_matching_shadow_result_v1(request, result)
    return result


def validate_semantic_matching_shadow_result_v1(
    request: SemanticMatchingShadowRequestV1,
    result: SemanticMatchingShadowResultV1,
) -> None:
    """Prove survivor preservation and an unchanged eligibility boundary."""

    if (
        type(request) is not SemanticMatchingShadowRequestV1
        or type(result) is not SemanticMatchingShadowResultV1
        or result.request_ref != request.request_ref
    ):
        raise SemanticMatchingShadowContractError(
            "semantic_shadow_request_result_mismatch"
        )
    candidates = {item.opportunity_ref: item for item in request.candidates}
    items = {item.opportunity_ref: item for item in result.items}
    if set(candidates) != set(items):
        raise SemanticMatchingShadowContractError(
            "semantic_shadow_survivor_set_changed"
        )
    for opportunity_ref, candidate in candidates.items():
        item = items[opportunity_ref]
        if (
            item.deterministic_eligibility
            is not candidate.shortlist_candidate.eligibility
            or contract_fingerprint(item.deterministic_eligibility)
            != contract_fingerprint(candidate.shortlist_candidate.eligibility)
            or item.semantic_authority["candidate_exclusion_authorized"] is not False
            or item.semantic_authority["hard_eligibility_authorized"] is not False
            or item.semantic_authority["deterministic_failure_authorized"] is not False
        ):
            raise SemanticMatchingShadowContractError(
                "semantic_shadow_eligibility_boundary_changed"
            )


__all__ = [
    "LOCAL_SHADOW_EVALUATOR_VERSION",
    "MAX_SHADOW_SHORTLIST_SIZE_V1",
    "NORMALIZED_PROFILE_SIGNAL_SCHEMA_VERSION",
    "NormalizedProfileSemanticSignalV1",
    "SEMANTIC_MATCHING_SHADOW_REQUEST_SCHEMA_VERSION",
    "SEMANTIC_MATCHING_SHADOW_RESULT_SCHEMA_VERSION",
    "SEMANTIC_MATCHING_SHADOW_SEAM_VERSION",
    "SEMANTIC_PACKET_INPUT_SCHEMA_VERSION",
    "SHADOW_EXECUTION_MODE",
    "SemanticMatchingShadowCandidateV1",
    "SemanticMatchingShadowContractError",
    "SemanticMatchingShadowRequestV1",
    "SemanticMatchingShadowResultV1",
    "SemanticPacketInputV1",
    "normalize_semantic_term",
    "run_semantic_matching_shadow_v1",
    "semantic_shadow_candidate_from_packet_v1",
    "validate_semantic_matching_shadow_result_v1",
]

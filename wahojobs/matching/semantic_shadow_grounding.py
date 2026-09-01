"""Closed reference and structured-grounding contract for shadow matching.

This module is evaluation infrastructure only.  It is not imported by the
runtime matcher or UI.  It converts minimized profile facts and authenticated
semantic packets into request-local opaque catalogs, builds a strict Responses
API output schema, and validates the returned structured findings without
allowing missingness, unrelated semantic dimensions, or packet incompleteness
to become negative candidate facts.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from wahojobs.opportunity_semantic_authority import (
    semantic_non_exclusionary_authority,
    validate_semantic_matching_packet,
)


SEMANTIC_SHADOW_GROUNDING_REQUEST_VERSION = (
    "wahojobs_semantic_shadow_grounding_request_v1"
)
SEMANTIC_SHADOW_GROUNDING_OUTPUT_VERSION = (
    "wahojobs_semantic_shadow_grounding_output_v1"
)
SEMANTIC_SHADOW_REFERENCE_CATALOG_VERSION = (
    "wahojobs_semantic_shadow_reference_catalog_v1"
)

PROFILE_REFERENCE_STATES = frozenset(
    {"grounded", "not_grounded", "not_specified", "unknown", "unavailable"}
)
OPPORTUNITY_REFERENCE_STATES = frozenset(
    {"grounded", "incomplete", "unresolved", "unavailable", "variant_scoped"}
)
UNCERTAINTY_STATES = frozenset(
    {
        "not_grounded",
        "not_specified",
        "unknown",
        "unresolved",
        "unavailable",
        "incomplete",
        "variant_scoped",
    }
)
FINDING_TYPES = frozenset(
    {"direct_alignment", "partial_alignment", "context_only"}
)
RELATIONSHIP_TYPES = frozenset(
    {
        "education_alignment",
        "language_alignment",
        "capability_alignment",
        "experience_alignment",
        "role_activity_alignment",
        "work_condition_alignment",
        "domain_context",
    }
)
ASSESSMENT_STATES = frozenset(
    {
        "grounded_alignment",
        "limited_grounded_alignment",
        "unresolved_only",
        "no_grounded_alignment",
    }
)

PROFILE_DIMENSIONS = {
    "education": "education",
    "languages": "language",
    "skills": "capability",
    "experience": "experience",
    "prior_roles": "experience",
    "domains": "domain",
    "target_work": "role_activity",
    "work_conditions": "work_condition",
    "location": "location",
    "work_authorization": "work_authorization",
    "experience_years": "experience",
    "language_locale_or_variant": "language",
}
OPPORTUNITY_DIMENSIONS = {
    "education": "education",
    "locale_dialect_expertise": "language",
    "language_proficiency": "language",
    "capability": "capability",
    "experience": "experience",
    "role_activity": "role_activity",
    "work_condition": "work_condition",
    "asset_access": "asset_access",
}
RELATIONSHIP_DIMENSIONS = {
    "education_alignment": ({"education"}, {"education"}),
    "language_alignment": ({"language"}, {"language"}),
    "capability_alignment": ({"capability"}, {"capability"}),
    "experience_alignment": ({"experience"}, {"experience"}),
    "role_activity_alignment": ({"role_activity"}, {"role_activity"}),
    "work_condition_alignment": ({"work_condition"}, {"work_condition"}),
    "domain_context": ({"domain"}, set(OPPORTUNITY_DIMENSIONS.values())),
}

GROUNDING_INSTRUCTIONS = """You are performing a bounded, shadow-only, non-exclusionary semantic comparison.
Use only the supplied request-local opaque IDs and their immutable structured meanings.
Cite P###, O###, and J### IDs exactly as supplied. Never construct paths, hashes, composite references, or new IDs.
Every finding must use the typed fields profile_reference_ids, proposition_reference_ids, group_reference_ids, evidence_reference_ids, and scope_reference_ids exactly as supplied. A finding always requires at least one proposition_reference_id and every group named by that proposition. Evidence and scope references may corroborate or qualify a finding but can never replace proposition/group grounding.
Context-only findings have ranking_effect none. Education alignment may use only profile education facts and opportunity education propositions. Language or domain facts cannot establish education. Domain context cannot establish degree attainment. Incomplete or unresolved opportunity material can only support partial_alignment and must also be cited by a matching non-negative uncertainty.
The states not_grounded, not_specified, unknown, unresolved, and unavailable never assert factual absence. Do not infer candidate lacks X, candidate has no X, or opportunity does not require X from any missing or unresolved state. Every uncertainty state must exactly match at least one cited catalog entry's state.
Context-only findings always have ranking_effect none and obey the same proposition/group grounding rule. Required is semantic modality only and never exclusion authority. Unknown or missing information is never negative fit. Preserve all supplied opportunities exactly once. In relative reranking, every opportunity with zero positive_support findings must be tied in the final ordering group. In single_opportunity_assessment mode, relative_ordering_groups must be empty. Produce no scores, buckets, eligibility decisions, or free-form evidentiary claims.
Return only the strict structured output."""


class SemanticShadowGroundingContractError(ValueError):
    """A closed-contract failure that does not echo profile or packet data."""

    def __init__(self, *reason_codes: str):
        self.reason_codes = tuple(sorted(set(reason_codes))) or (
            "invalid_semantic_shadow_grounding_contract",
        )
        super().__init__(
            "semantic shadow grounding contract rejected; reason_codes="
            + ",".join(self.reason_codes)
        )


@dataclass(frozen=True, slots=True)
class SemanticShadowGroundingRequestV1:
    provider_input: dict
    local_catalog: dict


def _state_from_value(value) -> tuple[str, object | None]:
    if type(value) is str and value in {
        "not_grounded",
        "not_specified",
        "unknown",
        "unavailable",
    }:
        return value, None
    return "grounded", copy.deepcopy(value)


def _profile_catalog(profile: Mapping) -> tuple[list[dict], dict[str, dict]]:
    if type(profile) is not dict or type(profile.get("facts")) is not list:
        raise SemanticShadowGroundingContractError("invalid_minimized_profile")
    entries: list[dict] = []
    local: dict[str, dict] = {}

    def add(category: str, value, *, original_ref: str | None, state=None):
        ref_id = f"P{len(entries) + 1:03d}"
        resolved_state, resolved_value = _state_from_value(value)
        if state is not None:
            resolved_state = state
            resolved_value = None if state != "grounded" else copy.deepcopy(value)
        if resolved_state not in PROFILE_REFERENCE_STATES:
            raise SemanticShadowGroundingContractError(
                "invalid_profile_reference_state"
            )
        entry = {
            "reference_id": ref_id,
            "reference_type": "profile_fact",
            "semantic_dimension": PROFILE_DIMENSIONS.get(category, "other"),
            "category": category,
            "state": resolved_state,
            "value": resolved_value,
        }
        entries.append(entry)
        local[ref_id] = {
            **copy.deepcopy(entry),
            "original_profile_fact_ref": original_ref,
        }

    for fact in profile["facts"]:
        if type(fact) is not dict or type(fact.get("category")) is not str:
            raise SemanticShadowGroundingContractError("invalid_profile_fact")
        values = fact.get("value")
        atomic_values = values if type(values) is list else [values]
        if not atomic_values:
            atomic_values = ["unavailable"]
        for value in atomic_values:
            add(
                fact["category"],
                value,
                original_ref=fact.get("fact_ref"),
            )
    limitations = profile.get("grounding_limitations", [])
    if type(limitations) is not list:
        raise SemanticShadowGroundingContractError(
            "invalid_profile_grounding_limitations"
        )
    for limitation in limitations:
        if (
            type(limitation) is not dict
            or type(limitation.get("category")) is not str
            or limitation.get("status") not in PROFILE_REFERENCE_STATES
        ):
            raise SemanticShadowGroundingContractError(
                "invalid_profile_grounding_limitation"
            )
        add(
            limitation["category"],
            None,
            original_ref=None,
            state=limitation["status"],
        )
    return entries, local


def _opportunity_state(status: Mapping) -> str:
    if type(status) is not dict:
        return "unresolved"
    completeness = status.get("completeness")
    support = status.get("semantic_support")
    if completeness in {"unresolved", "incomplete"} or support == "pending":
        return "unresolved"
    return "grounded"


def _packet_catalog(
    packet: Mapping,
    opportunity_id: str,
    next_ordinal: int,
) -> tuple[list[dict], dict[str, dict], int]:
    entries: list[dict] = []
    local: dict[str, dict] = {}
    proposition_ids: dict[str, str] = {}
    evidence_ids: dict[str, str] = {}
    variant_ids: dict[str, str] = {}
    proposition_group_ids: dict[str, list[str]] = {}

    def add(reference_type: str, state: str, dimension: str, meaning: dict, internal):
        nonlocal next_ordinal
        ref_id = f"O{next_ordinal:03d}"
        next_ordinal += 1
        entry = {
            "reference_id": ref_id,
            "reference_type": reference_type,
            "opportunity_id": opportunity_id,
            "semantic_dimension": dimension,
            "state": state,
            "meaning": copy.deepcopy(meaning),
        }
        entries.append(entry)
        local[ref_id] = {**copy.deepcopy(entry), "internal_reference": internal}
        return ref_id

    for variant_ref in packet["opportunity_scope"]["known_variant_refs"]:
        variant_ids[variant_ref] = add(
            "variant",
            "variant_scoped",
            "variant_scope",
            {"scope": "known_opportunity_variant"},
            variant_ref,
        )
    for evidence in packet["evidence_sources"]:
        evidence_ids[evidence["id"]] = add(
            "evidence",
            "grounded",
            "evidence",
            {"text": evidence["text"], "authority": evidence["authority"]},
            evidence["id"],
        )
    for proposition in packet["propositions"]:
        meaning = proposition["meaning"]
        raw_values = []
        for source_value in proposition.get("normalization", {}).get(
            "source_values", []
        ):
            raw_values.append(
                {
                    "field": source_value.get("field"),
                    "raw_value": source_value.get("raw_value"),
                    "normalization_status": source_value.get(
                        "normalization_status"
                    ),
                    "normalized_value": source_value.get("normalized_value"),
                }
            )
        proposition_ids[proposition["proposition_id"]] = add(
            "proposition",
            _opportunity_state(proposition.get("status", {})),
            OPPORTUNITY_DIMENSIONS.get(meaning["kind"], "other"),
            {
                "semantic_kind": meaning["kind"],
                "polarity": meaning["polarity"],
                "subject": meaning["subject"],
                "temporal": meaning["temporal"],
                "typed_payload": copy.deepcopy(meaning["typed_payload"]),
                "normalized_value": proposition.get("normalized_value"),
                "normalization_status": proposition.get("normalization", {}).get(
                    "status"
                ),
                "qualifier_complete": proposition.get("normalization", {}).get(
                    "qualifier_complete"
                ),
                "raw_values": raw_values,
                "evidence_reference_ids": [
                    evidence_ids[item["source_id"]]
                    for item in proposition.get("evidence", [])
                    if item.get("source_id") in evidence_ids
                ],
                "variant_reference_ids": [
                    variant_ids[item]
                    for item in proposition["applicability"]["variant_refs"]
                ],
            },
            proposition["proposition_id"],
        )
    for group in packet["groups"]:
        group_ref_id = add(
            "group",
            "incomplete"
            if group["completeness"] != "complete"
            else "grounded",
            "relation",
            {
                "modality": group["modality"],
                "completeness": group["completeness"],
                "logic": {
                    "form": group["logic"]["form"],
                    "any_of": [
                        {
                            "all_of": [
                                proposition_ids[item]
                                for item in branch["all_of"]
                            ]
                        }
                        for branch in group["logic"]["any_of"]
                    ],
                },
                "proposition_reference_ids": [
                    proposition_ids[item]
                    for item in group["referenced_proposition_ids"]
                ],
                "variant_reference_ids": [
                    variant_ids[item]
                    for item in group["applicability"]["variant_refs"]
                ],
                "reason_codes": copy.deepcopy(group["reason_codes"]),
            },
            group["group_id"],
        )
        for proposition_id in group["referenced_proposition_ids"]:
            proposition_group_ids.setdefault(proposition_id, []).append(group_ref_id)
    entries_by_id = {item["reference_id"]: item for item in entries}
    for proposition_id, proposition_ref_id in proposition_ids.items():
        group_reference_ids = proposition_group_ids.get(proposition_id, [])
        entries_by_id[proposition_ref_id]["meaning"]["group_reference_ids"] = (
            group_reference_ids
        )
        local[proposition_ref_id]["meaning"]["group_reference_ids"] = copy.deepcopy(
            group_reference_ids
        )
    return entries, local, next_ordinal


def build_semantic_shadow_grounding_request_v1(
    *,
    profile: Mapping,
    packets: Sequence[Mapping],
    evaluation_mode: str,
) -> SemanticShadowGroundingRequestV1:
    """Build one blinded provider payload plus its non-provider local map."""

    if evaluation_mode not in {
        "relative_reranking",
        "single_opportunity_assessment",
    }:
        raise SemanticShadowGroundingContractError("invalid_evaluation_mode")
    if not packets:
        raise SemanticShadowGroundingContractError("empty_packet_population")

    profile_entries, local_profile = _profile_catalog(profile)
    provider_opportunities = []
    local_opportunities = {}
    next_ordinal = 1
    seen_internal_refs = set()
    for ordinal, original_packet in enumerate(packets, start=1):
        packet = copy.deepcopy(original_packet)
        try:
            validate_semantic_matching_packet(packet)
        except Exception as exc:
            raise SemanticShadowGroundingContractError(
                "invalid_semantic_packet"
            ) from exc
        internal_ref = packet["opportunity_scope"]["canonical_ref"]
        if internal_ref in seen_internal_refs:
            raise SemanticShadowGroundingContractError(
                "duplicate_opportunity_packet"
            )
        seen_internal_refs.add(internal_ref)
        opportunity_id = f"J{ordinal:03d}"
        entries, local_entries, next_ordinal = _packet_catalog(
            packet, opportunity_id, next_ordinal
        )
        provider_opportunities.append(
            {
                "opportunity_id": opportunity_id,
                "authority": {
                    "authority_type": packet["authority"]["authority_type"],
                    "candidate_exclusion_authorized": False,
                    "hard_eligibility_authorized": False,
                    "canonical_fact_promotion_allowed": packet[
                        "opportunity_scope"
                    ]["canonical_fact_promotion_allowed"],
                },
                "reference_catalog": entries,
            }
        )
        local_opportunities[opportunity_id] = {
            "internal_opportunity_ref": internal_ref,
            "authenticated_packet_sha256": packet["packet_sha256"],
            "references": local_entries,
        }
    provider_input = {
        "contract_version": SEMANTIC_SHADOW_GROUNDING_REQUEST_VERSION,
        "reference_catalog_version": SEMANTIC_SHADOW_REFERENCE_CATALOG_VERSION,
        "evaluation_mode": evaluation_mode,
        "missingness_semantics": {
            "states": sorted(UNCERTAINTY_STATES),
            "states_are_factual_absence": False,
            "states_are_negative_fit": False,
        },
        "profile_reference_catalog": profile_entries,
        "opportunities": provider_opportunities,
    }
    local_catalog = {
        "contract_version": SEMANTIC_SHADOW_REFERENCE_CATALOG_VERSION,
        "profile_references": local_profile,
        "opportunities": local_opportunities,
    }
    return SemanticShadowGroundingRequestV1(
        provider_input=provider_input,
        local_catalog=local_catalog,
    )


def _string_array_schema(values: Iterable[str], *, min_items: int = 0) -> dict:
    values = sorted(values)
    schema = {
        "type": "array",
        "items": {"type": "string"},
        "minItems": min_items,
    }
    if values:
        schema["items"]["enum"] = values
    else:
        schema["maxItems"] = 0
    return schema


def build_semantic_shadow_grounding_output_schema_v1(
    provider_input: Mapping,
) -> dict:
    """Build a strict role-aware schema closed to this exact request."""

    profile_catalog = provider_input["profile_reference_catalog"]
    opportunity_ids = [
        item["opportunity_id"] for item in provider_input["opportunities"]
    ]
    grounded_profile_by_dimension = {
        dimension: [
            item["reference_id"]
            for item in profile_catalog
            if item["state"] == "grounded"
            and item["semantic_dimension"] == dimension
        ]
        for dimension in set(PROFILE_DIMENSIONS.values()) | {"other"}
    }

    def finding_variant(
        *,
        finding_type: str,
        relationship_type: str,
        ranking_effect: str,
        allowed_profile_ids: list[str],
        proposition_id: str,
        group_ids: list[str],
        evidence_ids: list[str],
        scope_ids: list[str],
    ) -> dict:
        group_schema = _string_array_schema(
            group_ids, min_items=len(group_ids)
        )
        group_schema["maxItems"] = len(group_ids)
        properties = {
            "finding_type": {"type": "string", "const": finding_type},
            "relationship_type": {
                "type": "string",
                "const": relationship_type,
            },
            "ranking_effect": {"type": "string", "const": ranking_effect},
            "profile_reference_ids": _string_array_schema(
                allowed_profile_ids, min_items=1
            ),
            "proposition_reference_ids": _string_array_schema(
                [proposition_id], min_items=1
            ),
            "group_reference_ids": group_schema,
            "evidence_reference_ids": _string_array_schema(evidence_ids),
            "scope_reference_ids": _string_array_schema(scope_ids),
        }
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": list(properties),
        }

    def assessment_schema(opportunity: Mapping) -> dict:
        opportunity_id = opportunity["opportunity_id"]
        catalog = opportunity["reference_catalog"]
        by_id = {item["reference_id"]: item for item in catalog}
        propositions = [
            item for item in catalog if item["reference_type"] == "proposition"
        ]
        group_ids = [
            item["reference_id"]
            for item in catalog
            if item["reference_type"] == "group"
        ]
        evidence_ids = [
            item["reference_id"]
            for item in catalog
            if item["reference_type"] == "evidence"
        ]
        scope_ids = [
            item["reference_id"]
            for item in catalog
            if item["reference_type"] == "variant"
        ]
        direct_proposition_ids = {
            item["reference_id"]
            for item in propositions
            if item["state"] == "grounded"
            and all(
                by_id[group_ref]["state"] == "grounded"
                for group_ref in item["meaning"].get(
                    "group_reference_ids", []
                )
            )
        }
        finding_variants = []
        for relationship_type, (
            allowed_profile_dimensions,
            allowed_opportunity_dimensions,
        ) in RELATIONSHIP_DIMENSIONS.items():
            allowed_profile_ids = sorted(
                {
                    ref_id
                    for dimension in allowed_profile_dimensions
                    for ref_id in grounded_profile_by_dimension.get(dimension, [])
                }
            )
            allowed_propositions = [
                item
                for item in propositions
                if item["semantic_dimension"] in allowed_opportunity_dimensions
            ]
            if not allowed_profile_ids or not allowed_propositions:
                continue
            for proposition in allowed_propositions:
                proposition_id = proposition["reference_id"]
                required_group_ids = proposition["meaning"].get(
                    "group_reference_ids", []
                )
                if relationship_type == "domain_context":
                    finding_variants.append(
                        finding_variant(
                            finding_type="context_only",
                            relationship_type=relationship_type,
                            ranking_effect="none",
                            allowed_profile_ids=allowed_profile_ids,
                            proposition_id=proposition_id,
                            group_ids=required_group_ids,
                            evidence_ids=evidence_ids,
                            scope_ids=scope_ids,
                        )
                    )
                    continue
                finding_variants.append(
                    finding_variant(
                        finding_type="partial_alignment",
                        relationship_type=relationship_type,
                        ranking_effect="positive_support",
                        allowed_profile_ids=allowed_profile_ids,
                        proposition_id=proposition_id,
                        group_ids=required_group_ids,
                        evidence_ids=evidence_ids,
                        scope_ids=scope_ids,
                    )
                )
                if proposition_id in direct_proposition_ids:
                    finding_variants.append(
                        finding_variant(
                            finding_type="direct_alignment",
                            relationship_type=relationship_type,
                            ranking_effect="positive_support",
                            allowed_profile_ids=allowed_profile_ids,
                            proposition_id=proposition_id,
                            group_ids=required_group_ids,
                            evidence_ids=evidence_ids,
                            scope_ids=scope_ids,
                        )
                    )
        uncertainty_role_catalogs = {
            "profile_reference_ids": profile_catalog,
            "proposition_reference_ids": propositions,
            "group_reference_ids": [
                item for item in catalog if item["reference_type"] == "group"
            ],
            "evidence_reference_ids": [
                item for item in catalog if item["reference_type"] == "evidence"
            ],
            "scope_reference_ids": [
                item for item in catalog if item["reference_type"] == "variant"
            ],
        }
        uncertainty_variants = []
        for state in sorted(UNCERTAINTY_STATES):
            for anchor_field, anchor_catalog in uncertainty_role_catalogs.items():
                matching_anchor_ids = [
                    item["reference_id"]
                    for item in anchor_catalog
                    if item["state"] == state
                ]
                if not matching_anchor_ids:
                    continue
                uncertainty_properties = {
                    "state": {"type": "string", "const": state},
                    "effect": {"type": "string", "const": "non_negative"},
                }
                for field, role_catalog in uncertainty_role_catalogs.items():
                    uncertainty_properties[field] = _string_array_schema(
                        matching_anchor_ids
                        if field == anchor_field
                        else [item["reference_id"] for item in role_catalog],
                        min_items=1 if field == anchor_field else 0,
                    )
                uncertainty_variants.append(
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": uncertainty_properties,
                        "required": list(uncertainty_properties),
                    }
                )
        properties = {
            "opportunity_id": {"type": "string", "const": opportunity_id},
            "assessment_state": {
                "type": "string",
                "enum": sorted(ASSESSMENT_STATES),
            },
            "findings": {
                "type": "array",
                "items": {"anyOf": finding_variants},
            }
            if finding_variants
            else {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {},
                    "required": [],
                },
                "maxItems": 0,
            },
            "uncertainties": {
                "type": "array",
                "items": {"anyOf": uncertainty_variants},
            }
            if uncertainty_variants
            else {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {},
                    "required": [],
                },
                "maxItems": 0,
            },
        }
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": list(properties),
        }

    assessment_properties = {
        item["opportunity_id"]: assessment_schema(item)
        for item in provider_input["opportunities"]
    }
    relative_mode = provider_input["evaluation_mode"] == "relative_reranking"
    ordering_schema = {
        "type": "array",
        "items": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "opportunity_ids": _string_array_schema(
                    opportunity_ids, min_items=1
                )
            },
            "required": ["opportunity_ids"],
        },
        "minItems": 1 if relative_mode else 0,
    }
    if not relative_mode:
        ordering_schema["maxItems"] = 0
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "contract_version": {
                "type": "string",
                "const": SEMANTIC_SHADOW_GROUNDING_OUTPUT_VERSION,
            },
            "evaluation_mode": {
                "type": "string",
                "const": provider_input["evaluation_mode"],
            },
            "relative_ordering_groups": ordering_schema,
            "opportunity_assessments": {
                "type": "object",
                "additionalProperties": False,
                "properties": assessment_properties,
                "required": list(assessment_properties),
            },
            "authority_confirmation": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "all_supplied_opportunities_retained": {
                        "type": "boolean",
                        "const": True,
                    },
                    "unknown_treated_as_negative": {
                        "type": "boolean",
                        "const": False,
                    },
                    "profile_changed": {"type": "boolean", "const": False},
                    "eligibility_changed": {
                        "type": "boolean",
                        "const": False,
                    },
                },
                "required": [
                    "all_supplied_opportunities_retained",
                    "unknown_treated_as_negative",
                    "profile_changed",
                    "eligibility_changed",
                ],
            },
        },
        "required": [
            "contract_version",
            "evaluation_mode",
            "relative_ordering_groups",
            "opportunity_assessments",
            "authority_confirmation",
        ],
    }


def _closed_unique_refs(
    values,
    allowed: Mapping[str, dict],
    reason: str,
    errors: list[str],
    *,
    allow_empty: bool = False,
) -> list[dict]:
    if (
        type(values) is not list
        or (not allow_empty and not values)
        or len(values) != len(set(values))
    ):
        errors.append(reason)
        return []
    resolved = []
    for value in values:
        if type(value) is not str or value not in allowed:
            errors.append(reason)
        else:
            resolved.append(allowed[value])
    return resolved


def validate_semantic_shadow_grounding_output_v1(
    *,
    output: Mapping,
    provider_input: Mapping,
    local_catalog: Mapping,
) -> dict:
    """Validate closed references, evidence roles, missingness, and safety."""

    errors: list[str] = []
    if type(output) is not dict:
        raise SemanticShadowGroundingContractError("output_not_object")
    if set(output) != {
        "contract_version",
        "evaluation_mode",
        "relative_ordering_groups",
        "opportunity_assessments",
        "authority_confirmation",
    }:
        errors.append("unexpected_output_field")
    if output.get("contract_version") != SEMANTIC_SHADOW_GROUNDING_OUTPUT_VERSION:
        errors.append("output_contract_version_mismatch")
    if output.get("evaluation_mode") != provider_input.get("evaluation_mode"):
        errors.append("evaluation_mode_mismatch")
    expected_confirmation = {
        "all_supplied_opportunities_retained": True,
        "unknown_treated_as_negative": False,
        "profile_changed": False,
        "eligibility_changed": False,
    }
    if output.get("authority_confirmation") != expected_confirmation:
        errors.append("authority_confirmation_violation")

    profile_refs = local_catalog["profile_references"]
    opportunities = local_catalog["opportunities"]
    expected_opportunity_ids = set(opportunities)
    all_opportunity_refs = {
        ref_id: item
        for opportunity in opportunities.values()
        for ref_id, item in opportunity["references"].items()
    }
    assessments = output.get("opportunity_assessments")
    if type(assessments) is not dict:
        assessments = {}
        errors.append("assessment_membership_mismatch")
    if set(assessments) != expected_opportunity_ids:
        errors.append("assessment_membership_mismatch")

    positive_support_by_opportunity = {}
    reference_count = 0
    for assessment_key, assessment in assessments.items():
        if type(assessment) is not dict:
            errors.append("invalid_assessment")
            continue
        if set(assessment) != {
            "opportunity_id",
            "assessment_state",
            "findings",
            "uncertainties",
        }:
            errors.append("unexpected_assessment_field")
        opportunity_id = assessment.get("opportunity_id")
        if opportunity_id != assessment_key:
            errors.append("assessment_opportunity_identity_mismatch")
        if assessment_key not in opportunities:
            errors.append("invented_opportunity_reference")
            continue
        opportunity_id = assessment_key
        own_refs = opportunities[opportunity_id]["references"]
        positive_count = 0
        uncertainty_pairs = set()
        uncertainties = assessment.get("uncertainties")
        if type(uncertainties) is not list:
            errors.append("invalid_uncertainties")
            uncertainties = []
        for uncertainty in uncertainties:
            if type(uncertainty) is not dict:
                errors.append("invalid_uncertainty")
                continue
            if set(uncertainty) != {
                "state",
                "effect",
                "profile_reference_ids",
                "proposition_reference_ids",
                "group_reference_ids",
                "evidence_reference_ids",
                "scope_reference_ids",
            }:
                errors.append("unexpected_uncertainty_field")
            state = uncertainty.get("state")
            if state not in UNCERTAINTY_STATES or uncertainty.get("effect") != "non_negative":
                errors.append("invalid_uncertainty_state")
            role_fields = {
                "profile_reference_ids": (profile_refs, "profile_fact"),
                "proposition_reference_ids": (all_opportunity_refs, "proposition"),
                "group_reference_ids": (all_opportunity_refs, "group"),
                "evidence_reference_ids": (all_opportunity_refs, "evidence"),
                "scope_reference_ids": (all_opportunity_refs, "variant"),
            }
            role_values = {
                field: uncertainty.get(field) for field in role_fields
            }
            if any(type(values) is not list for values in role_values.values()) or not any(
                role_values.values()
            ):
                errors.append("uncertainty_without_reference")
                continue
            resolved_states = []
            for field, (allowed, expected_role) in role_fields.items():
                values = role_values[field]
                if len(values) != len(set(values)):
                    errors.append("duplicate_uncertainty_reference")
                for value in values:
                    reference_count += 1
                    if type(value) is not str or value not in allowed:
                        errors.append(
                            "invented_profile_reference"
                            if field == "profile_reference_ids"
                            else "invented_opportunity_item_reference"
                        )
                        continue
                    item = allowed[value]
                    if item["reference_type"] != expected_role:
                        errors.append("reference_role_mismatch")
                        continue
                    if field != "profile_reference_ids" and value not in own_refs:
                        errors.append("cross_opportunity_reference")
                        continue
                    resolved_states.append(item["state"])
                    uncertainty_pairs.add((state, value))
            if state not in resolved_states:
                errors.append("uncertainty_state_not_grounded_by_reference")

        findings = assessment.get("findings")
        if type(findings) is not list:
            errors.append("invalid_findings")
            findings = []
        direct_count = 0
        partial_count = 0
        for finding in findings:
            if type(finding) is not dict:
                errors.append("invalid_finding")
                continue
            if set(finding) != {
                "finding_type",
                "relationship_type",
                "ranking_effect",
                "profile_reference_ids",
                "proposition_reference_ids",
                "group_reference_ids",
                "evidence_reference_ids",
                "scope_reference_ids",
            }:
                errors.append("unexpected_finding_field")
            finding_type = finding.get("finding_type")
            relationship = finding.get("relationship_type")
            ranking_effect = finding.get("ranking_effect")
            if finding_type not in FINDING_TYPES or relationship not in RELATIONSHIP_TYPES:
                errors.append("invalid_structured_finding")
                continue
            if (finding_type == "context_only") != (ranking_effect == "none"):
                errors.append("finding_ranking_effect_mismatch")
            if finding_type != "context_only" and ranking_effect != "positive_support":
                errors.append("finding_ranking_effect_mismatch")
            p_items = _closed_unique_refs(
                finding.get("profile_reference_ids"),
                profile_refs,
                "invented_or_duplicate_profile_reference",
                errors,
            )
            opportunity_fields = {
                "proposition_reference_ids": "proposition",
                "group_reference_ids": "group",
                "evidence_reference_ids": "evidence",
                "scope_reference_ids": "variant",
            }
            resolved_by_role = {}
            for field, expected_role in opportunity_fields.items():
                items = _closed_unique_refs(
                    finding.get(field),
                    all_opportunity_refs,
                    "invented_or_duplicate_opportunity_item_reference",
                    errors,
                    allow_empty=field != "proposition_reference_ids",
                )
                resolved_by_role[field] = items
                if any(item["reference_type"] != expected_role for item in items):
                    errors.append("reference_role_mismatch")
                if any(item["opportunity_id"] != opportunity_id for item in items):
                    errors.append("cross_opportunity_reference")
            reference_count += len(finding.get("profile_reference_ids", []))
            reference_count += sum(
                len(finding.get(field, [])) for field in opportunity_fields
            )
            if any(item["state"] != "grounded" for item in p_items):
                errors.append("missing_profile_state_used_as_finding")
            propositions = [
                item
                for item in resolved_by_role["proposition_reference_ids"]
                if item["reference_type"] == "proposition"
                and item["opportunity_id"] == opportunity_id
            ]
            if not propositions:
                errors.append("finding_without_opportunity_proposition")
            cited_group_ref_ids = {
                item["reference_id"]
                for item in resolved_by_role["group_reference_ids"]
                if item["reference_type"] == "group"
                and item["opportunity_id"] == opportunity_id
            }
            required_group_refs = {
                group_ref
                for proposition in propositions
                for group_ref in proposition["meaning"].get(
                    "group_reference_ids", []
                )
            }
            if cited_group_ref_ids != required_group_refs:
                errors.append("finding_relation_group_reference_mismatch")
            allowed_profile_dimensions, allowed_opportunity_dimensions = (
                RELATIONSHIP_DIMENSIONS[relationship]
            )
            if any(
                item["semantic_dimension"] not in allowed_profile_dimensions
                for item in p_items
            ) or any(
                item["semantic_dimension"] not in allowed_opportunity_dimensions
                for item in propositions
            ):
                errors.append("evidence_role_mismatch")
            if relationship == "domain_context" and (
                finding_type != "context_only" or ranking_effect != "none"
            ):
                errors.append("context_used_as_ranking_support")
            unresolved_refs = [
                item["reference_id"]
                for item in [
                    *propositions,
                    *resolved_by_role["group_reference_ids"],
                ]
                if item["state"] in {"unresolved", "incomplete"}
            ]
            if unresolved_refs and finding_type == "direct_alignment":
                errors.append("unresolved_material_used_as_direct_alignment")
            if unresolved_refs and finding_type == "partial_alignment" and any(
                not any(
                    (state, ref_id) in uncertainty_pairs
                    for state in {"unresolved", "incomplete"}
                )
                for ref_id in unresolved_refs
            ):
                errors.append("partial_alignment_missing_uncertainty")
            if ranking_effect == "positive_support":
                positive_count += 1
            direct_count += int(finding_type == "direct_alignment")
            partial_count += int(finding_type == "partial_alignment")

        expected_state = (
            "grounded_alignment"
            if direct_count
            else "limited_grounded_alignment"
            if partial_count
            else "unresolved_only"
            if uncertainties
            else "no_grounded_alignment"
        )
        if assessment.get("assessment_state") != expected_state:
            errors.append("assessment_state_mismatch")
        positive_support_by_opportunity[opportunity_id] = positive_count

    ordering_groups = output.get("relative_ordering_groups")
    if type(ordering_groups) is not list:
        ordering_groups = []
        errors.append("ordering_membership_mismatch")
    ordered_ids = []
    group_by_opportunity = {}
    for group_index, group in enumerate(ordering_groups):
        if type(group) is not dict or set(group) != {"opportunity_ids"}:
            errors.append("unexpected_ordering_group_field")
        values = group.get("opportunity_ids") if type(group) is dict else None
        if type(values) is not list or not values or len(values) != len(set(values)):
            errors.append("invalid_ordering_group")
            continue
        for value in values:
            ordered_ids.append(value)
            group_by_opportunity[value] = group_index
    if provider_input["evaluation_mode"] == "relative_reranking":
        if set(ordered_ids) != expected_opportunity_ids or len(ordered_ids) != len(
            set(ordered_ids)
        ):
            errors.append("ordering_membership_mismatch")
        zero_support = [
            item
            for item in expected_opportunity_ids
            if positive_support_by_opportunity.get(item, 0) == 0
        ]
        if zero_support and ordering_groups:
            last_index = len(ordering_groups) - 1
            if any(group_by_opportunity.get(item) != last_index for item in zero_support):
                errors.append("missing_support_used_as_negative_ranking_factor")
    elif ordering_groups:
        errors.append("single_assessment_created_ordering")

    if errors:
        raise SemanticShadowGroundingContractError(*errors)
    return {
        "contract_valid": True,
        "assessment_count": len(assessments),
        "resolvable_reference_count": reference_count,
        "reference_count": reference_count,
        "fabricated_reference_count": 0,
        "cross_opportunity_reference_count": 0,
        "unsupported_free_form_claim_count": 0,
        "missingness_as_absence_error_count": 0,
        "evidence_role_error_count": 0,
        "unresolved_relation_misuse_count": 0,
        "semantic_authority": semantic_non_exclusionary_authority(),
    }


__all__ = [
    "GROUNDING_INSTRUCTIONS",
    "SEMANTIC_SHADOW_GROUNDING_OUTPUT_VERSION",
    "SEMANTIC_SHADOW_GROUNDING_REQUEST_VERSION",
    "SemanticShadowGroundingContractError",
    "SemanticShadowGroundingRequestV1",
    "build_semantic_shadow_grounding_output_schema_v1",
    "build_semantic_shadow_grounding_request_v1",
    "validate_semantic_shadow_grounding_output_v1",
]

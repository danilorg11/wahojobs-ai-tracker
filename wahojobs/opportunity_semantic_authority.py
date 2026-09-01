"""OE Semantic Authority Boundary v1 and semantic matching packet.

This module is pure and offline.  It does not import verifier implementations,
matching runtime, persistence, candidate presentation, or provider clients.  Its
closed authority discriminators make model-derived opportunity semantics useful
for matching reasoning without allowing them to become eligibility truth.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from types import MappingProxyType

from wahojobs.opportunity_fact_authority import (
    OBJECTIVE_EXCLUSION_AUTHORIZED_EVIDENCE_BASES,
    evidence_record_has_objective_exclusion_authority,
)
from wahojobs.opportunity_semantic_contract import (
    ATOM_KINDS,
    CONTRACT_VERSION,
    MAX_ALTERNATIVES_PER_GROUP,
    MAX_ATOMS,
    MAX_ATOMS_PER_CONJUNCTION,
    MAX_CONSTRAINT_GROUPS,
    MAX_EVIDENCE_REFS_PER_ATOM,
    MODALITIES,
    POLARITIES,
    SUBJECTS,
    TEMPORALS,
    contract_atom_sha256,
    validate_accepted_evidence_catalog,
    validate_and_normalize_contract,
)


AUTHORITY_POLICY_VERSION = "oe_semantic_authority_boundary_v1"
SEMANTIC_MATCHING_PACKET_VERSION = "oe_semantic_matching_packet_v1"
SEMANTIC_MATCHING_PACKET_BUILDER_VERSION = (
    "oe_semantic_matching_packet_builder_v1"
)
OPPORTUNITY_FACT_AUTHORITY_VERSION = "oe_opportunity_fact_authority_v1"

SEMANTIC_AUTHORITY_TYPE = "semantic_non_exclusionary"
DETERMINISTIC_HARD_AUTHORITY_TYPE = "deterministic_objective_hard_eligibility"
AUTHORITY_TYPES = frozenset(
    {SEMANTIC_AUTHORITY_TYPE, DETERMINISTIC_HARD_AUTHORITY_TYPE}
)

SEMANTIC_ALLOWED_USES = (
    "evidence_retrieval",
    "explanations",
    "fit_assessment",
    "later_match_time_reasoning",
    "preferred_fit_signals",
    "semantic_reranking",
    "shortlist_generation",
)
SEMANTIC_FORBIDDEN_EFFECTS = (
    "authoritative_profile_contradiction",
    "candidate_exclusion",
    "deterministic_eligibility_failure",
    "hard_gate_failure",
    "lifecycle_suppression",
)

# This is pinned to matching_deterministic_eligibility_decision_v1 rather than
# imported from matching.  A test proves equality with the existing matching
# contract.  The separation prevents the semantic packet from depending on, or
# mutating, current matching behavior.
DETERMINISTIC_ELIGIBILITY_DECISION_VERSION = (
    "matching_deterministic_eligibility_decision_v1"
)
DETERMINISTIC_ELIGIBILITY_CRITERIA_V1 = MappingProxyType(
    {
        "eligibility.credentials_licenses": "credential_eligibility",
        "eligibility.location": "location_eligibility",
        "eligibility.required_languages": "required_language_eligibility",
    }
)
DETERMINISTIC_FACT_PATHS_BY_CRITERION_V1 = MappingProxyType(
    {
        "eligibility.credentials_licenses": frozenset(
            {
                "attributes.requirements.credentials",
                "attributes.requirements.licenses",
            }
        ),
        "eligibility.location": frozenset(
            {
                "attributes.work_arrangement.eligible_countries",
                "attributes.work_arrangement.eligible_locations",
                "attributes.work_arrangement.eligible_regions",
                "attributes.work_arrangement.location_scope",
            }
        ),
        "eligibility.required_languages": frozenset(
            {"attributes.requirements.languages"}
        ),
    }
)

# These compatibility fields can look authoritative to legacy consumers even
# when they came from a semantic projection.  They remain diagnostic-only under
# this policy.  The list is intentionally machine-readable for later integration.
DANGEROUS_SEMANTIC_COMPATIBILITY_FIELD_PATHS = frozenset(
    {
        "attributes.requirements.credentials",
        "attributes.requirements.current_status_requirements",
        "attributes.requirements.education.minimum_level",
        "attributes.requirements.experience_required",
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
DANGEROUS_DOWNSTREAM_INTERFACES = (
    "enrichment.attributes.requirements",
    "enrichment.attributes.work_arrangement.eligible_*",
    "enrichment.variant_facts",
    "matching.GroundedFactV1.automatic_enrichment",
    "matching.OPPORTUNITY_ENRICHMENT_FACT_FIELD_PATHS_V1",
    "matching.REQUIREMENT_FACT_FIELD_PATHS_V1",
    "semantic_contract.legacy_patch",
)

RELATION_STATES = frozenset(
    {
        "complete_validated",
        "complete_verified",
        "grounded_incomplete",
        "invalid_proposal",
        "unrepresentable_relation",
    }
)
SEMANTIC_SUPPORT_STATES = frozenset(
    {
        "accepted_evidence_validated",
        "contradicted",
        "not_established",
        "pending",
        "supported",
    }
)
COMPLETENESS_STATES = frozenset(
    {"complete", "incomplete", "invalid", "unresolved"}
)
NORMALIZATION_STATES = frozenset(
    {"ambiguous", "not_available", "resolved", "unmapped"}
)
SOURCE_BRANCH_STATES = frozenset({"matched", "unresolved"})
DESCRIPTIVE_SIGNAL_KINDS = frozenset({"candidate_profile", "responsibility"})
VARIANT_MODES = frozenset({"single_variant", "multi_variant"})
VARIANT_APPLICABILITY_STATES = frozenset(
    {"single_variant", "variant_subset", "all_known_variants", "unresolved"}
)
MAX_DESCRIPTIVE_SIGNALS = 65
MAX_SOURCE_BRANCHES_PER_GROUP = (
    MAX_ALTERNATIVES_PER_GROUP * MAX_ATOMS_PER_CONJUNCTION
)
EXPERIMENTAL_SEMANTIC_DECISIONS = frozenset(
    {"contradicts", "entails", "not_established", "not_supplied", "pending"}
)
EXPERIMENTAL_LEGACY_ASSURANCES = frozenset(
    {
        "hard_projection_authorized",
        "not_supplied",
        "rejected",
        "retained_unresolved",
        "semantic_verified",
    }
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PROPOSITION_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class OpportunitySemanticAuthorityError(ValueError):
    """Raised when an authority envelope or semantic packet is malformed."""


def _fail(path: str, reason: str) -> None:
    raise OpportunitySemanticAuthorityError(f"{path}: {reason}")


def canonical_json(value) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_sha256(value) -> str:
    if type(value) is str:
        material = value
    else:
        material = canonical_json(value)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _expect_exact_keys(value, keys, path: str) -> None:
    if type(value) is not dict:
        _fail(path, "must be an object")
    missing = set(keys) - set(value)
    extra = set(value) - set(keys)
    if missing or extra:
        _fail(
            path,
            f"has invalid keys; missing={sorted(missing)} extra={sorted(extra)}",
        )


def _expect_nonempty_string(value, path: str, *, maximum: int = 1000) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        _fail(path, f"must be a non-empty string of at most {maximum} characters")
    if value != value.strip():
        _fail(path, "must not have leading or trailing whitespace")
    return value


def _expect_sha256(value, path: str) -> str:
    value = _expect_nonempty_string(value, path, maximum=64)
    if _SHA256_RE.fullmatch(value) is None:
        _fail(path, "must be a lowercase SHA-256 digest")
    return value


def semantic_non_exclusionary_authority() -> dict:
    """Return the immutable semantic-soft authority discriminator."""

    return {
        "authority_policy_version": AUTHORITY_POLICY_VERSION,
        "authority_type": SEMANTIC_AUTHORITY_TYPE,
        "hard_eligibility_authorized": False,
        "deterministic_failure_authorized": False,
        "candidate_exclusion_authorized": False,
        "lifecycle_suppression_authorized": False,
        "profile_contradiction_authorized": False,
        "allowed_uses": list(SEMANTIC_ALLOWED_USES),
        "forbidden_effects": list(SEMANTIC_FORBIDDEN_EFFECTS),
    }


def semantic_compatibility_projection_authority() -> dict:
    """Return the closed diagnostic-only authority for legacy projections."""

    return {
        "authority_policy_version": AUTHORITY_POLICY_VERSION,
        "authority_type": SEMANTIC_AUTHORITY_TYPE,
        "projection_role": "diagnostic_compatibility_only",
        "hard_eligibility_authorized": False,
        "candidate_exclusion_authorized": False,
        "authority_effect": "none",
    }


def _deterministic_hard_authority(criterion_id: str) -> dict:
    if criterion_id not in DETERMINISTIC_ELIGIBILITY_CRITERIA_V1:
        _fail("criterion_id", "is not an authorized deterministic criterion")
    return {
        "authority_policy_version": AUTHORITY_POLICY_VERSION,
        "authority_type": DETERMINISTIC_HARD_AUTHORITY_TYPE,
        "decision_contract_version": DETERMINISTIC_ELIGIBILITY_DECISION_VERSION,
        "eligibility_criterion_id": criterion_id,
        "eligibility_dimension": DETERMINISTIC_ELIGIBILITY_CRITERIA_V1[
            criterion_id
        ],
        "hard_eligibility_authorized": True,
        "deterministic_failure_authorized": True,
        "candidate_exclusion_authorized": True,
        "scope": "criterion_only",
    }


def authority_can_create_hard_eligibility_failure(value) -> bool:
    """Return true only for a valid deterministic hard-authority envelope."""

    authority = value.get("authority") if type(value) is dict else None
    if type(authority) is not dict:
        authority = value
    if type(authority) is not dict:
        _fail("authority", "must be an object")
    authority_type = authority.get("authority_type")
    if authority_type == SEMANTIC_AUTHORITY_TYPE:
        if authority != semantic_non_exclusionary_authority():
            _fail("authority", "does not equal the closed semantic authority state")
        return False
    if authority_type != DETERMINISTIC_HARD_AUTHORITY_TYPE:
        _fail("authority.authority_type", "is outside the closed authority set")
    criterion_id = authority.get("eligibility_criterion_id")
    if authority != _deterministic_hard_authority(criterion_id):
        _fail("authority", "does not equal a closed deterministic authority state")
    return True


def hard_authoritative_objective_fact(fact: dict, *, criterion_id: str) -> dict:
    """Wrap one separately authorized deterministic/objective fact.

    Merely being deterministic is insufficient.  The caller must name a closed
    criterion, the fact path must be valid for that criterion, and every evidence
    record must be high-confidence deterministic or source-explicit evidence.
    """

    _expect_exact_keys(
        fact,
        {"field_path", "value", "knowledge_state", "variant_refs", "evidence"},
        "fact",
    )
    field_path = _expect_nonempty_string(fact["field_path"], "fact.field_path")
    if criterion_id not in DETERMINISTIC_FACT_PATHS_BY_CRITERION_V1:
        _fail("criterion_id", "is not a closed deterministic eligibility criterion")
    if field_path not in DETERMINISTIC_FACT_PATHS_BY_CRITERION_V1[criterion_id]:
        _fail("fact.field_path", "is outside the criterion's objective fact scope")
    if fact["knowledge_state"] != "known_value":
        _fail("fact.knowledge_state", "must be known_value")
    variant_refs = fact["variant_refs"]
    if (
        type(variant_refs) is not list
        or not variant_refs
        or variant_refs != sorted(set(variant_refs))
        or any(type(item) is not str or not item for item in variant_refs)
    ):
        _fail("fact.variant_refs", "must be a non-empty sorted unique string list")
    evidence = fact["evidence"]
    if type(evidence) is not list or not evidence:
        _fail("fact.evidence", "must be a non-empty list")
    for index, item in enumerate(evidence):
        path = f"fact.evidence[{index}]"
        _expect_exact_keys(
            item,
            {
                "evidence_block_id",
                "source_refs",
                "authority_refs",
                "evidence_text",
                "basis",
                "confidence",
            },
            path,
        )
        if item["basis"] not in OBJECTIVE_EXCLUSION_AUTHORIZED_EVIDENCE_BASES:
            _fail(f"{path}.basis", "is not deterministic/objective")
        if not evidence_record_has_objective_exclusion_authority(item):
            _fail(f"{path}.confidence", "must be high")
        for field in ("source_refs", "authority_refs"):
            values = item[field]
            if (
                type(values) is not list
                or values != sorted(set(values))
                or any(type(value) is not str or not value for value in values)
            ):
                _fail(f"{path}.{field}", "must be a sorted unique string list")
        if not item["source_refs"]:
            _fail(f"{path}.source_refs", "must be non-empty")
        _expect_nonempty_string(
            item["evidence_block_id"], f"{path}.evidence_block_id"
        )
        _expect_nonempty_string(item["evidence_text"], f"{path}.evidence_text")
    envelope = {
        "fact_authority_version": OPPORTUNITY_FACT_AUTHORITY_VERSION,
        "origin_type": "deterministic_objective",
        "field_path": field_path,
        "value": copy.deepcopy(fact["value"]),
        "variant_refs": copy.deepcopy(variant_refs),
        "evidence": copy.deepcopy(evidence),
        "authority": _deterministic_hard_authority(criterion_id),
    }
    if not authority_can_create_hard_eligibility_failure(envelope):
        raise AssertionError("deterministic authority construction failed closed")
    return envelope


def derive_server_variant_relationships(
    source_packet: dict,
    accepted_evidence_bindings: list[dict],
    accepted_evidence_catalog: list[dict],
) -> list[dict]:
    """Derive source-to-variant links from authenticated server-owned bindings.

    This performs no semantic inference.  It reproduces the deterministic quote
    source identity and copies only variant/source/authority references already
    attached to the accepted packet block by the server.
    """

    if type(source_packet) is not dict or type(source_packet.get("evidence_blocks")) is not list:
        _fail("source_packet", "must contain evidence_blocks")
    blocks = {}
    for index, block in enumerate(source_packet["evidence_blocks"]):
        path = f"source_packet.evidence_blocks[{index}]"
        if type(block) is not dict:
            _fail(path, "must be an object")
        alias = _expect_nonempty_string(
            block.get("evidence_block_id"), f"{path}.evidence_block_id"
        )
        if alias in blocks:
            _fail(f"{path}.evidence_block_id", "must be unique")
        blocks[alias] = block

    bindings = {}
    if type(accepted_evidence_bindings) is not list:
        _fail("accepted_evidence_bindings", "must be a list")
    for index, binding in enumerate(accepted_evidence_bindings):
        path = f"accepted_evidence_bindings[{index}]"
        _expect_exact_keys(
            binding,
            {"alias", "authority", "text", "text_sha256", "provenance"},
            path,
        )
        alias = _expect_nonempty_string(binding["alias"], f"{path}.alias")
        block = blocks.get(alias)
        if block is None or block.get("authority_class") != "accepted_body_evidence":
            _fail(f"{path}.alias", "does not name accepted body evidence")
        if block.get("content") != binding["text"]:
            _fail(f"{path}.text", "does not match the accepted packet block")
        if canonical_sha256(binding["text"]) != binding["text_sha256"]:
            _fail(f"{path}.text_sha256", "does not authenticate binding text")
        if binding["authority"] not in {
            "accepted_capture",
            "accepted_review_checkpoint",
        }:
            _fail(f"{path}.authority", "is not accepted evidence authority")
        bindings[alias] = binding

    sources = validate_accepted_evidence_catalog(accepted_evidence_catalog)
    relationships = []
    for source_id in sorted(sources):
        source = sources[source_id]
        matches = []
        for alias in sorted(bindings):
            binding = bindings[alias]
            if binding["authority"] != source["authority"]:
                continue
            if binding["provenance"] != source["provenance"]:
                continue
            if source["text"] not in binding["text"]:
                continue
            identity = {
                "alias": alias,
                "quote": source["text"],
                "authority": binding["authority"],
                "provenance": binding["provenance"],
            }
            expected_source_id = f"extract:{canonical_sha256(identity)[:40]}"
            if expected_source_id != source_id:
                continue
            block = blocks[alias]
            matches.append(
                {
                    "source_id": source_id,
                    "evidence_block_id": alias,
                    "derivation": "server_authenticated_evidence_binding",
                    "variant_refs": sorted(set(block.get("variant_refs") or [])),
                    "source_refs": sorted(set(block.get("source_refs") or [])),
                    "authority_refs": sorted(set(block.get("authority_refs") or [])),
                }
            )
        if len(matches) != 1:
            _fail(
                f"accepted_evidence_catalog[{source_id!r}]",
                "must map to exactly one authenticated packet binding",
            )
        relationships.append(matches[0])
    return relationships


def _validate_variant_relationships(
    relationships, evidence_source_ids: set[str]
) -> list[dict]:
    if type(relationships) not in {list, tuple}:
        _fail("variant_relationships", "must be a list")
    normalized = []
    seen = set()
    for index, item in enumerate(relationships):
        path = f"variant_relationships[{index}]"
        _expect_exact_keys(
            item,
            {
                "source_id",
                "evidence_block_id",
                "derivation",
                "variant_refs",
                "source_refs",
                "authority_refs",
            },
            path,
        )
        source_id = _expect_nonempty_string(item["source_id"], f"{path}.source_id")
        if source_id not in evidence_source_ids or source_id in seen:
            _fail(f"{path}.source_id", "is unknown or duplicated")
        if item["derivation"] != "server_authenticated_evidence_binding":
            _fail(f"{path}.derivation", "is not server-derived")
        for field in ("variant_refs", "source_refs", "authority_refs"):
            values = item[field]
            if (
                type(values) is not list
                or values != sorted(set(values))
                or any(type(value) is not str or not value for value in values)
            ):
                _fail(f"{path}.{field}", "must be a sorted unique string list")
        _expect_nonempty_string(
            item["evidence_block_id"], f"{path}.evidence_block_id"
        )
        seen.add(source_id)
        normalized.append(copy.deepcopy(item))
    return sorted(normalized, key=lambda item: item["source_id"])


def semantic_opportunity_scope(
    *, canonical_ref: str, known_variant_refs
) -> dict:
    """Return the closed server-owned opportunity scope carried by packet v1."""

    canonical_ref = _expect_nonempty_string(
        canonical_ref, "canonical_ref", maximum=256
    )
    if (
        type(known_variant_refs) not in {list, tuple}
        or not known_variant_refs
        or list(known_variant_refs) != sorted(set(known_variant_refs))
        or any(type(value) is not str or not value for value in known_variant_refs)
    ):
        _fail(
            "known_variant_refs",
            "must be a non-empty sorted unique string list",
        )
    variant_refs = list(known_variant_refs)
    return {
        "canonical_ref": canonical_ref,
        "known_variant_refs": copy.deepcopy(variant_refs),
        "variant_mode": (
            "single_variant" if len(variant_refs) == 1 else "multi_variant"
        ),
        "scope_authority": "server_opportunity_record",
        "canonical_fact_promotion_allowed": False,
    }


def _semantic_applicability(relationships, opportunity_scope: dict) -> dict:
    variant_refs = sorted(
        {
            variant_ref
            for relationship in relationships
            for variant_ref in relationship["variant_refs"]
        }
    )
    known_variant_refs = opportunity_scope["known_variant_refs"]
    if not variant_refs:
        variant_applicability = "unresolved"
    elif variant_refs == known_variant_refs:
        variant_applicability = (
            "single_variant"
            if opportunity_scope["variant_mode"] == "single_variant"
            else "all_known_variants"
        )
    else:
        variant_applicability = "variant_subset"
    return {
        "canonical_ref": opportunity_scope["canonical_ref"],
        "canonical_applicability": "not_claimed",
        "variant_applicability": variant_applicability,
        "variant_refs": variant_refs,
        "known_variant_coverage_complete": variant_refs == known_variant_refs,
        "derivation": "server_authenticated_evidence_relationships",
    }


def _validate_semantic_applicability(
    value,
    *,
    relationships,
    opportunity_scope: dict,
    path: str,
) -> None:
    _expect_exact_keys(
        value,
        {
            "canonical_ref",
            "canonical_applicability",
            "variant_applicability",
            "variant_refs",
            "known_variant_coverage_complete",
            "derivation",
        },
        path,
    )
    if value != _semantic_applicability(relationships, opportunity_scope):
        _fail(path, "does not match server-derived evidence applicability")


def _meaning(atom: dict) -> dict:
    return {
        "subject": copy.deepcopy(atom["subject"]),
        "kind": copy.deepcopy(atom["kind"]),
        "typed_payload": copy.deepcopy(atom["typed_payload"]),
        "polarity": copy.deepcopy(atom["polarity"]),
        "temporal": copy.deepcopy(atom["temporal"]),
    }


def _normalized_value(atom: dict) -> str:
    return f"{atom['kind']}:{canonical_json(atom['typed_payload'])}"


def _group_atom_ids(proposal) -> list[str]:
    if type(proposal) is not dict or type(proposal.get("any_of")) is not list:
        return []
    values = []
    for alternative in proposal["any_of"]:
        if type(alternative) is not dict or type(alternative.get("all_of")) is not list:
            continue
        values.extend(
            item for item in alternative["all_of"] if type(item) is str
        )
    return values


def _logic_from_proposal(proposal, state: str):
    if state == "unrepresentable_relation" or type(proposal) is not dict:
        return None
    if set(proposal) != {"modality", "any_of"}:
        return None
    if proposal.get("modality") not in MODALITIES or type(proposal.get("any_of")) is not list:
        return None
    for alternative in proposal["any_of"]:
        if type(alternative) is not dict or set(alternative) != {"all_of"}:
            return None
        if type(alternative["all_of"]) is not list:
            return None
    return {
        "form": "bounded_dnf",
        "any_of": copy.deepcopy(proposal["any_of"]),
    }


def _variant_links_for_sources(source_ids, relationships_by_source: dict) -> list[dict]:
    return [
        copy.deepcopy(relationships_by_source[source_id])
        for source_id in sorted(set(source_ids))
        if source_id in relationships_by_source
    ]


def _variant_links_for_propositions(propositions, proposition_ids) -> list[dict]:
    by_source = {}
    for proposition_id in proposition_ids:
        proposition = propositions.get(proposition_id)
        if proposition is None:
            continue
        for relationship in proposition["variant_relationships"]:
            by_source[relationship["source_id"]] = relationship
    return [copy.deepcopy(by_source[key]) for key in sorted(by_source)]


def _descriptive_signals(
    raw_signals,
    *,
    evidence_source_ids: set[str],
    relationships_by_source: dict,
    opportunity_scope: dict,
) -> list[dict]:
    """Normalize native semantic responsibilities/candidate-profile content."""

    if type(raw_signals) not in {list, tuple}:
        _fail("descriptive_signals", "must be a list")
    normalized = []
    seen_ids = set()
    candidate_profiles = 0
    for index, raw in enumerate(raw_signals):
        path = f"descriptive_signals[{index}]"
        _expect_exact_keys(
            raw,
            {"signal_id", "kind", "value", "evidence_source_ids"},
            path,
        )
        signal_id = _expect_nonempty_string(
            raw["signal_id"], f"{path}.signal_id", maximum=64
        )
        if _PROPOSITION_ID_RE.fullmatch(signal_id) is None or signal_id in seen_ids:
            _fail(f"{path}.signal_id", "is invalid or duplicated")
        kind = raw["kind"]
        if kind not in DESCRIPTIVE_SIGNAL_KINDS:
            _fail(f"{path}.kind", "is outside the closed descriptive signal set")
        candidate_profiles += kind == "candidate_profile"
        if candidate_profiles > 1:
            _fail("descriptive_signals", "may contain at most one candidate profile")
        value = _expect_nonempty_string(raw["value"], f"{path}.value", maximum=4000)
        source_ids = raw["evidence_source_ids"]
        if (
            type(source_ids) is not list
            or not source_ids
            or source_ids != sorted(set(source_ids))
            or not set(source_ids) <= evidence_source_ids
        ):
            _fail(
                f"{path}.evidence_source_ids",
                "must be non-empty, sorted, unique accepted source IDs",
            )
        identity = {
            "signal_id": signal_id,
            "kind": kind,
            "value": value,
            "evidence_source_ids": copy.deepcopy(source_ids),
        }
        variant_relationships = _variant_links_for_sources(
            source_ids, relationships_by_source
        )
        normalized.append(
            {
                **identity,
                "signal_sha256": canonical_sha256(identity),
                "variant_relationships": variant_relationships,
                "applicability": _semantic_applicability(
                    variant_relationships, opportunity_scope
                ),
                "authority": semantic_non_exclusionary_authority(),
            }
        )
        seen_ids.add(signal_id)
    return sorted(normalized, key=lambda item: item["signal_id"])


def _semantic_proposition(
    atom: dict,
    *,
    atom_sha256: str,
    support_state: str,
    completeness_state: str,
    normalization_status: str,
    qualifier_complete: bool,
    source_values: list[dict],
    group_refs: list[str],
    relationships_by_source: dict,
    opportunity_scope: dict,
    experimental_decision: str,
    legacy_assurance: str,
) -> dict:
    meaning = _meaning(atom)
    safe_normalized = (
        _normalized_value(atom)
        if normalization_status == "resolved" and qualifier_complete
        else None
    )
    source_ids = [item["source_id"] for item in atom["evidence"]]
    variant_relationships = _variant_links_for_sources(
        source_ids, relationships_by_source
    )
    return {
        "proposition_id": atom["id"],
        "atom_sha256": atom_sha256,
        "meaning_sha256": canonical_sha256(meaning),
        "meaning": meaning,
        "normalized_value": safe_normalized,
        "normalization": {
            "status": normalization_status,
            "qualifier_complete": qualifier_complete,
            "source_values": copy.deepcopy(source_values),
        },
        "evidence": copy.deepcopy(atom["evidence"]),
        "variant_relationships": variant_relationships,
        "applicability": _semantic_applicability(
            variant_relationships, opportunity_scope
        ),
        "status": {
            "semantic_support": support_state,
            "completeness": completeness_state,
        },
        "group_refs": sorted(set(group_refs)),
        "authority": semantic_non_exclusionary_authority(),
        "experimental_observation": {
            "semantic_decision": experimental_decision,
            "legacy_assurance": legacy_assurance,
            "authority_effect": "none",
        },
    }


def _semantic_group(
    *,
    group_id: str,
    proposal,
    state: str,
    server_derived_modality,
    reasons: list[str],
    source_branches: list[dict],
    source_logic_exact,
    propositions_by_id: dict,
    opportunity_scope: dict,
    experimental_state: str,
) -> dict:
    logic = _logic_from_proposal(proposal, state)
    atom_ids = sorted(set(_group_atom_ids(proposal)))
    modality = proposal.get("modality") if type(proposal) is dict else None
    variant_relationships = _variant_links_for_propositions(
        propositions_by_id, atom_ids
    )
    return {
        "group_id": group_id,
        "group_sha256": canonical_sha256(proposal),
        "modality": modality if modality in MODALITIES else None,
        "server_derived_modality": (
            server_derived_modality
            if server_derived_modality in MODALITIES
            else None
        ),
        "logic": logic,
        "raw_proposal": copy.deepcopy(proposal),
        "referenced_proposition_ids": atom_ids,
        "relation_state": state,
        "completeness": {
            "complete_validated": "complete",
            "complete_verified": "complete",
            "grounded_incomplete": "incomplete",
            "invalid_proposal": "invalid",
            "unrepresentable_relation": "unresolved",
        }[state],
        "reason_codes": sorted(set(reasons)),
        "source_branches": copy.deepcopy(source_branches),
        "source_logic_exact": source_logic_exact,
        "variant_relationships": variant_relationships,
        "applicability": _semantic_applicability(
            variant_relationships, opportunity_scope
        ),
        "authority": semantic_non_exclusionary_authority(),
        "experimental_observation": {
            "legacy_relation_state": experimental_state,
            "authority_effect": "none",
        },
    }


def _packet_accounting(packet: dict) -> dict:
    propositions = packet["propositions"]
    groups = packet["groups"]
    branches = [branch for group in groups for branch in group["source_branches"]]
    return {
        "proposition_count": len(propositions),
        "group_count": len(groups),
        "descriptive_signal_count": len(packet["descriptive_signals"]),
        "retained_invalid_proposal_count": len(packet["retained_invalid_proposals"]),
        "unassigned_proposition_count": len(packet["unassigned_proposition_ids"]),
        "evidence_source_count": len(packet["evidence_sources"]),
        "source_branch_count": len(branches),
        "unresolved_source_branch_count": sum(
            branch.get("coverage_state") == "unresolved" for branch in branches
        ),
        "complete_group_count": sum(group["completeness"] == "complete" for group in groups),
        "incomplete_or_unresolved_group_count": sum(
            group["completeness"] != "complete" for group in groups
        ),
        "required_group_count": sum(group["modality"] == "required" for group in groups),
        "preferred_group_count": sum(group["modality"] == "preferred" for group in groups),
        "descriptive_group_count": sum(group["modality"] == "descriptive" for group in groups),
        "canonical_semantic_fact_claim_count": sum(
            item["applicability"]["canonical_applicability"] != "not_claimed"
            for item in [
                *propositions,
                *groups,
                *packet["descriptive_signals"],
                *packet["retained_invalid_proposals"],
            ]
        ),
        "semantic_hard_exclusion_count": sum(
            authority_can_create_hard_eligibility_failure(item)
            for item in [
                *propositions,
                *groups,
                *packet["descriptive_signals"],
                *packet["retained_invalid_proposals"],
            ]
        ),
    }


def _identities(
    *,
    semantic_input_version: str,
    semantic_input_sha256: str,
    source_packet_sha256: str,
    semantic_extraction_version,
    semantic_grouping_version,
    contract_version: str,
    staging_version,
    relation_builder_version,
) -> dict:
    return {
        "semantic_input_version": _expect_nonempty_string(
            semantic_input_version, "semantic_input_version", maximum=128
        ),
        "semantic_input_sha256": _expect_sha256(
            semantic_input_sha256, "semantic_input_sha256"
        ),
        "source_packet_sha256": _expect_sha256(
            source_packet_sha256, "source_packet_sha256"
        ),
        "semantic_extraction_version": semantic_extraction_version,
        "semantic_grouping_version": semantic_grouping_version,
        "semantic_contract_version": _expect_nonempty_string(
            contract_version, "contract_version", maximum=128
        ),
        "staging_version": staging_version,
        "relation_builder_version": relation_builder_version,
    }


def _finish_packet(packet: dict) -> dict:
    packet["accounting"] = _packet_accounting(packet)
    packet["packet_sha256"] = semantic_matching_packet_sha256(packet)
    return validate_semantic_matching_packet(packet)


def build_semantic_matching_packet(
    contract: dict,
    accepted_evidence_catalog: list[dict],
    *,
    canonical_ref: str,
    known_variant_refs,
    semantic_input_version: str,
    semantic_input_sha256: str,
    source_packet_sha256: str,
    variant_relationships=(),
    descriptive_signals=(),
) -> dict:
    """Build a verifier-free semantic packet from a validated v0 contract."""

    normalized = validate_and_normalize_contract(
        contract, accepted_evidence_catalog
    )
    sources = validate_accepted_evidence_catalog(accepted_evidence_catalog)
    relationships = _validate_variant_relationships(
        variant_relationships, set(sources)
    )
    relationships_by_source = {
        item["source_id"]: item for item in relationships
    }
    opportunity_scope = semantic_opportunity_scope(
        canonical_ref=canonical_ref,
        known_variant_refs=known_variant_refs,
    )
    normalized_descriptive_signals = _descriptive_signals(
        descriptive_signals,
        evidence_source_ids=set(sources),
        relationships_by_source=relationships_by_source,
        opportunity_scope=opportunity_scope,
    )
    group_refs_by_atom = {atom["id"]: [] for atom in normalized["atoms"]}
    for index, group in enumerate(normalized["constraint_groups"]):
        for atom_id in _group_atom_ids(group):
            group_refs_by_atom[atom_id].append(f"g{index:03d}")
    propositions = [
        _semantic_proposition(
            atom,
            atom_sha256=contract_atom_sha256(atom),
            support_state="accepted_evidence_validated",
            completeness_state="complete",
            normalization_status="resolved",
            qualifier_complete=True,
            source_values=[],
            group_refs=group_refs_by_atom[atom["id"]],
            relationships_by_source=relationships_by_source,
            opportunity_scope=opportunity_scope,
            experimental_decision="not_supplied",
            legacy_assurance="not_supplied",
        )
        for atom in normalized["atoms"]
    ]
    propositions_by_id = {item["proposition_id"]: item for item in propositions}
    groups = []
    for index, group in enumerate(normalized["constraint_groups"]):
        source_branches = [
            {
                "source_branch_id": "contract:" + canonical_sha256(alternative)[:24],
                "source_values": [],
                "proposed_atom_ids": copy.deepcopy(alternative["all_of"]),
                "coverage_state": "matched",
            }
            for alternative in group["any_of"]
        ]
        groups.append(
            _semantic_group(
                group_id=f"g{index:03d}",
                proposal=group,
                state="complete_validated",
                server_derived_modality=group["modality"],
                reasons=[],
                source_branches=source_branches,
                source_logic_exact=True,
                propositions_by_id=propositions_by_id,
                opportunity_scope=opportunity_scope,
                experimental_state="not_supplied",
            )
        )
    packet = {
        "packet_version": SEMANTIC_MATCHING_PACKET_VERSION,
        "authority_policy_version": AUTHORITY_POLICY_VERSION,
        "builder_version": SEMANTIC_MATCHING_PACKET_BUILDER_VERSION,
        "packet_sha256": None,
        "identities": _identities(
            semantic_input_version=semantic_input_version,
            semantic_input_sha256=semantic_input_sha256,
            source_packet_sha256=source_packet_sha256,
            semantic_extraction_version=None,
            semantic_grouping_version=None,
            contract_version=normalized["contract_version"],
            staging_version=None,
            relation_builder_version=None,
        ),
        "opportunity_scope": opportunity_scope,
        "authority": semantic_non_exclusionary_authority(),
        "construction_policy": {
            "evidence_authority": "accepted_server_authenticated_only",
            "variant_relationship_authority": "server_derived_only",
            "verifier_dependency": "none",
            "verifier_authority_effect": "none",
            "legacy_compatibility_authority_effect": "none",
        },
        "evidence_sources": [copy.deepcopy(sources[key]) for key in sorted(sources)],
        "propositions": propositions,
        "groups": groups,
        "descriptive_signals": normalized_descriptive_signals,
        "unassigned_proposition_ids": [],
        "retained_invalid_proposals": [],
        "accounting": None,
    }
    return _finish_packet(packet)


def build_semantic_matching_packet_from_staging(
    staging: dict,
    relation_ledger: dict,
    *,
    canonical_ref: str,
    known_variant_refs,
    semantic_input_version: str,
    semantic_input_sha256: str,
    source_packet_sha256: str,
    semantic_extraction_version: str,
    semantic_grouping_version: str | None = None,
    variant_relationships=(),
    descriptive_signals=(),
) -> dict:
    """Build a semantic packet from a staging ledger without verifier code.

    Semantic decisions and legacy assurances are copied only into experimental
    observations.  No observation changes the semantic authority discriminator.
    Incomplete, invalid, and unrepresentable relation proposals remain present.
    """

    if type(staging) is not dict or type(staging.get("provisional_atoms")) is not list:
        _fail("staging", "must contain a provisional atom ledger")
    if type(relation_ledger) is not dict or type(relation_ledger.get("relations")) is not list:
        _fail("relation_ledger", "must contain relations")
    sources = validate_accepted_evidence_catalog(
        staging.get("accepted_evidence_catalog")
    )
    relationships = _validate_variant_relationships(
        variant_relationships, set(sources)
    )
    relationships_by_source = {
        item["source_id"]: item for item in relationships
    }
    opportunity_scope = semantic_opportunity_scope(
        canonical_ref=canonical_ref,
        known_variant_refs=known_variant_refs,
    )
    normalized_descriptive_signals = _descriptive_signals(
        descriptive_signals,
        evidence_source_ids=set(sources),
        relationships_by_source=relationships_by_source,
        opportunity_scope=opportunity_scope,
    )
    group_refs_by_atom = {}
    for relation in relation_ledger["relations"]:
        relation_id = relation.get("relation_id")
        for atom_id in _group_atom_ids(relation.get("proposal")):
            group_refs_by_atom.setdefault(atom_id, []).append(relation_id)

    propositions = []
    invalid = []
    for index, item in enumerate(staging["provisional_atoms"]):
        path = f"staging.provisional_atoms[{index}]"
        if type(item) is not dict:
            _fail(path, "must be an object")
        atom = item.get("atom")
        if item.get("status") != "provisional" or type(atom) is not dict:
            invalid.append(
                {
                    "ledger_id": copy.deepcopy(item.get("ledger_id")),
                    "proposal_id": copy.deepcopy(item.get("proposal_id")),
                    "raw_proposal_sha256": canonical_sha256(item.get("raw_proposal")),
                    "raw_proposal": copy.deepcopy(item.get("raw_proposal")),
                    "retention_state": "structurally_invalid",
                    "reason_codes": [
                        str((item.get("authentication") or {}).get("reason") or "structurally_invalid")
                    ],
                    "applicability": _semantic_applicability(
                        [], opportunity_scope
                    ),
                    "authority": semantic_non_exclusionary_authority(),
                }
            )
            continue
        decision = (item.get("semantic_verification") or {}).get(
            "decision", "pending"
        )
        support_state = {
            "entails": "supported",
            "contradicts": "contradicted",
            "not_established": "not_established",
            "pending": "pending",
        }.get(decision, "pending")
        normalization = item.get("normalization") or {}
        normalization_status = normalization.get("status", "not_available")
        qualifier_complete = normalization.get("qualifier_complete") is True
        if support_state == "supported" and normalization_status == "resolved" and qualifier_complete:
            completeness = "complete"
        elif support_state in {"contradicted", "not_established"}:
            completeness = "invalid"
        else:
            completeness = "unresolved"
        propositions.append(
            _semantic_proposition(
                atom,
                atom_sha256=item.get("atom_sha256") or contract_atom_sha256(atom),
                support_state=support_state,
                completeness_state=completeness,
                normalization_status=normalization_status,
                qualifier_complete=qualifier_complete,
                source_values=normalization.get("source_values") or [],
                group_refs=group_refs_by_atom.get(atom["id"], []),
                relationships_by_source=relationships_by_source,
                opportunity_scope=opportunity_scope,
                experimental_decision=decision,
                legacy_assurance=str(item.get("assurance") or "not_supplied"),
            )
        )
    propositions.sort(key=lambda item: item["proposition_id"])
    propositions_by_id = {item["proposition_id"]: item for item in propositions}
    if len(propositions_by_id) != len(propositions):
        _fail("staging.provisional_atoms", "contains duplicate proposition identities")

    groups = []
    for index, relation in enumerate(relation_ledger["relations"]):
        path = f"relation_ledger.relations[{index}]"
        if type(relation) is not dict:
            _fail(path, "must be an object")
        state = relation.get("state")
        if state not in RELATION_STATES - {"complete_validated"}:
            _fail(f"{path}.state", "is outside the closed relation state set")
        groups.append(
            _semantic_group(
                group_id=str(relation.get("relation_id") or f"r{index:03d}"),
                proposal=relation.get("proposal"),
                state=state,
                server_derived_modality=relation.get("authoritative_modality"),
                reasons=relation.get("reason_codes") or [],
                source_branches=relation.get("source_branches") or [],
                source_logic_exact=relation.get("source_logic_exact"),
                propositions_by_id=propositions_by_id,
                opportunity_scope=opportunity_scope,
                experimental_state=state,
            )
        )
    groups.sort(key=lambda item: item["group_id"])
    unassigned = sorted(
        set((relation_ledger.get("atom_membership") or {}).get("unassigned_atom_ids") or [])
    )
    packet = {
        "packet_version": SEMANTIC_MATCHING_PACKET_VERSION,
        "authority_policy_version": AUTHORITY_POLICY_VERSION,
        "builder_version": SEMANTIC_MATCHING_PACKET_BUILDER_VERSION,
        "packet_sha256": None,
        "identities": _identities(
            semantic_input_version=semantic_input_version,
            semantic_input_sha256=semantic_input_sha256,
            source_packet_sha256=source_packet_sha256,
            semantic_extraction_version=semantic_extraction_version,
            semantic_grouping_version=semantic_grouping_version,
            contract_version=CONTRACT_VERSION,
            staging_version=staging.get("staging_version"),
            relation_builder_version=relation_ledger.get("relation_builder_version"),
        ),
        "opportunity_scope": opportunity_scope,
        "authority": semantic_non_exclusionary_authority(),
        "construction_policy": {
            "evidence_authority": "accepted_server_authenticated_only",
            "variant_relationship_authority": "server_derived_only",
            "verifier_dependency": "none",
            "verifier_authority_effect": "none",
            "legacy_compatibility_authority_effect": "none",
        },
        "evidence_sources": [copy.deepcopy(sources[key]) for key in sorted(sources)],
        "propositions": propositions,
        "groups": groups,
        "descriptive_signals": normalized_descriptive_signals,
        "unassigned_proposition_ids": unassigned,
        "retained_invalid_proposals": invalid,
        "accounting": None,
    }
    return _finish_packet(packet)


def semantic_matching_packet_sha256(packet: dict) -> str:
    material = copy.deepcopy(packet)
    material.pop("packet_sha256", None)
    return canonical_sha256(material)


def validate_semantic_matching_packet(packet: dict) -> dict:
    """Validate all authority and preservation invariants of packet v1."""

    _expect_exact_keys(
        packet,
        {
            "packet_version",
            "authority_policy_version",
            "builder_version",
            "packet_sha256",
            "identities",
            "opportunity_scope",
            "authority",
            "construction_policy",
            "evidence_sources",
            "propositions",
            "groups",
            "descriptive_signals",
            "unassigned_proposition_ids",
            "retained_invalid_proposals",
            "accounting",
        },
        "packet",
    )
    if packet["packet_version"] != SEMANTIC_MATCHING_PACKET_VERSION:
        _fail("packet.packet_version", "is not supported")
    if packet["authority_policy_version"] != AUTHORITY_POLICY_VERSION:
        _fail("packet.authority_policy_version", "is not supported")
    if packet["builder_version"] != SEMANTIC_MATCHING_PACKET_BUILDER_VERSION:
        _fail("packet.builder_version", "is not supported")
    if packet["authority"] != semantic_non_exclusionary_authority():
        _fail("packet.authority", "must be the closed semantic authority state")
    if packet["construction_policy"] != {
        "evidence_authority": "accepted_server_authenticated_only",
        "variant_relationship_authority": "server_derived_only",
        "verifier_dependency": "none",
        "verifier_authority_effect": "none",
        "legacy_compatibility_authority_effect": "none",
    }:
        _fail("packet.construction_policy", "does not match boundary v1")
    identities = packet["identities"]
    _expect_exact_keys(
        identities,
        {
            "semantic_input_version",
            "semantic_input_sha256",
            "source_packet_sha256",
            "semantic_extraction_version",
            "semantic_grouping_version",
            "semantic_contract_version",
            "staging_version",
            "relation_builder_version",
        },
        "packet.identities",
    )
    _expect_nonempty_string(
        identities["semantic_input_version"],
        "packet.identities.semantic_input_version",
        maximum=128,
    )
    _expect_sha256(
        identities["semantic_input_sha256"],
        "packet.identities.semantic_input_sha256",
    )
    _expect_sha256(
        identities["source_packet_sha256"],
        "packet.identities.source_packet_sha256",
    )
    semantic_extraction_version = identities["semantic_extraction_version"]
    if semantic_extraction_version is None:
        if identities["staging_version"] is not None:
            _fail(
                "packet.identities.semantic_extraction_version",
                "is required for a staged packet",
            )
    else:
        _expect_nonempty_string(
            semantic_extraction_version,
            "packet.identities.semantic_extraction_version",
            maximum=128,
        )
    semantic_grouping_version = identities["semantic_grouping_version"]
    if semantic_grouping_version is not None:
        _expect_nonempty_string(
            semantic_grouping_version,
            "packet.identities.semantic_grouping_version",
            maximum=128,
        )
    if identities["semantic_contract_version"] != CONTRACT_VERSION:
        _fail("packet.identities.semantic_contract_version", "is not supported")
    for key in ("staging_version", "relation_builder_version"):
        value = identities[key]
        if value is not None:
            _expect_nonempty_string(value, f"packet.identities.{key}", maximum=128)

    opportunity_scope = packet["opportunity_scope"]
    _expect_exact_keys(
        opportunity_scope,
        {
            "canonical_ref",
            "known_variant_refs",
            "variant_mode",
            "scope_authority",
            "canonical_fact_promotion_allowed",
        },
        "packet.opportunity_scope",
    )
    expected_scope = semantic_opportunity_scope(
        canonical_ref=opportunity_scope["canonical_ref"],
        known_variant_refs=opportunity_scope["known_variant_refs"],
    )
    if opportunity_scope != expected_scope:
        _fail(
            "packet.opportunity_scope",
            "does not equal the closed server-owned opportunity scope",
        )

    sources = validate_accepted_evidence_catalog(packet["evidence_sources"])
    propositions = packet["propositions"]
    if type(propositions) is not list:
        _fail("packet.propositions", "must be a list")
    if len(propositions) > MAX_ATOMS:
        _fail("packet.propositions", f"must contain at most {MAX_ATOMS} items")
    proposition_ids = set()
    for index, item in enumerate(propositions):
        path = f"packet.propositions[{index}]"
        _expect_exact_keys(
            item,
            {
                "proposition_id",
                "atom_sha256",
                "meaning_sha256",
                "meaning",
                "normalized_value",
                "normalization",
                "evidence",
                "variant_relationships",
                "applicability",
                "status",
                "group_refs",
                "authority",
                "experimental_observation",
            },
            path,
        )
        proposition_id = _expect_nonempty_string(
            item["proposition_id"], f"{path}.proposition_id", maximum=64
        )
        if _PROPOSITION_ID_RE.fullmatch(proposition_id) is None or proposition_id in proposition_ids:
            _fail(f"{path}.proposition_id", "is invalid or duplicated")
        proposition_ids.add(proposition_id)
        _expect_sha256(item["atom_sha256"], f"{path}.atom_sha256")
        _expect_sha256(item["meaning_sha256"], f"{path}.meaning_sha256")
        meaning = item["meaning"]
        _expect_exact_keys(
            meaning,
            {"subject", "kind", "typed_payload", "polarity", "temporal"},
            f"{path}.meaning",
        )
        if meaning["subject"] not in SUBJECTS or meaning["kind"] not in ATOM_KINDS:
            _fail(f"{path}.meaning", "has an invalid subject or kind")
        if meaning["polarity"] not in POLARITIES or meaning["temporal"] not in TEMPORALS:
            _fail(f"{path}.meaning", "has invalid polarity or temporal meaning")
        if item["meaning_sha256"] != canonical_sha256(meaning):
            _fail(f"{path}.meaning_sha256", "does not authenticate immutable meaning")
        normalization = item["normalization"]
        _expect_exact_keys(
            normalization,
            {"status", "qualifier_complete", "source_values"},
            f"{path}.normalization",
        )
        if normalization["status"] not in NORMALIZATION_STATES:
            _fail(f"{path}.normalization.status", "is invalid")
        if type(normalization["qualifier_complete"]) is not bool:
            _fail(f"{path}.normalization.qualifier_complete", "must be boolean")
        if type(normalization["source_values"]) is not list:
            _fail(f"{path}.normalization.source_values", "must be a list")
        safely_resolved = (
            normalization["status"] == "resolved"
            and normalization["qualifier_complete"]
        )
        if safely_resolved != (item["normalized_value"] is not None):
            _fail(
                f"{path}.normalized_value",
                "must exist exactly when normalization is safely resolved",
            )
        if safely_resolved and item["normalized_value"] != (
            f"{meaning['kind']}:{canonical_json(meaning['typed_payload'])}"
        ):
            _fail(f"{path}.normalized_value", "does not match immutable meaning")
        evidence = item["evidence"]
        if (
            type(evidence) is not list
            or not evidence
            or len(evidence) > MAX_EVIDENCE_REFS_PER_ATOM
        ):
            _fail(f"{path}.evidence", "must be a non-empty list")
        for evidence_index, reference in enumerate(evidence):
            reference_path = f"{path}.evidence[{evidence_index}]"
            _expect_exact_keys(
                reference,
                {"source_id", "start", "end", "quote"},
                reference_path,
            )
            source = sources.get(reference["source_id"])
            if source is None:
                _fail(f"{reference_path}.source_id", "is not accepted evidence")
            start = reference["start"]
            end = reference["end"]
            if (
                type(start) is not int
                or type(end) is not int
                or start < 0
                or end <= start
                or end > len(source["text"])
                or source["text"][start:end] != reference["quote"]
            ):
                _fail(reference_path, "does not preserve an exact evidence span")
        immutable_atom = {
            "id": proposition_id,
            **copy.deepcopy(meaning),
            "evidence": copy.deepcopy(evidence),
        }
        if item["atom_sha256"] != contract_atom_sha256(immutable_atom):
            _fail(f"{path}.atom_sha256", "does not authenticate immutable atom")
        for source_value_index, source_value in enumerate(
            normalization["source_values"]
        ):
            source_value_path = (
                f"{path}.normalization.source_values[{source_value_index}]"
            )
            _expect_exact_keys(
                source_value,
                {
                    "field",
                    "raw_value",
                    "normalized_value",
                    "normalization_status",
                    "source_id",
                    "start",
                    "end",
                    "quote_sha256",
                },
                source_value_path,
            )
            source = sources.get(source_value["source_id"])
            if source is None:
                _fail(f"{source_value_path}.source_id", "is not accepted evidence")
            start = source_value["start"]
            end = source_value["end"]
            if (
                type(start) is not int
                or type(end) is not int
                or start < 0
                or end <= start
                or end > len(source["text"])
                or source["text"][start:end] != source_value["raw_value"]
            ):
                _fail(source_value_path, "does not preserve authenticated raw value")
            if source_value["quote_sha256"] != source["text_sha256"]:
                _fail(f"{source_value_path}.quote_sha256", "does not authenticate quote")
            expected_value_status = (
                "resolved"
                if source_value["normalized_value"] is not None
                else "unmapped"
            )
            if source_value["normalization_status"] != expected_value_status:
                _fail(f"{source_value_path}.normalization_status", "is inconsistent")
        normalized_relationships = _validate_variant_relationships(
            item["variant_relationships"], set(sources)
        )
        if any(
            not set(relationship["variant_refs"])
            <= set(opportunity_scope["known_variant_refs"])
            for relationship in normalized_relationships
        ):
            _fail(f"{path}.variant_relationships", "names an unknown variant")
        if not {
            relationship["source_id"]
            for relationship in item["variant_relationships"]
        } <= {reference["source_id"] for reference in evidence}:
            _fail(f"{path}.variant_relationships", "is not evidence-linked")
        _validate_semantic_applicability(
            item["applicability"],
            relationships=normalized_relationships,
            opportunity_scope=opportunity_scope,
            path=f"{path}.applicability",
        )
        status = item["status"]
        _expect_exact_keys(
            status, {"semantic_support", "completeness"}, f"{path}.status"
        )
        if status["semantic_support"] not in SEMANTIC_SUPPORT_STATES:
            _fail(f"{path}.status.semantic_support", "is invalid")
        if status["completeness"] not in COMPLETENESS_STATES:
            _fail(f"{path}.status.completeness", "is invalid")
        if status["completeness"] == "complete" and not (
            safely_resolved
            and status["semantic_support"]
            in {"accepted_evidence_validated", "supported"}
        ):
            _fail(f"{path}.status.completeness", "is not safely complete")
        if (
            type(item["group_refs"]) is not list
            or item["group_refs"] != sorted(set(item["group_refs"]))
        ):
            _fail(f"{path}.group_refs", "must be sorted and unique")
        if item["authority"] != semantic_non_exclusionary_authority():
            _fail(f"{path}.authority", "must be semantic non-exclusionary")
        observation = item["experimental_observation"]
        _expect_exact_keys(
            observation,
            {"semantic_decision", "legacy_assurance", "authority_effect"},
            f"{path}.experimental_observation",
        )
        if observation["authority_effect"] != "none":
            _fail(f"{path}.experimental_observation.authority_effect", "must be none")
        if observation["semantic_decision"] not in EXPERIMENTAL_SEMANTIC_DECISIONS:
            _fail(f"{path}.experimental_observation.semantic_decision", "is invalid")
        if observation["legacy_assurance"] not in EXPERIMENTAL_LEGACY_ASSURANCES:
            _fail(f"{path}.experimental_observation.legacy_assurance", "is invalid")
        expected_support = {
            "contradicts": "contradicted",
            "entails": "supported",
            "not_established": "not_established",
            "not_supplied": "accepted_evidence_validated",
            "pending": "pending",
        }[observation["semantic_decision"]]
        if status["semantic_support"] != expected_support:
            _fail(f"{path}.status.semantic_support", "does not match observation")

    descriptive_signals = packet["descriptive_signals"]
    if type(descriptive_signals) is not list:
        _fail("packet.descriptive_signals", "must be a list")
    if len(descriptive_signals) > MAX_DESCRIPTIVE_SIGNALS:
        _fail(
            "packet.descriptive_signals",
            f"must contain at most {MAX_DESCRIPTIVE_SIGNALS} items",
        )
    signal_ids = set()
    candidate_profile_count = 0
    for index, signal in enumerate(descriptive_signals):
        path = f"packet.descriptive_signals[{index}]"
        _expect_exact_keys(
            signal,
            {
                "signal_id",
                "kind",
                "value",
                "evidence_source_ids",
                "signal_sha256",
                "variant_relationships",
                "applicability",
                "authority",
            },
            path,
        )
        signal_id = _expect_nonempty_string(
            signal["signal_id"], f"{path}.signal_id", maximum=64
        )
        if _PROPOSITION_ID_RE.fullmatch(signal_id) is None or signal_id in signal_ids:
            _fail(f"{path}.signal_id", "is invalid or duplicated")
        signal_ids.add(signal_id)
        if signal["kind"] not in DESCRIPTIVE_SIGNAL_KINDS:
            _fail(f"{path}.kind", "is invalid")
        candidate_profile_count += signal["kind"] == "candidate_profile"
        if candidate_profile_count > 1:
            _fail("packet.descriptive_signals", "contains multiple candidate profiles")
        _expect_nonempty_string(signal["value"], f"{path}.value", maximum=4000)
        source_ids = signal["evidence_source_ids"]
        if (
            type(source_ids) is not list
            or not source_ids
            or source_ids != sorted(set(source_ids))
            or not set(source_ids) <= set(sources)
        ):
            _fail(f"{path}.evidence_source_ids", "must name accepted evidence")
        identity = {
            "signal_id": signal_id,
            "kind": signal["kind"],
            "value": signal["value"],
            "evidence_source_ids": source_ids,
        }
        if signal["signal_sha256"] != canonical_sha256(identity):
            _fail(f"{path}.signal_sha256", "does not authenticate signal")
        normalized_relationships = _validate_variant_relationships(
            signal["variant_relationships"], set(sources)
        )
        if any(
            not set(relationship["variant_refs"])
            <= set(opportunity_scope["known_variant_refs"])
            for relationship in normalized_relationships
        ):
            _fail(f"{path}.variant_relationships", "names an unknown variant")
        _validate_semantic_applicability(
            signal["applicability"],
            relationships=normalized_relationships,
            opportunity_scope=opportunity_scope,
            path=f"{path}.applicability",
        )
        if signal["authority"] != semantic_non_exclusionary_authority():
            _fail(f"{path}.authority", "must be semantic non-exclusionary")

    groups = packet["groups"]
    if type(groups) is not list:
        _fail("packet.groups", "must be a list")
    if len(groups) > MAX_CONSTRAINT_GROUPS:
        _fail("packet.groups", f"must contain at most {MAX_CONSTRAINT_GROUPS} items")
    group_ids = set()
    for index, group in enumerate(groups):
        path = f"packet.groups[{index}]"
        _expect_exact_keys(
            group,
            {
                "group_id",
                "group_sha256",
                "modality",
                "server_derived_modality",
                "logic",
                "raw_proposal",
                "referenced_proposition_ids",
                "relation_state",
                "completeness",
                "reason_codes",
                "source_branches",
                "source_logic_exact",
                "variant_relationships",
                "applicability",
                "authority",
                "experimental_observation",
            },
            path,
        )
        group_id = _expect_nonempty_string(group["group_id"], f"{path}.group_id")
        if group_id in group_ids:
            _fail(f"{path}.group_id", "must be unique")
        group_ids.add(group_id)
        if group["group_sha256"] != canonical_sha256(group["raw_proposal"]):
            _fail(f"{path}.group_sha256", "does not authenticate raw proposal")
        if group["modality"] is not None and group["modality"] not in MODALITIES:
            _fail(f"{path}.modality", "is invalid")
        if (
            group["server_derived_modality"] is not None
            and group["server_derived_modality"] not in MODALITIES
        ):
            _fail(f"{path}.server_derived_modality", "is invalid")
        if group["relation_state"] not in RELATION_STATES:
            _fail(f"{path}.relation_state", "is invalid")
        expected_completeness = {
            "complete_validated": "complete",
            "complete_verified": "complete",
            "grounded_incomplete": "incomplete",
            "invalid_proposal": "invalid",
            "unrepresentable_relation": "unresolved",
        }[group["relation_state"]]
        if group["completeness"] != expected_completeness:
            _fail(f"{path}.completeness", "does not match relation state")
        if group["logic"] is not None:
            _expect_exact_keys(group["logic"], {"form", "any_of"}, f"{path}.logic")
            if group["logic"]["form"] != "bounded_dnf":
                _fail(f"{path}.logic.form", "must be bounded_dnf")
            alternatives = group["logic"]["any_of"]
            if (
                type(alternatives) is not list
                or not alternatives
                or len(alternatives) > MAX_ALTERNATIVES_PER_GROUP
            ):
                _fail(f"{path}.logic.any_of", "is outside bounded DNF")
            for alternative_index, alternative in enumerate(alternatives):
                alternative_path = (
                    f"{path}.logic.any_of[{alternative_index}]"
                )
                _expect_exact_keys(
                    alternative, {"all_of"}, alternative_path
                )
                conjunction = alternative["all_of"]
                if (
                    type(conjunction) is not list
                    or not conjunction
                    or len(conjunction) > MAX_ATOMS_PER_CONJUNCTION
                    or any(type(atom_id) is not str for atom_id in conjunction)
                    or len(conjunction) != len(set(conjunction))
                ):
                    _fail(f"{alternative_path}.all_of", "is outside bounded DNF")
            raw_any_of = (
                group["raw_proposal"].get("any_of")
                if type(group["raw_proposal"]) is dict
                else None
            )
            if group["logic"]["any_of"] != raw_any_of:
                _fail(f"{path}.logic.any_of", "must preserve raw AND/OR structure")
            if group["modality"] != group["raw_proposal"].get("modality"):
                _fail(f"{path}.modality", "must preserve raw modality")
        if (
            group["completeness"] == "complete"
            and group["server_derived_modality"] != group["modality"]
        ):
            _fail(
                f"{path}.server_derived_modality",
                "must equal complete semantic modality",
            )
        refs = group["referenced_proposition_ids"]
        if type(refs) is not list or refs != sorted(set(refs)):
            _fail(f"{path}.referenced_proposition_ids", "must be sorted and unique")
        if type(group["reason_codes"]) is not list or group["reason_codes"] != sorted(set(group["reason_codes"])):
            _fail(f"{path}.reason_codes", "must be sorted and unique")
        if type(group["source_branches"]) is not list:
            _fail(f"{path}.source_branches", "must be a list")
        if len(group["source_branches"]) > MAX_SOURCE_BRANCHES_PER_GROUP:
            _fail(f"{path}.source_branches", "is outside the packet bound")
        source_branch_ids = set()
        for branch_index, branch in enumerate(group["source_branches"]):
            branch_path = f"{path}.source_branches[{branch_index}]"
            _expect_exact_keys(
                branch,
                {
                    "source_branch_id",
                    "source_values",
                    "proposed_atom_ids",
                    "coverage_state",
                },
                branch_path,
            )
            source_branch_id = _expect_nonempty_string(
                branch["source_branch_id"],
                f"{branch_path}.source_branch_id",
                maximum=128,
            )
            if source_branch_id in source_branch_ids:
                _fail(f"{branch_path}.source_branch_id", "must be unique")
            source_branch_ids.add(source_branch_id)
            proposed_atom_ids = branch["proposed_atom_ids"]
            if (
                type(proposed_atom_ids) is not list
                or len(proposed_atom_ids) != len(set(proposed_atom_ids))
                or any(type(atom_id) is not str or not atom_id for atom_id in proposed_atom_ids)
            ):
                _fail(
                    f"{branch_path}.proposed_atom_ids",
                    "must be a unique string list",
                )
            if branch["coverage_state"] not in SOURCE_BRANCH_STATES:
                _fail(f"{branch_path}.coverage_state", "is invalid")
            if branch["coverage_state"] == "matched" and not proposed_atom_ids:
                _fail(
                    f"{branch_path}.proposed_atom_ids",
                    "must be non-empty for a matched branch",
                )
            source_values = branch["source_values"]
            if type(source_values) is not list:
                _fail(f"{branch_path}.source_values", "must be a list")
            for source_value_index, source_value in enumerate(source_values):
                source_value_path = (
                    f"{branch_path}.source_values[{source_value_index}]"
                )
                _expect_exact_keys(
                    source_value,
                    {
                        "field",
                        "raw_value",
                        "normalized_value",
                        "normalization_status",
                        "source_id",
                        "start",
                        "end",
                        "quote_sha256",
                    },
                    source_value_path,
                )
                source = sources.get(source_value["source_id"])
                if source is None:
                    _fail(
                        f"{source_value_path}.source_id",
                        "is not accepted evidence",
                    )
                start = source_value["start"]
                end = source_value["end"]
                if (
                    type(start) is not int
                    or type(end) is not int
                    or start < 0
                    or end <= start
                    or end > len(source["text"])
                    or source["text"][start:end] != source_value["raw_value"]
                ):
                    _fail(
                        source_value_path,
                        "does not preserve authenticated raw value",
                    )
                if source_value["quote_sha256"] != source["text_sha256"]:
                    _fail(
                        f"{source_value_path}.quote_sha256",
                        "does not authenticate quote",
                    )
                expected_value_status = (
                    "resolved"
                    if source_value["normalized_value"] is not None
                    else "unmapped"
                )
                if source_value["normalization_status"] != expected_value_status:
                    _fail(
                        f"{source_value_path}.normalization_status",
                        "is inconsistent",
                    )
        normalized_relationships = _validate_variant_relationships(
            group["variant_relationships"], set(sources)
        )
        if any(
            not set(relationship["variant_refs"])
            <= set(opportunity_scope["known_variant_refs"])
            for relationship in normalized_relationships
        ):
            _fail(f"{path}.variant_relationships", "names an unknown variant")
        linked_sources = {
            relationship["source_id"]
            for proposition in propositions
            if proposition["proposition_id"] in refs
            for relationship in proposition["variant_relationships"]
        }
        if not {
            relationship["source_id"]
            for relationship in group["variant_relationships"]
        } <= linked_sources:
            _fail(f"{path}.variant_relationships", "is not proposition-linked")
        _validate_semantic_applicability(
            group["applicability"],
            relationships=normalized_relationships,
            opportunity_scope=opportunity_scope,
            path=f"{path}.applicability",
        )
        if group["authority"] != semantic_non_exclusionary_authority():
            _fail(f"{path}.authority", "must be semantic non-exclusionary")
        observation = group["experimental_observation"]
        _expect_exact_keys(
            observation,
            {"legacy_relation_state", "authority_effect"},
            f"{path}.experimental_observation",
        )
        if observation["authority_effect"] != "none":
            _fail(f"{path}.experimental_observation.authority_effect", "must be none")
        if observation["legacy_relation_state"] not in {
            "not_supplied",
            group["relation_state"],
        }:
            _fail(f"{path}.experimental_observation.legacy_relation_state", "is invalid")

    for proposition in propositions:
        if not set(proposition["group_refs"]) <= group_ids:
            _fail("packet.propositions.group_refs", "references an unknown group")
    groups_by_id = {group["group_id"]: group for group in groups}
    for proposition in propositions:
        for group_ref in proposition["group_refs"]:
            if proposition["proposition_id"] not in groups_by_id[group_ref][
                "referenced_proposition_ids"
            ]:
                _fail("packet.propositions.group_refs", "is not bidirectional")
    unassigned = packet["unassigned_proposition_ids"]
    if type(unassigned) is not list or unassigned != sorted(set(unassigned)):
        _fail("packet.unassigned_proposition_ids", "must be sorted and unique")
    if not set(unassigned) <= proposition_ids:
        _fail("packet.unassigned_proposition_ids", "contains an unknown proposition")
    invalid = packet["retained_invalid_proposals"]
    if type(invalid) is not list:
        _fail("packet.retained_invalid_proposals", "must be a list")
    for index, item in enumerate(invalid):
        path = f"packet.retained_invalid_proposals[{index}]"
        _expect_exact_keys(
            item,
            {
                "ledger_id",
                "proposal_id",
                "raw_proposal_sha256",
                "raw_proposal",
                "retention_state",
                "reason_codes",
                "applicability",
                "authority",
            },
            path,
        )
        if item["retention_state"] != "structurally_invalid":
            _fail(f"{path}.retention_state", "must be structurally_invalid")
        if item["raw_proposal_sha256"] != canonical_sha256(item["raw_proposal"]):
            _fail(f"{path}.raw_proposal_sha256", "does not authenticate raw proposal")
        _validate_semantic_applicability(
            item["applicability"],
            relationships=[],
            opportunity_scope=opportunity_scope,
            path=f"{path}.applicability",
        )
        if item["authority"] != semantic_non_exclusionary_authority():
            _fail(f"{path}.authority", "must be semantic non-exclusionary")

    expected_accounting = _packet_accounting(packet)
    if packet["accounting"] != expected_accounting:
        _fail("packet.accounting", "does not match packet contents")
    if packet["accounting"]["semantic_hard_exclusion_count"] != 0:
        _fail("packet.accounting.semantic_hard_exclusion_count", "must be zero")
    if packet["accounting"]["canonical_semantic_fact_claim_count"] != 0:
        _fail(
            "packet.accounting.canonical_semantic_fact_claim_count",
            "must be zero",
        )
    _expect_sha256(packet["packet_sha256"], "packet.packet_sha256")
    if packet["packet_sha256"] != semantic_matching_packet_sha256(packet):
        _fail("packet.packet_sha256", "does not authenticate packet")
    return copy.deepcopy(packet)

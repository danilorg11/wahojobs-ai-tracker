"""Read-only OE Semantic Shadow Packet Integration v1.

This module is the sole Opportunity Enrichment orchestration seam for building
``oe_semantic_matching_packet_v1``.  It is deliberately not imported by the
matching runtime, candidate presentation, lifecycle/trust code, or persistence
services.  Construction reads accepted opportunity evidence and returns an
inert shadow result; it never writes a packet or routes semantic data through
legacy enrichment compatibility fields.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
from collections import Counter

from wahojobs.opportunity_enrichment import (
    SEMANTIC_INPUT_VERSION,
    EnrichmentValidationError,
    extract_deterministic_objective_facts,
    llm_source_packet,
    load_semantic_input,
    semantic_input_sha256,
    semantic_variant_refs,
    validate_enrichment_document,
)
from wahojobs.opportunity_semantic_authority import (
    OpportunitySemanticAuthorityError,
    build_semantic_matching_packet_from_staging,
    canonical_sha256,
    derive_server_variant_relationships,
)
from wahojobs.opportunity_semantic_contract import (
    SemanticContractValidationError,
)
from wahojobs.opportunity_semantic_extraction import (
    DEFAULT_MODEL as SEMANTIC_EXTRACTION_MODEL,
    EXTRACTION_CONTRACT_VERSION,
    REASONING_EFFORT as SEMANTIC_EXTRACTION_REASONING_EFFORT,
    SemanticExtractionError,
    SemanticExtractionValidationError,
    accepted_evidence_aliases,
)
from wahojobs.opportunity_semantic_staging import (
    SemanticStagingValidationError,
    construct_relations,
    stage_provisional_atoms,
)


SHADOW_PACKET_INTEGRATION_VERSION = "oe_semantic_shadow_packet_integration_v1"
SHADOW_PACKET_RESULT_VERSION = "oe_semantic_shadow_packet_result_v1"
SHADOW_EXECUTION_MODE = "shadow_offline_only"

SHADOW_STATUSES = frozenset({"available", "unavailable", "invalid"})
SEMANTIC_EXTRACTION_SOURCES = frozenset({"frozen_artifact", "provider_shadow"})
_CAPTURE_REF_RE = re.compile(r"^source_capture:(\d+)$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ShadowPacketConstructionError(ValueError):
    """An expected fail-closed shadow construction condition."""

    def __init__(self, code: str, stage: str, message: str):
        super().__init__(message)
        self.code = code
        self.stage = stage


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _table_exists(connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _accepted_capture_bindings(
    connection,
    canonical_opportunity_id: int,
    semantic_input: dict,
    source_packet: dict,
) -> list[dict]:
    """Bind accepted packet aliases to server-owned capture/job provenance."""

    job_ids = {
        f"source_hash:{row['source_hash']}": int(row["id"])
        for row in connection.execute(
            """
            SELECT id, source_hash
            FROM jobs
            WHERE canonical_opportunity_id = ?
              AND title NOT LIKE '[SIMULATION]%'
            """,
            (canonical_opportunity_id,),
        ).fetchall()
    }
    sources = {
        source["source_ref"]: source
        for source in semantic_input.get("rich_content") or []
    }
    bindings = []
    for block in source_packet.get("evidence_blocks") or []:
        if block.get("authority_class") != "accepted_body_evidence":
            continue
        candidates = []
        for source_ref in block.get("source_refs") or [block.get("source_ref")]:
            source = sources.get(source_ref)
            authority = (source or {}).get("authority") or {}
            capture_ref = authority.get("accepted_capture_ref")
            match = (
                _CAPTURE_REF_RE.fullmatch(capture_ref)
                if type(capture_ref) is str
                else None
            )
            if (
                source is not None
                and authority.get("semantic_authority_state")
                == "versioned_accepted"
                and match is not None
                and source_ref in job_ids
            ):
                candidates.append((source_ref, source, int(match.group(1))))
        if not candidates:
            raise ShadowPacketConstructionError(
                "accepted_capture_binding_unavailable",
                "accepted_evidence_binding",
                "An accepted body evidence block lacks versioned capture authority.",
            )
        source_ref, source, accepted_capture_id = sorted(
            candidates, key=lambda item: item[0]
        )[0]
        material_hash = source.get("material_content_sha256")
        source_url = source.get("source_url")
        if (
            type(material_hash) is not str
            or _SHA256_RE.fullmatch(material_hash) is None
            or type(source_url) is not str
            or not source_url.startswith("https://")
        ):
            raise ShadowPacketConstructionError(
                "accepted_capture_provenance_invalid",
                "accepted_evidence_binding",
                "Accepted capture provenance is incomplete or malformed.",
            )
        text = block.get("content")
        if type(text) is not str or not text:
            raise ShadowPacketConstructionError(
                "accepted_evidence_text_invalid",
                "accepted_evidence_binding",
                "An accepted body evidence block has no text.",
            )
        bindings.append(
            {
                "alias": block["evidence_block_id"],
                "authority": "accepted_capture",
                "text": text,
                "text_sha256": _sha256_text(text),
                "provenance": {
                    "accepted_capture_id": accepted_capture_id,
                    "job_id": job_ids[source_ref],
                    "material_content_sha256": material_hash,
                    "source_url": source_url,
                },
            }
        )
    return sorted(bindings, key=lambda item: item["alias"])


def _accepted_source_for_full_block(block: dict, binding: dict) -> dict:
    text = block["content"]
    identity = {
        "alias": block["evidence_block_id"],
        "quote": text,
        "authority": binding["authority"],
        "provenance": binding["provenance"],
    }
    return {
        "id": f"extract:{canonical_sha256(identity)[:40]}",
        "authority": binding["authority"],
        "text": text,
        "text_sha256": _sha256_text(text),
        "provenance": copy.deepcopy(binding["provenance"]),
    }


def _native_descriptive_signals(
    connection,
    canonical_opportunity_id: int,
    *,
    semantic_input_sha: str,
    source_packet: dict,
    bindings: list[dict],
) -> tuple[list[dict], list[dict], dict]:
    """Read only current automatic opportunity prose with accepted provenance.

    Requirement, location, legacy-patch, and ``variant_facts`` fields are never
    read here.  Unsupported or stale prose is omitted with an explicit status.
    """

    availability = {
        "responsibilities": {
            "status": "unavailable",
            "count": 0,
            "reason": "current_native_enrichment_not_available",
        },
        "candidate_profile": {
            "status": "unavailable",
            "count": 0,
            "reason": "current_native_enrichment_not_available",
        },
    }
    if not _table_exists(connection, "opportunity_enrichments"):
        return [], [], availability
    row = connection.execute(
        """
        SELECT input_sha256, semantic_input_version, automatic_document_json
        FROM opportunity_enrichments
        WHERE canonical_opportunity_id = ?
        """,
        (canonical_opportunity_id,),
    ).fetchone()
    if row is None:
        return [], [], availability
    if (
        row["input_sha256"] != semantic_input_sha
        or row["semantic_input_version"] != SEMANTIC_INPUT_VERSION
    ):
        for value in availability.values():
            value["reason"] = "native_enrichment_is_stale_or_unversioned"
        return [], [], availability
    try:
        document = json.loads(row["automatic_document_json"])
        validate_enrichment_document(document)
    except (TypeError, json.JSONDecodeError, EnrichmentValidationError):
        for value in availability.values():
            value["reason"] = "native_enrichment_document_invalid"
        return [], [], availability

    blocks = {
        block["evidence_block_id"]: block
        for block in source_packet.get("evidence_blocks") or []
        if block.get("authority_class") == "accepted_body_evidence"
    }
    bindings_by_alias = {binding["alias"]: binding for binding in bindings}
    evidence_by_path = {}
    for evidence in document.get("field_evidence") or []:
        path = evidence.get("field_path")
        alias = evidence.get("evidence_block_id")
        if (
            path
            in {
                "attributes.content.responsibilities",
                "attributes.content.candidate_profile",
            }
            and evidence.get("basis") == "llm_source_evidence"
            and evidence.get("confidence") == "high"
            and alias in blocks
            and alias in bindings_by_alias
            and evidence.get("evidence_text") == blocks[alias].get("content")
        ):
            evidence_by_path.setdefault(path, set()).add(alias)

    signals = []
    source_records = {}
    content = document["attributes"]["content"]
    signal_specs = []
    for index, value in enumerate(content.get("responsibilities") or [], start=1):
        signal_specs.append(
            (
                f"responsibility_{index:03d}",
                "responsibility",
                value,
                "attributes.content.responsibilities",
            )
        )
    if content.get("candidate_profile"):
        signal_specs.append(
            (
                "candidate_profile",
                "candidate_profile",
                content["candidate_profile"],
                "attributes.content.candidate_profile",
            )
        )

    accepted_counts = Counter()
    for signal_id, kind, value, field_path in signal_specs:
        aliases = sorted(evidence_by_path.get(field_path) or [])
        evidence_source_ids = []
        for alias in aliases:
            source = _accepted_source_for_full_block(
                blocks[alias], bindings_by_alias[alias]
            )
            # Full-block semantic sources remain bounded by the accepted catalog.
            if len(source["text"]) > 2000:
                continue
            source_records[source["id"]] = source
            evidence_source_ids.append(source["id"])
        evidence_source_ids = sorted(set(evidence_source_ids))
        if not evidence_source_ids:
            continue
        signals.append(
            {
                "signal_id": signal_id,
                "kind": kind,
                "value": value,
                "evidence_source_ids": evidence_source_ids,
            }
        )
        accepted_counts[kind] += 1

    requested_counts = Counter(kind for _id, kind, _value, _path in signal_specs)
    for key, kind in (
        ("responsibilities", "responsibility"),
        ("candidate_profile", "candidate_profile"),
    ):
        if accepted_counts[kind]:
            availability[key] = {
                "status": "available",
                "count": accepted_counts[kind],
                "reason": None,
            }
        elif requested_counts[kind]:
            availability[key] = {
                "status": "unavailable",
                "count": 0,
                "reason": "accepted_evidence_provenance_unavailable",
            }
        else:
            availability[key] = {
                "status": "not_present",
                "count": 0,
                "reason": None,
            }
    return (
        sorted(signals, key=lambda item: item["signal_id"]),
        [source_records[key] for key in sorted(source_records)],
        availability,
    )


def _semantic_extraction_payload(
    source_packet: dict,
    *,
    extraction_payload,
    extraction_client,
) -> tuple[dict, dict]:
    if extraction_payload is not None and extraction_client is not None:
        raise ShadowPacketConstructionError(
            "ambiguous_semantic_extraction_input",
            "semantic_extraction",
            "Supply either a frozen extraction payload or a shadow client, not both.",
        )
    if extraction_payload is not None:
        return copy.deepcopy(extraction_payload), {
            "source": "frozen_artifact",
            "provider_called": False,
            "model": None,
            "reasoning_effort": None,
            "store": None,
        }
    if extraction_client is None:
        raise ShadowPacketConstructionError(
            "semantic_extraction_not_supplied",
            "semantic_extraction",
            "No frozen extraction payload or explicit shadow extraction client was supplied.",
        )
    if (
        getattr(extraction_client, "provider", None) != "openai"
        or getattr(extraction_client, "model", None) != SEMANTIC_EXTRACTION_MODEL
        or getattr(extraction_client, "reasoning_effort", None)
        != SEMANTIC_EXTRACTION_REASONING_EFFORT
    ):
        raise ShadowPacketConstructionError(
            "semantic_extraction_configuration_invalid",
            "semantic_extraction",
            "The shadow extraction client does not match the pinned configuration.",
        )
    extraction_result = extraction_client.extract(source_packet)
    return copy.deepcopy(extraction_result.payload), {
        "source": "provider_shadow",
        "provider_called": bool(extraction_result.provider_called),
        "model": extraction_result.response_model,
        "reasoning_effort": SEMANTIC_EXTRACTION_REASONING_EFFORT,
        "store": False,
        "response_id": extraction_result.response_id,
        "response_status": extraction_result.response_status,
    }


def _packet_metrics(packet: dict, signal_availability: dict) -> dict:
    source_values = [
        source_value
        for proposition in packet["propositions"]
        for source_value in proposition["normalization"]["source_values"]
    ]
    semantic_items = [
        *packet["propositions"],
        *packet["groups"],
        *packet["descriptive_signals"],
        *packet["retained_invalid_proposals"],
    ]
    return {
        "packet_validation_success": True,
        "proposition_count": len(packet["propositions"]),
        "complete_relation_count": packet["accounting"]["complete_group_count"],
        "incomplete_or_unresolved_relation_count": packet["accounting"]
        ["incomplete_or_unresolved_group_count"],
        "normalized_source_value_count": sum(
            value["normalized_value"] is not None for value in source_values
        ),
        "raw_or_unmapped_source_value_count": sum(
            value["normalized_value"] is None for value in source_values
        ),
        "raw_source_value_count": len(source_values),
        "variant_mode": packet["opportunity_scope"]["variant_mode"],
        "applicability_counts": dict(
            sorted(
                Counter(
                    item["applicability"]["variant_applicability"]
                    for item in semantic_items
                ).items()
            )
        ),
        "source_branch_count": packet["accounting"]["source_branch_count"],
        "unresolved_source_branch_count": packet["accounting"]
        ["unresolved_source_branch_count"],
        "evidence_source_count": packet["accounting"]["evidence_source_count"],
        "evidence_provenance_integrity": True,
        "descriptive_signal_availability": copy.deepcopy(signal_availability),
        "semantic_authority_type": packet["authority"]["authority_type"],
        "semantic_hard_exclusion_count": packet["accounting"]
        ["semantic_hard_exclusion_count"],
        "canonical_semantic_fact_claim_count": packet["accounting"]
        ["canonical_semantic_fact_claim_count"],
    }


def _base_result(canonical_opportunity_id: int) -> dict:
    return {
        "result_version": SHADOW_PACKET_RESULT_VERSION,
        "integration_version": SHADOW_PACKET_INTEGRATION_VERSION,
        "execution_mode": SHADOW_EXECUTION_MODE,
        "canonical_opportunity_id": canonical_opportunity_id,
        "status": None,
        "packet": None,
        "failure": None,
        "semantic_extraction": None,
        "objective_deterministic_context": None,
        "metrics": None,
        "isolation": {
            "runtime_consumption_authorized": False,
            "matcher_consumption_authorized": False,
            "ranking_consumption_authorized": False,
            "candidate_ui_consumption_authorized": False,
            "lifecycle_or_trust_effect_authorized": False,
            "database_persistence_authorized": False,
            "legacy_compatibility_routing_authorized": False,
        },
    }


def _failed_result(
    canonical_opportunity_id: int,
    *,
    status: str,
    code: str,
    stage: str,
    message: str,
) -> dict:
    if status not in SHADOW_STATUSES - {"available"}:
        raise AssertionError("invalid fail-closed status")
    result = _base_result(canonical_opportunity_id)
    result["status"] = status
    result["failure"] = {
        "code": code,
        "stage": stage,
        "message": message,
    }
    return result


def construct_oe_semantic_matching_packet_v1_shadow(
    connection,
    canonical_opportunity_id: int,
    *,
    extraction_payload=None,
    grouping_payload=None,
    extraction_client=None,
) -> dict:
    """Construct and validate one packet through the read-only OE shadow seam.

    Callers provide only a canonical identity plus either frozen model output or
    an explicitly configured shadow extraction client.  Evidence bindings,
    staging, relation states, opportunity scope, provenance/version identities,
    semantic authority, native descriptive signals, and complete-packet
    validation are owned here.  Expected failures return no partial packet.
    """

    if type(canonical_opportunity_id) is not int or canonical_opportunity_id <= 0:
        return _failed_result(
            canonical_opportunity_id,
            status="invalid",
            code="canonical_opportunity_id_invalid",
            stage="input",
            message="canonical_opportunity_id must be a positive integer.",
        )
    try:
        semantic_input = load_semantic_input(connection, canonical_opportunity_id)
        known_variant_refs = semantic_variant_refs(semantic_input)
        if not known_variant_refs:
            raise ShadowPacketConstructionError(
                "server_variant_scope_unavailable",
                "source_evidence",
                "The canonical has no server-derived variants.",
            )
        semantic_sha = semantic_input_sha256(semantic_input)
        source_packet, _evidence_blocks = llm_source_packet(semantic_input)
        aliases = accepted_evidence_aliases(source_packet)
        if not aliases:
            raise ShadowPacketConstructionError(
                "accepted_body_evidence_unavailable",
                "source_evidence",
                "No accepted body evidence is available for semantic extraction.",
            )
        bindings = _accepted_capture_bindings(
            connection,
            canonical_opportunity_id,
            semantic_input,
            source_packet,
        )
        if sorted(binding["alias"] for binding in bindings) != aliases:
            raise ShadowPacketConstructionError(
                "accepted_evidence_binding_incomplete",
                "accepted_evidence_binding",
                "Accepted evidence alias binding is incomplete.",
            )

        # Objective extraction is invoked for OE flow continuity, then retained
        # outside the semantic packet and outside any eligibility consumer.
        objective_facts = extract_deterministic_objective_facts(semantic_input)
        objective_context = {
            "extractor": "existing_oe_deterministic_objective_extraction",
            "fact_count": len(objective_facts),
            "facts_sha256": canonical_sha256(objective_facts),
            "included_in_semantic_packet": False,
            "eligibility_consumer_invoked": False,
        }

        model_payload, extraction_metadata = _semantic_extraction_payload(
            source_packet,
            extraction_payload=extraction_payload,
            extraction_client=extraction_client,
        )
        if type(model_payload) is not dict:
            raise SemanticExtractionValidationError(
                "semantic extraction payload must be an object"
            )
        if model_payload.get("extraction_version") != EXTRACTION_CONTRACT_VERSION:
            raise ShadowPacketConstructionError(
                "semantic_extraction_version_invalid",
                "semantic_extraction",
                "The semantic extraction payload has an unsupported version.",
            )
        staging = stage_provisional_atoms(model_payload, source_packet, bindings)
        relations = construct_relations(staging, grouping_payload)

        descriptive_signals, descriptive_sources, signal_availability = (
            _native_descriptive_signals(
                connection,
                canonical_opportunity_id,
                semantic_input_sha=semantic_sha,
                source_packet=source_packet,
                bindings=bindings,
            )
        )
        sources_by_id = {
            source["id"]: source
            for source in staging["accepted_evidence_catalog"]
        }
        for source in descriptive_sources:
            prior = sources_by_id.get(source["id"])
            if prior is not None and prior != source:
                raise ShadowPacketConstructionError(
                    "descriptive_source_identity_collision",
                    "descriptive_signals",
                    "Native descriptive evidence identity is inconsistent.",
                )
            sources_by_id[source["id"]] = source
        staging = copy.deepcopy(staging)
        staging["accepted_evidence_catalog"] = [
            sources_by_id[key] for key in sorted(sources_by_id)
        ]

        relationships = derive_server_variant_relationships(
            source_packet,
            bindings,
            staging["accepted_evidence_catalog"],
        )
        packet = build_semantic_matching_packet_from_staging(
            staging,
            relations,
            canonical_ref=f"canonical_opportunity:{canonical_opportunity_id}",
            known_variant_refs=known_variant_refs,
            semantic_input_version=SEMANTIC_INPUT_VERSION,
            semantic_input_sha256=semantic_sha,
            source_packet_sha256=canonical_sha256(source_packet),
            semantic_extraction_version=model_payload["extraction_version"],
            semantic_grouping_version=(
                grouping_payload.get("grouping_version")
                if type(grouping_payload) is dict
                else None
            ),
            variant_relationships=relationships,
            descriptive_signals=descriptive_signals,
        )
        result = _base_result(canonical_opportunity_id)
        result.update(
            {
                "status": "available",
                "packet": packet,
                "semantic_extraction": extraction_metadata,
                "objective_deterministic_context": objective_context,
                "metrics": _packet_metrics(packet, signal_availability),
            }
        )
        return result
    except ShadowPacketConstructionError as exc:
        return _failed_result(
            canonical_opportunity_id,
            status="unavailable",
            code=exc.code,
            stage=exc.stage,
            message=str(exc),
        )
    except (
        EnrichmentValidationError,
        SemanticContractValidationError,
        SemanticExtractionValidationError,
        SemanticStagingValidationError,
        OpportunitySemanticAuthorityError,
        KeyError,
        TypeError,
    ) as exc:
        return _failed_result(
            canonical_opportunity_id,
            status="invalid",
            code="packet_construction_validation_failed",
            stage="packet_validation",
            message=str(exc),
        )
    except SemanticExtractionError as exc:
        return _failed_result(
            canonical_opportunity_id,
            status="unavailable",
            code="semantic_extraction_provider_failed",
            stage="semantic_extraction",
            message=str(exc),
        )
    except sqlite3.Error as exc:
        return _failed_result(
            canonical_opportunity_id,
            status="unavailable",
            code="read_only_source_access_failed",
            stage="source_evidence",
            message=str(exc),
        )


__all__ = [
    "SHADOW_EXECUTION_MODE",
    "SHADOW_PACKET_INTEGRATION_VERSION",
    "SHADOW_PACKET_RESULT_VERSION",
    "ShadowPacketConstructionError",
    "construct_oe_semantic_matching_packet_v1_shadow",
]

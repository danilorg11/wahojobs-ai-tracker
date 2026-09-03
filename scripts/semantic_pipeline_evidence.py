"""Generic accepted-evidence adapter and historical packet assembly, offline only.

Promoted from the September 1 source loader and September 2 provisioning harness.
No benchmark directory, sample membership, profile, or human-review input is read.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from wahojobs.opportunity_enrichment import (
    SEMANTIC_INPUT_VERSION, load_semantic_input, semantic_input_sha256,
    semantic_variant_refs, llm_source_packet,
)
from wahojobs.opportunity_semantic_authority import (
    build_semantic_matching_packet_from_staging, canonical_sha256,
    derive_server_variant_relationships, semantic_non_exclusionary_authority,
    validate_semantic_matching_packet,
)
from wahojobs.opportunity_semantic_extraction import (
    accepted_evidence_aliases, validate_accepted_evidence_bindings,
    validate_model_extraction,
)
from wahojobs.opportunity_semantic_staging import stage_provisional_atoms, construct_relations

EVIDENCE_VERSION = "semantic_pipeline_frozen_evidence_v1"
PACKET_RECIPE_VERSION = "semantic_pipeline_historical_packet_recipe_v1"
FORBIDDEN_METADATA = frozenset({
    "human_relevance", "human_label", "human_labels", "review_notes", "reviewer_notes",
    "benchmark_metrics", "metric_outcomes", "expected_ranking", "expected_order",
    "expected_ordering", "error_analysis", "known_error_categories", "selection_stratum",
    "legacy_score", "legacy_rank", "legacy_bucket", "profile_facts", "user_data",
})
EVIDENCE_FIELDS = frozenset({
    "schema_version", "opportunity_ref", "local_authority_identifiers", "authority",
    "frozen_semantic_input", "frozen_source_packet", "evidence_sha256",
})


class ProvisioningError(ValueError):
    pass


def canonical_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def reject_metadata(value):
    if isinstance(value, dict):
        if set(value) & FORBIDDEN_METADATA:
            raise ProvisioningError("forbidden_runtime_metadata")
        for child in value.values():
            reject_metadata(child)
    elif isinstance(value, list):
        for child in value:
            reject_metadata(child)


def open_immutable_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    mutation_actions = {
        getattr(sqlite3, name)
        for name in (
            "SQLITE_INSERT",
            "SQLITE_UPDATE",
            "SQLITE_DELETE",
            "SQLITE_CREATE_INDEX",
            "SQLITE_CREATE_TABLE",
            "SQLITE_CREATE_TEMP_INDEX",
            "SQLITE_CREATE_TEMP_TABLE",
            "SQLITE_CREATE_TEMP_TRIGGER",
            "SQLITE_CREATE_TEMP_VIEW",
            "SQLITE_CREATE_TRIGGER",
            "SQLITE_CREATE_VIEW",
            "SQLITE_DROP_INDEX",
            "SQLITE_DROP_TABLE",
            "SQLITE_DROP_TEMP_INDEX",
            "SQLITE_DROP_TEMP_TABLE",
            "SQLITE_DROP_TEMP_TRIGGER",
            "SQLITE_DROP_TEMP_VIEW",
            "SQLITE_DROP_TRIGGER",
            "SQLITE_DROP_VIEW",
            "SQLITE_ALTER_TABLE",
            "SQLITE_REINDEX",
            "SQLITE_ANALYZE",
            "SQLITE_ATTACH",
            "SQLITE_DETACH",
        )
        if hasattr(sqlite3, name)
    }

    def authorize(action, _first, _second, _database, _trigger):
        return sqlite3.SQLITE_DENY if action in mutation_actions else sqlite3.SQLITE_OK

    connection.set_authorizer(authorize)
    return connection


def _acceptance_rows(
    connection: sqlite3.Connection,
    canonical_id: int,
    variant_refs: list[str],
) -> list[dict]:
    rows = connection.execute(
        """
        SELECT
          j.id AS job_id,
          j.source_hash,
          j.is_active,
          j.semantic_authority_state,
          sc.provider,
          sc.source_type,
          sc.source_url,
          sc.external_id,
          sc.body,
          sc.body_format,
          sc.material_content_sha256,
          a.accepted_capture_id,
          a.promotion_policy_version,
          a.accepted_at,
          a.last_confirmed_at,
          cap.capture_contract_version,
          cap.semantic_material_sha256,
          cap.capture_quality,
          cap.provider_outcome,
          cap.used_sample_data,
          cap.snapshot_complete,
          cap.pagination_complete,
          cap.promotion_decision,
          cap.body_observation,
          cap.observed_at
        FROM jobs j
        JOIN job_source_contents sc ON sc.job_id = j.id
        JOIN job_source_content_acceptances a ON a.job_id = j.id
        JOIN job_source_content_captures cap
          ON cap.id = a.accepted_capture_id AND cap.job_id = j.id
        WHERE j.canonical_opportunity_id = ?
          AND j.title NOT LIKE '[SIMULATION]%'
          AND j.semantic_authority_state != 'pending'
        ORDER BY j.source_hash, j.id
        """,
        (canonical_id,),
    ).fetchall()
    by_ref = {f"source_hash:{row['source_hash']}": dict(row) for row in rows}
    if set(by_ref) < set(variant_refs):
        raise ProvisioningError("accepted binding coverage is incomplete")
    selected = [by_ref[ref] for ref in variant_refs]
    for row in selected:
        if (
            row["semantic_authority_state"] != "versioned_accepted"
            or not row["accepted_capture_id"]
            or not row["material_content_sha256"]
            or not row["semantic_material_sha256"]
            or not str(row["body"] or "").strip()
            or row["used_sample_data"]
            or row["capture_quality"] != "healthy_body"
            or row["promotion_decision"] != "promoted"
            or row["body_observation"] != "present"
        ):
            raise ProvisioningError("accepted binding is not authoritative")
    return selected


def seal_evidence(canonical_id, semantic_input, source_packet, capture_rows):
    """Seal trusted server/snapshot parts; no automatic artifact lookup or DB writes."""
    if type(canonical_id) is not int or canonical_id <= 0:
        raise ProvisioningError("invalid_canonical_id")
    record = {
        "schema_version": EVIDENCE_VERSION,
        "opportunity_ref": f"canonical_opportunity:{canonical_id}",
        "local_authority_identifiers": {"canonical_opportunity_id": canonical_id},
        "authority": {
            "semantic_input_version": SEMANTIC_INPUT_VERSION,
            "semantic_input_sha256": semantic_input_sha256(semantic_input),
            "source_packet_sha256": canonical_sha256(source_packet),
            "accepted_capture_bindings": deepcopy(capture_rows),
        },
        "frozen_semantic_input": deepcopy(semantic_input),
        "frozen_source_packet": deepcopy(source_packet),
    }
    record["evidence_sha256"] = fingerprint(record)
    validate_frozen_evidence(record)
    return record


def validate_frozen_evidence(record):
    if type(record) is not dict or set(record) != EVIDENCE_FIELDS:
        raise ProvisioningError("invalid_frozen_evidence_shape")
    reject_metadata(record)
    if record["schema_version"] != EVIDENCE_VERSION:
        raise ProvisioningError("invalid_evidence_version")
    cid = record["local_authority_identifiers"].get("canonical_opportunity_id")
    if type(cid) is not int or cid <= 0 or record["opportunity_ref"] != f"canonical_opportunity:{cid}":
        raise ProvisioningError("canonical_identity_mismatch")
    material = {key: value for key, value in record.items() if key != "evidence_sha256"}
    if fingerprint(material) != record["evidence_sha256"]:
        raise ProvisioningError("frozen_evidence_hash_mismatch")
    semantic = record["frozen_semantic_input"]
    source = record["frozen_source_packet"]
    authority = record["authority"]
    if authority["semantic_input_version"] != SEMANTIC_INPUT_VERSION:
        raise ProvisioningError("semantic_input_version_mismatch")
    if semantic_input_sha256(semantic) != authority["semantic_input_sha256"]:
        raise ProvisioningError("semantic_input_hash_mismatch")
    if canonical_sha256(source) != authority["source_packet_sha256"] or llm_source_packet(semantic)[0] != source:
        raise ProvisioningError("source_packet_hash_or_derivation_mismatch")
    variants = set(semantic_variant_refs(semantic))
    rows = {f"source_hash:{row['source_hash']}": row for row in authority["accepted_capture_bindings"]}
    rich = {row["variant_ref"]: row for row in semantic.get("rich_content", [])}
    if not variants or set(rows) != variants or set(rich) != variants:
        raise ProvisioningError("accepted_variant_coverage_incomplete")
    for ref in sorted(variants):
        row, content = rows[ref], rich[ref]
        if (row["semantic_authority_state"] != "versioned_accepted"
                or row["capture_quality"] != "healthy_body"
                or row["promotion_decision"] != "promoted"
                or row["body_observation"] != "present" or row["used_sample_data"]
                or content["body"] != row["body"]
                or content["material_content_sha256"] != row["material_content_sha256"]
                or content["semantic_material_sha256"] != row["semantic_material_sha256"]
                or content["authority"]["accepted_capture_ref"] != f"source_capture:{row['accepted_capture_id']}"):
            raise ProvisioningError("accepted_evidence_identity_mismatch")
    if not accepted_evidence_aliases(source):
        raise ProvisioningError("accepted_body_evidence_unavailable")
    return accepted_bindings(record)


def capture_opportunity(database, canonical_id):
    """Read one explicitly supplied canonical ID; never enumerate or sample a universe."""
    if type(canonical_id) is not int or canonical_id <= 0:
        raise ProvisioningError("invalid_canonical_id")
    ref = f"canonical_opportunity:{canonical_id}"
    try:
        connection = open_immutable_database(Path(database))
        try:
            semantic = load_semantic_input(connection, canonical_id)
            rows = _acceptance_rows(connection, canonical_id, semantic_variant_refs(semantic))
            source, _ = llm_source_packet(semantic)
            evidence = seal_evidence(canonical_id, semantic, source, rows)
        finally:
            connection.close()
        return {"opportunity_ref": ref, "evidence": evidence, "failure": None}
    except (ValueError, RuntimeError, sqlite3.Error, KeyError, TypeError, OSError) as exc:
        return {"opportunity_ref": ref, "evidence": None, "failure": "source_unavailable:" + type(exc).__name__}


def review_variant_aliases(opportunity_ref, evidence):
    return {ref: f"{opportunity_ref}:V{index:03d}"
            for index, ref in enumerate(evidence["variant_refs"], 1)}


def _evidence_catalog(opportunity_ref: str, evidence: dict) -> dict:
    aliases = review_variant_aliases(opportunity_ref, evidence)
    semantic_input = evidence["semantic_input"]
    source_packet = evidence["source_packet"]
    variants = []
    for variant in source_packet.get("variants") or []:
        variants.append(
            {
                "variant_ref": aliases[variant["variant_ref"]],
                "title": variant.get("title"),
                "location": variant.get("location"),
                "department": variant.get("department"),
                "expertise": variant.get("expertise"),
                "commitment": variant.get("commitment"),
                "opportunity_kind": variant.get("opportunity_kind"),
                "availability_basis": variant.get("availability_basis"),
                "include_in_live_market_estimate": variant.get(
                    "include_in_live_market_estimate"
                ),
            }
        )
    blocks = []
    for block in source_packet.get("evidence_blocks") or []:
        allowed = block.get("authority_class") == "accepted_body_evidence" or (
            block.get("authority_class") == "variant_listing_evidence"
            and block.get("label") != "listing.url"
        )
        if not allowed:
            continue
        blocks.append(
            {
                "evidence_block_id": block["evidence_block_id"],
                "kind": block["kind"],
                "authority_class": block["authority_class"],
                "label": block["label"],
                "variant_refs": sorted(
                    aliases[ref]
                    for ref in block.get("variant_refs") or []
                    if ref in aliases
                ),
                "content": block["content"],
            }
        )
    catalog = {
        "opportunity_ref": opportunity_ref,
        "canonical_context": {
            "canonical_title": semantic_input["canonical"].get("canonical_title"),
            "source_category": semantic_input["canonical"].get("source_category"),
            "language": semantic_input["canonical"].get("language"),
            "language_locale": semantic_input["canonical"].get("language_locale"),
        },
        "variants": variants,
        "evidence_blocks": blocks,
    }
    catalog["substantive_evidence_sha256"] = fingerprint(catalog)
    return catalog


def _provider_input(opportunity_ref: str, catalog: dict) -> dict:
    variants_by_ref = {
        item["variant_ref"]: item for item in catalog.get("variants") or []
    }
    blocks = []
    for block in catalog.get("evidence_blocks") or []:
        refs = block.get("variant_refs") or []
        blocks.append(
            {
                "evidence_block_id": block["evidence_block_id"],
                "source_ref": refs[0] if refs else f"{opportunity_ref}:source",
                "source_refs": refs or [f"{opportunity_ref}:source"],
                "variant_refs": refs,
                "authority_refs": [],
                "kind": block["kind"],
                "authority_class": block["authority_class"],
                "label": block["label"],
                "content": block["content"],
            }
        )
    return {
        "company": {
            "name": "Withheld opportunity provider",
            "slug": "provider_blind",
        },
        "canonical": deepcopy(catalog["canonical_context"]),
        "variants": [deepcopy(variants_by_ref[key]) for key in sorted(variants_by_ref)],
        "evidence_blocks": blocks,
    }


def provider_evidence(record):
    """Historical blinding/allowlist; neutral request-local alias namespace."""
    validate_frozen_evidence(record)
    parts = {"variant_refs": semantic_variant_refs(record["frozen_semantic_input"]),
             "semantic_input": record["frozen_semantic_input"],
             "source_packet": record["frozen_source_packet"]}
    # One opportunity per request: no global identity or benchmark IDs are needed.
    catalog = _evidence_catalog("opportunity", parts)
    return _provider_input("opportunity", catalog)


def accepted_bindings(frozen: dict) -> list[dict]:
    semantic_input = frozen["frozen_semantic_input"]
    source_packet = frozen["frozen_source_packet"]
    rows = {
        f"source_hash:{row['source_hash']}": row
        for row in frozen["authority"]["accepted_capture_bindings"]
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
            row = rows.get(source_ref)
            authority = (source or {}).get("authority") or {}
            if (
                source is not None
                and row is not None
                and authority.get("semantic_authority_state") == "versioned_accepted"
                and authority.get("accepted_capture_ref")
                == f"source_capture:{row['accepted_capture_id']}"
                and source.get("material_content_sha256")
                == row.get("material_content_sha256")
                and source.get("source_url") == row.get("source_url")
            ):
                candidates.append((source_ref, source, row))
        if not candidates:
            raise ProvisioningError(
                f"{frozen['opportunity_ref']}: accepted binding is unavailable"
            )
        _source_ref, source, row = sorted(candidates, key=lambda item: item[0])[0]
        text = block["content"]
        bindings.append(
            {
                "alias": block["evidence_block_id"],
                "authority": "accepted_capture",
                "text": text,
                "text_sha256": sha256(text.encode("utf-8")).hexdigest(),
                "provenance": {
                    "accepted_capture_id": int(row["accepted_capture_id"]),
                    "job_id": int(row["job_id"]),
                    "material_content_sha256": source["material_content_sha256"],
                    "source_url": source["source_url"],
                },
            }
        )
    bindings.sort(key=lambda item: item["alias"])
    validate_accepted_evidence_bindings(source_packet, bindings)
    if [item["alias"] for item in bindings] != accepted_evidence_aliases(source_packet):
        raise ProvisioningError(
            f"{frozen['opportunity_ref']}: accepted aliases are incomplete"
        )
    return bindings


def build_packet(frozen: dict, raw_extraction: dict, bindings: list[dict]) -> tuple:
    source_packet = frozen["frozen_source_packet"]
    semantic_input = frozen["frozen_semantic_input"]
    validation = validate_model_extraction(raw_extraction, source_packet, bindings)
    staging = stage_provisional_atoms(raw_extraction, source_packet, bindings)
    relations = construct_relations(staging, None)
    relationships = derive_server_variant_relationships(
        source_packet,
        bindings,
        staging["accepted_evidence_catalog"],
    )
    canonical_id = frozen["local_authority_identifiers"]["canonical_opportunity_id"]
    packet = build_semantic_matching_packet_from_staging(
        staging,
        relations,
        canonical_ref=f"canonical_opportunity:{canonical_id}",
        known_variant_refs=semantic_variant_refs(semantic_input),
        semantic_input_version=SEMANTIC_INPUT_VERSION,
        semantic_input_sha256=frozen["authority"]["semantic_input_sha256"],
        source_packet_sha256=frozen["authority"]["source_packet_sha256"],
        semantic_extraction_version=raw_extraction["extraction_version"],
        semantic_grouping_version=None,
        variant_relationships=relationships,
        descriptive_signals=[],
    )
    validate_semantic_matching_packet(packet)
    return packet, validation


def validate_packet_provenance(packet: dict, frozen: dict, bindings: list[dict]) -> dict:
    binding_values = list(bindings)
    checks = {
        "packet_semantic_input_hash_matches_frozen": packet["identities"][
            "semantic_input_sha256"
        ]
        == frozen["authority"]["semantic_input_sha256"],
        "packet_source_packet_hash_matches_frozen": packet["identities"][
            "source_packet_sha256"
        ]
        == frozen["authority"]["source_packet_sha256"],
        "all_evidence_hashes_valid": all(
            source["text_sha256"]
            == sha256(source["text"].encode("utf-8")).hexdigest()
            for source in packet["evidence_sources"]
        ),
        "all_evidence_is_frozen_accepted_capture_text": all(
            any(
                source["authority"] == binding["authority"]
                and source["provenance"] == binding["provenance"]
                and source["text"] in binding["text"]
                for binding in binding_values
            )
            for source in packet["evidence_sources"]
        ),
        "semantic_authority_exact": packet["authority"]
        == semantic_non_exclusionary_authority(),
        "zero_semantic_hard_exclusions": packet["accounting"][
            "semantic_hard_exclusion_count"
        ]
        == 0,
        "verifier_dependency_none": packet["construction_policy"][
            "verifier_dependency"
        ]
        == "none",
    }
    if not all(checks.values()):
        raise ProvisioningError(
            f"{frozen['opportunity_ref']}: packet provenance validation failed"
        )
    return {"integrity": True, "checks": checks}

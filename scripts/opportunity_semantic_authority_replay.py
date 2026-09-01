#!/usr/bin/env python3
"""Offline proof for OE Semantic Authority Boundary v1.

The replay reads only reviewed fixtures.  It performs no provider, network,
application-database, persistence, matching, or candidate-UI operation and does
not execute either verifier experiment.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wahojobs.matching.foundation_contracts import (  # noqa: E402
    DETERMINISTIC_ELIGIBILITY_CRITERIA_V1 as MATCHING_ELIGIBILITY_CRITERIA,
)
from wahojobs.opportunity_enrichment import SEMANTIC_INPUT_VERSION  # noqa: E402
from wahojobs.opportunity_semantic_authority import (  # noqa: E402
    AUTHORITY_POLICY_VERSION,
    DETERMINISTIC_ELIGIBILITY_CRITERIA_V1,
    SEMANTIC_AUTHORITY_TYPE,
    SEMANTIC_MATCHING_PACKET_VERSION,
    authority_can_create_hard_eligibility_failure,
    build_semantic_matching_packet,
    build_semantic_matching_packet_from_staging,
    canonical_sha256,
    derive_server_variant_relationships,
    hard_authoritative_objective_fact,
)
from wahojobs.opportunity_semantic_contract import (  # noqa: E402
    project_legacy_compatibility,
)
from wahojobs.opportunity_semantic_staging import (  # noqa: E402
    aggregate_replay,
    construct_relations,
    replay_case,
    stage_provisional_atoms,
)


REPORT_VERSION = "oe_semantic_authority_boundary_v1_offline_replay_v1"
RAW_ARTIFACT_SHA256 = (
    "37d6e7bea857274477ad08a89fef81023b3c1502c5064242ba4fe34c5540daf4"
)
PREREGISTRATION_SHA256 = (
    "177574a22812a1f6122b3a03e640848fd6f211a9d19476ff416ed7330f6fbd38"
)

DEFAULT_GOLD = (
    ROOT / "tests" / "fixtures" / "opportunity_semantic_contract_v0.json"
)
DEFAULT_RAW = (
    ROOT
    / "tests"
    / "fixtures"
    / "opportunity_semantic_staging_v0_fresh_canary_raw.json"
)
DEFAULT_PREREGISTRATION = (
    ROOT
    / "tests"
    / "fixtures"
    / "opportunity_semantic_extraction_v0_fresh_canary.json"
)
DEFAULT_REVIEW = (
    ROOT
    / "tests"
    / "fixtures"
    / "opportunity_semantic_staging_v0_reviewed_canary.json"
)
AUTHORITY_MODULE = ROOT / "wahojobs" / "opportunity_semantic_authority.py"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _report_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT).as_posix()
    except ValueError:
        return str(resolved)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _assert_opened_regression_artifacts(
    raw,
    preregistration,
    review,
    *,
    raw_path: Path,
    preregistration_path: Path,
) -> None:
    if file_sha256(raw_path) != RAW_ARTIFACT_SHA256:
        raise RuntimeError("opened raw regression artifact SHA-256 mismatch")
    if file_sha256(preregistration_path) != PREREGISTRATION_SHA256:
        raise RuntimeError("preregistration artifact SHA-256 mismatch")
    if review["raw_artifact_sha256"] != RAW_ARTIFACT_SHA256:
        raise RuntimeError("review fixture names another raw artifact")
    if review["preregistration_fixture_sha256"] != PREREGISTRATION_SHA256:
        raise RuntimeError("review fixture names another preregistration")
    if raw["preregistration"]["fixture_sha256"] != PREREGISTRATION_SHA256:
        raise RuntimeError("raw artifact names another preregistration")
    selected = [str(value) for value in preregistration["selected_canonical_ids"]]
    if list(raw["cases"]) != selected or set(review["cases"]) != set(selected):
        raise RuntimeError("opened 16-case identities do not match")
    if not raw["fixture_preserved"] or not raw["database_preserved"]:
        raise RuntimeError("opened regression did not preserve inputs")


def _gold_replay(gold: dict) -> tuple[dict, dict[str, dict]]:
    packets = {}
    compatibility = {}
    logic_preserved = []
    expected_projection_preserved = []
    for case in gold["cases"]:
        case_id = case["id"]
        semantic_bundle = {
            "contract": case["contract"],
            "evidence_catalog": case["evidence_catalog"],
        }
        packet = build_semantic_matching_packet(
            case["contract"],
            case["evidence_catalog"],
            canonical_ref=f"fixture:{case_id}",
            known_variant_refs=[f"fixture:{case_id}"],
            semantic_input_version=gold["fixture_version"],
            semantic_input_sha256=canonical_sha256(semantic_bundle),
            source_packet_sha256=canonical_sha256(case["evidence_catalog"]),
        )
        projection = project_legacy_compatibility(
            case["contract"], case["evidence_catalog"]
        )
        packets[case_id] = packet
        compatibility[case_id] = projection
        logic_preserved.append(
            [group["raw_proposal"] for group in packet["groups"]]
            == projection["normalized_contract"]["constraint_groups"]
        )
        expected_projection_preserved.append(
            projection["legacy_patch"] == case["expected"]["legacy_patch"]
            and [item["state"] for item in projection["group_outcomes"]]
            == case["expected"]["group_states"]
        )
    all_items = [
        item
        for packet in packets.values()
        for item in [
            *packet["propositions"],
            *packet["groups"],
            *packet["descriptive_signals"],
            *packet["retained_invalid_proposals"],
        ]
    ]
    groups = [group for packet in packets.values() for group in packet["groups"]]
    modalities = Counter(group["modality"] for group in groups)
    return (
        {
            "case_count": len(packets),
            "proposition_count": sum(
                len(packet["propositions"]) for packet in packets.values()
            ),
            "group_count": len(groups),
            "group_modalities": dict(sorted(modalities.items())),
            "required_group_count": modalities["required"],
            "preferred_group_count": modalities["preferred"],
            "descriptive_group_count": modalities["descriptive"],
            "all_authority_types": sorted(
                {item["authority"]["authority_type"] for item in all_items}
            ),
            "semantic_hard_exclusion_count": sum(
                authority_can_create_hard_eligibility_failure(item)
                for item in all_items
            ),
            "required_group_hard_failure_count": sum(
                group["modality"] == "required"
                and authority_can_create_hard_eligibility_failure(group)
                for group in groups
            ),
            "dnf_logic_preserved": all(logic_preserved),
            "legacy_gold_projection_preserved": all(
                expected_projection_preserved
            ),
            "compatibility_projection_hard_authority_count": sum(
                projection["compatibility_authority"][
                    "hard_eligibility_authorized"
                ]
                for projection in compatibility.values()
            ),
        },
        packets,
    )


def _regression_replay(
    raw: dict, review: dict
) -> tuple[dict, dict[str, dict], dict[str, dict]]:
    replays = {
        case_id: replay_case(case, review["cases"][case_id])
        for case_id, case in raw["cases"].items()
    }
    packets = {}
    exact_relation_preservation = []
    exact_source_value_preservation = []
    for case_id, case in raw["cases"].items():
        replay = replays[case_id]
        relationships = derive_server_variant_relationships(
            case["source_packet"],
            case["accepted_evidence_bindings"],
            replay["staging"]["accepted_evidence_catalog"],
        )
        packet = build_semantic_matching_packet_from_staging(
            replay["staging"],
            replay["relations"],
            canonical_ref=f"canonical_opportunity:{case['canonical_opportunity_id']}",
            known_variant_refs=sorted(
                item["variant_ref"] for item in case["source_packet"]["variants"]
            ),
            semantic_input_version=SEMANTIC_INPUT_VERSION,
            semantic_input_sha256=case["semantic_input_sha256"],
            source_packet_sha256=case["source_packet_sha256"],
            semantic_extraction_version=case["raw_extraction"]["extraction_version"],
            semantic_grouping_version=case["raw_grouping"]["grouping_version"],
            variant_relationships=relationships,
        )
        packets[case_id] = packet
        packet_groups = {group["group_id"]: group for group in packet["groups"]}
        for relation in replay["relations"]["relations"]:
            group = packet_groups[relation["relation_id"]]
            exact_relation_preservation.append(
                group["raw_proposal"] == relation["proposal"]
                and group["source_branches"] == relation["source_branches"]
                and group["relation_state"] == relation["state"]
            )
            if group["logic"] is not None:
                exact_relation_preservation.append(
                    group["logic"]["any_of"] == relation["proposal"]["any_of"]
                )
        packet_propositions = {
            item["proposition_id"]: item for item in packet["propositions"]
        }
        for staged in replay["staging"]["provisional_atoms"]:
            if staged["status"] != "provisional":
                continue
            exact_source_value_preservation.append(
                packet_propositions[staged["proposal_id"]]["normalization"][
                    "source_values"
                ]
                == staged["normalization"]["source_values"]
            )

    all_items = [
        item
        for packet in packets.values()
        for item in [
            *packet["propositions"],
            *packet["groups"],
            *packet["descriptive_signals"],
            *packet["retained_invalid_proposals"],
        ]
    ]
    propositions = [
        proposition
        for packet in packets.values()
        for proposition in packet["propositions"]
    ]
    groups = [group for packet in packets.values() for group in packet["groups"]]
    incomplete_groups = [group for group in groups if group["completeness"] != "complete"]
    unresolved_propositions = [
        proposition
        for proposition in propositions
        if proposition["normalization"]["status"] != "resolved"
        or proposition["status"]["semantic_support"]
        in {"contradicted", "not_established", "pending"}
    ]
    raw_source_values = [
        source_value
        for proposition in propositions
        for source_value in proposition["normalization"]["source_values"]
    ]
    aggregate = aggregate_replay(replays)
    modalities = Counter(group["modality"] for group in groups)
    return (
        {
            "case_count": len(packets),
            "proposition_count": len(propositions),
            "group_count": len(groups),
            "group_modalities": dict(sorted(modalities.items())),
            "complete_group_count": len(groups) - len(incomplete_groups),
            "incomplete_or_unresolved_group_count": len(incomplete_groups),
            "source_branch_count": sum(
                len(group["source_branches"]) for group in groups
            ),
            "unresolved_source_branch_count": sum(
                branch.get("coverage_state") == "unresolved"
                for group in groups
                for branch in group["source_branches"]
            ),
            "raw_source_value_count": len(raw_source_values),
            "raw_source_values_preserved": all(exact_source_value_preservation),
            "relation_and_dnf_preserved": all(exact_relation_preservation),
            "logic_weakening_cases": aggregate["relations"][
                "logic_weakening_cases"
            ],
            "projected_from_incomplete_relation_ids": aggregate["relations"][
                "projected_from_incomplete_relation_ids"
            ],
            "legacy_unsafe_hard_gates": aggregate["projection"][
                "unsafe_hard_gates"
            ],
            "all_authority_types": sorted(
                {item["authority"]["authority_type"] for item in all_items}
            ),
            "semantic_hard_exclusion_count": sum(
                authority_can_create_hard_eligibility_failure(item)
                for item in all_items
            ),
            "required_group_hard_failure_count": sum(
                group["modality"] == "required"
                and authority_can_create_hard_eligibility_failure(group)
                for group in groups
            ),
            "unresolved_or_unsupported_hard_gate_count": sum(
                authority_can_create_hard_eligibility_failure(item)
                for item in [*unresolved_propositions, *incomplete_groups]
            ),
            "legacy_hard_assurance_observation_count": sum(
                proposition["experimental_observation"]["legacy_assurance"]
                == "hard_projection_authorized"
                for proposition in propositions
            ),
            "legacy_hard_assurance_promotions": sum(
                proposition["experimental_observation"]["legacy_assurance"]
                == "hard_projection_authorized"
                and authority_can_create_hard_eligibility_failure(proposition)
                for proposition in propositions
            ),
            "server_derived_variant_relationship_count": sum(
                len(proposition["variant_relationships"])
                for proposition in propositions
            ),
        },
        packets,
        replays,
    )


def _objective_authority_proof() -> dict:
    objective = {
        "field_path": "attributes.work_arrangement.eligible_countries",
        "value": "Brazil",
        "knowledge_state": "known_value",
        "variant_refs": ["fixture:objective"],
        "evidence": [
            {
                "evidence_block_id": "Eobjective",
                "source_refs": ["fixture:objective"],
                "authority_refs": ["fixture:accepted"],
                "evidence_text": "Brazil - Remote",
                "basis": "deterministic_parse",
                "confidence": "high",
            }
        ],
    }
    hard_fact = hard_authoritative_objective_fact(
        objective, criterion_id="eligibility.location"
    )
    return {
        "fact_authority_type": hard_fact["authority"]["authority_type"],
        "hard_eligibility_authorized": authority_can_create_hard_eligibility_failure(
            hard_fact
        ),
        "criterion_id": hard_fact["authority"]["eligibility_criterion_id"],
        "matching_criteria_contract_aligned": dict(
            DETERMINISTIC_ELIGIBILITY_CRITERIA_V1
        )
        == dict(MATCHING_ELIGIBILITY_CRITERIA),
        "separate_from_semantic_authority": hard_fact["authority"][
            "authority_type"
        ]
        != SEMANTIC_AUTHORITY_TYPE,
    }


def _verifier_free_packet_proof(raw: dict) -> dict:
    case_id = next(iter(raw["cases"]))
    case = raw["cases"][case_id]
    staging = stage_provisional_atoms(
        case["raw_extraction"],
        case["source_packet"],
        case["accepted_evidence_bindings"],
    )
    relations = construct_relations(staging, case.get("raw_grouping"))
    relationships = derive_server_variant_relationships(
        case["source_packet"],
        case["accepted_evidence_bindings"],
        staging["accepted_evidence_catalog"],
    )
    packet = build_semantic_matching_packet_from_staging(
        staging,
        relations,
        canonical_ref=f"canonical_opportunity:{case['canonical_opportunity_id']}",
        known_variant_refs=sorted(
            item["variant_ref"] for item in case["source_packet"]["variants"]
        ),
        semantic_input_version=SEMANTIC_INPUT_VERSION,
        semantic_input_sha256=case["semantic_input_sha256"],
        source_packet_sha256=case["source_packet_sha256"],
        semantic_extraction_version=case["raw_extraction"]["extraction_version"],
        semantic_grouping_version=case["raw_grouping"]["grouping_version"],
        variant_relationships=relationships,
    )
    return {
        "case_id": case_id,
        "packet_constructed": True,
        "proposition_count": len(packet["propositions"]),
        "pending_support_count": sum(
            proposition["status"]["semantic_support"] == "pending"
            for proposition in packet["propositions"]
        ),
        "hard_exclusion_count": packet["accounting"][
            "semantic_hard_exclusion_count"
        ],
        "verifier_result_supplied": False,
    }


def build_report(
    gold_path: Path = DEFAULT_GOLD,
    raw_path: Path = DEFAULT_RAW,
    preregistration_path: Path = DEFAULT_PREREGISTRATION,
    review_path: Path = DEFAULT_REVIEW,
) -> dict:
    gold = _read_json(gold_path)
    raw = _read_json(raw_path)
    preregistration = _read_json(preregistration_path)
    review = _read_json(review_path)
    _assert_opened_regression_artifacts(
        raw,
        preregistration,
        review,
        raw_path=raw_path,
        preregistration_path=preregistration_path,
    )

    gold_metrics, _gold_packets = _gold_replay(gold)
    regression_metrics, _regression_packets, _replays = _regression_replay(
        raw, review
    )
    objective = _objective_authority_proof()
    verifier_free = _verifier_free_packet_proof(raw)
    imports = _imported_modules(AUTHORITY_MODULE)
    verifier_imports = sorted(
        name for name in imports if "opportunity_semantic_verifier" in name
    )
    checks = {
        "gold_every_fact_and_group_semantic_non_exclusionary": (
            gold_metrics["all_authority_types"] == [SEMANTIC_AUTHORITY_TYPE]
            and gold_metrics["semantic_hard_exclusion_count"] == 0
        ),
        "gold_required_groups_cannot_hard_fail": (
            gold_metrics["required_group_hard_failure_count"] == 0
        ),
        "gold_required_preferred_descriptive_and_dnf_preserved": (
            gold_metrics["required_group_count"] > 0
            and gold_metrics["preferred_group_count"] > 0
            and gold_metrics["descriptive_group_count"] > 0
            and gold_metrics["dnf_logic_preserved"]
        ),
        "gold_legacy_behavior_preserved_but_non_authoritative": (
            gold_metrics["legacy_gold_projection_preserved"]
            and gold_metrics[
                "compatibility_projection_hard_authority_count"
            ]
            == 0
        ),
        "regression_every_fact_and_group_semantic_non_exclusionary": (
            regression_metrics["all_authority_types"]
            == [SEMANTIC_AUTHORITY_TYPE]
            and regression_metrics["semantic_hard_exclusion_count"] == 0
        ),
        "regression_required_groups_cannot_hard_fail": (
            regression_metrics["required_group_hard_failure_count"] == 0
        ),
        "incomplete_unresolved_relations_preserved": (
            regression_metrics["incomplete_or_unresolved_group_count"] > 0
            and regression_metrics["relation_and_dnf_preserved"]
            and regression_metrics["raw_source_values_preserved"]
        ),
        "unsupported_or_unresolved_cannot_hard_gate": (
            regression_metrics["unresolved_or_unsupported_hard_gate_count"]
            == 0
        ),
        "zero_or_and_weakening": (
            regression_metrics["logic_weakening_cases"] == []
            and regression_metrics["projected_from_incomplete_relation_ids"]
            == []
        ),
        "legacy_verifier_assurance_cannot_promote": (
            regression_metrics["legacy_hard_assurance_observation_count"] > 0
            and regression_metrics["legacy_hard_assurance_promotions"] == 0
        ),
        "packet_builder_has_no_verifier_dependency": verifier_imports == [],
        "packet_constructs_without_verifier_result": (
            verifier_free["packet_constructed"]
            and verifier_free["proposition_count"] > 0
            and verifier_free["pending_support_count"]
            == verifier_free["proposition_count"]
            and verifier_free["hard_exclusion_count"] == 0
            and not verifier_free["verifier_result_supplied"]
        ),
        "objective_authority_is_separate_and_closed": (
            objective["hard_eligibility_authorized"]
            and objective["separate_from_semantic_authority"]
            and objective["matching_criteria_contract_aligned"]
        ),
        "existing_regression_unsafe_hard_gate_controls_remain_zero": (
            regression_metrics["legacy_unsafe_hard_gates"] == []
        ),
    }
    passed = all(checks.values())
    report = {
        "report_version": REPORT_VERSION,
        "mode": "offline_reviewed_artifact_replay_no_provider_no_database_no_verifier",
        "authority_policy_version": AUTHORITY_POLICY_VERSION,
        "semantic_packet_version": SEMANTIC_MATCHING_PACKET_VERSION,
        "artifacts": {
            "gold": {
                "path": _report_path(gold_path),
                "sha256": file_sha256(gold_path),
            },
            "opened_regression": {
                "path": _report_path(raw_path),
                "sha256": file_sha256(raw_path),
                "expected_sha256": RAW_ARTIFACT_SHA256,
            },
            "preregistration": {
                "path": _report_path(preregistration_path),
                "sha256": file_sha256(preregistration_path),
                "expected_sha256": PREREGISTRATION_SHA256,
            },
            "review": {
                "path": _report_path(review_path),
                "sha256": file_sha256(review_path),
            },
        },
        "offline_controls": {
            "provider_calls": 0,
            "network_calls": 0,
            "database_reads": 0,
            "database_writes": 0,
            "verifier_experiments_run": 0,
        },
        "artifact_preservation": {
            "fixture_preserved": raw["fixture_preserved"],
            "database_preserved": raw["database_preserved"],
        },
        "gold": gold_metrics,
        "opened_16_case_regression": regression_metrics,
        "objective_deterministic_authority": objective,
        "verifier_dependency": {
            "packet_builder_imported_modules": sorted(imports),
            "verifier_imports": verifier_imports,
            "required": False,
            "pending_packet_proof": verifier_free,
        },
        "acceptance": {"checks": checks, "passed": passed},
        "verdict": (
            "SEMANTIC AUTHORITY BOUNDARY PASSED"
            if passed
            else "AUTHORITY GAP FOUND"
        ),
    }
    report["report_sha256"] = canonical_sha256(report)
    return report


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    parser.add_argument(
        "--preregistration", type=Path, default=DEFAULT_PREREGISTRATION
    )
    parser.add_argument("--review", type=Path, default=DEFAULT_REVIEW)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    report = build_report(
        args.gold, args.raw, args.preregistration, args.review
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["acceptance"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

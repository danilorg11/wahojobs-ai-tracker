#!/usr/bin/env python3
"""Zero-provider replay for OE Semantic Grouping v0 hybrid gold closure."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.opportunity_semantic_grouping_eval import (  # noqa: E402
    FROZEN_EXTRACTION_PROMPT_SHA256,
    FROZEN_EXTRACTION_PROMPT_VERSION,
    FROZEN_EXTRACTION_SCHEMA_SHA256,
    canonical_json,
    file_sha256,
    gold_packet_and_bindings,
    sha256_text,
)
from wahojobs.opportunity_semantic_contract import (  # noqa: E402
    CONTRACT_VERSION,
    project_legacy_compatibility,
)
from wahojobs.opportunity_semantic_evaluation import (  # noqa: E402
    model_payload_from_reviewed_contract,
)
from wahojobs.opportunity_semantic_grouping import (  # noqa: E402
    GROUPING_MODEL,
    GROUPING_REASONING_EFFORT,
    HYBRID_GROUPING_STRATEGY_VERSION,
    SAFE_SINGLETON_COMPLETER_VERSION,
    complete_safe_singletons,
    project_validated_grouping,
    validate_frozen_atom_output,
    validate_model_grouping,
)
from wahojobs.opportunity_semantic_grouping_evaluation import (  # noqa: E402
    GROUPING_ACCEPTANCE_CRITERIA,
    GROUPING_EVALUATION_VERSION,
    aggregate_grouping_scores,
    grouping_acceptance,
    grouping_evaluation_sha256,
    score_grouping_case,
)


REPORT_VERSION = "oe_semantic_grouping_v0_hybrid_gold_closure_report_v1"
STORED_GROUPING_PROMPT_VERSION = "oe_semantic_grouping_v0_prompt_v1"
STORED_GROUPING_PROMPT_SHA256 = (
    "7875a3db9a1687bebacf7270ebe80f422cf2150db95dd86cd73b766e95784773"
)
STORED_GROUPING_SCHEMA_VERSION = "oe_semantic_grouping_v0_schema_v1"
STORED_GROUPING_SCHEMA_SHA256 = (
    "48819e9887548ef0e446c818e4b923ac34b3c5533672b3e1597546507db14700"
)
STORED_GROUPING_REPORT_SHA256 = (
    "ea0e5e8cf1e6eeb595919976b70b804241dcf361791eb892a3ab44d534fa6b8c"
)
DEFAULT_GOLD = ROOT / "tests" / "fixtures" / "opportunity_semantic_contract_v0.json"
DEFAULT_FROZEN_EXTRACTION = (
    ROOT / "exports" / "opportunity_semantic_extraction_v0_evaluation_v3.json"
)
DEFAULT_STORED_GROUPING = (
    ROOT / "exports" / "opportunity_semantic_grouping_v0_evaluation.json"
)
DEFAULT_OUTPUT = (
    ROOT / "exports" / "opportunity_semantic_grouping_v0_hybrid_gold_closure.json"
)


def validate_stored_inputs(
    *,
    gold_path: Path,
    frozen_path: Path,
    grouping_path: Path,
    frozen_report: dict,
    grouping_report: dict,
) -> None:
    if file_sha256(grouping_path) != STORED_GROUPING_REPORT_SHA256:
        raise ValueError("stored grouping-v1 report hash changed")
    frozen_identities = frozen_report.get("frozen_identities") or {}
    if frozen_identities.get("prompt_version") != FROZEN_EXTRACTION_PROMPT_VERSION:
        raise ValueError("frozen atom report is not prompt v3")
    if frozen_identities.get("prompt_sha256") != FROZEN_EXTRACTION_PROMPT_SHA256:
        raise ValueError("frozen atom prompt changed")
    if frozen_identities.get("schema_sha256") != FROZEN_EXTRACTION_SCHEMA_SHA256:
        raise ValueError("frozen atom schema changed")
    identities = grouping_report.get("frozen_identities") or {}
    expected = {
        "grouping_prompt_version": STORED_GROUPING_PROMPT_VERSION,
        "grouping_prompt_sha256": STORED_GROUPING_PROMPT_SHA256,
        "grouping_schema_version": STORED_GROUPING_SCHEMA_VERSION,
        "grouping_schema_sha256": STORED_GROUPING_SCHEMA_SHA256,
        "model": GROUPING_MODEL,
        "reasoning_effort": GROUPING_REASONING_EFFORT,
        "grouping_evaluation_version": GROUPING_EVALUATION_VERSION,
        "grouping_evaluation_sha256": grouping_evaluation_sha256(),
        "gold_fixture_sha256": file_sha256(gold_path),
        "frozen_extraction_report_sha256": file_sha256(frozen_path),
    }
    for key, value in expected.items():
        if identities.get(key) != value:
            raise ValueError(f"stored grouping-v1 identity changed: {key}")
    if grouping_report.get("atom_extraction_provider_calls") != 0:
        raise ValueError("stored grouping report does not preserve atom replay")
    if len((grouping_report.get("gold") or {}).get("cases") or {}) != 20:
        raise ValueError("stored grouping report is not the frozen 20-case gold")


def final_grouping_payload(group_validation: dict) -> dict:
    evidence_by_group = {
        item["group_index"]: {
            "alias": item["alias"],
            "quote": item["quote"],
        }
        for item in group_validation["accepted_group_evidence"]
    }
    groups = []
    for index, group in enumerate(group_validation["accepted_model_groups"]):
        groups.append(
            {
                **copy.deepcopy(group),
                "evidence": [copy.deepcopy(evidence_by_group[index])],
            }
        )
    return {
        "grouping_version": "oe_semantic_grouping_v0",
        "constraint_groups": groups,
        "ungrouped_atom_ids": copy.deepcopy(
            group_validation["ungrouped_atom_ids"]
        ),
    }


def replay_case(
    *,
    case: dict,
    frozen_record: dict,
    stored_grouping_record: dict,
) -> dict:
    packet, bindings = gold_packet_and_bindings(case)
    frozen_payload = copy.deepcopy(frozen_record["raw_extraction"])
    stored_grouping = copy.deepcopy(stored_grouping_record["raw_grouping"])
    atom_validation = validate_frozen_atom_output(frozen_payload, packet, bindings)
    model_group_validation = validate_model_grouping(
        stored_grouping,
        atom_validation,
        packet,
        bindings,
    )
    hybrid_validation = complete_safe_singletons(
        model_group_validation,
        atom_validation,
        packet,
        bindings,
    )
    hybrid_payload = final_grouping_payload(hybrid_validation)
    projection = project_validated_grouping(hybrid_validation)
    expected_payload = model_payload_from_reviewed_contract(case["contract"])
    expected_projection = project_legacy_compatibility(
        case["contract"], case["evidence_catalog"]
    )
    score = score_grouping_case(
        expected_payload,
        frozen_payload,
        atom_validation,
        hybrid_payload,
        hybrid_validation,
        projection,
        expected_projection,
    )
    return {
        "case_id": case["id"],
        "family": case.get("family"),
        "source_packet_sha256": sha256_text(canonical_json(packet)),
        "frozen_raw_extraction_sha256": sha256_text(
            canonical_json(frozen_payload)
        ),
        "stored_grouping_v1_sha256": sha256_text(
            canonical_json(stored_grouping)
        ),
        "expected_extraction": expected_payload,
        "expected_projection": expected_projection,
        "atom_validation": atom_validation,
        "stored_grouping_v1": stored_grouping,
        "model_group_validation": model_group_validation,
        "hybrid_grouping": hybrid_payload,
        "hybrid_validation": hybrid_validation,
        "projection": projection,
        "score": score,
        "status": "completed",
    }


def build_report(args) -> dict:
    gold_path = args.gold.resolve()
    frozen_path = args.frozen_extraction.resolve()
    grouping_path = args.stored_grouping.resolve()
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    grouping = json.loads(grouping_path.read_text(encoding="utf-8"))
    validate_stored_inputs(
        gold_path=gold_path,
        frozen_path=frozen_path,
        grouping_path=grouping_path,
        frozen_report=frozen,
        grouping_report=grouping,
    )

    frozen_cases = frozen["gold"]["cases"]
    stored_cases = grouping["gold"]["cases"]
    cases = {}
    scores = []
    for case in gold["cases"]:
        case_id = case["id"]
        record = replay_case(
            case=case,
            frozen_record=frozen_cases[case_id],
            stored_grouping_record=stored_cases[case_id],
        )
        cases[case_id] = record
        scores.append((case_id, record["score"]))

    aggregate = aggregate_grouping_scores(scores)
    acceptance = grouping_acceptance(aggregate)
    completed = [
        {
            "case_id": case_id,
            **item,
        }
        for case_id, record in cases.items()
        for item in record["hybrid_validation"]["completed_singletons"]
    ]
    still_ungrouped = [
        {"case_id": case_id, "atom_id": atom_id}
        for case_id, record in cases.items()
        for atom_id in record["hybrid_validation"]["ungrouped_atom_ids"]
    ]
    return {
        "report_version": REPORT_VERSION,
        "mode": "offline_zero_provider_hybrid_grouping_gold_replay",
        "identities": {
            "semantic_contract_version": CONTRACT_VERSION,
            "hybrid_grouping_strategy_version": HYBRID_GROUPING_STRATEGY_VERSION,
            "safe_singleton_completer_version": SAFE_SINGLETON_COMPLETER_VERSION,
            "stored_grouping_prompt_version": STORED_GROUPING_PROMPT_VERSION,
            "stored_grouping_prompt_sha256": STORED_GROUPING_PROMPT_SHA256,
            "stored_grouping_schema_version": STORED_GROUPING_SCHEMA_VERSION,
            "stored_grouping_schema_sha256": STORED_GROUPING_SCHEMA_SHA256,
            "stored_grouping_report_sha256": file_sha256(grouping_path),
            "frozen_extraction_prompt_version": FROZEN_EXTRACTION_PROMPT_VERSION,
            "frozen_extraction_prompt_sha256": FROZEN_EXTRACTION_PROMPT_SHA256,
            "frozen_extraction_schema_sha256": FROZEN_EXTRACTION_SCHEMA_SHA256,
            "frozen_extraction_report_sha256": file_sha256(frozen_path),
            "gold_fixture_sha256": file_sha256(gold_path),
            "evaluation_version": GROUPING_EVALUATION_VERSION,
            "evaluation_sha256": grouping_evaluation_sha256(),
            "evaluation_criteria": copy.deepcopy(GROUPING_ACCEPTANCE_CRITERIA),
            "model": GROUPING_MODEL,
            "reasoning_effort": GROUPING_REASONING_EFFORT,
        },
        "provider_calls": 0,
        "atom_extraction_provider_calls": 0,
        "grouping_provider_calls": 0,
        "gold": {
            "case_count": len(cases),
            "cases": cases,
            "aggregate": aggregate,
            "acceptance": acceptance,
            "model_created_group_count": sum(
                len(record["model_group_validation"]["accepted_model_groups"])
                for record in cases.values()
            ),
            "server_completed_singleton_count": len(completed),
            "server_completed_singletons": completed,
            "still_ungrouped_atoms": still_ungrouped,
        },
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Replay stored grouping v1 with deterministic singleton completion."
    )
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument(
        "--frozen-extraction", type=Path, default=DEFAULT_FROZEN_EXTRACTION
    )
    parser.add_argument(
        "--stored-grouping", type=Path, default=DEFAULT_STORED_GROUPING
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    report = build_report(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "provider_calls": report["provider_calls"],
                "acceptance": report["gold"]["acceptance"],
                "aggregate": report["gold"]["aggregate"],
                "model_created_group_count": report["gold"][
                    "model_created_group_count"
                ],
                "server_completed_singletons": report["gold"][
                    "server_completed_singletons"
                ],
                "still_ungrouped_atoms": report["gold"][
                    "still_ungrouped_atoms"
                ],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Offline two-stage OE Semantic Grouping v0 gold evaluation."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wahojobs.opportunity_semantic_contract import (  # noqa: E402
    CONTRACT_VERSION,
    project_legacy_compatibility,
)
from wahojobs.opportunity_semantic_evaluation import (  # noqa: E402
    model_payload_from_reviewed_contract,
)
from wahojobs.opportunity_semantic_grouping import (  # noqa: E402
    GROUPING_MAX_OUTPUT_TOKENS,
    GROUPING_MODEL,
    GROUPING_PROMPT_VERSION,
    GROUPING_REASONING_EFFORT,
    GROUPING_SCHEMA_VERSION,
    GROUPING_VALIDATOR_VERSION,
    GROUPING_VERSION,
    OpenAISemanticGroupingClient,
    accepted_evidence_aliases,
    grouping_prompt_sha256,
    grouping_schema_sha256,
    packet_grouping_schema_sha256,
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


REPORT_VERSION = "oe_semantic_grouping_v0_evaluation_report_v1"
FROZEN_EXTRACTION_PROMPT_VERSION = "oe_semantic_extraction_v0_prompt_v3"
FROZEN_EXTRACTION_PROMPT_SHA256 = (
    "434092fe85c6043df3ea2c134bc914584ce07ef26d7c66c09ed00e9f86fe537d"
)
FROZEN_EXTRACTION_SCHEMA_SHA256 = (
    "ba1766f4f00fc782d0e8a63bc208d2e3c73d8462fd0a8e4c96776c6167440124"
)
DEFAULT_GOLD = ROOT / "tests" / "fixtures" / "opportunity_semantic_contract_v0.json"
DEFAULT_FROZEN_EXTRACTION = (
    ROOT / "exports" / "opportunity_semantic_extraction_v0_evaluation_v3.json"
)
DEFAULT_OUTPUT = (
    ROOT / "exports" / "opportunity_semantic_grouping_v0_evaluation.json"
)


def canonical_json(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def gold_packet_and_bindings(case: dict) -> tuple[dict, list[dict]]:
    blocks = []
    bindings = []
    for source in case["evidence_catalog"]:
        blocks.append(
            {
                "evidence_block_id": source["id"],
                "source_ref": source["id"],
                "source_refs": [source["id"]],
                "variant_refs": [f"fixture:{case['id']}"],
                "authority_refs": [],
                "kind": "body_paragraph",
                "authority_class": "accepted_body_evidence",
                "label": "reviewed semantic-contract evidence",
                "content": source["text"],
            }
        )
        bindings.append(
            {
                "alias": source["id"],
                "authority": source["authority"],
                "text": source["text"],
                "text_sha256": source["text_sha256"],
                "provenance": copy.deepcopy(source["provenance"]),
            }
        )
    return (
        {
            "company": {"name": "Reviewed OE Semantic Contract fixture"},
            "canonical": {"canonical_title": case["id"]},
            "variants": [],
            "evidence_blocks": blocks,
        },
        bindings,
    )


def validate_frozen_report(report: dict, gold_sha256: str) -> None:
    identities = report.get("frozen_identities") or {}
    if identities.get("prompt_version") != FROZEN_EXTRACTION_PROMPT_VERSION:
        raise ValueError("frozen extraction report is not prompt v3")
    if identities.get("prompt_sha256") != FROZEN_EXTRACTION_PROMPT_SHA256:
        raise ValueError("frozen extraction prompt hash changed")
    if identities.get("schema_sha256") != FROZEN_EXTRACTION_SCHEMA_SHA256:
        raise ValueError("frozen extraction schema hash changed")
    if identities.get("model") != GROUPING_MODEL:
        raise ValueError("frozen extraction model is not Terra")
    if identities.get("reasoning_effort") != "low":
        raise ValueError("frozen extraction report is not the low arm")
    gold = report.get("gold") or {}
    if gold.get("fixture_sha256") != gold_sha256:
        raise ValueError("frozen extraction gold fixture changed")
    cases = gold.get("cases") or {}
    if len(cases) != 20:
        raise ValueError("frozen extraction report is not the 20-case gold")
    for case_id, record in cases.items():
        if record.get("status") != "completed" or not record.get("raw_extraction"):
            raise ValueError(f"frozen atom output is unavailable for {case_id}")


def frozen_identities(frozen_report_path: Path, gold_path: Path) -> dict:
    return {
        "semantic_contract_version": CONTRACT_VERSION,
        "grouping_version": GROUPING_VERSION,
        "grouping_prompt_version": GROUPING_PROMPT_VERSION,
        "grouping_prompt_sha256": grouping_prompt_sha256(),
        "grouping_schema_version": GROUPING_SCHEMA_VERSION,
        "grouping_schema_sha256": grouping_schema_sha256(),
        "grouping_validator_version": GROUPING_VALIDATOR_VERSION,
        "grouping_evaluation_version": GROUPING_EVALUATION_VERSION,
        "grouping_evaluation_sha256": grouping_evaluation_sha256(),
        "grouping_evaluation_criteria": copy.deepcopy(
            GROUPING_ACCEPTANCE_CRITERIA
        ),
        "model": GROUPING_MODEL,
        "reasoning_effort": GROUPING_REASONING_EFFORT,
        "max_output_tokens": GROUPING_MAX_OUTPUT_TOKENS,
        "frozen_extraction_prompt_version": FROZEN_EXTRACTION_PROMPT_VERSION,
        "frozen_extraction_prompt_sha256": FROZEN_EXTRACTION_PROMPT_SHA256,
        "frozen_extraction_schema_sha256": FROZEN_EXTRACTION_SCHEMA_SHA256,
        "frozen_extraction_report_sha256": file_sha256(frozen_report_path),
        "gold_fixture_sha256": file_sha256(gold_path),
    }


def new_report(frozen_report_path: Path, gold_path: Path) -> dict:
    return {
        "report_version": REPORT_VERSION,
        "mode": "offline_non_persisting_two_stage_grouping_evaluation",
        "frozen_identities": frozen_identities(frozen_report_path, gold_path),
        "atom_extraction_provider_calls": 0,
        "gold": None,
    }


def read_report(path: Path, frozen_report_path: Path, gold_path: Path) -> dict:
    if not path.exists():
        return new_report(frozen_report_path, gold_path)
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("report_version") != REPORT_VERSION:
        raise ValueError("existing grouping report has the wrong version")
    if report.get("frozen_identities") != frozen_identities(
        frozen_report_path, gold_path
    ):
        raise ValueError("existing grouping report has different frozen identities")
    return report


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def response_metadata(result) -> dict:
    metadata = asdict(result)
    metadata.pop("payload", None)
    return metadata


def usage_summary(cases: dict) -> dict:
    responses = [
        record["response"]
        for record in cases.values()
        if record.get("status") == "completed"
    ]
    return {
        "provider_calls": sum(response["provider_called"] for response in responses),
        "input_tokens": sum(response["input_tokens"] for response in responses),
        "cached_input_tokens": sum(
            response["cached_input_tokens"] for response in responses
        ),
        "output_tokens": sum(response["output_tokens"] for response in responses),
        "reasoning_tokens": sum(
            response["reasoning_tokens"] for response in responses
        ),
        "visible_output_tokens": sum(
            response["visible_output_tokens"] for response in responses
        ),
        "total_tokens": sum(response["total_tokens"] for response in responses),
        "total_latency_seconds": round(
            sum(response["latency_seconds"] for response in responses), 6
        ),
        "estimated_cost_usd": round(
            sum(response["estimated_cost_usd"] or 0.0 for response in responses),
            6,
        ),
        "returned_models": sorted(
            {
                response["response_model"]
                for response in responses
                if response.get("response_model")
            }
        ),
    }


def run_case(
    *,
    api_key: str,
    case: dict,
    frozen_record: dict,
) -> dict:
    packet, bindings = gold_packet_and_bindings(case)
    frozen_payload = copy.deepcopy(frozen_record["raw_extraction"])
    atom_validation = validate_frozen_atom_output(
        frozen_payload, packet, bindings
    )
    client = OpenAISemanticGroupingClient(api_key, model=GROUPING_MODEL)
    result = client.group(packet, atom_validation)
    group_validation = validate_model_grouping(
        result.payload, atom_validation, packet, bindings
    )
    projection = project_validated_grouping(group_validation)
    expected_payload = model_payload_from_reviewed_contract(case["contract"])
    expected_projection = project_legacy_compatibility(
        case["contract"], case["evidence_catalog"]
    )
    score = score_grouping_case(
        expected_payload,
        frozen_payload,
        atom_validation,
        result.payload,
        group_validation,
        projection,
        expected_projection,
    )
    aliases = accepted_evidence_aliases(packet)
    return {
        "case_id": case["id"],
        "family": case.get("family"),
        "review_note": case.get("review_note"),
        "source_packet_sha256": sha256_text(canonical_json(packet)),
        "frozen_raw_extraction_sha256": sha256_text(
            canonical_json(frozen_payload)
        ),
        "accepted_evidence_alias_count": len(aliases),
        "packet_grouping_schema_sha256": packet_grouping_schema_sha256(
            atom_validation["validated_atom_ids"], aliases
        ),
        "expected_extraction": expected_payload,
        "expected_projection": expected_projection,
        "frozen_raw_extraction": frozen_payload,
        "atom_validation": atom_validation,
        "response": response_metadata(result),
        "raw_grouping": result.payload,
        "group_validation": group_validation,
        "projection": projection,
        "score": score,
    }


def finalize(section: dict) -> None:
    completed = [
        (case_id, record["score"])
        for case_id, record in section["cases"].items()
        if record.get("status") == "completed"
    ]
    section["case_count"] = len(completed)
    section["aggregate"] = aggregate_grouping_scores(completed)
    section["acceptance"] = grouping_acceptance(section["aggregate"])
    section["usage"] = usage_summary(section["cases"])


def run_gold(args) -> int:
    api_key = str(os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required")
    gold_path = args.gold.resolve()
    frozen_path = args.frozen_extraction.resolve()
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    gold_sha = file_sha256(gold_path)
    validate_frozen_report(frozen, gold_sha)
    report = read_report(args.output, frozen_path, gold_path)
    section = report.get("gold")
    if section is None:
        section = {
            "fixture_version": gold["fixture_version"],
            "fixture_sha256": gold_sha,
            "frozen_extraction_report_sha256": file_sha256(frozen_path),
            "cases": {},
        }
        report["gold"] = section

    frozen_cases = frozen["gold"]["cases"]
    total = len(gold["cases"])
    for index, case in enumerate(gold["cases"], start=1):
        case_id = case["id"]
        existing = section["cases"].get(case_id)
        if existing and existing.get("status") == "completed":
            print(f"grouping {index}/{total} {case_id}: resume-skip", flush=True)
            continue
        print(f"grouping {index}/{total} {case_id}: grouping", flush=True)
        record = run_case(
            api_key=api_key,
            case=case,
            frozen_record=frozen_cases[case_id],
        )
        record["status"] = "completed"
        section["cases"][case_id] = record
        write_report(args.output, report)
        score = record["score"]
        print(
            f"grouping {index}/{total} {case_id}: "
            f"groups={score['grouping']['exact_groups']}/"
            f"{score['grouping']['expected_groups']} "
            f"ungrouped={len(score['grouping']['ungrouped_atom_ids'])} "
            f"ready={score['two_stage_human_ready']}",
            flush=True,
        )

    finalize(section)
    write_report(args.output, report)
    print(
        json.dumps(
            {
                "acceptance": section["acceptance"],
                "aggregate": section["aggregate"],
                "usage": section["usage"],
                "atom_extraction_provider_calls": report[
                    "atom_extraction_provider_calls"
                ],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Offline two-stage OE Semantic Grouping v0 evaluation."
    )
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument(
        "--frozen-extraction",
        type=Path,
        default=DEFAULT_FROZEN_EXTRACTION,
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    return run_gold(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())

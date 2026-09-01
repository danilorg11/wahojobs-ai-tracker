#!/usr/bin/env python3
"""Bounded, non-persisting OE Semantic Extraction v0 evaluation harness."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wahojobs.opportunity_enrichment import (  # noqa: E402
    llm_source_packet,
    load_semantic_input,
    semantic_input_sha256,
)
from wahojobs.opportunity_semantic_contract import (  # noqa: E402
    CONTRACT_VERSION,
)
from wahojobs.opportunity_semantic_evaluation import (  # noqa: E402
    EVALUATION_CRITERIA_VERSION,
    REGRESSION_ACCEPTANCE_CRITERIA,
    aggregate_scores,
    evaluation_criteria_sha256,
    model_payload_from_reviewed_contract,
    regression_acceptance,
    score_case,
)
from wahojobs.opportunity_semantic_extraction import (  # noqa: E402
    DEFAULT_MODEL,
    EXTRACTION_CONTRACT_VERSION,
    MAX_OUTPUT_TOKENS,
    PROMPT_VERSION,
    REASONING_EFFORT,
    SCHEMA_VERSION,
    VALIDATOR_VERSION,
    OpenAISemanticExtractionClient,
    accepted_evidence_aliases,
    packet_schema_sha256,
    project_validated_extraction,
    prompt_sha256,
    schema_sha256,
    validate_model_extraction,
)


REPORT_VERSION = "oe_semantic_extraction_v0_evaluation_report_v1"
FRESH_PREREGISTRATION_VERSION = (
    "oe_semantic_extraction_v0_fresh_preregistration_v1"
)
DEFAULT_DATABASE = ROOT / "data" / "wahojobs.sqlite"
DEFAULT_GOLD = ROOT / "tests" / "fixtures" / "opportunity_semantic_contract_v0.json"
DEFAULT_FRESH = (
    ROOT
    / "tests"
    / "fixtures"
    / "opportunity_semantic_extraction_v0_fresh_canary.json"
)
DEFAULT_OUTPUT = (
    ROOT / "exports" / "opportunity_semantic_extraction_v0_evaluation_v3.json"
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


def frozen_identities() -> dict:
    return {
        "semantic_contract_version": CONTRACT_VERSION,
        "extraction_contract_version": EXTRACTION_CONTRACT_VERSION,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": prompt_sha256(),
        "schema_version": SCHEMA_VERSION,
        "schema_sha256": schema_sha256(),
        "validator_version": VALIDATOR_VERSION,
        "evaluation_criteria_version": EVALUATION_CRITERIA_VERSION,
        "evaluation_criteria_sha256": evaluation_criteria_sha256(),
        "evaluation_criteria": copy.deepcopy(REGRESSION_ACCEPTANCE_CRITERIA),
        "model": DEFAULT_MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
    }


def new_report() -> dict:
    return {
        "report_version": REPORT_VERSION,
        "mode": "offline_non_persisting_evaluation",
        "frozen_identities": frozen_identities(),
        "gold": None,
        "fresh_canary": None,
    }


def read_report(path: Path) -> dict:
    if not path.exists():
        return new_report()
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("report_version") != REPORT_VERSION:
        raise ValueError("existing evaluation report has the wrong version")
    if report.get("frozen_identities") != frozen_identities():
        raise ValueError("existing evaluation report has different frozen identities")
    return report


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


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


def expected_result(expected_payload, packet, bindings):
    validation = validate_model_extraction(expected_payload, packet, bindings)
    if validation["rejected_atoms"] or validation["rejected_groups"]:
        raise ValueError("reviewed expectation does not pass the frozen validator")
    return validation, project_validated_extraction(validation)


def response_metadata(result) -> dict:
    metadata = asdict(result)
    metadata.pop("payload", None)
    return metadata


def run_one_case(
    *,
    api_key: str,
    case_id: str,
    title: str,
    packet: dict,
    bindings: list[dict],
    expected_payload: dict,
    review_note: str | None,
) -> dict:
    expected_validation, expected_projection = expected_result(
        expected_payload, packet, bindings
    )
    aliases = accepted_evidence_aliases(packet)
    client = OpenAISemanticExtractionClient(api_key, model=DEFAULT_MODEL)
    result = client.extract(packet)
    validation = validate_model_extraction(result.payload, packet, bindings)
    projection = project_validated_extraction(validation)
    score = score_case(
        expected_payload,
        result.payload,
        validation,
        projection,
        expected_projection,
    )
    return {
        "case_id": case_id,
        "title": title,
        "review_note": review_note,
        "source_packet_sha256": sha256_text(canonical_json(packet)),
        "accepted_evidence_alias_count": len(aliases),
        "packet_schema_sha256": packet_schema_sha256(aliases),
        "expected_extraction": expected_payload,
        "expected_projection": expected_projection,
        "response": response_metadata(result),
        "raw_extraction": result.payload,
        "validation": validation,
        "projection": projection,
        "score": score,
    }


def usage_summary(cases: dict) -> dict:
    responses = [
        record["response"]
        for record in cases.values()
        if record.get("status") == "completed"
    ]
    costs = [
        response["estimated_cost_usd"]
        for response in responses
        if response["estimated_cost_usd"] is not None
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
        "estimated_cost_usd": round(sum(costs), 8) if len(costs) == len(responses) else None,
        "returned_models": sorted(
            {
                response["response_model"]
                for response in responses
                if response["response_model"]
            }
        ),
    }


def stage_attribution(aggregate: dict) -> dict:
    extraction = aggregate["extraction"]
    validation = aggregate["validation"]
    projection = aggregate["projection"]
    return {
        "model_extraction": {
            "material_false_positives": copy.deepcopy(
                extraction["material_false_positives"]
            ),
            "material_false_negatives": copy.deepcopy(
                extraction["material_false_negatives"]
            ),
            "atom_kind_errors": extraction["expected_atoms"]
            - extraction["atom_kind_correct"],
            "payload_errors": extraction["expected_atoms"]
            - extraction["payload_correct"],
            "polarity_errors": extraction["expected_atoms"]
            - extraction["polarity_correct"],
            "temporal_errors": extraction["expected_atoms"]
            - extraction["temporal_correct"],
            "required_preferred_errors": extraction[
                "required_preferred_denominator"
            ]
            - extraction["required_preferred_correct"],
            "group_structure_errors": extraction["expected_groups"]
            - extraction["group_structure_correct"],
        },
        "validation": {
            "rejected_atoms": validation["rejected_atoms"],
            "rejected_groups": validation["rejected_groups"],
            "rejection_reasons": copy.deepcopy(validation["rejection_reasons"]),
            "unsupported_propositions_survived": copy.deepcopy(
                validation["unsupported_propositions_survived"]
            ),
            "false_rejection_candidates": copy.deepcopy(
                validation["validator_false_rejection_candidates"]
            ),
        },
        "projection": {
            "projection_failures": copy.deepcopy(
                projection["projection_failures"]
            ),
            "unsafe_hard_gate_projections": copy.deepcopy(
                projection["unsafe_hard_gate_projections"]
            ),
            "grounded_unprojected_groups": projection[
                "grounded_unprojected_groups"
            ],
            "grounded_unprojected_coverage": projection[
                "grounded_unprojected_coverage"
            ],
        },
    }


def finalize_section(section: dict) -> None:
    completed = [
        (case_id, record["score"])
        for case_id, record in sorted(section["cases"].items())
        if record.get("status") == "completed"
    ]
    failed = [
        case_id
        for case_id, record in sorted(section["cases"].items())
        if record.get("status") != "completed"
    ]
    section["aggregate"] = aggregate_scores(completed)
    section["usage"] = usage_summary(section["cases"])
    section["error_attribution"] = stage_attribution(section["aggregate"])
    section["failed_case_ids"] = failed


def require_api_key() -> str:
    value = os.environ.get("OPENAI_API_KEY", "").strip()
    if not value:
        raise RuntimeError("OPENAI_API_KEY is unavailable")
    return value


def run_gold(args) -> int:
    api_key = require_api_key()
    fixture = json.loads(args.gold.read_text(encoding="utf-8"))
    report = read_report(args.output)
    section = report.get("gold")
    fixture_hash = file_sha256(args.gold)
    if section is None:
        section = {
            "fixture_version": fixture["fixture_version"],
            "fixture_sha256": fixture_hash,
            "case_count": len(fixture["cases"]),
            "cases": {},
        }
        report["gold"] = section
    if section["fixture_sha256"] != fixture_hash:
        raise ValueError("gold fixture changed after evaluation began")

    total = len(fixture["cases"])
    for index, case in enumerate(fixture["cases"], start=1):
        case_id = case["id"]
        if section["cases"].get(case_id, {}).get("status") == "completed":
            print(f"gold {index}/{total} {case_id}: resume-skip", flush=True)
            continue
        print(f"gold {index}/{total} {case_id}: extracting", flush=True)
        packet, bindings = gold_packet_and_bindings(case)
        expected_payload = model_payload_from_reviewed_contract(case["contract"])
        try:
            record = run_one_case(
                api_key=api_key,
                case_id=case_id,
                title=case_id,
                packet=packet,
                bindings=bindings,
                expected_payload=expected_payload,
                review_note=case.get("review_note"),
            )
            record["status"] = "completed"
            section["cases"][case_id] = record
            print(
                f"gold {index}/{total} {case_id}: "
                f"atoms={record['score']['extraction']['exact_atoms']}/"
                f"{record['score']['extraction']['expected_atoms']} "
                f"ready={record['score']['human_quality_semantic_ready']}",
                flush=True,
            )
        except Exception as exc:  # checkpoint public error class/message only
            section["cases"][case_id] = {
                "status": "failed",
                "case_id": case_id,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            write_report(args.output, report)
            raise
        finalize_section(section)
        section["acceptance"] = regression_acceptance(section["aggregate"])
        write_report(args.output, report)

    finalize_section(section)
    section["acceptance"] = regression_acceptance(section["aggregate"])
    write_report(args.output, report)
    print(
        json.dumps(
            {
                "aggregate": section["aggregate"],
                "acceptance": section["acceptance"],
                "usage": section["usage"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        flush=True,
    )
    return 0 if not section["failed_case_ids"] else 1


@contextmanager
def open_read_only(path: Path):
    resolved = path.resolve()
    connection = sqlite3.connect(
        f"file:{resolved.as_posix()}?mode=ro&immutable=1",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    try:
        yield connection
    finally:
        connection.close()


def job_ids_by_source_ref(connection, canonical_id: int) -> dict[str, int]:
    return {
        f"source_hash:{row['source_hash']}": int(row["id"])
        for row in connection.execute(
            """
            SELECT id, source_hash
            FROM jobs
            WHERE canonical_opportunity_id = ?
              AND title NOT LIKE '[SIMULATION]%'
            """,
            (canonical_id,),
        ).fetchall()
    }


def accepted_capture_bindings(
    connection,
    canonical_id: int,
    semantic_input: dict,
    packet: dict,
) -> list[dict]:
    job_ids = job_ids_by_source_ref(connection, canonical_id)
    sources = {
        source["source_ref"]: source
        for source in semantic_input.get("rich_content") or []
    }
    bindings = []
    for block in packet.get("evidence_blocks") or []:
        if block.get("authority_class") != "accepted_body_evidence":
            continue
        source = None
        for source_ref in block.get("source_refs") or [block.get("source_ref")]:
            candidate = sources.get(source_ref)
            authority = (candidate or {}).get("authority") or {}
            if (
                candidate is not None
                and authority.get("semantic_authority_state")
                == "versioned_accepted"
                and authority.get("accepted_capture_ref")
            ):
                source = candidate
                break
        if source is None:
            raise ValueError(
                f"accepted alias {block.get('evidence_block_id')!r} lacks authority"
            )
        capture_ref = source["authority"]["accepted_capture_ref"]
        match = re.fullmatch(r"source_capture:(\d+)", capture_ref)
        if match is None:
            raise ValueError("accepted capture reference is malformed")
        source_ref = source["source_ref"]
        if source_ref not in job_ids:
            raise ValueError("accepted source does not map to a selected job")
        text = block["content"]
        bindings.append(
            {
                "alias": block["evidence_block_id"],
                "authority": "accepted_capture",
                "text": text,
                "text_sha256": sha256_text(text),
                "provenance": {
                    "accepted_capture_id": int(match.group(1)),
                    "job_id": job_ids[source_ref],
                    "material_content_sha256": source[
                        "material_content_sha256"
                    ],
                    "source_url": source["source_url"],
                },
            }
        )
    return bindings


def database_identity(path: Path) -> dict:
    item = path.stat()
    return {
        "path": str(path.resolve()),
        "sha256": file_sha256(path),
        "size": item.st_size,
        "mtime_ns": item.st_mtime_ns,
    }


def validate_preregistration(document: dict, report: dict) -> None:
    if document.get("schema_version") != FRESH_PREREGISTRATION_VERSION:
        raise ValueError("fresh preregistration has the wrong version")
    if document.get("frozen_identities") != frozen_identities():
        raise ValueError("fresh preregistration does not match frozen identities")
    if not report.get("gold", {}).get("acceptance", {}).get("satisfactory"):
        raise ValueError("gold regression did not satisfy preregistered criteria")
    excluded = set(document.get("excluded_canonical_ids") or [])
    selected = [case["canonical_opportunity_id"] for case in document["cases"]]
    if len(selected) != len(set(selected)):
        raise ValueError("fresh preregistration repeats a canonical")
    overlap = excluded & set(selected)
    if overlap:
        raise ValueError(f"fresh sample overlaps excluded IDs: {sorted(overlap)}")


def run_fresh(args) -> int:
    api_key = require_api_key()
    preregistration = json.loads(args.fresh.read_text(encoding="utf-8"))
    report = read_report(args.output)
    validate_preregistration(preregistration, report)
    prereg_hash = file_sha256(args.fresh)
    before = database_identity(args.database)
    section = report.get("fresh_canary")
    if section is None:
        section = {
            "preregistration_version": preregistration["schema_version"],
            "preregistration_sha256": prereg_hash,
            "sample_size": len(preregistration["cases"]),
            "selection_policy": preregistration["selection_policy"],
            "diversity": preregistration["diversity"],
            "excluded_canonical_ids": preregistration[
                "excluded_canonical_ids"
            ],
            "canonical_ids": [
                case["canonical_opportunity_id"]
                for case in preregistration["cases"]
            ],
            "database_before": before,
            "cases": {},
        }
        report["fresh_canary"] = section
    if section["preregistration_sha256"] != prereg_hash:
        raise ValueError("fresh preregistration changed after evaluation began")
    if section["database_before"] != before:
        raise ValueError("database changed after fresh evaluation began")

    total = len(preregistration["cases"])
    with open_read_only(args.database) as connection:
        for index, reviewed in enumerate(preregistration["cases"], start=1):
            canonical_id = int(reviewed["canonical_opportunity_id"])
            case_id = str(canonical_id)
            if section["cases"].get(case_id, {}).get("status") == "completed":
                print(f"fresh {index}/{total} {canonical_id}: resume-skip", flush=True)
                continue
            semantic_input = load_semantic_input(connection, canonical_id)
            input_hash = semantic_input_sha256(semantic_input)
            if input_hash != reviewed["semantic_input_sha256"]:
                raise ValueError(
                    f"fresh semantic input changed for canonical {canonical_id}"
                )
            packet, _ = llm_source_packet(semantic_input)
            packet_hash = sha256_text(canonical_json(packet))
            if packet_hash != reviewed["source_packet_sha256"]:
                raise ValueError(
                    f"fresh source packet changed for canonical {canonical_id}"
                )
            bindings = accepted_capture_bindings(
                connection, canonical_id, semantic_input, packet
            )
            print(f"fresh {index}/{total} {canonical_id}: extracting", flush=True)
            try:
                record = run_one_case(
                    api_key=api_key,
                    case_id=case_id,
                    title=reviewed["title"],
                    packet=packet,
                    bindings=bindings,
                    expected_payload=reviewed["expected_extraction"],
                    review_note=reviewed.get("review_note"),
                )
                record["status"] = "completed"
                record["canonical_opportunity_id"] = canonical_id
                record["semantic_input_sha256"] = input_hash
                record["diversity_labels"] = reviewed["diversity_labels"]
                section["cases"][case_id] = record
                print(
                    f"fresh {index}/{total} {canonical_id}: "
                    f"atoms={record['score']['extraction']['exact_atoms']}/"
                    f"{record['score']['extraction']['expected_atoms']} "
                    f"ready={record['score']['human_quality_semantic_ready']}",
                    flush=True,
                )
            except Exception as exc:
                section["cases"][case_id] = {
                    "status": "failed",
                    "canonical_opportunity_id": canonical_id,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
                write_report(args.output, report)
                raise
            finalize_section(section)
            write_report(args.output, report)

    after = database_identity(args.database)
    section["database_after"] = after
    section["database_preserved"] = before == after
    if not section["database_preserved"]:
        write_report(args.output, report)
        raise RuntimeError("workspace database changed during fresh evaluation")
    finalize_section(section)
    write_report(args.output, report)
    print(
        json.dumps(
            {
                "aggregate": section["aggregate"],
                "usage": section["usage"],
                "database_preserved": section["database_preserved"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        flush=True,
    )
    return 0 if not section["failed_case_ids"] else 1


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    gold = subparsers.add_parser("gold", help="run the reviewed contract benchmark")
    gold.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    gold.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    gold.set_defaults(func=run_gold)

    fresh = subparsers.add_parser("fresh", help="run the frozen fresh canary")
    fresh.add_argument("--fresh", type=Path, default=DEFAULT_FRESH)
    fresh.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    fresh.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    fresh.set_defaults(func=run_fresh)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        return args.func(args)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(
            f"OE Semantic Extraction v0 evaluation failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

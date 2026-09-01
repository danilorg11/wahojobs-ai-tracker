#!/usr/bin/env python3
"""Replay the opened 16-case canary through the isolated staging proof."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wahojobs.opportunity_semantic_staging import (  # noqa: E402
    aggregate_replay,
    replay_case,
    role_activity_composition_supported,
)


REPORT_VERSION = "oe_semantic_staging_v0_offline_replay_report_v1"
RAW_ARTIFACT_SHA256 = (
    "37d6e7bea857274477ad08a89fef81023b3c1502c5064242ba4fe34c5540daf4"
)
PREREGISTRATION_SHA256 = (
    "177574a22812a1f6122b3a03e640848fd6f211a9d19476ff416ed7330f6fbd38"
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


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_artifacts(
    raw_path: Path,
    preregistration_path: Path,
    review_path: Path,
) -> tuple[dict, dict, dict, dict]:
    identities = {
        "raw_artifact": {
            "path": str(raw_path),
            "sha256": file_sha256(raw_path),
            "expected_sha256": RAW_ARTIFACT_SHA256,
        },
        "preregistration_fixture": {
            "path": str(preregistration_path),
            "sha256": file_sha256(preregistration_path),
            "expected_sha256": PREREGISTRATION_SHA256,
        },
    }
    for name, identity in identities.items():
        if identity["sha256"] != identity["expected_sha256"]:
            raise RuntimeError(f"{name} SHA-256 mismatch")
    raw = _read_json(raw_path)
    preregistration = _read_json(preregistration_path)
    review = _read_json(review_path)
    if review["raw_artifact_sha256"] != RAW_ARTIFACT_SHA256:
        raise RuntimeError("review fixture names a different raw artifact")
    if review["preregistration_fixture_sha256"] != PREREGISTRATION_SHA256:
        raise RuntimeError("review fixture names a different preregistration")
    if raw["preregistration"]["fixture_sha256"] != PREREGISTRATION_SHA256:
        raise RuntimeError("raw artifact names a different preregistration")
    if raw["fixture_before_sha256"] != PREREGISTRATION_SHA256:
        raise RuntimeError("raw artifact did not open the frozen preregistration")
    if not raw["fixture_preserved"] or not raw["database_preserved"]:
        raise RuntimeError("opened canary did not preserve its frozen inputs")
    raw_ids = list(raw["cases"])
    selected_ids = [
        str(value) for value in preregistration["selected_canonical_ids"]
    ]
    if raw_ids != selected_ids or set(review["cases"]) != set(raw_ids):
        raise RuntimeError("16-case identities do not match across frozen artifacts")
    return raw, preregistration, review, identities


def _composition_controls() -> dict:
    fact_checking_ai_output = {
        "kind": "role_activity",
        "typed_payload": {
            "activity": "fact_checking",
            "artifact": "ai_output",
        },
        "evidence": [
            {
                "quote": "Verify factual accuracy in the model output."
            }
        ],
    }
    generic_qa_as_software_testing = {
        "kind": "role_activity",
        "typed_payload": {
            "activity": "software_testing",
            "artifact": "software",
        },
        "evidence": [
            {
                "quote": "Perform generic quality assurance and fact-checking of generated tasks."
            }
        ],
    }
    return {
        "fact_checking_ai_output_supported": role_activity_composition_supported(
            fact_checking_ai_output
        ),
        "generic_qa_becomes_software_testing": role_activity_composition_supported(
            generic_qa_as_software_testing
        ),
    }


def _notable_values_satisfy_expectations(
    case_results: dict[str, dict], review: dict
) -> tuple[bool, list[str]]:
    missing = []
    for identity, expected_values in review["source_value_expectations"].items():
        case_id, atom_id = identity.split(":", 1)
        atom = next(
            item
            for item in case_results[case_id]["staging"]["provisional_atoms"]
            if item["proposal_id"] == atom_id
        )
        observed = {
            item["raw_value"].casefold()
            for item in atom["normalization"]["source_values"]
        }
        for expected in expected_values:
            if expected.casefold() not in observed:
                missing.append(f"{identity}:{expected}")
    return not missing, missing


def _acceptance(aggregate: dict, controls: dict, review: dict) -> dict:
    criteria = review["acceptance"]
    checks = {
        "provisional_atom_accounting": aggregate["provisional_atoms"][
            "accounting_rate"
        ]
        == criteria["required_provisional_atom_accounting"],
        "source_branch_accounting": aggregate["relations"][
            "source_branch_accounting_rate"
        ]
        == criteria["required_source_branch_accounting"],
        "source_values_and_qualifiers_safe": len(
            aggregate["normalization"][
                "finalized_source_value_substitution_or_qualifier_loss"
            ]
        )
        <= criteria[
            "maximum_source_value_substitutions_or_qualifier_losses_in_final_contract"
        ],
        "and_or_not_weakened": len(
            aggregate["relations"]["logic_weakening_cases"]
        )
        <= criteria["maximum_logic_weakening_cases"],
        "incomplete_relations_do_not_project": len(
            aggregate["relations"]["projected_from_incomplete_relation_ids"]
        )
        <= criteria["maximum_projection_from_incomplete_relations"],
        "reviewed_unsupported_does_not_project": len(
            aggregate["projection"]["reviewed_unsupported_projected_atom_ids"]
        )
        <= criteria["maximum_reviewed_unsupported_projected_atoms"],
        "supported_proposal_retention": aggregate[
            "supported_proposal_retention"
        ]["rate"]
        >= criteria["minimum_supported_retention"],
        "conditional_projection_precision": aggregate["projection"][
            "conditional_precision"
        ]
        == criteria["required_conditional_projection_precision"],
        "unsafe_hard_gates": len(aggregate["projection"]["unsafe_hard_gates"])
        <= criteria["maximum_unsafe_hard_gates"],
        "fact_checking_ai_output_representable": bool(
            aggregate["final_contract"]["fact_checking_ai_output"]
        )
        and controls["fact_checking_ai_output_supported"],
        "generic_qa_not_software_testing": not controls[
            "generic_qa_becomes_software_testing"
        ],
        "legacy_25_of_72_is_reproduced": aggregate[
            "legacy_semantic_validator"
        ]["reviewed_supported_accepted"]
        == 25
        and aggregate["legacy_semantic_validator"][
            "reviewed_supported_rejected"
        ]
        == 47,
    }
    return {"checks": checks, "passed": all(checks.values())}


def build_report(
    raw_path: Path = DEFAULT_RAW,
    preregistration_path: Path = DEFAULT_PREREGISTRATION,
    review_path: Path = DEFAULT_REVIEW,
) -> dict:
    raw, preregistration, review, identities = _assert_artifacts(
        raw_path, preregistration_path, review_path
    )
    case_results = {
        case_id: replay_case(case, review["cases"][case_id])
        for case_id, case in raw["cases"].items()
    }
    aggregate = aggregate_replay(case_results)
    controls = _composition_controls()
    notable_ok, missing_values = _notable_values_satisfy_expectations(
        case_results, review
    )
    controls["source_value_expectations_preserved"] = notable_ok
    controls["missing_source_value_expectations"] = missing_values
    acceptance = _acceptance(aggregate, controls, review)
    acceptance["checks"]["source_value_expectations_preserved"] = notable_ok
    acceptance["passed"] = all(acceptance["checks"].values())
    return {
        "report_version": REPORT_VERSION,
        "mode": "offline_frozen_artifact_replay_no_provider_no_database",
        "artifacts": identities,
        "selected_canonical_ids": preregistration["selected_canonical_ids"],
        "raw_artifact_preservation": {
            "fixture_preserved": raw["fixture_preserved"],
            "database_preserved": raw["database_preserved"],
        },
        "reviewed_atom_counts": review["reviewed_atom_counts"],
        "case_metrics": {
            case_id: result["metrics"] for case_id, result in case_results.items()
        },
        "aggregate": aggregate,
        "composition_controls": controls,
        "acceptance": acceptance,
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    parser.add_argument(
        "--preregistration", type=Path, default=DEFAULT_PREREGISTRATION
    )
    parser.add_argument("--review", type=Path, default=DEFAULT_REVIEW)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    report = build_report(args.raw, args.preregistration, args.review)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["acceptance"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

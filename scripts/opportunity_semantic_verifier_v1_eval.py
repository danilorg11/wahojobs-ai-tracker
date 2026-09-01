#!/usr/bin/env python3
"""Freeze and run OE Semantic Verifier v1 on the opened 16-case set."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.opportunity_semantic_staging_replay import (  # noqa: E402
    DEFAULT_PREREGISTRATION,
    DEFAULT_RAW,
    DEFAULT_REVIEW,
    PREREGISTRATION_SHA256,
    RAW_ARTIFACT_SHA256,
    _assert_artifacts,
    file_sha256,
)
from scripts.opportunity_semantic_verifier_eval import (  # noqa: E402
    _run_provider,
)
from wahojobs.opportunity_semantic_staging import (  # noqa: E402
    SEMANTIC_DECISIONS,
    apply_semantic_verification,
    construct_relations,
    finalize_verified_relations,
    stage_provisional_atoms,
    verification_from_reviewed_labels,
)
from wahojobs.opportunity_semantic_verifier_v1 import (  # noqa: E402
    ATOM_SERIALIZER_VERSION,
    CHALLENGE_MODEL,
    HARD_CONSTRAINT_SERIALIZER_VERSION,
    OpenAIExactClaimVerifierClient,
    PRIMARY_MODEL,
    RELATION_SERIALIZER_VERSION,
    REASONING_EFFORT,
    STORE,
    hard_challenge_input,
    model_configuration_identity,
    model_configuration_sha256,
    primary_atom_input,
    primary_relation_input,
    prompt_sha256,
    schema_sha256,
    serializer_identities,
    validate_exact_input,
    validate_exact_result_record,
)


REFERENCE_VERSION = "oe_semantic_verifier_v1_reviewed_reference_v1"
MANIFEST_VERSION = "oe_semantic_verifier_v1_frozen_manifest_v1"
REPORT_VERSION = "oe_semantic_verifier_v1_exact_claim_evaluation_v1"
EVALUATOR_VERSION = "oe_semantic_verifier_v1_evaluator_v1"
HARD_SELECTION_VERSION = "oe_semantic_verifier_v1_hard_candidate_selection_v1"
DEFAULT_REFERENCE = (
    ROOT
    / "tests"
    / "fixtures"
    / "opportunity_semantic_verifier_v1_reviewed_reference.json"
)
DEFAULT_MANIFEST = (
    ROOT / "exports" / "opportunity_semantic_verifier_v1_manifest.json"
)
DEFAULT_OUTPUT = (
    ROOT / "exports" / "opportunity_semantic_verifier_v1_evaluation.json"
)
DEFAULT_PRIMARY_ACCOUNTING = (
    ROOT / "exports" / "opportunity_semantic_verifier_v1_primary_accounting.jsonl"
)
DEFAULT_CHALLENGE_ACCOUNTING = (
    ROOT / "exports" / "opportunity_semantic_verifier_v1_challenge_accounting.jsonl"
)

FROZEN_GATES = {
    "primary_atom_verifier": {
        "minimum_entailment_precision": 0.97,
        "minimum_supported_recall": 0.90,
        "maximum_reviewed_contradicted_marked_entails": 0,
    },
    "primary_relation_verifier": {
        "minimum_exact_relation_decision_accuracy": 0.95,
        "maximum_narrowed_or_and_marked_entails": 0,
        "maximum_incomplete_relation_marked_entails": 0,
    },
    "final_hard_assurance": {
        "maximum_reviewed_unsupported_hard_authorized": 0,
        "maximum_incomplete_relation_authorized": 0,
        "maximum_source_value_substitutions": 0,
        "maximum_or_and_weakening": 0,
        "required_conditional_hard_projection_precision": 1.0,
        "maximum_unsupported_deterministic_hard_gates": 0,
    },
}


def _canonical_json(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _sha256(value) -> str:
    if type(value) is not str:
        value = _canonical_json(value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _labels_by_atom(reviewed: dict) -> dict[str, str]:
    result = {}
    for decision in sorted(SEMANTIC_DECISIONS):
        for atom_id in reviewed[decision]:
            if atom_id in result:
                raise RuntimeError("duplicate reviewed atom decision")
            result[atom_id] = decision
    return result


def _all_entails_verification(staged: dict) -> dict:
    return {
        "verification_version": "oe_semantic_verification_v0",
        "decisions": [
            {
                "ledger_id": item["ledger_id"],
                "atom_sha256": item["atom_sha256"],
                "decision": "entails",
                "qualifier_decisions": {
                    "payload": "entails",
                    "polarity": "entails",
                    "temporal": "entails",
                },
            }
            for item in staged["provisional_atoms"]
            if item["status"] == "provisional"
        ],
    }


def _relation_atom_ids(relation: dict) -> list[str]:
    return [
        atom_id
        for alternative in relation["proposal"]["any_of"]
        for atom_id in alternative["all_of"]
    ]


def _reference_rows(reference: dict) -> dict[str, dict]:
    return {
        f"{case_id}:{relation_id}": copy.deepcopy(row)
        for case_id, relations in reference["relation_reference"]["cases"].items()
        for relation_id, row in relations.items()
    }


def _validate_reference(
    reference: dict,
    *,
    review_path: Path,
    relation_ids: set[str],
) -> dict[str, dict]:
    if reference.get("schema_version") != REFERENCE_VERSION:
        raise RuntimeError("v1 reviewed reference version mismatch")
    if not reference.get("review_locked_before_provider_calls"):
        raise RuntimeError("v1 reviewed reference is not frozen")
    if reference.get("raw_artifact_sha256") != RAW_ARTIFACT_SHA256:
        raise RuntimeError("v1 reviewed reference raw artifact mismatch")
    if (
        reference.get("preregistration_fixture_sha256")
        != PREREGISTRATION_SHA256
    ):
        raise RuntimeError("v1 reviewed reference preregistration mismatch")
    if (
        reference["atom_reference"]["source_fixture_sha256"]
        != file_sha256(review_path)
    ):
        raise RuntimeError("v1 atom reference fixture mismatch")
    rows = _reference_rows(reference)
    if set(rows) != relation_ids:
        raise RuntimeError("v1 direct relation reference population mismatch")
    counts = Counter(row["decision"] for row in rows.values())
    if dict(counts) != {
        key: value
        for key, value in reference["relation_reference"]["counts"].items()
        if key != "total"
    }:
        raise RuntimeError("v1 direct relation reference counts mismatch")
    if any(row["decision"] not in SEMANTIC_DECISIONS for row in rows.values()):
        raise RuntimeError("v1 direct relation decision is invalid")
    return rows


def build_population(
    raw_path: Path = DEFAULT_RAW,
    preregistration_path: Path = DEFAULT_PREREGISTRATION,
    review_path: Path = DEFAULT_REVIEW,
    reference_path: Path = DEFAULT_REFERENCE,
) -> tuple[dict, dict, dict, dict, dict, list[dict]]:
    raw, preregistration, review, artifacts = _assert_artifacts(
        raw_path, preregistration_path, review_path
    )
    reference = _read_json(reference_path)
    cases = {}
    tasks = []
    relation_ids = set()
    for case_id, case in raw["cases"].items():
        staged = stage_provisional_atoms(
            case["raw_extraction"],
            case["source_packet"],
            case["accepted_evidence_bindings"],
        )
        atom_labels = _labels_by_atom(review["cases"][case_id])
        reviewed_staging = apply_semantic_verification(
            copy.deepcopy(staged),
            verification_from_reviewed_labels(
                staged, review["cases"][case_id]
            ),
        )
        reviewed_relations = construct_relations(
            reviewed_staging, case.get("raw_grouping")
        )
        deterministic_staging = apply_semantic_verification(
            copy.deepcopy(staged), _all_entails_verification(staged)
        )
        deterministic_relations = construct_relations(
            deterministic_staging, case.get("raw_grouping")
        )
        reviewed_by_id = {
            item["relation_id"]: item
            for item in reviewed_relations["relations"]
        }
        deterministic_by_id = {
            item["relation_id"]: item
            for item in deterministic_relations["relations"]
        }
        if set(reviewed_by_id) != set(deterministic_by_id):
            raise RuntimeError("relation identity changed across semantic overlays")
        for relation_id in reviewed_by_id:
            left = reviewed_by_id[relation_id]
            right = deterministic_by_id[relation_id]
            if left.get("proposal") != right.get("proposal") or left.get(
                "source_branches"
            ) != right.get("source_branches"):
                raise RuntimeError("semantic overlay changed immutable relation")

        atom_packets = {}
        for item in staged["provisional_atoms"]:
            packet = primary_atom_input(item)
            atom_packets[item["proposal_id"]] = packet
            tasks.append(
                {
                    "key": f"primary:atom:{case_id}:{item['proposal_id']}",
                    "case_id": case_id,
                    "claim_local_id": item["proposal_id"],
                    "claim_type": "atom",
                    "packet": packet,
                }
            )
        relation_packets = {}
        for relation in deterministic_relations["relations"]:
            if relation["state"] == "unrepresentable_relation":
                continue
            relation_id = relation["relation_id"]
            packet = primary_relation_input(staged, relation)
            relation_packets[relation_id] = packet
            identity = f"{case_id}:{relation_id}"
            relation_ids.add(identity)
            tasks.append(
                {
                    "key": f"primary:relation:{case_id}:{relation_id}",
                    "case_id": case_id,
                    "claim_local_id": relation_id,
                    "claim_type": "relation",
                    "packet": packet,
                }
            )
        cases[case_id] = {
            "case": case,
            "staged": staged,
            "atom_labels": atom_labels,
            "reviewed_staging": reviewed_staging,
            "reviewed_relations": reviewed_relations,
            "deterministic_staging": deterministic_staging,
            "deterministic_relations": deterministic_relations,
            "atom_packets": atom_packets,
            "relation_packets": relation_packets,
        }
    relation_references = _validate_reference(
        reference, review_path=review_path, relation_ids=relation_ids
    )
    for case_id, data in cases.items():
        data["relation_references"] = {
            relation_id: relation_references[f"{case_id}:{relation_id}"]
            for relation_id in data["relation_packets"]
        }
    tasks.sort(key=lambda item: item["key"])
    return raw, preregistration, review, artifacts, reference, [cases, tasks]


def _task_identity(task: dict) -> dict:
    packet = task["packet"]
    return {
        "key": task["key"],
        "claim_type": task["claim_type"],
        "claim_identity_sha256": packet["claim_identity_sha256"],
        "claim_serialization_sha256": packet["claim_serialization_sha256"],
        "evidence_identity_sha256": packet["evidence_identity_sha256"],
        "request_input_sha256": _sha256(packet),
    }


def _hard_candidate_universe(cases: dict) -> list[dict]:
    rows = []
    for case_id, data in cases.items():
        for relation in data["deterministic_relations"]["relations"]:
            if (
                relation["state"] == "complete_verified"
                and relation["proposal"]["modality"] == "required"
            ):
                packet = hard_challenge_input(data["staged"], relation)
                rows.append(
                    {
                        "case_id": case_id,
                        "relation_id": relation["relation_id"],
                        "claim_identity_sha256": packet[
                            "claim_identity_sha256"
                        ],
                        "claim_serialization_sha256": packet[
                            "claim_serialization_sha256"
                        ],
                        "evidence_identity_sha256": packet[
                            "evidence_identity_sha256"
                        ],
                    }
                )
    rows.sort(key=lambda row: (row["case_id"], row["relation_id"]))
    return rows


def build_manifest(
    raw_path: Path = DEFAULT_RAW,
    preregistration_path: Path = DEFAULT_PREREGISTRATION,
    review_path: Path = DEFAULT_REVIEW,
    reference_path: Path = DEFAULT_REFERENCE,
) -> dict:
    _, preregistration, review, artifacts, reference, population = build_population(
        raw_path, preregistration_path, review_path, reference_path
    )
    cases, tasks = population
    atom_tasks = [task for task in tasks if task["claim_type"] == "atom"]
    relation_tasks = [task for task in tasks if task["claim_type"] == "relation"]
    hard_universe = _hard_candidate_universe(cases)
    hard_universe_ids = {
        f"{row['case_id']}:{row['relation_id']}" for row in hard_universe
    }
    reference_supported_hard = _reference_supported_hard(cases)
    if not reference_supported_hard <= hard_universe_ids:
        raise RuntimeError("supported hard reference escapes candidate universe")
    reference_rows = _reference_rows(reference)
    source_branches = sum(
        len(relation["source_branches"])
        for data in cases.values()
        for relation in data["deterministic_relations"]["relations"]
    )
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "evaluator_version": EVALUATOR_VERSION,
        "mode": "opened_16_case_development_regression_set",
        "fresh_evidence_claimed": False,
        "identities": {
            "primary_prompt_sha256": prompt_sha256("primary"),
            "challenge_prompt_sha256": prompt_sha256("hard_challenge"),
            "decision_schema_sha256": schema_sha256(),
            "serializer_identities": serializer_identities(),
            "primary_model_configuration_sha256": (
                model_configuration_sha256("primary")
            ),
            "challenge_model_configuration_sha256": (
                model_configuration_sha256("hard_challenge")
            ),
            "reviewed_reference_sha256": file_sha256(reference_path),
            "hard_selection_version": HARD_SELECTION_VERSION,
        },
        "provider_configurations": {
            "primary": model_configuration_identity("primary"),
            "hard_challenge": model_configuration_identity("hard_challenge"),
        },
        "artifacts": {
            "raw_artifact_sha256": artifacts["raw_artifact"]["sha256"],
            "preregistration_fixture_sha256": artifacts[
                "preregistration_fixture"
            ]["sha256"],
            "atom_review_fixture_sha256": file_sha256(review_path),
            "v1_reviewed_reference_sha256": file_sha256(reference_path),
        },
        "population": {
            "case_count": len(cases),
            "primary_atom_claim_count": len(atom_tasks),
            "primary_relation_claim_count": len(relation_tasks),
            "primary_provider_call_count": len(tasks),
            "source_branch_count": source_branches,
            "hard_candidate_universe_count": len(hard_universe),
            "selected_canonical_ids": preregistration[
                "selected_canonical_ids"
            ],
            "primary_task_identity_set_sha256": _sha256(
                [_task_identity(task) for task in tasks]
            ),
            "hard_candidate_universe_sha256": _sha256(hard_universe),
        },
        "review_reference": {
            "atom_counts": review["reviewed_atom_counts"],
            "relation_counts": reference["relation_reference"]["counts"],
            "relation_class_counts": dict(
                Counter(row["class"] for row in reference_rows.values())
            ),
            "hard_candidate_reference_counts": {
                "reviewed_supported": len(reference_supported_hard),
                "reviewed_unsupported_or_incomplete": len(
                    hard_universe_ids - reference_supported_hard
                ),
                "total": len(hard_universe_ids),
            },
            "direct_relation_reference_sha256": _sha256(reference_rows),
            "labels_locked_before_provider_calls": True,
        },
        "frozen_acceptance_gates": copy.deepcopy(FROZEN_GATES),
        "controls": {
            "primary_one_exact_claim_per_call": True,
            "challenge_only_after_primary_entails": True,
            "challenge_only_complete_required_candidates": True,
            "challenge_receives_primary_result_or_rationale": False,
            "provider_tools": [],
            "store": STORE,
            "reasoning_effort": REASONING_EFFORT,
            "candidate_profile_or_user_data": False,
            "database_access": False,
            "runtime_integration": False,
            "new_extraction_calls": 0,
            "new_grouping_calls": 0,
        },
    }
    manifest["manifest_sha256"] = _sha256(manifest)
    return manifest


def _require_manifest(path: Path, expected: dict) -> dict:
    if not path.exists():
        raise RuntimeError("v1 manifest is absent; freeze after local tests")
    actual = _read_json(path)
    if actual != expected:
        raise RuntimeError("frozen v1 manifest does not match current inputs")
    return actual


def _primary_decision(records: dict, case_id: str, claim_type: str, local_id: str):
    return records[f"primary:{claim_type}:{case_id}:{local_id}"]["payload"][
        "decision"
    ]


def select_hard_challenges(cases: dict, primary_records: dict) -> list[dict]:
    """Select only complete required groups already entailed by primary."""

    tasks = []
    for case_id, data in cases.items():
        for relation in data["deterministic_relations"]["relations"]:
            if (
                relation["state"] != "complete_verified"
                or relation["proposal"]["modality"] != "required"
            ):
                continue
            relation_id = relation["relation_id"]
            if (
                _primary_decision(
                    primary_records, case_id, "relation", relation_id
                )
                != "entails"
            ):
                continue
            atom_ids = _relation_atom_ids(relation)
            if any(
                _primary_decision(primary_records, case_id, "atom", atom_id)
                != "entails"
                for atom_id in atom_ids
            ):
                continue
            packet = hard_challenge_input(data["staged"], relation)
            validate_exact_input(packet)
            serialized = _canonical_json(packet)
            if "primary_result" in serialized or "primary_decision" in serialized:
                raise RuntimeError("challenge packet leaked primary output")
            tasks.append(
                {
                    "key": f"challenge:hard_constraint:{case_id}:{relation_id}",
                    "case_id": case_id,
                    "claim_local_id": relation_id,
                    "claim_type": "hard_constraint",
                    "packet": packet,
                }
            )
    tasks.sort(key=lambda task: task["key"])
    return tasks


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def _usage(records: dict) -> dict:
    totals = Counter()
    latencies = []
    models = Counter()
    statuses = Counter()
    cost = 0.0
    cost_known = True
    for record in records.values():
        diagnostics = record["diagnostics"]
        for key in (
            "input_tokens",
            "cached_input_tokens",
            "cache_write_input_tokens",
            "visible_output_tokens",
            "reasoning_tokens",
            "output_tokens",
            "total_tokens",
        ):
            totals[key] += diagnostics[key]
        latencies.append(diagnostics["latency_seconds"])
        models[diagnostics["response_model"]] += 1
        statuses[diagnostics["response_status"] or "unknown"] += 1
        if diagnostics["estimated_cost_usd"] is None:
            cost_known = False
        else:
            cost += diagnostics["estimated_cost_usd"]
    return {
        "provider_calls": len(records),
        **dict(totals),
        "sum_call_latency_seconds": sum(latencies),
        "mean_call_latency_seconds": statistics.fmean(latencies)
        if latencies
        else 0.0,
        "p50_call_latency_seconds": statistics.median(latencies)
        if latencies
        else 0.0,
        "p95_call_latency_seconds": _percentile(latencies, 0.95),
        "max_call_latency_seconds": max(latencies) if latencies else 0.0,
        "estimated_cost_usd": round(cost, 8) if cost_known else None,
        "returned_model_aliases": dict(models),
        "response_statuses": dict(statuses),
    }


def _reference_supported_hard(cases: dict) -> set[str]:
    supported = set()
    for case_id, data in cases.items():
        for relation in data["deterministic_relations"]["relations"]:
            if (
                relation["state"] != "complete_verified"
                or relation["proposal"]["modality"] != "required"
            ):
                continue
            relation_id = relation["relation_id"]
            if data["relation_references"][relation_id]["decision"] != "entails":
                continue
            if all(
                data["atom_labels"][atom_id] == "entails"
                for atom_id in _relation_atom_ids(relation)
            ):
                supported.add(f"{case_id}:{relation_id}")
    return supported


def _finalize_authorized(
    data: dict,
    case_id: str,
    primary_records: dict,
    authorized_relation_ids: set[str],
) -> dict:
    staging = copy.deepcopy(data["deterministic_staging"])
    for item in staging["provisional_atoms"]:
        decision = _primary_decision(
            primary_records, case_id, "atom", item["proposal_id"]
        )
        item["semantic_verification"] = {"decision": decision}
        item["assurance"] = "retained_grounded"
    ledger = copy.deepcopy(data["deterministic_relations"])
    for relation in ledger["relations"]:
        relation_id = relation["relation_id"]
        if relation_id in authorized_relation_ids:
            if relation["state"] != "complete_verified":
                raise RuntimeError("hard challenge authorized incomplete staging state")
            for atom_id in _relation_atom_ids(relation):
                item = next(
                    atom
                    for atom in staging["provisional_atoms"]
                    if atom["proposal_id"] == atom_id
                )
                if item["semantic_verification"]["decision"] != "entails":
                    raise RuntimeError("hard challenge bypassed primary atom entailment")
                item["assurance"] = "hard_projection_authorized"
        elif relation["state"] == "complete_verified":
            relation["state"] = "grounded_incomplete"
            relation["reason_codes"].append("v1_hard_assurance_not_authorized")
    return finalize_verified_relations(staging, ledger)


def evaluate_records(
    cases: dict,
    tasks: list[dict],
    primary_records: dict,
    challenge_tasks: list[dict],
    challenge_records: dict,
) -> dict:
    task_by_key = {task["key"]: task for task in tasks + challenge_tasks}
    for key, record in {**primary_records, **challenge_records}.items():
        validate_exact_result_record(task_by_key[key]["packet"], record)

    atom_rows = []
    relation_rows = []
    for case_id, data in cases.items():
        for atom_id in sorted(data["atom_packets"]):
            atom_rows.append(
                {
                    "identity": f"{case_id}:{atom_id}",
                    "reference": data["atom_labels"][atom_id],
                    "observed": _primary_decision(
                        primary_records, case_id, "atom", atom_id
                    ),
                }
            )
        for relation_id in sorted(data["relation_packets"]):
            reference = data["relation_references"][relation_id]
            relation_rows.append(
                {
                    "identity": f"{case_id}:{relation_id}",
                    "reference": reference["decision"],
                    "reference_class": reference["class"],
                    "observed": _primary_decision(
                        primary_records, case_id, "relation", relation_id
                    ),
                }
            )

    atom_true_entails = sum(
        row["reference"] == row["observed"] == "entails" for row in atom_rows
    )
    atom_predicted_entails = sum(row["observed"] == "entails" for row in atom_rows)
    atom_supported = sum(row["reference"] == "entails" for row in atom_rows)
    atom_precision = (
        atom_true_entails / atom_predicted_entails
        if atom_predicted_entails
        else 1.0
    )
    atom_recall = atom_true_entails / atom_supported if atom_supported else 1.0
    contradicted = [row for row in atom_rows if row["reference"] == "contradicts"]
    contradiction_accuracy = (
        sum(row["observed"] == "contradicts" for row in contradicted)
        / len(contradicted)
        if contradicted
        else 1.0
    )
    contradicted_entails = [
        row["identity"]
        for row in contradicted
        if row["observed"] == "entails"
    ]
    unsupported_atom_entails = [
        row["identity"]
        for row in atom_rows
        if row["reference"] != "entails" and row["observed"] == "entails"
    ]
    supported_atom_abstentions = [
        row["identity"]
        for row in atom_rows
        if row["reference"] == "entails"
        and row["observed"] == "not_established"
    ]

    relation_correct = sum(
        row["reference"] == row["observed"] for row in relation_rows
    )
    relation_accuracy = relation_correct / len(relation_rows)
    incomplete_classes = {
        "incomplete_source_relation",
        "incomplete_source_relation_and_unsupported_member",
    }
    incomplete_overaccepted = [
        row["identity"]
        for row in relation_rows
        if row["reference_class"] in incomplete_classes
        and row["observed"] == "entails"
    ]
    narrowed_overaccepted = [
        row["identity"]
        for row in relation_rows
        if row["reference_class"] == "narrowed_conjunction"
        and row["observed"] == "entails"
    ]
    extra_member_overaccepted = [
        row["identity"]
        for row in relation_rows
        if row["reference_class"] == "extra_speculative_member"
        and row["observed"] == "entails"
    ]
    relation_abstentions = [
        row["identity"]
        for row in relation_rows
        if row["reference"] == "entails"
        and row["observed"] == "not_established"
    ]

    challenge_by_identity = {
        f"{task['case_id']}:{task['claim_local_id']}": challenge_records[
            task["key"]
        ]["payload"]["decision"]
        for task in challenge_tasks
    }
    authorized = {
        identity
        for identity, decision in challenge_by_identity.items()
        if decision == "entails"
    }
    reference_supported = _reference_supported_hard(cases)
    supported_authorized = sorted(authorized & reference_supported)
    supported_withheld = sorted(reference_supported - authorized)
    unsupported_authorized = sorted(authorized - reference_supported)
    reference_by_identity = {
        row["identity"]: row for row in relation_rows
    }
    incomplete_authorized = sorted(
        identity
        for identity in authorized
        if reference_by_identity[identity]["reference_class"]
        in incomplete_classes | {"narrowed_conjunction"}
    )
    challenge_agreement = sorted(
        identity
        for identity, decision in challenge_by_identity.items()
        if decision == "entails"
    )
    hard_precision = (
        len(supported_authorized) / len(authorized) if authorized else 1.0
    )
    hard_recall = (
        len(supported_authorized) / len(reference_supported)
        if reference_supported
        else 1.0
    )

    finalizations = {}
    projected = []
    source_substitutions = []
    logic_weakening = []
    for case_id, data in cases.items():
        case_authorized = {
            identity.split(":", 1)[1]
            for identity in authorized
            if identity.startswith(case_id + ":")
        }
        finalization = _finalize_authorized(
            data, case_id, primary_records, case_authorized
        )
        finalizations[case_id] = finalization
        if finalization["logic_weakening_detected"]:
            logic_weakening.append(case_id)
        original_atoms = {
            item["proposal_id"]: item["atom"]
            for item in data["staged"]["provisional_atoms"]
        }
        for atom in finalization["final_contract"]["atoms"]:
            # The unchanged compatibility projector may add its deterministic
            # normalized_value.  Source substitution concerns the immutable
            # semantic/evidence fields, not that server-owned projection field.
            projected_core = {
                key: value for key, value in atom.items() if key != "normalized_value"
            }
            if projected_core != original_atoms[atom["id"]]:
                source_substitutions.append(f"{case_id}:{atom['id']}")
        relation_by_group = dict(
            zip(
                finalization["final_relation_ids"],
                finalization["projection"]["group_outcomes"],
            )
        )
        for relation_id, outcome in relation_by_group.items():
            if outcome["state"] == "projected":
                identity = f"{case_id}:{relation_id}"
                projected.append(
                    {
                        "identity": identity,
                        "supported": identity in reference_supported,
                    }
                )
    correct_projected = sum(row["supported"] for row in projected)
    projection_precision = (
        correct_projected / len(projected) if projected else 1.0
    )
    unsafe_hard_gates = sorted(set(unsupported_authorized))

    source_branch_count = sum(
        ledger["source_branch_accounting"]["source_branch_count"]
        for ledger in (
            data["deterministic_relations"] for data in cases.values()
        )
    )
    accounted_branch_count = sum(
        ledger["source_branch_accounting"]["accounted_source_branch_count"]
        for ledger in (
            data["deterministic_relations"] for data in cases.values()
        )
    )

    atom_checks = {
        "entailment_precision": atom_precision
        >= FROZEN_GATES["primary_atom_verifier"][
            "minimum_entailment_precision"
        ],
        "supported_recall": atom_recall
        >= FROZEN_GATES["primary_atom_verifier"][
            "minimum_supported_recall"
        ],
        "reviewed_contradicted_marked_entails": len(contradicted_entails)
        <= FROZEN_GATES["primary_atom_verifier"][
            "maximum_reviewed_contradicted_marked_entails"
        ],
    }
    relation_checks = {
        "exact_relation_decision_accuracy": relation_accuracy
        >= FROZEN_GATES["primary_relation_verifier"][
            "minimum_exact_relation_decision_accuracy"
        ],
        "narrowed_or_and_marked_entails": len(narrowed_overaccepted)
        <= FROZEN_GATES["primary_relation_verifier"][
            "maximum_narrowed_or_and_marked_entails"
        ],
        "incomplete_relation_marked_entails": len(incomplete_overaccepted)
        <= FROZEN_GATES["primary_relation_verifier"][
            "maximum_incomplete_relation_marked_entails"
        ],
    }
    hard_checks = {
        "reviewed_unsupported_hard_authorized": len(unsupported_authorized)
        <= FROZEN_GATES["final_hard_assurance"][
            "maximum_reviewed_unsupported_hard_authorized"
        ],
        "incomplete_relation_authorized": len(incomplete_authorized)
        <= FROZEN_GATES["final_hard_assurance"][
            "maximum_incomplete_relation_authorized"
        ],
        "source_value_substitutions": len(source_substitutions)
        <= FROZEN_GATES["final_hard_assurance"][
            "maximum_source_value_substitutions"
        ],
        "or_and_weakening": len(logic_weakening)
        <= FROZEN_GATES["final_hard_assurance"][
            "maximum_or_and_weakening"
        ],
        "conditional_hard_projection_precision": projection_precision
        == FROZEN_GATES["final_hard_assurance"][
            "required_conditional_hard_projection_precision"
        ],
        "unsupported_deterministic_hard_gates": len(unsafe_hard_gates)
        <= FROZEN_GATES["final_hard_assurance"][
            "maximum_unsupported_deterministic_hard_gates"
        ],
    }
    passed = all(atom_checks.values()) and all(relation_checks.values()) and all(
        hard_checks.values()
    )
    if passed:
        verdict = "VERIFIER V1 HARD-ASSURANCE GATES PASSED"
    elif source_substitutions or logic_weakening:
        verdict = "STAGING CONTRACT GAP FOUND"
    else:
        verdict = "VERIFIER V1 WORK NEEDED"

    errors = defaultdict(list)
    for row in atom_rows:
        if row["reference"] == row["observed"]:
            continue
        if row["reference"] == "entails" and row["observed"] == "not_established":
            family = "supported_atom_conservative_abstention"
        elif row["reference"] != "entails" and row["observed"] == "entails":
            family = "unsupported_atom_overacceptance"
        else:
            family = "atom_wrong_decision_class"
        errors[family].append(row["identity"])
    for row in relation_rows:
        if row["reference"] == row["observed"]:
            continue
        if row["reference"] == "entails" and row["observed"] == "not_established":
            family = "supported_relation_conservative_abstention"
        elif row["reference"] != "entails" and row["observed"] == "entails":
            family = "unsupported_relation_overacceptance"
        else:
            family = "relation_wrong_decision_class"
        errors[family].append(row["identity"])

    return {
        "primary_atom_verifier": {
            "reviewed_claim_count": len(atom_rows),
            "reviewed_supported": atom_supported,
            "entailment_precision": atom_precision,
            "supported_claim_recall": atom_recall,
            "supported_marked_entails": atom_true_entails,
            "supported_conservative_abstentions": supported_atom_abstentions,
            "unsupported_incorrectly_entailed": unsupported_atom_entails,
            "reviewed_contradicted_count": len(contradicted),
            "contradiction_accuracy": contradiction_accuracy,
            "reviewed_contradicted_marked_entails": contradicted_entails,
            "confusion": dict(
                Counter(
                    f"{row['reference']}->{row['observed']}" for row in atom_rows
                )
            ),
            "exact_claim_decisions": atom_rows,
            "qualifier_reconstruction_scored": False,
        },
        "primary_relation_verifier": {
            "reviewed_claim_count": len(relation_rows),
            "exact_relation_decision_accuracy": relation_accuracy,
            "incomplete_relation_overacceptance": incomplete_overaccepted,
            "narrowed_or_and_overacceptance": narrowed_overaccepted,
            "extra_member_overacceptance": extra_member_overaccepted,
            "supported_conservative_abstentions": relation_abstentions,
            "source_branch_count": source_branch_count,
            "accounted_source_branch_count": accounted_branch_count,
            "source_branch_accounting": accounted_branch_count
            / source_branch_count
            if source_branch_count
            else 1.0,
            "or_and_weakening": logic_weakening,
            "confusion": dict(
                Counter(
                    f"{row['reference']}->{row['observed']}"
                    for row in relation_rows
                )
            ),
            "exact_claim_decisions": relation_rows,
        },
        "hard_assurance": {
            "potential_hard_constraint_count": len(challenge_tasks),
            "challenged_count": len(challenge_tasks),
            "primary_challenge_entailment_agreement_count": len(
                challenge_agreement
            ),
            "primary_challenge_entailment_agreement_rate": len(
                challenge_agreement
            )
            / len(challenge_tasks)
            if challenge_tasks
            else 1.0,
            "challenge_decisions": dict(sorted(challenge_by_identity.items())),
            "reviewed_supported_hard_constraints": len(reference_supported),
            "supported_hard_constraints_authorized": supported_authorized,
            "supported_constraints_conservatively_withheld": supported_withheld,
            "reviewed_unsupported_hard_constraints_authorized": (
                unsupported_authorized
            ),
            "incomplete_relations_authorized": incomplete_authorized,
            "hard_assurance_precision": hard_precision,
            "hard_assurance_recall": hard_recall,
            "preferred_or_descriptive_challenges": [
                task["key"]
                for task in challenge_tasks
                if task["packet"]["claim"]["required_constraint"]["modality"]
                != "required"
            ],
        },
        "soft_retention": {
            "grounded_provisional_claim_count": len(atom_rows),
            "claims_deleted_by_primary_abstention": 0,
            "primary_abstentions_retained_for_ranking_or_review": sum(
                row["observed"] == "not_established" for row in atom_rows
            ),
        },
        "finalization_and_projection": {
            "authorized_hard_relations": sorted(authorized),
            "projected_hard_groups": projected,
            "conditional_hard_projection_precision": projection_precision,
            "source_value_substitutions": source_substitutions,
            "or_and_weakening": logic_weakening,
            "unsupported_deterministic_hard_gates": unsafe_hard_gates,
            "final_relation_count": sum(
                len(item["final_relation_ids"])
                for item in finalizations.values()
            ),
            "final_atom_count": sum(
                len(item["final_contract"]["atoms"])
                for item in finalizations.values()
            ),
        },
        "error_attribution": {
            "verifier_error_families": dict(errors),
            "extraction_omissions_scored_as_atom_errors": False,
            "qualifier_reconstruction_scored": False,
            "stages": [
                "extraction_quality",
                "deterministic_authentication",
                "normalization",
                "primary_exact_verifier",
                "primary_exact_relation_verifier",
                "independent_hard_challenge",
                "finalization_assurance",
                "projection",
            ],
        },
        "acceptance": {
            "frozen_gates": copy.deepcopy(FROZEN_GATES),
            "checks": {
                "primary_atom_verifier": atom_checks,
                "primary_relation_verifier": relation_checks,
                "final_hard_assurance": hard_checks,
            },
            "passed": passed,
        },
        "verdict": verdict,
    }


def _accounting_summary(accounting: dict, wall_seconds: float) -> dict:
    keys = (
        "accounting_version",
        "run_id",
        "event_count",
        "registered_tasks",
        "dispatched_requests",
        "completed_requests",
        "failed_requests",
        "status_counts",
        "journal_sha256",
    )
    return {
        **{key: copy.deepcopy(accounting[key]) for key in keys},
        "provider_wall_latency_seconds": wall_seconds,
    }


def build_report(
    *,
    manifest: dict,
    cases: dict,
    tasks: list[dict],
    primary_records: dict,
    primary_accounting: dict,
    primary_wall_seconds: float,
    challenge_tasks: list[dict],
    challenge_records: dict,
    challenge_accounting: dict,
    challenge_wall_seconds: float,
) -> dict:
    metrics = evaluate_records(
        cases,
        tasks,
        primary_records,
        challenge_tasks,
        challenge_records,
    )
    primary_usage = _usage(primary_records)
    primary_usage["provider_wall_latency_seconds"] = primary_wall_seconds
    challenge_usage = _usage(challenge_records)
    challenge_usage["provider_wall_latency_seconds"] = challenge_wall_seconds
    report = {
        "report_version": REPORT_VERSION,
        "evaluator_version": EVALUATOR_VERSION,
        "mode": "single_live_exact_claim_development_regression",
        "fresh_evidence_claimed": False,
        "manifest_sha256": manifest["manifest_sha256"],
        "identities": copy.deepcopy(manifest["identities"]),
        "provider_configurations": copy.deepcopy(
            manifest["provider_configurations"]
        ),
        "population": copy.deepcopy(manifest["population"]),
        "review_reference": copy.deepcopy(manifest["review_reference"]),
        "metrics": metrics,
        "usage": {
            "primary_terra": primary_usage,
            "hard_challenge_sol": challenge_usage,
            "combined_estimated_cost_usd": round(
                (primary_usage["estimated_cost_usd"] or 0.0)
                + (challenge_usage["estimated_cost_usd"] or 0.0),
                8,
            ),
        },
        "durable_accounting": {
            "primary_terra": _accounting_summary(
                primary_accounting, primary_wall_seconds
            ),
            "hard_challenge_sol": _accounting_summary(
                challenge_accounting, challenge_wall_seconds
            ),
        },
        "provider_results": {
            "primary": {
                key: primary_records[key] for key in sorted(primary_records)
            },
            "hard_challenge": {
                key: challenge_records[key] for key in sorted(challenge_records)
            },
        },
        "preservation": {
            "database_reads": 0,
            "database_writes": 0,
            "new_extraction_calls": 0,
            "new_grouping_calls": 0,
            "runtime_integration": False,
            "persistence_integration": False,
            "matching_integration": False,
            "ui_integration": False,
        },
    }
    report["report_sha256"] = _sha256(report)
    return report


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--freeze-manifest", action="store_true")
    mode.add_argument("--evaluate", action="store_true")
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    parser.add_argument(
        "--preregistration", type=Path, default=DEFAULT_PREREGISTRATION
    )
    parser.add_argument("--review", type=Path, default=DEFAULT_REVIEW)
    parser.add_argument("--reference", type=Path, default=DEFAULT_REFERENCE)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--primary-accounting", type=Path, default=DEFAULT_PRIMARY_ACCOUNTING
    )
    parser.add_argument(
        "--challenge-accounting",
        type=Path,
        default=DEFAULT_CHALLENGE_ACCOUNTING,
    )
    parser.add_argument("--workers", type=int, default=8)
    return parser.parse_args(argv)


def _blocked_report(
    *,
    manifest: dict,
    phase: str,
    accounting: dict,
    failures: dict,
) -> dict:
    report = {
        "report_version": REPORT_VERSION,
        "mode": "execution_blocked_before_complete_semantic_scoring",
        "manifest_sha256": manifest["manifest_sha256"],
        "blocked_phase": phase,
        "durable_accounting": _accounting_summary(accounting, 0.0),
        "failures": failures,
        "semantic_results_evaluable": False,
        "verdict": "BLOCKED",
    }
    report["report_sha256"] = _sha256(report)
    return report


def main(argv=None) -> int:
    args = parse_args(argv)
    manifest = build_manifest(
        args.raw,
        args.preregistration,
        args.review,
        args.reference,
    )
    if args.freeze_manifest:
        if args.manifest.exists():
            raise RuntimeError("refusing to overwrite frozen v1 manifest")
        _write_json(args.manifest, manifest)
        print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    for path in (
        args.output,
        args.primary_accounting,
        args.challenge_accounting,
    ):
        if path.exists():
            raise RuntimeError(f"refusing to reuse live v1 artifact: {path}")
    if args.workers < 1 or args.workers > 16:
        raise RuntimeError("--workers must be between 1 and 16")
    frozen = _require_manifest(args.manifest, manifest)
    api_key = str(os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required")
    _, _, _, _, _, population = build_population(
        args.raw,
        args.preregistration,
        args.review,
        args.reference,
    )
    cases, tasks = population
    primary_records, primary_failures, primary_wall, primary_accounting = (
        _run_provider(
            tasks,
            api_key,
            args.workers,
            accounting_path=args.primary_accounting,
            manifest_sha256=frozen["manifest_sha256"],
            client_factory=lambda key, _model: OpenAIExactClaimVerifierClient(
                key, role="primary"
            ),
        )
    )
    if primary_failures:
        report = _blocked_report(
            manifest=frozen,
            phase="primary_terra",
            accounting=primary_accounting,
            failures=primary_failures,
        )
        _write_json(args.output, report)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 2

    challenge_tasks = select_hard_challenges(cases, primary_records)
    challenge_records, challenge_failures, challenge_wall, challenge_accounting = (
        _run_provider(
            challenge_tasks,
            api_key,
            args.workers,
            accounting_path=args.challenge_accounting,
            manifest_sha256=frozen["manifest_sha256"],
            client_factory=lambda key, _model: OpenAIExactClaimVerifierClient(
                key, role="hard_challenge"
            ),
        )
    )
    if challenge_failures:
        report = _blocked_report(
            manifest=frozen,
            phase="hard_challenge_sol",
            accounting=challenge_accounting,
            failures=challenge_failures,
        )
        _write_json(args.output, report)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    report = build_report(
        manifest=frozen,
        cases=cases,
        tasks=tasks,
        primary_records=primary_records,
        primary_accounting=primary_accounting,
        primary_wall_seconds=primary_wall,
        challenge_tasks=challenge_tasks,
        challenge_records=challenge_records,
        challenge_accounting=challenge_accounting,
        challenge_wall_seconds=challenge_wall,
    )
    _write_json(args.output, report)
    print(
        json.dumps(
            {
                "report_path": str(args.output),
                "report_sha256": report["report_sha256"],
                "verdict": report["metrics"]["verdict"],
                "acceptance": report["metrics"]["acceptance"],
                "usage": report["usage"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

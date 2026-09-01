#!/usr/bin/env python3
"""Freeze and run OE Semantic Verifier v0 on the opened 16-case set."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import statistics
import sys
import threading
import time
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
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
from wahojobs.opportunity_semantic_staging import (  # noqa: E402
    SEMANTIC_DECISIONS,
    apply_semantic_verification,
    construct_relations,
    finalize_verified_relations,
    role_activity_composition_supported,
    stage_provisional_atoms,
    verification_from_reviewed_labels,
)
from wahojobs.opportunity_semantic_verifier import (  # noqa: E402
    DEFAULT_MODEL,
    REASONING_EFFORT,
    STORE,
    OpenAISemanticVerifierClient,
    SemanticVerifierError,
    apply_relation_verification,
    atom_verifier_input,
    model_configuration_identity,
    model_configuration_sha256,
    prompt_sha256,
    relation_verifier_input,
    result_record,
    schema_identities,
    schema_sha256,
    staging_verification_from_results,
    validate_result_record,
    validate_verifier_input,
)


MANIFEST_VERSION = "oe_semantic_verifier_v0_schema_repaired_manifest_v2"
REPORT_VERSION = "oe_semantic_verifier_v0_schema_repaired_evaluation_v2"
EVALUATOR_VERSION = "oe_semantic_verifier_v0_evaluator_v2"
ACCOUNTING_VERSION = "oe_semantic_verifier_v0_accounting_v1"
FROZEN_TASK_IDENTITY_SET_SHA256 = (
    "07502a40512534a98e22aa418e7fcddfbd24474c417295bf4937e9d82e51aa66"
)
DEFAULT_MANIFEST = (
    ROOT / "exports" / "opportunity_semantic_verifier_v0_schema_repaired_manifest.json"
)
DEFAULT_OUTPUT = (
    ROOT / "exports" / "opportunity_semantic_verifier_v0_schema_repaired_evaluation.json"
)
DEFAULT_ACCOUNTING = (
    ROOT / "exports" / "opportunity_semantic_verifier_v0_schema_repaired_accounting.jsonl"
)

FROZEN_GATES = {
    "atom_verifier": {
        "minimum_entailment_precision": 0.95,
        "minimum_supported_recall": 0.95,
        "maximum_reviewed_unsupported_hard_assurance": 0,
        "minimum_qualifier_semantic_accuracy": 0.95,
    },
    "relation_verifier": {
        "minimum_review_bound_relation_decision_accuracy": 0.95,
        "required_source_branch_accounting": 1.0,
        "maximum_or_and_weakening": 0,
        "maximum_incomplete_relation_promoted_complete": 0,
    },
    "final_safety": {
        "maximum_source_value_substitutions": 0,
        "maximum_projection_from_incomplete_relations": 0,
        "required_conditional_projection_precision": 1.0,
        "maximum_unsupported_deterministic_hard_gates": 0,
    },
}


def _canonical_json(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
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


def _task_identity(task: dict) -> dict:
    packet = task["packet"]
    return {
        "key": task["key"],
        "case_id": task["case_id"],
        "claim_local_id": task["claim_local_id"],
        "claim_type": task["claim_type"],
        "claim_identity_sha256": packet["claim_identity_sha256"],
        "evidence_identity_sha256": packet["evidence_identity_sha256"],
        "request_input_sha256": _sha256(packet),
    }


class DurableAccountingJournal:
    """Append-only, fsync-backed provider-call accounting for one evaluation."""

    def __init__(self, path: Path, *, run_id: str):
        self.path = Path(path)
        self.run_id = run_id
        self._lock = threading.Lock()
        self._sequence = 0

    def create(self, *, manifest_sha256: str, tasks: list[dict]) -> None:
        if self.path.exists():
            raise RuntimeError("refusing to reuse an existing accounting journal")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.append(
            "run_opened",
            manifest_sha256=manifest_sha256,
            task_count=len(tasks),
            accounting_version=ACCOUNTING_VERSION,
        )
        for task in tasks:
            self.append("task_registered", task=_task_identity(task))

    def append(self, event: str, **fields) -> None:
        with self._lock:
            self._sequence += 1
            record = {
                "accounting_version": ACCOUNTING_VERSION,
                "run_id": self.run_id,
                "sequence": self._sequence,
                "recorded_unix_ns": time.time_ns(),
                "event": event,
                **copy.deepcopy(fields),
            }
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(_canonical_json(record) + "\n")
                handle.flush()
                os.fsync(handle.fileno())


def reconstruct_accounting(path: Path) -> dict:
    """Reconstruct exact task/call state from a durable evaluation journal."""

    events = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"accounting journal line {line_number} is invalid"
                ) from exc
            events.append(event)
    if not events:
        raise RuntimeError("accounting journal is empty")
    expected_sequences = list(range(1, len(events) + 1))
    if [event.get("sequence") for event in events] != expected_sequences:
        raise RuntimeError("accounting journal sequence is not contiguous")
    run_ids = {event.get("run_id") for event in events}
    versions = {event.get("accounting_version") for event in events}
    if len(run_ids) != 1 or versions != {ACCOUNTING_VERSION}:
        raise RuntimeError("accounting journal identity mismatch")

    tasks = {}
    records = {}
    failures = {}
    for event in events:
        kind = event.get("event")
        if kind == "task_registered":
            identity = event["task"]
            key = identity["key"]
            if key in tasks:
                raise RuntimeError("duplicate accounting task registration")
            tasks[key] = {
                "identity": identity,
                "status": "registered",
                "dispatch_sequence": None,
                "terminal_sequence": None,
            }
        elif kind == "dispatch_started":
            key = event["task_key"]
            if key not in tasks or tasks[key]["status"] != "registered":
                raise RuntimeError("invalid or duplicate dispatch accounting")
            tasks[key]["status"] = "dispatched"
            tasks[key]["dispatch_sequence"] = event["sequence"]
        elif kind in {"request_completed", "request_failed"}:
            key = event["task_key"]
            if key not in tasks or tasks[key]["status"] != "dispatched":
                raise RuntimeError("invalid or duplicate terminal accounting")
            tasks[key]["terminal_sequence"] = event["sequence"]
            if kind == "request_completed":
                tasks[key]["status"] = "completed"
                records[key] = event["result_record"]
            else:
                tasks[key]["status"] = "failed"
                failures[key] = event["failure"]

    status_counts = Counter(value["status"] for value in tasks.values())
    return {
        "accounting_version": ACCOUNTING_VERSION,
        "run_id": next(iter(run_ids)),
        "event_count": len(events),
        "registered_tasks": len(tasks),
        "dispatched_requests": sum(
            value["dispatch_sequence"] is not None for value in tasks.values()
        ),
        "completed_requests": len(records),
        "failed_requests": len(failures),
        "status_counts": dict(sorted(status_counts.items())),
        "tasks": {key: tasks[key] for key in sorted(tasks)},
        "records": {key: records[key] for key in sorted(records)},
        "failures": {key: failures[key] for key in sorted(failures)},
        "journal_sha256": file_sha256(Path(path)),
    }


def _labels_by_atom(reviewed_labels: dict) -> dict[str, str]:
    result = {}
    for decision in sorted(SEMANTIC_DECISIONS):
        for atom_id in reviewed_labels[decision]:
            if atom_id in result:
                raise RuntimeError(f"duplicate reviewed atom label: {atom_id}")
            result[atom_id] = decision
    return result


def _atom_qualifier_reference(overall: str) -> dict[str, str]:
    # The frozen eight reviewed errors are kind/payload meaning errors.  Their
    # affirmed polarity and unspecified temporal meaning are not the error.
    return {
        "kind_payload": overall,
        "polarity": "entails",
        "temporal": "entails",
    }


def _combine_decisions(values) -> str:
    values = list(values)
    if "contradicts" in values:
        return "contradicts"
    if values and all(value == "entails" for value in values):
        return "entails"
    return "not_established"


def _relation_atom_ids(relation: dict) -> list[str]:
    return [
        atom_id
        for alternative in relation["proposal"]["any_of"]
        for atom_id in alternative["all_of"]
    ]


def _relation_reference_payload(
    relation: dict,
    atom_labels: dict[str, str],
) -> dict:
    member_decision = _combine_decisions(
        atom_labels[atom_id] for atom_id in _relation_atom_ids(relation)
    )
    authoritative = relation.get("authoritative_modality")
    if authoritative is None:
        modality = "not_established"
    elif authoritative == relation["proposal"]["modality"]:
        modality = "entails"
    else:
        modality = "contradicts"
    completeness = (
        "entails" if relation.get("source_logic_exact") else "not_established"
    )
    qualifiers = {
        "relation_logic": member_decision,
        "modality": modality,
        "completeness": completeness,
    }
    return {
        "decision": _combine_decisions(qualifiers.values()),
        "qualifier_decisions": qualifiers,
    }


def build_population(
    raw_path: Path = DEFAULT_RAW,
    preregistration_path: Path = DEFAULT_PREREGISTRATION,
    review_path: Path = DEFAULT_REVIEW,
) -> tuple[dict, dict, dict, dict, list[dict]]:
    raw, preregistration, review, artifacts = _assert_artifacts(
        raw_path, preregistration_path, review_path
    )
    cases = {}
    tasks = []
    for case_id, case in raw["cases"].items():
        staged = stage_provisional_atoms(
            case["raw_extraction"],
            case["source_packet"],
            case["accepted_evidence_bindings"],
        )
        atom_labels = _labels_by_atom(review["cases"][case_id])
        human_verification = verification_from_reviewed_labels(
            staged, review["cases"][case_id]
        )
        human_verified = apply_semantic_verification(
            copy.deepcopy(staged), human_verification
        )
        reference_relations = construct_relations(
            human_verified, case.get("raw_grouping")
        )
        atom_packets = {}
        for item in staged["provisional_atoms"]:
            if item["status"] != "provisional":
                continue
            packet = atom_verifier_input(item)
            validate_verifier_input(packet)
            key = f"atom:{case_id}:{item['proposal_id']}"
            atom_packets[item["proposal_id"]] = packet
            tasks.append(
                {
                    "key": key,
                    "case_id": case_id,
                    "claim_local_id": item["proposal_id"],
                    "claim_type": "atom",
                    "packet": packet,
                }
            )
        relation_packets = {}
        relation_references = {}
        for relation in reference_relations["relations"]:
            if relation["state"] == "unrepresentable_relation":
                continue
            packet = relation_verifier_input(human_verified, relation)
            validate_verifier_input(packet)
            relation_id = relation["relation_id"]
            key = f"relation:{case_id}:{relation_id}"
            relation_packets[relation_id] = packet
            relation_references[relation_id] = _relation_reference_payload(
                relation, atom_labels
            )
            tasks.append(
                {
                    "key": key,
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
            "atom_qualifier_references": {
                atom_id: _atom_qualifier_reference(decision)
                for atom_id, decision in atom_labels.items()
            },
            "human_verified": human_verified,
            "reference_relations": reference_relations,
            "relation_references": relation_references,
            "atom_packets": atom_packets,
            "relation_packets": relation_packets,
        }
    tasks.sort(key=lambda item: item["key"])
    return raw, preregistration, review, artifacts, [cases, tasks]


def build_manifest(
    raw_path: Path = DEFAULT_RAW,
    preregistration_path: Path = DEFAULT_PREREGISTRATION,
    review_path: Path = DEFAULT_REVIEW,
) -> dict:
    raw, preregistration, review, artifacts, population = build_population(
        raw_path, preregistration_path, review_path
    )
    cases, tasks = population
    atom_tasks = [item for item in tasks if item["claim_type"] == "atom"]
    relation_tasks = [item for item in tasks if item["claim_type"] == "relation"]
    relation_decisions = Counter()
    relation_states = Counter()
    source_branches = 0
    reference_rows = []
    atom_qualifier_rows = []
    for case_id, data in cases.items():
        for atom_id in sorted(data["atom_labels"]):
            atom_qualifier_rows.append(
                {
                    "case_id": case_id,
                    "atom_id": atom_id,
                    "overall": data["atom_labels"][atom_id],
                    "qualifiers": data["atom_qualifier_references"][atom_id],
                }
            )
        for relation in data["reference_relations"]["relations"]:
            relation_states[relation["state"]] += 1
            source_branches += len(relation["source_branches"])
            if relation["relation_id"] not in data["relation_references"]:
                continue
            reference = data["relation_references"][relation["relation_id"]]
            relation_decisions[reference["decision"]] += 1
            reference_rows.append(
                {
                    "case_id": case_id,
                    "relation_id": relation["relation_id"],
                    "reference_state": relation["state"],
                    **reference,
                }
            )
    task_identities = [
        {
            "key": item["key"],
            "claim_type": item["claim_type"],
            "claim_identity_sha256": item["packet"]["claim_identity_sha256"],
            "evidence_identity_sha256": item["packet"][
                "evidence_identity_sha256"
            ],
            "request_input_sha256": _sha256(item["packet"]),
        }
        for item in tasks
    ]
    task_identity_set_sha256 = _sha256(task_identities)
    if task_identity_set_sha256 != FROZEN_TASK_IDENTITY_SET_SHA256:
        raise RuntimeError(
            "frozen verifier task population identity changed during schema repair"
        )
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "evaluator_version": EVALUATOR_VERSION,
        "mode": "already_opened_16_case_development_regression_set",
        "fresh_evidence_claimed": False,
        "provider_configuration": model_configuration_identity(),
        "identities": {
            "prompt_sha256": prompt_sha256(),
            "schema_sha256": schema_sha256(),
            "provider_schema_identities": schema_identities(),
            "model_configuration_sha256": model_configuration_sha256(),
        },
        "artifacts": {
            "raw_artifact_sha256": artifacts["raw_artifact"]["sha256"],
            "preregistration_fixture_sha256": artifacts[
                "preregistration_fixture"
            ]["sha256"],
            "review_fixture_sha256": file_sha256(review_path),
            "raw_artifact_expected_sha256": RAW_ARTIFACT_SHA256,
            "preregistration_expected_sha256": PREREGISTRATION_SHA256,
        },
        "population": {
            "case_count": len(cases),
            "atom_claim_count": len(atom_tasks),
            "relation_claim_count": len(relation_tasks),
            "provider_call_count": len(tasks),
            "source_branch_count": source_branches,
            "selected_canonical_ids": preregistration["selected_canonical_ids"],
            "task_identity_set_sha256": task_identity_set_sha256,
        },
        "review_reference": {
            "atom_counts": review["reviewed_atom_counts"],
            "atom_qualifier_reference_basis": (
                "the frozen eight reviewed unsupported proposals are "
                "kind/payload meaning errors; polarity and temporal remain entails"
            ),
            "atom_qualifier_reference_sha256": _sha256(atom_qualifier_rows),
            "relation_reference_basis": (
                "derived before provider calls from frozen human atom labels, "
                "immutable raw relations, authenticated source branches, and "
                "the already-passed deterministic staging policy; not a separate "
                "independent human relation annotation"
            ),
            "relation_decision_counts": dict(relation_decisions),
            "relation_state_counts": dict(relation_states),
            "relation_reference_sha256": _sha256(reference_rows),
        },
        "frozen_acceptance_gates": copy.deepcopy(FROZEN_GATES),
        "controls": {
            "one_claim_per_provider_call": True,
            "provider_tools": [],
            "store": STORE,
            "reasoning_effort": REASONING_EFFORT,
            "candidate_profile_or_user_data": False,
            "runtime_integration": False,
            "database_access": False,
            "new_extraction_calls": 0,
            "new_grouping_calls": 0,
        },
    }
    manifest["manifest_sha256"] = _sha256(manifest)
    return manifest


def _require_frozen_manifest(path: Path, expected: dict) -> dict:
    if not path.exists():
        raise RuntimeError(
            "frozen manifest is absent; run --freeze-manifest after local tests"
        )
    actual = _read_json(path)
    if actual != expected:
        raise RuntimeError("frozen manifest does not match current verifier inputs")
    return actual


def _run_provider(
    tasks: list[dict],
    api_key: str,
    workers: int,
    *,
    accounting_path: Path,
    manifest_sha256: str,
    client_factory=None,
) -> tuple[dict, dict, float, dict]:
    local = threading.local()
    run_id = uuid.uuid4().hex
    journal = DurableAccountingJournal(accounting_path, run_id=run_id)
    journal.create(manifest_sha256=manifest_sha256, tasks=tasks)

    if client_factory is None:
        client_factory = lambda key, model: OpenAISemanticVerifierClient(
            key, model=model
        )

    def worker(task: dict) -> tuple[str, bool]:
        key = task["key"]
        journal.append("dispatch_started", task_key=key)
        client = getattr(local, "client", None)
        if client is None:
            client = client_factory(api_key, DEFAULT_MODEL)
            local.client = client
        started = time.perf_counter()
        try:
            result = client.verify(task["packet"])
            record = result_record(result)
        except Exception as exc:
            elapsed = time.perf_counter() - started
            diagnostics = copy.deepcopy(
                getattr(exc, "diagnostics", None) or {}
            )
            diagnostics.setdefault("latency_seconds", elapsed)
            diagnostics.setdefault(
                "schema_validation_outcome",
                "failed"
                if isinstance(exc, SemanticVerifierError)
                and getattr(exc, "category", "") == "schema_validation"
                else "not_reached",
            )
            failure = {
                "category": getattr(exc, "category", "harness_internal"),
                "exception_type": type(exc).__name__,
                "message": str(exc)[:500],
                "diagnostics": diagnostics,
            }
            journal.append(
                "request_failed", task_key=key, failure=failure
            )
            return key, False
        journal.append(
            "request_completed", task_key=key, result_record=record
        )
        return key, True

    started = time.perf_counter()
    total = len(tasks)
    completed = 0
    failures = 0
    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(worker, task): task for task in tasks}
            for future in as_completed(futures):
                _, succeeded = future.result()
                completed += int(succeeded)
                failures += int(not succeeded)
                terminal = completed + failures
                if terminal == 1 or terminal % 10 == 0 or terminal == total:
                    print(
                        "semantic verifier provider progress: "
                        f"{terminal}/{total} terminal "
                        f"({completed} completed, {failures} failed)",
                        flush=True,
                    )
    except BaseException as exc:
        journal.append(
            "run_interrupted",
            exception_type=type(exc).__name__,
            message=str(exc)[:500],
        )
        raise
    wall_seconds = time.perf_counter() - started
    journal.append(
        "run_closed",
        provider_wall_latency_seconds=wall_seconds,
        completed_requests=completed,
        failed_requests=failures,
    )
    accounting = reconstruct_accounting(accounting_path)
    if accounting["dispatched_requests"] != len(tasks):
        raise RuntimeError("provider dispatch accounting is incomplete")
    if completed + failures != len(tasks):
        raise RuntimeError("provider terminal accounting is incomplete")
    return accounting["records"], accounting["failures"], wall_seconds, accounting


def _prediction_qualifiers(record: dict) -> dict[str, str]:
    return {
        item["qualifier"]: item["decision"]
        for item in record["payload"]["qualifier_decisions"]
    }


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def _composition_controls() -> dict:
    fact = {
        "kind": "role_activity",
        "typed_payload": {"activity": "fact_checking", "artifact": "ai_output"},
        "evidence": [{"quote": "Verify factual accuracy in the model output."}],
    }
    generic = {
        "kind": "role_activity",
        "typed_payload": {"activity": "software_testing", "artifact": "software"},
        "evidence": [
            {
                "quote": "Perform generic quality assurance and fact-checking of generated tasks."
            }
        ],
    }
    software = copy.deepcopy(generic)
    software["evidence"] = [
        {"quote": "Test the application and its platform features."}
    ]
    return {
        "fact_checking_ai_output_supported": role_activity_composition_supported(
            fact
        ),
        "generic_qa_becomes_software_testing": role_activity_composition_supported(
            generic
        ),
        "actual_software_proposition_supports_software_testing": (
            role_activity_composition_supported(software)
        ),
    }


def evaluate_records(
    cases: dict,
    tasks: list[dict],
    records: dict,
    *,
    provider_wall_seconds: float,
) -> dict:
    task_by_key = {item["key"]: item for item in tasks}
    for key, record in records.items():
        validate_result_record(task_by_key[key]["packet"], record)

    evaluated = {}
    for case_id, data in cases.items():
        staged = copy.deepcopy(data["staged"])
        atom_records = {
            packet["claim_identity_sha256"]: records[
                f"atom:{case_id}:{atom_id}"
            ]
            for atom_id, packet in data["atom_packets"].items()
        }
        live_verification = staging_verification_from_results(
            staged, atom_records
        )
        live_verified = apply_semantic_verification(staged, live_verification)
        live_relations = construct_relations(
            live_verified, data["case"].get("raw_grouping")
        )
        relation_records = {
            packet["claim_identity_sha256"]: records[
                f"relation:{case_id}:{relation_id}"
            ]
            for relation_id, packet in data["relation_packets"].items()
        }
        live_verified, live_relations = apply_relation_verification(
            live_verified, live_relations, relation_records
        )
        finalization = finalize_verified_relations(
            live_verified, live_relations
        )
        evaluated[case_id] = {
            "staging": live_verified,
            "relations": live_relations,
            "finalization": finalization,
        }

    atom_rows = []
    atom_confusion = Counter()
    qualifier_metrics = {
        qualifier: {"correct": 0, "total": 0, "errors": []}
        for qualifier in ("kind_payload", "polarity", "temporal")
    }
    supported_entails = 0
    supported_not_established = 0
    supported_contradicted = 0
    unsupported_entails = []
    unsupported_safe = []
    for case_id, data in cases.items():
        for atom_id, gold in sorted(data["atom_labels"].items()):
            record = records[f"atom:{case_id}:{atom_id}"]
            predicted = record["payload"]["decision"]
            qualifiers = _prediction_qualifiers(record)
            gold_qualifiers = data["atom_qualifier_references"][atom_id]
            atom_confusion[(gold, predicted)] += 1
            if gold == "entails":
                supported_entails += predicted == "entails"
                supported_not_established += predicted == "not_established"
                supported_contradicted += predicted == "contradicts"
            elif predicted == "entails":
                unsupported_entails.append(f"{case_id}:{atom_id}")
            else:
                unsupported_safe.append(
                    {"identity": f"{case_id}:{atom_id}", "decision": predicted}
                )
            for qualifier, expected in gold_qualifiers.items():
                metric = qualifier_metrics[qualifier]
                metric["total"] += 1
                if qualifiers[qualifier] == expected:
                    metric["correct"] += 1
                else:
                    metric["errors"].append(
                        {
                            "identity": f"{case_id}:{atom_id}",
                            "expected": expected,
                            "observed": qualifiers[qualifier],
                        }
                    )
            atom_rows.append(
                {
                    "identity": f"{case_id}:{atom_id}",
                    "reviewed_decision": gold,
                    "verifier_decision": predicted,
                    "reviewed_qualifiers": gold_qualifiers,
                    "verifier_qualifiers": qualifiers,
                }
            )
    reviewed_supported = sum(
        value == "entails"
        for data in cases.values()
        for value in data["atom_labels"].values()
    )
    predicted_entails = sum(row["verifier_decision"] == "entails" for row in atom_rows)
    entailment_precision = (
        supported_entails / predicted_entails if predicted_entails else 1.0
    )
    supported_recall = (
        supported_entails / reviewed_supported if reviewed_supported else 1.0
    )
    for metric in qualifier_metrics.values():
        metric["accuracy"] = (
            metric["correct"] / metric["total"] if metric["total"] else 1.0
        )

    relation_rows = []
    relation_confusion = Counter()
    relation_qualifiers = {
        qualifier: {"correct": 0, "total": 0, "errors": []}
        for qualifier in ("relation_logic", "modality", "completeness")
    }
    relation_correct = 0
    relation_total = 0
    state_correct = 0
    state_total = 0
    source_branch_count = 0
    accounted_source_branch_count = 0
    unresolved_reference_branches = 0
    unresolved_branches_preserved = 0
    proposal_mutations = []
    incomplete_promoted = []
    live_relation_states = Counter()
    reference_relation_states = Counter()
    for case_id, data in cases.items():
        live_by_id = {
            relation["relation_id"]: relation
            for relation in evaluated[case_id]["relations"]["relations"]
        }
        reference_by_id = {
            relation["relation_id"]: relation
            for relation in data["reference_relations"]["relations"]
        }
        branches = evaluated[case_id]["relations"]["source_branch_accounting"]
        source_branch_count += branches["source_branch_count"]
        accounted_source_branch_count += branches["accounted_source_branch_count"]
        for relation_id, reference in reference_by_id.items():
            live = live_by_id[relation_id]
            reference_relation_states[reference["state"]] += 1
            live_relation_states[live["state"]] += 1
            state_total += 1
            state_correct += live["state"] == reference["state"]
            if (
                reference["state"] != "complete_verified"
                and live["state"] == "complete_verified"
            ):
                incomplete_promoted.append(f"{case_id}:{relation_id}")
            if reference.get("proposal_sha256") != live.get("proposal_sha256"):
                proposal_mutations.append(f"{case_id}:{relation_id}")
            reference_unresolved = {
                branch["source_branch_id"]
                for branch in reference["source_branches"]
                if branch["coverage_state"] == "unresolved"
            }
            live_unresolved = {
                branch["source_branch_id"]
                for branch in live["source_branches"]
                if branch["coverage_state"] == "unresolved"
            }
            unresolved_reference_branches += len(reference_unresolved)
            unresolved_branches_preserved += len(
                reference_unresolved & live_unresolved
            )
            if relation_id not in data["relation_references"]:
                continue
            gold = data["relation_references"][relation_id]
            record = records[f"relation:{case_id}:{relation_id}"]
            predicted = record["payload"]["decision"]
            predicted_qualifiers = _prediction_qualifiers(record)
            relation_total += 1
            relation_correct += predicted == gold["decision"]
            relation_confusion[(gold["decision"], predicted)] += 1
            for qualifier, expected in gold["qualifier_decisions"].items():
                metric = relation_qualifiers[qualifier]
                metric["total"] += 1
                if predicted_qualifiers[qualifier] == expected:
                    metric["correct"] += 1
                else:
                    metric["errors"].append(
                        {
                            "identity": f"{case_id}:{relation_id}",
                            "expected": expected,
                            "observed": predicted_qualifiers[qualifier],
                        }
                    )
            relation_rows.append(
                {
                    "identity": f"{case_id}:{relation_id}",
                    "reference_decision": gold["decision"],
                    "verifier_decision": predicted,
                    "reference_qualifiers": gold["qualifier_decisions"],
                    "verifier_qualifiers": predicted_qualifiers,
                    "reference_staging_state": reference["state"],
                    "live_staging_state": live["state"],
                }
            )
    for metric in relation_qualifiers.values():
        metric["accuracy"] = (
            metric["correct"] / metric["total"] if metric["total"] else 1.0
        )
    relation_accuracy = relation_correct / relation_total if relation_total else 1.0
    relation_state_accuracy = state_correct / state_total if state_total else 1.0
    source_branch_accounting = (
        accounted_source_branch_count / source_branch_count
        if source_branch_count
        else 1.0
    )

    hard_atom_ids = []
    unsupported_hard = []
    source_integrity_violations = []
    projected_from_incomplete = []
    projected_groups = 0
    correct_projected_groups = 0
    unsafe_hard_gates = []
    final_atom_count = 0
    final_relation_count = 0
    final_relation_state_exclusions = Counter()
    logic_weakening = []
    role_activity_live = []
    for case_id, data in cases.items():
        current = evaluated[case_id]
        labels = data["atom_labels"]
        reference_by_id = {
            relation["relation_id"]: relation
            for relation in data["reference_relations"]["relations"]
        }
        live_items = {
            item["proposal_id"]: item
            for item in current["staging"]["provisional_atoms"]
        }
        case_hard = {
            atom_id
            for atom_id, item in live_items.items()
            if item["assurance"] == "hard_projection_authorized"
        }
        hard_atom_ids.extend(f"{case_id}:{atom_id}" for atom_id in sorted(case_hard))
        unsupported = {
            atom_id for atom_id, decision in labels.items() if decision != "entails"
        }
        unsupported_hard.extend(
            f"{case_id}:{atom_id}" for atom_id in sorted(case_hard & unsupported)
        )
        final_atom_ids = {
            atom["id"]
            for atom in current["finalization"]["final_contract"]["atoms"]
        }
        final_atom_count += len(final_atom_ids)
        final_relation_ids = current["finalization"]["final_relation_ids"]
        final_relation_count += len(final_relation_ids)
        for atom_id in final_atom_ids:
            if not live_items[atom_id]["normalization"]["qualifier_complete"]:
                source_integrity_violations.append(f"{case_id}:{atom_id}")
        for relation_id, reference in reference_by_id.items():
            if relation_id not in final_relation_ids:
                final_relation_state_exclusions[reference["state"]] += 1
        if current["finalization"]["logic_weakening_detected"]:
            logic_weakening.append(case_id)
        for outcome in current["finalization"]["projection"]["group_outcomes"]:
            if outcome["state"] != "projected":
                continue
            projected_groups += 1
            relation_id = final_relation_ids[outcome["group_index"]]
            reference = reference_by_id[relation_id]
            atom_ids = set(outcome["atom_ids"])
            correct = (
                reference["state"] == "complete_verified"
                and not (atom_ids & unsupported)
            )
            correct_projected_groups += correct
            if reference["state"] != "complete_verified":
                projected_from_incomplete.append(f"{case_id}:{relation_id}")
            if not correct:
                unsafe_hard_gates.append(
                    {
                        "case_id": case_id,
                        "relation_id": relation_id,
                        "atom_ids": sorted(atom_ids),
                        "reference_state": reference["state"],
                    }
                )
        for relation_id in final_relation_ids:
            if reference_by_id[relation_id]["state"] != "complete_verified":
                unsafe_hard_gates.append(
                    {
                        "case_id": case_id,
                        "relation_id": relation_id,
                        "atom_ids": _relation_atom_ids(reference_by_id[relation_id]),
                        "reference_state": reference_by_id[relation_id]["state"],
                    }
                )
        for atom in current["finalization"]["final_contract"]["atoms"]:
            if atom["kind"] == "role_activity":
                role_activity_live.append(
                    {
                        "identity": f"{case_id}:{atom['id']}",
                        "typed_payload": atom["typed_payload"],
                    }
                )
    for identity in unsupported_hard:
        unsafe_hard_gates.append({"unsupported_atom_identity": identity})
    conditional_projection_precision = (
        correct_projected_groups / projected_groups if projected_groups else 1.0
    )

    acceptance_qualifier_correct = (
        qualifier_metrics["kind_payload"]["correct"]
        + qualifier_metrics["polarity"]["correct"]
        + qualifier_metrics["temporal"]["correct"]
        + relation_qualifiers["modality"]["correct"]
    )
    acceptance_qualifier_total = (
        qualifier_metrics["kind_payload"]["total"]
        + qualifier_metrics["polarity"]["total"]
        + qualifier_metrics["temporal"]["total"]
        + relation_qualifiers["modality"]["total"]
    )
    qualifier_semantic_accuracy = (
        acceptance_qualifier_correct / acceptance_qualifier_total
        if acceptance_qualifier_total
        else 1.0
    )

    notable_values = []
    all_notable_match = True
    review = _read_json(DEFAULT_REVIEW)
    for identity, expected_values in review["source_value_expectations"].items():
        case_id, atom_id = identity.split(":", 1)
        item = next(
            value
            for value in evaluated[case_id]["staging"]["provisional_atoms"]
            if value["proposal_id"] == atom_id
        )
        observed = [
            value["raw_value"]
            for value in item["normalization"]["source_values"]
            if value["raw_value"].casefold()
            in {expected.casefold() for expected in expected_values}
        ]
        preserved = {value.casefold() for value in observed} == {
            value.casefold() for value in expected_values
        }
        prediction = records[f"atom:{case_id}:{atom_id}"]["payload"]["decision"]
        expected_decision = cases[case_id]["atom_labels"][atom_id]
        meaning_correct = prediction == expected_decision
        all_notable_match = all_notable_match and preserved and meaning_correct
        notable_values.append(
            {
                "identity": identity,
                "expected_raw_values": expected_values,
                "observed_raw_values": observed,
                "normalization_status": item["normalization"]["status"],
                "reviewed_semantic_decision": expected_decision,
                "verifier_semantic_decision": prediction,
                "raw_value_preserved": preserved,
                "actual_source_meaning_decision_correct": meaning_correct,
            }
        )

    usage = Counter()
    latencies = []
    models = Counter()
    response_statuses = Counter()
    cost_known = True
    estimated_cost = 0.0
    for record in records.values():
        diagnostics = record["diagnostics"]
        for name in (
            "input_tokens",
            "cached_input_tokens",
            "cache_write_input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "visible_output_tokens",
            "total_tokens",
        ):
            usage[name] += diagnostics[name]
        latencies.append(diagnostics["latency_seconds"])
        models[diagnostics["response_model"]] += 1
        response_statuses[str(diagnostics["response_status"])] += 1
        if diagnostics["estimated_cost_usd"] is None:
            cost_known = False
        else:
            estimated_cost += diagnostics["estimated_cost_usd"]

    composition = _composition_controls()
    fact_check_identities = {"1422:a4", "1519:a8"}
    fact_predictions = {
        identity: next(
            row["verifier_decision"]
            for row in atom_rows
            if row["identity"] == identity
        )
        for identity in sorted(fact_check_identities)
    }
    proposed_software_testing = [
        row["identity"]
        for row in atom_rows
        if cases[row["identity"].split(":", 1)[0]]["atom_packets"][
            row["identity"].split(":", 1)[1]
        ]["claim"]["kind"]
        == "role_activity"
        and cases[row["identity"].split(":", 1)[0]]["atom_packets"][
            row["identity"].split(":", 1)[1]
        ]["claim"]["typed_payload"].get("activity")
        == "software_testing"
    ]

    atom_checks = {
        "entailment_precision": entailment_precision
        >= FROZEN_GATES["atom_verifier"]["minimum_entailment_precision"],
        "supported_recall": supported_recall
        >= FROZEN_GATES["atom_verifier"]["minimum_supported_recall"],
        "reviewed_unsupported_hard_assurance": len(unsupported_hard)
        <= FROZEN_GATES["atom_verifier"][
            "maximum_reviewed_unsupported_hard_assurance"
        ],
        "qualifier_semantic_accuracy": qualifier_semantic_accuracy
        >= FROZEN_GATES["atom_verifier"][
            "minimum_qualifier_semantic_accuracy"
        ],
    }
    relation_checks = {
        "review_bound_relation_decision_accuracy": relation_accuracy
        >= FROZEN_GATES["relation_verifier"][
            "minimum_review_bound_relation_decision_accuracy"
        ],
        "source_branch_accounting": source_branch_accounting
        == FROZEN_GATES["relation_verifier"][
            "required_source_branch_accounting"
        ],
        "or_and_weakening": len(logic_weakening) + len(proposal_mutations)
        <= FROZEN_GATES["relation_verifier"]["maximum_or_and_weakening"],
        "incomplete_relation_promoted_complete": len(incomplete_promoted)
        <= FROZEN_GATES["relation_verifier"][
            "maximum_incomplete_relation_promoted_complete"
        ],
    }
    final_checks = {
        "source_value_substitutions": len(source_integrity_violations)
        <= FROZEN_GATES["final_safety"]["maximum_source_value_substitutions"],
        "projection_from_incomplete_relations": len(projected_from_incomplete)
        <= FROZEN_GATES["final_safety"][
            "maximum_projection_from_incomplete_relations"
        ],
        "conditional_projection_precision": conditional_projection_precision
        == FROZEN_GATES["final_safety"][
            "required_conditional_projection_precision"
        ],
        "unsupported_deterministic_hard_gates": len(unsafe_hard_gates)
        <= FROZEN_GATES["final_safety"][
            "maximum_unsupported_deterministic_hard_gates"
        ],
    }
    all_checks = {
        "atom_verifier": atom_checks,
        "relation_verifier": relation_checks,
        "final_safety": final_checks,
    }
    verifier_passed = all(atom_checks.values()) and all(relation_checks.values())
    safety_passed = all(final_checks.values())
    if verifier_passed and safety_passed:
        verdict = "SEMANTIC VERIFIER REGRESSION GATES PASSED"
    elif not safety_passed and (
        logic_weakening
        or proposal_mutations
        or projected_from_incomplete
        or source_integrity_violations
    ):
        verdict = "STAGING CONTRACT GAP FOUND"
    else:
        verdict = "VERIFIER WORK NEEDED"

    error_families = defaultdict(list)
    for row in atom_rows:
        gold = row["reviewed_decision"]
        predicted = row["verifier_decision"]
        if gold == predicted:
            continue
        if gold == "entails" and predicted == "not_established":
            family = "supported_claim_conservative_abstention"
        elif gold == "entails" and predicted == "contradicts":
            family = "supported_claim_false_contradiction"
        elif gold != "entails" and predicted == "entails":
            family = "unsupported_claim_overacceptance"
        else:
            family = "unsupported_claim_wrong_rejection_class"
        error_families[family].append(row["identity"])
    for row in relation_rows:
        if row["reference_decision"] == row["verifier_decision"]:
            continue
        if (
            row["reference_decision"] == "entails"
            and row["verifier_decision"] == "not_established"
        ):
            family = "supported_relation_conservative_abstention"
        elif (
            row["reference_decision"] != "entails"
            and row["verifier_decision"] == "entails"
        ):
            family = "incomplete_or_invalid_relation_overacceptance"
        else:
            family = "relation_wrong_decision_class"
        error_families[family].append(row["identity"])

    return {
        "atom_verifier": {
            "reviewed_total": len(atom_rows),
            "reviewed_supported": reviewed_supported,
            "reviewed_unsupported": len(atom_rows) - reviewed_supported,
            "entailment_precision": entailment_precision,
            "supported_recall": supported_recall,
            "supported_claims_marked_entails": supported_entails,
            "supported_claims_marked_not_established": supported_not_established,
            "supported_claims_incorrectly_contradicted": supported_contradicted,
            "unsupported_claims_incorrectly_marked_entails": unsupported_entails,
            "unsupported_claims_safely_rejected_or_unresolved": unsupported_safe,
            "confusion": {
                f"{gold}->{predicted}": count
                for (gold, predicted), count in sorted(atom_confusion.items())
            },
            "qualifiers": qualifier_metrics,
            "acceptance_qualifier_semantic_accuracy": qualifier_semantic_accuracy,
            "decisions": atom_rows,
        },
        "relation_verifier": {
            "reference_basis": (
                "frozen human atom labels plus immutable relation/source-branch "
                "ledger and deterministic staging policy; no separate independent "
                "human relation annotation"
            ),
            "reviewed_claim_count": relation_total,
            "decision_accuracy": relation_accuracy,
            "downstream_state_accuracy": relation_state_accuracy,
            "confusion": {
                f"{gold}->{predicted}": count
                for (gold, predicted), count in sorted(relation_confusion.items())
            },
            "qualifiers": relation_qualifiers,
            "source_branch_count": source_branch_count,
            "accounted_source_branch_count": accounted_source_branch_count,
            "source_branch_accounting": source_branch_accounting,
            "unresolved_reference_branches": unresolved_reference_branches,
            "unresolved_branches_preserved": unresolved_branches_preserved,
            "proposal_mutations": proposal_mutations,
            "or_and_weakening_cases": logic_weakening,
            "incomplete_relation_promoted_complete": incomplete_promoted,
            "reference_states": dict(reference_relation_states),
            "live_states": dict(live_relation_states),
            "decisions": relation_rows,
        },
        "supported_proposal_retention": {
            "reviewed_supported": reviewed_supported,
            "retained_by_semantic_entailment": supported_entails,
            "rate": supported_recall,
        },
        "unsupported_proposal_behavior": {
            "reviewed_unsupported": len(atom_rows) - reviewed_supported,
            "semantic_overacceptance": unsupported_entails,
            "hard_assurance_acceptance": unsupported_hard,
            "safe_rejection_or_unresolved": unsupported_safe,
        },
        "normalization_and_unmapped_values": {
            "notable_values": notable_values,
            "all_actual_source_meaning_decisions_correct": all_notable_match,
            "provider_inputs_contained_normalized_values": False,
            "final_source_value_substitution_or_qualifier_loss": (
                source_integrity_violations
            ),
        },
        "staging_and_finalization": {
            "reference_relation_states": dict(reference_relation_states),
            "live_relation_states": dict(live_relation_states),
            "final_atom_count": final_atom_count,
            "final_relation_count": final_relation_count,
            "excluded_relations_by_reference_state": dict(
                final_relation_state_exclusions
            ),
            "hard_projection_authorized_atom_ids": hard_atom_ids,
            "incomplete_relation_promoted_complete": incomplete_promoted,
        },
        "projection_and_hard_gates": {
            "projected_groups": projected_groups,
            "correct_projected_groups": correct_projected_groups,
            "conditional_projection_precision": conditional_projection_precision,
            "projected_from_incomplete_relations": projected_from_incomplete,
            "logic_weakening_cases": logic_weakening,
            "proposal_mutations": proposal_mutations,
            "reviewed_unsupported_hard_assurance": unsupported_hard,
            "unsafe_hard_gates": unsafe_hard_gates,
        },
        "role_activity_controls": {
            **composition,
            "live_fact_checking_ai_output_predictions": fact_predictions,
            "live_final_role_activities": role_activity_live,
            "proposed_software_testing_atoms_in_frozen_population": (
                proposed_software_testing
            ),
            "ids_or_phrases_special_cased_in_verifier_implementation": False,
        },
        "error_attribution": {
            "evaluated_provisional_atoms_only": True,
            "omitted_extraction_atoms_scored_as_verifier_errors": False,
            "verifier_error_families": dict(error_families),
            "semantic_overacceptance_but_finalizer_blocked": sorted(
                set(unsupported_entails) - set(unsupported_hard)
            ),
            "stage_boundaries": [
                "extraction_quality",
                "deterministic_authentication",
                "normalization",
                "semantic_verifier",
                "relation_verifier",
                "finalization_assurance",
                "projection",
            ],
        },
        "usage": {
            "provider_calls": len(records),
            **dict(usage),
            "provider_wall_latency_seconds": provider_wall_seconds,
            "sum_call_latency_seconds": sum(latencies),
            "mean_call_latency_seconds": statistics.fmean(latencies)
            if latencies
            else 0.0,
            "p50_call_latency_seconds": statistics.median(latencies)
            if latencies
            else 0.0,
            "p95_call_latency_seconds": _percentile(latencies, 0.95),
            "max_call_latency_seconds": max(latencies) if latencies else 0.0,
            "estimated_cost_usd": round(estimated_cost, 8)
            if cost_known
            else None,
            "returned_model_aliases": dict(models),
            "response_statuses": dict(response_statuses),
        },
        "acceptance": {
            "frozen_gates": copy.deepcopy(FROZEN_GATES),
            "checks": all_checks,
            "atom_verifier_passed": all(atom_checks.values()),
            "relation_verifier_passed": all(relation_checks.values()),
            "final_safety_passed": all(final_checks.values()),
            "passed": verifier_passed and safety_passed,
        },
        "verdict": verdict,
    }


def build_evaluation_report(
    records: dict,
    *,
    provider_wall_seconds: float,
    manifest: dict,
    accounting: dict | None = None,
    raw_path: Path = DEFAULT_RAW,
    preregistration_path: Path = DEFAULT_PREREGISTRATION,
    review_path: Path = DEFAULT_REVIEW,
) -> dict:
    raw, preregistration, review, artifacts, population = build_population(
        raw_path, preregistration_path, review_path
    )
    cases, tasks = population
    metrics = evaluate_records(
        cases,
        tasks,
        records,
        provider_wall_seconds=provider_wall_seconds,
    )
    report = {
        "report_version": REPORT_VERSION,
        "evaluator_version": EVALUATOR_VERSION,
        "mode": "single_live_evaluation_of_opened_development_regression_set",
        "fresh_evidence_claimed": False,
        "manifest_sha256": manifest["manifest_sha256"],
        "provider_configuration": model_configuration_identity(),
        "identities": copy.deepcopy(manifest["identities"]),
        "artifacts": copy.deepcopy(manifest["artifacts"]),
        "population": copy.deepcopy(manifest["population"]),
        "review_reference": copy.deepcopy(manifest["review_reference"]),
        "metrics": metrics,
        "provider_results": {
            key: records[key] for key in sorted(records)
        },
        "durable_accounting": {
            key: copy.deepcopy(accounting[key])
            for key in (
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
        }
        if accounting is not None
        else None,
        "preservation": {
            "raw_fixture_preserved": raw["fixture_preserved"],
            "database_preserved_by_opened_canary": raw["database_preserved"],
            "database_reads_this_evaluation": 0,
            "database_writes_this_evaluation": 0,
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
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--accounting", type=Path, default=DEFAULT_ACCOUNTING)
    parser.add_argument("--workers", type=int, default=8)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    manifest = build_manifest(args.raw, args.preregistration, args.review)
    if args.freeze_manifest:
        if args.manifest.exists():
            raise RuntimeError("refusing to overwrite an existing frozen manifest")
        _write_json(args.manifest, manifest)
        print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    if args.output.exists():
        raise RuntimeError("refusing to rerun or overwrite the live evaluation")
    if args.accounting.exists():
        raise RuntimeError("refusing to reuse an existing accounting journal")
    if args.workers < 1 or args.workers > 16:
        raise RuntimeError("--workers must be between 1 and 16")
    frozen = _require_frozen_manifest(args.manifest, manifest)
    api_key = str(os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required for live evaluation")
    _, _, _, _, population = build_population(
        args.raw, args.preregistration, args.review
    )
    _, tasks = population
    records, failures, wall_seconds, accounting = _run_provider(
        tasks,
        api_key,
        args.workers,
        accounting_path=args.accounting,
        manifest_sha256=frozen["manifest_sha256"],
    )
    if failures:
        blocked = {
            "report_version": REPORT_VERSION,
            "evaluator_version": EVALUATOR_VERSION,
            "mode": "schema_repaired_execution_blocked_before_semantic_scoring",
            "fresh_evidence_claimed": False,
            "manifest_sha256": frozen["manifest_sha256"],
            "provider_configuration": model_configuration_identity(),
            "population": copy.deepcopy(frozen["population"]),
            "durable_accounting": {
                key: copy.deepcopy(accounting[key])
                for key in (
                    "accounting_version",
                    "run_id",
                    "event_count",
                    "registered_tasks",
                    "dispatched_requests",
                    "completed_requests",
                    "failed_requests",
                    "status_counts",
                    "journal_sha256",
                    "failures",
                )
            },
            "semantic_results_evaluable": False,
            "verdict": "BLOCKED",
        }
        blocked["report_sha256"] = _sha256(blocked)
        _write_json(args.output, blocked)
        print(json.dumps(blocked, ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    report = build_evaluation_report(
        records,
        provider_wall_seconds=wall_seconds,
        manifest=frozen,
        accounting=accounting,
        raw_path=args.raw,
        preregistration_path=args.preregistration,
        review_path=args.review,
    )
    _write_json(args.output, report)
    summary = {
        "report_path": str(args.output),
        "report_sha256": report["report_sha256"],
        "verdict": report["metrics"]["verdict"],
        "acceptance": report["metrics"]["acceptance"],
        "usage": report["metrics"]["usage"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Frozen scoring for the two-stage OE Semantic Grouping v0 proof."""

from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter

from wahojobs.opportunity_semantic_evaluation import (
    _pair_groups,
    aggregate_scores,
    atom_descriptor,
    atom_signature,
    score_case,
)


GROUPING_EVALUATION_VERSION = "oe_semantic_grouping_v0_evaluation_v1"

# Frozen before the first grouping-only model call.
GROUPING_ACCEPTANCE_CRITERIA = {
    "minimum_validated_expected_atom_recall": 1.0,
    "maximum_unsupported_validated_atoms": 0,
    "minimum_group_modality_accuracy": 0.90,
    "minimum_group_structure_accuracy": 0.90,
    "minimum_independent_group_accuracy": 1.0,
    "minimum_group_evidence_support_rate": 1.0,
    "maximum_ungrouped_expected_atoms": 0,
    "minimum_projection_precision": 1.0,
    "minimum_projection_recall": 1.0,
    "maximum_unsupported_survivors": 0,
    "maximum_unsafe_hard_gate_projections": 0,
}


def _canonical_json(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def grouping_evaluation_sha256() -> str:
    identity = {
        "version": GROUPING_EVALUATION_VERSION,
        "criteria": GROUPING_ACCEPTANCE_CRITERIA,
    }
    return hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()


def _rate(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 1.0
    return round(numerator / denominator, 6)


def _counter_descriptors(atoms: list[dict], remaining: Counter) -> list[dict]:
    output = []
    pending = remaining.copy()
    for atom in atoms:
        signature = atom_signature(atom)
        if pending[signature] <= 0:
            continue
        output.append(atom_descriptor(atom))
        pending[signature] -= 1
    return output


def _independent_group_pairs(
    expected_payload: dict,
    predicted_payload: dict,
) -> tuple[int, int]:
    expected_atoms = {
        atom["id"]: atom_signature(atom)
        for atom in expected_payload.get("atoms") or []
    }
    predicted_atoms = {
        atom["id"]: atom_signature(atom)
        for atom in predicted_payload.get("atoms") or []
    }
    expected_location = {}
    for group_index, group in enumerate(expected_payload.get("constraint_groups") or []):
        for alternative in group.get("any_of") or []:
            for atom_id in alternative.get("all_of") or []:
                if atom_id in expected_atoms:
                    expected_location[expected_atoms[atom_id]] = group_index
    predicted_location = {}
    for group_index, group in enumerate(predicted_payload.get("constraint_groups") or []):
        for alternative in group.get("any_of") or []:
            for atom_id in alternative.get("all_of") or []:
                if atom_id in predicted_atoms:
                    predicted_location[predicted_atoms[atom_id]] = group_index

    signatures = sorted(expected_location)
    denominator = 0
    correct = 0
    for left_index, left in enumerate(signatures):
        for right in signatures[left_index + 1 :]:
            if expected_location[left] == expected_location[right]:
                continue
            denominator += 1
            if (
                left in predicted_location
                and right in predicted_location
                and predicted_location[left] != predicted_location[right]
            ):
                correct += 1
    return correct, denominator


def score_grouping_case(
    expected_payload: dict,
    frozen_payload: dict,
    atom_validation: dict,
    grouping_payload: dict,
    group_validation: dict,
    projection: dict,
    expected_projection: dict,
) -> dict:
    """Score atom filtering, grouping, group evidence, and projection separately."""

    predicted_payload = {
        "extraction_version": expected_payload["extraction_version"],
        "atoms": copy.deepcopy(atom_validation["validated_model_atoms"]),
        "constraint_groups": copy.deepcopy(
            group_validation["accepted_model_groups"]
        ),
    }
    score_validation = copy.deepcopy(group_validation)
    score_validation["accepted_group_indices"] = list(
        range(len(group_validation["accepted_model_groups"]))
    )
    base = score_case(
        expected_payload,
        predicted_payload,
        score_validation,
        projection,
        expected_projection,
    )

    expected_atoms = expected_payload.get("atoms") or []
    frozen_atoms = frozen_payload.get("atoms") or []
    validated_atoms = atom_validation.get("validated_model_atoms") or []
    expected_counter = Counter(atom_signature(atom) for atom in expected_atoms)
    frozen_counter = Counter(atom_signature(atom) for atom in frozen_atoms)
    validated_counter = Counter(atom_signature(atom) for atom in validated_atoms)
    frozen_exact = sum((expected_counter & frozen_counter).values())
    validated_expected = sum((expected_counter & validated_counter).values())
    unsupported_validated_counter = validated_counter - expected_counter

    group_pairs = _pair_groups(expected_payload, predicted_payload)
    modality_correct = sum(int(item[3]) for item in group_pairs)
    independent_correct, independent_denominator = _independent_group_pairs(
        expected_payload, predicted_payload
    )
    proposed_group_count = len(grouping_payload.get("constraint_groups") or [])
    supported_group_count = len(
        group_validation.get("accepted_group_evidence") or []
    )

    validated_by_id = {
        atom["id"]: atom for atom in validated_atoms
    }
    ungrouped_atoms = [
        validated_by_id[atom_id]
        for atom_id in group_validation.get("ungrouped_atom_ids") or []
        if atom_id in validated_by_id
    ]
    ungrouped_counter = Counter(atom_signature(atom) for atom in ungrouped_atoms)
    ungrouped_expected_count = sum(
        (ungrouped_counter & expected_counter).values()
    )

    grouping_ready = (
        validated_expected == len(expected_atoms)
        and not unsupported_validated_counter
        and base["extraction"]["exact_groups"]
        == len(expected_payload.get("constraint_groups") or [])
        == len(predicted_payload["constraint_groups"])
        and not group_validation.get("rejected_groups")
        and not group_validation.get("ungrouped_atom_ids")
        and not base["validation"]["unsupported_propositions_survived"]
        and not base["projection"]["unsafe_hard_gate_projections"]
        and not base["projection"]["projection_failures"]
        and base["projection"]["precision"] == 1.0
        and base["projection"]["recall"] == 1.0
    )
    return {
        "atom_stage": {
            "expected_atoms": len(expected_atoms),
            "frozen_atoms": len(frozen_atoms),
            "frozen_exact_atoms": frozen_exact,
            "frozen_atom_precision": _rate(frozen_exact, len(frozen_atoms)),
            "frozen_atom_recall": _rate(frozen_exact, len(expected_atoms)),
            "validated_atoms": len(validated_atoms),
            "validated_expected_atoms": validated_expected,
            "validated_expected_atom_recall": _rate(
                validated_expected, len(expected_atoms)
            ),
            "unsupported_validated_atoms": _counter_descriptors(
                validated_atoms, unsupported_validated_counter
            ),
            "rejected_atoms": copy.deepcopy(
                atom_validation.get("rejected_atoms") or []
            ),
        },
        "grouping": {
            "expected_groups": len(expected_payload.get("constraint_groups") or []),
            "proposed_groups": proposed_group_count,
            "accepted_groups": len(group_validation.get("accepted_model_groups") or []),
            "exact_groups": base["extraction"]["exact_groups"],
            "and_or_structure_accuracy": base["extraction"][
                "group_structure_accuracy"
            ],
            "modality_correct": modality_correct,
            "modality_accuracy": _rate(
                modality_correct,
                len(expected_payload.get("constraint_groups") or []),
            ),
            "independent_group_correct": independent_correct,
            "independent_group_denominator": independent_denominator,
            "independent_group_accuracy": _rate(
                independent_correct, independent_denominator
            ),
            "group_evidence_supported": supported_group_count,
            "group_evidence_support_rate": _rate(
                supported_group_count, proposed_group_count
            ),
            "ungrouped_atom_ids": copy.deepcopy(
                group_validation.get("ungrouped_atom_ids") or []
            ),
            "ungrouped_expected_atoms": ungrouped_expected_count,
            "rejected_groups": copy.deepcopy(
                group_validation.get("rejected_groups") or []
            ),
        },
        "validation": base["validation"],
        "projection": base["projection"],
        "two_stage_human_ready": grouping_ready,
    }


def aggregate_grouping_scores(case_scores: list[tuple[str, dict]]) -> dict:
    atom_sums = Counter()
    grouping_sums = Counter()
    unsupported_validated = []
    rejected_atoms = []
    rejected_groups = []
    ungrouped_ids = []
    ready_ids = []
    base_scores = []
    for case_id, score in case_scores:
        atom = score["atom_stage"]
        grouping = score["grouping"]
        for key in (
            "expected_atoms",
            "frozen_atoms",
            "frozen_exact_atoms",
            "validated_atoms",
            "validated_expected_atoms",
        ):
            atom_sums[key] += atom[key]
        unsupported_validated.extend(
            {"case_id": case_id, **item}
            for item in atom["unsupported_validated_atoms"]
        )
        rejected_atoms.extend(
            {"case_id": case_id, **item} for item in atom["rejected_atoms"]
        )
        for key in (
            "expected_groups",
            "proposed_groups",
            "accepted_groups",
            "exact_groups",
            "modality_correct",
            "independent_group_correct",
            "independent_group_denominator",
            "group_evidence_supported",
            "ungrouped_expected_atoms",
        ):
            grouping_sums[key] += grouping[key]
        rejected_groups.extend(
            {"case_id": case_id, **item}
            for item in grouping["rejected_groups"]
        )
        ungrouped_ids.extend(
            {"case_id": case_id, "atom_id": atom_id}
            for atom_id in grouping["ungrouped_atom_ids"]
        )
        if score["two_stage_human_ready"]:
            ready_ids.append(case_id)
        base_scores.append(
            (
                case_id,
                {
                    "extraction": {
                        "expected_atoms": atom["expected_atoms"],
                        "predicted_atoms": atom["validated_atoms"],
                        "exact_atoms": atom["validated_expected_atoms"],
                        "evidence_supported_atoms": atom["validated_atoms"],
                        "atom_kind_correct": atom["validated_expected_atoms"],
                        "payload_correct": atom["validated_expected_atoms"],
                        "polarity_correct": atom["validated_expected_atoms"],
                        "temporal_correct": atom["validated_expected_atoms"],
                        "expected_groups": grouping["expected_groups"],
                        "predicted_groups": grouping["accepted_groups"],
                        "exact_groups": grouping["exact_groups"],
                        "required_preferred_denominator": grouping["expected_groups"],
                        "required_preferred_correct": grouping["modality_correct"],
                        "group_structure_correct": grouping["exact_groups"],
                        "material_false_positives": atom[
                            "unsupported_validated_atoms"
                        ],
                        "material_false_negatives": [],
                    },
                    "validation": score["validation"],
                    "projection": score["projection"],
                    "human_quality_semantic_ready": score[
                        "two_stage_human_ready"
                    ],
                },
            )
        )

    # Reuse the proven projection/validation aggregation, then replace the
    # extraction-facing view with explicitly staged grouping metrics below.
    aggregate = aggregate_scores(base_scores)
    aggregate["atom_stage"] = {
        **dict(atom_sums),
        "frozen_atom_precision": _rate(
            atom_sums["frozen_exact_atoms"], atom_sums["frozen_atoms"]
        ),
        "frozen_atom_recall": _rate(
            atom_sums["frozen_exact_atoms"], atom_sums["expected_atoms"]
        ),
        "validated_expected_atom_recall": _rate(
            atom_sums["validated_expected_atoms"], atom_sums["expected_atoms"]
        ),
        "unsupported_validated_atoms": unsupported_validated,
        "rejected_atoms": rejected_atoms,
    }
    aggregate["grouping"] = {
        **dict(grouping_sums),
        "and_or_structure_accuracy": _rate(
            grouping_sums["exact_groups"], grouping_sums["expected_groups"]
        ),
        "modality_accuracy": _rate(
            grouping_sums["modality_correct"], grouping_sums["expected_groups"]
        ),
        "independent_group_accuracy": _rate(
            grouping_sums["independent_group_correct"],
            grouping_sums["independent_group_denominator"],
        ),
        "group_evidence_support_rate": _rate(
            grouping_sums["group_evidence_supported"],
            grouping_sums["proposed_groups"],
        ),
        "ungrouped_grounded_atoms": ungrouped_ids,
        "rejected_groups": rejected_groups,
    }
    aggregate["two_stage_human_ready_count"] = len(ready_ids)
    aggregate["two_stage_human_ready_ids"] = sorted(ready_ids)
    return aggregate


def grouping_acceptance(aggregate: dict) -> dict:
    atom = aggregate["atom_stage"]
    grouping = aggregate["grouping"]
    validation = aggregate["validation"]
    projection = aggregate["projection"]
    criteria = GROUPING_ACCEPTANCE_CRITERIA
    checks = {
        "validated_expected_atom_recall": (
            atom["validated_expected_atom_recall"]
            >= criteria["minimum_validated_expected_atom_recall"]
        ),
        "unsupported_validated_atoms": (
            len(atom["unsupported_validated_atoms"])
            <= criteria["maximum_unsupported_validated_atoms"]
        ),
        "group_modality_accuracy": (
            grouping["modality_accuracy"]
            >= criteria["minimum_group_modality_accuracy"]
        ),
        "group_structure_accuracy": (
            grouping["and_or_structure_accuracy"]
            >= criteria["minimum_group_structure_accuracy"]
        ),
        "independent_group_accuracy": (
            grouping["independent_group_accuracy"]
            >= criteria["minimum_independent_group_accuracy"]
        ),
        "group_evidence_support_rate": (
            grouping["group_evidence_support_rate"]
            >= criteria["minimum_group_evidence_support_rate"]
        ),
        "ungrouped_expected_atoms": (
            grouping["ungrouped_expected_atoms"]
            <= criteria["maximum_ungrouped_expected_atoms"]
        ),
        "projection_precision": (
            projection["precision"] >= criteria["minimum_projection_precision"]
        ),
        "projection_recall": (
            projection["recall"] >= criteria["minimum_projection_recall"]
        ),
        "unsupported_survivors": (
            len(validation["unsupported_propositions_survived"])
            <= criteria["maximum_unsupported_survivors"]
        ),
        "unsafe_hard_gate_projections": (
            len(projection["unsafe_hard_gate_projections"])
            <= criteria["maximum_unsafe_hard_gate_projections"]
        ),
    }
    return {
        "criteria_version": GROUPING_EVALUATION_VERSION,
        "criteria_sha256": grouping_evaluation_sha256(),
        "criteria": copy.deepcopy(criteria),
        "checks": checks,
        "satisfactory": all(checks.values()),
    }


__all__ = [
    "GROUPING_ACCEPTANCE_CRITERIA",
    "GROUPING_EVALUATION_VERSION",
    "aggregate_grouping_scores",
    "grouping_acceptance",
    "grouping_evaluation_sha256",
    "score_grouping_case",
]

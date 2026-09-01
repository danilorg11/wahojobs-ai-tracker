"""Offline scoring for OE Semantic Extraction v0.

Extraction, server validation, and compatibility projection are intentionally
scored as separate stages so a grounded-but-unprojected proposition is never
misclassified as a model extraction error.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter

from wahojobs.opportunity_semantic_contract import (
    HARD_LEGACY_FIELD_PATHS,
    flatten_patch_paths,
)
from wahojobs.opportunity_semantic_extraction import (
    EXTRACTION_CONTRACT_VERSION,
)


EVALUATION_CRITERIA_VERSION = "oe_semantic_extraction_v0_evaluation_v1"

# Frozen before the first live regression extraction.  These are stage-specific
# gates, not prompt-tuning targets for individual fixtures.
REGRESSION_ACCEPTANCE_CRITERIA = {
    "minimum_atom_precision": 0.95,
    "minimum_atom_recall": 0.90,
    "minimum_atom_kind_accuracy": 0.95,
    "minimum_payload_accuracy": 0.95,
    "minimum_polarity_accuracy": 0.95,
    "minimum_temporal_accuracy": 0.95,
    "minimum_required_preferred_accuracy": 0.90,
    "minimum_group_structure_accuracy": 0.90,
    "minimum_projection_precision": 1.0,
    "minimum_projection_on_correct_groups_accuracy": 1.0,
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


def evaluation_criteria_sha256() -> str:
    identity = {
        "version": EVALUATION_CRITERIA_VERSION,
        "criteria": REGRESSION_ACCEPTANCE_CRITERIA,
    }
    return hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()


def model_payload_from_reviewed_contract(contract: dict) -> dict:
    """Project a reviewed v0 contract back to the model-facing comparison shape."""

    atoms = []
    for raw_atom in contract["atoms"]:
        payload = copy.deepcopy(raw_atom["typed_payload"])
        if raw_atom["kind"] == "experience":
            payload.setdefault("minimum_years", None)
        elif raw_atom["kind"] == "education":
            payload.setdefault("field", None)
        elif raw_atom["kind"] == "professional_credential":
            payload.setdefault("jurisdiction", None)
        atoms.append(
            {
                "id": raw_atom["id"],
                "kind": raw_atom["kind"],
                "typed_payload": payload,
                "polarity": raw_atom["polarity"],
                "temporal": raw_atom["temporal"],
                "evidence": [
                    {
                        "alias": item["source_id"],
                        "quote": item["quote"],
                    }
                    for item in raw_atom["evidence"]
                ],
            }
        )
    return {
        "extraction_version": EXTRACTION_CONTRACT_VERSION,
        "atoms": atoms,
        "constraint_groups": copy.deepcopy(contract["constraint_groups"]),
    }


def _normalized_model_payload(kind: str, payload) -> dict:
    if type(payload) is not dict:
        return {"__invalid_payload__": payload}
    normalized = {
        key: copy.deepcopy(value)
        for key, value in payload.items()
        if value is not None
    }
    if kind == "language_proficiency" and "locale" in payload:
        normalized["locale"] = payload["locale"]
    return normalized


def atom_signature(atom: dict) -> str:
    kind = atom.get("kind")
    return _canonical_json(
        {
            "kind": kind,
            "typed_payload": _normalized_model_payload(
                kind, atom.get("typed_payload")
            ),
            "polarity": atom.get("polarity"),
            "temporal": atom.get("temporal"),
        }
    )


def atom_descriptor(atom: dict) -> dict:
    kind = atom.get("kind")
    return {
        "id": atom.get("id"),
        "kind": kind,
        "typed_payload": _normalized_model_payload(
            kind, atom.get("typed_payload")
        ),
        "polarity": atom.get("polarity"),
        "temporal": atom.get("temporal"),
        "evidence_aliases": sorted(
            {
                item.get("alias")
                for item in atom.get("evidence") or []
                if type(item) is dict and type(item.get("alias")) is str
            }
        ),
    }


def _atom_evidence_aliases(atom: dict) -> set[str]:
    return {
        item.get("alias")
        for item in atom.get("evidence") or []
        if type(item) is dict and type(item.get("alias")) is str
    }


def _pair_atoms(expected_atoms: list[dict], predicted_atoms: list[dict]):
    candidates = []
    for expected_index, expected in enumerate(expected_atoms):
        for predicted_index, predicted in enumerate(predicted_atoms):
            kind_equal = expected.get("kind") == predicted.get("kind")
            payload_equal = _normalized_model_payload(
                expected.get("kind"), expected.get("typed_payload")
            ) == _normalized_model_payload(
                predicted.get("kind"), predicted.get("typed_payload")
            )
            polarity_equal = expected.get("polarity") == predicted.get("polarity")
            temporal_equal = expected.get("temporal") == predicted.get("temporal")
            evidence_overlap = bool(
                _atom_evidence_aliases(expected)
                & _atom_evidence_aliases(predicted)
            )
            score = (
                8 * int(kind_equal)
                + 8 * int(payload_equal)
                + 2 * int(polarity_equal)
                + 2 * int(temporal_equal)
                + 4 * int(evidence_overlap)
            )
            if score:
                candidates.append(
                    (
                        score,
                        expected_index,
                        predicted_index,
                        kind_equal,
                        payload_equal,
                        polarity_equal,
                        temporal_equal,
                    )
                )
    pairs = []
    used_expected = set()
    used_predicted = set()
    for candidate in sorted(candidates, key=lambda item: (-item[0], item[1], item[2])):
        _, expected_index, predicted_index, *_ = candidate
        if expected_index in used_expected or predicted_index in used_predicted:
            continue
        used_expected.add(expected_index)
        used_predicted.add(predicted_index)
        pairs.append(candidate)
    return pairs


def _groups(payload: dict) -> tuple[list[dict], dict[str, str]]:
    atoms = payload.get("atoms") if type(payload) is dict else []
    groups = payload.get("constraint_groups") if type(payload) is dict else []
    atoms = atoms if type(atoms) is list else []
    groups = groups if type(groups) is list else []
    signatures = {
        atom.get("id"): atom_signature(atom)
        for atom in atoms
        if type(atom) is dict and type(atom.get("id")) is str
    }
    return groups, signatures


def group_structure(group: dict, signatures: dict[str, str]) -> tuple:
    alternatives = []
    for alternative in group.get("any_of") or []:
        conjunction = tuple(
            sorted(
                signatures.get(atom_id, f"__missing_atom__:{atom_id}")
                for atom_id in alternative.get("all_of") or []
            )
        )
        alternatives.append(conjunction)
    return tuple(sorted(alternatives))


def group_signature(group: dict, signatures: dict[str, str]) -> str:
    return _canonical_json(
        {
            "modality": group.get("modality"),
            "any_of": group_structure(group, signatures),
        }
    )


def _group_atom_signatures(group: dict, signatures: dict[str, str]) -> Counter:
    return Counter(
        signatures.get(atom_id, f"__missing_atom__:{atom_id}")
        for alternative in group.get("any_of") or []
        for atom_id in alternative.get("all_of") or []
    )


def _pair_groups(expected_payload: dict, predicted_payload: dict):
    expected_groups, expected_signatures = _groups(expected_payload)
    predicted_groups, predicted_signatures = _groups(predicted_payload)
    candidates = []
    for expected_index, expected in enumerate(expected_groups):
        expected_atoms = _group_atom_signatures(expected, expected_signatures)
        expected_structure = group_structure(expected, expected_signatures)
        for predicted_index, predicted in enumerate(predicted_groups):
            predicted_atoms = _group_atom_signatures(predicted, predicted_signatures)
            overlap = sum((expected_atoms & predicted_atoms).values())
            structure_equal = expected_structure == group_structure(
                predicted, predicted_signatures
            )
            modality_equal = expected.get("modality") == predicted.get("modality")
            score = overlap * 4 + int(structure_equal) * 8 + int(modality_equal)
            if score:
                candidates.append(
                    (
                        score,
                        expected_index,
                        predicted_index,
                        modality_equal,
                        structure_equal,
                    )
                )
    pairs = []
    used_expected = set()
    used_predicted = set()
    for candidate in sorted(candidates, key=lambda item: (-item[0], item[1], item[2])):
        _, expected_index, predicted_index, *_ = candidate
        if expected_index in used_expected or predicted_index in used_predicted:
            continue
        used_expected.add(expected_index)
        used_predicted.add(predicted_index)
        pairs.append(candidate)
    return pairs


def _counter_difference_items(
    source_atoms: list[dict], source: Counter, target: Counter
) -> list[dict]:
    remaining = source - target
    items = []
    for atom in source_atoms:
        signature = atom_signature(atom)
        if remaining[signature] <= 0:
            continue
        items.append(atom_descriptor(atom))
        remaining[signature] -= 1
    return items


def patch_facts(patch: dict) -> Counter:
    facts = Counter()
    for path, value in flatten_patch_paths(patch).items():
        if type(value) is list:
            for item in value:
                facts[(path, _canonical_json(item))] += 1
        else:
            facts[(path, _canonical_json(value))] += 1
    return facts


def _rate(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 1.0
    return round(numerator / denominator, 6)


def score_case(
    expected_payload: dict,
    predicted_payload: dict,
    validation: dict,
    projection: dict,
    expected_projection: dict,
) -> dict:
    expected_atoms = expected_payload.get("atoms") or []
    predicted_atoms = predicted_payload.get("atoms") or []
    expected_counter = Counter(atom_signature(atom) for atom in expected_atoms)
    predicted_counter = Counter(atom_signature(atom) for atom in predicted_atoms)
    exact_atom_count = sum((expected_counter & predicted_counter).values())
    atom_pairs = _pair_atoms(expected_atoms, predicted_atoms)

    component = {
        "denominator": len(expected_atoms),
        "kind_correct": sum(int(item[3]) for item in atom_pairs),
        "payload_correct": sum(int(item[4]) for item in atom_pairs),
        "polarity_correct": sum(int(item[5]) for item in atom_pairs),
        "temporal_correct": sum(int(item[6]) for item in atom_pairs),
    }

    expected_groups, expected_atom_signatures = _groups(expected_payload)
    predicted_groups, predicted_atom_signatures = _groups(predicted_payload)
    expected_group_counter = Counter(
        group_signature(group, expected_atom_signatures)
        for group in expected_groups
    )
    predicted_group_counter = Counter(
        group_signature(group, predicted_atom_signatures)
        for group in predicted_groups
    )
    exact_group_count = sum(
        (expected_group_counter & predicted_group_counter).values()
    )
    group_pairs = _pair_groups(expected_payload, predicted_payload)
    candidate_expected_group_indices = {
        index
        for index, group in enumerate(expected_groups)
        if group.get("modality") in {"required", "preferred"}
    }
    required_preferred_correct = sum(
        int(modality_equal)
        for _, expected_index, _, modality_equal, _ in group_pairs
        if expected_index in candidate_expected_group_indices
    )
    group_structure_correct = sum(int(item[4]) for item in group_pairs)

    individually_valid_ids = set(validation.get("individually_valid_atom_ids") or [])
    accepted_ids = set(validation.get("accepted_atom_ids") or [])
    predicted_by_id = {
        atom.get("id"): atom
        for atom in predicted_atoms
        if type(atom) is dict and type(atom.get("id")) is str
    }
    individually_valid_counter = Counter(
        atom_signature(predicted_by_id[atom_id])
        for atom_id in individually_valid_ids
        if atom_id in predicted_by_id
    )
    accepted_counter = Counter(
        atom_signature(predicted_by_id[atom_id])
        for atom_id in accepted_ids
        if atom_id in predicted_by_id
    )
    unsupported_survivor_counter = accepted_counter - expected_counter
    unsupported_survivors = _counter_difference_items(
        [
            predicted_by_id[atom_id]
            for atom_id in accepted_ids
            if atom_id in predicted_by_id
        ],
        accepted_counter,
        expected_counter,
    )
    validator_false_rejection_candidates = []
    expected_remaining = expected_counter.copy()
    for atom in predicted_atoms:
        signature = atom_signature(atom)
        if expected_remaining[signature] <= 0:
            continue
        expected_remaining[signature] -= 1
        if atom.get("id") not in individually_valid_ids:
            validator_false_rejection_candidates.append(atom_descriptor(atom))

    expected_facts = patch_facts(expected_projection.get("legacy_patch") or {})
    predicted_facts = patch_facts(projection.get("legacy_patch") or {})
    projection_fact_matches = sum((expected_facts & predicted_facts).values())
    unsafe_hard_gate_facts = [
        {"field_path": path, "value": json.loads(value)}
        for (path, value), count in (predicted_facts - expected_facts).items()
        for _ in range(count)
        if path in HARD_LEGACY_FIELD_PATHS
    ]

    expected_outcomes = expected_projection.get("group_outcomes") or []
    predicted_outcomes = projection.get("group_outcomes") or []
    accepted_group_indices = validation.get("accepted_group_indices") or []
    predicted_outcome_by_raw_group = {
        raw_index: predicted_outcomes[accepted_index]
        for accepted_index, raw_index in enumerate(accepted_group_indices)
        if accepted_index < len(predicted_outcomes)
    }
    projection_group_comparisons = 0
    projection_group_correct = 0
    projection_failures = []
    grounded_unprojected_expected = sum(
        outcome.get("state") == "grounded_unprojected"
        for outcome in expected_outcomes
    )
    grounded_unprojected_covered = 0
    used_expected_groups = set()
    for _, expected_index, predicted_index, modality_equal, structure_equal in group_pairs:
        if not (modality_equal and structure_equal):
            continue
        if predicted_index not in predicted_outcome_by_raw_group:
            continue
        if expected_index >= len(expected_outcomes):
            continue
        expected_outcome = expected_outcomes[expected_index]
        predicted_outcome = predicted_outcome_by_raw_group[predicted_index]
        projection_group_comparisons += 1
        used_expected_groups.add(expected_index)
        expected_view = {
            "state": expected_outcome.get("state"),
            "reason": expected_outcome.get("reason"),
            "field_paths": sorted(expected_outcome.get("field_paths") or []),
        }
        predicted_view = {
            "state": predicted_outcome.get("state"),
            "reason": predicted_outcome.get("reason"),
            "field_paths": sorted(predicted_outcome.get("field_paths") or []),
        }
        if predicted_view == expected_view:
            projection_group_correct += 1
        else:
            projection_failures.append(
                {
                    "expected_group_index": expected_index,
                    "predicted_group_index": predicted_index,
                    "expected": expected_view,
                    "predicted": predicted_view,
                }
            )
        if (
            expected_view["state"] == "grounded_unprojected"
            and predicted_view["state"] == "grounded_unprojected"
        ):
            grounded_unprojected_covered += 1

    raw_false_positives = _counter_difference_items(
        predicted_atoms, predicted_counter, expected_counter
    )
    raw_false_negatives = _counter_difference_items(
        expected_atoms, expected_counter, predicted_counter
    )
    validation_expected_accepted = sum(
        (accepted_counter & expected_counter).values()
    )
    projected_group_count = sum(
        item.get("state") == "projected"
        for item in predicted_outcomes
    )
    grounded_unprojected_count = sum(
        item.get("state") == "grounded_unprojected"
        for item in predicted_outcomes
    )
    conflict_count = sum(
        item.get("state") == "conflicted"
        for item in predicted_outcomes
    )

    human_ready = (
        not raw_false_positives
        and not raw_false_negatives
        and exact_group_count == len(expected_groups) == len(predicted_groups)
        and not validation.get("rejected_atoms")
        and not validation.get("rejected_groups")
        and not unsupported_survivors
        and not unsafe_hard_gate_facts
        and not projection_failures
        and predicted_facts == expected_facts
    )

    return {
        "extraction": {
            "expected_atoms": len(expected_atoms),
            "predicted_atoms": len(predicted_atoms),
            "exact_atoms": exact_atom_count,
            "atom_precision": _rate(exact_atom_count, len(predicted_atoms)),
            "atom_recall": _rate(exact_atom_count, len(expected_atoms)),
            "evidence_supported_atoms": sum(individually_valid_counter.values()),
            "evidence_supported_atom_rate": _rate(
                sum(individually_valid_counter.values()), len(predicted_atoms)
            ),
            "atom_kind_correct": component["kind_correct"],
            "atom_kind_accuracy": _rate(
                component["kind_correct"], component["denominator"]
            ),
            "payload_correct": component["payload_correct"],
            "payload_accuracy": _rate(
                component["payload_correct"], component["denominator"]
            ),
            "polarity_correct": component["polarity_correct"],
            "polarity_accuracy": _rate(
                component["polarity_correct"], component["denominator"]
            ),
            "temporal_correct": component["temporal_correct"],
            "temporal_accuracy": _rate(
                component["temporal_correct"], component["denominator"]
            ),
            "expected_groups": len(expected_groups),
            "predicted_groups": len(predicted_groups),
            "exact_groups": exact_group_count,
            "required_preferred_denominator": len(
                candidate_expected_group_indices
            ),
            "required_preferred_correct": required_preferred_correct,
            "required_preferred_accuracy": _rate(
                required_preferred_correct, len(candidate_expected_group_indices)
            ),
            "group_structure_correct": group_structure_correct,
            "group_structure_accuracy": _rate(
                group_structure_correct, len(expected_groups)
            ),
            "material_false_positives": raw_false_positives,
            "material_false_negatives": raw_false_negatives,
        },
        "validation": {
            "accepted_atoms": len(accepted_ids),
            "accepted_expected_atoms": validation_expected_accepted,
            "accepted_groups": len(validation.get("accepted_group_indices") or []),
            "rejected_atoms": copy.deepcopy(validation.get("rejected_atoms") or []),
            "rejected_groups": copy.deepcopy(validation.get("rejected_groups") or []),
            "unsupported_propositions_survived": unsupported_survivors,
            "validator_false_rejection_candidates": (
                validator_false_rejection_candidates
            ),
        },
        "projection": {
            "expected_facts": sum(expected_facts.values()),
            "predicted_facts": sum(predicted_facts.values()),
            "matching_facts": projection_fact_matches,
            "precision": _rate(
                projection_fact_matches, sum(predicted_facts.values())
            ),
            "recall": _rate(
                projection_fact_matches, sum(expected_facts.values())
            ),
            "projected_groups": projected_group_count,
            "grounded_unprojected_groups": grounded_unprojected_count,
            "conflicts": conflict_count,
            "unsafe_hard_gate_projections": unsafe_hard_gate_facts,
            "correctly_extracted_group_comparisons": projection_group_comparisons,
            "correctly_projected_groups": projection_group_correct,
            "projection_on_correct_groups_accuracy": _rate(
                projection_group_correct, projection_group_comparisons
            ),
            "projection_failures": projection_failures,
            "expected_grounded_unprojected_groups": grounded_unprojected_expected,
            "grounded_unprojected_covered": grounded_unprojected_covered,
            "grounded_unprojected_coverage": _rate(
                grounded_unprojected_covered, grounded_unprojected_expected
            ),
        },
        "human_quality_semantic_ready": human_ready,
    }


def aggregate_scores(case_scores: list[tuple[str, dict]]) -> dict:
    extraction_sums = Counter()
    validation_sums = Counter()
    projection_sums = Counter()
    false_positives = []
    false_negatives = []
    unsupported_survivors = []
    validator_candidates = []
    rejection_reasons = Counter()
    unsafe_hard_gates = []
    projection_failures = []
    ready_case_ids = []

    extraction_keys = (
        "expected_atoms",
        "predicted_atoms",
        "exact_atoms",
        "evidence_supported_atoms",
        "atom_kind_correct",
        "payload_correct",
        "polarity_correct",
        "temporal_correct",
        "expected_groups",
        "predicted_groups",
        "exact_groups",
        "required_preferred_denominator",
        "required_preferred_correct",
        "group_structure_correct",
    )
    validation_keys = ("accepted_atoms", "accepted_expected_atoms", "accepted_groups")
    projection_keys = (
        "expected_facts",
        "predicted_facts",
        "matching_facts",
        "projected_groups",
        "grounded_unprojected_groups",
        "conflicts",
        "correctly_extracted_group_comparisons",
        "correctly_projected_groups",
        "expected_grounded_unprojected_groups",
        "grounded_unprojected_covered",
    )

    for case_id, score in case_scores:
        extraction = score["extraction"]
        validation = score["validation"]
        projection = score["projection"]
        for key in extraction_keys:
            extraction_sums[key] += extraction[key]
        for key in validation_keys:
            validation_sums[key] += validation[key]
        for key in projection_keys:
            projection_sums[key] += projection[key]
        false_positives.extend(
            {"case_id": case_id, **item}
            for item in extraction["material_false_positives"]
        )
        false_negatives.extend(
            {"case_id": case_id, **item}
            for item in extraction["material_false_negatives"]
        )
        unsupported_survivors.extend(
            {"case_id": case_id, **item}
            for item in validation["unsupported_propositions_survived"]
        )
        validator_candidates.extend(
            {"case_id": case_id, **item}
            for item in validation["validator_false_rejection_candidates"]
        )
        for item in validation["rejected_atoms"]:
            rejection_reasons[item["reason"]] += 1
        for item in validation["rejected_groups"]:
            rejection_reasons[item["reason"]] += 1
        unsafe_hard_gates.extend(
            {"case_id": case_id, **item}
            for item in projection["unsafe_hard_gate_projections"]
        )
        projection_failures.extend(
            {"case_id": case_id, **item}
            for item in projection["projection_failures"]
        )
        if score["human_quality_semantic_ready"]:
            ready_case_ids.append(case_id)

    extraction = dict(extraction_sums)
    extraction.update(
        {
            "atom_precision": _rate(
                extraction_sums["exact_atoms"], extraction_sums["predicted_atoms"]
            ),
            "atom_recall": _rate(
                extraction_sums["exact_atoms"], extraction_sums["expected_atoms"]
            ),
            "evidence_supported_atom_rate": _rate(
                extraction_sums["evidence_supported_atoms"],
                extraction_sums["predicted_atoms"],
            ),
            "atom_kind_accuracy": _rate(
                extraction_sums["atom_kind_correct"],
                extraction_sums["expected_atoms"],
            ),
            "payload_accuracy": _rate(
                extraction_sums["payload_correct"],
                extraction_sums["expected_atoms"],
            ),
            "polarity_accuracy": _rate(
                extraction_sums["polarity_correct"],
                extraction_sums["expected_atoms"],
            ),
            "temporal_accuracy": _rate(
                extraction_sums["temporal_correct"],
                extraction_sums["expected_atoms"],
            ),
            "required_preferred_accuracy": _rate(
                extraction_sums["required_preferred_correct"],
                extraction_sums["required_preferred_denominator"],
            ),
            "group_structure_accuracy": _rate(
                extraction_sums["group_structure_correct"],
                extraction_sums["expected_groups"],
            ),
            "material_false_positives": false_positives,
            "material_false_negatives": false_negatives,
        }
    )
    validation = dict(validation_sums)
    validation.update(
        {
            "rejected_atoms": sum(
                len(score["validation"]["rejected_atoms"])
                for _, score in case_scores
            ),
            "rejected_groups": sum(
                len(score["validation"]["rejected_groups"])
                for _, score in case_scores
            ),
            "rejection_reasons": [
                {"reason": reason, "count": count}
                for reason, count in sorted(rejection_reasons.items())
            ],
            "unsupported_propositions_survived": unsupported_survivors,
            "validator_false_rejection_candidates": validator_candidates,
        }
    )
    projection = dict(projection_sums)
    projection.update(
        {
            "precision": _rate(
                projection_sums["matching_facts"],
                projection_sums["predicted_facts"],
            ),
            "recall": _rate(
                projection_sums["matching_facts"],
                projection_sums["expected_facts"],
            ),
            "unsafe_hard_gate_projections": unsafe_hard_gates,
            "projection_on_correct_groups_accuracy": _rate(
                projection_sums["correctly_projected_groups"],
                projection_sums["correctly_extracted_group_comparisons"],
            ),
            "projection_failures": projection_failures,
            "grounded_unprojected_coverage": _rate(
                projection_sums["grounded_unprojected_covered"],
                projection_sums["expected_grounded_unprojected_groups"],
            ),
        }
    )
    return {
        "case_count": len(case_scores),
        "extraction": extraction,
        "validation": validation,
        "projection": projection,
        "human_quality_semantic_ready_count": len(ready_case_ids),
        "human_quality_semantic_ready_ids": ready_case_ids,
    }


def regression_acceptance(aggregate: dict) -> dict:
    extraction = aggregate["extraction"]
    validation = aggregate["validation"]
    projection = aggregate["projection"]
    criteria = REGRESSION_ACCEPTANCE_CRITERIA
    checks = {
        "atom_precision": extraction["atom_precision"]
        >= criteria["minimum_atom_precision"],
        "atom_recall": extraction["atom_recall"]
        >= criteria["minimum_atom_recall"],
        "atom_kind_accuracy": extraction["atom_kind_accuracy"]
        >= criteria["minimum_atom_kind_accuracy"],
        "payload_accuracy": extraction["payload_accuracy"]
        >= criteria["minimum_payload_accuracy"],
        "polarity_accuracy": extraction["polarity_accuracy"]
        >= criteria["minimum_polarity_accuracy"],
        "temporal_accuracy": extraction["temporal_accuracy"]
        >= criteria["minimum_temporal_accuracy"],
        "required_preferred_accuracy": extraction["required_preferred_accuracy"]
        >= criteria["minimum_required_preferred_accuracy"],
        "group_structure_accuracy": extraction["group_structure_accuracy"]
        >= criteria["minimum_group_structure_accuracy"],
        "projection_precision": projection["precision"]
        >= criteria["minimum_projection_precision"],
        "projection_on_correct_groups_accuracy": projection[
            "projection_on_correct_groups_accuracy"
        ]
        >= criteria["minimum_projection_on_correct_groups_accuracy"],
        "unsupported_survivors": len(
            validation["unsupported_propositions_survived"]
        )
        <= criteria["maximum_unsupported_survivors"],
        "unsafe_hard_gate_projections": len(
            projection["unsafe_hard_gate_projections"]
        )
        <= criteria["maximum_unsafe_hard_gate_projections"],
    }
    return {
        "criteria_version": EVALUATION_CRITERIA_VERSION,
        "criteria_sha256": evaluation_criteria_sha256(),
        "criteria": copy.deepcopy(criteria),
        "checks": checks,
        "satisfactory": all(checks.values()),
    }


__all__ = [
    "EVALUATION_CRITERIA_VERSION",
    "REGRESSION_ACCEPTANCE_CRITERIA",
    "aggregate_scores",
    "atom_descriptor",
    "atom_signature",
    "evaluation_criteria_sha256",
    "group_signature",
    "group_structure",
    "model_payload_from_reviewed_contract",
    "patch_facts",
    "regression_acceptance",
    "score_case",
]

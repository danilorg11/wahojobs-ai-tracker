import copy
import json
import unittest
from collections import defaultdict
from pathlib import Path

from scripts.opportunity_semantic_grouping_eval import gold_packet_and_bindings
from wahojobs.opportunity_semantic_contract import project_legacy_compatibility
from wahojobs.opportunity_semantic_evaluation import (
    atom_signature,
    model_payload_from_reviewed_contract,
)
from wahojobs.opportunity_semantic_grouping import (
    GROUPING_VERSION,
    project_validated_grouping,
    validate_frozen_atom_output,
    validate_model_grouping,
)
from wahojobs.opportunity_semantic_grouping_evaluation import (
    GROUPING_EVALUATION_VERSION,
    aggregate_grouping_scores,
    grouping_acceptance,
    grouping_evaluation_sha256,
    score_grouping_case,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "opportunity_semantic_contract_v0.json"
FROZEN_PATH = ROOT / "exports" / "opportunity_semantic_extraction_v0_evaluation_v3.json"


class OpportunitySemanticGroupingEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        cls.frozen = json.loads(FROZEN_PATH.read_text(encoding="utf-8"))

    def test_grouping_evaluation_identity_is_frozen(self):
        self.assertEqual(
            GROUPING_EVALUATION_VERSION,
            "oe_semantic_grouping_v0_evaluation_v1",
        )
        self.assertEqual(
            grouping_evaluation_sha256(),
            "bfbbcdd886384d14a3e82c3cf756537be57d4e9e2fdd23aaa1b265ebd5316837",
        )

    def expected_grouping(self, expected_payload, atom_validation):
        ids_by_signature = defaultdict(list)
        for atom in atom_validation["validated_model_atoms"]:
            ids_by_signature[atom_signature(atom)].append(atom["id"])
        expected_to_validated = {}
        for atom in expected_payload["atoms"]:
            signature = atom_signature(atom)
            self.assertTrue(ids_by_signature[signature])
            expected_to_validated[atom["id"]] = ids_by_signature[signature].pop(0)
        expected_atoms = {atom["id"]: atom for atom in expected_payload["atoms"]}
        groups = []
        for expected_group in expected_payload["constraint_groups"]:
            atom_ids = [
                atom_id
                for alternative in expected_group["any_of"]
                for atom_id in alternative["all_of"]
            ]
            common_evidence = None
            for atom_id in atom_ids:
                evidence = {
                    (item["alias"], item["quote"])
                    for item in expected_atoms[atom_id]["evidence"]
                }
                common_evidence = (
                    evidence
                    if common_evidence is None
                    else common_evidence & evidence
                )
            self.assertTrue(common_evidence)
            alias, quote = sorted(common_evidence)[0]
            groups.append(
                {
                    "modality": expected_group["modality"],
                    "any_of": [
                        {
                            "all_of": [
                                expected_to_validated[atom_id]
                                for atom_id in alternative["all_of"]
                            ]
                        }
                        for alternative in expected_group["any_of"]
                    ],
                    "evidence": [{"alias": alias, "quote": quote}],
                }
            )
        return {
            "grouping_version": GROUPING_VERSION,
            "constraint_groups": groups,
            "ungrouped_atom_ids": [],
        }

    def test_reviewed_groupings_pass_two_stage_server_and_frozen_gates(self):
        case_scores = []
        for case in self.fixture["cases"]:
            case_id = case["id"]
            packet, bindings = gold_packet_and_bindings(case)
            frozen_payload = self.frozen["gold"]["cases"][case_id][
                "raw_extraction"
            ]
            atom_validation = validate_frozen_atom_output(
                frozen_payload, packet, bindings
            )
            expected_payload = model_payload_from_reviewed_contract(
                case["contract"]
            )
            grouping_payload = self.expected_grouping(
                expected_payload, atom_validation
            )
            group_validation = validate_model_grouping(
                grouping_payload, atom_validation, packet, bindings
            )
            projection = project_validated_grouping(group_validation)
            expected_projection = project_legacy_compatibility(
                case["contract"], case["evidence_catalog"]
            )
            score = score_grouping_case(
                expected_payload,
                frozen_payload,
                atom_validation,
                grouping_payload,
                group_validation,
                projection,
                expected_projection,
            )
            self.assertTrue(score["two_stage_human_ready"], case_id)
            case_scores.append((case_id, score))

        aggregate = aggregate_grouping_scores(case_scores)
        acceptance = grouping_acceptance(aggregate)
        self.assertTrue(acceptance["satisfactory"])
        self.assertEqual(aggregate["atom_stage"]["frozen_atoms"], 33)
        self.assertEqual(aggregate["atom_stage"]["validated_atoms"], 32)
        self.assertEqual(
            aggregate["atom_stage"]["validated_expected_atom_recall"], 1.0
        )
        self.assertEqual(aggregate["grouping"]["exact_groups"], 23)
        self.assertEqual(
            aggregate["grouping"]["and_or_structure_accuracy"], 1.0
        )
        self.assertEqual(
            aggregate["grouping"]["independent_group_accuracy"], 1.0
        )
        self.assertEqual(aggregate["projection"]["conflicts"], 2)
        self.assertEqual(
            aggregate["projection"]["unsafe_hard_gate_projections"], []
        )

    def test_combined_conflict_is_not_counted_as_independent_group_success(self):
        case = next(
            item
            for item in self.fixture["cases"]
            if item["id"] == "conflicting_polarity_control"
        )
        packet, bindings = gold_packet_and_bindings(case)
        frozen_payload = self.frozen["gold"]["cases"][case["id"]][
            "raw_extraction"
        ]
        atom_validation = validate_frozen_atom_output(
            frozen_payload, packet, bindings
        )
        grouping_payload = {
            "grouping_version": GROUPING_VERSION,
            "constraint_groups": [
                {
                    "modality": "required",
                    "any_of": [{"all_of": ["a1", "a2"]}],
                    "evidence": [
                        {
                            "alias": "review:control:python-required",
                            "quote": "Python programming capability is required.",
                        },
                        {
                            "alias": "review:control:python-negated",
                            "quote": "Candidates must not have Python programming capability.",
                        },
                    ],
                }
            ],
            "ungrouped_atom_ids": [],
        }
        group_validation = validate_model_grouping(
            grouping_payload, atom_validation, packet, bindings
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
            grouping_payload,
            group_validation,
            projection,
            expected_projection,
        )
        self.assertEqual(score["grouping"]["independent_group_accuracy"], 0.0)
        self.assertEqual(score["grouping"]["ungrouped_expected_atoms"], 2)
        self.assertEqual(len(score["grouping"]["rejected_groups"]), 1)


if __name__ == "__main__":
    unittest.main()

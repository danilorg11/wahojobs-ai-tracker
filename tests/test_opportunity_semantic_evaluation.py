import copy
import json
import unittest
from pathlib import Path

from wahojobs.opportunity_semantic_evaluation import (
    aggregate_scores,
    evaluation_criteria_sha256,
    model_payload_from_reviewed_contract,
    regression_acceptance,
    score_case,
)
from wahojobs.opportunity_semantic_extraction import (
    EXTRACTION_CONTRACT_VERSION,
    project_validated_extraction,
    validate_model_extraction,
)


FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "opportunity_semantic_contract_v0.json"
)


def packet_and_bindings(case: dict):
    packet = {"evidence_blocks": []}
    bindings = []
    for source in case["evidence_catalog"]:
        packet["evidence_blocks"].append(
            {
                "evidence_block_id": source["id"],
                "authority_class": "accepted_body_evidence",
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
    return packet, bindings


def evaluate(case: dict, payload: dict) -> dict:
    packet, bindings = packet_and_bindings(case)
    validation = validate_model_extraction(payload, packet, bindings)
    projection = project_validated_extraction(validation)
    expected_validation = validate_model_extraction(
        model_payload_from_reviewed_contract(case["contract"]),
        packet,
        bindings,
    )
    expected_projection = project_validated_extraction(expected_validation)
    return score_case(
        model_payload_from_reviewed_contract(case["contract"]),
        payload,
        validation,
        projection,
        expected_projection,
    )


class OpportunitySemanticEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        cls.cases = {case["id"]: case for case in fixture["cases"]}

    def test_evaluation_criteria_identity_is_frozen(self):
        self.assertEqual(
            evaluation_criteria_sha256(),
            "68d87f35373dd00ff83062d5e313e98f02e725acfc379d6eacf5539fcc30f7e0",
        )

    def test_perfect_reviewed_gold_scores_each_stage_perfectly(self):
        scored = []
        for case_id, case in self.cases.items():
            payload = model_payload_from_reviewed_contract(case["contract"])
            scored.append((case_id, evaluate(case, payload)))

        aggregate = aggregate_scores(scored)
        acceptance = regression_acceptance(aggregate)

        self.assertEqual(aggregate["extraction"]["atom_precision"], 1.0)
        self.assertEqual(aggregate["extraction"]["atom_recall"], 1.0)
        self.assertEqual(
            aggregate["extraction"]["group_structure_accuracy"], 1.0
        )
        self.assertEqual(
            aggregate["validation"]["unsupported_propositions_survived"], []
        )
        self.assertEqual(aggregate["projection"]["precision"], 1.0)
        self.assertEqual(aggregate["projection"]["recall"], 1.0)
        self.assertEqual(
            aggregate["projection"]["projection_on_correct_groups_accuracy"],
            1.0,
        )
        self.assertTrue(acceptance["satisfactory"])

    def test_omitted_registration_is_extraction_failure_not_projection_failure(self):
        case = self.cases["contractor_registration"]
        silence = {
            "extraction_version": EXTRACTION_CONTRACT_VERSION,
            "atoms": [],
            "constraint_groups": [],
        }

        score = evaluate(case, silence)

        self.assertEqual(score["extraction"]["material_false_positives"], [])
        self.assertEqual(len(score["extraction"]["material_false_negatives"]), 1)
        self.assertEqual(score["projection"]["projection_failures"], [])
        self.assertEqual(score["projection"]["unsafe_hard_gate_projections"], [])

    def test_correct_grounded_unprojected_registration_is_not_extraction_error(self):
        case = self.cases["contractor_registration"]
        payload = model_payload_from_reviewed_contract(case["contract"])

        score = evaluate(case, payload)

        self.assertEqual(score["extraction"]["atom_precision"], 1.0)
        self.assertEqual(score["extraction"]["atom_recall"], 1.0)
        self.assertEqual(score["projection"]["grounded_unprojected_groups"], 1)
        self.assertEqual(score["projection"]["grounded_unprojected_coverage"], 1.0)
        self.assertEqual(score["projection"]["projection_failures"], [])

    def test_split_or_is_group_extraction_error_and_unsafe_projection(self):
        case = self.cases["sports_background_or_interest"]
        payload = model_payload_from_reviewed_contract(case["contract"])
        payload["constraint_groups"] = [
            {
                "modality": "required",
                "any_of": [{"all_of": ["sports_background"]}],
            },
            {
                "modality": "required",
                "any_of": [{"all_of": ["sports_interest"]}],
            },
        ]

        score = evaluate(case, payload)

        self.assertEqual(score["extraction"]["atom_precision"], 1.0)
        self.assertEqual(score["extraction"]["atom_recall"], 1.0)
        self.assertLess(score["extraction"]["group_structure_accuracy"], 1.0)
        self.assertEqual(
            len(score["projection"]["unsafe_hard_gate_projections"]), 1
        )
        self.assertFalse(score["human_quality_semantic_ready"])


if __name__ == "__main__":
    unittest.main()

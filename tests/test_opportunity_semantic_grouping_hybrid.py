import copy
import json
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from scripts.opportunity_semantic_grouping_eval import gold_packet_and_bindings
from scripts.opportunity_semantic_grouping_hybrid_replay import build_report
from wahojobs.opportunity_semantic_grouping import (
    HYBRID_GROUPING_STRATEGY_VERSION,
    SAFE_SINGLETON_COMPLETER_VERSION,
    complete_safe_singletons,
    validate_frozen_atom_output,
    validate_model_grouping,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "opportunity_semantic_contract_v0.json"
FROZEN_PATH = ROOT / "exports" / "opportunity_semantic_extraction_v0_evaluation_v3.json"
GROUPING_V1_PATH = ROOT / "exports" / "opportunity_semantic_grouping_v0_evaluation.json"


class OpportunitySemanticGroupingHybridTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        cls.cases = {case["id"]: case for case in fixture["cases"]}
        cls.frozen = json.loads(FROZEN_PATH.read_text(encoding="utf-8"))
        cls.grouping_v1 = json.loads(
            GROUPING_V1_PATH.read_text(encoding="utf-8")
        )

    def context(self, case_id, grouping_payload=None):
        case = self.cases[case_id]
        packet, bindings = gold_packet_and_bindings(case)
        frozen_payload = self.frozen["gold"]["cases"][case_id][
            "raw_extraction"
        ]
        atom_validation = validate_frozen_atom_output(
            frozen_payload, packet, bindings
        )
        if grouping_payload is None:
            grouping_payload = self.grouping_v1["gold"]["cases"][case_id][
                "raw_grouping"
            ]
        group_validation = validate_model_grouping(
            copy.deepcopy(grouping_payload),
            atom_validation,
            packet,
            bindings,
        )
        return packet, bindings, atom_validation, group_validation

    def test_hybrid_strategy_identities_are_versioned(self):
        self.assertEqual(
            HYBRID_GROUPING_STRATEGY_VERSION,
            "oe_semantic_grouping_v0_hybrid_safe_singleton_v1",
        )
        self.assertEqual(
            SAFE_SINGLETON_COMPLETER_VERSION,
            "oe_semantic_grouping_v0_safe_singleton_completer_v1",
        )

    def test_explicit_standalone_required_capability_is_completed_without_mutation(self):
        packet, bindings, atoms, grouping = self.context(
            "latex_capability_without_math_domain"
        )
        original_atom = copy.deepcopy(atoms["validated_atoms"][0])
        completed = complete_safe_singletons(grouping, atoms, packet, bindings)
        self.assertEqual(
            completed["completed_singletons"][0]["modality"], "required"
        )
        self.assertEqual(
            completed["accepted_model_groups"],
            [{"modality": "required", "any_of": [{"all_of": ["a1"]}]}],
        )
        self.assertEqual(completed["accepted_contract"]["atoms"], [original_atom])
        self.assertEqual(completed["ungrouped_atom_ids"], [])
        self.assertTrue(completed["atom_accounting"]["complete_and_exact"])

        evidence = completed["completed_singletons"][0]["evidence"]
        source = next(
            item
            for item in completed["accepted_evidence_catalog"]
            if item["id"] == evidence["source_id"]
        )
        self.assertIn(evidence["quote"], source["text"])
        final_reference = completed["accepted_contract"]["atoms"][0]["evidence"][0]
        self.assertEqual(
            source["text"][final_reference["start"] : final_reference["end"]],
            final_reference["quote"],
        )

    def test_explicit_standalone_preferred_fact_is_completed_as_preferred(self):
        case_id = "required_capability_preferred_experience_control"
        stored = copy.deepcopy(
            self.grouping_v1["gold"]["cases"][case_id]["raw_grouping"]
        )
        preferred = next(
            group for group in stored["constraint_groups"]
            if group["modality"] == "preferred"
        )
        preferred_ids = [
            atom_id
            for alternative in preferred["any_of"]
            for atom_id in alternative["all_of"]
        ]
        stored["constraint_groups"] = [
            group for group in stored["constraint_groups"]
            if group["modality"] != "preferred"
        ]
        stored["ungrouped_atom_ids"] = preferred_ids
        packet, bindings, atoms, grouping = self.context(case_id, stored)
        completed = complete_safe_singletons(grouping, atoms, packet, bindings)
        self.assertEqual(
            [item["modality"] for item in completed["completed_singletons"]],
            ["preferred"],
        )
        self.assertEqual(
            completed["accepted_group_origins"],
            ["model", "server_safe_singleton"],
        )

    def test_ambiguous_singleton_modality_remains_ungrouped(self):
        packet, bindings, atoms, grouping = self.context(
            "latex_capability_without_math_domain"
        )
        ambiguous = copy.deepcopy(atoms)
        ambiguous["allowed_modalities_by_atom_id"]["a1"] = [
            "required",
            "preferred",
        ]

        def support_every_modality(group, atom_ids, atoms_by_id, evidence):
            return evidence[0], None

        with mock.patch(
            "wahojobs.opportunity_semantic_grouping._relation_support",
            side_effect=support_every_modality,
        ):
            completed = complete_safe_singletons(
                grouping, ambiguous, packet, bindings
            )
        self.assertEqual(completed["completed_singletons"], [])
        self.assertEqual(completed["ungrouped_atom_ids"], ["a1"])
        self.assertEqual(
            completed["safe_singleton_not_completed"],
            [{"atom_id": "a1", "reason": "ambiguous_singleton_modality"}],
        )

    def test_or_alternatives_are_not_hardened_into_singletons(self):
        case_id = "sports_background_or_interest"
        validated_ids = self.grouping_v1["gold"]["cases"][case_id][
            "atom_validation"
        ]["validated_atom_ids"]
        payload = {
            "grouping_version": "oe_semantic_grouping_v0",
            "constraint_groups": [],
            "ungrouped_atom_ids": validated_ids,
        }
        packet, bindings, atoms, grouping = self.context(case_id, payload)
        completed = complete_safe_singletons(grouping, atoms, packet, bindings)
        self.assertEqual(completed["completed_singletons"], [])
        self.assertEqual(
            completed["ungrouped_atom_ids"], sorted(validated_ids)
        )
        self.assertTrue(
            all(
                item["reason"] == "relation_evidence_requires_non_singleton_logic"
                for item in completed["safe_singleton_not_completed"]
            )
        )

    def test_already_grouped_atoms_are_untouched_and_not_duplicated(self):
        packet, bindings, atoms, grouping = self.context(
            "contractor_registration"
        )
        before = copy.deepcopy(grouping["accepted_contract"])
        grouping["model_ungrouped_atom_ids"] = ["a1"]
        completed = complete_safe_singletons(grouping, atoms, packet, bindings)
        self.assertEqual(completed["completed_singletons"], [])
        self.assertEqual(completed["accepted_contract"], before)
        occurrences = sum(
            atom_id == "a1"
            for group in completed["accepted_model_groups"]
            for alternative in group["any_of"]
            for atom_id in alternative["all_of"]
        )
        self.assertEqual(occurrences, 1)

    def test_rejected_atoms_are_never_available_for_completion(self):
        packet, bindings, atoms, grouping = self.context(
            "bilingual_all_required_control"
        )
        completed = complete_safe_singletons(grouping, atoms, packet, bindings)
        self.assertEqual(
            [item["atom_id"] for item in atoms["rejected_atoms"]], ["a3"]
        )
        self.assertNotIn("a3", completed["accepted_atom_ids"])
        self.assertNotIn(
            "a3", [item["atom_id"] for item in completed["completed_singletons"]]
        )

    def test_frozen_hybrid_replay_passes_every_unchanged_gate_without_provider_calls(self):
        report = build_report(
            Namespace(
                gold=FIXTURE_PATH,
                frozen_extraction=FROZEN_PATH,
                stored_grouping=GROUPING_V1_PATH,
            )
        )
        self.assertEqual(report["provider_calls"], 0)
        self.assertEqual(report["atom_extraction_provider_calls"], 0)
        self.assertEqual(report["grouping_provider_calls"], 0)
        self.assertTrue(report["gold"]["acceptance"]["satisfactory"])
        self.assertEqual(report["gold"]["model_created_group_count"], 22)
        self.assertEqual(report["gold"]["server_completed_singleton_count"], 1)
        self.assertEqual(report["gold"]["still_ungrouped_atoms"], [])

        aggregate = report["gold"]["aggregate"]
        self.assertEqual(aggregate["grouping"]["exact_groups"], 23)
        self.assertEqual(aggregate["grouping"]["and_or_structure_accuracy"], 1.0)
        self.assertEqual(aggregate["grouping"]["modality_accuracy"], 1.0)
        self.assertEqual(aggregate["grouping"]["group_evidence_support_rate"], 1.0)
        self.assertEqual(aggregate["projection"]["precision"], 1.0)
        self.assertEqual(aggregate["projection"]["recall"], 1.0)
        self.assertEqual(aggregate["projection"]["grounded_unprojected_coverage"], 1.0)
        self.assertEqual(aggregate["projection"]["conflicts"], 2)
        self.assertEqual(aggregate["projection"]["unsafe_hard_gate_projections"], [])

        controls = report["gold"]["cases"]
        for case_id in (
            "latex_capability_without_math_domain",
            "generic_qa_fact_checking",
            "genuine_software_testing",
            "bilingual_all_required_control",
            "conflicting_polarity_control",
            "contractor_registration",
        ):
            self.assertTrue(controls[case_id]["score"]["two_stage_human_ready"])
        contractor_atom = controls["contractor_registration"][
            "hybrid_validation"
        ]["accepted_contract"]["atoms"][0]
        self.assertEqual(contractor_atom["temporal"], "unspecified")


if __name__ == "__main__":
    unittest.main()

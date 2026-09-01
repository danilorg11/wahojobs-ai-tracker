import copy
import json
import unittest
from pathlib import Path

from scripts.opportunity_semantic_authority_replay import build_report
from wahojobs.matching.foundation_contracts import (
    DETERMINISTIC_ELIGIBILITY_CRITERIA_V1 as MATCHING_ELIGIBILITY_CRITERIA,
)
from wahojobs.opportunity_enrichment import SEMANTIC_INPUT_VERSION
from wahojobs.opportunity_semantic_authority import (
    AUTHORITY_POLICY_VERSION,
    DANGEROUS_DOWNSTREAM_INTERFACES,
    DANGEROUS_SEMANTIC_COMPATIBILITY_FIELD_PATHS,
    DETERMINISTIC_ELIGIBILITY_CRITERIA_V1,
    DETERMINISTIC_HARD_AUTHORITY_TYPE,
    OpportunitySemanticAuthorityError,
    SEMANTIC_AUTHORITY_TYPE,
    SEMANTIC_MATCHING_PACKET_VERSION,
    authority_can_create_hard_eligibility_failure,
    build_semantic_matching_packet,
    build_semantic_matching_packet_from_staging,
    canonical_sha256,
    derive_server_variant_relationships,
    hard_authoritative_objective_fact,
    semantic_compatibility_projection_authority,
    semantic_non_exclusionary_authority,
    validate_semantic_matching_packet,
)
from wahojobs.opportunity_semantic_contract import (
    flatten_patch_paths,
    project_legacy_compatibility,
)
from wahojobs.opportunity_semantic_staging import (
    construct_relations,
    replay_case,
    stage_provisional_atoms,
)


ROOT = Path(__file__).resolve().parents[1]
GOLD_PATH = ROOT / "tests" / "fixtures" / "opportunity_semantic_contract_v0.json"
RAW_PATH = (
    ROOT
    / "tests"
    / "fixtures"
    / "opportunity_semantic_staging_v0_fresh_canary_raw.json"
)
REVIEW_PATH = (
    ROOT
    / "tests"
    / "fixtures"
    / "opportunity_semantic_staging_v0_reviewed_canary.json"
)


def objective_fact(field_path="attributes.work_arrangement.eligible_countries"):
    return {
        "field_path": field_path,
        "value": "Brazil",
        "knowledge_state": "known_value",
        "variant_refs": ["fixture:objective"],
        "evidence": [
            {
                "evidence_block_id": "Eobjective",
                "source_refs": ["fixture:objective"],
                "authority_refs": ["fixture:accepted"],
                "evidence_text": "Brazil - Remote",
                "basis": "deterministic_parse",
                "confidence": "high",
            }
        ],
    }


class OpportunitySemanticAuthorityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gold = json.loads(GOLD_PATH.read_text(encoding="utf-8"))
        cls.gold_by_id = {case["id"]: case for case in cls.gold["cases"]}
        cls.raw = json.loads(RAW_PATH.read_text(encoding="utf-8"))
        cls.review = json.loads(REVIEW_PATH.read_text(encoding="utf-8"))
        cls.report = build_report()

    def gold_packet(self, case_id):
        case = self.gold_by_id[case_id]
        semantic_bundle = {
            "contract": case["contract"],
            "evidence_catalog": case["evidence_catalog"],
        }
        return build_semantic_matching_packet(
            case["contract"],
            case["evidence_catalog"],
            canonical_ref=f"fixture:{case_id}",
            known_variant_refs=[f"fixture:{case_id}"],
            semantic_input_version=self.gold["fixture_version"],
            semantic_input_sha256=canonical_sha256(semantic_bundle),
            source_packet_sha256=canonical_sha256(case["evidence_catalog"]),
        )

    def regression_packet(self, case_id):
        case = self.raw["cases"][case_id]
        replay = replay_case(case, self.review["cases"][case_id])
        relationships = derive_server_variant_relationships(
            case["source_packet"],
            case["accepted_evidence_bindings"],
            replay["staging"]["accepted_evidence_catalog"],
        )
        packet = build_semantic_matching_packet_from_staging(
            replay["staging"],
            replay["relations"],
            canonical_ref=f"canonical_opportunity:{case['canonical_opportunity_id']}",
            known_variant_refs=sorted(
                item["variant_ref"] for item in case["source_packet"]["variants"]
            ),
            semantic_input_version=SEMANTIC_INPUT_VERSION,
            semantic_input_sha256=case["semantic_input_sha256"],
            source_packet_sha256=case["source_packet_sha256"],
            semantic_extraction_version=case["raw_extraction"]["extraction_version"],
            semantic_grouping_version=case["raw_grouping"]["grouping_version"],
            variant_relationships=relationships,
        )
        return packet, replay

    def test_authority_type_is_closed_and_semantic_tampering_is_rejected(self):
        packet = self.gold_packet("genuine_historical_experience")
        self.assertEqual(packet["packet_version"], SEMANTIC_MATCHING_PACKET_VERSION)
        self.assertEqual(packet["authority_policy_version"], AUTHORITY_POLICY_VERSION)
        self.assertFalse(authority_can_create_hard_eligibility_failure(packet))
        for item in [*packet["propositions"], *packet["groups"]]:
            self.assertEqual(item["authority"]["authority_type"], SEMANTIC_AUTHORITY_TYPE)
            self.assertFalse(authority_can_create_hard_eligibility_failure(item))

        tampered = copy.deepcopy(packet)
        tampered["groups"][0]["authority"]["hard_eligibility_authorized"] = True
        tampered["packet_sha256"] = canonical_sha256(
            {key: value for key, value in tampered.items() if key != "packet_sha256"}
        )
        with self.assertRaises(OpportunitySemanticAuthorityError):
            validate_semantic_matching_packet(tampered)

        invalid_authority = semantic_non_exclusionary_authority()
        invalid_authority["authority_type"] = "verified_required"
        with self.assertRaises(OpportunitySemanticAuthorityError):
            authority_can_create_hard_eligibility_failure(invalid_authority)

    def test_required_is_semantic_modality_not_hard_gate_authority(self):
        packet = self.gold_packet("advanced_degree_or_professional_standing")
        group = packet["groups"][0]
        self.assertEqual(group["modality"], "required")
        self.assertEqual(
            group["logic"]["any_of"],
            [
                {"all_of": ["advanced_degree"]},
                {"all_of": ["industry_standing"]},
            ],
        )
        self.assertFalse(group["authority"]["hard_eligibility_authorized"])
        self.assertFalse(group["authority"]["candidate_exclusion_authorized"])
        self.assertFalse(authority_can_create_hard_eligibility_failure(group))

    def test_gold_replay_proves_zero_semantic_origin_hard_exclusions(self):
        report = self.report
        self.assertTrue(report["acceptance"]["passed"])
        self.assertEqual(report["gold"]["case_count"], 20)
        self.assertEqual(report["gold"]["proposition_count"], 32)
        self.assertEqual(report["gold"]["group_count"], 23)
        self.assertEqual(
            report["gold"]["group_modalities"],
            {"descriptive": 2, "preferred": 2, "required": 19},
        )
        self.assertEqual(report["gold"]["semantic_hard_exclusion_count"], 0)
        self.assertEqual(report["gold"]["required_group_hard_failure_count"], 0)
        self.assertTrue(report["gold"]["dnf_logic_preserved"])
        self.assertTrue(report["gold"]["legacy_gold_projection_preserved"])

    def test_opened_16_case_replay_preserves_relations_and_zero_exclusions(self):
        metrics = self.report["opened_16_case_regression"]
        self.assertEqual(metrics["case_count"], 16)
        self.assertEqual(metrics["proposition_count"], 80)
        self.assertEqual(metrics["group_count"], 49)
        self.assertEqual(metrics["complete_group_count"], 35)
        self.assertEqual(metrics["incomplete_or_unresolved_group_count"], 14)
        self.assertEqual(metrics["source_branch_count"], 77)
        self.assertEqual(metrics["unresolved_source_branch_count"], 14)
        self.assertEqual(metrics["raw_source_value_count"], 249)
        self.assertTrue(metrics["raw_source_values_preserved"])
        self.assertTrue(metrics["relation_and_dnf_preserved"])
        self.assertEqual(metrics["logic_weakening_cases"], [])
        self.assertEqual(metrics["projected_from_incomplete_relation_ids"], [])
        self.assertEqual(metrics["legacy_unsafe_hard_gates"], [])
        self.assertEqual(metrics["semantic_hard_exclusion_count"], 0)
        self.assertEqual(metrics["required_group_hard_failure_count"], 0)
        self.assertEqual(metrics["unresolved_or_unsupported_hard_gate_count"], 0)

    def test_unresolved_or_and_and_are_retained_without_subtraction(self):
        packet, replay = self.regression_packet("1117")
        groups = {item["group_id"]: item for item in packet["groups"]}
        required_and = groups["r000"]
        education_or = groups["r001"]
        self.assertEqual(required_and["relation_state"], "invalid_proposal")
        self.assertEqual(
            required_and["logic"]["any_of"],
            [{"all_of": ["a1", "a2", "a3", "a10", "a11"]}],
        )
        self.assertEqual(education_or["relation_state"], "grounded_incomplete")
        self.assertEqual(len(education_or["logic"]["any_of"]), 6)
        physics = [
            branch
            for branch in education_or["source_branches"]
            if branch["coverage_state"] == "unresolved"
            and any(
                item["raw_value"].casefold() == "physics"
                for item in branch["source_values"]
            )
        ]
        self.assertEqual(len(physics), 2)
        self.assertEqual(
            [group["raw_proposal"] for group in packet["groups"]],
            [relation["proposal"] for relation in replay["relations"]["relations"]],
        )
        self.assertTrue(
            all(
                not authority_can_create_hard_eligibility_failure(group)
                for group in packet["groups"]
            )
        )

    def test_raw_unsupported_values_survive_but_cannot_become_hard_gates(self):
        packet, _replay = self.regression_packet("1117")
        values = {
            item["raw_value"].casefold()
            for proposition in packet["propositions"]
            for item in proposition["normalization"]["source_values"]
        }
        self.assertIn("indonesian", values)
        self.assertIn("physics", values)
        unresolved = [
            proposition
            for proposition in packet["propositions"]
            if proposition["normalization"]["status"] != "resolved"
        ]
        self.assertTrue(unresolved)
        self.assertTrue(all(item["normalized_value"] is None for item in unresolved))
        self.assertTrue(
            all(
                not authority_can_create_hard_eligibility_failure(item)
                for item in unresolved
            )
        )

    def test_verifier_observations_and_legacy_assurances_have_no_authority_effect(self):
        packet, _replay = self.regression_packet("1422")
        legacy_hard = [
            item
            for item in packet["propositions"]
            if item["experimental_observation"]["legacy_assurance"]
            == "hard_projection_authorized"
        ]
        self.assertTrue(legacy_hard)
        self.assertTrue(
            all(
                item["experimental_observation"]["authority_effect"] == "none"
                and not authority_can_create_hard_eligibility_failure(item)
                for item in legacy_hard
            )
        )
        self.assertEqual(packet["construction_policy"]["verifier_dependency"], "none")
        self.assertEqual(packet["construction_policy"]["verifier_authority_effect"], "none")
        self.assertFalse(self.report["verifier_dependency"]["required"])
        self.assertEqual(self.report["verifier_dependency"]["verifier_imports"], [])
        self.assertEqual(
            self.report["opened_16_case_regression"][
                "legacy_hard_assurance_promotions"
            ],
            0,
        )

    def test_packet_constructs_with_pending_support_and_no_verifier_result(self):
        case = self.raw["cases"]["1068"]
        staging = stage_provisional_atoms(
            case["raw_extraction"],
            case["source_packet"],
            case["accepted_evidence_bindings"],
        )
        relations = construct_relations(staging, case["raw_grouping"])
        relationships = derive_server_variant_relationships(
            case["source_packet"],
            case["accepted_evidence_bindings"],
            staging["accepted_evidence_catalog"],
        )
        packet = build_semantic_matching_packet_from_staging(
            staging,
            relations,
            canonical_ref=f"canonical_opportunity:{case['canonical_opportunity_id']}",
            known_variant_refs=sorted(
                item["variant_ref"] for item in case["source_packet"]["variants"]
            ),
            semantic_input_version=SEMANTIC_INPUT_VERSION,
            semantic_input_sha256=case["semantic_input_sha256"],
            source_packet_sha256=case["source_packet_sha256"],
            semantic_extraction_version=case["raw_extraction"]["extraction_version"],
            semantic_grouping_version=case["raw_grouping"]["grouping_version"],
            variant_relationships=relationships,
        )
        self.assertTrue(packet["propositions"])
        self.assertTrue(
            all(
                item["status"]["semantic_support"] == "pending"
                and item["experimental_observation"]["semantic_decision"]
                == "pending"
                and not authority_can_create_hard_eligibility_failure(item)
                for item in packet["propositions"]
            )
        )
        proof = self.report["verifier_dependency"]["pending_packet_proof"]
        self.assertTrue(proof["packet_constructed"])
        self.assertFalse(proof["verifier_result_supplied"])
        self.assertEqual(proof["hard_exclusion_count"], 0)

    def test_objective_hard_fact_is_separate_explicit_and_criterion_scoped(self):
        self.assertEqual(
            dict(DETERMINISTIC_ELIGIBILITY_CRITERIA_V1),
            dict(MATCHING_ELIGIBILITY_CRITERIA),
        )
        hard_fact = hard_authoritative_objective_fact(
            objective_fact(), criterion_id="eligibility.location"
        )
        self.assertEqual(
            hard_fact["authority"]["authority_type"],
            DETERMINISTIC_HARD_AUTHORITY_TYPE,
        )
        self.assertTrue(authority_can_create_hard_eligibility_failure(hard_fact))
        self.assertNotEqual(
            hard_fact["authority"]["authority_type"], SEMANTIC_AUTHORITY_TYPE
        )

        with self.assertRaises(OpportunitySemanticAuthorityError):
            hard_authoritative_objective_fact(
                objective_fact("attributes.requirements.education.minimum_level"),
                criterion_id="eligibility.location",
            )
        semantic_evidence = objective_fact()
        semantic_evidence["evidence"][0]["basis"] = "llm_extraction"
        with self.assertRaises(OpportunitySemanticAuthorityError):
            hard_authoritative_objective_fact(
                semantic_evidence, criterion_id="eligibility.location"
            )

    def test_legacy_compatibility_patch_is_machine_readable_diagnostic_only(self):
        case = self.gold_by_id[
            "required_capability_preferred_experience_control"
        ]
        projection = project_legacy_compatibility(
            case["contract"], case["evidence_catalog"]
        )
        paths = flatten_patch_paths(projection["legacy_patch"])
        self.assertIn("attributes.requirements.skills_required", paths)
        self.assertIn(
            "attributes.requirements.skills_required",
            DANGEROUS_SEMANTIC_COMPATIBILITY_FIELD_PATHS,
        )
        self.assertEqual(
            projection["compatibility_authority"],
            semantic_compatibility_projection_authority(),
        )
        self.assertFalse(
            projection["compatibility_authority"]["hard_eligibility_authorized"]
        )
        self.assertEqual(
            projection["compatibility_authority"]["authority_effect"], "none"
        )
        self.assertIn("semantic_contract.legacy_patch", DANGEROUS_DOWNSTREAM_INTERFACES)

    def test_responsibilities_and_candidate_profile_use_native_soft_signals(self):
        case = self.gold_by_id["genuine_software_testing"]
        source_id = case["evidence_catalog"][0]["id"]
        semantic_bundle = {
            "contract": case["contract"],
            "evidence_catalog": case["evidence_catalog"],
        }
        packet = build_semantic_matching_packet(
            case["contract"],
            case["evidence_catalog"],
            canonical_ref="fixture:genuine_software_testing",
            known_variant_refs=["fixture:genuine_software_testing"],
            semantic_input_version=self.gold["fixture_version"],
            semantic_input_sha256=canonical_sha256(semantic_bundle),
            source_packet_sha256=canonical_sha256(case["evidence_catalog"]),
            descriptive_signals=[
                {
                    "signal_id": "candidate_profile",
                    "kind": "candidate_profile",
                    "value": "A software tester who can evaluate platform behavior.",
                    "evidence_source_ids": [source_id],
                },
                {
                    "signal_id": "responsibility_1",
                    "kind": "responsibility",
                    "value": "Test software and report defects.",
                    "evidence_source_ids": [source_id],
                },
            ],
        )
        self.assertEqual(len(packet["descriptive_signals"]), 2)
        self.assertEqual(packet["accounting"]["descriptive_signal_count"], 2)
        self.assertEqual(
            {item["kind"] for item in packet["descriptive_signals"]},
            {"candidate_profile", "responsibility"},
        )
        self.assertTrue(
            all(
                item["evidence_source_ids"] == [source_id]
                and item["authority"]["authority_type"] == SEMANTIC_AUTHORITY_TYPE
                and not authority_can_create_hard_eligibility_failure(item)
                for item in packet["descriptive_signals"]
            )
        )

    def test_variant_links_are_server_derived_and_version_identities_are_pinned(self):
        packet, _replay = self.regression_packet("1068")
        self.assertEqual(
            packet["identities"]["semantic_input_version"], SEMANTIC_INPUT_VERSION
        )
        self.assertEqual(
            packet["identities"]["semantic_input_sha256"],
            self.raw["cases"]["1068"]["semantic_input_sha256"],
        )
        self.assertEqual(
            packet["identities"]["source_packet_sha256"],
            self.raw["cases"]["1068"]["source_packet_sha256"],
        )
        self.assertTrue(packet["propositions"])
        for proposition in packet["propositions"]:
            self.assertTrue(proposition["variant_relationships"])
            self.assertTrue(
                all(
                    item["derivation"]
                    == "server_authenticated_evidence_binding"
                    and item["variant_refs"]
                    for item in proposition["variant_relationships"]
                )
            )

    def test_offline_replay_declares_no_provider_network_database_or_verifier_run(self):
        self.assertEqual(
            self.report["offline_controls"],
            {
                "provider_calls": 0,
                "network_calls": 0,
                "database_reads": 0,
                "database_writes": 0,
                "verifier_experiments_run": 0,
            },
        )
        self.assertTrue(self.report["artifact_preservation"]["fixture_preserved"])
        self.assertTrue(self.report["artifact_preservation"]["database_preserved"])
        self.assertEqual(
            self.report["verdict"], "SEMANTIC AUTHORITY BOUNDARY PASSED"
        )


if __name__ == "__main__":
    unittest.main()

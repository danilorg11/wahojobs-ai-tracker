import copy
import hashlib
import json
import unittest
from pathlib import Path

from scripts.opportunity_semantic_staging_replay import (
    PREREGISTRATION_SHA256,
    RAW_ARTIFACT_SHA256,
    build_report,
    file_sha256,
)
from wahojobs.opportunity_semantic_contract import (
    SemanticContractValidationError,
    project_verified_legacy_compatibility,
)
from wahojobs.opportunity_semantic_extraction import (
    SemanticExtractionValidationError,
)
from wahojobs.opportunity_semantic_staging import (
    ASSURANCE_STATES,
    NORMALIZATION_STATUSES,
    PROVISIONAL_STATUSES,
    RELATION_STATES,
    SEMANTIC_DECISIONS,
    SemanticStagingValidationError,
    apply_semantic_verification,
    construct_relations,
    finalize_verified_relations,
    replay_case,
    role_activity_composition_supported,
    semantic_verification_request,
    stage_provisional_atoms,
    validate_semantic_verification,
    verification_from_reviewed_labels,
)


ROOT = Path(__file__).resolve().parents[1]
RAW_PATH = (
    ROOT
    / "tests"
    / "fixtures"
    / "opportunity_semantic_staging_v0_fresh_canary_raw.json"
)
PREREGISTRATION_PATH = (
    ROOT
    / "tests"
    / "fixtures"
    / "opportunity_semantic_extraction_v0_fresh_canary.json"
)
REVIEW_PATH = (
    ROOT
    / "tests"
    / "fixtures"
    / "opportunity_semantic_staging_v0_reviewed_canary.json"
)


def packet_and_bindings(text: str):
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    provenance = {
        "review_id": "oe-semantic-staging-v0-tests",
        "case_id": "staging-control",
    }
    packet = {
        "company": {"name": "Reviewed fixture"},
        "canonical": {"canonical_title": "staging-control"},
        "variants": [],
        "evidence_blocks": [
            {
                "evidence_block_id": "accepted:control",
                "source_ref": "accepted:control",
                "source_refs": ["accepted:control"],
                "variant_refs": ["fixture:staging-control"],
                "authority_refs": [],
                "kind": "body_paragraph",
                "authority_class": "accepted_body_evidence",
                "label": "reviewed staging evidence",
                "content": text,
            }
        ],
    }
    bindings = [
        {
            "alias": "accepted:control",
            "authority": "accepted_review_checkpoint",
            "text": text,
            "text_sha256": digest,
            "provenance": provenance,
        }
    ]
    return packet, bindings


def role_activity_model(text: str, activity: str, artifact: str) -> dict:
    return {
        "extraction_version": "oe_semantic_extraction_v0",
        "atoms": [
            {
                "id": "a1",
                "kind": "role_activity",
                "typed_payload": {"activity": activity, "artifact": artifact},
                "polarity": "affirmed",
                "temporal": "unspecified",
                "evidence": [{"alias": "accepted:control", "quote": text}],
            }
        ],
        "constraint_groups": [
            {
                "modality": "descriptive",
                "any_of": [{"all_of": ["a1"]}],
            }
        ],
    }


class OpportunitySemanticStagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = json.loads(RAW_PATH.read_text(encoding="utf-8"))
        cls.review = json.loads(REVIEW_PATH.read_text(encoding="utf-8"))
        cls.report = build_report()

    def replay(self, case_id: str) -> dict:
        return replay_case(
            self.raw["cases"][case_id], self.review["cases"][case_id]
        )

    def test_frozen_artifact_hashes_and_offline_preservation_are_exact(self):
        self.assertEqual(file_sha256(RAW_PATH), RAW_ARTIFACT_SHA256)
        self.assertEqual(
            file_sha256(PREREGISTRATION_PATH), PREREGISTRATION_SHA256
        )
        self.assertTrue(self.report["raw_artifact_preservation"]["fixture_preserved"])
        self.assertTrue(self.report["raw_artifact_preservation"]["database_preserved"])
        self.assertEqual(
            self.report["aggregate"]["offline_controls"],
            {
                "provider_calls": 0,
                "network_calls": 0,
                "database_reads": 0,
                "database_writes": 0,
            },
        )

    def test_every_offline_acceptance_gate_passes_on_the_opened_16_cases(self):
        report = self.report
        self.assertTrue(report["acceptance"]["passed"])
        self.assertTrue(all(report["acceptance"]["checks"].values()))
        aggregate = report["aggregate"]
        self.assertEqual(aggregate["case_count"], 16)
        self.assertEqual(aggregate["provisional_atoms"]["proposed"], 80)
        self.assertEqual(aggregate["provisional_atoms"]["accounted"], 80)
        self.assertEqual(aggregate["provisional_atoms"]["accounting_rate"], 1.0)
        self.assertEqual(
            aggregate["semantic_verification"],
            {"entails": 72, "contradicts": 2, "not_established": 6},
        )
        self.assertEqual(
            aggregate["legacy_semantic_validator"],
            {
                "reviewed_supported_accepted": 25,
                "reviewed_supported_rejected": 47,
                "reviewed_unsupported_accepted": 2,
                "reviewed_unsupported_rejected": 6,
            },
        )
        self.assertEqual(
            aggregate["supported_proposal_retention"]["rate"], 1.0
        )
        self.assertEqual(
            aggregate["relations"]["source_branch_accounting_rate"], 1.0
        )
        self.assertEqual(aggregate["relations"]["source_branches"], 77)
        self.assertEqual(aggregate["relations"]["logic_weakening_cases"], [])
        self.assertEqual(
            aggregate["relations"]["projected_from_incomplete_relation_ids"], []
        )
        self.assertEqual(aggregate["projection"]["conditional_precision"], 1.0)
        self.assertEqual(aggregate["projection"]["projected_groups"], 10)
        self.assertEqual(aggregate["projection"]["unsafe_hard_gates"], [])
        self.assertEqual(
            aggregate["projection"]["reviewed_unsupported_projected_atom_ids"],
            [],
        )
        observations = aggregate["relations"]["frozen_grouping_observations"]
        self.assertEqual(sum(observations.values()), 49)
        self.assertNotIn("not_available", observations)

    def test_verifier_contract_is_bounded_and_identity_immutable(self):
        case = self.raw["cases"]["1422"]
        staging = stage_provisional_atoms(
            case["raw_extraction"],
            case["source_packet"],
            case["accepted_evidence_bindings"],
        )
        request = semantic_verification_request(staging)
        self.assertEqual(
            set(request), {"verification_version", "atoms"}
        )
        self.assertEqual(
            set(request["atoms"][0]),
            {
                "ledger_id",
                "atom_sha256",
                "subject",
                "kind",
                "typed_payload",
                "polarity",
                "temporal",
                "evidence",
            },
        )
        serialized = json.dumps(request)
        for forbidden in (
            "constraint_groups",
            "field_path",
            "eligibility",
            "variant_scope",
            "normalized_value",
        ):
            self.assertNotIn(forbidden, serialized)

        verification = verification_from_reviewed_labels(
            staging, self.review["cases"]["1422"]
        )
        tampered = copy.deepcopy(verification)
        tampered["decisions"][0]["typed_payload"] = {"invented": True}
        with self.assertRaises(SemanticStagingValidationError):
            validate_semantic_verification(staging, tampered)
        tampered = copy.deepcopy(verification)
        tampered["decisions"][0]["atom_sha256"] = "0" * 64
        with self.assertRaises(SemanticStagingValidationError):
            validate_semantic_verification(staging, tampered)

    def test_lexical_failure_abstains_while_reviewed_support_is_retained(self):
        result = self.replay("1422")
        fact = next(
            item
            for item in result["staging"]["provisional_atoms"]
            if item["proposal_id"] == "a4"
        )
        self.assertEqual(fact["lexical_fast_path"]["decision"], "abstain")
        self.assertEqual(
            fact["semantic_verification"]["decision"], "entails"
        )
        self.assertEqual(fact["assurance"], "hard_projection_authorized")
        self.assertIn(
            fact["assurance"], ASSURANCE_STATES
        )

    def test_authentication_rejects_changed_accepted_text_before_semantics(self):
        case = copy.deepcopy(self.raw["cases"]["1422"])
        case["accepted_evidence_bindings"][0]["text"] += " changed"
        with self.assertRaises(SemanticExtractionValidationError):
            stage_provisional_atoms(
                case["raw_extraction"],
                case["source_packet"],
                case["accepted_evidence_bindings"],
            )

    def test_italian_indonesian_chemistry_and_physics_remain_raw(self):
        italian = self.replay("1116")
        italian_atom = next(
            item
            for item in italian["staging"]["provisional_atoms"]
            if item["proposal_id"] == "a3"
        )
        self.assertEqual(italian_atom["atom"]["typed_payload"]["language"], "english")
        self.assertEqual(italian_atom["normalization"]["status"], "unmapped")
        self.assertIn(
            "Italian",
            [
                item["raw_value"]
                for item in italian_atom["normalization"]["source_values"]
            ],
        )
        self.assertNotIn(
            "a3", [atom["id"] for atom in italian["finalization"]["final_contract"]["atoms"]]
        )

        indonesian = self.replay("1117")
        values = {
            item["raw_value"].casefold()
            for atom in indonesian["staging"]["provisional_atoms"]
            for item in atom["normalization"]["source_values"]
        }
        self.assertIn("indonesian", values)
        self.assertIn("physics", values)
        chemistry = self.replay("1519")
        chemistry_values = {
            item["raw_value"].casefold()
            for atom in chemistry["staging"]["provisional_atoms"]
            for item in atom["normalization"]["source_values"]
        }
        self.assertIn("chemistry", chemistry_values)
        self.assertEqual(
            chemistry["metrics"][
                "finalized_source_value_or_qualifier_violations"
            ],
            [],
        )

    def test_unresolved_or_and_and_branches_never_disappear(self):
        result = self.replay("1117")
        required_and = result["relations"]["relations"][0]
        education_or = result["relations"]["relations"][1]
        self.assertEqual(required_and["state"], "invalid_proposal")
        self.assertEqual(
            required_and["proposal"]["any_of"],
            [
                {
                    "all_of": ["a1", "a2", "a3", "a10", "a11"]
                }
            ],
        )
        self.assertEqual(education_or["state"], "grounded_incomplete")
        self.assertEqual(len(education_or["proposal"]["any_of"]), 6)
        unresolved_physics = [
            branch
            for branch in education_or["source_branches"]
            if branch["coverage_state"] == "unresolved"
            and any(
                value["raw_value"].casefold() == "physics"
                for value in branch["source_values"]
            )
        ]
        self.assertEqual(len(unresolved_physics), 2)
        self.assertEqual(result["finalization"]["final_contract"]["atoms"], [])
        self.assertEqual(result["metrics"]["projected_from_incomplete_relation_ids"], [])
        self.assertFalse(result["metrics"]["logic_weakening_detected"])

    def test_unsupported_member_invalidates_whole_relation_without_subtraction(self):
        result = self.replay("1443")
        unsupported = next(
            item
            for item in result["staging"]["provisional_atoms"]
            if item["proposal_id"] == "a1"
        )
        self.assertEqual(
            unsupported["semantic_verification"]["decision"], "not_established"
        )
        self.assertEqual(unsupported["assurance"], "rejected")
        self.assertEqual(result["relations"]["relations"][0]["state"], "invalid_proposal")
        self.assertNotIn(
            "a1", [atom["id"] for atom in result["finalization"]["final_contract"]["atoms"]]
        )

    def test_fact_checking_ai_output_composes_but_generic_qa_is_not_testing(self):
        result = self.replay("1422")
        payloads = [
            atom["typed_payload"]
            for atom in result["finalization"]["final_contract"]["atoms"]
            if atom["kind"] == "role_activity"
        ]
        self.assertIn(
            {"activity": "fact_checking", "artifact": "ai_output"}, payloads
        )

        generic = (
            "Perform generic quality assurance and fact-checking of generated tasks."
        )
        packet, bindings = packet_and_bindings(generic)
        model = role_activity_model(generic, "software_testing", "software")
        staging = stage_provisional_atoms(model, packet, bindings)
        verification = verification_from_reviewed_labels(
            staging,
            {"entails": ["a1"], "contradicts": [], "not_established": []},
        )
        verified = apply_semantic_verification(staging, verification)
        relations = construct_relations(verified)
        finalization = finalize_verified_relations(verified, relations)
        self.assertFalse(role_activity_composition_supported(verified["provisional_atoms"][0]["atom"]))
        self.assertEqual(relations["relations"][0]["state"], "grounded_incomplete")
        self.assertEqual(finalization["final_contract"]["atoms"], [])
        self.assertEqual(finalization["projection"]["legacy_patch"], {})

    def test_hard_projection_requires_stronger_explicit_assurance(self):
        result = self.replay("1422")
        finalization = result["finalization"]
        assurances = copy.deepcopy(finalization["verified_atom_assurances"])
        first = next(iter(assurances))
        assurances[first]["projection_assurance"] = "semantic_matching_only"
        with self.assertRaises(SemanticContractValidationError):
            project_verified_legacy_compatibility(
                finalization["final_contract"],
                finalization["accepted_evidence_catalog"],
                assurances,
                finalization["authoritative_modalities"],
            )

    def test_structural_and_state_vocabularies_are_closed(self):
        self.assertEqual(
            RELATION_STATES,
            {
                "complete_verified",
                "grounded_incomplete",
                "invalid_proposal",
                "unrepresentable_relation",
            },
        )
        self.assertEqual(
            SEMANTIC_DECISIONS,
            {"entails", "contradicts", "not_established"},
        )
        report = self.report["aggregate"]
        self.assertLessEqual(
            set(report["normalization"]["atom_statuses"]),
            NORMALIZATION_STATUSES,
        )
        for case_id in self.raw["cases"]:
            result = self.replay(case_id)
            self.assertLessEqual(
                {item["status"] for item in result["staging"]["provisional_atoms"]},
                PROVISIONAL_STATUSES,
            )
            self.assertLessEqual(
                {item["state"] for item in result["relations"]["relations"]},
                RELATION_STATES,
            )

    def test_oversized_relation_is_retained_as_unrepresentable(self):
        text = "Verify factual accuracy in the model output."
        packet, bindings = packet_and_bindings(text)
        model = role_activity_model(text, "fact_checking", "ai_output")
        model["constraint_groups"][0]["any_of"] = [
            {"all_of": ["a1"]} for _ in range(9)
        ]
        staging = stage_provisional_atoms(model, packet, bindings)
        verification = verification_from_reviewed_labels(
            staging,
            {"entails": ["a1"], "contradicts": [], "not_established": []},
        )
        verified = apply_semantic_verification(staging, verification)
        relations = construct_relations(verified)
        self.assertEqual(
            relations["relations"][0]["state"], "unrepresentable_relation"
        )
        self.assertTrue(verified["atom_accounting"]["complete_and_exact"])


if __name__ == "__main__":
    unittest.main()

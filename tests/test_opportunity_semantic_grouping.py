import json
import unittest
from pathlib import Path

from scripts.opportunity_semantic_grouping_eval import gold_packet_and_bindings
from wahojobs.opportunity_semantic_grouping import (
    GROUPING_MODEL,
    GROUPING_PROMPT_VERSION,
    GROUPING_REASONING_EFFORT,
    GROUPING_SCHEMA_VERSION,
    GROUPING_VALIDATOR_VERSION,
    GROUPING_VERSION,
    OpenAISemanticGroupingClient,
    SemanticGroupingValidationError,
    grouping_prompt_sha256,
    grouping_schema_sha256,
    project_validated_grouping,
    semantic_grouping_prompt,
    semantic_grouping_schema,
    validate_frozen_atom_output,
    validate_model_grouping,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "opportunity_semantic_contract_v0.json"
FROZEN_PATH = ROOT / "exports" / "opportunity_semantic_extraction_v0_evaluation_v3.json"


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response


class OpportunitySemanticGroupingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        cls.cases = {case["id"]: case for case in cls.fixture["cases"]}
        cls.frozen = json.loads(FROZEN_PATH.read_text(encoding="utf-8"))

    def frozen_atom_validation(self, case_id):
        case = self.cases[case_id]
        packet, bindings = gold_packet_and_bindings(case)
        raw = self.frozen["gold"]["cases"][case_id]["raw_extraction"]
        validation = validate_frozen_atom_output(raw, packet, bindings)
        return case, packet, bindings, raw, validation

    def test_grouping_identities_are_frozen(self):
        self.assertEqual(GROUPING_VERSION, "oe_semantic_grouping_v0")
        self.assertEqual(GROUPING_MODEL, "gpt-5.6-terra")
        self.assertEqual(GROUPING_REASONING_EFFORT, "low")
        self.assertEqual(GROUPING_PROMPT_VERSION, "oe_semantic_grouping_v0_prompt_v2")
        self.assertEqual(GROUPING_SCHEMA_VERSION, "oe_semantic_grouping_v0_schema_v1")
        self.assertEqual(
            GROUPING_VALIDATOR_VERSION,
            "oe_semantic_grouping_v0_validator_v2",
        )
        self.assertEqual(
            grouping_prompt_sha256(),
            "1ce15d443b4c0d9bbd8fab99003545f44999140d6a8dd28888a066563e564bba",
        )
        self.assertEqual(
            grouping_schema_sha256(),
            "48819e9887548ef0e446c818e4b923ac34b3c5533672b3e1597546507db14700",
        )

    def test_grouping_schema_cannot_create_or_rewrite_atoms(self):
        schema = semantic_grouping_schema(["a1", "a2"], ["E1"])
        self.assertEqual(schema["$id"], GROUPING_SCHEMA_VERSION)
        self.assertEqual(
            set(schema["properties"]),
            {"grouping_version", "constraint_groups", "ungrouped_atom_ids"},
        )
        serialized = json.dumps(schema, sort_keys=True)
        for forbidden in (
            '"atoms"',
            '"kind"',
            '"typed_payload"',
            '"polarity"',
            '"temporal"',
            '"field_path"',
            '"eligibility"',
            '"variant_scope"',
            '"legacy_patch"',
        ):
            self.assertNotIn(forbidden, serialized)
        atom_enum = schema["properties"]["constraint_groups"]["items"][
            "properties"
        ]["any_of"]["items"]["properties"]["all_of"]["items"]["enum"]
        self.assertEqual(atom_enum, ["a1", "a2"])

    def test_prompt_preserves_grouping_authority_boundaries(self):
        prompt = semantic_grouping_prompt()
        for requirement in (
            "may not add, remove, rewrite, infer, or repair an atom",
            "Every validated atom ID must appear exactly once",
            "never strengthen preferred to required",
            "Independent statements remain independent groups",
            "opposite polarities",
            "must not be put into one conjunction",
            "exact contiguous accepted-evidence quotes",
            "corresponding singleton group",
            "with no omissions, overlap, or duplicate assignment",
            "ungrouped_atom_ids instead of guessing",
        ):
            self.assertIn(requirement, prompt)

    def test_responses_request_is_grouping_only_terra_low_and_store_false(self):
        provider_payload = {
            "grouping_version": GROUPING_VERSION,
            "constraint_groups": [],
            "ungrouped_atom_ids": ["a1"],
        }
        response = FakeResponse(
            {
                "id": "resp_group",
                "model": GROUPING_MODEL,
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": json.dumps(provider_payload)}
                        ],
                    }
                ],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                    "input_tokens_details": {"cached_tokens": 5},
                    "output_tokens_details": {"reasoning_tokens": 7},
                },
            }
        )
        session = FakeSession(response)
        client = OpenAISemanticGroupingClient("secret", session=session)
        packet = {
            "company": {"name": "not sent"},
            "evidence_blocks": [
                {
                    "evidence_block_id": "E1",
                    "authority_class": "accepted_body_evidence",
                    "content": "Python programming capability is required.",
                }
            ],
        }
        atom_validation = {
            "grouping_atoms": [
                {
                    "id": "a1",
                    "kind": "capability",
                    "typed_payload": {"capability": "python_programming"},
                    "polarity": "affirmed",
                    "temporal": "unspecified",
                    "evidence": [
                        {
                            "alias": "E1",
                            "quote": "Python programming capability is required.",
                        }
                    ],
                    "allowed_modalities": ["required"],
                }
            ]
        }
        result = client.group(packet, atom_validation)
        self.assertEqual(result.payload, provider_payload)
        request = session.calls[0][1]["json"]
        self.assertEqual(request["model"], GROUPING_MODEL)
        self.assertFalse(request["store"])
        self.assertEqual(request["reasoning"], {"effort": "low"})
        self.assertTrue(request["text"]["format"]["strict"])
        user_input = json.loads(request["input"][1]["content"][0]["text"])
        self.assertNotIn("company", user_input)
        self.assertEqual(user_input["validated_atoms"][0]["id"], "a1")

    def test_frozen_atoms_are_validated_before_grouping_without_poisoning(self):
        raw_count = validated_count = rejected_count = 0
        rejected = []
        for case in self.fixture["cases"]:
            _, _, _, raw, validation = self.frozen_atom_validation(case["id"])
            raw_count += len(raw["atoms"])
            validated_count += len(validation["validated_atom_ids"])
            rejected_count += len(validation["rejected_atoms"])
            rejected.extend(
                (case["id"], item["atom_id"])
                for item in validation["rejected_atoms"]
            )
        self.assertEqual(raw_count, 33)
        self.assertEqual(validated_count, 32)
        self.assertEqual(rejected_count, 1)
        self.assertEqual(rejected, [("bilingual_all_required_control", "a3")])

    def test_bilingual_valid_atoms_survive_and_form_required_and_group(self):
        _, packet, bindings, _, atoms = self.frozen_atom_validation(
            "bilingual_all_required_control"
        )
        payload = {
            "grouping_version": GROUPING_VERSION,
            "constraint_groups": [
                {
                    "modality": "required",
                    "any_of": [{"all_of": ["a1", "a2"]}],
                    "evidence": [
                        {
                            "alias": "accepted:438:korean-japanese",
                            "quote": "Fluency in both Korean and Japanese is required, with strong reading, writing, and communication skills in each.",
                        }
                    ],
                }
            ],
            "ungrouped_atom_ids": [],
        }
        result = validate_model_grouping(payload, atoms, packet, bindings)
        self.assertEqual(result["accepted_atom_ids"], ["a1", "a2"])
        self.assertEqual(result["ungrouped_atom_ids"], [])
        self.assertEqual(result["rejected_groups"], [])
        self.assertEqual([item["atom_id"] for item in result["rejected_atoms"]], ["a3"])

    def test_combined_opposing_statements_are_rejected_without_losing_atoms(self):
        _, packet, bindings, _, atoms = self.frozen_atom_validation(
            "conflicting_polarity_control"
        )
        payload = {
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
        result = validate_model_grouping(payload, atoms, packet, bindings)
        self.assertEqual(result["accepted_atom_ids"], [])
        self.assertEqual(result["ungrouped_atom_ids"], ["a1", "a2"])
        self.assertEqual(result["rejected_atoms"], [])
        self.assertEqual(len(result["rejected_groups"]), 1)
        self.assertIn("no_single_relation_span", result["rejected_groups"][0]["reason"])

    def test_independent_opposing_groups_produce_reviewed_conflict(self):
        _, packet, bindings, _, atoms = self.frozen_atom_validation(
            "conflicting_polarity_control"
        )
        payload = {
            "grouping_version": GROUPING_VERSION,
            "constraint_groups": [
                {
                    "modality": "required",
                    "any_of": [{"all_of": ["a1"]}],
                    "evidence": [
                        {
                            "alias": "review:control:python-required",
                            "quote": "Python programming capability is required.",
                        }
                    ],
                },
                {
                    "modality": "required",
                    "any_of": [{"all_of": ["a2"]}],
                    "evidence": [
                        {
                            "alias": "review:control:python-negated",
                            "quote": "Candidates must not have Python programming capability.",
                        }
                    ],
                },
            ],
            "ungrouped_atom_ids": [],
        }
        result = validate_model_grouping(payload, atoms, packet, bindings)
        projection = project_validated_grouping(result)
        self.assertEqual(len(result["accepted_model_groups"]), 2)
        self.assertEqual(
            sum(item["state"] == "conflicted" for item in projection["group_outcomes"]),
            2,
        )

    def test_contractor_temporal_cannot_be_changed_by_grouping(self):
        _, packet, bindings, _, atoms = self.frozen_atom_validation(
            "contractor_registration"
        )
        payload = {
            "grouping_version": GROUPING_VERSION,
            "constraint_groups": [
                {
                    "modality": "required",
                    "any_of": [{"all_of": ["a1"]}],
                    "evidence": [
                        {
                            "alias": "accepted:641:contractor-registration",
                            "quote": "Global Compliance: Candidates must be properly registered to operate as independent contractors in their country of residence.",
                        }
                    ],
                }
            ],
            "ungrouped_atom_ids": [],
        }
        result = validate_model_grouping(payload, atoms, packet, bindings)
        self.assertEqual(
            result["accepted_contract"]["atoms"][0]["temporal"], "unspecified"
        )

    def test_preferred_atom_cannot_be_strengthened_to_required(self):
        _, packet, bindings, _, atoms = self.frozen_atom_validation(
            "required_capability_preferred_experience_control"
        )
        preferred_id = next(
            atom_id
            for atom_id, modalities in atoms[
                "allowed_modalities_by_atom_id"
            ].items()
            if modalities == ["preferred"]
        )
        preferred_atom = next(
            atom
            for atom in atoms["grouping_atoms"]
            if atom["id"] == preferred_id
        )
        payload = {
            "grouping_version": GROUPING_VERSION,
            "constraint_groups": [
                {
                    "modality": "required",
                    "any_of": [{"all_of": [preferred_id]}],
                    "evidence": preferred_atom["evidence"],
                }
            ],
            "ungrouped_atom_ids": [
                atom_id
                for atom_id in atoms["validated_atom_ids"]
                if atom_id != preferred_id
            ],
        }
        result = validate_model_grouping(payload, atoms, packet, bindings)
        self.assertEqual(result["accepted_group_indices"], [])
        self.assertIn(preferred_id, result["ungrouped_atom_ids"])
        self.assertIn(
            "modality is not supported", result["rejected_groups"][0]["reason"]
        )

    def test_rejected_atom_cannot_be_resurrected_by_grouping(self):
        _, packet, bindings, _, atoms = self.frozen_atom_validation(
            "bilingual_all_required_control"
        )
        payload = {
            "grouping_version": GROUPING_VERSION,
            "constraint_groups": [
                {
                    "modality": "required",
                    "any_of": [{"all_of": ["a1", "a2", "a3"]}],
                    "evidence": [
                        {
                            "alias": "accepted:438:korean-japanese",
                            "quote": "Fluency in both Korean and Japanese is required, with strong reading, writing, and communication skills in each.",
                        }
                    ],
                }
            ],
            "ungrouped_atom_ids": [],
        }
        result = validate_model_grouping(payload, atoms, packet, bindings)
        self.assertEqual(result["accepted_atom_ids"], [])
        self.assertEqual(result["ungrouped_atom_ids"], ["a1", "a2"])
        self.assertEqual(len(result["rejected_groups"]), 1)
        self.assertIn("unknown atom", result["rejected_groups"][0]["reason"])

    def test_ungrouped_valid_atom_remains_grounded_and_unprojected(self):
        _, packet, bindings, _, atoms = self.frozen_atom_validation(
            "contractor_registration"
        )
        payload = {
            "grouping_version": GROUPING_VERSION,
            "constraint_groups": [],
            "ungrouped_atom_ids": ["a1"],
        }
        result = validate_model_grouping(payload, atoms, packet, bindings)
        projection = project_validated_grouping(result)
        self.assertEqual(result["ungrouped_atom_ids"], ["a1"])
        self.assertEqual(result["rejected_atoms"], [])
        self.assertEqual(result["rejected_groups"], [])
        self.assertEqual(projection["legacy_patch"], {})

    def test_validated_atom_cannot_be_silently_omitted(self):
        _, packet, bindings, _, atoms = self.frozen_atom_validation(
            "contractor_registration"
        )
        payload = {
            "grouping_version": GROUPING_VERSION,
            "constraint_groups": [],
            "ungrouped_atom_ids": [],
        }
        with self.assertRaisesRegex(
            SemanticGroupingValidationError,
            "grouping omits validated atoms: a1",
        ):
            validate_model_grouping(payload, atoms, packet, bindings)

    def test_validated_atom_cannot_be_grouped_and_ungrouped(self):
        _, packet, bindings, _, atoms = self.frozen_atom_validation(
            "contractor_registration"
        )
        payload = {
            "grouping_version": GROUPING_VERSION,
            "constraint_groups": [
                {
                    "modality": "required",
                    "any_of": [{"all_of": ["a1"]}],
                    "evidence": [
                        {
                            "alias": "accepted:641:contractor-registration",
                            "quote": "Global Compliance: Candidates must be properly registered to operate as independent contractors in their country of residence.",
                        }
                    ],
                }
            ],
            "ungrouped_atom_ids": ["a1"],
        }
        with self.assertRaisesRegex(
            SemanticGroupingValidationError,
            "accounts for validated atoms more than once: a1",
        ):
            validate_model_grouping(payload, atoms, packet, bindings)

    def test_singleton_group_is_explicitly_and_exactly_accounted(self):
        _, packet, bindings, _, atoms = self.frozen_atom_validation(
            "contractor_registration"
        )
        payload = {
            "grouping_version": GROUPING_VERSION,
            "constraint_groups": [
                {
                    "modality": "required",
                    "any_of": [{"all_of": ["a1"]}],
                    "evidence": [
                        {
                            "alias": "accepted:641:contractor-registration",
                            "quote": "Global Compliance: Candidates must be properly registered to operate as independent contractors in their country of residence.",
                        }
                    ],
                }
            ],
            "ungrouped_atom_ids": [],
        }
        result = validate_model_grouping(payload, atoms, packet, bindings)
        self.assertEqual(
            result["atom_accounting"],
            {
                "validated_atom_ids": ["a1"],
                "grouped_atom_ids": ["a1"],
                "explicitly_ungrouped_atom_ids": [],
                "complete_and_exact": True,
            },
        )


if __name__ == "__main__":
    unittest.main()

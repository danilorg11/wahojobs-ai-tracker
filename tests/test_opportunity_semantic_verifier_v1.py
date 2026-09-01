import copy
import json
import tempfile
import unittest
from pathlib import Path

from scripts.opportunity_semantic_verifier_v1_eval import (
    FROZEN_GATES,
    build_manifest,
    build_population,
    evaluate_records,
    select_hard_challenges,
)
from wahojobs.opportunity_semantic_verifier import (
    SemanticVerifierError,
)
from wahojobs.opportunity_semantic_verifier_v1 import (
    CHALLENGE_MODEL,
    PRIMARY_MODEL,
    OpenAIExactClaimVerifierClient,
    challenge_prompt,
    decision_schema,
    exact_atom_claim,
    hard_challenge_input,
    model_configuration_sha256,
    primary_atom_input,
    primary_prompt,
    primary_relation_input,
    prompt_sha256,
    result_record,
    schema_sha256,
    validate_exact_input,
    validate_exact_result_record,
    validate_provider_output,
)


class _FakeResponse:
    status_code = 200

    def __init__(self, decision, model):
        self.decision = decision
        self.model = model

    def json(self):
        return {
            "id": "resp_exact_verifier_test",
            "model": self.model,
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps({"decision": self.decision}),
                        }
                    ],
                }
            ],
            "usage": {
                "input_tokens": 90,
                "input_tokens_details": {
                    "cached_tokens": 10,
                    "cache_write_tokens": 20,
                },
                "output_tokens": 24,
                "output_tokens_details": {"reasoning_tokens": 16},
                "total_tokens": 114,
            },
        }


class _FakeSession:
    def __init__(self, decision, model):
        self.decision = decision
        self.model = model
        self.calls = []

    def post(self, url, *, headers, json, timeout):
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "json": copy.deepcopy(json),
                "timeout": timeout,
            }
        )
        return _FakeResponse(self.decision, self.model)


def _record(packet: dict, decision: str) -> dict:
    role = packet["verifier_role"]
    model = PRIMARY_MODEL if role == "primary" else CHALLENGE_MODEL
    session = _FakeSession(decision, model)
    client = OpenAIExactClaimVerifierClient(
        "test-key", role=role, session=session
    )
    return result_record(client.verify(packet))


class OpportunitySemanticVerifierV1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, _, _, _, cls.reference, population = build_population()
        cls.cases, cls.tasks = population

    def test_frozen_prompt_schema_configuration_and_population_identities(self):
        self.assertEqual(
            prompt_sha256("primary"),
            "a2b4bae6c60dbfcfda49b2665e5d8d719795aeef79d1fbae9f7ea35df8b20680",
        )
        self.assertEqual(
            prompt_sha256("hard_challenge"),
            "14348aeaa7902d8ea1972ce06ae49cbc07405dcc5fde72e3b18c074435d6dde5",
        )
        self.assertEqual(
            schema_sha256(),
            "5c0a37380cf6ba5e1ba84e0e0d72f18b9af3b0c300ff6a5ff87ac870e47bdd9e",
        )
        self.assertEqual(
            model_configuration_sha256("primary"),
            "c7445df5ebb5825b12353c4b65c021d4dd1ce6d4a78c635a4894ae701f34687c",
        )
        self.assertEqual(
            model_configuration_sha256("hard_challenge"),
            "536da0c53c6699cff10ba56b9307d6ea90f702daf7e73b30f3e40d9917a9eb26",
        )
        self.assertEqual(set(decision_schema()["properties"]), {"decision"})
        self.assertEqual(
            decision_schema()["properties"]["decision"]["enum"],
            ["contradicts", "entails", "not_established"],
        )
        manifest = build_manifest()
        self.assertEqual(manifest["population"]["case_count"], 16)
        self.assertEqual(manifest["population"]["primary_atom_claim_count"], 80)
        self.assertEqual(
            manifest["population"]["primary_relation_claim_count"], 49
        )
        self.assertEqual(manifest["population"]["primary_provider_call_count"], 129)
        self.assertEqual(manifest["population"]["source_branch_count"], 77)
        self.assertEqual(
            manifest["population"]["hard_candidate_universe_count"], 24
        )
        self.assertEqual(
            manifest["population"]["primary_task_identity_set_sha256"],
            "eed6217a092a62697f844c0bb604c0d4d7d288ea41ad1e39f8b91bc368ec4720",
        )
        self.assertEqual(
            manifest["population"]["hard_candidate_universe_sha256"],
            "240ecca48425003f5cca8df00c307026503b04e9072cad14c43126f65f2c922c",
        )
        self.assertEqual(
            manifest["review_reference"]["hard_candidate_reference_counts"],
            {
                "reviewed_supported": 19,
                "reviewed_unsupported_or_incomplete": 5,
                "total": 24,
            },
        )
        self.assertEqual(manifest["frozen_acceptance_gates"], FROZEN_GATES)

    def test_direct_relation_reference_is_explicit_complete_and_not_derived(self):
        rows = {
            f"{case_id}:{relation_id}": row
            for case_id, relations in self.reference["relation_reference"][
                "cases"
            ].items()
            for relation_id, row in relations.items()
        }
        self.assertEqual(len(rows), 49)
        self.assertEqual(
            self.reference["relation_reference"]["counts"],
            {
                "entails": 33,
                "contradicts": 2,
                "not_established": 14,
                "total": 49,
            },
        )
        self.assertEqual(rows["1068:r003"]["decision"], "entails")
        for identity in (
            "1319:r000",
            "1335:r001",
            "1341:r001",
            "1443:r001",
        ):
            self.assertEqual(rows[identity]["class"], "narrowed_conjunction")
            self.assertEqual(rows[identity]["decision"], "not_established")
        self.assertIn(
            "not derived from provider results",
            self.reference["relation_reference"]["basis"],
        )

    def test_atom_serializer_contains_complete_meaning_and_raw_not_normalized(self):
        item = next(
            atom
            for atom in self.cases["1116"]["staged"]["provisional_atoms"]
            if atom["proposal_id"] == "a3"
        )
        claim = exact_atom_claim(item)
        self.assertEqual(
            set(claim),
            {
                "subject",
                "kind",
                "typed_payload",
                "raw_source_values",
                "polarity",
                "temporal",
            },
        )
        self.assertEqual(claim["typed_payload"]["language"], "english")
        self.assertIn(
            "Italian", [value["raw_value"] for value in claim["raw_source_values"]]
        )
        serialized = json.dumps(claim)
        self.assertNotIn("normalized_value", serialized)
        self.assertNotIn("normalization_status", serialized)

    def test_relation_serializer_inlines_exact_atoms_logic_modality_and_all_branches(self):
        data = self.cases["1117"]
        relation = next(
            item
            for item in data["deterministic_relations"]["relations"]
            if item["relation_id"] == "r001"
        )
        packet = primary_relation_input(data["staged"], relation)
        claim = packet["claim"]
        self.assertEqual(claim["modality"], "preferred")
        self.assertEqual(len(claim["any_of"]), 6)
        self.assertEqual(len(claim["source_branches"]), 10)
        unresolved_raw = [
            value["raw_value"]
            for branch in claim["source_branches"]
            if branch["coverage_state"] == "unresolved"
            for value in branch["raw_source_values"]
        ]
        self.assertIn("Physics", unresolved_raw)
        self.assertNotIn("normalized_value", json.dumps(packet))
        self.assertEqual(validate_exact_input(packet), packet)

    def test_narrowed_relation_receives_complete_same_span_evidence(self):
        data = self.cases["1443"]
        relation = next(
            item
            for item in data["deterministic_relations"]["relations"]
            if item["relation_id"] == "r001"
        )
        packet = primary_relation_input(data["staged"], relation)
        evidence = " ".join(
            item["quote"] for item in packet["authenticated_evidence"]
        )
        self.assertIn("professional recording equipment", evidence)
        member_ids = [
            item["atom_id"]
            for alternative in packet["claim"]["any_of"]
            for item in alternative["all_of"]
        ]
        self.assertEqual(member_ids, ["a2", "a3"])

    def test_provider_outputs_only_decision_and_requests_are_role_bound(self):
        atom_packet = next(
            task["packet"] for task in self.tasks if task["claim_type"] == "atom"
        )
        terra = _FakeSession("entails", PRIMARY_MODEL)
        primary = OpenAIExactClaimVerifierClient(
            "secret", role="primary", session=terra
        )
        result = primary.verify(atom_packet)
        request = terra.calls[0]["json"]
        self.assertEqual(request["model"], PRIMARY_MODEL)
        self.assertEqual(request["reasoning"], {"effort": "low"})
        self.assertIs(request["store"], False)
        self.assertEqual(request["tools"], [])
        self.assertEqual(
            request["text"]["format"]["schema"], decision_schema()
        )
        self.assertEqual(result.payload, {"decision": "entails"})
        self.assertEqual(result.reasoning_tokens, 16)
        self.assertEqual(result.visible_output_tokens, 8)
        with self.assertRaises(SemanticVerifierError):
            validate_provider_output(
                {"decision": "entails", "kind": "invented"}
            )

    def test_challenge_is_required_only_independent_and_sol_bound(self):
        data = self.cases["1422"]
        required = next(
            item
            for item in data["deterministic_relations"]["relations"]
            if item["relation_id"] == "r001"
        )
        packet = hard_challenge_input(data["staged"], required)
        serialized = json.dumps(packet)
        self.assertNotIn("primary_result", serialized)
        self.assertNotIn("primary_decision", serialized)
        sol = _FakeSession("entails", CHALLENGE_MODEL)
        client = OpenAIExactClaimVerifierClient(
            "secret", role="hard_challenge", session=sol
        )
        client.verify(packet)
        self.assertEqual(sol.calls[0]["json"]["model"], CHALLENGE_MODEL)
        preferred = next(
            item
            for item in self.cases["1117"]["deterministic_relations"]["relations"]
            if item["relation_id"] == "r001"
        )
        with self.assertRaises(SemanticVerifierError):
            hard_challenge_input(self.cases["1117"]["staged"], preferred)

    def test_exact_result_binding_rejects_claim_evidence_and_config_tampering(self):
        packet = next(task["packet"] for task in self.tasks)
        record = _record(packet, "entails")
        self.assertEqual(validate_exact_result_record(packet, record), record)
        for key in (
            "claim_identity_sha256",
            "claim_serialization_sha256",
            "evidence_identity_sha256",
            "model_configuration_sha256",
            "provider_output_sha256",
        ):
            tampered = copy.deepcopy(record)
            tampered["binding"][key] = "0" * 64
            with self.assertRaises(SemanticVerifierError):
                validate_exact_result_record(packet, tampered)
        changed = copy.deepcopy(packet)
        changed["claim"]["polarity"] = "negated"
        with self.assertRaises(SemanticVerifierError):
            validate_exact_result_record(changed, record)

    def _perfect_primary_records(self):
        records = {}
        for task in self.tasks:
            case_id = task["case_id"]
            local_id = task["claim_local_id"]
            if task["claim_type"] == "atom":
                decision = self.cases[case_id]["atom_labels"][local_id]
            else:
                decision = self.cases[case_id]["relation_references"][local_id][
                    "decision"
                ]
            records[task["key"]] = _record(task["packet"], decision)
        return records

    def test_perfect_primary_selects_only_19_supported_required_challenges(self):
        primary = self._perfect_primary_records()
        challenges = select_hard_challenges(self.cases, primary)
        self.assertEqual(len(challenges), 19)
        self.assertTrue(
            all(
                task["packet"]["claim"]["required_constraint"]["modality"]
                == "required"
                for task in challenges
            )
        )
        identities = {
            f"{task['case_id']}:{task['claim_local_id']}" for task in challenges
        }
        self.assertNotIn("1319:r000", identities)
        self.assertNotIn("1443:r001", identities)

    def test_perfect_exact_and_challenge_records_pass_all_gates(self):
        primary = self._perfect_primary_records()
        challenges = select_hard_challenges(self.cases, primary)
        challenge_records = {
            task["key"]: _record(task["packet"], "entails")
            for task in challenges
        }
        metrics = evaluate_records(
            self.cases, self.tasks, primary, challenges, challenge_records
        )
        self.assertTrue(metrics["acceptance"]["passed"])
        self.assertEqual(metrics["hard_assurance"]["hard_assurance_precision"], 1.0)
        self.assertEqual(metrics["hard_assurance"]["hard_assurance_recall"], 1.0)
        self.assertEqual(
            metrics["finalization_and_projection"][
                "unsupported_deterministic_hard_gates"
            ],
            [],
        )

    def test_challenge_abstention_withholds_without_promoting_or_deleting(self):
        primary = self._perfect_primary_records()
        challenges = select_hard_challenges(self.cases, primary)
        withheld = challenges[0]["key"]
        challenge_records = {
            task["key"]: _record(
                task["packet"],
                "not_established" if task["key"] == withheld else "entails",
            )
            for task in challenges
        }
        metrics = evaluate_records(
            self.cases, self.tasks, primary, challenges, challenge_records
        )
        self.assertEqual(metrics["hard_assurance"]["hard_assurance_precision"], 1.0)
        self.assertLess(metrics["hard_assurance"]["hard_assurance_recall"], 1.0)
        self.assertEqual(
            metrics["soft_retention"]["claims_deleted_by_primary_abstention"], 0
        )
        self.assertEqual(
            metrics["hard_assurance"][
                "reviewed_unsupported_hard_constraints_authorized"
            ],
            [],
        )


if __name__ == "__main__":
    unittest.main()

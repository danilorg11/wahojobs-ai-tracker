import copy
import itertools
import json
import tempfile
import unittest
from pathlib import Path

from scripts.opportunity_semantic_verifier_eval import (
    DurableAccountingJournal,
    FROZEN_GATES,
    FROZEN_TASK_IDENTITY_SET_SHA256,
    _run_provider,
    build_manifest,
    build_population,
    evaluate_records,
    reconstruct_accounting,
)
from wahojobs.opportunity_semantic_staging import (
    apply_semantic_verification,
    construct_relations,
    finalize_verified_relations,
    role_activity_composition_supported,
    stage_provisional_atoms,
    verification_from_reviewed_labels,
)
from wahojobs.opportunity_semantic_verifier import (
    DEFAULT_MODEL,
    OpenAISemanticVerifierClient,
    SemanticVerifierError,
    apply_relation_verification,
    atom_verifier_input,
    estimate_cost_usd,
    model_configuration_sha256,
    prompt_sha256,
    relation_verifier_input,
    result_record,
    schema_sha256,
    semantic_verifier_prompt,
    semantic_verifier_schema,
    staging_verification_from_results,
    validate_result_record,
    validate_provider_verifier_output,
    validate_verifier_input,
    validate_verifier_output,
)


ROOT = Path(__file__).resolve().parents[1]
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


class _FakeResponse:
    status_code = 200

    def __init__(self, payload, *, model="gpt-5.6-terra-2026-08-01"):
        self.payload = payload
        self.model = model

    def json(self):
        return {
            "id": "resp_semantic_verifier_test",
            "model": self.model,
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(self.payload),
                        }
                    ],
                }
            ],
            "usage": {
                "input_tokens": 100,
                "input_tokens_details": {
                    "cached_tokens": 20,
                    "cache_write_tokens": 10,
                },
                "output_tokens": 30,
                "output_tokens_details": {"reasoning_tokens": 12},
                "total_tokens": 130,
            },
        }


class _FakeSession:
    def __init__(self, payload):
        self.payload = payload
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
        return _FakeResponse(self.payload)


def _payload(claim_type: str, decisions: dict[str, str]) -> dict:
    return {
        "verification_version": "oe_semantic_verifier_v0",
        "claim_type": claim_type,
        "qualifier_decisions": copy.deepcopy(decisions),
    }


def _schema_accepts(schema: dict, value) -> bool:
    expected_type = schema.get("type")
    if expected_type == "object":
        if type(value) is not dict:
            return False
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        if not required <= set(value):
            return False
        if schema.get("additionalProperties") is False and not set(value) <= set(
            properties
        ):
            return False
        return all(
            key not in value or _schema_accepts(child, value[key])
            for key, child in properties.items()
        )
    if expected_type == "string" and type(value) is not str:
        return False
    return "enum" not in schema or value in schema["enum"]


def _verify_record(packet: dict, decisions: dict[str, str]) -> dict:
    session = _FakeSession(_payload(packet["claim_type"], decisions))
    client = OpenAISemanticVerifierClient("test-key", session=session)
    return result_record(client.verify(packet))


class OpportunitySemanticVerifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = json.loads(RAW_PATH.read_text(encoding="utf-8"))
        cls.review = json.loads(REVIEW_PATH.read_text(encoding="utf-8"))

    def staged(self, case_id: str) -> dict:
        case = self.raw["cases"][case_id]
        return stage_provisional_atoms(
            case["raw_extraction"],
            case["source_packet"],
            case["accepted_evidence_bindings"],
        )

    def reviewed_relations(self, case_id: str) -> tuple[dict, dict]:
        case = self.raw["cases"][case_id]
        staged = self.staged(case_id)
        reviewed = verification_from_reviewed_labels(
            staged, self.review["cases"][case_id]
        )
        verified = apply_semantic_verification(staged, reviewed)
        relations = construct_relations(verified, case.get("raw_grouping"))
        return verified, relations

    def test_frozen_prompt_schema_and_configuration_hashes(self):
        self.assertEqual(
            prompt_sha256(),
            "11ccf12bdd149a387e84fb4b05bea412e065d56e21f3b8cec3b9803270b56a1b",
        )
        self.assertEqual(
            schema_sha256(),
            "1ec1e5e4207019e219c47ef07bdeda557561d6acaf93f835c728c903c92a7068",
        )
        self.assertEqual(
            model_configuration_sha256(),
            "3ed9d85a29e7dc94c077b6c5ad336e9a214521b779011925cd26f705d500d963",
        )
        self.assertEqual(
            set(
                semantic_verifier_schema("atom")["properties"]
                ["qualifier_decisions"]["properties"]
            ),
            {"kind_payload", "polarity", "temporal"},
        )
        self.assertEqual(
            set(
                semantic_verifier_schema("relation")["properties"]
                ["qualifier_decisions"]["properties"]
            ),
            {"relation_logic", "modality", "completeness"},
        )

    def test_manifest_freezes_exact_population_relation_reference_and_gates(self):
        manifest = build_manifest()
        self.assertEqual(manifest["population"]["case_count"], 16)
        self.assertEqual(manifest["population"]["atom_claim_count"], 80)
        self.assertEqual(manifest["population"]["relation_claim_count"], 49)
        self.assertEqual(manifest["population"]["provider_call_count"], 129)
        self.assertEqual(manifest["population"]["source_branch_count"], 77)
        self.assertEqual(
            manifest["population"]["task_identity_set_sha256"],
            FROZEN_TASK_IDENTITY_SET_SHA256,
        )
        self.assertEqual(
            manifest["review_reference"]["relation_decision_counts"],
            {"entails": 36, "contradicts": 2, "not_established": 11},
        )
        self.assertEqual(
            manifest["review_reference"]["relation_state_counts"],
            {
                "complete_verified": 35,
                "grounded_incomplete": 7,
                "invalid_proposal": 7,
            },
        )
        self.assertEqual(manifest["frozen_acceptance_gates"], FROZEN_GATES)

    def test_perfect_bound_records_pass_every_frozen_gate(self):
        _, _, _, _, population = build_population()
        cases, tasks = population
        records = {}
        for task in tasks:
            case_id = task["case_id"]
            local_id = task["claim_local_id"]
            if task["claim_type"] == "atom":
                decisions = cases[case_id]["atom_qualifier_references"][local_id]
            else:
                decisions = cases[case_id]["relation_references"][local_id][
                    "qualifier_decisions"
                ]
            records[task["key"]] = _verify_record(task["packet"], decisions)
        metrics = evaluate_records(
            cases, tasks, records, provider_wall_seconds=0.0
        )
        self.assertTrue(metrics["acceptance"]["passed"])
        for family in metrics["acceptance"]["checks"].values():
            self.assertTrue(all(family.values()))
        self.assertEqual(
            metrics["normalization_and_unmapped_values"][
                "final_source_value_substitution_or_qualifier_loss"
            ],
            [],
        )
        self.assertEqual(
            metrics["projection_and_hard_gates"]["unsafe_hard_gates"], []
        )

    def test_atom_input_contains_only_immutable_claim_and_bound_evidence(self):
        staging = self.staged("1422")
        item = next(
            value
            for value in staging["provisional_atoms"]
            if value["proposal_id"] == "a4"
        )
        packet = atom_verifier_input(item)
        self.assertEqual(
            set(packet["claim"]),
            {"subject", "kind", "typed_payload", "polarity", "temporal"},
        )
        self.assertEqual(len(packet["authenticated_evidence"]), 1)
        serialized = json.dumps(packet)
        for forbidden in (
            "constraint_groups",
            "normalized_value",
            "normalization_status",
            "variant_scope",
            "eligibility",
            "hard_projection_authorized",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(validate_verifier_input(packet), packet)

        # Evidence is untrusted text. Authority-like words in a quote are data,
        # while authority-like structural fields remain forbidden.
        altered = copy.deepcopy(item)
        altered["atom"]["evidence"][0]["quote"] += " Eligibility is described."
        word_packet = atom_verifier_input(altered)
        self.assertEqual(validate_verifier_input(word_packet), word_packet)
        forbidden = copy.deepcopy(packet)
        forbidden["claim"]["typed_payload"]["eligibility"] = True
        with self.assertRaises(SemanticVerifierError):
            validate_verifier_input(forbidden)

    def test_relation_input_keeps_raw_unmapped_branches_without_normalization(self):
        verified, relations = self.reviewed_relations("1117")
        packet = relation_verifier_input(
            verified, relations["relations"][1]
        )
        source_values = [
            value["raw_value"]
            for branch in packet["claim"]["source_branches"]
            for value in branch["raw_source_values"]
        ]
        self.assertIn("Physics", source_values)
        serialized = json.dumps(packet)
        self.assertNotIn("normalized_value", serialized)
        self.assertNotIn("coverage_state", serialized)
        self.assertNotIn("proposed_atom_ids", serialized)

    def test_output_contract_uses_exact_staging_decisions_and_is_consistent(self):
        packet = atom_verifier_input(self.staged("1422")["provisional_atoms"][0])
        valid = _payload(
            "atom",
            {
                "kind_payload": "entails",
                "polarity": "entails",
                "temporal": "not_established",
            },
        )
        self.assertEqual(
            validate_provider_verifier_output(packet, valid)["decision"],
            "not_established",
        )
        canonical = validate_provider_verifier_output(packet, valid)
        invalid = copy.deepcopy(canonical)
        invalid["decision"] = "entails"
        with self.assertRaises(SemanticVerifierError):
            validate_verifier_output(packet, invalid)
        invalid = copy.deepcopy(canonical)
        invalid["replacement_payload"] = {"invented": True}
        with self.assertRaises(SemanticVerifierError):
            validate_verifier_output(packet, invalid)

    def test_claim_specific_provider_schemas_exclude_inapplicable_qualifiers(self):
        atom_schema = semantic_verifier_schema("atom")
        relation_schema = semantic_verifier_schema("relation")
        atom = _payload(
            "atom",
            {
                "kind_payload": "entails",
                "polarity": "entails",
                "temporal": "entails",
            },
        )
        relation = _payload(
            "relation",
            {
                "relation_logic": "entails",
                "modality": "entails",
                "completeness": "entails",
            },
        )
        self.assertTrue(_schema_accepts(atom_schema, atom))
        self.assertFalse(_schema_accepts(relation_schema, atom))
        self.assertTrue(_schema_accepts(relation_schema, relation))
        self.assertFalse(_schema_accepts(atom_schema, relation))

        atom["qualifier_decisions"]["modality"] = "entails"
        self.assertFalse(_schema_accepts(atom_schema, atom))
        relation["qualifier_decisions"]["polarity"] = "entails"
        self.assertFalse(_schema_accepts(relation_schema, relation))

    def test_every_provider_schema_valid_decision_tuple_is_locally_representable(self):
        _, _, _, _, population = build_population()
        _, tasks = population
        packets = {
            claim_type: next(
                task["packet"]
                for task in tasks
                if task["claim_type"] == claim_type
            )
            for claim_type in ("atom", "relation")
        }
        qualifiers = {
            "atom": ("kind_payload", "polarity", "temporal"),
            "relation": ("relation_logic", "modality", "completeness"),
        }
        decisions = ("entails", "contradicts", "not_established")
        for claim_type in ("atom", "relation"):
            schema = semantic_verifier_schema(claim_type)
            for values in itertools.product(decisions, repeat=3):
                raw = _payload(
                    claim_type, dict(zip(qualifiers[claim_type], values))
                )
                self.assertTrue(_schema_accepts(schema, raw))
                canonical = validate_provider_verifier_output(
                    packets[claim_type], raw
                )
                self.assertEqual(
                    validate_verifier_output(packets[claim_type], canonical),
                    canonical,
                )

    def test_inapplicable_qualifiers_fail_provider_schema_and_local_boundary(self):
        _, _, _, _, population = build_population()
        _, tasks = population
        for claim_type, forbidden in (
            ("atom", "modality"),
            ("relation", "polarity"),
        ):
            task = next(
                item for item in tasks if item["claim_type"] == claim_type
            )
            applicable = (
                {"kind_payload", "polarity", "temporal"}
                if claim_type == "atom"
                else {"relation_logic", "modality", "completeness"}
            )
            raw = _payload(
                claim_type, {key: "entails" for key in applicable}
            )
            raw["qualifier_decisions"][forbidden] = "entails"
            self.assertFalse(
                _schema_accepts(semantic_verifier_schema(claim_type), raw)
            )
            with self.assertRaises(SemanticVerifierError):
                validate_provider_verifier_output(task["packet"], raw)

    def test_provider_request_is_frozen_stateless_toolless_and_one_claim(self):
        packet = atom_verifier_input(self.staged("1422")["provisional_atoms"][0])
        payload = _payload(
            "atom",
            {
                "kind_payload": "entails",
                "polarity": "entails",
                "temporal": "entails",
            },
        )
        session = _FakeSession(payload)
        client = OpenAISemanticVerifierClient("secret-key", session=session)
        result = client.verify(packet)
        request = session.calls[0]["json"]
        self.assertEqual(request["model"], DEFAULT_MODEL)
        self.assertIs(request["store"], False)
        self.assertEqual(request["tools"], [])
        self.assertEqual(request["reasoning"], {"effort": "low"})
        self.assertEqual(
            request["text"]["format"]["name"],
            "oe_semantic_verifier_v0_atom",
        )
        self.assertEqual(
            set(
                request["text"]["format"]["schema"]["properties"]
                ["qualifier_decisions"]["properties"]
            ),
            {"kind_payload", "polarity", "temporal"},
        )
        self.assertEqual(
            json.loads(request["input"][1]["content"][0]["text"]), packet
        )
        self.assertEqual(result.reasoning_tokens, 12)
        self.assertEqual(result.visible_output_tokens, 18)
        self.assertEqual(result.output_tokens, 30)
        self.assertEqual(result.response_model, "gpt-5.6-terra-2026-08-01")

    def test_concurrent_provider_accounting_is_durable_and_exact(self):
        _, _, _, _, population = build_population()
        _, all_tasks = population
        tasks = all_tasks[:12]
        failed_identity = tasks[5]["packet"]["claim_identity_sha256"]

        class FakeClient:
            def verify(self, packet):
                if packet["claim_identity_sha256"] == failed_identity:
                    raise SemanticVerifierError(
                        "synthetic provider failure",
                        category="provider_http:test:synthetic",
                        diagnostics={
                            "response_model": "gpt-5.6-terra-test",
                            "input_tokens": 7,
                            "cached_input_tokens": 0,
                            "visible_output_tokens": 0,
                            "reasoning_tokens": 0,
                            "output_tokens": 0,
                            "total_tokens": 7,
                            "latency_seconds": 0.01,
                            "schema_validation_outcome": "not_reached",
                        },
                    )
                qualifier_names = (
                    ("kind_payload", "polarity", "temporal")
                    if packet["claim_type"] == "atom"
                    else ("relation_logic", "modality", "completeness")
                )
                session = _FakeSession(
                    _payload(
                        packet["claim_type"],
                        {name: "entails" for name in qualifier_names},
                    )
                )
                return OpenAISemanticVerifierClient(
                    "test-key", session=session
                ).verify(packet)

        with tempfile.TemporaryDirectory() as directory:
            journal_path = Path(directory) / "accounting.jsonl"
            records, failures, _, accounting = _run_provider(
                tasks,
                "test-key",
                4,
                accounting_path=journal_path,
                manifest_sha256="a" * 64,
                client_factory=lambda _key, _model: FakeClient(),
            )
            recovered = reconstruct_accounting(journal_path)
        self.assertEqual(len(records), 11)
        self.assertEqual(len(failures), 1)
        self.assertEqual(accounting, recovered)
        self.assertEqual(recovered["registered_tasks"], 12)
        self.assertEqual(recovered["dispatched_requests"], 12)
        self.assertEqual(recovered["completed_requests"], 11)
        self.assertEqual(recovered["failed_requests"], 1)
        self.assertEqual(
            next(iter(recovered["failures"].values()))["category"],
            "provider_http:test:synthetic",
        )

    def test_interrupted_journal_preserves_registered_and_dispatched_status(self):
        _, _, _, _, population = build_population()
        _, tasks = population
        selected = tasks[:3]
        with tempfile.TemporaryDirectory() as directory:
            journal_path = Path(directory) / "accounting.jsonl"
            journal = DurableAccountingJournal(
                journal_path, run_id="interrupted-test"
            )
            journal.create(manifest_sha256="b" * 64, tasks=selected)
            journal.append(
                "dispatch_started", task_key=selected[0]["key"]
            )
            journal.append(
                "run_interrupted",
                exception_type="KeyboardInterrupt",
                message="synthetic interruption",
            )
            recovered = reconstruct_accounting(journal_path)
        self.assertEqual(recovered["registered_tasks"], 3)
        self.assertEqual(recovered["dispatched_requests"], 1)
        self.assertEqual(recovered["completed_requests"], 0)
        self.assertEqual(
            recovered["status_counts"], {"dispatched": 1, "registered": 2}
        )

    def test_result_binding_rejects_claim_evidence_output_and_config_tampering(self):
        packet = atom_verifier_input(self.staged("1422")["provisional_atoms"][0])
        record = _verify_record(
            packet,
            {
                "kind_payload": "entails",
                "polarity": "entails",
                "temporal": "entails",
            },
        )
        self.assertEqual(validate_result_record(packet, record), record)
        for path in (
            "claim_identity_sha256",
            "evidence_identity_sha256",
            "model_configuration_sha256",
            "provider_output_sha256",
        ):
            tampered = copy.deepcopy(record)
            tampered["binding"][path] = "0" * 64
            with self.assertRaises(SemanticVerifierError):
                validate_result_record(packet, tampered)
        changed_packet = copy.deepcopy(packet)
        changed_packet["claim"]["polarity"] = "negated"
        with self.assertRaises(SemanticVerifierError):
            validate_result_record(changed_packet, record)

    def test_atom_results_project_only_into_existing_staging_decision_shape(self):
        staging = self.staged("1422")
        records = {}
        for item in staging["provisional_atoms"]:
            packet = atom_verifier_input(item)
            records[packet["claim_identity_sha256"]] = _verify_record(
                packet,
                {
                    "kind_payload": "entails",
                    "polarity": "entails",
                    "temporal": "entails",
                },
            )
        verification = staging_verification_from_results(staging, records)
        self.assertEqual(
            set(verification), {"verification_version", "decisions"}
        )
        self.assertEqual(
            set(verification["decisions"][0]),
            {"ledger_id", "atom_sha256", "decision", "qualifier_decisions"},
        )
        self.assertEqual(
            set(verification["decisions"][0]["qualifier_decisions"]),
            {"payload", "polarity", "temporal"},
        )

    def test_relation_abstention_downgrades_but_entailment_never_upgrades(self):
        verified, relations = self.reviewed_relations("1422")
        records = {}
        target_identity = None
        for relation in relations["relations"]:
            packet = relation_verifier_input(verified, relation)
            decisions = {
                "relation_logic": "entails",
                "modality": "entails",
                "completeness": "entails",
            }
            if relation["relation_id"] == "r002":
                decisions["completeness"] = "not_established"
                target_identity = packet["claim_identity_sha256"]
            records[packet["claim_identity_sha256"]] = _verify_record(
                packet, decisions
            )
        gated_staging, gated_relations = apply_relation_verification(
            verified, relations, records
        )
        self.assertIsNotNone(target_identity)
        self.assertEqual(
            gated_relations["relations"][2]["state"], "grounded_incomplete"
        )
        self.assertTrue(
            all(
                item["assurance"] != "hard_projection_authorized"
                for item in gated_staging["provisional_atoms"]
                if item["proposal_id"] in {"a4", "a5"}
            )
        )
        final = finalize_verified_relations(gated_staging, gated_relations)
        self.assertNotIn("r002", final["final_relation_ids"])

        verified, relations = self.reviewed_relations("1116")
        self.assertEqual(relations["relations"][2]["state"], "invalid_proposal")
        records = {}
        for relation in relations["relations"]:
            packet = relation_verifier_input(verified, relation)
            records[packet["claim_identity_sha256"]] = _verify_record(
                packet,
                {
                    "relation_logic": "entails",
                    "modality": "entails",
                    "completeness": "entails",
                },
            )
        _, gated_relations = apply_relation_verification(
            verified, relations, records
        )
        self.assertEqual(
            gated_relations["relations"][2]["state"], "invalid_proposal"
        )

    def test_role_activity_semantics_are_compositional_not_phrase_or_id_specials(self):
        fact = {
            "kind": "role_activity",
            "typed_payload": {
                "activity": "fact_checking",
                "artifact": "ai_output",
            },
            "evidence": [
                {"quote": "Verify factual accuracy in the model output."}
            ],
        }
        generic = {
            "kind": "role_activity",
            "typed_payload": {
                "activity": "software_testing",
                "artifact": "software",
            },
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
        self.assertTrue(role_activity_composition_supported(fact))
        self.assertFalse(role_activity_composition_supported(generic))
        self.assertTrue(role_activity_composition_supported(software))
        prompt = semantic_verifier_prompt()
        self.assertIn("Generic quality assurance", prompt)
        self.assertIn("actual proposition about testing software", prompt)

    def test_official_terra_cost_components_include_reasoning_as_output(self):
        usage = {
            "input_tokens": 100,
            "cached_input_tokens": 20,
            "cache_write_input_tokens": 10,
            "output_tokens": 30,
            "reasoning_tokens": 12,
            "visible_output_tokens": 18,
            "total_tokens": 130,
        }
        self.assertEqual(estimate_cost_usd(DEFAULT_MODEL, usage), 0.000529)


if __name__ == "__main__":
    unittest.main()

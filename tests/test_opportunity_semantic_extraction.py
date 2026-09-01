import copy
import hashlib
import json
import unittest
from pathlib import Path

from wahojobs.opportunity_semantic_contract import CONTRACT_VERSION
from wahojobs.opportunity_semantic_extraction import (
    DEFAULT_MODEL,
    EXTRACTION_CONTRACT_VERSION,
    OpenAISemanticExtractionClient,
    PROMPT_VERSION,
    REASONING_EFFORT,
    SCHEMA_VERSION,
    SemanticExtractionValidationError,
    VALIDATOR_VERSION,
    accepted_evidence_aliases,
    packet_schema_sha256,
    project_validated_extraction,
    prompt_sha256,
    schema_sha256,
    semantic_extraction_schema,
    system_prompt,
    validate_accepted_evidence_bindings,
    validate_model_extraction,
)


FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "opportunity_semantic_contract_v0.json"
)


def packet_and_bindings(case: dict):
    blocks = []
    bindings = []
    for source in case["evidence_catalog"]:
        blocks.append(
            {
                "evidence_block_id": source["id"],
                "source_ref": source["id"],
                "source_refs": [source["id"]],
                "variant_refs": [f"fixture:{case['id']}"],
                "authority_refs": [],
                "kind": "body_paragraph",
                "authority_class": "accepted_body_evidence",
                "label": "reviewed semantic-contract evidence",
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
    packet = {
        "company": {"name": "Reviewed fixture"},
        "canonical": {"canonical_title": case["id"]},
        "variants": [],
        "evidence_blocks": blocks,
    }
    return packet, bindings


def model_payload_from_contract(contract: dict) -> dict:
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
                        "alias": evidence["source_id"],
                        "quote": evidence["quote"],
                    }
                    for evidence in raw_atom["evidence"]
                ],
            }
        )
    return {
        "extraction_version": EXTRACTION_CONTRACT_VERSION,
        "atoms": atoms,
        "constraint_groups": copy.deepcopy(contract["constraint_groups"]),
    }


def review_source(alias: str, text: str) -> tuple[dict, dict]:
    source = {
        "id": alias,
        "authority": "accepted_review_checkpoint",
        "text": text,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "provenance": {
            "review_id": "oe-semantic-extraction-v0-tests",
            "case_id": "validation-control",
        },
    }
    case = {
        "id": "validation-control",
        "evidence_catalog": [source],
    }
    packet, bindings = packet_and_bindings(case)
    return packet, bindings


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


class OpportunitySemanticExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def test_prompt_schema_and_validator_identities_are_frozen(self):
        self.assertEqual(DEFAULT_MODEL, "gpt-5.6-terra")
        self.assertEqual(REASONING_EFFORT, "low")
        self.assertEqual(PROMPT_VERSION, "oe_semantic_extraction_v0_prompt_v3")
        self.assertEqual(SCHEMA_VERSION, "oe_semantic_extraction_v0_schema_v1")
        self.assertEqual(
            VALIDATOR_VERSION, "oe_semantic_extraction_v0_validator_v1"
        )
        self.assertEqual(
            prompt_sha256(),
            "434092fe85c6043df3ea2c134bc914584ce07ef26d7c66c09ed00e9f86fe537d",
        )
        self.assertEqual(
            schema_sha256(),
            "ba1766f4f00fc782d0e8a63bc208d2e3c73d8462fd0a8e4c96776c6167440124",
        )

    def test_model_schema_contains_only_atoms_evidence_and_dnf(self):
        aliases = ["E2222222222222222", "E1111111111111111"]
        schema = semantic_extraction_schema(aliases)

        self.assertEqual(schema["$id"], SCHEMA_VERSION)
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            set(schema["required"]),
            {"extraction_version", "atoms", "constraint_groups"},
        )
        self.assertEqual(
            schema["$defs"]["evidence_alias"]["enum"], sorted(aliases)
        )
        atom_variants = schema["properties"]["atoms"]["items"]["anyOf"]
        for variant in atom_variants:
            self.assertEqual(
                set(variant["properties"]),
                {"id", "kind", "typed_payload", "polarity", "temporal", "evidence"},
            )
        serialized = json.dumps(schema, sort_keys=True)
        for forbidden in (
            "eligibility_decision",
            "field_path",
            "variant_scope",
            "normalized_value",
            "legacy_patch",
            "arbitrary_predicate",
            '"subject"',
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(
            packet_schema_sha256(aliases),
            packet_schema_sha256(list(reversed(aliases))),
        )

    def test_prompt_preserves_semantic_authority_boundaries(self):
        prompt = system_prompt()
        for requirement in (
            "Precision is primary",
            "Omit an unknown",
            "Status is not experience",
            "Interest is not experience",
            "Do not relabel registration as a license or credential",
            "Expertise is not automatically a language-proficiency gate",
            "Generic quality assurance or fact-checking is not software_testing",
            "LaTeX or mathematical formatting is not mathematics domain expertise",
            "Every atom must occur exactly once in exactly one group",
            "Do not split an OR into separate hard requirements",
            "Do not narrow a coordinated OR or alternative",
            "examples do not create extra or replacement atoms",
            "Determine temporal meaning independently from requirement modality",
            "does not become by_start merely because it is required",
            "explicitly states a present state",
            "even when compatibility projection may leave it grounded_unprojected",
            "projection availability is irrelevant",
            "preserve the parallel source order",
            "Never create a Cartesian product",
            "omit only that pairing while preserving other directly supported propositions",
            "never surround a valid atom with plausible but unstated atoms",
            "Include any governing qualification header or lead-in",
            "Copy an exact, contiguous quote",
        ):
            self.assertIn(requirement, prompt)
        for forbidden in (
            "attributes.requirements",
            "eligibility_decision",
            "variant_scope",
            "legacy_patch",
        ):
            self.assertNotIn(forbidden, prompt)

    def test_responses_request_uses_terra_low_strict_schema_and_usage(self):
        payload = {
            "extraction_version": EXTRACTION_CONTRACT_VERSION,
            "atoms": [],
            "constraint_groups": [],
        }
        provider = {
            "id": "resp_fixture",
            "model": "gpt-5.6-terra",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": json.dumps(payload)}
                    ],
                }
            ],
            "usage": {
                "input_tokens": 100,
                "input_tokens_details": {"cached_tokens": 20},
                "output_tokens": 30,
                "output_tokens_details": {"reasoning_tokens": 10},
                "total_tokens": 130,
            },
        }
        session = FakeSession(FakeResponse(provider))
        client = OpenAISemanticExtractionClient("secret", session=session)
        packet = {
            "evidence_blocks": [
                {
                    "evidence_block_id": "E1111111111111111",
                    "authority_class": "accepted_body_evidence",
                    "content": "Accepted semantic evidence.",
                },
                {
                    "evidence_block_id": "E2222222222222222",
                    "authority_class": "page_metadata_context",
                    "content": "metadata.language: English",
                },
            ]
        }

        result = client.extract(packet)

        self.assertEqual(result.payload, payload)
        self.assertEqual(result.cached_input_tokens, 20)
        self.assertEqual(result.reasoning_tokens, 10)
        self.assertEqual(result.visible_output_tokens, 20)
        self.assertEqual(result.estimated_cost_usd, 0.000524)
        request = session.calls[0][1]["json"]
        self.assertEqual(request["model"], "gpt-5.6-terra")
        self.assertEqual(request["reasoning"], {"effort": "low"})
        self.assertFalse(request["store"])
        self.assertTrue(request["text"]["format"]["strict"])
        self.assertEqual(
            request["text"]["format"]["schema"]["$defs"]["evidence_alias"][
                "enum"
            ],
            ["E1111111111111111"],
        )
        self.assertNotIn("E2222222222222222", json.dumps(request["text"]))

    def test_empty_accepted_packet_short_circuits_without_provider_call(self):
        session = FakeSession(None)
        client = OpenAISemanticExtractionClient("secret", session=session)
        result = client.extract({"evidence_blocks": []})

        self.assertFalse(result.provider_called)
        self.assertEqual(result.payload["atoms"], [])
        self.assertEqual(session.calls, [])

    def test_every_reviewed_gold_contract_round_trips_through_model_boundary(self):
        for case in self.fixture["cases"]:
            with self.subTest(case=case["id"]):
                packet, bindings = packet_and_bindings(case)
                payload = model_payload_from_contract(case["contract"])

                validated = validate_model_extraction(payload, packet, bindings)
                projected = project_validated_extraction(validated)

                self.assertEqual(validated["rejected_atoms"], [])
                self.assertEqual(validated["rejected_groups"], [])
                self.assertEqual(
                    validated["accepted_contract"]["contract_version"],
                    CONTRACT_VERSION,
                )
                self.assertEqual(
                    projected["legacy_patch"], case["expected"]["legacy_patch"]
                )
                self.assertEqual(
                    [item["state"] for item in projected["group_outcomes"]],
                    case["expected"]["group_states"],
                )
                self.assertEqual(
                    [item["reason"] for item in projected["group_outcomes"]],
                    case["expected"]["group_reasons"],
                )

    def test_server_adds_subject_normalization_and_span_coordinates(self):
        case = next(
            item
            for item in self.fixture["cases"]
            if item["id"] == "latex_capability_without_math_domain"
        )
        packet, bindings = packet_and_bindings(case)
        payload = model_payload_from_contract(case["contract"])

        validated = validate_model_extraction(payload, packet, bindings)
        atom = validated["accepted_contract"]["atoms"][0]

        self.assertEqual(atom["subject"], "candidate")
        self.assertEqual(
            atom["normalized_value"],
            'capability:{"capability":"latex_typesetting"}',
        )
        self.assertEqual(atom["evidence"][0]["start"], 0)
        self.assertEqual(
            atom["evidence"][0]["end"], len(atom["evidence"][0]["quote"])
        )
        self.assertNotIn("subject", payload["atoms"][0])
        self.assertNotIn("normalized_value", payload["atoms"][0])

    def test_unsupported_atom_is_rejected_while_independent_valid_group_survives(self):
        text = (
            "Fact-checking capability is required. "
            "The role formats mathematical notation in LaTeX."
        )
        packet, bindings = review_source("review:partial:one", text)
        payload = {
            "extraction_version": EXTRACTION_CONTRACT_VERSION,
            "atoms": [
                {
                    "id": "fact_checking",
                    "kind": "capability",
                    "typed_payload": {"capability": "fact_checking"},
                    "polarity": "affirmed",
                    "temporal": "unspecified",
                    "evidence": [
                        {
                            "alias": "review:partial:one",
                            "quote": "Fact-checking capability is required.",
                        }
                    ],
                },
                {
                    "id": "invented_math_expertise",
                    "kind": "domain_expertise",
                    "typed_payload": {"domain": "mathematics"},
                    "polarity": "affirmed",
                    "temporal": "unspecified",
                    "evidence": [
                        {
                            "alias": "review:partial:one",
                            "quote": "The role formats mathematical notation in LaTeX.",
                        }
                    ],
                },
            ],
            "constraint_groups": [
                {
                    "modality": "required",
                    "any_of": [{"all_of": ["fact_checking"]}],
                },
                {
                    "modality": "required",
                    "any_of": [{"all_of": ["invented_math_expertise"]}],
                },
            ],
        }

        validated = validate_model_extraction(payload, packet, bindings)
        projected = project_validated_extraction(validated)

        self.assertEqual(validated["accepted_atom_ids"], ["fact_checking"])
        self.assertEqual(validated["accepted_group_indices"], [0])
        self.assertEqual(len(validated["rejected_atoms"]), 1)
        self.assertIn(
            "no single authenticated evidence span",
            validated["rejected_atoms"][0]["reason"],
        )
        self.assertEqual(len(validated["rejected_groups"]), 1)
        self.assertEqual(
            projected["legacy_patch"]["attributes"]["requirements"][
                "skills_required"
            ],
            ["Fact-checking"],
        )

    def test_model_cannot_supply_authority_or_legacy_fields(self):
        case = next(
            item
            for item in self.fixture["cases"]
            if item["id"] == "required_asset_access"
        )
        packet, bindings = packet_and_bindings(case)
        payload = model_payload_from_contract(case["contract"])
        forbidden = (
            (payload, "eligibility_decision", True),
            (payload["atoms"][0], "subject", "candidate"),
            (payload["atoms"][0], "field_path", "attributes.requirements.skills"),
            (payload["constraint_groups"][0], "variant_scope", ["v1"]),
        )
        for target, key, value in forbidden:
            with self.subTest(key=key):
                mutated = copy.deepcopy(payload)
                if target is payload:
                    mutated[key] = value
                elif target is payload["atoms"][0]:
                    mutated["atoms"][0][key] = value
                else:
                    mutated["constraint_groups"][0][key] = value
                if target is payload:
                    with self.assertRaises(SemanticExtractionValidationError):
                        validate_model_extraction(mutated, packet, bindings)
                else:
                    result = validate_model_extraction(mutated, packet, bindings)
                    self.assertEqual(result["accepted_group_indices"], [])
                    self.assertTrue(
                        result["rejected_atoms"] or result["rejected_groups"]
                    )

    def test_alias_quote_and_authority_are_authenticated_before_contract_validation(self):
        case = next(
            item
            for item in self.fixture["cases"]
            if item["id"] == "genuine_historical_experience"
        )
        packet, bindings = packet_and_bindings(case)
        payload = model_payload_from_contract(case["contract"])

        invented_alias = copy.deepcopy(payload)
        invented_alias["atoms"][0]["evidence"][0]["alias"] = "EINVENTED"
        result = validate_model_extraction(invented_alias, packet, bindings)
        self.assertEqual(result["accepted_atom_ids"], [])
        self.assertIn("does not name accepted evidence", result["rejected_atoms"][0]["reason"])

        paraphrase = copy.deepcopy(payload)
        paraphrase["atoms"][0]["evidence"][0]["quote"] = "Litigation experience required."
        result = validate_model_extraction(paraphrase, packet, bindings)
        self.assertEqual(result["accepted_atom_ids"], [])
        self.assertIn("not an exact packet-evidence span", result["rejected_atoms"][0]["reason"])

        bad_binding = copy.deepcopy(bindings)
        bad_binding[0]["text_sha256"] = "0" * 64
        with self.assertRaises(SemanticExtractionValidationError):
            validate_accepted_evidence_bindings(packet, bad_binding)

        metadata_packet = copy.deepcopy(packet)
        metadata_packet["evidence_blocks"][0]["authority_class"] = "page_metadata_context"
        with self.assertRaises(SemanticExtractionValidationError):
            validate_accepted_evidence_bindings(metadata_packet, bindings)

    def test_server_rejects_modality_and_dnf_changes(self):
        case = next(
            item
            for item in self.fixture["cases"]
            if item["id"] == "sports_background_or_interest"
        )
        packet, bindings = packet_and_bindings(case)
        payload = model_payload_from_contract(case["contract"])

        wrong_modality = copy.deepcopy(payload)
        wrong_modality["constraint_groups"][0]["modality"] = "preferred"
        result = validate_model_extraction(wrong_modality, packet, bindings)
        self.assertEqual(result["accepted_group_indices"], [])
        self.assertEqual(len(result["rejected_groups"]), 1)

        split_or = copy.deepcopy(payload)
        split_or["constraint_groups"] = [
            {"modality": "required", "any_of": [{"all_of": ["sports_background"]}]},
            {"modality": "required", "any_of": [{"all_of": ["sports_interest"]}]},
        ]
        # Each atom is evidence-supported, so the server cannot infer the lost OR;
        # the reviewed extraction benchmark, not a field-repair guard, owns this error.
        result = validate_model_extraction(split_or, packet, bindings)
        self.assertEqual(len(result["accepted_group_indices"]), 2)

    def test_only_accepted_body_aliases_are_model_visible(self):
        packet = {
            "evidence_blocks": [
                {
                    "evidence_block_id": "Ebody",
                    "authority_class": "accepted_body_evidence",
                },
                {
                    "evidence_block_id": "Elisting",
                    "authority_class": "variant_listing_evidence",
                },
                {
                    "evidence_block_id": "Emetadata",
                    "authority_class": "page_metadata_context",
                },
            ]
        }
        self.assertEqual(accepted_evidence_aliases(packet), ["Ebody"])


if __name__ == "__main__":
    unittest.main()

import ast
import copy
import json
from pathlib import Path
import unittest

from wahojobs.matching.semantic_shadow_grounding import (
    SEMANTIC_SHADOW_GROUNDING_OUTPUT_VERSION,
    SemanticShadowGroundingContractError,
    build_semantic_shadow_grounding_output_schema_v1,
    build_semantic_shadow_grounding_request_v1,
    validate_semantic_shadow_grounding_output_v1,
)
from wahojobs.opportunity_semantic_authority import (
    build_semantic_matching_packet,
    canonical_sha256,
)


ROOT = Path(__file__).resolve().parents[1]
GOLD = json.loads(
    (ROOT / "tests" / "fixtures" / "opportunity_semantic_contract_v0.json")
    .read_text(encoding="utf-8")
)


def reviewed_packet(canonical_id):
    case = next(
        item
        for item in GOLD["cases"]
        if item["id"] == "advanced_degree_or_professional_standing"
    )
    variant_ref = f"fixture_variant:{canonical_id}"
    relationships = [
        {
            "source_id": source["id"],
            "evidence_block_id": f"fixture_block_{index:03d}",
            "derivation": "server_authenticated_evidence_binding",
            "variant_refs": [variant_ref],
            "source_refs": [variant_ref],
            "authority_refs": ["fixture:grounding_contract"],
        }
        for index, source in enumerate(case["evidence_catalog"], start=1)
    ]
    bundle = {
        "contract": case["contract"],
        "evidence_catalog": case["evidence_catalog"],
    }
    return build_semantic_matching_packet(
        case["contract"],
        case["evidence_catalog"],
        canonical_ref=f"canonical_opportunity:{canonical_id}",
        known_variant_refs=[variant_ref],
        semantic_input_version="grounding-test-v1",
        semantic_input_sha256=canonical_sha256(bundle),
        source_packet_sha256=canonical_sha256(case["evidence_catalog"]),
        variant_relationships=relationships,
    )


def confirmation():
    return {
        "all_supplied_opportunities_retained": True,
        "unknown_treated_as_negative": False,
        "profile_changed": False,
        "eligibility_changed": False,
    }


class SemanticShadowGroundingTests(unittest.TestCase):
    def setUp(self):
        self.profile = {
            "schema_version": "benchmark_profile_minimized_v1",
            "facts": [
                {
                    "category": "education",
                    "fact_ref": "profile_fact:education",
                    "value": "advanced_degree",
                },
                {
                    "category": "languages",
                    "fact_ref": "profile_fact:languages",
                    "value": ["English", "Spanish"],
                },
                {
                    "category": "domains",
                    "fact_ref": "profile_fact:domains",
                    "value": ["biology"],
                },
            ],
            "grounding_limitations": [
                {"category": "location", "status": "not_grounded"},
                {"category": "experience_years", "status": "unknown"},
            ],
        }
        self.request = build_semantic_shadow_grounding_request_v1(
            profile=self.profile,
            packets=[reviewed_packet(980001), reviewed_packet(980002)],
            evaluation_mode="relative_reranking",
        )

    def _profile_ref(self, dimension, state="grounded"):
        return next(
            ref_id
            for ref_id, item in self.request.local_catalog[
                "profile_references"
            ].items()
            if item["semantic_dimension"] == dimension and item["state"] == state
        )

    def _opportunity_ref(self, opportunity_id, reference_type, dimension=None):
        return next(
            ref_id
            for ref_id, item in self.request.local_catalog["opportunities"][
                opportunity_id
            ]["references"].items()
            if item["reference_type"] == reference_type
            and (dimension is None or item["semantic_dimension"] == dimension)
        )

    def _valid_output(self):
        education = self._profile_ref("education")
        assessments = {}
        for opportunity_id in ("J001", "J002"):
            proposition = self._opportunity_ref(
                opportunity_id, "proposition", "education"
            )
            group_refs = self.request.local_catalog["opportunities"][
                opportunity_id
            ]["references"][proposition]["meaning"]["group_reference_ids"]
            assessments[opportunity_id] = {
                "opportunity_id": opportunity_id,
                "assessment_state": "grounded_alignment",
                "findings": [
                    {
                        "finding_type": "direct_alignment",
                        "relationship_type": "education_alignment",
                        "ranking_effect": "positive_support",
                        "profile_reference_ids": [education],
                        "proposition_reference_ids": [proposition],
                        "group_reference_ids": list(group_refs),
                        "evidence_reference_ids": [],
                        "scope_reference_ids": [],
                    }
                ],
                "uncertainties": [],
            }
        return {
            "contract_version": SEMANTIC_SHADOW_GROUNDING_OUTPUT_VERSION,
            "evaluation_mode": "relative_reranking",
            "relative_ordering_groups": [
                {"opportunity_ids": ["J001", "J002"]}
            ],
            "opportunity_assessments": assessments,
            "authority_confirmation": confirmation(),
        }

    def test_provider_catalog_uses_only_request_local_opaque_references(self):
        serialized = json.dumps(self.request.provider_input, sort_keys=True)
        self.assertNotIn("canonical_opportunity:", serialized)
        self.assertNotIn("source_hash:", serialized)
        self.assertNotIn("extract:", serialized)
        profile_ids = {
            item["reference_id"]
            for item in self.request.provider_input["profile_reference_catalog"]
        }
        opportunity_ids = {
            item["opportunity_id"]
            for item in self.request.provider_input["opportunities"]
        }
        opportunity_refs = {
            ref["reference_id"]
            for item in self.request.provider_input["opportunities"]
            for ref in item["reference_catalog"]
        }
        self.assertTrue(all(item.startswith("P") for item in profile_ids))
        self.assertEqual(opportunity_ids, {"J001", "J002"})
        self.assertTrue(all(item.startswith("O") for item in opportunity_refs))

    def test_runtime_and_ui_do_not_import_provider_grounding_contract(self):
        contract_path = (
            ROOT / "wahojobs" / "matching" / "semantic_shadow_grounding.py"
        )
        importers = []
        for path in (ROOT / "wahojobs").rglob("*.py"):
            if path == contract_path:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == (
                    "wahojobs.matching.semantic_shadow_grounding"
                ):
                    importers.append(str(path.relative_to(ROOT)))
                if isinstance(node, ast.Import) and any(
                    alias.name == "wahojobs.matching.semantic_shadow_grounding"
                    for alias in node.names
                ):
                    importers.append(str(path.relative_to(ROOT)))
        self.assertEqual(importers, [])

    def test_strict_schema_closes_reference_values_to_request_catalog(self):
        schema = build_semantic_shadow_grounding_output_schema_v1(
            self.request.provider_input
        )
        serialized = json.dumps(schema, sort_keys=True)
        self.assertIn('"enum": ["J001", "J002"]', serialized)
        self.assertIn('"enum": ["P001"', serialized)
        self.assertNotIn("canonical_opportunity:", serialized)

        assessment_properties = schema["properties"]["opportunity_assessments"][
            "properties"
        ]
        self.assertEqual(set(assessment_properties), {"J001", "J002"})
        finding_variants = assessment_properties["J001"]["properties"]["findings"][
            "items"
        ]["anyOf"]
        evidence_ids = {
            ref_id
            for ref_id, item in self.request.local_catalog["opportunities"]["J001"][
                "references"
            ].items()
            if item["reference_type"] == "evidence"
        }
        for variant in finding_variants:
            proposition_enum = set(
                variant["properties"]["proposition_reference_ids"]["items"]["enum"]
            )
            self.assertTrue(proposition_enum)
            self.assertTrue(proposition_enum.isdisjoint(evidence_ids))

    def test_valid_structured_grounding_passes_without_free_form_claims(self):
        audit = validate_semantic_shadow_grounding_output_v1(
            output=self._valid_output(),
            provider_input=self.request.provider_input,
            local_catalog=self.request.local_catalog,
        )
        self.assertTrue(audit["contract_valid"])
        self.assertEqual(audit["reference_count"], audit["resolvable_reference_count"])
        self.assertEqual(audit["unsupported_free_form_claim_count"], 0)

    def test_invented_path_reference_and_cross_opportunity_reference_fail(self):
        invented = self._valid_output()
        invented["opportunity_assessments"]["J001"]["findings"][0][
            "profile_reference_ids"
        ] = ["profile_fact:education"]
        with self.assertRaises(SemanticShadowGroundingContractError) as raised:
            validate_semantic_shadow_grounding_output_v1(
                output=invented,
                provider_input=self.request.provider_input,
                local_catalog=self.request.local_catalog,
            )
        self.assertIn(
            "invented_or_duplicate_profile_reference", raised.exception.reason_codes
        )

        cross = self._valid_output()
        cross["opportunity_assessments"]["J001"]["findings"][0][
            "proposition_reference_ids"
        ] = [self._opportunity_ref("J002", "proposition", "education")]
        with self.assertRaises(SemanticShadowGroundingContractError) as raised:
            validate_semantic_shadow_grounding_output_v1(
                output=cross,
                provider_input=self.request.provider_input,
                local_catalog=self.request.local_catalog,
            )
        self.assertIn("cross_opportunity_reference", raised.exception.reason_codes)

    def test_missingness_cannot_become_finding_or_free_form_absence(self):
        output = self._valid_output()
        missing = self._profile_ref("location", "not_grounded")
        output["opportunity_assessments"]["J001"]["findings"][0][
            "profile_reference_ids"
        ] = [missing]
        with self.assertRaises(SemanticShadowGroundingContractError) as raised:
            validate_semantic_shadow_grounding_output_v1(
                output=output,
                provider_input=self.request.provider_input,
                local_catalog=self.request.local_catalog,
            )
        self.assertIn(
            "missing_profile_state_used_as_finding", raised.exception.reason_codes
        )

        prose = self._valid_output()
        prose["opportunity_assessments"]["J001"]["summary"] = (
            "The candidate has no education."
        )
        with self.assertRaises(SemanticShadowGroundingContractError) as raised:
            validate_semantic_shadow_grounding_output_v1(
                output=prose,
                provider_input=self.request.provider_input,
                local_catalog=self.request.local_catalog,
            )
        self.assertIn("unexpected_assessment_field", raised.exception.reason_codes)

    def test_language_or_domain_evidence_cannot_establish_education(self):
        for dimension in ("language", "domain"):
            with self.subTest(dimension=dimension):
                output = self._valid_output()
                output["opportunity_assessments"]["J001"]["findings"][0][
                    "profile_reference_ids"
                ] = [self._profile_ref(dimension)]
                with self.assertRaises(
                    SemanticShadowGroundingContractError
                ) as raised:
                    validate_semantic_shadow_grounding_output_v1(
                        output=output,
                        provider_input=self.request.provider_input,
                        local_catalog=self.request.local_catalog,
                    )
                self.assertIn("evidence_role_mismatch", raised.exception.reason_codes)

    def test_evidence_only_finding_is_rejected(self):
        output = self._valid_output()
        finding = output["opportunity_assessments"]["J001"]["findings"][0]
        finding["proposition_reference_ids"] = []
        finding["group_reference_ids"] = []
        finding["evidence_reference_ids"] = [
            self._opportunity_ref("J001", "evidence")
        ]
        with self.assertRaises(SemanticShadowGroundingContractError) as raised:
            validate_semantic_shadow_grounding_output_v1(
                output=output,
                provider_input=self.request.provider_input,
                local_catalog=self.request.local_catalog,
            )
        self.assertIn(
            "finding_without_opportunity_proposition", raised.exception.reason_codes
        )

    def test_proposition_grounding_with_evidence_corroboration_is_valid(self):
        output = self._valid_output()
        output["opportunity_assessments"]["J001"]["findings"][0][
            "evidence_reference_ids"
        ] = [self._opportunity_ref("J001", "evidence")]
        audit = validate_semantic_shadow_grounding_output_v1(
            output=output,
            provider_input=self.request.provider_input,
            local_catalog=self.request.local_catalog,
        )
        self.assertTrue(audit["contract_valid"])

    def test_reference_roles_cannot_masquerade_as_semantic_references(self):
        for reference_type in ("evidence", "variant"):
            with self.subTest(reference_type=reference_type):
                output = self._valid_output()
                output["opportunity_assessments"]["J001"]["findings"][0][
                    "proposition_reference_ids"
                ] = [self._opportunity_ref("J001", reference_type)]
                with self.assertRaises(
                    SemanticShadowGroundingContractError
                ) as raised:
                    validate_semantic_shadow_grounding_output_v1(
                        output=output,
                        provider_input=self.request.provider_input,
                        local_catalog=self.request.local_catalog,
                    )
                self.assertIn("reference_role_mismatch", raised.exception.reason_codes)

    def test_context_only_findings_cannot_affect_ranking(self):
        output = self._valid_output()
        finding = output["opportunity_assessments"]["J001"]["findings"][0]
        finding["finding_type"] = "context_only"
        finding["relationship_type"] = "domain_context"
        finding["ranking_effect"] = "positive_support"
        finding["profile_reference_ids"] = [self._profile_ref("domain")]
        with self.assertRaises(SemanticShadowGroundingContractError) as raised:
            validate_semantic_shadow_grounding_output_v1(
                output=output,
                provider_input=self.request.provider_input,
                local_catalog=self.request.local_catalog,
            )
        self.assertIn("finding_ranking_effect_mismatch", raised.exception.reason_codes)
        self.assertIn("context_used_as_ranking_support", raised.exception.reason_codes)

    def test_incomplete_relation_requires_partial_finding_and_uncertainty(self):
        output = self._valid_output()
        local_catalog = copy.deepcopy(self.request.local_catalog)
        proposition = output["opportunity_assessments"]["J001"]["findings"][0][
            "proposition_reference_ids"
        ][0]
        local_catalog["opportunities"]["J001"]["references"][proposition][
            "state"
        ] = "unresolved"
        with self.assertRaises(SemanticShadowGroundingContractError) as raised:
            validate_semantic_shadow_grounding_output_v1(
                output=output,
                provider_input=self.request.provider_input,
                local_catalog=local_catalog,
            )
        self.assertIn(
            "unresolved_material_used_as_direct_alignment",
            raised.exception.reason_codes,
        )

        output["opportunity_assessments"]["J001"]["findings"][0][
            "finding_type"
        ] = "partial_alignment"
        output["opportunity_assessments"]["J001"]["assessment_state"] = (
            "limited_grounded_alignment"
        )
        output["opportunity_assessments"]["J001"]["uncertainties"] = [
            {
                "state": "unresolved",
                "effect": "non_negative",
                "profile_reference_ids": [],
                "proposition_reference_ids": [proposition],
                "group_reference_ids": [],
                "evidence_reference_ids": [],
                "scope_reference_ids": [],
            }
        ]
        audit = validate_semantic_shadow_grounding_output_v1(
            output=output,
            provider_input=self.request.provider_input,
            local_catalog=local_catalog,
        )
        self.assertTrue(audit["contract_valid"])

    def test_single_assessment_schema_and_validator_forbid_ordering(self):
        single_request = build_semantic_shadow_grounding_request_v1(
            profile=self.profile,
            packets=[reviewed_packet(980001)],
            evaluation_mode="single_opportunity_assessment",
        )
        schema = build_semantic_shadow_grounding_output_schema_v1(
            single_request.provider_input
        )
        ordering = schema["properties"]["relative_ordering_groups"]
        self.assertEqual(ordering["maxItems"], 0)
        output = copy.deepcopy(self._valid_output())
        output["evaluation_mode"] = "single_opportunity_assessment"
        output["opportunity_assessments"] = {
            "J001": output["opportunity_assessments"]["J001"]
        }
        output["relative_ordering_groups"] = [{"opportunity_ids": ["J001"]}]
        with self.assertRaises(SemanticShadowGroundingContractError) as raised:
            validate_semantic_shadow_grounding_output_v1(
                output=output,
                provider_input=single_request.provider_input,
                local_catalog=single_request.local_catalog,
            )
        self.assertIn("single_assessment_created_ordering", raised.exception.reason_codes)

        output["relative_ordering_groups"] = []
        audit = validate_semantic_shadow_grounding_output_v1(
            output=output,
            provider_input=single_request.provider_input,
            local_catalog=single_request.local_catalog,
        )
        self.assertTrue(audit["contract_valid"])

    def test_relative_ranking_schema_supports_valid_ordering(self):
        schema = build_semantic_shadow_grounding_output_schema_v1(
            self.request.provider_input
        )
        ordering = schema["properties"]["relative_ordering_groups"]
        self.assertEqual(ordering["minItems"], 1)
        audit = validate_semantic_shadow_grounding_output_v1(
            output=self._valid_output(),
            provider_input=self.request.provider_input,
            local_catalog=self.request.local_catalog,
        )
        self.assertTrue(audit["contract_valid"])


if __name__ == "__main__":
    unittest.main()

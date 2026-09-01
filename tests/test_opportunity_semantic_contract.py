import copy
import hashlib
import json
import unittest
from pathlib import Path

from wahojobs.opportunity_semantic_contract import (
    ATOM_KINDS,
    CONTRACT_VERSION,
    FIXTURE_VERSION,
    HARD_LEGACY_FIELD_PATHS,
    MAX_ALTERNATIVES_PER_GROUP,
    MAX_ATOMS_PER_CONJUNCTION,
    SemanticContractValidationError,
    TEMPORALS,
    TEMPORALS_BY_KIND,
    evaluate_constraint_group,
    flatten_patch_paths,
    project_legacy_compatibility,
    semantic_contract_schema,
    validate_accepted_evidence_catalog,
    validate_and_normalize_contract,
)


FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "opportunity_semantic_contract_v0.json"
)


def review_source(source_id: str, text: str, case_id: str = "test-control") -> dict:
    return {
        "id": source_id,
        "authority": "accepted_review_checkpoint",
        "text": text,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "provenance": {
            "review_id": "oe-semantic-contract-v0-tests",
            "case_id": case_id,
        },
    }


def whole_source_ref(source: dict) -> dict:
    return {
        "source_id": source["id"],
        "start": 0,
        "end": len(source["text"]),
        "quote": source["text"],
    }


def single_atom_contract(source: dict, atom: dict, modality: str = "required") -> dict:
    atom = copy.deepcopy(atom)
    atom["evidence"] = [whole_source_ref(source)]
    return {
        "contract_version": CONTRACT_VERSION,
        "atoms": [atom],
        "constraint_groups": [
            {"modality": modality, "any_of": [{"all_of": [atom["id"]]}]}
        ],
    }


class OpportunitySemanticContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        cls.cases = {case["id"]: case for case in cls.fixture["cases"]}

    def case(self, case_id: str) -> dict:
        return copy.deepcopy(self.cases[case_id])

    def project(self, case_id: str) -> dict:
        case = self.case(case_id)
        return project_legacy_compatibility(
            case["contract"], case["evidence_catalog"]
        )

    def assertInvalid(self, contract, catalog):
        with self.assertRaises(SemanticContractValidationError):
            validate_and_normalize_contract(contract, catalog)

    def test_schema_is_closed_typed_and_contains_no_authority_escape_hatches(self):
        schema = semantic_contract_schema()

        self.assertEqual(schema["$id"], CONTRACT_VERSION)
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            set(schema["required"]),
            {"contract_version", "atoms", "constraint_groups"},
        )
        atom_variants = schema["properties"]["atoms"]["items"]["oneOf"]
        self.assertEqual(len(atom_variants), len(ATOM_KINDS))
        self.assertEqual(
            {variant["properties"]["kind"]["const"] for variant in atom_variants},
            set(ATOM_KINDS),
        )
        for variant in atom_variants:
            self.assertFalse(variant["additionalProperties"])
            self.assertFalse(
                variant["properties"]["typed_payload"]["additionalProperties"]
            )
        serialized = json.dumps(schema, sort_keys=True)
        for forbidden in (
            "arbitrary_predicate",
            "eligibility_decision",
            "field_path",
            "other",
            "variant_scope",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_every_gold_case_validates_and_matches_its_reviewed_projection(self):
        self.assertEqual(self.fixture["fixture_version"], FIXTURE_VERSION)
        self.assertGreaterEqual(len(self.fixture["cases"]), 15)

        for case in self.fixture["cases"]:
            with self.subTest(case=case["id"]):
                result = project_legacy_compatibility(
                    case["contract"], case["evidence_catalog"]
                )
                self.assertEqual(result["legacy_patch"], case["expected"]["legacy_patch"])
                self.assertEqual(
                    [outcome["state"] for outcome in result["group_outcomes"]],
                    case["expected"]["group_states"],
                )
                self.assertEqual(
                    [outcome["reason"] for outcome in result["group_outcomes"]],
                    case["expected"]["group_reasons"],
                )
                for kind, expected in case["expected"]["knowledge_by_kind"].items():
                    self.assertEqual(result["knowledge_by_kind"][kind], expected)

    def test_gold_catalog_uses_stored_accepted_captures_and_explicit_controls(self):
        accepted_capture_ids = set()
        review_sources = 0
        represented_kinds = set()
        for case in self.fixture["cases"]:
            indexed = validate_accepted_evidence_catalog(case["evidence_catalog"])
            self.assertEqual(len(indexed), len(case["evidence_catalog"]))
            represented_kinds.update(
                atom["kind"] for atom in case["contract"]["atoms"]
            )
            for source in case["evidence_catalog"]:
                if source["authority"] == "accepted_capture":
                    accepted_capture_ids.add(
                        source["provenance"]["accepted_capture_id"]
                    )
                else:
                    review_sources += 1

        self.assertTrue(
            {274, 294, 437, 438, 439, 444, 641, 750, 776}.issubset(
                accepted_capture_ids
            )
        )
        self.assertGreaterEqual(review_sources, 6)
        self.assertEqual(represented_kinds, set(ATOM_KINDS))

    def test_open_predicates_paths_scope_and_eligibility_claims_are_rejected(self):
        base = self.case("latex_capability_without_math_domain")
        atom_forbidden = (
            ("predicate", "knows_latex"),
            ("field_path", "attributes.requirements.skills_required"),
            ("other", "mathematics-ish"),
            ("variant_scope", ["model-picked-variant"]),
            ("eligibility_decision", True),
        )
        for key, value in atom_forbidden:
            with self.subTest(atom_key=key):
                case = copy.deepcopy(base)
                case["contract"]["atoms"][0][key] = value
                self.assertInvalid(case["contract"], case["evidence_catalog"])

        case = copy.deepcopy(base)
        case["contract"]["atoms"][0]["kind"] = "other"
        self.assertInvalid(case["contract"], case["evidence_catalog"])

        case = copy.deepcopy(base)
        case["contract"]["atoms"][0]["typed_payload"]["other"] = "escape"
        self.assertInvalid(case["contract"], case["evidence_catalog"])

        case = copy.deepcopy(base)
        case["contract"]["constraint_groups"][0]["variant_scope"] = ["v1"]
        self.assertInvalid(case["contract"], case["evidence_catalog"])

        case = copy.deepcopy(base)
        case["contract"]["eligibility_decision"] = "eligible"
        self.assertInvalid(case["contract"], case["evidence_catalog"])

    def test_server_computes_normalized_values_and_rejects_a_supplied_mismatch(self):
        case = self.case("latex_capability_without_math_domain")
        normalized = validate_and_normalize_contract(
            case["contract"], case["evidence_catalog"]
        )
        expected = 'capability:{"capability":"latex_typesetting"}'
        self.assertEqual(normalized["atoms"][0]["normalized_value"], expected)
        self.assertNotIn("normalized_value", case["contract"]["atoms"][0])

        case["contract"]["atoms"][0]["normalized_value"] = "model:invented"
        self.assertInvalid(case["contract"], case["evidence_catalog"])

        case["contract"]["atoms"][0]["normalized_value"] = expected
        normalized = validate_and_normalize_contract(
            case["contract"], case["evidence_catalog"]
        )
        self.assertEqual(normalized["atoms"][0]["normalized_value"], expected)

    def test_only_authenticated_exact_accepted_evidence_spans_are_valid(self):
        base = self.case("latex_capability_without_math_domain")

        case = copy.deepcopy(base)
        case["evidence_catalog"][0]["authority"] = "model_claimed"
        self.assertInvalid(case["contract"], case["evidence_catalog"])

        case = copy.deepcopy(base)
        case["evidence_catalog"][0]["text_sha256"] = "0" * 64
        self.assertInvalid(case["contract"], case["evidence_catalog"])

        case = copy.deepcopy(base)
        case["contract"]["atoms"][0]["evidence"][0]["source_id"] = "unknown"
        self.assertInvalid(case["contract"], case["evidence_catalog"])

        case = copy.deepcopy(base)
        case["contract"]["atoms"][0]["evidence"][0]["end"] -= 1
        self.assertInvalid(case["contract"], case["evidence_catalog"])

        case = copy.deepcopy(base)
        case["contract"]["atoms"][0]["evidence"][0]["quote"] = "LaTeX"
        self.assertInvalid(case["contract"], case["evidence_catalog"])

        case = copy.deepcopy(base)
        case["contract"]["evidence_catalog"] = case["evidence_catalog"]
        self.assertInvalid(case["contract"], case["evidence_catalog"])

    def test_server_evidence_rules_reject_semantic_family_invention(self):
        latex = self.case("latex_capability_without_math_domain")
        atom = latex["contract"]["atoms"][0]
        atom["kind"] = "domain_expertise"
        atom["typed_payload"] = {"domain": "mathematics"}
        self.assertInvalid(latex["contract"], latex["evidence_catalog"])

        qa = self.case("generic_qa_fact_checking")
        atom = qa["contract"]["atoms"][0]
        atom["typed_payload"] = {
            "activity": "software_testing",
            "artifact": "platform_feature",
        }
        self.assertInvalid(qa["contract"], qa["evidence_catalog"])

        genuine = self.project("genuine_software_testing")
        self.assertEqual(genuine["legacy_patch"], {})
        self.assertEqual(
            genuine["group_outcomes"][0]["reason"],
            "role_activity_artifact_not_legacy_projectable",
        )
        self.assertEqual(
            genuine["normalized_contract"]["atoms"][0]["typed_payload"]["artifact"],
            "platform_feature",
        )

    def test_modality_is_derived_from_evidence_not_trusted_from_the_contract(self):
        required_case = self.case("required_asset_access")
        required_case["contract"]["constraint_groups"][0]["modality"] = "preferred"
        self.assertInvalid(
            required_case["contract"], required_case["evidence_catalog"]
        )

        preferred_case = self.case("required_capability_preferred_experience_control")
        preferred_case["contract"]["constraint_groups"][1]["modality"] = "required"
        self.assertInvalid(
            preferred_case["contract"], preferred_case["evidence_catalog"]
        )

        role_case = self.case("genuine_software_testing")
        role_case["contract"]["constraint_groups"][0]["modality"] = "required"
        self.assertInvalid(role_case["contract"], role_case["evidence_catalog"])

    def test_closed_temporals_are_kind_and_evidence_authoritative(self):
        distinction = self.project("required_capability_preferred_experience_control")
        atoms = {
            atom["id"]: atom
            for atom in distinction["normalized_contract"]["atoms"]
        }
        self.assertEqual(atoms["fact_check_capability"]["temporal"], "unspecified")
        self.assertEqual(atoms["fact_check_experience"]["temporal"], "prior")

        role = self.project("genuine_software_testing")
        self.assertEqual(
            role["normalized_contract"]["atoms"][0]["temporal"], "unspecified"
        )

        by_start = self.project("professional_license_with_jurisdiction")
        self.assertEqual(
            by_start["normalized_contract"]["atoms"][0]["temporal"], "by_start"
        )

        ongoing_source = review_source(
            "review:temporal:ongoing-authorization",
            "Candidates must remain independently authorized throughout the engagement in their jurisdiction without sponsorship.",
            "temporal",
        )
        ongoing_contract = {
            "contract_version": CONTRACT_VERSION,
            "atoms": [
                {
                    "id": "ongoing_authorization",
                    "subject": "candidate",
                    "kind": "work_authorization",
                    "typed_payload": {
                        "authorization": "independent_without_sponsorship",
                        "jurisdiction": "candidate_jurisdiction",
                    },
                    "polarity": "affirmed",
                    "temporal": "ongoing",
                    "evidence": [whole_source_ref(ongoing_source)],
                }
            ],
            "constraint_groups": [
                {
                    "modality": "required",
                    "any_of": [{"all_of": ["ongoing_authorization"]}],
                }
            ],
        }
        ongoing = project_legacy_compatibility(ongoing_contract, [ongoing_source])
        self.assertEqual(
            ongoing["normalized_contract"]["atoms"][0]["temporal"], "ongoing"
        )
        self.assertEqual(
            ongoing["group_outcomes"][0]["state"], "grounded_unprojected"
        )

        case = self.case("genuine_historical_experience")
        case["contract"]["atoms"][0]["temporal"] = "current"
        self.assertInvalid(case["contract"], case["evidence_catalog"])

        case = self.case("latex_capability_without_math_domain")
        case["contract"]["atoms"][0]["temporal"] = "prior"
        self.assertInvalid(case["contract"], case["evidence_catalog"])

        case = self.case("required_asset_access")
        case["contract"]["atoms"][0]["temporal"] = "by_start"
        self.assertInvalid(case["contract"], case["evidence_catalog"])

    def test_dnf_is_bounded_unique_and_owns_each_atom_once(self):
        base = self.case("advanced_degree_or_professional_standing")

        case = copy.deepcopy(base)
        case["contract"]["constraint_groups"][0]["any_of"] = [
            {"all_of": ["advanced_degree"]}
            for _ in range(MAX_ALTERNATIVES_PER_GROUP + 1)
        ]
        self.assertInvalid(case["contract"], case["evidence_catalog"])

        case = copy.deepcopy(base)
        case["contract"]["constraint_groups"][0]["any_of"][0]["all_of"] = [
            "advanced_degree"
        ] * (MAX_ATOMS_PER_CONJUNCTION + 1)
        self.assertInvalid(case["contract"], case["evidence_catalog"])

        case = copy.deepcopy(base)
        case["contract"]["constraint_groups"].append(
            {"modality": "required", "any_of": [{"all_of": ["advanced_degree"]}]}
        )
        self.assertInvalid(case["contract"], case["evidence_catalog"])

        case = copy.deepcopy(base)
        case["contract"]["constraint_groups"][0]["any_of"][0]["all_of"] = [
            "missing_atom"
        ]
        self.assertInvalid(case["contract"], case["evidence_catalog"])

    def test_or_and_three_valued_evaluation_preserves_dnf(self):
        group = {
            "modality": "required",
            "any_of": [
                {"all_of": ["a", "b"]},
                {"all_of": ["c"]},
            ],
        }
        examples = (
            ({"a": "true", "b": "true", "c": "false"}, "true"),
            ({"a": "true", "b": "unknown", "c": "false"}, "unknown"),
            ({"a": "false", "b": "unknown", "c": "false"}, "false"),
            ({"a": "false", "b": "false", "c": "true"}, "true"),
            ({"a": "true", "c": "false"}, "unknown"),
        )
        for atom_truth, expected in examples:
            with self.subTest(atom_truth=atom_truth):
                self.assertEqual(
                    evaluate_constraint_group(group, atom_truth), expected
                )

        preferred = copy.deepcopy(group)
        preferred["modality"] = "preferred"
        self.assertEqual(
            evaluate_constraint_group(
                preferred, {"a": "true", "b": "true", "c": "false"}
            ),
            "true",
        )
        with self.assertRaises(SemanticContractValidationError):
            evaluate_constraint_group(group, {"a": "maybe", "b": "true", "c": "false"})

    def test_mixed_kind_alternatives_are_retained_and_never_flattened(self):
        for case_id, kinds in (
            (
                "advanced_degree_or_professional_standing",
                {"education", "professional_standing"},
            ),
            (
                "sports_background_or_interest",
                {"experience", "interest_involvement"},
            ),
        ):
            with self.subTest(case=case_id):
                result = self.project(case_id)
                group = result["normalized_contract"]["constraint_groups"][0]
                self.assertEqual(len(group["any_of"]), 2)
                atom_by_id = {
                    atom["id"]: atom
                    for atom in result["normalized_contract"]["atoms"]
                }
                actual_kinds = {
                    atom_by_id[alternative["all_of"][0]]["kind"]
                    for alternative in group["any_of"]
                }
                self.assertEqual(actual_kinds, kinds)
                self.assertEqual(result["legacy_patch"], {})
                self.assertEqual(
                    result["group_outcomes"][0]["reason"],
                    "logical_shape_not_representable",
                )

    def test_language_locale_identity_and_dialect_expertise_stay_separate(self):
        result = self.project("dialect_expertise_and_language_locale")
        paths = flatten_patch_paths(result["legacy_patch"])
        self.assertEqual(
            paths["attributes.requirements.skills_required"],
            ["Spanish (Cordobes) dialect expertise"],
        )
        self.assertNotIn("attributes.requirements.languages", paths)
        atoms = {
            atom["id"]: atom for atom in result["normalized_contract"]["atoms"]
        }
        self.assertEqual(
            atoms["chinese_taiwan"]["typed_payload"],
            {"language": "chinese", "locale": "taiwan", "proficiency": "fluent"},
        )

        case = self.case("dialect_expertise_and_language_locale")
        case["contract"]["atoms"][0]["typed_payload"]["language"] = "spanish"
        self.assertInvalid(case["contract"], case["evidence_catalog"])

    def test_language_dnf_stays_grounded_when_proficiency_cannot_project(self):
        conjunction = self.project("bilingual_all_required_control")
        self.assertEqual(conjunction["legacy_patch"], {})
        self.assertEqual(
            conjunction["normalized_contract"]["constraint_groups"][0]["any_of"],
            [{"all_of": ["japanese", "korean"]}],
        )

        alternatives = self.project("language_or_control")
        self.assertEqual(alternatives["legacy_patch"], {})
        self.assertEqual(
            alternatives["normalized_contract"]["constraint_groups"][0]["any_of"],
            [{"all_of": ["english"]}, {"all_of": ["french"]}],
        )

    def test_lossy_payload_discriminators_fail_closed(self):
        language = self.project("dialect_expertise_and_language_locale")
        self.assertEqual(
            language["group_outcomes"][0]["reason"],
            "language_proficiency_not_losslessly_legacy_projectable",
        )
        self.assertEqual(
            language["normalized_contract"]["atoms"][0]["typed_payload"]["proficiency"],
            "fluent",
        )

        experience_source = review_source(
            "review:lossless:experience-years",
            "At least 5 years of litigation experience is required.",
            "lossless",
        )
        experience_contract = single_atom_contract(
            experience_source,
            {
                "id": "litigation_five_years",
                "subject": "candidate",
                "kind": "experience",
                "typed_payload": {"area": "litigation", "minimum_years": 5},
                "polarity": "affirmed",
                "temporal": "prior",
            },
        )
        experience = project_legacy_compatibility(
            experience_contract, [experience_source]
        )
        self.assertEqual(experience["legacy_patch"], {})
        self.assertEqual(
            experience["group_outcomes"][0]["reason"],
            "experience_minimum_years_not_legacy_projectable",
        )

        status_source = review_source(
            "review:lossless:business-owner",
            "Current business owner status is required.",
            "lossless",
        )
        status_contract = single_atom_contract(
            status_source,
            {
                "id": "business_owner",
                "subject": "candidate",
                "kind": "professional_status",
                "typed_payload": {"status": "owner", "scope": "business"},
                "polarity": "affirmed",
                "temporal": "current",
            },
        )
        status = project_legacy_compatibility(status_contract, [status_source])
        self.assertEqual(
            flatten_patch_paths(status["legacy_patch"])[
                "attributes.requirements.current_status_requirements"
            ],
            ["Current business owner"],
        )
        wrong_scope = copy.deepcopy(status_contract)
        wrong_scope["atoms"][0]["typed_payload"]["scope"] = "professional_practice"
        self.assertInvalid(wrong_scope, [status_source])

        credential = self.project("professional_license_with_jurisdiction")
        self.assertEqual(credential["legacy_patch"], {})
        self.assertEqual(
            credential["group_outcomes"][0]["reason"],
            "credential_jurisdiction_not_legacy_projectable",
        )

        role = self.project("genuine_software_testing")
        self.assertEqual(role["legacy_patch"], {})
        self.assertEqual(
            role["group_outcomes"][0]["reason"],
            "role_activity_artifact_not_legacy_projectable",
        )

        asset = self.project("required_asset_access")
        self.assertEqual(asset["legacy_patch"], {})
        self.assertEqual(
            asset["group_outcomes"][0]["reason"],
            "asset_access_has_no_exact_legacy_field",
        )

    def test_locale_and_dialect_expertise_are_distinct_and_lossless(self):
        dialect = self.project("dialect_expertise_and_language_locale")
        self.assertEqual(
            flatten_patch_paths(dialect["legacy_patch"])[
                "attributes.requirements.skills_required"
            ],
            ["Spanish (Cordobes) dialect expertise"],
        )

        locale_source = review_source(
            "review:locale:cordobes-required",
            "Cordobes locale expertise is required.",
            "locale",
        )
        locale_contract = single_atom_contract(
            locale_source,
            {
                "id": "cordobes_locale",
                "subject": "candidate",
                "kind": "locale_dialect_expertise",
                "typed_payload": {
                    "language": "spanish",
                    "locale": "cordobes",
                    "expertise": "locale",
                },
                "polarity": "affirmed",
                "temporal": "unspecified",
            },
        )
        locale = project_legacy_compatibility(locale_contract, [locale_source])
        self.assertEqual(
            flatten_patch_paths(locale["legacy_patch"])[
                "attributes.requirements.skills_required"
            ],
            ["Spanish (Cordobes) locale expertise"],
        )

    def test_required_capability_and_historical_experience_project_separately(self):
        result = self.project("required_capability_preferred_experience_control")
        paths = flatten_patch_paths(result["legacy_patch"])
        self.assertEqual(
            paths["attributes.requirements.skills_required"], ["Fact-checking"]
        )
        self.assertEqual(
            paths["attributes.requirements.experience_preferred"],
            ["Fact-checking experience"],
        )
        self.assertNotIn("attributes.requirements.experience_required", paths)

    def test_preferred_groups_never_project_to_hard_constraint_paths(self):
        for case_id in (
            "required_capability_preferred_experience_control",
            "education_required_preferred_control",
        ):
            result = self.project(case_id)
            groups = result["normalized_contract"]["constraint_groups"]
            for outcome in result["group_outcomes"]:
                if groups[outcome["group_index"]]["modality"] != "preferred":
                    continue
                self.assertTrue(
                    set(outcome["field_paths"]).isdisjoint(HARD_LEGACY_FIELD_PATHS)
                )

        work_authorization = self.project("independent_work_authorization")
        self.assertEqual(work_authorization["legacy_patch"], {})
        self.assertEqual(
            work_authorization["group_outcomes"][0]["state"],
            "grounded_unprojected",
        )

    def test_disclaimed_or_mixed_modality_cannot_create_a_hard_gate(self):
        disclaimed = review_source(
            "review:modality:not-required",
            "Prior software testing experience is not required.",
            "modality",
        )
        contract = {
            "contract_version": CONTRACT_VERSION,
            "atoms": [
                {
                    "id": "software_testing_experience",
                    "subject": "candidate",
                    "kind": "experience",
                    "typed_payload": {"area": "software_testing"},
                    "polarity": "affirmed",
                    "temporal": "prior",
                    "evidence": [whole_source_ref(disclaimed)],
                }
            ],
            "constraint_groups": [
                {
                    "modality": "required",
                    "any_of": [{"all_of": ["software_testing_experience"]}],
                }
            ],
        }
        self.assertInvalid(contract, [disclaimed])

        mixed = review_source(
            "review:modality:mixed",
            "Python programming capability is required and preferred.",
            "modality",
        )
        contract = {
            "contract_version": CONTRACT_VERSION,
            "atoms": [
                {
                    "id": "python",
                    "subject": "candidate",
                    "kind": "capability",
                    "typed_payload": {"capability": "python_programming"},
                    "polarity": "affirmed",
                    "temporal": "unspecified",
                    "evidence": [whole_source_ref(mixed)],
                }
            ],
            "constraint_groups": [
                {"modality": "required", "any_of": [{"all_of": ["python"]}]}
            ],
        }
        self.assertInvalid(contract, [mixed])

    def test_unknown_unprojected_and_conflicted_are_distinct(self):
        silence = self.project("model_silence_control")
        self.assertEqual(silence["legacy_patch"], {})
        self.assertEqual(set(silence["knowledge_by_kind"].values()), {"unknown"})

        unprojected = self.project("contractor_registration")
        self.assertEqual(
            unprojected["knowledge_by_kind"]["regulatory_registration"],
            "grounded_unprojected",
        )
        self.assertEqual(unprojected["group_outcomes"][0]["state"], "grounded_unprojected")

        conflict = self.project("conflicting_polarity_control")
        self.assertEqual(conflict["knowledge_by_kind"]["capability"], "conflicted")
        self.assertEqual(
            {outcome["state"] for outcome in conflict["group_outcomes"]},
            {"conflicted"},
        )
        self.assertEqual(conflict["legacy_patch"], {})

    def test_scalar_projection_conflicts_fail_closed(self):
        bachelor = review_source(
            "review:scalar:bachelor", "A bachelor's degree is required.", "scalar"
        )
        master = review_source(
            "review:scalar:master", "A master's degree is required.", "scalar"
        )
        contract = {
            "contract_version": CONTRACT_VERSION,
            "atoms": [
                {
                    "id": "bachelor",
                    "subject": "candidate",
                    "kind": "education",
                    "typed_payload": {"level": "bachelor"},
                    "polarity": "affirmed",
                    "temporal": "unspecified",
                    "evidence": [whole_source_ref(bachelor)],
                },
                {
                    "id": "master",
                    "subject": "candidate",
                    "kind": "education",
                    "typed_payload": {"level": "master"},
                    "polarity": "affirmed",
                    "temporal": "unspecified",
                    "evidence": [whole_source_ref(master)],
                },
            ],
            "constraint_groups": [
                {"modality": "required", "any_of": [{"all_of": ["bachelor"]}]},
                {"modality": "required", "any_of": [{"all_of": ["master"]}]},
            ],
        }
        result = project_legacy_compatibility(contract, [bachelor, master])

        self.assertEqual(result["legacy_patch"], {})
        self.assertEqual(
            [outcome["state"] for outcome in result["group_outcomes"]],
            ["grounded_unprojected", "grounded_unprojected"],
        )
        self.assertEqual(
            [outcome["reason"] for outcome in result["group_outcomes"]],
            ["legacy_projection_conflict", "legacy_projection_conflict"],
        )
        self.assertEqual(
            result["knowledge_by_kind"]["education"], "grounded_unprojected"
        )

    def test_polarity_conflict_is_logical_context_and_modality_aware(self):
        alternative = self.case("conflicting_polarity_control")
        alternative["contract"]["constraint_groups"] = [
            {
                "modality": "required",
                "any_of": [
                    {"all_of": ["python_affirmed"]},
                    {"all_of": ["python_negated"]},
                ],
            }
        ]
        alternative_result = project_legacy_compatibility(
            alternative["contract"], alternative["evidence_catalog"]
        )
        self.assertEqual(
            alternative_result["group_outcomes"][0]["state"],
            "grounded_unprojected",
        )
        self.assertNotEqual(
            alternative_result["group_outcomes"][0]["reason"],
            "contradictory_required_assertions",
        )

        contradictory = self.project("conflicting_polarity_control")
        self.assertEqual(
            {outcome["reason"] for outcome in contradictory["group_outcomes"]},
            {"contradictory_required_assertions"},
        )

        preferred_negative = self.case("conflicting_polarity_control")
        preferred_negative["evidence_catalog"][1] = review_source(
            "review:control:python-negated-preferred",
            "It is preferred that candidates cannot have Python programming capability.",
            "polarity",
        )
        preferred_negative["contract"]["atoms"][1]["evidence"] = [
            whole_source_ref(preferred_negative["evidence_catalog"][1])
        ]
        preferred_negative["contract"]["constraint_groups"][1][
            "modality"
        ] = "preferred"
        preferred_result = project_legacy_compatibility(
            preferred_negative["contract"], preferred_negative["evidence_catalog"]
        )
        self.assertNotIn(
            "conflicted", {item["state"] for item in preferred_result["group_outcomes"]}
        )

    def test_independent_language_groups_never_flatten_their_logic(self):
        west = review_source(
            "review:language:west",
            "Fluency in English or French is required.",
            "language-groups",
        )
        east = review_source(
            "review:language:east",
            "Fluency in Japanese or Korean is required.",
            "language-groups",
        )

        def language_atom(atom_id, language, source):
            return {
                "id": atom_id,
                "subject": "candidate",
                "kind": "language_proficiency",
                "typed_payload": {
                    "language": language,
                    "locale": None,
                    "proficiency": "fluent",
                },
                "polarity": "affirmed",
                "temporal": "unspecified",
                "evidence": [whole_source_ref(source)],
            }

        contract = {
            "contract_version": CONTRACT_VERSION,
            "atoms": [
                language_atom("english", "english", west),
                language_atom("french", "french", west),
                language_atom("japanese", "japanese", east),
                language_atom("korean", "korean", east),
            ],
            "constraint_groups": [
                {
                    "modality": "required",
                    "any_of": [
                        {"all_of": ["english"]},
                        {"all_of": ["french"]},
                    ],
                },
                {
                    "modality": "required",
                    "any_of": [
                        {"all_of": ["japanese"]},
                        {"all_of": ["korean"]},
                    ],
                },
            ],
        }
        result = project_legacy_compatibility(contract, [west, east])
        self.assertEqual(result["legacy_patch"], {})
        self.assertEqual(
            result["normalized_contract"]["constraint_groups"],
            contract["constraint_groups"],
        )
        self.assertEqual(
            [outcome["state"] for outcome in result["group_outcomes"]],
            ["grounded_unprojected", "grounded_unprojected"],
        )

    def test_unrelated_evidence_spans_cannot_launder_semantic_support(self):
        latex_only = review_source(
            "review:launder:a-latex", "LaTeX capability.", "evidence-laundering"
        )
        typesetting_required = review_source(
            "review:launder:b-typesetting",
            "Document typesetting is required.",
            "evidence-laundering",
        )
        contract = single_atom_contract(
            latex_only,
            {
                "id": "latex",
                "subject": "candidate",
                "kind": "capability",
                "typed_payload": {"capability": "latex_typesetting"},
                "polarity": "affirmed",
                "temporal": "unspecified",
            },
        )
        contract["atoms"][0]["evidence"].append(
            whole_source_ref(typesetting_required)
        )
        self.assertInvalid(contract, [latex_only, typesetting_required])

        python_required = review_source(
            "review:launder:c-python",
            "Python programming capability is required.",
            "evidence-laundering",
        )
        timing_only = review_source(
            "review:launder:d-timing",
            "This must be obtained by the engagement start.",
            "evidence-laundering",
        )
        timing_contract = single_atom_contract(
            python_required,
            {
                "id": "python_by_start",
                "subject": "candidate",
                "kind": "capability",
                "typed_payload": {"capability": "python_programming"},
                "polarity": "affirmed",
                "temporal": "by_start",
            },
        )
        timing_contract["atoms"][0]["evidence"].append(whole_source_ref(timing_only))
        self.assertInvalid(timing_contract, [python_required, timing_only])

        negation_only = review_source(
            "review:launder:e-negation",
            "Candidates must not have it.",
            "evidence-laundering",
        )
        polarity_contract = single_atom_contract(
            python_required,
            {
                "id": "no_python",
                "subject": "candidate",
                "kind": "capability",
                "typed_payload": {"capability": "python_programming"},
                "polarity": "negated",
                "temporal": "unspecified",
            },
        )
        polarity_contract["atoms"][0]["evidence"].append(
            whole_source_ref(negation_only)
        )
        self.assertInvalid(polarity_contract, [python_required, negation_only])

    def test_professional_credentials_and_regulatory_registration_are_separate(self):
        self.assertIn("professional_credential", ATOM_KINDS)
        self.assertIn("regulatory_registration", ATOM_KINDS)
        self.assertNotIn("credential_registration", ATOM_KINDS)

        credential_source = review_source(
            "review:credential:certification",
            "An industry certification is required.",
            "credential-split",
        )
        credential_contract = single_atom_contract(
            credential_source,
            {
                "id": "certification",
                "subject": "candidate",
                "kind": "professional_credential",
                "typed_payload": {"credential": "industry_certification"},
                "polarity": "affirmed",
                "temporal": "unspecified",
            },
        )
        credential = project_legacy_compatibility(
            credential_contract, [credential_source]
        )
        self.assertEqual(
            flatten_patch_paths(credential["legacy_patch"])[
                "attributes.requirements.credentials"
            ],
            ["Industry certification"],
        )

        registration = self.project("contractor_registration")
        self.assertEqual(registration["legacy_patch"], {})
        self.assertEqual(
            registration["normalized_contract"]["atoms"][0]["kind"],
            "regulatory_registration",
        )
        missing_jurisdiction = self.case("contractor_registration")
        del missing_jurisdiction["contract"]["atoms"][0]["typed_payload"][
            "jurisdiction"
        ]
        self.assertInvalid(
            missing_jurisdiction["contract"],
            missing_jurisdiction["evidence_catalog"],
        )

    def test_mixed_projected_and_unprojected_atoms_have_a_truthful_kind_state(self):
        fact_check = review_source(
            "review:mixed:fact-check",
            "Fact-checking capability is required.",
            "mixed-state",
        )
        python_by_start = review_source(
            "review:mixed:python-by-start",
            "Python programming capability is required by the engagement start.",
            "mixed-state",
        )
        contract = {
            "contract_version": CONTRACT_VERSION,
            "atoms": [
                {
                    "id": "fact_check",
                    "subject": "candidate",
                    "kind": "capability",
                    "typed_payload": {"capability": "fact_checking"},
                    "polarity": "affirmed",
                    "temporal": "unspecified",
                    "evidence": [whole_source_ref(fact_check)],
                },
                {
                    "id": "python_by_start",
                    "subject": "candidate",
                    "kind": "capability",
                    "typed_payload": {"capability": "python_programming"},
                    "polarity": "affirmed",
                    "temporal": "by_start",
                    "evidence": [whole_source_ref(python_by_start)],
                },
            ],
            "constraint_groups": [
                {"modality": "required", "any_of": [{"all_of": ["fact_check"]}]},
                {
                    "modality": "required",
                    "any_of": [{"all_of": ["python_by_start"]}],
                },
            ],
        }
        result = project_legacy_compatibility(
            contract, [fact_check, python_by_start]
        )
        self.assertEqual(
            result["knowledge_by_kind"]["capability"], "grounded_mixed"
        )
        self.assertEqual(
            [outcome["state"] for outcome in result["group_outcomes"]],
            ["projected", "grounded_unprojected"],
        )
        self.assertEqual(
            flatten_patch_paths(result["legacy_patch"])[
                "attributes.requirements.skills_required"
            ],
            ["Fact-checking"],
        )

    def test_temporal_vocabulary_and_kind_domains_are_closed(self):
        self.assertEqual(
            TEMPORALS,
            {"current", "prior", "by_start", "ongoing", "unspecified"},
        )
        self.assertEqual(TEMPORALS_BY_KIND["experience"], {"prior"})
        self.assertEqual(
            TEMPORALS_BY_KIND["role_activity"], {"ongoing", "unspecified"}
        )

    def test_sparse_projection_never_turns_silence_into_known_empty(self):
        forbidden_keys = {"known_empty", "known_empty_fields", "unknown_fields"}
        for case in self.fixture["cases"]:
            with self.subTest(case=case["id"]):
                result = project_legacy_compatibility(
                    case["contract"], case["evidence_catalog"]
                )

                def walk(value):
                    if type(value) is dict:
                        self.assertTrue(forbidden_keys.isdisjoint(value))
                        for child in value.values():
                            walk(child)
                    elif type(value) is list:
                        self.assertTrue(value)
                        for child in value:
                            walk(child)

                walk(result["legacy_patch"])
        self.assertEqual(self.project("model_silence_control")["legacy_patch"], {})


if __name__ == "__main__":
    unittest.main()

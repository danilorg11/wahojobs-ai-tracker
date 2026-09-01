import ast
import copy
import json
from pathlib import Path
import unittest

from wahojobs.matching.foundation_contracts import (
    DeterministicEligibilityDecisionV1,
    GroundedFactV1,
    ShortlistCandidateV1,
    contract_fingerprint,
)
from wahojobs.matching.semantic_shadow import (
    MAX_SHADOW_SHORTLIST_SIZE_V1,
    NormalizedProfileSemanticSignalV1,
    SemanticMatchingShadowContractError,
    SemanticMatchingShadowRequestV1,
    run_semantic_matching_shadow_v1,
    semantic_shadow_candidate_from_packet_v1,
)
from wahojobs.matching.typed_criteria import CriterionOutcomeV1
from wahojobs.opportunity_enrichment import SEMANTIC_INPUT_VERSION
from wahojobs.opportunity_semantic_authority import (
    AUTHORITY_POLICY_VERSION,
    SEMANTIC_AUTHORITY_TYPE,
    build_semantic_matching_packet,
    build_semantic_matching_packet_from_staging,
    canonical_sha256,
    derive_server_variant_relationships,
)
from wahojobs.opportunity_semantic_staging import replay_case


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
PROFILE_REVISION_REF = "profile_revision:semantic_shadow_fixture"


def criterion_outcome(criterion_id, dimension, outcome):
    return CriterionOutcomeV1(
        criterion_id=criterion_id,
        criterion_class="eligibility",
        dimension=dimension,
        outcome=outcome,
        reason_code=f"fixture_{outcome}",
        potentially_relaxable=False,
    )


def eligibility_decision(opportunity_ref, *, language="pass"):
    outcomes = (
        criterion_outcome(
            "eligibility.credentials_licenses",
            "credential_eligibility",
            "not_applicable",
        ),
        criterion_outcome(
            "eligibility.location",
            "location_eligibility",
            "pass",
        ),
        criterion_outcome(
            "eligibility.required_languages",
            "required_language_eligibility",
            language,
        ),
    )
    return DeterministicEligibilityDecisionV1.from_outcomes(
        opportunity_ref=opportunity_ref,
        policy_version="eligibility_policy_v1",
        outcomes=outcomes,
    )


def shortlist_candidate(opportunity_ref, canonical_id, *, language="pass"):
    return ShortlistCandidateV1(
        opportunity_ref=opportunity_ref,
        canonical_opportunity_id=canonical_id,
        selected_job_id=10_000 + canonical_id,
        variant_group_ref=f"canonical_group:{canonical_id}",
        variant_disposition="singleton",
        enrichment_fingerprint=(f"{canonical_id:064x}")[-64:],
        eligibility=eligibility_decision(opportunity_ref, language=language),
        retrieval_channels=("legacy_ranked_pool",),
    )


def profile_fact(terms):
    return GroundedFactV1(
        fact_ref="profile_fact:semantic_shadow_fixture",
        field_path="profile.derived_matcher_signals",
        value=sorted(set(terms)),
        provenance="user_confirmed",
        source_refs=(PROFILE_REVISION_REF,),
    )


def profile_signal(index, kind, *terms):
    return NormalizedProfileSemanticSignalV1(
        signal_ref=f"profile_signal:fixture_{index:03d}",
        profile_fact_ref="profile_fact:semantic_shadow_fixture",
        semantic_kind=kind,
        semantic_terms=tuple(sorted(set(terms))),
    )


class SemanticMatchingShadowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gold = json.loads(GOLD_PATH.read_text(encoding="utf-8"))
        cls.gold_by_id = {case["id"]: case for case in cls.gold["cases"]}
        cls.raw = json.loads(RAW_PATH.read_text(encoding="utf-8"))
        cls.review = json.loads(REVIEW_PATH.read_text(encoding="utf-8"))

    def reviewed_packet(
        self,
        case_id,
        opportunity_ref,
        *,
        variant_refs,
        source_variant_refs=None,
    ):
        case = self.gold_by_id[case_id]
        source_variant_refs = source_variant_refs or {
            source["id"]: list(variant_refs) for source in case["evidence_catalog"]
        }
        relationships = [
            {
                "source_id": source["id"],
                "evidence_block_id": f"fixture_block_{index:03d}",
                "derivation": "server_authenticated_evidence_binding",
                "variant_refs": sorted(source_variant_refs[source["id"]]),
                "source_refs": sorted(source_variant_refs[source["id"]]),
                "authority_refs": ["fixture:reviewed_semantic_gold"],
            }
            for index, source in enumerate(case["evidence_catalog"], start=1)
        ]
        semantic_bundle = {
            "contract": case["contract"],
            "evidence_catalog": case["evidence_catalog"],
        }
        return build_semantic_matching_packet(
            case["contract"],
            case["evidence_catalog"],
            canonical_ref=opportunity_ref,
            known_variant_refs=sorted(variant_refs),
            semantic_input_version=self.gold["fixture_version"],
            semantic_input_sha256=canonical_sha256(semantic_bundle),
            source_packet_sha256=canonical_sha256(case["evidence_catalog"]),
            variant_relationships=relationships,
        )

    def incomplete_reviewed_packet(self, case_id, opportunity_ref):
        case = self.raw["cases"][case_id]
        replay = replay_case(case, self.review["cases"][case_id])
        relationships = derive_server_variant_relationships(
            case["source_packet"],
            case["accepted_evidence_bindings"],
            replay["staging"]["accepted_evidence_catalog"],
        )
        variants = sorted(
            item["variant_ref"] for item in case["source_packet"]["variants"]
        )
        packet = build_semantic_matching_packet_from_staging(
            replay["staging"],
            replay["relations"],
            canonical_ref=opportunity_ref,
            known_variant_refs=variants,
            semantic_input_version=SEMANTIC_INPUT_VERSION,
            semantic_input_sha256=case["semantic_input_sha256"],
            source_packet_sha256=case["source_packet_sha256"],
            semantic_extraction_version=case["raw_extraction"]["extraction_version"],
            semantic_grouping_version=case["raw_grouping"]["grouping_version"],
            variant_relationships=relationships,
        )
        return packet, variants[0]

    def candidate(
        self,
        canonical_id,
        legacy_rank,
        *,
        packet=None,
        selected_variant_ref=None,
        language="pass",
    ):
        opportunity_ref = f"canonical_opportunity:{canonical_id}"
        selected_variant_ref = selected_variant_ref or f"job:{10_000 + canonical_id}"
        return semantic_shadow_candidate_from_packet_v1(
            shortlist_candidate=shortlist_candidate(
                opportunity_ref, canonical_id, language=language
            ),
            legacy_rank=legacy_rank,
            selected_variant_ref=selected_variant_ref,
            packet=packet,
        )

    def request(self, candidates, *, signals=(), terms=("fixture",)):
        return SemanticMatchingShadowRequestV1(
            request_ref="semantic_shadow_request:fixture",
            profile_revision_ref=PROFILE_REVISION_REF,
            profile_contract_version="canonical_profile_v2",
            profile_fingerprint="f" * 64,
            taxonomy_version="opportunity_taxonomy_v2_2026_08",
            shortlist_limit=MAX_SHADOW_SHORTLIST_SIZE_V1,
            profile_facts=(profile_fact(terms),),
            profile_signals=tuple(sorted(signals, key=lambda item: item.signal_ref)),
            candidates=tuple(sorted(candidates, key=lambda item: item.opportunity_ref)),
        )

    def test_preferred_semantic_support_changes_relative_rank_without_exclusion(self):
        first_ref = "canonical_opportunity:101"
        second_ref = "canonical_opportunity:102"
        first_variant = "job:10101"
        second_variant = "job:10102"
        required_only = self.reviewed_packet(
            "advanced_degree_or_professional_standing",
            first_ref,
            variant_refs=[first_variant],
        )
        required_and_preferred = self.reviewed_packet(
            "required_capability_preferred_experience_control",
            second_ref,
            variant_refs=[second_variant],
        )
        signals = (
            profile_signal(1, "education", "advanced_degree"),
            profile_signal(2, "capability", "fact_checking"),
            profile_signal(3, "experience", "fact_checking"),
        )
        request = self.request(
            (
                self.candidate(
                    101,
                    1,
                    packet=required_only,
                    selected_variant_ref=first_variant,
                ),
                self.candidate(
                    102,
                    2,
                    packet=required_and_preferred,
                    selected_variant_ref=second_variant,
                ),
                self.candidate(103, 3),
            ),
            signals=signals,
            terms=("advanced_degree", "fact_checking"),
        )
        result = run_semantic_matching_shadow_v1(request)

        self.assertEqual(
            [item.opportunity_ref for item in result.items],
            [second_ref, first_ref, "canonical_opportunity:103"],
        )
        by_ref = {item.opportunity_ref: item for item in result.items}
        self.assertEqual(by_ref[first_ref].supported_required_group_count, 1)
        self.assertEqual(by_ref[first_ref].supported_preferred_group_count, 0)
        self.assertEqual(by_ref[second_ref].supported_required_group_count, 1)
        self.assertEqual(by_ref[second_ref].supported_preferred_group_count, 1)
        self.assertEqual(len(result.items), len(request.candidates))
        self.assertEqual(result.invariant_proof["semantic_exclusion_count"], 0)
        self.assertEqual(
            result.invariant_proof["semantic_negative_ranking_factor_count"], 0
        )

    def test_required_and_or_structure_is_preserved_but_never_becomes_a_gate(self):
        or_ref = "canonical_opportunity:201"
        and_ref = "canonical_opportunity:202"
        or_variant = "job:10201"
        and_variant = "job:10202"
        or_packet = self.reviewed_packet(
            "advanced_degree_or_professional_standing",
            or_ref,
            variant_refs=[or_variant],
        )
        and_packet = self.reviewed_packet(
            "bilingual_all_required_control",
            and_ref,
            variant_refs=[and_variant],
        )
        request = self.request(
            (
                self.candidate(
                    201, 1, packet=or_packet, selected_variant_ref=or_variant
                ),
                self.candidate(
                    202, 2, packet=and_packet, selected_variant_ref=and_variant
                ),
            ),
            signals=(
                profile_signal(1, "education", "advanced_degree"),
                profile_signal(
                    2, "language_proficiency", "fluent", "japanese"
                ),
            ),
            terms=("advanced_degree", "fluent", "japanese"),
        )
        result = run_semantic_matching_shadow_v1(request)
        by_ref = {item.opportunity_ref: item for item in result.items}

        or_group = by_ref[or_ref].group_assessments[0]
        self.assertEqual(or_group.logic, or_packet["groups"][0]["logic"])
        self.assertEqual(len(or_group.branches), 2)
        self.assertEqual(or_group.status, "supported")

        and_group = by_ref[and_ref].group_assessments[0]
        self.assertEqual(and_group.logic, and_packet["groups"][0]["logic"])
        self.assertEqual(len(and_group.branches), 1)
        self.assertEqual(
            set(and_group.branches[0].all_of), {"japanese", "korean"}
        )
        self.assertEqual(and_group.status, "partially_supported")
        self.assertEqual(by_ref[and_ref].supported_required_group_count, 0)
        self.assertFalse(
            by_ref[and_ref].semantic_authority["candidate_exclusion_authorized"]
        )
        self.assertIn(and_ref, {item.opportunity_ref for item in result.items})

    def test_unavailable_invalid_and_incomplete_packets_are_neutral_and_preserved(self):
        partial_ref = "canonical_opportunity:301"
        partial_packet, partial_variant = self.incomplete_reviewed_packet(
            "1117", partial_ref
        )
        tampered = copy.deepcopy(
            self.reviewed_packet(
                "genuine_software_testing",
                "canonical_opportunity:303",
                variant_refs=["job:10303"],
            )
        )
        tampered["packet_sha256"] = "0" * 64
        request = self.request(
            (
                self.candidate(
                    301,
                    1,
                    packet=partial_packet,
                    selected_variant_ref=partial_variant,
                ),
                self.candidate(302, 2),
                self.candidate(
                    303,
                    3,
                    packet=tampered,
                    selected_variant_ref="job:10303",
                ),
            )
        )
        result = run_semantic_matching_shadow_v1(request)

        self.assertEqual(
            [(item.opportunity_ref, item.relative_rank) for item in result.items],
            [
                ("canonical_opportunity:301", 1),
                ("canonical_opportunity:302", 2),
                ("canonical_opportunity:303", 3),
            ],
        )
        by_ref = {item.opportunity_ref: item for item in result.items}
        self.assertEqual(
            by_ref[partial_ref].packet_coverage_state, "available_partial"
        )
        self.assertTrue(by_ref[partial_ref].packet_coverage_partial)
        self.assertEqual(
            by_ref["canonical_opportunity:302"].semantic_packet_status,
            "unavailable",
        )
        self.assertEqual(
            by_ref["canonical_opportunity:303"].semantic_packet_status, "invalid"
        )
        for item in result.items:
            self.assertEqual(item.positive_support_key, (0, 0, 0, 0, 0))
            self.assertTrue(item.uncertainties)

    def test_deterministic_eligibility_object_and_profile_truth_are_unchanged(self):
        packet = self.reviewed_packet(
            "genuine_software_testing",
            "canonical_opportunity:401",
            variant_refs=["job:10401"],
        )
        candidates = (
            self.candidate(
                401,
                1,
                packet=packet,
                selected_variant_ref="job:10401",
                language="unknown",
            ),
            self.candidate(402, 2),
        )
        request = self.request(candidates)
        profile_before = copy.deepcopy(request.profile_facts[0].as_dict())
        eligibility_before = {
            item.opportunity_ref: contract_fingerprint(
                item.shortlist_candidate.eligibility
            )
            for item in request.candidates
        }
        packet_before = canonical_sha256(packet)

        result = run_semantic_matching_shadow_v1(request)
        result_by_ref = {item.opportunity_ref: item for item in result.items}
        for candidate in request.candidates:
            output = result_by_ref[candidate.opportunity_ref]
            self.assertIs(
                output.deterministic_eligibility,
                candidate.shortlist_candidate.eligibility,
            )
            self.assertEqual(
                contract_fingerprint(output.deterministic_eligibility),
                eligibility_before[candidate.opportunity_ref],
            )
        self.assertEqual(request.profile_facts[0].as_dict(), profile_before)
        self.assertEqual(canonical_sha256(packet), packet_before)
        self.assertEqual(
            result_by_ref["canonical_opportunity:401"].deterministic_eligibility.status,
            "unknown",
        )

    def test_post_contract_packet_mutation_fails_soft_without_losing_survivor(self):
        packet = self.reviewed_packet(
            "genuine_software_testing",
            "canonical_opportunity:451",
            variant_refs=["job:10451"],
        )
        request = self.request(
            (
                self.candidate(
                    451,
                    1,
                    packet=packet,
                    selected_variant_ref="job:10451",
                ),
            )
        )
        request.candidates[0].semantic_packet.packet["packet_sha256"] = "0" * 64

        result = run_semantic_matching_shadow_v1(request)

        self.assertEqual(len(result.items), 1)
        self.assertEqual(
            result.items[0].opportunity_ref, "canonical_opportunity:451"
        )
        self.assertEqual(result.items[0].semantic_packet_status, "invalid")
        self.assertEqual(result.items[0].positive_support_key, (0, 0, 0, 0, 0))

    def test_variant_specific_fact_does_not_become_canonical_support(self):
        opportunity_ref = "canonical_opportunity:501"
        variants = ["job:10501", "job:10502"]
        case = self.gold_by_id[
            "required_capability_preferred_experience_control"
        ]
        source_ids = [item["id"] for item in case["evidence_catalog"]]
        packet = self.reviewed_packet(
            "required_capability_preferred_experience_control",
            opportunity_ref,
            variant_refs=variants,
            source_variant_refs={
                source_ids[0]: [variants[0]],
                source_ids[1]: [variants[1]],
            },
        )
        request = self.request(
            (
                self.candidate(
                    501,
                    1,
                    packet=packet,
                    selected_variant_ref=variants[0],
                ),
            ),
            signals=(profile_signal(1, "experience", "fact_checking"),),
            terms=("fact_checking",),
        )
        result = run_semantic_matching_shadow_v1(request)
        item = result.items[0]
        preferred = next(
            group for group in item.group_assessments if group.modality == "preferred"
        )

        self.assertEqual(preferred.status, "unknown")
        self.assertEqual(item.supported_preferred_group_count, 0)
        self.assertTrue(
            preferred.branches[0].variant_inapplicable_proposition_ids
        )
        preferred_proposition = next(
            proposition
            for proposition in packet["propositions"]
            if proposition["proposition_id"] == "fact_check_experience"
        )
        self.assertEqual(
            preferred_proposition["applicability"]["canonical_applicability"],
            "not_claimed",
        )
        self.assertFalse(
            packet["opportunity_scope"]["canonical_fact_promotion_allowed"]
        )
        self.assertEqual(item.packet_coverage_state, "available_partial")

    def test_profile_signals_must_be_grounded_and_shortlist_is_bounded(self):
        with self.assertRaises(SemanticMatchingShadowContractError):
            self.request(
                (),
                signals=(profile_signal(1, "capability", "invented_truth"),),
                terms=("fixture",),
            )
        with self.assertRaises(SemanticMatchingShadowContractError):
            SemanticMatchingShadowRequestV1(
                request_ref="semantic_shadow_request:too_large",
                profile_revision_ref=PROFILE_REVISION_REF,
                profile_contract_version="canonical_profile_v2",
                profile_fingerprint="f" * 64,
                taxonomy_version="opportunity_taxonomy_v2_2026_08",
                shortlist_limit=MAX_SHADOW_SHORTLIST_SIZE_V1 + 1,
                profile_facts=(profile_fact(("fixture",)),),
                profile_signals=(),
                candidates=(),
            )

    def test_no_runtime_or_ui_module_imports_the_shadow_seam(self):
        forbidden = []
        module_path = ROOT / "wahojobs" / "matching" / "semantic_shadow.py"
        for path in (ROOT / "wahojobs").rglob("*.py"):
            if path == module_path:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                if any(
                    name == "wahojobs.matching.semantic_shadow"
                    or name.startswith("wahojobs.matching.semantic_shadow.")
                    for name in names
                ):
                    forbidden.append(str(path.relative_to(ROOT)))
        self.assertEqual(forbidden, [])
        for path in (
            ROOT / "wahojobs" / "authenticated_profile_matches.py",
            ROOT / "scripts" / "local_product_app.py",
        ):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("semantic_shadow", source)
            self.assertNotIn("semantic_packet", source)

    def test_output_keeps_packet_authority_visible_and_candidate_copy_separate(self):
        packet = self.reviewed_packet(
            "required_capability_preferred_experience_control",
            "canonical_opportunity:601",
            variant_refs=["job:10601"],
        )
        request = self.request(
            (
                self.candidate(
                    601,
                    1,
                    packet=packet,
                    selected_variant_ref="job:10601",
                ),
            ),
            signals=(profile_signal(1, "capability", "fact_checking"),),
            terms=("fact_checking",),
        )
        document = run_semantic_matching_shadow_v1(request).as_dict()
        item = document["items"][0]

        self.assertEqual(
            document["semantic_authority"]["authority_policy_version"],
            AUTHORITY_POLICY_VERSION,
        )
        self.assertEqual(
            item["semantic_authority"]["authority_type"], SEMANTIC_AUTHORITY_TYPE
        )
        self.assertFalse(
            item["semantic_authority"]["hard_eligibility_authorized"]
        )
        self.assertFalse(
            item["semantic_authority"]["candidate_exclusion_authorized"]
        )
        self.assertEqual(item["positive_support"]["negative_factors"], [])
        self.assertTrue(item["grounded_match_explanations"])
        for evidence in item["supporting_evidence"]:
            self.assertNotIn("diagnostic", evidence["candidate_explanation"].lower())
            self.assertNotIn("reason_code", evidence["candidate_explanation"])
        self.assertFalse(document["isolation"]["find_matches_consumption_authorized"])
        self.assertFalse(document["isolation"]["database_persistence_authorized"])


if __name__ == "__main__":
    unittest.main()

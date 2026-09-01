import json
import unittest

from wahojobs.matching.foundation_contracts import (
    DeterministicEligibilityDecisionV1,
    EvidenceLinkV1,
    GroundedFactV1,
    MatchRunCandidateDispositionV1,
    MatchRunResultV1,
    MatchRunSnapshotV1,
    MatchingContractError,
    OPPORTUNITY_ENRICHMENT_FACT_FIELD_PATHS_V1,
    OpportunityRelationshipV1,
    RequirementCheckV1,
    SemanticRerankCandidateInputV1,
    SemanticRerankItemV1,
    SemanticRerankRequestV1,
    SemanticRerankResultV1,
    ShortlistCandidateV1,
    canonical_json,
    contract_fingerprint,
    parse_semantic_rerank_result_json,
    validate_match_run_result,
    validate_semantic_rerank_result,
)
from wahojobs.matching.typed_criteria import CriterionOutcomeV1
from wahojobs.opportunity_enrichment import FIELD_DEFAULTS


class MatchingFoundationContractTests(unittest.TestCase):
    def test_pinned_v1_projection_does_not_silently_adopt_vnext_fields(self):
        vnext_only_fields = frozenset(
            {
                "attributes.requirements.credentials_preferred",
                "attributes.requirements.current_status_requirements",
                "attributes.requirements.education.preferred_levels",
                "attributes.requirements.experience_preferred",
                "attributes.requirements.experience_required",
                "attributes.requirements.licenses_preferred",
                "attributes.requirements.years_experience_preferred_min",
            }
        )
        self.assertEqual(
            OPPORTUNITY_ENRICHMENT_FACT_FIELD_PATHS_V1,
            frozenset(FIELD_DEFAULTS) - vnext_only_fields,
        )
        self.assertTrue(
            OPPORTUNITY_ENRICHMENT_FACT_FIELD_PATHS_V1.isdisjoint(
                vnext_only_fields
            )
        )

    def test_eligibility_aggregation_preserves_unknown_and_fail(self):
        unknown = eligibility_decision("canonical:1", language="unknown")
        failed = eligibility_decision("canonical:2", language="fail")
        passed = eligibility_decision("canonical:3", language="pass")

        self.assertEqual(unknown.status, "unknown")
        self.assertEqual(
            unknown.unresolved_criterion_ids,
            ("eligibility.required_languages",),
        )
        self.assertEqual(failed.status, "ineligible")
        self.assertEqual(passed.status, "eligible")

    def test_eligibility_authority_is_closed_and_complete(self):
        valid = list(eligibility_outcomes(language="pass"))
        valid[-1] = CriterionOutcomeV1(
            criterion_id="eligibility.made_up",
            criterion_class="eligibility",
            dimension="made_up",
            outcome="pass",
            reason_code="fixture_pass",
            potentially_relaxable=False,
        )
        with self.assertRaises(MatchingContractError):
            DeterministicEligibilityDecisionV1.from_outcomes(
                opportunity_ref="canonical:1",
                policy_version="eligibility_policy_v1",
                outcomes=tuple(valid),
            )

        with self.assertRaises(MatchingContractError):
            DeterministicEligibilityDecisionV1.from_outcomes(
                opportunity_ref="canonical:1",
                policy_version="eligibility_policy_v1",
                outcomes=eligibility_outcomes(language="pass")[:-1],
            )

        professional_domain = CriterionOutcomeV1(
            criterion_id="eligibility.professional_domain",
            criterion_class="eligibility",
            dimension="professional_domain_eligibility",
            outcome="fail",
            reason_code="legacy_relevance_gate",
            potentially_relaxable=False,
        )
        with self.assertRaises(MatchingContractError):
            DeterministicEligibilityDecisionV1.from_outcomes(
                opportunity_ref="canonical:1",
                policy_version="eligibility_policy_v1",
                outcomes=tuple(sorted(
                    (*eligibility_outcomes(language="pass"), professional_domain),
                    key=lambda item: item.criterion_id,
                )),
            )

    def test_shortlist_admits_unknown_but_rejects_deterministic_fail(self):
        candidate = shortlist_candidate("canonical:1", 1, language="unknown")

        self.assertEqual(candidate.eligibility.status, "unknown")
        self.assertEqual(
            candidate.missing_semantic_fields,
            ("attributes.content.responsibilities",),
        )
        with self.assertRaises(MatchingContractError):
            shortlist_candidate("canonical:2", 2, language="fail")
        with self.assertRaises(MatchingContractError):
            ShortlistCandidateV1(
                opportunity_ref="canonical:2",
                canonical_opportunity_id=2,
                selected_job_id=102,
                variant_group_ref="canonical_group:2",
                variant_disposition="singleton",
                enrichment_fingerprint="b" * 64,
                eligibility=eligibility_decision("canonical:2", language="unknown"),
                retrieval_channels=("domain_recall",),
                missing_semantic_fields=("attributes.future.unversioned",),
            )

    def test_grounded_facts_reject_non_json_and_unversioned_paths(self):
        with self.assertRaises(MatchingContractError):
            GroundedFactV1(
                fact_ref="profile_fact:not_finite",
                field_path="profile.skills",
                value=float("nan"),
                provenance="user_confirmed",
                source_refs=("profile_revision:test_1",),
            )
        with self.assertRaises(ValueError):
            canonical_json({"not_finite": float("nan")})
        with self.assertRaises(MatchingContractError):
            GroundedFactV1(
                fact_ref="profile_fact:unknown_path",
                field_path="profile.future_unversioned_field",
                value="value",
                provenance="user_confirmed",
                source_refs=("profile_revision:test_1",),
            )

    def test_semantic_request_binds_fact_scope_and_source(self):
        candidate = shortlist_candidate("canonical:1", 1, language="unknown")
        bad_fact = GroundedFactV1(
            fact_ref="opportunity_fact:canonical_1_title",
            field_path="source.canonical_title",
            value="Software Engineer",
            provenance="source_explicit",
            source_refs=("canonical:999",),
        )
        with self.assertRaises(MatchingContractError):
            SemanticRerankRequestV1(
                request_ref="rerank_request:test_1",
                profile_revision_ref="profile_revision:test_1",
                profile_contract_version="canonical_profile_v2",
                profile_fingerprint="f" * 64,
                taxonomy_version="opportunity_taxonomy_v2_2026_08",
                profile_facts=(profile_fact(),),
                candidates=(
                    SemanticRerankCandidateInputV1(
                        shortlist_candidate=candidate,
                        opportunity_facts=(bad_fact,),
                    ),
                ),
            )

    def test_semantic_result_is_complete_and_grounded(self):
        request = semantic_request(("canonical:1", "canonical:2"))
        valid = semantic_result(request, ("canonical:1", "canonical:2"))
        validate_semantic_rerank_result(request, valid)

        omitted = semantic_result(request, ("canonical:1",))
        with self.assertRaises(MatchingContractError) as raised:
            validate_semantic_rerank_result(request, omitted)
        self.assertIn("semantic_candidate_set_mismatch", raised.exception.reason_codes)

        with self.assertRaises(MatchingContractError):
            SemanticRerankItemV1(
                opportunity_ref="canonical:1",
                rank=1,
                evidence_links=(),
                requirement_set_status="no_structured_requirements",
                requirement_checks=(),
                caveat_fact_refs=(),
                reason_codes=("direct_domain_alignment",),
            )

        hallucinated = SemanticRerankResultV1(
            request_ref=request.request_ref,
            producer_version="fixture_reranker_v1",
            items=(
                semantic_item(
                    "canonical:1",
                    1,
                    profile_fact_ref="profile_fact:missing",
                ),
                semantic_item("canonical:2", 2),
            ),
        )
        with self.assertRaises(MatchingContractError) as raised:
            validate_semantic_rerank_result(request, hallucinated)
        self.assertIn(
            "semantic_evidence_reference_unresolved",
            raised.exception.reason_codes,
        )

    def test_requirement_and_caveat_outputs_must_cover_input(self):
        request = semantic_request(
            ("canonical:1",),
            include_requirement=True,
            include_caveat=True,
        )
        incomplete = SemanticRerankResultV1(
            request_ref=request.request_ref,
            producer_version="fixture_reranker_v1",
            items=(
                semantic_item(
                    "canonical:1",
                    1,
                    requirement_set_status="no_structured_requirements",
                ),
            ),
        )
        with self.assertRaises(MatchingContractError):
            validate_semantic_rerank_result(request, incomplete)

        complete = SemanticRerankResultV1(
            request_ref=request.request_ref,
            producer_version="fixture_reranker_v1",
            items=(
                semantic_item(
                    "canonical:1",
                    1,
                    requirement_set_status="checked",
                    include_requirement=True,
                    include_caveat=True,
                ),
            ),
        )
        validate_semantic_rerank_result(request, complete)

        missing_caveat = SemanticRerankResultV1(
            request_ref=request.request_ref,
            producer_version="fixture_reranker_v1",
            items=(
                semantic_item(
                    "canonical:1",
                    1,
                    requirement_set_status="checked",
                    include_requirement=True,
                    include_caveat=False,
                ),
            ),
        )
        with self.assertRaises(MatchingContractError) as raised:
            validate_semantic_rerank_result(request, missing_caveat)
        self.assertIn("semantic_caveats_incomplete", raised.exception.reason_codes)

    def test_strict_raw_json_boundary_rejects_extra_duplicate_and_nonstandard_data(self):
        request = semantic_request(("canonical:1",))
        result = semantic_result(request, ("canonical:1",))
        parsed = parse_semantic_rerank_result_json(
            canonical_json(result.as_dict()),
            request,
        )
        self.assertEqual(parsed, result)

        extra = result.as_dict()
        extra["relationships"] = []
        with self.assertRaises(MatchingContractError):
            parse_semantic_rerank_result_json(json.dumps(extra), request)

        duplicate_key = canonical_json(result.as_dict()).replace(
            '"producer_version":"fixture_reranker_v1"',
            '"producer_version":"fixture_reranker_v1","producer_version":"other"',
        )
        with self.assertRaises(MatchingContractError):
            parse_semantic_rerank_result_json(duplicate_key, request)

        with self.assertRaises(MatchingContractError):
            parse_semantic_rerank_result_json('{"value": NaN}', request)

    def test_malformed_contract_collections_fail_with_bounded_errors(self):
        with self.assertRaises(MatchingContractError):
            DeterministicEligibilityDecisionV1.from_outcomes(
                opportunity_ref="canonical:1",
                policy_version="eligibility_policy_v1",
                outcomes=(object(),),
            )
        with self.assertRaises(MatchingContractError):
            SemanticRerankResultV1(
                request_ref="rerank_request:test_1",
                producer_version="fixture_reranker_v1",
                items=(object(),),
            )

    def test_match_run_requires_disposition_for_every_shortlisted_candidate(self):
        candidates = (
            shortlist_candidate("canonical:1", 1, language="unknown"),
            shortlist_candidate("canonical:2", 2, language="pass"),
        )
        snapshot = match_run_snapshot(candidates)
        result = MatchRunResultV1(
            run_ref=snapshot.run_ref,
            snapshot_fingerprint=snapshot.fingerprint,
            completed_at="2026-08-29T12:00:03+00:00",
            status="completed",
            mode="deterministic_fallback",
            producer_version="fallback_v1",
            items=(semantic_item("canonical:1", 1),),
            candidate_dispositions=(
                disposition("canonical:1", "ranked"),
                disposition("canonical:2", "not_ranked"),
            ),
        )
        validate_match_run_result(snapshot, result)

        incomplete = MatchRunResultV1(
            run_ref=snapshot.run_ref,
            snapshot_fingerprint=snapshot.fingerprint,
            completed_at="2026-08-29T12:00:03+00:00",
            status="completed",
            mode="deterministic_fallback",
            producer_version="fallback_v1",
            items=(semantic_item("canonical:1", 1),),
            candidate_dispositions=(disposition("canonical:1", "ranked"),),
        )
        with self.assertRaises(MatchingContractError) as raised:
            validate_match_run_result(snapshot, incomplete)
        self.assertIn(
            "match_run_candidate_dispositions_incomplete",
            raised.exception.reason_codes,
        )

    def test_snapshot_and_result_are_fingerprint_bound(self):
        snapshot = match_run_snapshot(
            (shortlist_candidate("canonical:1", 1, language="unknown"),)
        )
        result = MatchRunResultV1(
            run_ref=snapshot.run_ref,
            snapshot_fingerprint=snapshot.fingerprint,
            completed_at="2026-08-29T12:00:03+00:00",
            status="completed",
            mode="deterministic_fallback",
            producer_version="fallback_v1",
            items=(semantic_item("canonical:1", 1),),
            candidate_dispositions=(disposition("canonical:1", "ranked"),),
        )
        validate_match_run_result(snapshot, result)
        self.assertEqual(snapshot.fingerprint, contract_fingerprint(snapshot.as_dict()))

        wrong = MatchRunResultV1(
            run_ref=snapshot.run_ref,
            snapshot_fingerprint="e" * 64,
            completed_at="2026-08-29T12:00:03+00:00",
            status="completed",
            mode="deterministic_fallback",
            producer_version="fallback_v1",
            items=(semantic_item("canonical:1", 1),),
            candidate_dispositions=(disposition("canonical:1", "ranked"),),
        )
        with self.assertRaises(MatchingContractError):
            validate_match_run_result(snapshot, wrong)

    def test_match_run_zero_candidate_and_failure_paths_remain_explicit(self):
        empty_snapshot = match_run_snapshot(())
        no_candidates = MatchRunResultV1(
            run_ref=empty_snapshot.run_ref,
            snapshot_fingerprint=empty_snapshot.fingerprint,
            completed_at="2026-08-29T12:00:03+00:00",
            status="completed",
            mode="no_candidates",
            producer_version="fallback_v1",
            items=(),
            candidate_dispositions=(),
        )
        validate_match_run_result(empty_snapshot, no_candidates)

        candidate_snapshot = match_run_snapshot(
            (shortlist_candidate("canonical:1", 1, language="unknown"),)
        )
        failed = MatchRunResultV1(
            run_ref=candidate_snapshot.run_ref,
            snapshot_fingerprint=candidate_snapshot.fingerprint,
            completed_at="2026-08-29T12:00:03+00:00",
            status="failed",
            mode="none",
            producer_version="reranker_v1",
            items=(),
            candidate_dispositions=(
                disposition("canonical:1", "not_evaluated"),
            ),
        )
        validate_match_run_result(candidate_snapshot, failed)

    def test_relationship_is_deterministic_and_outside_reranker_result(self):
        relationship = OpportunityRelationshipV1(
            identity_policy_version="identity_policy_v1",
            left_opportunity_ref="canonical:2904",
            right_opportunity_ref="canonical:2970",
            relationship="material_variant",
            distinguishing_field_paths=(
                "attributes.compensation.amount_min",
                "attributes.requirements.education.minimum_level",
                "attributes.role.specializations",
            ),
            evidence_refs=("job:9106", "job:9172"),
            reason_code="same_title_materially_different_requirements",
        )
        self.assertEqual(relationship.relationship, "material_variant")
        self.assertIn("identity_policy_version", relationship.as_dict())

        result = semantic_result(semantic_request(("canonical:1",)), ("canonical:1",))
        self.assertNotIn("relationships", result.as_dict())

    def test_serialized_contracts_have_no_legacy_ranking_abstractions(self):
        request = semantic_request(("canonical:1",))
        result = semantic_result(request, ("canonical:1",))
        keys = recursive_keys({"request": request.as_dict(), "result": result.as_dict()})
        self.assertFalse({"score", "band", "bucket", "recommendation"} & keys)


def criterion_outcome(criterion_id, dimension, outcome):
    return CriterionOutcomeV1(
        criterion_id=criterion_id,
        criterion_class="eligibility",
        dimension=dimension,
        outcome=outcome,
        reason_code=f"fixture_{outcome}",
        potentially_relaxable=False,
    )


def eligibility_outcomes(*, language):
    return (
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


def eligibility_decision(opportunity_ref, *, language):
    return DeterministicEligibilityDecisionV1.from_outcomes(
        opportunity_ref=opportunity_ref,
        policy_version="eligibility_policy_v1",
        outcomes=eligibility_outcomes(language=language),
    )


def shortlist_candidate(opportunity_ref, canonical_id, *, language):
    return ShortlistCandidateV1(
        opportunity_ref=opportunity_ref,
        canonical_opportunity_id=canonical_id,
        selected_job_id=100 + canonical_id,
        variant_group_ref=f"canonical_group:{canonical_id}",
        variant_disposition="singleton",
        enrichment_fingerprint="a" * 64,
        eligibility=eligibility_decision(opportunity_ref, language=language),
        retrieval_channels=("domain_recall",),
        missing_semantic_fields=("attributes.content.responsibilities",),
    )


def profile_fact():
    return GroundedFactV1(
        fact_ref="profile_fact:python",
        field_path="profile.skills",
        value=["python"],
        provenance="user_confirmed",
        source_refs=("profile_revision:test_1",),
    )


def opportunity_facts(opportunity_ref, *, include_requirement=False, include_caveat=False):
    suffix = opportunity_ref.replace(":", "_")
    facts = [
        GroundedFactV1(
            fact_ref=f"opportunity_fact:{suffix}_title",
            field_path="source.canonical_title",
            value="Software Engineer",
            provenance="source_explicit",
            source_refs=(opportunity_ref,),
        )
    ]
    if include_requirement:
        facts.append(
            GroundedFactV1(
                fact_ref=f"opportunity_fact:{suffix}_skills_required",
                field_path="attributes.requirements.skills_required",
                value=["python"],
                provenance="automatic_enrichment",
                source_refs=(opportunity_ref,),
            )
        )
    if include_caveat:
        facts.append(
            GroundedFactV1(
                fact_ref=f"opportunity_fact:{suffix}_caveats",
                field_path="attributes.content.caveats",
                value=["Compensation is not disclosed."],
                provenance="automatic_enrichment",
                source_refs=(opportunity_ref,),
            )
        )
    return tuple(sorted(facts, key=lambda item: item.fact_ref))


def semantic_request(
    opportunity_refs,
    *,
    include_requirement=False,
    include_caveat=False,
):
    candidates = tuple(
        SemanticRerankCandidateInputV1(
            shortlist_candidate=shortlist_candidate(ref, index, language="unknown"),
            opportunity_facts=opportunity_facts(
                ref,
                include_requirement=include_requirement,
                include_caveat=include_caveat,
            ),
        )
        for index, ref in enumerate(opportunity_refs, start=1)
    )
    return SemanticRerankRequestV1(
        request_ref="rerank_request:test_1",
        profile_revision_ref="profile_revision:test_1",
        profile_contract_version="canonical_profile_v2",
        profile_fingerprint="f" * 64,
        taxonomy_version="opportunity_taxonomy_v2_2026_08",
        profile_facts=(profile_fact(),),
        candidates=candidates,
    )


def semantic_item(
    opportunity_ref,
    rank,
    *,
    profile_fact_ref="profile_fact:python",
    requirement_set_status="no_structured_requirements",
    include_requirement=False,
    include_caveat=False,
):
    suffix = opportunity_ref.replace(":", "_")
    title_ref = f"opportunity_fact:{suffix}_title"
    checks = ()
    if include_requirement:
        checks = (
            RequirementCheckV1(
                opportunity_fact_ref=f"opportunity_fact:{suffix}_skills_required",
                status="met",
                profile_fact_refs=("profile_fact:python",),
                reason_code="requirement_supported",
            ),
        )
    caveats = (
        (f"opportunity_fact:{suffix}_caveats",)
        if include_caveat
        else ()
    )
    return SemanticRerankItemV1(
        opportunity_ref=opportunity_ref,
        rank=rank,
        evidence_links=(
            EvidenceLinkV1(
                profile_fact_ref=profile_fact_ref,
                opportunity_fact_ref=title_ref,
                relation="supports",
                reason_code="direct_skill_alignment",
            ),
        ),
        requirement_set_status=requirement_set_status,
        requirement_checks=checks,
        caveat_fact_refs=caveats,
        reason_codes=("direct_skill_alignment",),
    )


def semantic_result(request, opportunity_refs):
    return SemanticRerankResultV1(
        request_ref=request.request_ref,
        producer_version="fixture_reranker_v1",
        items=tuple(
            semantic_item(opportunity_ref, rank)
            for rank, opportunity_ref in enumerate(opportunity_refs, start=1)
        ),
    )


def disposition(opportunity_ref, value):
    return MatchRunCandidateDispositionV1(
        opportunity_ref=opportunity_ref,
        disposition=value,
        reason_codes=(f"fixture_{value}",),
    )


def match_run_snapshot(candidates):
    return MatchRunSnapshotV1(
        run_ref="match_run:test_1",
        created_at="2026-08-29T12:00:00+00:00",
        profile_revision_ref="profile_revision:test_1",
        profile_fingerprint="c" * 64,
        inventory_as_of="2026-08-29T11:55:00+00:00",
        inventory_fingerprint="d" * 64,
        eligibility_policy_version="eligibility_v1",
        identity_policy_version="identity_v1",
        shortlist_policy_version="shortlist_v1",
        taxonomy_version="opportunity_taxonomy_v2_2026_08",
        candidates=candidates,
    )


def recursive_keys(value):
    keys = set()
    if isinstance(value, dict):
        keys.update(value)
        for item in value.values():
            keys.update(recursive_keys(item))
    elif isinstance(value, list):
        for item in value:
            keys.update(recursive_keys(item))
    return keys


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Offline architecture report for Semantic Matching Shadow v1.

The report consumes only local synthetic profile facts, existing reviewed OE
semantic fixtures, and existing matching-foundation metadata.  It performs no
model, provider, network, database, runtime matcher, UI, or persistence call.
"""

from __future__ import annotations

import argparse
import ast
from hashlib import sha256
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wahojobs.matching.foundation_contracts import (  # noqa: E402
    DeterministicEligibilityDecisionV1,
    GroundedFactV1,
    ShortlistCandidateV1,
    contract_fingerprint,
)
from wahojobs.matching.semantic_shadow import (  # noqa: E402
    LOCAL_SHADOW_EVALUATOR_VERSION,
    SEMANTIC_MATCHING_SHADOW_RESULT_SCHEMA_VERSION,
    SEMANTIC_MATCHING_SHADOW_SEAM_VERSION,
    NormalizedProfileSemanticSignalV1,
    SemanticMatchingShadowRequestV1,
    run_semantic_matching_shadow_v1,
    semantic_shadow_candidate_from_packet_v1,
)
from wahojobs.matching.typed_criteria import CriterionOutcomeV1  # noqa: E402
from wahojobs.opportunity_semantic_authority import (  # noqa: E402
    AUTHORITY_POLICY_VERSION,
    SEMANTIC_MATCHING_PACKET_VERSION,
    build_semantic_matching_packet,
    canonical_sha256,
)


REPORT_SCHEMA_VERSION = "matching_semantic_shadow_report_v1"
DEFAULT_FIXTURE = (
    ROOT / "tests" / "fixtures" / "semantic_matching_shadow_v1.json"
)
DEFAULT_SEMANTIC_GOLD = (
    ROOT / "tests" / "fixtures" / "opportunity_semantic_contract_v0.json"
)
MATCHING_EVALUATION = (
    ROOT / "tests" / "fixtures" / "matching_foundation_evaluation.json"
)
MATCHING_INVENTORY = (
    ROOT / "tests" / "fixtures" / "matching_foundation_inventory_snapshot.json"
)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _criterion_outcome(criterion_id: str, dimension: str, outcome: str):
    return CriterionOutcomeV1(
        criterion_id=criterion_id,
        criterion_class="eligibility",
        dimension=dimension,
        outcome=outcome,
        reason_code=f"fixture_{outcome}",
        potentially_relaxable=False,
    )


def _shortlist_candidate(canonical_id: int) -> ShortlistCandidateV1:
    opportunity_ref = f"canonical_opportunity:{canonical_id}"
    outcomes = (
        _criterion_outcome(
            "eligibility.credentials_licenses",
            "credential_eligibility",
            "not_applicable",
        ),
        _criterion_outcome(
            "eligibility.location", "location_eligibility", "pass"
        ),
        _criterion_outcome(
            "eligibility.required_languages",
            "required_language_eligibility",
            "pass",
        ),
    )
    decision = DeterministicEligibilityDecisionV1.from_outcomes(
        opportunity_ref=opportunity_ref,
        policy_version="eligibility_policy_v1",
        outcomes=outcomes,
    )
    return ShortlistCandidateV1(
        opportunity_ref=opportunity_ref,
        canonical_opportunity_id=canonical_id,
        selected_job_id=canonical_id,
        variant_group_ref=f"canonical_group:{canonical_id}",
        variant_disposition="singleton",
        enrichment_fingerprint=(f"{canonical_id:064x}")[-64:],
        eligibility=decision,
        retrieval_channels=("legacy_ranked_pool",),
    )


def _reviewed_packet(
    case: dict,
    *,
    fixture_version: int,
    opportunity_ref: str,
    selected_variant_ref: str,
) -> dict:
    relationships = [
        {
            "source_id": source["id"],
            "evidence_block_id": f"fixture_block_{index:03d}",
            "derivation": "server_authenticated_evidence_binding",
            "variant_refs": [selected_variant_ref],
            "source_refs": [selected_variant_ref],
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
        known_variant_refs=[selected_variant_ref],
        semantic_input_version=str(fixture_version),
        semantic_input_sha256=canonical_sha256(semantic_bundle),
        source_packet_sha256=canonical_sha256(case["evidence_catalog"]),
        variant_relationships=relationships,
    )


def _runtime_shadow_imports() -> list[str]:
    forbidden = []
    module_path = ROOT / "wahojobs" / "matching" / "semantic_shadow.py"
    for path in (ROOT / "wahojobs").rglob("*.py"):
        if path == module_path:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            if any(
                name == "wahojobs.matching.semantic_shadow"
                or name.startswith("wahojobs.matching.semantic_shadow.")
                for name in modules
            ):
                forbidden.append(path.relative_to(ROOT).as_posix())
    return sorted(set(forbidden))


def _foundation_context() -> dict:
    evaluation = _read_json(MATCHING_EVALUATION)
    return {
        "evaluation_schema_version": evaluation["schema_version"],
        "evaluation_sha256": _file_sha256(MATCHING_EVALUATION),
        "inventory_snapshot_sha256": _file_sha256(MATCHING_INVENTORY),
        "human_reviewed_relevance_case_count": len(
            evaluation["authoritative_relevance_case_ids"]
        ),
        "reviewed_or_derived_eligibility_case_count": len(
            evaluation["eligibility_cases"]
        ),
        "human_reviewed_relationship_case_count": len(
            evaluation["relationship_cases"]
        ),
        "matching_thresholds_changed": False,
        "deterministic_eligibility_policy_changed": False,
    }


def build_report(
    fixture_path: Path = DEFAULT_FIXTURE,
    semantic_gold_path: Path = DEFAULT_SEMANTIC_GOLD,
) -> dict:
    fixture = _read_json(fixture_path)
    semantic_gold = _read_json(semantic_gold_path)
    if fixture.get("schema_version") != "matching_semantic_shadow_evaluation_fixture_v1":
        raise ValueError("unsupported semantic shadow evaluation fixture")
    gold_by_id = {case["id"]: case for case in semantic_gold["cases"]}

    profile = fixture["profile"]
    profile_fact_ref = "profile_fact:semantic_shadow_evaluation"
    fact = GroundedFactV1(
        fact_ref=profile_fact_ref,
        field_path="profile.derived_matcher_signals",
        value=profile["semantic_terms"],
        provenance="user_confirmed",
        source_refs=(profile["profile_revision_ref"],),
    )
    signals = tuple(
        sorted(
            (
                NormalizedProfileSemanticSignalV1(
                    signal_ref=item["signal_ref"],
                    profile_fact_ref=profile_fact_ref,
                    semantic_kind=item["semantic_kind"],
                    semantic_terms=tuple(item["semantic_terms"]),
                )
                for item in profile["signals"]
            ),
            key=lambda item: item.signal_ref,
        )
    )

    candidates = []
    packet_case_ids = []
    for item in fixture["candidates"]:
        canonical_id = item["canonical_opportunity_id"]
        shortlist = _shortlist_candidate(canonical_id)
        case_id = item["reviewed_semantic_case_id"]
        packet = None
        if case_id is not None:
            packet_case_ids.append(case_id)
            packet = _reviewed_packet(
                gold_by_id[case_id],
                fixture_version=semantic_gold["fixture_version"],
                opportunity_ref=shortlist.opportunity_ref,
                selected_variant_ref=item["selected_variant_ref"],
            )
        candidates.append(
            semantic_shadow_candidate_from_packet_v1(
                shortlist_candidate=shortlist,
                legacy_rank=item["legacy_rank"],
                selected_variant_ref=item["selected_variant_ref"],
                packet=packet,
            )
        )
    candidates = tuple(sorted(candidates, key=lambda item: item.opportunity_ref))
    eligibility_before = {
        item.opportunity_ref: contract_fingerprint(item.shortlist_candidate.eligibility)
        for item in candidates
    }
    request = SemanticMatchingShadowRequestV1(
        request_ref="semantic_shadow_request:offline_evaluation",
        profile_revision_ref=profile["profile_revision_ref"],
        profile_contract_version="canonical_profile_v2",
        profile_fingerprint=canonical_sha256(fact.as_dict()),
        taxonomy_version="opportunity_taxonomy_v2_2026_08",
        shortlist_limit=32,
        profile_facts=(fact,),
        profile_signals=signals,
        candidates=candidates,
    )
    result = run_semantic_matching_shadow_v1(request)
    result_by_ref = {item.opportunity_ref: item for item in result.items}
    legacy_order = [
        item.opportunity_ref
        for item in sorted(candidates, key=lambda value: value.legacy_rank)
    ]
    shadow_order = [item.opportunity_ref for item in result.items]

    or_item = result_by_ref["canonical_opportunity:910001"]
    preferred_item = result_by_ref["canonical_opportunity:910002"]
    missing_item = result_by_ref["canonical_opportunity:910003"]
    and_item = result_by_ref["canonical_opportunity:910004"]
    expected = fixture["expected"]
    runtime_imports = _runtime_shadow_imports()
    deterministic_unchanged = all(
        contract_fingerprint(result_by_ref[ref].deterministic_eligibility)
        == fingerprint
        for ref, fingerprint in eligibility_before.items()
    )
    candidate_set_preserved = set(legacy_order) == set(shadow_order)
    and_logic = and_item.group_assessments[0].logic
    or_logic = or_item.group_assessments[0].logic

    acceptance = {
        "expected_legacy_order_preserved_as_input": legacy_order
        == expected["legacy_order"],
        "expected_semantic_shadow_order": shadow_order
        == expected["semantic_shadow_order"],
        "survivor_set_preserved": candidate_set_preserved,
        "deterministic_eligibility_unchanged": deterministic_unchanged,
        "preferred_support_changes_relative_order": (
            preferred_item.relative_rank < preferred_item.legacy_rank
            and preferred_item.supported_preferred_group_count == 1
        ),
        "required_unknown_is_non_exclusionary": (
            and_item.group_assessments[0].status == "partially_supported"
            and and_item.supported_required_group_count == 0
            and and_item.opportunity_ref in shadow_order
        ),
        "missing_packet_is_non_exclusionary": (
            missing_item.semantic_packet_status == "unavailable"
            and missing_item.opportunity_ref in shadow_order
        ),
        "and_structure_preserved": (
            and_logic["form"] == "bounded_dnf"
            and len(and_logic["any_of"]) == 1
            and set(and_logic["any_of"][0]["all_of"])
            == {"japanese", "korean"}
        ),
        "or_structure_preserved": (
            or_logic["form"] == "bounded_dnf"
            and len(or_logic["any_of"]) == 2
        ),
        "semantic_negative_ranking_factors_absent": result.invariant_proof[
            "semantic_negative_ranking_factor_count"
        ]
        == 0,
        "runtime_and_ui_imports_absent": not runtime_imports,
    }
    acceptance["passed"] = all(acceptance.values())

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "seam_version": SEMANTIC_MATCHING_SHADOW_SEAM_VERSION,
        "request_schema_version": request.schema_version,
        "result_schema_version": SEMANTIC_MATCHING_SHADOW_RESULT_SCHEMA_VERSION,
        "packet_version": SEMANTIC_MATCHING_PACKET_VERSION,
        "semantic_authority_policy_version": AUTHORITY_POLICY_VERSION,
        "producer_version": LOCAL_SHADOW_EVALUATOR_VERSION,
        "fixture": {
            "path": fixture_path.resolve().relative_to(ROOT).as_posix(),
            "sha256": _file_sha256(fixture_path),
            "semantic_gold_path": semantic_gold_path.resolve()
            .relative_to(ROOT)
            .as_posix(),
            "semantic_gold_sha256": _file_sha256(semantic_gold_path),
            "reviewed_semantic_case_ids": sorted(packet_case_ids),
            "authority": fixture["authority"],
        },
        "matching_foundation_context": _foundation_context(),
        "comparison": {
            "legacy_order": legacy_order,
            "semantic_shadow_order": shadow_order,
            "reordered_opportunity_count": sum(
                left != right for left, right in zip(legacy_order, shadow_order)
            ),
            "candidate_count": len(candidates),
            "result_items": [
                {
                    "opportunity_ref": item.opportunity_ref,
                    "legacy_rank": item.legacy_rank,
                    "relative_rank": item.relative_rank,
                    "packet_coverage_state": item.packet_coverage_state,
                    "positive_support": {
                        "required_groups": item.supported_required_group_count,
                        "preferred_groups": item.supported_preferred_group_count,
                        "descriptive_groups": item.supported_descriptive_group_count,
                        "unassigned_propositions": item.supported_unassigned_proposition_count,
                        "native_descriptive_signals": item.supported_native_signal_count,
                    },
                    "deterministic_eligibility_status": item.deterministic_eligibility.status,
                }
                for item in result.items
            ],
        },
        "logic_proof": {
            "or_logic": or_logic,
            "and_logic": and_logic,
            "required_partial_status": and_item.group_assessments[0].status,
            "required_partial_ranking_effect": and_item.group_assessments[
                0
            ].ranking_effect,
        },
        "invariant_proof": result.invariant_proof,
        "runtime_shadow_imports": runtime_imports,
        "offline_controls": {
            "model_calls": 0,
            "provider_calls": 0,
            "network_calls": 0,
            "database_reads": 0,
            "database_writes": 0,
            "runtime_matcher_calls": 0,
            "candidate_ui_calls": 0,
        },
        "acceptance": acceptance,
        "bounded_live_evaluation_proposal": {
            "executed": False,
            "reviewed_profile_ids": [
                "biology_or_medicine_academic",
                "multilingual_translator",
                "software_engineer",
            ],
            "profile_count": 3,
            "eligible_survivors_per_profile": 12,
            "maximum_profile_opportunity_comparisons": 36,
            "missing_packet_controls_per_profile": 2,
            "precondition": (
                "read_only_native_packet_availability_audit_and_sample_reduction_if_needed"
            ),
            "comparison": [
                "legacy_rank_vs_semantic_shadow_rank",
                "survivor_set_identity",
                "human_reviewed_relevance_ordering",
                "pairwise_rank_changes",
                "packet_coverage_and_uncertainty",
            ],
            "external_model_required_for_semantic_comparison": True,
            "explicit_profile_data_authorization_required": True,
        },
        "verdict": (
            "SEMANTIC MATCHING SHADOW SEAM READY"
            if acceptance["passed"]
            else "MATCHING ARCHITECTURE WORK NEEDED"
        ),
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--semantic-gold", type=Path, default=DEFAULT_SEMANTIC_GOLD)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Exit non-zero if any architecture acceptance check fails.",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    report = build_report(args.fixture, args.semantic_gold)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    return 0 if not args.verify or report["acceptance"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

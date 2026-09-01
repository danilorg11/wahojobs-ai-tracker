#!/usr/bin/env python3
"""Build and verify Semantic Ranking Quality Benchmark v1 preregistration.

This program is deliberately local and read-only except for an explicitly named
JSON output.  It does not import or execute semantic matching, packet
construction, extraction clients, prompts, HTTP clients, or persistence code.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import matching_foundation_report as foundation  # noqa: E402
import matching_quality_report as golden  # noqa: E402
from wahojobs.matching.foundation_contracts import (  # noqa: E402
    DeterministicEligibilityDecisionV1,
)


SCHEMA_VERSION = "wahojobs_semantic_ranking_quality_preregistration_v1"
BENCHMARK_VERSION = "wahojobs_semantic_ranking_quality_benchmark_v1"
EXPECTED_HEAD = "1b00ede0a698ffc51014f05a9e192c54c9c860f6"
EXPECTED_SUBJECT = "Harden semantic matching shadow grounding"
GOLDEN_PATH = ROOT / "tests" / "fixtures" / "matching_golden_set.json"
EVALUATION_PATH = (
    ROOT / "tests" / "fixtures" / "matching_foundation_evaluation.json"
)
DATABASE_PATH = ROOT / "data" / "wahojobs.sqlite"
DEFAULT_OUTPUT = (
    ROOT
    / "tests"
    / "fixtures"
    / "semantic_ranking_quality_benchmark_v1_preregistration.json"
)
LABEL_GRADE = {"false_positive": 0, "weak": 1, "plausible": 2, "strong": 3}
LABEL_ORDER = ("strong", "plausible", "weak", "false_positive")
PROFILE_FACT_FIELDS = (
    "summary",
    "education_level",
    "degrees_or_domains",
    "languages",
    "skills",
    "work_preferences",
    "constraints",
    "target_opportunity_types",
    "notes",
)
OPPORTUNITY_EVIDENCE_FIELDS = (
    "title",
    "canonical_title",
    "source",
    "source_slug",
    "source_tier",
    "location",
    "department",
    "expertise",
    "source_category",
    "commitment",
    "opportunity_kind",
    "availability_basis",
    "inventory_model",
    "market_count_policy",
    "include_in_live_market_estimate",
    "language",
    "language_locale",
    "required_languages",
)
PROTECTED_MATCHING_PATHS = (
    "scripts/profile_match_digest.py",
    "scripts/local_product_app.py",
    "wahojobs/authenticated_profile_matches.py",
    "wahojobs/matching/foundation_contracts.py",
    "wahojobs/matching/semantic_shadow.py",
    "wahojobs/matching/semantic_shadow_grounding.py",
)
FORBIDDEN_PROVIDER_KEYS = frozenset(
    {
        "approved_at",
        "approved_by",
        "benchmark_metric_outcomes",
        "expected_label",
        "expected_ordering",
        "expected_section",
        "human_notes",
        "human_relevance",
        "known_legacy_inversion",
        "label_source",
        "legacy_bucket",
        "legacy_rank",
        "legacy_score",
        "metric_outcomes",
        "rationale",
        "review_required",
    }
)
HUMAN_REVIEW_COMMIT = "029067f5743d2aeac95c15ba7271c88682cd69ee"
FOUNDATION_COMMIT = "6c54fcf7f8144e111b21fd301624e3bc60f417ec"
SEMANTIC_SEAM_COMMIT = "5cf9da285e55eb42cf3ff09693a2295e85c34c86"
SEMANTIC_GROUNDING_COMMIT = EXPECTED_HEAD
PROVIDER_ORDER_SEED = "wahojobs-semantic-ranking-quality-benchmark-v1-provider-order"


class PreregistrationError(ValueError):
    pass


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=ROOT, text=True, encoding="utf-8"
    ).strip()


def commit_provenance(commit: str) -> dict:
    rendered = git(
        "show",
        "-s",
        "--date=iso-strict",
        "--format=%H%x09%ad%x09%s",
        commit,
    )
    commit_hash, committed_at, subject = rendered.split("\t", 2)
    return {
        "commit": commit_hash,
        "committed_at": committed_at,
        "subject": subject,
    }


def workspace_databases() -> list[Path]:
    values = []
    for pattern in ("*.db", "*.sqlite", "*.sqlite3"):
        values.extend(ROOT.rglob(pattern))
    return sorted(set(values))


def database_states() -> list[dict]:
    return [
        {
            "path": path.relative_to(ROOT).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in workspace_databases()
    ]


def open_immutable_database(path: Path):
    connection = sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def read_only_table_count(path: Path, table: str) -> int:
    if table not in {"opportunity_enrichment_runs"}:
        raise PreregistrationError("table count is outside the audit allowlist")
    connection = open_immutable_database(path)
    try:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        connection.close()


def current_job_binding(connection, matcher_input: dict) -> dict | None:
    source_hash = str(matcher_input.get("source_hash") or "").strip()
    source_slug = str(matcher_input.get("source_slug") or "").strip()
    if not source_hash or not source_slug:
        return None
    row = connection.execute(
        """
        SELECT
          j.id AS job_id, j.canonical_opportunity_id, j.is_active AS job_is_active,
          j.semantic_authority_state, j.opportunity_kind, j.availability_basis,
          j.include_in_live_market_estimate, c.slug AS source_slug,
          c.source_tier, c.inventory_model, c.market_count_policy,
          co.is_active AS canonical_is_active
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        LEFT JOIN canonical_opportunities co ON co.id = j.canonical_opportunity_id
        WHERE j.source_hash = ? AND c.slug = ?
        """,
        (source_hash, source_slug),
    ).fetchone()
    return dict(row) if row is not None else None


def canonical_evidence_state(connection, canonical_id: int | None) -> dict | None:
    if type(canonical_id) is not int or canonical_id <= 0:
        return None
    row = connection.execute(
        """
        SELECT
          co.id AS canonical_opportunity_id,
          co.is_active AS canonical_is_active,
          COUNT(j.id) AS variant_count,
          SUM(j.is_active = 1) AS active_variant_count,
          SUM(
            sc.body IS NOT NULL AND LENGTH(TRIM(sc.body)) > 0
          ) AS rich_body_variant_count,
          SUM(
            sc.body IS NOT NULL AND LENGTH(TRIM(sc.body)) > 0
            AND j.semantic_authority_state = 'versioned_accepted'
            AND a.accepted_capture_id IS NOT NULL
          ) AS accepted_body_variant_count
        FROM canonical_opportunities co
        LEFT JOIN jobs j
          ON j.canonical_opportunity_id = co.id
          AND j.title NOT LIKE '[SIMULATION]%'
          AND j.semantic_authority_state != 'pending'
        LEFT JOIN job_source_contents sc ON sc.job_id = j.id
        LEFT JOIN job_source_content_acceptances a ON a.job_id = j.id
        WHERE co.id = ?
        GROUP BY co.id
        """,
        (canonical_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def packet_readiness(binding: dict | None, evidence: dict | None) -> dict:
    base = {
        "semantic_packet_status": "unavailable_not_provisioned",
        "frozen_semantic_extraction_available": False,
        "semantic_output_observed_for_benchmark": False,
        "provider_extraction_performed_in_this_milestone": False,
    }
    if binding is None:
        return {
            **base,
            "accepted_lkg_evidence_state": "unavailable",
            "provider_extraction_required_after_prerequisites": True,
            "provider_extraction_is_only_remaining_packet_step": False,
            "source_authority_blocker": "no_stable_database_opportunity_binding",
        }
    canonical_id = binding.get("canonical_opportunity_id")
    if evidence is None or type(canonical_id) is not int:
        return {
            **base,
            "accepted_lkg_evidence_state": "unavailable",
            "provider_extraction_required_after_prerequisites": True,
            "provider_extraction_is_only_remaining_packet_step": False,
            "source_authority_blocker": "no_current_canonical_binding",
        }
    rich_count = int(evidence.get("rich_body_variant_count") or 0)
    accepted_count = int(evidence.get("accepted_body_variant_count") or 0)
    if rich_count == 0:
        blocker = "accepted_body_evidence_unavailable"
        available = False
    elif accepted_count != rich_count:
        blocker = "versioned_accepted_capture_authority_incomplete"
        available = False
    else:
        blocker = None
        available = True
    return {
        **base,
        "accepted_lkg_evidence_state": "available" if available else "unavailable",
        "provider_extraction_required_after_prerequisites": True,
        "provider_extraction_is_only_remaining_packet_step": available,
        "source_authority_blocker": blocker,
    }


def profile_facts(profile: dict) -> dict:
    return {field: deepcopy(profile.get(field)) for field in PROFILE_FACT_FIELDS}


def opportunity_evidence(matcher_input: dict) -> dict:
    return {
        field: deepcopy(matcher_input.get(field))
        for field in OPPORTUNITY_EVIDENCE_FIELDS
    }


def provider_candidate_order(profile_ref: str, opportunity_refs: list[str]) -> list[str]:
    return sorted(
        opportunity_refs,
        key=lambda ref: sha256(
            f"{PROVIDER_ORDER_SEED}|{profile_ref}|{ref}".encode("utf-8")
        ).hexdigest(),
    )


def forbidden_provider_paths(value, path="provider_request_templates") -> list[str]:
    found = []
    if type(value) is dict:
        for key, item in value.items():
            child = f"{path}.{key}"
            if key in FORBIDDEN_PROVIDER_KEYS:
                found.append(child)
            found.extend(forbidden_provider_paths(item, child))
    elif type(value) is list:
        for index, item in enumerate(value):
            found.extend(forbidden_provider_paths(item, f"{path}[{index}]"))
    return found


def matching_paths_state() -> list[dict]:
    rows = []
    for relative in PROTECTED_MATCHING_PATHS:
        path = ROOT / relative
        rows.append(
            {
                "path": relative,
                "git_blob": git("rev-parse", f"HEAD:{relative}"),
                "sha256": sha256_file(path),
                "differs_from_head": bool(
                    subprocess.run(
                        ["git", "diff", "--quiet", "HEAD", "--", relative],
                        cwd=ROOT,
                        check=False,
                    ).returncode
                ),
            }
        )
    return rows


def metric_definitions() -> list[dict]:
    coverage_rule = (
        "A ranking-quality pair is covered only when both deterministic survivors "
        "have valid semantic packets and a validated semantic ordering in the same "
        "provider-blind request. Uncovered pairs are reported only as coverage."
    )
    return [
        {
            "metric_id": "pairwise_preference_accuracy",
            "primary": True,
            "definition": (
                "Concordant covered cross-label pairs divided by all covered "
                "cross-label pairs; higher human class must rank first."
            ),
            "same_label_pairs": "excluded",
            "coverage_rule": coverage_rule,
            "aggregation": ["micro_over_pairs", "macro_over_comparative_profiles"],
        },
        {
            "metric_id": "improved_legacy_inversions",
            "primary": True,
            "definition": (
                "Count covered pairs the legacy survivor order places below the "
                "lower-relevance item and semantic order corrects."
            ),
            "same_label_pairs": "excluded",
        },
        {
            "metric_id": "new_regressions",
            "primary": True,
            "definition": (
                "Count covered pairs legacy orders correctly and semantic order "
                "reverses against the human classes."
            ),
            "same_label_pairs": "excluded",
        },
        {
            "metric_id": "net_inversion_improvement",
            "primary": True,
            "definition": "improved_legacy_inversions minus new_regressions",
            "same_label_pairs": "excluded",
        },
        {
            "metric_id": "kendall_tau_b",
            "primary": False,
            "definition": (
                "Per-profile Kendall tau-b between rank and ordinal human grade, "
                "preserving human-class ties; report macro mean and pair-weighted mean."
            ),
            "minimum_profile_condition": "at_least_one_cross_label_covered_pair",
        },
        {
            "metric_id": "top_of_list_quality",
            "primary": False,
            "definition": (
                "Report NDCG@min(3,n), full-list NDCG, and top-1-max-class hit. "
                "Use ordinal grades strong=3, plausible=2, weak=1, false_positive=0 "
                "with gains 2^grade-1; compare semantic minus legacy."
            ),
            "profile_scope": (
                "Only profiles whose covered candidates include at least two human "
                "classes; same-class order never creates a win or loss."
            ),
        },
        {
            "metric_id": "coverage",
            "primary": True,
            "definition": (
                "Report packet and valid-output coverage by judgment, opportunity, "
                "profile, and cross-label pair, plus blocker category."
            ),
            "missingness_treatment": (
                "Missing, unavailable, or invalid packets are not semantic ranking "
                "errors or wins and remain outside quality denominators."
            ),
        },
    ]


def build_preregistration(captured_at: str) -> dict:
    try:
        parsed_time = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PreregistrationError("captured_at must be ISO-8601") from exc
    if parsed_time.tzinfo is None:
        raise PreregistrationError("captured_at must include a timezone")
    if git("rev-parse", "HEAD") != EXPECTED_HEAD:
        raise PreregistrationError("repository HEAD differs from the requested baseline")
    if git("show", "-s", "--format=%s", "HEAD") != EXPECTED_SUBJECT:
        raise PreregistrationError("repository subject differs from the requested baseline")

    databases_before = database_states()
    enrichment_run_rows_before = read_only_table_count(
        DATABASE_PATH, "opportunity_enrichment_runs"
    )
    golden_fixture = read_json(GOLDEN_PATH)
    evaluation = read_json(EVALUATION_PATH)
    authority = foundation.validate_evaluation_set(golden_fixture, evaluation)
    case_index = authority["case_index"]

    # Population selection intentionally happens from the complete authoritative
    # ID manifest before expected_label values are read for evaluation below.
    selected_case_ids = sorted(evaluation["authoritative_relevance_case_ids"])
    selected_cases = [case_index[case_id] for case_id in selected_case_ids]
    profiles_by_source_id = golden.load_benchmark_profiles(golden_fixture)
    selected_profile_ids = sorted({case["profile_id"] for case in selected_cases})
    profile_ref_by_id = {
        profile_id: f"benchmark_profile:P{index:03d}"
        for index, profile_id in enumerate(selected_profile_ids, start=1)
    }
    profiles = []
    for profile_id in selected_profile_ids:
        facts = profile_facts(profiles_by_source_id[profile_id])
        profiles.append(
            {
                "profile_ref": profile_ref_by_id[profile_id],
                "source_profile_id": profile_id,
                "profile_facts": facts,
                "profile_facts_sha256": fingerprint(facts),
                "provider_fact_fields": list(PROFILE_FACT_FIELDS),
                "legacy_weighted_signals_excluded_from_provider": True,
            }
        )

    case_rows = {}
    opportunity_identity_by_case = {}
    for case in selected_cases:
        row = golden.matcher_input_from_snapshot(case["matcher_input_snapshot"])
        case_rows[case["case_id"]] = row
        opportunity_identity_by_case[case["case_id"]] = foundation.corpus_identity(row)
    unique_identities = sorted(set(opportunity_identity_by_case.values()))
    opportunity_ref_by_identity = {
        identity: f"benchmark_opportunity:O{index:03d}"
        for index, identity in enumerate(unique_identities, start=1)
    }

    opportunities = []
    readiness_by_ref = {}
    connection = open_immutable_database(DATABASE_PATH)
    try:
        for identity in unique_identities:
            case_ids = sorted(
                case_id
                for case_id, value in opportunity_identity_by_case.items()
                if value == identity
            )
            representative = case_index[case_ids[0]]
            row = case_rows[case_ids[0]]
            binding = current_job_binding(connection, row)
            canonical_id = (
                binding.get("canonical_opportunity_id") if binding is not None else None
            )
            evidence_state = canonical_evidence_state(connection, canonical_id)
            readiness = packet_readiness(binding, evidence_state)
            opportunity_ref = opportunity_ref_by_identity[identity]
            readiness_by_ref[opportunity_ref] = readiness
            snapshot_metadata = representative["matcher_input_snapshot"][
                "snapshot_metadata"
            ]
            source_identifiers = {
                "snapshot_job_id": row.get("job_id"),
                "snapshot_canonical_opportunity_id": row.get(
                    "canonical_opportunity_id"
                ),
                "snapshot_external_id": row.get("external_id"),
                "snapshot_source_hash": row.get("source_hash"),
                "current_job_id": binding.get("job_id") if binding else None,
                "current_canonical_opportunity_id": canonical_id,
            }
            lifecycle = {
                "snapshot_opportunity_kind": row.get("opportunity_kind"),
                "snapshot_availability_basis": row.get("availability_basis"),
                "snapshot_source_tier": row.get("source_tier"),
                "snapshot_inventory_model": row.get("inventory_model"),
                "snapshot_market_count_policy": row.get("market_count_policy"),
                "snapshot_include_in_live_market_estimate": bool(
                    row.get("include_in_live_market_estimate")
                ),
                "current_job_is_active": (
                    bool(binding.get("job_is_active")) if binding else None
                ),
                "current_canonical_is_active": (
                    bool(binding.get("canonical_is_active")) if binding else None
                ),
                "current_semantic_authority_state": (
                    binding.get("semantic_authority_state") if binding else None
                ),
            }
            evidence = opportunity_evidence(row)
            opportunities.append(
                {
                    "opportunity_ref": opportunity_ref,
                    "source_case_ids": case_ids,
                    "local_identity": identity,
                    "source_row_identifiers_local_only": source_identifiers,
                    "selected_variant_ref": (
                        f"source_hash:{row['source_hash']}"
                        if row.get("source_hash")
                        else f"fixture_variant:{opportunity_ref.rsplit(':', 1)[-1]}"
                    ),
                    "evidence_snapshot": evidence,
                    "evidence_snapshot_sha256": fingerprint(evidence),
                    "snapshot_provenance": {
                        "source_type": snapshot_metadata["snapshot_source_type"],
                        "resolution_status": snapshot_metadata["resolution_status"],
                        "source_fixture": GOLDEN_PATH.relative_to(ROOT).as_posix(),
                    },
                    "lifecycle_and_trust": lifecycle,
                    "packet_readiness": readiness,
                    "provider_evidence_rule": (
                        "Use only independently accepted/LKG source evidence in a "
                        "validated native semantic packet; never send this legacy "
                        "matcher snapshot, human notes, or human-derived fixture metadata."
                    ),
                }
            )
    finally:
        connection.close()

    judgments = []
    baseline_items_by_profile = defaultdict(list)
    for index, case in enumerate(selected_cases, start=1):
        case_id = case["case_id"]
        row = case_rows[case_id]
        profile_id = case["profile_id"]
        profile_ref = profile_ref_by_id[profile_id]
        opportunity_ref = opportunity_ref_by_identity[
            opportunity_identity_by_case[case_id]
        ]
        authoritative_match = foundation.authoritative_match(
            profiles_by_source_id[profile_id], row
        )
        decision = DeterministicEligibilityDecisionV1.from_outcomes(
            opportunity_ref=foundation.evaluation_opportunity_ref(row, case_id),
            policy_version=foundation.ELIGIBILITY_POLICY_VERSION,
            outcomes=foundation.objective_eligibility_outcomes(authoritative_match),
        )
        scored = golden.matcher.score_opportunity(profiles_by_source_id[profile_id], row)
        judgment_ref = f"benchmark_judgment:J{index:03d}"
        human_relevance = case["expected_label"]
        judgments.append(
            {
                "judgment_ref": judgment_ref,
                "source_case_id": case_id,
                "profile_ref": profile_ref,
                "opportunity_ref": opportunity_ref,
                "human_relevance": human_relevance,
                "human_relevance_grade": LABEL_GRADE[human_relevance],
                "label_source": case["label_source"],
                "approved_by": case["approved_by"],
                "approved_at": case["approved_at"],
                "review_required": case["review_required"],
                "human_notes_sha256": sha256(
                    case["human_notes"].encode("utf-8")
                ).hexdigest(),
                "ranking_candidate": decision.status != "ineligible",
                "quality_exclusion_reason": (
                    None
                    if decision.status != "ineligible"
                    else "deterministic_hard_ineligibility_safety_boundary"
                ),
            }
        )
        baseline_items_by_profile[profile_ref].append(
            {
                "judgment_ref": judgment_ref,
                "source_case_id": case_id,
                "opportunity_ref": opportunity_ref,
                "legacy_score": int(scored["score"]),
                "legacy_raw_section": scored.get("raw_product_section"),
                "legacy_effective_section": scored.get("effective_product_section"),
                "deterministic_eligibility": decision.as_dict(),
                "tie_break_source": str(row.get("source") or ""),
                "tie_break_title": str(
                    scored.get("display_title") or row.get("title") or ""
                ),
            }
        )

    for profile_ref, items in baseline_items_by_profile.items():
        items.sort(
            key=lambda item: (
                -item["legacy_score"],
                item["tie_break_source"],
                item["tie_break_title"],
                item["opportunity_ref"],
            )
        )
        survivor_rank = 0
        for rank, item in enumerate(items, start=1):
            item["legacy_rank_all_reviewed"] = rank
            if item["deterministic_eligibility"]["status"] == "ineligible":
                item["legacy_rank_semantic_survivors"] = None
            else:
                survivor_rank += 1
                item["legacy_rank_semantic_survivors"] = survivor_rank
            item.pop("tie_break_source")
            item.pop("tie_break_title")

    judgment_by_ref = {item["judgment_ref"]: item for item in judgments}
    baseline_by_judgment = {
        item["judgment_ref"]: item
        for items in baseline_items_by_profile.values()
        for item in items
    }
    judgments_by_profile = defaultdict(list)
    for judgment in judgments:
        judgments_by_profile[judgment["profile_ref"]].append(judgment)
    pairs = []
    for profile_ref in sorted(judgments_by_profile):
        items = sorted(
            judgments_by_profile[profile_ref], key=lambda item: item["judgment_ref"]
        )
        for left_index, left in enumerate(items):
            for right in items[left_index + 1 :]:
                if left["human_relevance_grade"] == right["human_relevance_grade"]:
                    continue
                higher, lower = (
                    (left, right)
                    if left["human_relevance_grade"]
                    > right["human_relevance_grade"]
                    else (right, left)
                )
                ranking_quality = higher["ranking_candidate"] and lower[
                    "ranking_candidate"
                ]
                higher_rank = baseline_by_judgment[higher["judgment_ref"]][
                    "legacy_rank_semantic_survivors"
                ]
                lower_rank = baseline_by_judgment[lower["judgment_ref"]][
                    "legacy_rank_semantic_survivors"
                ]
                pairs.append(
                    {
                        "pair_ref": f"benchmark_pair:Q{len(pairs) + 1:03d}",
                        "profile_ref": profile_ref,
                        "higher_relevance_judgment_ref": higher["judgment_ref"],
                        "lower_relevance_judgment_ref": lower["judgment_ref"],
                        "scope": "ranking_quality" if ranking_quality else "safety_only",
                        "legacy_inversion": (
                            bool(higher_rank > lower_rank) if ranking_quality else None
                        ),
                        "same_label_pair": False,
                    }
                )

    quality_pairs = [item for item in pairs if item["scope"] == "ranking_quality"]
    comparative_profile_refs = sorted(
        {item["profile_ref"] for item in quality_pairs}
    )
    quality_judgment_refs = sorted(
        {
            judgment_ref
            for pair in quality_pairs
            for judgment_ref in (
                pair["higher_relevance_judgment_ref"],
                pair["lower_relevance_judgment_ref"],
            )
        }
    )

    provider_templates = []
    for profile_ref in sorted(judgments_by_profile):
        candidate_refs = sorted(
            {
                item["opportunity_ref"]
                for item in judgments_by_profile[profile_ref]
                if item["ranking_candidate"]
            }
        )
        provider_templates.append(
            {
                "request_schema_version": (
                    "wahojobs_semantic_ranking_quality_provider_input_manifest_v1"
                ),
                "benchmark_version": BENCHMARK_VERSION,
                "request_mode": "relative_reranking",
                "profile_ref": profile_ref,
                "profile_evidence_ref": f"{profile_ref}:profile_facts",
                "candidate_refs_in_provider_order": provider_candidate_order(
                    profile_ref, candidate_refs
                ),
                "candidate_evidence_source": (
                    "validated_native_semantic_packet_from_accepted_lkg_evidence"
                ),
            }
        )
    if forbidden_provider_paths(provider_templates):
        raise PreregistrationError("provider template contains forbidden information")

    readiness_judgments = Counter(
        readiness_by_ref[item["opportunity_ref"]]["source_authority_blocker"]
        or "accepted_lkg_ready_provider_extraction_required"
        for item in judgments
    )
    readiness_opportunities = Counter(
        item["packet_readiness"]["source_authority_blocker"]
        or "accepted_lkg_ready_provider_extraction_required"
        for item in opportunities
    )
    eligibility_counts = Counter(
        baseline_by_judgment[item["judgment_ref"]]["deterministic_eligibility"][
            "status"
        ]
        for item in judgments
    )
    protected_paths = matching_paths_state()
    if any(item["differs_from_head"] for item in protected_paths):
        raise PreregistrationError("a protected matching path differs from HEAD")
    databases_after = database_states()
    enrichment_run_rows_after = read_only_table_count(
        DATABASE_PATH, "opportunity_enrichment_runs"
    )
    if databases_before != databases_after:
        raise PreregistrationError("a workspace database changed during preregistration")
    if enrichment_run_rows_before != enrichment_run_rows_after:
        raise PreregistrationError("provider-run accounting changed during preregistration")

    preregistration = {
        "schema_version": SCHEMA_VERSION,
        "benchmark_version": BENCHMARK_VERSION,
        "milestone": "ground_truth_preregistration_only",
        "status": "frozen",
        "captured_at": captured_at,
        "verdict": "RANKING QUALITY BENCHMARK PREREGISTERED",
        "repository": {
            "branch": git("branch", "--show-current"),
            "head": EXPECTED_HEAD,
            "subject": EXPECTED_SUBJECT,
            "semantic_outputs_in_artifact": False,
        },
        "source_corpus": {
            "golden_fixture": GOLDEN_PATH.relative_to(ROOT).as_posix(),
            "golden_fixture_sha256": sha256_file(GOLDEN_PATH),
            "authority_fixture": EVALUATION_PATH.relative_to(ROOT).as_posix(),
            "authority_fixture_sha256": sha256_file(EVALUATION_PATH),
            "authority_schema_version": evaluation["schema_version"],
            "human_relevance_scale_high_to_low": list(LABEL_ORDER),
            "same_class_semantics": "tie",
            "prior_human_review_provenance": {
                "label_application": commit_provenance(HUMAN_REVIEW_COMMIT),
                "explicit_authority_confirmation": commit_provenance(
                    FOUNDATION_COMMIT
                ),
                "approved_case_count": len(selected_case_ids),
                "approval_actor_convention": evaluation["authority_policy"][
                    "approval_actor_convention"
                ],
                "approval_confirmed_at": evaluation["authority_policy"][
                    "approval_confirmed_at"
                ],
                "labels_predate_semantic_matching_shadow_v1": True,
            },
        },
        "population_selection": {
            "rule": (
                "Include every case ID in matching_foundation_evaluation_v2."
                "authoritative_relevance_case_ids; apply no relevance-value filter. "
                "The only provider-ranking exclusion is the pre-existing "
                "deterministic hard-eligibility boundary."
            ),
            "selection_frozen_before_label_evaluation": True,
            "selection_fields": [
                "authoritative_relevance_case_ids",
                "case_id",
                "profile_id",
                "deterministic_eligibility_status",
            ],
            "selection_forbidden_fields": [
                "expected_label",
                "expected_section",
                "human_notes",
                "legacy_score",
                "legacy_rank",
                "semantic_output",
            ],
            "selected_case_ids": selected_case_ids,
            "human_label_based_exclusions": [],
            "case_count": len(selected_case_ids),
            "profile_count": len(selected_profile_ids),
            "unique_opportunity_count": len(opportunities),
            "ranking_candidate_judgment_count": sum(
                item["ranking_candidate"] for item in judgments
            ),
            "deterministic_safety_only_judgment_count": sum(
                not item["ranking_candidate"] for item in judgments
            ),
            "comparative_profile_count": len(comparative_profile_refs),
            "comparative_profile_refs": comparative_profile_refs,
            "ranking_quality_judgment_count": len(quality_judgment_refs),
            "cross_label_pair_count_before_safety_boundary": len(pairs),
            "evaluable_ranking_quality_pair_count": len(quality_pairs),
            "safety_only_cross_label_pair_count": len(pairs) - len(quality_pairs),
        },
        "profiles": profiles,
        "opportunities": opportunities,
        "human_ground_truth": {
            "labels_are_frozen": True,
            "labels_changed_or_reinterpreted": False,
            "ordinal_grade_mapping": LABEL_GRADE,
            "same_label_pairs_are_ties": True,
            "judgments": judgments,
            "cross_label_pairs": pairs,
        },
        "legacy_baseline_local_only": {
            "must_never_enter_provider_payload": True,
            "matcher": "current_legacy_numeric_matcher_at_repository_head",
            "ordering_rule": (
                "Within each frozen reviewed profile set: descending raw legacy "
                "score, then source, display title, and internal opportunity ref."
            ),
            "candidate_rule": (
                "All reviewed opportunities are scored locally; only deterministic "
                "eligible/unknown survivors receive a semantic-survivor rank."
            ),
            "eligibility_policy_version": foundation.ELIGIBILITY_POLICY_VERSION,
            "eligibility_counts": dict(sorted(eligibility_counts.items())),
            "ranking_quality_pairs": len(quality_pairs),
            "correct_ranking_quality_pairs": sum(
                not item["legacy_inversion"] for item in quality_pairs
            ),
            "legacy_inversions": sum(
                item["legacy_inversion"] for item in quality_pairs
            ),
            "pairwise_preference_accuracy": (
                sum(not item["legacy_inversion"] for item in quality_pairs)
                / len(quality_pairs)
            ),
            "profiles": [
                {
                    "profile_ref": profile_ref,
                    "ordered_items": baseline_items_by_profile[profile_ref],
                }
                for profile_ref in sorted(baseline_items_by_profile)
            ],
        },
        "packet_readiness": {
            "packet_provisioning_performed": False,
            "semantic_packet_available_judgments": 0,
            "semantic_packet_available_opportunities": 0,
            "judgment_blocker_counts": dict(sorted(readiness_judgments.items())),
            "opportunity_blocker_counts": dict(
                sorted(readiness_opportunities.items())
            ),
            "missing_packets_do_not_remove_ground_truth": True,
            "human_derived_fixture_metadata_is_not_packet_authority": True,
        },
        "quality_metrics": {
            "definitions_frozen_before_semantic_output": True,
            "metrics": metric_definitions(),
            "pair_coverage_condition": (
                "both judgments are deterministic survivors with valid packets and "
                "a validated semantic ordering from the same blinded request"
            ),
            "same_label_pair_policy": "exclude_from_wins_losses_and_inversions",
            "safety_metrics_separate": True,
        },
        "provider_blinding": {
            "assertion_frozen": True,
            "provider_order_seed": PROVIDER_ORDER_SEED,
            "candidate_input_order": "sha256_seeded_label_blind_per_profile",
            "forbidden_keys_recursive": sorted(FORBIDDEN_PROVIDER_KEYS),
            "forbidden_information": [
                "human relevance labels or notes",
                "legacy rank, score, section, bucket, or tie break",
                "expected ordering",
                "known legacy inversions",
                "benchmark metric outcomes",
                "semantic output from any previous benchmark",
            ],
            "profile_fact_allowlist": list(PROFILE_FACT_FIELDS),
            "opportunity_evidence_authority": (
                "validated native packet sourced only from accepted/LKG evidence"
            ),
            "human_review_fixture_evidence_forbidden": True,
            "local_metadata_separation": [
                "human_ground_truth",
                "legacy_baseline_local_only",
                "quality_metrics",
            ],
            "provider_request_templates": provider_templates,
            "forbidden_paths_found_in_templates": [],
        },
        "contamination_and_independence": {
            "semantic_matching_implementation": [
                commit_provenance(SEMANTIC_SEAM_COMMIT),
                commit_provenance(SEMANTIC_GROUNDING_COMMIT),
            ],
            "direct_case_id_references_in_semantic_implementation_or_tests": [],
            "direct_human_label_value_use_in_semantic_ordering_contract": False,
            "semantic_architecture_fixture": (
                "tests/fixtures/semantic_matching_shadow_v1.json uses a synthetic "
                "profile and separate reviewed OE contract cases"
            ),
            "generic_architecture_regression_use": (
                "The closed semantic-shadow report read only aggregate foundation "
                "counts and proposed three profile IDs for an unexecuted future run; "
                "it did not load relevance labels into the semantic evaluator."
            ),
            "legacy_baseline_prior_exposure": (
                "The current legacy matcher and fixture metadata have prior golden-set "
                "regression exposure. Results therefore compare against an in-sample, "
                "potentially advantaged legacy baseline and are not a generalization claim."
            ),
            "prior_semantic_shadow_benchmark_outputs_used": False,
            "semantic_matcher_tuning_performed": False,
            "case_exclusions_for_contamination": [],
        },
        "validation": {
            "provider_or_network_calls": 0,
            "semantic_ranking_calls": 0,
            "semantic_packet_provisioning_calls": 0,
            "workspace_database_mutations": 0,
            "generator_has_provider_or_network_capability": False,
            "opportunity_enrichment_run_rows_before": enrichment_run_rows_before,
            "opportunity_enrichment_run_rows_after": enrichment_run_rows_after,
            "opportunity_enrichment_run_rows_unchanged": True,
            "workspace_databases_before": databases_before,
            "workspace_databases_after": databases_after,
            "workspace_databases_byte_identical": True,
            "protected_matching_paths": protected_paths,
            "protected_matching_paths_unchanged_from_head": True,
        },
    }
    preregistration["content_sha256"] = fingerprint(preregistration)
    validate_preregistration(preregistration)
    return preregistration


def validate_preregistration(value: dict) -> None:
    if type(value) is not dict or value.get("schema_version") != SCHEMA_VERSION:
        raise PreregistrationError("unsupported preregistration schema")
    without_hash = deepcopy(value)
    observed_hash = without_hash.pop("content_sha256", None)
    if observed_hash != fingerprint(without_hash):
        raise PreregistrationError("content fingerprint mismatch")
    population = value["population_selection"]
    if (
        population["case_count"] != 30
        or population["profile_count"] != 9
        or population["ranking_candidate_judgment_count"] != 24
        or population["comparative_profile_count"] != 4
        or population["ranking_quality_judgment_count"] != 14
        or population["cross_label_pair_count_before_safety_boundary"] != 30
        or population["evaluable_ranking_quality_pair_count"] != 14
        or population["safety_only_cross_label_pair_count"] != 16
    ):
        raise PreregistrationError("frozen population counts are inconsistent")
    if len(value["human_ground_truth"]["judgments"]) != 30:
        raise PreregistrationError("ground-truth judgment count is invalid")
    if any(
        item["same_label_pair"]
        for item in value["human_ground_truth"]["cross_label_pairs"]
    ):
        raise PreregistrationError("same-label pair entered the evaluation set")
    baseline = value["legacy_baseline_local_only"]
    if (
        baseline["ranking_quality_pairs"] != 14
        or baseline["correct_ranking_quality_pairs"] != 10
        or baseline["legacy_inversions"] != 4
        or baseline["pairwise_preference_accuracy"] != 10 / 14
    ):
        raise PreregistrationError("legacy baseline summary is inconsistent")
    blinding = value["provider_blinding"]
    paths = forbidden_provider_paths(blinding["provider_request_templates"])
    if paths or blinding["forbidden_paths_found_in_templates"]:
        raise PreregistrationError("provider blinding assertion failed")
    if value["repository"]["semantic_outputs_in_artifact"]:
        raise PreregistrationError("semantic output is forbidden in preregistration")
    validation = value["validation"]
    if (
        validation["provider_or_network_calls"]
        or validation["semantic_ranking_calls"]
        or validation["semantic_packet_provisioning_calls"]
        or validation["workspace_database_mutations"]
        or not validation["workspace_databases_byte_identical"]
        or not validation["protected_matching_paths_unchanged_from_head"]
        or not validation["opportunity_enrichment_run_rows_unchanged"]
        or validation["opportunity_enrichment_run_rows_before"]
        != validation["opportunity_enrichment_run_rows_after"]
    ):
        raise PreregistrationError("offline preservation assertions failed")


def verify_file(path: Path) -> dict:
    frozen = read_json(path)
    validate_preregistration(frozen)
    rebuilt = build_preregistration(frozen["captured_at"])
    if rebuilt != frozen:
        raise PreregistrationError("preregistration does not reproduce at current HEAD")
    return frozen


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--build", action="store_true")
    action.add_argument("--verify", action="store_true")
    parser.add_argument("--captured-at")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    output = args.output.resolve()
    if args.build:
        if not args.captured_at:
            raise SystemExit("--captured-at is required with --build")
        value = build_preregistration(args.captured_at)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"wrote={output}")
        print(f"content_sha256={value['content_sha256']}")
        print(f"file_sha256={sha256_file(output)}")
        return 0
    value = verify_file(output)
    print(f"verified={output}")
    print(f"content_sha256={value['content_sha256']}")
    print(f"file_sha256={sha256_file(output)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

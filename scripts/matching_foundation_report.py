#!/usr/bin/env python3
"""Deterministic, read-only report for the score-free matching foundation.

The default report uses only versioned JSON fixtures.  A workspace database is
optional and is opened immutable/query-only solely to capture or verify the
versioned inventory observation snapshot.  No model, production matcher write,
MatchRun persistence, UI path, or database mutation is reachable here.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import matching_quality_report as golden_tools  # noqa: E402
import profile_match_digest as legacy_matcher  # noqa: E402
import profile_to_matches_preview as preview  # noqa: E402
from wahojobs.matching.foundation_contracts import (  # noqa: E402
    DETERMINISTIC_ELIGIBILITY_CRITERIA_V1,
    DETERMINISTIC_ELIGIBILITY_DECISION_SCHEMA_VERSION,
    MATCH_RUN_CANDIDATE_DISPOSITION_SCHEMA_VERSION,
    MATCH_RUN_RESULT_SCHEMA_VERSION,
    MATCH_RUN_SNAPSHOT_SCHEMA_VERSION,
    OPPORTUNITY_RELATIONSHIP_SCHEMA_VERSION,
    SEMANTIC_RERANK_REQUEST_SCHEMA_VERSION,
    SEMANTIC_RERANK_RESULT_SCHEMA_VERSION,
    SHORTLIST_CANDIDATE_SCHEMA_VERSION,
    DeterministicEligibilityDecisionV1,
    canonical_json,
)
from wahojobs.matching.typed_criteria import (  # noqa: E402
    bridge_existing_matcher_eligibility,
)
from wahojobs.opportunity_enrichment import (  # noqa: E402
    DERIVATION_RECIPE_VERSION,
    FIELD_DEFAULTS,
    LLM_ACCEPTANCE_GUARDS_VERSION,
    SEMANTIC_INPUT_VERSION,
    STALE_REASON_DERIVATION_CONTRACT_CHANGED,
    STALE_REASON_MISSING_ENRICHMENT,
    STALE_REASON_SOURCE_INPUT_CHANGED,
    classify_enrichment_freshness,
    load_semantic_input,
    resolve_effective_enrichments,
)


EVALUATION_SCHEMA_VERSION = "matching_foundation_evaluation_v2"
INVENTORY_SNAPSHOT_SCHEMA_VERSION = "matching_foundation_inventory_snapshot_v2"
REPORT_SCHEMA_VERSION = "matching_foundation_report_v3"
DEFAULT_GOLDEN_PATH = ROOT / "tests" / "fixtures" / "matching_golden_set.json"
DEFAULT_EVALUATION_PATH = (
    ROOT / "tests" / "fixtures" / "matching_foundation_evaluation.json"
)
DEFAULT_INVENTORY_SNAPSHOT_PATH = (
    ROOT / "tests" / "fixtures" / "matching_foundation_inventory_snapshot.json"
)
ELIGIBILITY_POLICY_VERSION = "objective_existing_matcher_bridge_v1"
IDENTITY_POLICY_VERSION = "deterministic_identity_observation_v1"
LEGACY_SHORTLIST_BENCHMARK_VERSION = "legacy_score_ordering_snapshot_v1"
APPROVAL_ACTOR = "user_confirmation"

READINESS_FIELD_SPECS = (
    ("attributes.role.role_family", "critical", "role identity"),
    ("attributes.role.professional_domains", "critical", "domain fit"),
    ("attributes.role.work_activities", "critical", "actual work"),
    ("attributes.role.specializations", "important", "specialty fit"),
    ("attributes.content.responsibilities", "critical", "actual work"),
    ("attributes.content.candidate_profile", "critical", "candidate fit"),
    ("attributes.content.quick_take", "important", "candidate explanation"),
    ("attributes.content.caveats", "important", "candidate caveats"),
    ("attributes.requirements.skills_required", "critical", "objective requirements"),
    ("attributes.requirements.skills_preferred", "important", "semantic fit"),
    ("attributes.requirements.education.minimum_level", "critical", "objective requirements"),
    ("attributes.requirements.education.accepted_alternatives", "important", "objective requirements"),
    ("attributes.requirements.credentials", "critical", "objective requirements"),
    ("attributes.requirements.licenses", "critical", "objective requirements"),
    ("attributes.requirements.years_experience_min", "critical", "objective requirements"),
    ("attributes.requirements.languages", "critical", "objective requirements"),
    ("attributes.work_arrangement.workplace_mode", "important", "actionability"),
    ("attributes.work_arrangement.location_scope", "critical", "objective requirements"),
    ("attributes.work_arrangement.eligible_countries", "critical", "objective requirements"),
    ("attributes.work_arrangement.eligible_regions", "important", "objective requirements"),
    ("attributes.work_arrangement.engagement_type", "important", "work preference"),
    ("attributes.work_arrangement.hours_per_week_min", "important", "work preference"),
    ("attributes.work_arrangement.hours_per_week_max", "important", "work preference"),
    ("attributes.work_arrangement.schedule_type", "important", "work preference"),
    ("attributes.compensation.disclosed", "important", "candidate decision"),
    ("attributes.compensation.currency", "important", "candidate decision"),
    ("attributes.compensation.amount_min", "important", "candidate decision"),
    ("attributes.compensation.amount_max", "important", "candidate decision"),
    ("attributes.compensation.period", "important", "candidate decision"),
)
CORE_SEMANTIC_IDENTITY_FIELDS = (
    "attributes.role.role_family",
    "attributes.role.professional_domains",
    "attributes.role.work_activities",
)
CORE_SEMANTIC_CONTENT_FIELDS = (
    "attributes.content.responsibilities",
    "attributes.content.candidate_profile",
)
STRUCTURED_REQUIREMENT_SIGNAL_FIELDS = (
    "attributes.requirements.skills_required",
    "attributes.requirements.education.minimum_level",
    "attributes.requirements.credentials",
    "attributes.requirements.licenses",
    "attributes.requirements.years_experience_min",
    "attributes.requirements.languages",
    "attributes.work_arrangement.location_scope",
    "attributes.work_arrangement.eligible_countries",
)
SOURCE_FACT_FLAGS = frozenset(
    {
        "canonical_title",
        "company_name",
        "rich_content",
        "source_category",
        "variant_commitment",
        "variant_location",
        "variant_title",
    }
)
RELATIONSHIP_COMPARE_FIELDS = (
    "title",
    "location",
    "department",
    "expertise",
    "commitment",
    "source_category",
    "language",
    "language_locale",
    "opportunity_kind",
)
ELIGIBILITY_VARIANT_FIELDS = frozenset({"location", "language", "language_locale"})
RELATIONSHIP_ROW_FIELDS = (
    "job_id",
    "external_id",
    "source_hash",
    "title",
    "location",
    "department",
    "expertise",
    "commitment",
    "url",
    "opportunity_kind",
    "canonical_opportunity_id",
    "canonical_title",
    "source_category",
    "language",
    "language_locale",
    "company",
    "source_slug",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class EvaluationHarnessError(ValueError):
    pass


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Generate or verify the matching-foundation evaluation report."
    )
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN_PATH)
    parser.add_argument("--evaluation", type=Path, default=DEFAULT_EVALUATION_PATH)
    parser.add_argument(
        "--inventory-snapshot",
        type=Path,
        default=DEFAULT_INVENTORY_SNAPSHOT_PATH,
    )
    parser.add_argument(
        "--shortlist-size",
        type=int,
        help="Override the manifest's initial shortlist size (normally 32).",
    )
    parser.add_argument(
        "--output",
        default="-",
        help="Markdown destination; default '-' writes deterministic output to stdout.",
    )
    parser.add_argument("--json-output", type=Path)
    parser.add_argument(
        "--capture-database",
        type=Path,
        help="Immutable/query-only database used to create --capture-snapshot.",
    )
    parser.add_argument(
        "--capture-snapshot",
        type=Path,
        help="Explicit destination for a newly captured versioned observation snapshot.",
    )
    parser.add_argument(
        "--captured-at",
        help="Required ISO-8601 confirmation time when capturing a snapshot.",
    )
    parser.add_argument(
        "--verify-database",
        type=Path,
        help="Read-only live database whose observations must match the loaded snapshot.",
    )
    return parser.parse_args(argv)


def load_json(path: Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EvaluationHarnessError(f"missing input: {path}") from exc
    except json.JSONDecodeError as exc:
        raise EvaluationHarnessError(f"invalid JSON: {path}") from exc
    if type(value) is not dict:
        raise EvaluationHarnessError(f"expected a JSON object: {path}")
    return value


def sha256_file(path: Path) -> str:
    digest = sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fingerprint(value) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def database_state(path: Path) -> dict:
    path = Path(path).resolve()
    state = {}
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        if candidate.exists():
            state[candidate.name] = {
                "size": candidate.stat().st_size,
                "sha256": sha256_file(candidate),
            }
    return state


def open_immutable_database(path: Path) -> sqlite3.Connection:
    path = Path(path).resolve()
    if not path.is_file():
        raise EvaluationHarnessError(f"database does not exist: {path}")
    connection = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro&immutable=1",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    mutation_actions = {
        getattr(sqlite3, name)
        for name in (
            "SQLITE_INSERT",
            "SQLITE_UPDATE",
            "SQLITE_DELETE",
            "SQLITE_CREATE_INDEX",
            "SQLITE_CREATE_TABLE",
            "SQLITE_CREATE_TEMP_INDEX",
            "SQLITE_CREATE_TEMP_TABLE",
            "SQLITE_CREATE_TEMP_TRIGGER",
            "SQLITE_CREATE_TEMP_VIEW",
            "SQLITE_CREATE_TRIGGER",
            "SQLITE_CREATE_VIEW",
            "SQLITE_DROP_INDEX",
            "SQLITE_DROP_TABLE",
            "SQLITE_DROP_TEMP_INDEX",
            "SQLITE_DROP_TEMP_TABLE",
            "SQLITE_DROP_TEMP_TRIGGER",
            "SQLITE_DROP_TEMP_VIEW",
            "SQLITE_DROP_TRIGGER",
            "SQLITE_DROP_VIEW",
            "SQLITE_ALTER_TABLE",
            "SQLITE_REINDEX",
            "SQLITE_ANALYZE",
            "SQLITE_ATTACH",
            "SQLITE_DETACH",
        )
        if hasattr(sqlite3, name)
    }

    def authorize(action, _first, _second, _database, _trigger):
        return sqlite3.SQLITE_DENY if action in mutation_actions else sqlite3.SQLITE_OK

    connection.set_authorizer(authorize)
    return connection


def valid_iso_datetime(value) -> bool:
    if type(value) is not str or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def approved(item: dict, actor: str, approved_at: str) -> bool:
    return (
        item.get("approved_by") == actor
        and item.get("approved_at") == approved_at
        and valid_iso_datetime(item.get("approved_at"))
    )


def validate_evaluation_set(golden: dict, evaluation: dict) -> dict:
    if evaluation.get("schema_version") != EVALUATION_SCHEMA_VERSION:
        raise EvaluationHarnessError("unsupported evaluation schema version")
    if golden.get("version") != evaluation.get("source_fixture_version"):
        raise EvaluationHarnessError("golden fixture version does not match evaluation manifest")
    authority_policy = evaluation.get("authority_policy")
    if type(authority_policy) is not dict:
        raise EvaluationHarnessError("authority policy is missing")
    actor = authority_policy.get("approval_actor_convention")
    approval_time = authority_policy.get("approval_confirmed_at")
    if actor != APPROVAL_ACTOR:
        raise EvaluationHarnessError("unsupported approval actor convention")
    if not valid_iso_datetime(approval_time):
        raise EvaluationHarnessError("authority confirmation time is invalid")
    if authority_policy.get("draft_labels_are_ground_truth") is not False:
        raise EvaluationHarnessError("draft labels must not be ground truth")

    cases = golden.get("cases")
    if type(cases) is not list or not cases:
        raise EvaluationHarnessError("golden fixture has no cases")
    case_index = {case.get("case_id"): case for case in cases}
    if None in case_index or len(case_index) != len(cases):
        raise EvaluationHarnessError("golden fixture case IDs must be present and unique")
    human_ids = {
        case["case_id"] for case in cases if case.get("label_source") == "human_reviewed"
    }
    draft_ids = {
        case["case_id"] for case in cases if case.get("label_source") == "codex_draft"
    }
    manifest_ids = set(evaluation.get("authoritative_relevance_case_ids") or [])
    if manifest_ids != human_ids or manifest_ids & draft_ids:
        raise EvaluationHarnessError(
            "authoritative relevance manifest must equal approved human-reviewed cases"
        )
    for case_id in sorted(human_ids):
        case = case_index[case_id]
        if not approved(case, actor, approval_time):
            raise EvaluationHarnessError("authoritative relevance case lacks approval")
        if type(case.get("matcher_input_snapshot")) is not dict:
            raise EvaluationHarnessError("authoritative cases require self-contained snapshots")

    derived = []
    approved_interpretations = []
    for item in evaluation.get("eligibility_cases") or []:
        case = case_index.get(item.get("case_id"))
        if case is None or case.get("label_source") != "human_reviewed":
            raise EvaluationHarnessError("eligibility case must reference approved relevance truth")
        if item.get("criterion_id") not in DETERMINISTIC_ELIGIBILITY_CRITERIA_V1:
            raise EvaluationHarnessError("eligibility criterion is outside the closed authority set")
        if item.get("expected_outcome") not in {
            "pass",
            "fail",
            "unknown",
            "not_applicable",
        }:
            raise EvaluationHarnessError("invalid eligibility expectation")
        if item.get("authority") == "derived_from_human_reviewed_relevance_rule":
            if item.get("basis") != case.get("regression_rule"):
                raise EvaluationHarnessError("derived regression lacks its reviewed rule basis")
            derived.append(item)
        elif item.get("authority") == "human_approved_interpretation":
            if not approved(item, actor, approval_time):
                raise EvaluationHarnessError("eligibility interpretation lacks approval")
            approved_interpretations.append(item)
        else:
            raise EvaluationHarnessError("invalid eligibility authority")

    relationships = evaluation.get("relationship_cases") or []
    relationship_ids = [item.get("relationship_case_id") for item in relationships]
    if (
        any(not item for item in relationship_ids)
        or len(relationship_ids) != len(set(relationship_ids))
    ):
        raise EvaluationHarnessError("relationship case IDs must be present and unique")
    if any(
        item.get("authority") != "human_approved_relationship"
        or not approved(item, actor, approval_time)
        for item in relationships
    ):
        raise EvaluationHarnessError("relationship judgments must be explicitly approved")

    labels = Counter(case_index[case_id]["expected_label"] for case_id in human_ids)
    return {
        "golden_cases_total": len(cases),
        "approved_relevance_cases": len(human_ids),
        "draft_relevance_cases_excluded": len(draft_ids),
        "approved_label_counts": dict(sorted(labels.items())),
        "approved_self_contained_snapshots": len(human_ids),
        "derived_eligibility_regressions": len(derived),
        "human_approved_eligibility_interpretations": len(approved_interpretations),
        "human_approved_relationship_judgments": len(relationships),
        "approved_judgments_total": (
            len(human_ids) + len(approved_interpretations) + len(relationships)
        ),
        "draft_exclusion_verified": not bool(manifest_ids & draft_ids),
        "approval_metadata_verified": True,
        "case_index": case_index,
    }


def authoritative_match(profile: dict, row: dict) -> dict:
    scored = legacy_matcher.score_opportunity(profile, row)
    return preview.apply_preview_guardrails(profile, row, scored)


def objective_eligibility_outcomes(match: dict) -> tuple:
    bridged = bridge_existing_matcher_eligibility(match)
    return tuple(
        item
        for item in bridged
        if item.criterion_id in DETERMINISTIC_ELIGIBILITY_CRITERIA_V1
    )


def evaluate_eligibility(
    evaluation: dict,
    case_index: dict,
    profiles: dict[str, dict],
) -> dict:
    rows = []
    for expectation in evaluation["eligibility_cases"]:
        case = case_index[expectation["case_id"]]
        row = golden_tools.matcher_input_from_snapshot(case["matcher_input_snapshot"])
        match = authoritative_match(profiles[case["profile_id"]], row)
        decision = DeterministicEligibilityDecisionV1.from_outcomes(
            opportunity_ref=evaluation_opportunity_ref(row, case["case_id"]),
            policy_version=ELIGIBILITY_POLICY_VERSION,
            outcomes=objective_eligibility_outcomes(match),
        )
        actual = {item.criterion_id: item for item in decision.outcomes}.get(
            expectation["criterion_id"]
        )
        actual_outcome = actual.outcome if actual is not None else "missing"
        rows.append(
            {
                "case_id": case["case_id"],
                "profile_id": case["profile_id"],
                "title": case["title"],
                "criterion_id": expectation["criterion_id"],
                "expected_outcome": expectation["expected_outcome"],
                "actual_outcome": actual_outcome,
                "aggregate_status": decision.status,
                "authority": expectation["authority"],
                "correct": actual_outcome == expectation["expected_outcome"],
            }
        )
    derived = [
        item
        for item in rows
        if item["authority"] == "derived_from_human_reviewed_relevance_rule"
    ]
    human = [
        item for item in rows if item["authority"] == "human_approved_interpretation"
    ]
    approved_unknowns = [
        item for item in human if item["expected_outcome"] == "unknown"
    ]
    return {
        "policy_version": ELIGIBILITY_POLICY_VERSION,
        "cases": rows,
        "derived_regression_cases": len(derived),
        "derived_regression_correct": sum(item["correct"] for item in derived),
        "derived_regression_accuracy": ratio(
            sum(item["correct"] for item in derived), len(derived)
        ),
        "human_approved_cases": len(human),
        "human_approved_correct": sum(item["correct"] for item in human),
        "human_approved_accuracy": ratio(
            sum(item["correct"] for item in human), len(human)
        ),
        "human_approved_unknown_cases": len(approved_unknowns),
        "human_approved_unknown_preserved": sum(
            item["actual_outcome"] == "unknown" for item in approved_unknowns
        ),
        "human_approved_unknown_preservation": ratio(
            sum(item["actual_outcome"] == "unknown" for item in approved_unknowns),
            len(approved_unknowns),
        ),
    }


def evaluation_opportunity_ref(row: dict, case_id: str) -> str:
    canonical_id = positive_int_or_none(row.get("canonical_opportunity_id"))
    if canonical_id is not None:
        return f"canonical:{canonical_id}"
    job_id = positive_int_or_none(row.get("job_id"))
    if job_id is not None:
        return f"job:{job_id}"
    return "fixture:" + sha256(case_id.encode("utf-8")).hexdigest()[:24]


def positive_int_or_none(value) -> int | None:
    if type(value) is int and value > 0:
        return value
    if type(value) is str and value.isdigit() and int(value) > 0:
        return int(value)
    return None


def corpus_identity(row: dict) -> str:
    canonical_id = positive_int_or_none(row.get("canonical_opportunity_id"))
    if canonical_id is not None:
        return f"canonical:{canonical_id}"
    url = str(row.get("url") or "").strip().casefold().rstrip("/")
    if url:
        return "urlsha:" + sha256(url.encode("utf-8")).hexdigest()[:24]
    material = "\x1f".join(
        normalize_value(row.get(field))
        for field in ("source_slug", "source", "title", "location", "expertise")
    )
    return "fixtureop:" + sha256(material.encode("utf-8")).hexdigest()[:24]


def row_signature(row: dict) -> tuple:
    return (
        corpus_identity(row),
        normalize_value(row.get("location")),
        normalize_value(row.get("title") or row.get("canonical_title")),
    )


def capture_legacy_ranking_observation(
    active_rows: list[dict],
    authoritative_cases: list[dict],
    profiles: dict[str, dict],
) -> dict:
    corpus = [dict(row) for row in active_rows]
    existing_signatures = {row_signature(row) for row in corpus}
    for case in authoritative_cases:
        row = golden_tools.matcher_input_from_snapshot(case["matcher_input_snapshot"])
        signature = row_signature(row)
        if signature not in existing_signatures:
            corpus.append(row)
            existing_signatures.add(signature)
    rows_by_identity = defaultdict(list)
    for row in corpus:
        rows_by_identity[corpus_identity(row)].append(row)
    positives = [
        case
        for case in authoritative_cases
        if case["expected_label"] in {"strong", "plausible"}
    ]
    rankings = []
    for profile_id in sorted({case["profile_id"] for case in positives}):
        profile = profiles[profile_id]
        ranked = []
        for identity, variants in rows_by_identity.items():
            best_score = None
            for row in variants:
                scored = legacy_matcher.score_opportunity(profile, row)
                if any(
                    item.outcome == "fail"
                    for item in bridge_existing_matcher_eligibility(scored)
                ):
                    continue
                score = int(scored["score"])
                if best_score is None or score > best_score:
                    best_score = score
            if best_score is not None:
                ranked.append((identity, best_score))
        ranked.sort(key=lambda item: (-item[1], item[0]))
        rankings.append(
            {
                "profile_id": profile_id,
                "ranked_candidate_refs": [item[0] for item in ranked],
            }
        )
    return {
        "benchmark_version": LEGACY_SHORTLIST_BENCHMARK_VERSION,
        "active_inventory_rows": len(active_rows),
        "evaluation_corpus_rows": len(corpus),
        "evaluation_corpus_opportunity_groups": len(rows_by_identity),
        "profile_rankings": rankings,
    }


def evaluate_shortlist_recall(
    observation: dict,
    authoritative_cases: list[dict],
    shortlist_size: int,
) -> dict:
    if type(shortlist_size) is not int or shortlist_size <= 0:
        raise EvaluationHarnessError("shortlist size must be a positive integer")
    positives = [
        case
        for case in authoritative_cases
        if case["expected_label"] in {"strong", "plausible"}
    ]
    ranking_index = {
        item["profile_id"]: item["ranked_candidate_refs"]
        for item in observation["profile_rankings"]
    }
    per_profile = []
    case_results = []
    for profile_id in sorted({case["profile_id"] for case in positives}):
        ranking = ranking_index.get(profile_id)
        if ranking is None:
            raise EvaluationHarnessError("inventory snapshot lacks a required profile ranking")
        ranks = {identity: rank for rank, identity in enumerate(ranking, start=1)}
        profile_results = []
        for case in [item for item in positives if item["profile_id"] == profile_id]:
            row = golden_tools.matcher_input_from_snapshot(case["matcher_input_snapshot"])
            identity = corpus_identity(row)
            rank = ranks.get(identity)
            result = {
                "case_id": case["case_id"],
                "profile_id": profile_id,
                "expected_label": case["expected_label"],
                "title": case["title"],
                "candidate_identity": identity,
                "rank": rank,
                "recalled": rank is not None and rank <= shortlist_size,
            }
            profile_results.append(result)
            case_results.append(result)
        per_profile.append(
            {
                "profile_id": profile_id,
                "positive_cases": len(profile_results),
                "recalled_at_k": sum(item["recalled"] for item in profile_results),
                "recall_at_k": ratio(
                    sum(item["recalled"] for item in profile_results),
                    len(profile_results),
                ),
                "top_candidate_refs": ranking[:shortlist_size],
            }
        )
    strong = [item for item in case_results if item["expected_label"] == "strong"]
    return {
        "benchmark_version": observation["benchmark_version"],
        "measurement_note": (
            "Versioned ordering from the current legacy numeric matcher is a comparison "
            "benchmark only; it grants no new matching authority."
        ),
        "identity_note": (
            "Legacy recall groups canonical IDs and may credit a different variant in the "
            "same canonical group; variant-sensitive evaluation remains separate."
        ),
        "shortlist_size": shortlist_size,
        "active_inventory_rows": observation["active_inventory_rows"],
        "evaluation_corpus_rows": observation["evaluation_corpus_rows"],
        "evaluation_corpus_opportunity_groups": observation[
            "evaluation_corpus_opportunity_groups"
        ],
        "profiles_evaluated": len(per_profile),
        "strong_cases": len(strong),
        "strong_recalled": sum(item["recalled"] for item in strong),
        "strong_recall_at_k": ratio(
            sum(item["recalled"] for item in strong), len(strong)
        ),
        "strong_or_plausible_cases": len(case_results),
        "strong_or_plausible_recalled": sum(
            item["recalled"] for item in case_results
        ),
        "strong_or_plausible_recall_at_k": ratio(
            sum(item["recalled"] for item in case_results), len(case_results)
        ),
        "missed_cases": [item for item in case_results if not item["recalled"]],
        "case_results": case_results,
        "per_profile": per_profile,
    }


def semantic_source_flags(semantic_input: dict) -> list[str]:
    flags = set()
    if str(semantic_input.get("company", {}).get("name") or "").strip():
        flags.add("company_name")
    canonical = semantic_input.get("canonical") or {}
    if str(canonical.get("canonical_title") or "").strip():
        flags.add("canonical_title")
    if str(canonical.get("source_category") or "").strip():
        flags.add("source_category")
    variants = semantic_input.get("variants") or []
    for field, flag in (
        ("title", "variant_title"),
        ("location", "variant_location"),
        ("commitment", "variant_commitment"),
    ):
        if any(str(item.get(field) or "").strip() for item in variants):
            flags.add(flag)
    if any(
        str(item.get("body") or "").strip()
        for item in (semantic_input.get("rich_content") or [])
    ):
        flags.add("rich_content")
    return sorted(flags)


def capture_enrichment_observation(
    connection: sqlite3.Connection,
    active_rows: list[dict],
) -> dict:
    canonical_ids = sorted(
        {
            int(row["canonical_opportunity_id"])
            for row in active_rows
            if positive_int_or_none(row.get("canonical_opportunity_id")) is not None
        }
    )
    effective = resolve_effective_enrichments(connection, canonical_ids)
    stored_rows = connection.execute(
        "SELECT * FROM opportunity_enrichments"
    ).fetchall()
    stored = {
        int(row["canonical_opportunity_id"]): dict(row)
        for row in stored_rows
        if int(row["canonical_opportunity_id"]) in set(canonical_ids)
    }
    observations = []
    for canonical_id in canonical_ids:
        semantic_input = load_semantic_input(connection, canonical_id)
        persisted = stored.get(canonical_id)
        effective_item = effective.get(canonical_id)
        freshness_evidence = classify_enrichment_freshness(
            semantic_input,
            persisted if effective_item is not None else None,
        )
        known_fields = []
        if freshness_evidence["freshness"] == "current":
            unknown_fields = set(
                effective_item["document"].get("unknown_fields") or []
            )
            known_fields = sorted(set(FIELD_DEFAULTS) - unknown_fields)
        observations.append(
            {
                "canonical_opportunity_id": canonical_id,
                **freshness_evidence,
                "stored_status": (
                    persisted.get("status") if persisted is not None else None
                ),
                "model_enriched": bool(
                    freshness_evidence["freshness"] == "current"
                    and persisted is not None
                    and persisted.get("model_provider")
                ),
                "source_fact_flags": semantic_source_flags(semantic_input),
                "known_fields": known_fields,
            }
        )
    return {"canonicals": observations}


def field_known(item: dict, field_path: str) -> bool:
    return (
        item["freshness"] == "current"
        and field_path in set(item["known_fields"])
    )


def semantic_packet_ready(item: dict) -> bool:
    source_identity = {
        "company_name",
        "canonical_title",
    }.issubset(item["source_fact_flags"])
    structured_identity = field_known(
        item, CORE_SEMANTIC_IDENTITY_FIELDS[0]
    ) and any(field_known(item, field) for field in CORE_SEMANTIC_IDENTITY_FIELDS[1:])
    structured_content = any(
        field_known(item, field) for field in CORE_SEMANTIC_CONTENT_FIELDS
    )
    return source_identity and structured_identity and structured_content


def structured_requirement_signal(item: dict) -> bool:
    return any(
        field_known(item, field) for field in STRUCTURED_REQUIREMENT_SIGNAL_FIELDS
    )


def analyze_enrichment_readiness(
    observation: dict,
    shortlist_opportunity_refs: set[str],
) -> dict:
    canonicals = observation["canonicals"]
    by_id = {item["canonical_opportunity_id"]: item for item in canonicals}
    freshness_counts = Counter(item["freshness"] for item in canonicals)
    stale_reason_counts = Counter(
        reason for item in canonicals for reason in item["stale_reasons"]
    )
    source_input_status_counts = Counter(
        item["source_input_status"] for item in canonicals
    )
    derivation_status_counts = Counter(
        item["derivation_status"] for item in canonicals
    )
    field_rows = []
    for field_path, priority, purpose in READINESS_FIELD_SPECS:
        known = sum(field_known(item, field_path) for item in canonicals)
        current_unknown = sum(
            item["freshness"] == "current" and not field_known(item, field_path)
            for item in canonicals
        )
        field_rows.append(
            {
                "field_path": field_path,
                "priority": priority,
                "purpose": purpose,
                "known_current": known,
                "unknown_current": current_unknown,
                "unavailable_stale": freshness_counts["stale"],
                "unavailable_missing": freshness_counts["missing"],
                "coverage_all_active": ratio(known, len(canonicals)),
            }
        )
    source_identity = sum(
        {"company_name", "canonical_title"}.issubset(item["source_fact_flags"])
        for item in canonicals
    )
    semantic_ready = sum(semantic_packet_ready(item) for item in canonicals)
    requirement_signal = sum(
        structured_requirement_signal(item) for item in canonicals
    )
    rich_source = sum("rich_content" in item["source_fact_flags"] for item in canonicals)
    current_unknown_counts = sorted(
        len(FIELD_DEFAULTS) - len(item["known_fields"])
        for item in canonicals
        if item["freshness"] == "current"
    )
    shortlist_refs = sorted(shortlist_opportunity_refs)
    shortlist_semantic_ready = 0
    shortlist_requirement_signal = 0
    shortlist_current = 0
    shortlist_not_in_snapshot = 0
    for opportunity_ref in shortlist_refs:
        canonical_id = canonical_id_from_ref(opportunity_ref)
        item = by_id.get(canonical_id) if canonical_id is not None else None
        if item is None:
            shortlist_not_in_snapshot += 1
            continue
        shortlist_current += item["freshness"] == "current"
        shortlist_semantic_ready += semantic_packet_ready(item)
        shortlist_requirement_signal += structured_requirement_signal(item)
    return {
        "active_canonical_opportunities": len(canonicals),
        "freshness_counts": {
            key: freshness_counts.get(key, 0)
            for key in ("current", "stale", "missing")
        },
        "freshness_reason_counts": {
            "current": freshness_counts.get("current", 0),
            "contract_recipe_stale": stale_reason_counts.get(
                STALE_REASON_DERIVATION_CONTRACT_CHANGED,
                0,
            ),
            "source_changed": stale_reason_counts.get(
                STALE_REASON_SOURCE_INPUT_CHANGED,
                0,
            ),
            "missing": stale_reason_counts.get(
                STALE_REASON_MISSING_ENRICHMENT,
                0,
            ),
        },
        "source_input_status_counts": {
            key: source_input_status_counts.get(key, 0)
            for key in ("current", "changed", "not_comparable", "missing")
        },
        "derivation_status_counts": {
            key: derivation_status_counts.get(key, 0)
            for key in (
                "current",
                "legacy_compatible",
                "changed",
                "unknown",
                "missing",
            )
        },
        "current_enrichment_coverage": ratio(
            freshness_counts["current"], len(canonicals)
        ),
        "model_enriched_current": sum(item["model_enriched"] for item in canonicals),
        "source_identity_ready_canonicals": source_identity,
        "source_identity_coverage": ratio(source_identity, len(canonicals)),
        "rich_source_canonicals": rich_source,
        "rich_source_coverage": ratio(rich_source, len(canonicals)),
        "median_unknown_fields_current": (
            current_unknown_counts[len(current_unknown_counts) // 2]
            if current_unknown_counts
            else None
        ),
        "minimum_semantic_packet_ready_canonicals": semantic_ready,
        "minimum_semantic_packet_coverage": ratio(semantic_ready, len(canonicals)),
        "structured_requirement_signal_canonicals": requirement_signal,
        "structured_requirement_signal_coverage": ratio(
            requirement_signal, len(canonicals)
        ),
        "baseline_shortlist_opportunities": len(shortlist_refs),
        "baseline_shortlist_current_enrichments": shortlist_current,
        "baseline_shortlist_minimum_semantic_packet_ready": shortlist_semantic_ready,
        "baseline_shortlist_structured_requirement_signal": shortlist_requirement_signal,
        "baseline_shortlist_not_in_active_snapshot": shortlist_not_in_snapshot,
        "field_readiness": field_rows,
    }


def load_relationship_rows(
    connection: sqlite3.Connection,
    job_ids: list[int],
) -> list[dict]:
    placeholders = ",".join("?" for _ in job_ids)
    rows = connection.execute(
        "SELECT j.id AS job_id, j.external_id, j.source_hash, j.title, j.location, "
        "j.department, j.expertise, j.commitment, j.url, j.opportunity_kind, "
        "j.canonical_opportunity_id, co.canonical_title, co.source_category, "
        "co.language, co.language_locale, c.name AS company, c.slug AS source_slug "
        "FROM jobs j JOIN companies c ON c.id = j.company_id "
        "LEFT JOIN canonical_opportunities co ON co.id = j.canonical_opportunity_id "
        f"WHERE j.id IN ({placeholders}) ORDER BY j.id",
        job_ids,
    ).fetchall()
    return [
        {field: dict(row).get(field) for field in RELATIONSHIP_ROW_FIELDS}
        for row in rows
    ]


def classify_relationship(left: dict, right: dict) -> tuple[str, list[str]]:
    differences = sorted(
        field
        for field in RELATIONSHIP_COMPARE_FIELDS
        if normalize_value(left.get(field)) != normalize_value(right.get(field))
    )
    same_canonical = (
        positive_int_or_none(left.get("canonical_opportunity_id")) is not None
        and left.get("canonical_opportunity_id")
        == right.get("canonical_opportunity_id")
    )
    same_company = normalize_value(left.get("company")) == normalize_value(
        right.get("company")
    )
    same_title = normalize_value(left.get("title")) == normalize_value(
        right.get("title")
    )
    stable_identity_fields = ("external_id", "source_hash", "url")
    stable_identity_equal = all(
        normalize_value(left.get(field))
        and normalize_value(left.get(field)) == normalize_value(right.get(field))
        for field in stable_identity_fields
    )
    if same_canonical and not differences:
        if stable_identity_equal:
            return "exact_duplicate", differences
        return "unresolved_related", differences
    if same_canonical and set(differences).issubset(ELIGIBILITY_VARIANT_FIELDS):
        return "eligibility_variant", differences
    if same_canonical:
        return "material_variant", differences
    if same_company and same_title and differences:
        return "material_variant", differences
    if same_company and same_title:
        return "related_not_duplicate", differences
    return "distinct", differences


def evaluate_relationships(observation: dict, evaluation: dict) -> dict:
    row_index = {
        int(row["job_id"]): row for row in observation["jobs"]
    }
    rows = []
    for case in evaluation["relationship_cases"]:
        left = row_index.get(int(case["left_job_id"]))
        right = row_index.get(int(case["right_job_id"]))
        if left is None or right is None:
            raise EvaluationHarnessError(
                f"relationship snapshot rows missing: {case['relationship_case_id']}"
            )
        validate_expected_row(left, case["left_expected"], case["relationship_case_id"])
        validate_expected_row(right, case["right_expected"], case["relationship_case_id"])
        actual, differences = classify_relationship(left, right)
        expected_differences = sorted(case["expected_distinguishing_fields"])
        rows.append(
            {
                "relationship_case_id": case["relationship_case_id"],
                "left_job_id": case["left_job_id"],
                "right_job_id": case["right_job_id"],
                "expected_relationship": case["expected_relationship"],
                "actual_relationship": actual,
                "expected_distinguishing_fields": expected_differences,
                "actual_distinguishing_fields": differences,
                "authority": case["authority"],
                "correct": (
                    actual == case["expected_relationship"]
                    and differences == expected_differences
                ),
            }
        )
    correct = sum(item["correct"] for item in rows)
    return {
        "identity_policy_version": IDENTITY_POLICY_VERSION,
        "cases": rows,
        "human_approved_cases": len(rows),
        "human_approved_correct": correct,
        "human_approved_accuracy": ratio(correct, len(rows)),
    }


def validate_expected_row(actual: dict, expected: dict, case_id: str) -> None:
    mismatched = [
        key
        for key, value in expected.items()
        if normalize_value(actual.get(key)) != normalize_value(value)
    ]
    if mismatched:
        raise EvaluationHarnessError(
            f"relationship snapshot drift for {case_id}: {', '.join(mismatched)}"
        )


def canonical_id_from_ref(opportunity_ref: str) -> int | None:
    prefix = "canonical:"
    if type(opportunity_ref) is not str or not opportunity_ref.startswith(prefix):
        return None
    return positive_int_or_none(opportunity_ref[len(prefix):])


def capture_inventory_snapshot(
    *,
    database: Path,
    golden: dict,
    evaluation: dict,
    captured_at: str,
) -> dict:
    if not valid_iso_datetime(captured_at):
        raise EvaluationHarnessError("captured-at must be a timezone-aware ISO datetime")
    authority = validate_evaluation_set(golden, evaluation)
    case_index = authority["case_index"]
    authoritative_cases = [
        case_index[case_id]
        for case_id in evaluation["authoritative_relevance_case_ids"]
    ]
    profiles = golden_tools.load_benchmark_profiles(golden)
    database = Path(database).resolve()
    before_state = database_state(database)
    connection = open_immutable_database(database)
    try:
        active_rows = preview.query_preview_rows(connection)
        shortlist = capture_legacy_ranking_observation(
            active_rows, authoritative_cases, profiles
        )
        enrichment = capture_enrichment_observation(connection, active_rows)
        relationship_ids = sorted(
            {
                int(value)
                for case in evaluation["relationship_cases"]
                for value in (case["left_job_id"], case["right_job_id"])
            }
        )
        relationships = {"jobs": load_relationship_rows(connection, relationship_ids)}
    finally:
        connection.close()
    after_state = database_state(database)
    if before_state != after_state:
        raise EvaluationHarnessError("database changed during read-only snapshot capture")
    observations = {
        "legacy_shortlist": shortlist,
        "enrichment": enrichment,
        "relationships": relationships,
    }
    snapshot = {
        "schema_version": INVENTORY_SNAPSHOT_SCHEMA_VERSION,
        "captured_at": captured_at,
        "source_database": {
            "filename": database.name,
            "sha256": sha256_file(database),
            "state_fingerprint": fingerprint(before_state),
            "immutable_query_only": True,
        },
        "observations": observations,
        "observation_fingerprint": fingerprint(observations),
    }
    snapshot["snapshot_fingerprint"] = fingerprint(snapshot)
    validate_inventory_snapshot(snapshot)
    return snapshot


def validate_inventory_snapshot(snapshot: dict) -> dict:
    required = {
        "schema_version",
        "captured_at",
        "source_database",
        "observations",
        "observation_fingerprint",
        "snapshot_fingerprint",
    }
    if type(snapshot) is not dict or set(snapshot) != required:
        raise EvaluationHarnessError("inventory snapshot shape is invalid")
    if snapshot["schema_version"] != INVENTORY_SNAPSHOT_SCHEMA_VERSION:
        raise EvaluationHarnessError("unsupported inventory snapshot version")
    if not valid_iso_datetime(snapshot["captured_at"]):
        raise EvaluationHarnessError("inventory snapshot capture time is invalid")
    source = snapshot["source_database"]
    if (
        type(source) is not dict
        or set(source)
        != {"filename", "sha256", "state_fingerprint", "immutable_query_only"}
        or type(source.get("filename")) is not str
        or not _SHA256_RE.fullmatch(str(source.get("sha256") or ""))
        or not _SHA256_RE.fullmatch(str(source.get("state_fingerprint") or ""))
        or source.get("immutable_query_only") is not True
    ):
        raise EvaluationHarnessError("inventory snapshot source metadata is invalid")
    observations = snapshot["observations"]
    if type(observations) is not dict or set(observations) != {
        "legacy_shortlist",
        "enrichment",
        "relationships",
    }:
        raise EvaluationHarnessError("inventory observations are invalid")
    if snapshot["observation_fingerprint"] != fingerprint(observations):
        raise EvaluationHarnessError("inventory observation fingerprint mismatch")
    snapshot_without_fingerprint = dict(snapshot)
    snapshot_without_fingerprint.pop("snapshot_fingerprint")
    if snapshot["snapshot_fingerprint"] != fingerprint(snapshot_without_fingerprint):
        raise EvaluationHarnessError("inventory snapshot fingerprint mismatch")
    validate_shortlist_observation(observations["legacy_shortlist"])
    validate_enrichment_observation(observations["enrichment"])
    validate_relationship_observation(observations["relationships"])
    return {
        "snapshot_fingerprint_verified": True,
        "observation_fingerprint_verified": True,
    }


def validate_shortlist_observation(observation: dict) -> None:
    required = {
        "benchmark_version",
        "active_inventory_rows",
        "evaluation_corpus_rows",
        "evaluation_corpus_opportunity_groups",
        "profile_rankings",
    }
    if type(observation) is not dict or set(observation) != required:
        raise EvaluationHarnessError("shortlist observation shape is invalid")
    if observation["benchmark_version"] != LEGACY_SHORTLIST_BENCHMARK_VERSION:
        raise EvaluationHarnessError("shortlist benchmark version is invalid")
    if any(
        type(observation[key]) is not int or observation[key] <= 0
        for key in (
            "active_inventory_rows",
            "evaluation_corpus_rows",
            "evaluation_corpus_opportunity_groups",
        )
    ):
        raise EvaluationHarnessError("shortlist observation counts are invalid")
    rankings = observation["profile_rankings"]
    if type(rankings) is not list:
        raise EvaluationHarnessError("profile rankings are invalid")
    profile_ids = [item.get("profile_id") for item in rankings]
    if profile_ids != sorted(profile_ids) or len(profile_ids) != len(set(profile_ids)):
        raise EvaluationHarnessError("profile rankings must be sorted and unique")
    for item in rankings:
        refs = item.get("ranked_candidate_refs")
        if (
            type(refs) is not list
            or any(type(ref) is not str or not ref for ref in refs)
            or len(refs) != len(set(refs))
        ):
            raise EvaluationHarnessError("candidate ranking is invalid")


def validate_enrichment_observation(observation: dict) -> None:
    if type(observation) is not dict or set(observation) != {"canonicals"}:
        raise EvaluationHarnessError("enrichment observation shape is invalid")
    canonicals = observation["canonicals"]
    if type(canonicals) is not list:
        raise EvaluationHarnessError("canonical observations are invalid")
    ids = [item.get("canonical_opportunity_id") for item in canonicals]
    if (
        any(type(item) is not int or item <= 0 for item in ids)
        or ids != sorted(ids)
        or len(ids) != len(set(ids))
    ):
        raise EvaluationHarnessError("canonical observations must be sorted and unique")
    expected_fields = {
        "canonical_opportunity_id",
        "current_semantic_input_version",
        "stored_semantic_input_version",
        "semantic_input_version_basis",
        "current_input_sha256",
        "comparable_input_sha256",
        "stored_input_sha256",
        "source_input_status",
        "current_derivation_fingerprint",
        "stored_derivation_fingerprint",
        "derivation_status",
        "changed_derivation_components",
        "stale_reasons",
        "freshness",
        "stored_status",
        "model_enriched",
        "source_fact_flags",
        "known_fields",
    }
    for item in canonicals:
        flags = item.get("source_fact_flags")
        known = item.get("known_fields")
        stale_reasons = item.get("stale_reasons")
        changed_components = item.get("changed_derivation_components")
        optional_hashes = (
            item.get("comparable_input_sha256"),
            item.get("stored_input_sha256"),
            item.get("stored_derivation_fingerprint"),
        )
        if (
            set(item) != expected_fields
            or not _SHA256_RE.fullmatch(str(item.get("current_input_sha256") or ""))
            or not _SHA256_RE.fullmatch(
                str(item.get("current_derivation_fingerprint") or "")
            )
            or any(
                value is not None and not _SHA256_RE.fullmatch(str(value))
                for value in optional_hashes
            )
            or item.get("freshness") not in {"current", "stale", "missing"}
            or item.get("source_input_status")
            not in {"current", "changed", "not_comparable", "missing"}
            or item.get("derivation_status")
            not in {"current", "legacy_compatible", "changed", "unknown", "missing"}
            or item.get("semantic_input_version_basis")
            not in {
                "explicit",
                "legacy_extractor_mapping",
                "invalid_explicit",
                "unknown",
                "missing",
            }
            or type(item.get("current_semantic_input_version")) is not str
            or (
                item.get("stored_semantic_input_version") is not None
                and type(item["stored_semantic_input_version"]) is not str
            )
            or type(stale_reasons) is not list
            or stale_reasons != sorted(set(stale_reasons))
            or not set(stale_reasons).issubset(
                {
                    STALE_REASON_DERIVATION_CONTRACT_CHANGED,
                    STALE_REASON_SOURCE_INPUT_CHANGED,
                    STALE_REASON_MISSING_ENRICHMENT,
                }
            )
            or type(changed_components) is not list
            or changed_components != sorted(set(changed_components))
            or type(item.get("model_enriched")) is not bool
            or type(flags) is not list
            or flags != sorted(flags)
            or len(flags) != len(set(flags))
            or not set(flags).issubset(SOURCE_FACT_FLAGS)
            or type(known) is not list
            or known != sorted(known)
            or len(known) != len(set(known))
            or not set(known).issubset(FIELD_DEFAULTS)
            or (item["freshness"] != "current" and known)
            or (item["freshness"] == "current" and stale_reasons)
            or (item["freshness"] == "stale" and not stale_reasons)
            or (
                item["freshness"] == "missing"
                and stale_reasons != [STALE_REASON_MISSING_ENRICHMENT]
            )
        ):
            raise EvaluationHarnessError("canonical enrichment observation is invalid")


def validate_relationship_observation(observation: dict) -> None:
    if type(observation) is not dict or set(observation) != {"jobs"}:
        raise EvaluationHarnessError("relationship observation shape is invalid")
    jobs = observation["jobs"]
    if type(jobs) is not list:
        raise EvaluationHarnessError("relationship rows are invalid")
    ids = [item.get("job_id") for item in jobs]
    if (
        any(type(item) is not int or item <= 0 for item in ids)
        or ids != sorted(ids)
        or len(ids) != len(set(ids))
        or any(set(item) != set(RELATIONSHIP_ROW_FIELDS) for item in jobs)
    ):
        raise EvaluationHarnessError("relationship rows must be closed, sorted, and unique")


def build_acceptance(
    evaluation: dict,
    authority: dict,
    eligibility: dict,
    shortlist: dict,
    relationships: dict,
    snapshot_validation: dict,
) -> dict:
    criteria = evaluation["acceptance_criteria"]
    hard = criteria["hard_safety_and_regression_requirements"]
    hard_checks = [
        threshold_check(
            "derived_eligibility_regressions",
            eligibility["derived_regression_accuracy"],
            hard["derived_eligibility_regression_accuracy_minimum"],
            eligibility["derived_regression_cases"],
        ),
        threshold_check(
            "human_approved_eligibility_interpretations",
            eligibility["human_approved_accuracy"],
            hard["human_approved_eligibility_accuracy_minimum"],
            eligibility["human_approved_cases"],
        ),
        threshold_check(
            "human_approved_relationship_judgments",
            relationships["human_approved_accuracy"],
            hard["human_approved_relationship_accuracy_minimum"],
            relationships["human_approved_cases"],
        ),
        threshold_check(
            "human_approved_unknown_preservation",
            eligibility["human_approved_unknown_preservation"],
            hard["human_approved_unknown_preservation_minimum"],
            eligibility["human_approved_unknown_cases"],
        ),
        boolean_check(
            "draft_labels_excluded",
            authority["draft_exclusion_verified"],
            hard["draft_labels_must_be_excluded_from_quality_metrics"],
        ),
        boolean_check(
            "approval_metadata_verified",
            authority["approval_metadata_verified"],
            hard["all_authoritative_judgments_must_have_approval_metadata"],
        ),
        boolean_check(
            "inventory_snapshot_fingerprint_verified",
            snapshot_validation["snapshot_fingerprint_verified"]
            and snapshot_validation["observation_fingerprint_verified"],
            hard["versioned_inventory_snapshot_fingerprint_required"],
        ),
    ]
    quality = criteria["sample_limited_quality_targets"]
    quality_targets = [
        threshold_check(
            "strong_recall_at_k",
            shortlist["strong_recall_at_k"],
            quality["strong_recall_minimum"],
            shortlist["strong_cases"],
        ),
        threshold_check(
            "strong_or_plausible_recall_at_k",
            shortlist["strong_or_plausible_recall_at_k"],
            quality["strong_or_plausible_recall_minimum"],
            shortlist["strong_or_plausible_cases"],
        ),
    ]
    return {
        "hard_requirements": hard_checks,
        "hard_requirements_passed": all(item["passed"] for item in hard_checks),
        "foundation_measurement_authority_ready": all(
            item["passed"] for item in hard_checks
        ),
        "sample_limited_quality_targets": quality_targets,
        "sample_limited_quality_targets_passed": all(
            item["passed"] for item in quality_targets
        ),
        "provisional_readiness": {
            **criteria["provisional_readiness_indicators"],
            "targets_passed": None,
        },
        "semantic_reranker_experiment_entry": "not_assessed",
    }


def threshold_check(name: str, actual: float, required: float, sample_size: int) -> dict:
    return {
        "criterion": name,
        "actual": actual,
        "required": required,
        "sample_size": sample_size,
        "passed": sample_size > 0 and actual >= required,
    }


def boolean_check(name: str, actual: bool, required: bool) -> dict:
    return {
        "criterion": name,
        "actual": actual,
        "required": required,
        "sample_size": None,
        "passed": type(actual) is bool and actual is required,
    }


def build_report_data(
    *,
    golden_path: Path,
    evaluation_path: Path,
    inventory_snapshot_path: Path,
    shortlist_size: int | None = None,
) -> dict:
    golden = load_json(golden_path)
    evaluation = load_json(evaluation_path)
    snapshot = load_json(inventory_snapshot_path)
    authority = validate_evaluation_set(golden, evaluation)
    snapshot_validation = validate_inventory_snapshot(snapshot)
    case_index = authority.pop("case_index")
    profiles = golden_tools.load_benchmark_profiles(golden)
    authoritative_cases = [
        case_index[case_id]
        for case_id in evaluation["authoritative_relevance_case_ids"]
    ]
    configured_size = evaluation["acceptance_criteria"]["initial_shortlist_size"]
    effective_size = configured_size if shortlist_size is None else shortlist_size
    eligibility = evaluate_eligibility(evaluation, case_index, profiles)
    shortlist = evaluate_shortlist_recall(
        snapshot["observations"]["legacy_shortlist"],
        authoritative_cases,
        effective_size,
    )
    shortlist_refs = {
        opportunity_ref
        for profile_result in shortlist["per_profile"]
        for opportunity_ref in profile_result["top_candidate_refs"]
    }
    enrichment = analyze_enrichment_readiness(
        snapshot["observations"]["enrichment"],
        shortlist_refs,
    )
    relationships = evaluate_relationships(
        snapshot["observations"]["relationships"],
        evaluation,
    )
    acceptance = build_acceptance(
        evaluation,
        authority,
        eligibility,
        shortlist,
        relationships,
        snapshot_validation,
    )
    input_files = {
        "golden_fixture_sha256": sha256_file(golden_path),
        "evaluation_fixture_sha256": sha256_file(evaluation_path),
        "inventory_snapshot_file_sha256": sha256_file(inventory_snapshot_path),
        "inventory_snapshot_fingerprint": snapshot["snapshot_fingerprint"],
        "inventory_observation_fingerprint": snapshot["observation_fingerprint"],
    }
    input_files["combined_input_fingerprint"] = fingerprint(input_files)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "inputs": input_files,
        "inventory_snapshot": {
            "schema_version": snapshot["schema_version"],
            "captured_at": snapshot["captured_at"],
            "source_database": snapshot["source_database"],
            **snapshot_validation,
        },
        "contracts": {
            "deterministic_eligibility": DETERMINISTIC_ELIGIBILITY_DECISION_SCHEMA_VERSION,
            "shortlist_candidate": SHORTLIST_CANDIDATE_SCHEMA_VERSION,
            "deterministic_relationship": OPPORTUNITY_RELATIONSHIP_SCHEMA_VERSION,
            "semantic_rerank_request": SEMANTIC_RERANK_REQUEST_SCHEMA_VERSION,
            "semantic_rerank_result": SEMANTIC_RERANK_RESULT_SCHEMA_VERSION,
            "match_run_snapshot": MATCH_RUN_SNAPSHOT_SCHEMA_VERSION,
            "match_run_candidate_disposition": MATCH_RUN_CANDIDATE_DISPOSITION_SCHEMA_VERSION,
            "match_run_result": MATCH_RUN_RESULT_SCHEMA_VERSION,
            "opportunity_semantic_input": SEMANTIC_INPUT_VERSION,
            "opportunity_enrichment_derivation": DERIVATION_RECIPE_VERSION,
            "opportunity_llm_acceptance_guards": LLM_ACCEPTANCE_GUARDS_VERSION,
            "score_or_bucket_fields": 0,
        },
        "authority": authority,
        "eligibility": eligibility,
        "shortlist": shortlist,
        "relationships": relationships,
        "enrichment": enrichment,
        "acceptance": acceptance,
    }


def verify_live_database(
    *,
    database: Path,
    golden: dict,
    evaluation: dict,
    expected_snapshot: dict,
) -> dict:
    live = capture_inventory_snapshot(
        database=database,
        golden=golden,
        evaluation=evaluation,
        captured_at=expected_snapshot["captured_at"],
    )
    matched = (
        live["observation_fingerprint"]
        == expected_snapshot["observation_fingerprint"]
    )
    if not matched:
        raise EvaluationHarnessError(
            "live database observations differ from the versioned inventory snapshot"
        )
    return {
        "matched": True,
        "observation_fingerprint": live["observation_fingerprint"],
        "database_sha256": live["source_database"]["sha256"],
    }


def render_markdown(report: dict) -> str:
    authority = report["authority"]
    eligibility = report["eligibility"]
    shortlist = report["shortlist"]
    relationships = report["relationships"]
    enrichment = report["enrichment"]
    acceptance = report["acceptance"]
    inputs = report["inputs"]
    lines = [
        "# Matching Contracts and Evaluation Foundation",
        "",
        "This report is deterministic and generated entirely from versioned inputs.",
        "",
        f"Combined input fingerprint: `{inputs['combined_input_fingerprint']}`",
        "",
        f"Inventory observation captured: `{report['inventory_snapshot']['captured_at']}`",
        "",
        f"Foundation measurement authority ready: **{'yes' if acceptance['foundation_measurement_authority_ready'] else 'no'}**.",
        "",
        "Semantic-reranker experiment entry: **not assessed**. Readiness thresholds remain provisional and unset.",
        "",
        "## Versioned contracts",
        "",
    ]
    for name, version in report["contracts"].items():
        lines.append(f"- {name}: `{version}`")
    lines.extend(
        [
        "",
        "## Evaluation authority",
        "",
        f"- Golden cases: {authority['golden_cases_total']}",
        f"- Approved relevance truth: {authority['approved_relevance_cases']}",
        f"- Draft relevance labels excluded: {authority['draft_relevance_cases_excluded']}",
        f"- Derived eligibility regressions: {authority['derived_eligibility_regressions']}",
        f"- Human-approved eligibility interpretations: {authority['human_approved_eligibility_interpretations']}",
        f"- Human-approved relationship judgments: {authority['human_approved_relationship_judgments']}",
        f"- Total explicitly approved judgments: {authority['approved_judgments_total']}",
        "",
        "## Deterministic eligibility regression coverage",
        "",
        f"- Derived relevance-rule agreement: {percent(eligibility['derived_regression_accuracy'])} ({eligibility['derived_regression_correct']}/{eligibility['derived_regression_cases']})",
        f"- Human-approved interpretation agreement: {percent(eligibility['human_approved_accuracy'])} ({eligibility['human_approved_correct']}/{eligibility['human_approved_cases']})",
        f"- Approved unknown preservation: {percent(eligibility['human_approved_unknown_preservation'])} ({eligibility['human_approved_unknown_preserved']}/{eligibility['human_approved_unknown_cases']})",
        "",
        "The six derived language regressions are not a general human-reviewed eligibility benchmark.",
        "",
        "## Legacy shortlist comparison",
        "",
        shortlist["measurement_note"],
        "",
        shortlist["identity_note"],
        "",
        f"- K: {shortlist['shortlist_size']}",
        f"- Strong recall@K: {percent(shortlist['strong_recall_at_k'])} ({shortlist['strong_recalled']}/{shortlist['strong_cases']})",
        f"- Strong + plausible recall@K: {percent(shortlist['strong_or_plausible_recall_at_k'])} ({shortlist['strong_or_plausible_recalled']}/{shortlist['strong_or_plausible_cases']})",
        "",
        "These are regression samples (n=10 and n=17), not population estimates.",
        "",
        "## Deterministic duplicate and variant behavior",
        "",
        f"Human-approved agreement: {percent(relationships['human_approved_accuracy'])} ({relationships['human_approved_correct']}/{relationships['human_approved_cases']}).",
        "",
        "| Case | Expected | Actual | Distinguishing fields |",
        "|---|---|---|---|",
        ]
    )
    for item in relationships["cases"]:
        fields = ", ".join(item["actual_distinguishing_fields"]) or "none"
        lines.append(
            f"| `{item['relationship_case_id']}` | {item['expected_relationship']} | "
            f"{item['actual_relationship']} | {fields} |"
        )
    freshness = enrichment["freshness_counts"]
    freshness_reasons = enrichment["freshness_reason_counts"]
    lines.extend(
        [
            "",
            "## Enrichment freshness and readiness",
            "",
            f"- Active canonicals: {enrichment['active_canonical_opportunities']}",
            f"- Current enrichments: {freshness['current']} ({percent(enrichment['current_enrichment_coverage'])})",
            f"- Stale enrichments: {freshness['stale']}",
            f"- Missing enrichments: {freshness['missing']}",
            f"- Contract/recipe stale: {freshness_reasons['contract_recipe_stale']}",
            f"- Source changed: {freshness_reasons['source_changed']}",
            f"- Current model-enriched documents: {enrichment['model_enriched_current']}",
            f"- Source identity coverage: {percent(enrichment['source_identity_coverage'])} ({enrichment['source_identity_ready_canonicals']}/{enrichment['active_canonical_opportunities']})",
            f"- Rich-source coverage: {percent(enrichment['rich_source_coverage'])} ({enrichment['rich_source_canonicals']}/{enrichment['active_canonical_opportunities']})",
            f"- Minimum semantic packet: {percent(enrichment['minimum_semantic_packet_coverage'])} ({enrichment['minimum_semantic_packet_ready_canonicals']}/{enrichment['active_canonical_opportunities']})",
            f"- Structured requirement signal: {percent(enrichment['structured_requirement_signal_coverage'])} ({enrichment['structured_requirement_signal_canonicals']}/{enrichment['active_canonical_opportunities']})",
            f"- Legacy top-K unique opportunity refs: {enrichment['baseline_shortlist_opportunities']}",
            f"- Legacy top-K refs with current enrichments: {enrichment['baseline_shortlist_current_enrichments']}",
            f"- Legacy top-K refs absent from the active canonical snapshot: {enrichment['baseline_shortlist_not_in_active_snapshot']}",
            "",
            "Freshness compares source input under the row's stored or safely inferred semantic-input contract, independently from derivation compatibility. Reason counts can overlap when both changed.",
            "Only current enrichments contribute structured-field coverage. Stale and missing documents are reported as unavailable.",
            "The minimum packet requires source identity, current structured role/activity, and current responsibility or candidate-profile evidence. The requirement indicator means at least one current structured requirement signal; it is not a claim that all requirements are resolved.",
            "",
            "| Structured field | Current known | Current unknown | Stale unavailable | Missing unavailable | All-active coverage |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for item in enrichment["field_readiness"]:
        lines.append(
            f"| `{item['field_path']}` | {item['known_current']} | "
            f"{item['unknown_current']} | {item['unavailable_stale']} | "
            f"{item['unavailable_missing']} | {percent(item['coverage_all_active'])} |"
        )
    lines.extend(
        [
            "",
            "## Hard safety and regression requirements",
            "",
            "| Requirement | Sample | Actual | Required | Status |",
            "|---|---:|---:|---:|---|",
        ]
    )
    for item in acceptance["hard_requirements"]:
        sample = item["sample_size"] if item["sample_size"] is not None else "n/a"
        lines.append(
            f"| `{item['criterion']}` | {sample} | {format_metric(item['actual'])} | "
            f"{format_metric(item['required'])} | {'pass' if item['passed'] else 'fail'} |"
        )
    lines.extend(
        [
            "",
            "## Sample-limited quality targets",
            "",
            "| Target | Sample | Actual | Target | Status |",
            "|---|---:|---:|---:|---|",
        ]
    )
    for item in acceptance["sample_limited_quality_targets"]:
        lines.append(
            f"| `{item['criterion']}` | {item['sample_size']} | "
            f"{format_metric(item['actual'])} | {format_metric(item['required'])} | "
            f"{'pass' if item['passed'] else 'fail'} |"
        )
    lines.extend(
        [
            "",
            "## Provisional readiness",
            "",
            "Readiness indicators are reported, but no experiment or production threshold is asserted in this milestone.",
            "",
            f"Threshold status: `{acceptance['provisional_readiness']['threshold_status']}`",
            "",
            "## Reproduction",
            "",
            "`python scripts/matching_foundation_report.py --shortlist-size 32`",
            "",
            f"Inventory snapshot fingerprint: `{inputs['inventory_snapshot_fingerprint']}`",
            "",
            f"Inventory observation fingerprint: `{inputs['inventory_observation_fingerprint']}`",
            "",
        ]
    )
    return "\n".join(lines)


def normalize_value(value) -> str:
    if value is None:
        return ""
    return " ".join(str(value).casefold().split())


def ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def format_metric(value) -> str:
    if type(value) is bool:
        return str(value).lower()
    if type(value) in {int, float}:
        return percent(float(value))
    return str(value)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        golden = load_json(args.golden)
        evaluation = load_json(args.evaluation)
        if args.capture_snapshot is not None:
            if args.capture_database is None or args.captured_at is None:
                raise EvaluationHarnessError(
                    "--capture-snapshot requires --capture-database and --captured-at"
                )
            snapshot = capture_inventory_snapshot(
                database=args.capture_database,
                golden=golden,
                evaluation=evaluation,
                captured_at=args.captured_at,
            )
            write_json(args.capture_snapshot, snapshot)
            print(f"Wrote {Path(args.capture_snapshot).resolve()}")
        snapshot_path = (
            args.capture_snapshot
            if args.capture_snapshot is not None
            else args.inventory_snapshot
        )
        snapshot = load_json(snapshot_path)
        validate_inventory_snapshot(snapshot)
        if args.verify_database is not None:
            verified = verify_live_database(
                database=args.verify_database,
                golden=golden,
                evaluation=evaluation,
                expected_snapshot=snapshot,
            )
            print(
                "Verified live observations: "
                + verified["observation_fingerprint"]
            )
        report = build_report_data(
            golden_path=args.golden,
            evaluation_path=args.evaluation,
            inventory_snapshot_path=snapshot_path,
            shortlist_size=args.shortlist_size,
        )
    except (EvaluationHarnessError, sqlite3.Error, ValueError, TypeError) as exc:
        raise SystemExit(f"Matching foundation report failed: {exc}") from exc
    markdown = render_markdown(report)
    if args.output == "-":
        print(markdown)
    else:
        output_path = Path(args.output).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(markdown, encoding="utf-8")
        print(f"Wrote {output_path}")
    if args.json_output is not None:
        write_json(Path(args.json_output).resolve(), report)
        print(f"Wrote {Path(args.json_output).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

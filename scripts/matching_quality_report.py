#!/usr/bin/env python3
"""Evaluate the current profile matcher against a draft golden set.

This is intentionally read-only. It imports the production matching helpers and
reports what the matcher does today without changing scores, thresholds, plans,
database rows, or crawler behavior.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import types
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import profile_match_digest as matcher  # noqa: E402
from product_demo_report import match_strength_from_score  # noqa: E402
from wahojobs.classification import (  # noqa: E402
    INVENTORY_MODEL_EVERGREEN_APPLICATION,
    INVENTORY_MODEL_LIVE_FEED,
    INVENTORY_MODEL_MIXED,
    INVENTORY_MODEL_PUBLIC_INVENTORY,
    MARKET_COUNT_POLICY_COUNT_LIVE,
    MARKET_COUNT_POLICY_REPORT_SEPARATELY,
    SOURCE_TIER_CORE,
)
from wahojobs.db.connection import get_connection  # noqa: E402
from wahojobs.matching.languages import (  # noqa: E402
    CANONICAL_LANGUAGES,
    detect_explicit_languages,
    language_eligibility,
    row_language_text,
)

FIXTURE_PATH = ROOT / "tests" / "fixtures" / "matching_golden_set.json"
REPORT_PATH = ROOT / "exports" / "matching_quality_report.md"
REVIEW_PATH = ROOT / "exports" / "matching_golden_set_review.md"
SAMPLE_PROFILES_PATH = ROOT / "profiles" / "sample_profiles.json"

SECTION_LEVELS = {
    "exclude": 0,
    "explore_only": 1,
    "also_worth_reviewing": 2,
    "best_matches": 3,
    "do_these_first": 4,
}

VISIBLE_SCORE_THRESHOLD = 18
BEST_MATCH_SCORE_THRESHOLD = 22
DO_THESE_FIRST_SCORE_THRESHOLD = 30

RELEVANT_LABELS = {"strong", "plausible"}
STRICT_RELEVANT_LABELS = {"strong"}

SUPPORTED_FIXTURE_LABELS = {"strong", "plausible", "weak", "false_positive"}
SUPPORTED_LABEL_SOURCES = {"codex_draft", "human_reviewed"}
SUPPORTED_SECTIONS = set(SECTION_LEVELS)
PERSONALIZED_SECTIONS = {"do_these_first", "best_matches", "also_worth_reviewing"}
MEDIUM_SCORE_THRESHOLD = 24

LANGUAGE_WORDS = {
    "arabic",
    "catalan",
    "chinese",
    "czech",
    "danish",
    "dutch",
    "english",
    "french",
    "german",
    "hebrew",
    "hindi",
    "japanese",
    "korean",
    "kiswahili",
    "norwegian",
    "polish",
    "portuguese",
    "romanian",
    "spanish",
    "swedish",
    "turkish",
    "vietnamese",
}

PROFILE_LANGUAGE_ALLOWLIST = {
    "portuguese_english_reviewer": {"portuguese", "english"},
    "beginner_bilingual_no_degree": {"english", "spanish"},
    "english_teacher_remote": {"english"},
    "multilingual_translator": {"english", "spanish", "portuguese", "french"},
    "phd_history_researcher": {"english", "french"},
}

TECHNICAL_TERMS = {
    "api",
    "backend",
    "c#",
    "code",
    "coding",
    "developer",
    "fastapi",
    "frontend",
    "full-stack",
    "java",
    "javascript",
    "llm evaluation",
    "python",
    "react",
    "software",
    "swe-bench",
    "typescript",
}

LEGAL_TERMS = {
    "attorney",
    "contract law",
    "corporate law",
    "employment law",
    "law",
    "legal",
    "litigation",
    "m&a",
    "regulatory",
}

FINANCE_TERMS = {
    "accountant",
    "accounting",
    "banking",
    "equity",
    "finance",
    "financial",
    "investment",
    "tax",
}

SCIENCE_TERMS = {
    "biology",
    "biomedical",
    "biophysics",
    "chemistry",
    "clinical",
    "dermatologist",
    "medicine",
    "medical",
    "pharma",
    "physics",
    "science",
}


@dataclass
class EvaluatedCase:
    case: dict
    row: dict
    source_status: str
    score: int
    match_label: str
    current_section: str
    evaluation_label: str
    evaluation_section: str
    raw_match_label: str
    raw_section: str
    hard_gate_type: str
    hard_gate_status: str
    hard_gate_reason: str
    location_eligibility_status: str
    location_eligibility_reason: str
    profile_location: str
    profile_location_status: str
    applicant_location_requirements: str
    location_restriction_type: str
    job_location_scope: str
    job_remote_status: str
    location_actionability_cap_required: bool
    location_actionability_cap_applied: bool
    eligible_for_personalized: bool
    language_eligibility_reason: str
    reasons: list[str]
    positives: list[str]
    penalties: list[str]
    signals: list[str]
    contradictions: list[str]
    failure_patterns: list[str]


@dataclass(frozen=True)
class BenchmarkPrediction:
    evaluation_label: str
    evaluation_section: str
    raw_match_label: str
    raw_section: str
    raw_score: int
    hard_gate_type: str = ""
    hard_gate_status: str = ""
    hard_gate_reason: str = ""


def main() -> None:
    generated_at = datetime.now(timezone.utc)
    fixture = load_fixture()
    profiles = load_benchmark_profiles(fixture)

    with get_connection() as conn:
        db_rows = [row_to_dict(row) for row in matcher.get_active_rows(conn)]

    baseline_hash = get_baseline_commit_hash()
    baseline_matcher = load_baseline_matcher()
    baseline_evaluated = [
        evaluate_case(case, profiles[case["profile_id"]], db_rows, baseline_matcher)
        for case in fixture["cases"]
    ]
    evaluated = [
        evaluate_case(case, profiles[case["profile_id"]], db_rows, matcher)
        for case in fixture["cases"]
    ]
    live_snapshot = build_live_snapshot(profiles, db_rows)
    baseline_metrics = calculate_metrics(baseline_evaluated)
    metrics = calculate_metrics(evaluated)
    language_diagnostics = build_language_diagnostics(profiles, db_rows)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        render_quality_report(
            generated_at,
            fixture,
            evaluated,
            metrics,
            live_snapshot,
            baseline_hash,
            baseline_metrics,
            language_diagnostics,
        ),
        encoding="utf-8",
    )
    REVIEW_PATH.write_text(
        render_review_report(generated_at, fixture, evaluated),
        encoding="utf-8",
    )

    print_terminal_summary(fixture, metrics, baseline_hash, baseline_metrics)


def get_baseline_commit_hash() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            "Could not determine baseline commit with 'git rev-parse HEAD': "
            + (result.stderr or result.stdout).strip()
        )
    return result.stdout.strip()


def load_baseline_matcher():
    result = subprocess.run(
        ["git", "show", "HEAD:scripts/profile_match_digest.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            "Could not load previous matcher from HEAD:scripts/profile_match_digest.py: "
            + (result.stderr or result.stdout).strip()
        )

    module = types.ModuleType("profile_match_digest_baseline")
    module.__file__ = str(ROOT / "scripts" / "profile_match_digest.py")
    try:
        exec(
            compile(result.stdout, "HEAD:scripts/profile_match_digest.py", "exec"),
            module.__dict__,
        )
    except Exception as exc:
        raise SystemExit(f"Could not import previous matcher from HEAD: {exc}") from exc

    if not hasattr(module, "score_opportunity"):
        raise SystemExit("Previous matcher from HEAD does not expose score_opportunity.")
    return module


def load_fixture() -> dict:
    try:
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"Fixture not found: {FIXTURE_PATH}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Fixture is not valid JSON: {FIXTURE_PATH} ({exc})")

    if not isinstance(fixture, dict) or not isinstance(fixture.get("cases"), list):
        raise SystemExit("Fixture must be an object with a 'cases' list.")
    if not fixture["cases"]:
        raise SystemExit("Fixture case list is empty.")

    seen_case_ids = set()
    for index, case in enumerate(fixture["cases"], start=1):
        validate_case(case, index)
        if case["case_id"] in seen_case_ids:
            raise SystemExit(f"Duplicate case_id in fixture: {case['case_id']}")
        seen_case_ids.add(case["case_id"])
    return fixture


def validate_case(case: dict, index: int) -> None:
    required = [
        "case_id",
        "profile_id",
        "source",
        "title",
        "location",
        "department",
        "expertise",
        "commitment",
        "opportunity_kind",
        "expected_label",
        "label_source",
        "rationale",
        "review_required",
    ]
    missing = [field for field in required if field not in case]
    if missing:
        raise SystemExit(f"Fixture case #{index} is missing fields: {', '.join(missing)}")
    if case["expected_label"] not in SUPPORTED_FIXTURE_LABELS:
        raise SystemExit(
            f"Unsupported expected_label for {case['case_id']}: {case['expected_label']}"
        )
    if case["label_source"] not in SUPPORTED_LABEL_SOURCES:
        raise SystemExit(
            f"{case['case_id']} has unsupported label_source: {case['label_source']}. "
            f"Expected one of: {', '.join(sorted(SUPPORTED_LABEL_SOURCES))}."
        )
    if not isinstance(case["review_required"], bool):
        raise SystemExit(f"{case['case_id']} review_required must be true or false.")
    if case["label_source"] == "human_reviewed":
        if case["review_required"]:
            raise SystemExit(f"{case['case_id']} is human_reviewed but still review_required=true.")
        if not str(case.get("human_notes") or "").strip():
            raise SystemExit(f"{case['case_id']} is human_reviewed but missing human_notes.")
    if case.get("expected_section") and case["expected_section"] not in SUPPORTED_SECTIONS:
        raise SystemExit(
            f"Unsupported expected_section for {case['case_id']}: {case['expected_section']}"
        )


def load_benchmark_profiles(fixture: dict) -> dict[str, dict]:
    built_in_profiles, _ = matcher.load_profiles(None)
    profiles = {profile["profile_id"]: profile for profile in built_in_profiles}
    if SAMPLE_PROFILES_PATH.exists():
        sample_profiles, _ = matcher.load_profiles(SAMPLE_PROFILES_PATH)
        profiles.update({profile["profile_id"]: profile for profile in sample_profiles})

    required_profile_ids = sorted({case["profile_id"] for case in fixture["cases"]})
    missing = [profile_id for profile_id in required_profile_ids if profile_id not in profiles]
    if missing:
        available = ", ".join(sorted(profiles))
        raise SystemExit(
            "Fixture references unknown profile_id values: "
            f"{', '.join(missing)}. Available: {available}"
        )
    return {profile_id: profiles[profile_id] for profile_id in required_profile_ids}


def evaluate_case(case: dict, profile: dict, db_rows: list[dict], matcher_module) -> EvaluatedCase:
    row, source_status = resolve_case_row(case, db_rows)
    scored = matcher_module.score_opportunity(profile, row)
    score = scored["score"]
    raw_match_label = match_strength_from_score(score)
    eligible_for_personalized = scored.get("eligible_for_personalized", True)
    raw_section = scored.get("raw_product_section") or section_for_score(score, eligible_for_personalized)
    effective_section = scored.get("effective_product_section") or raw_section
    projection = project_benchmark_prediction(score, raw_match_label, raw_section, scored, effective_section)
    positives, penalties = explain_score(profile, row, matcher_module)
    signals, contradictions = detect_signals(profile, row, case)
    failure_patterns = detect_failure_patterns(
        case,
        row,
        positives,
        contradictions,
        score,
        projection.evaluation_section,
    )

    return EvaluatedCase(
        case=case,
        row=row,
        source_status=source_status,
        score=score,
        match_label=projection.raw_match_label,
        current_section=projection.raw_section,
        evaluation_label=projection.evaluation_label,
        evaluation_section=projection.evaluation_section,
        raw_match_label=projection.raw_match_label,
        raw_section=projection.raw_section,
        hard_gate_type=projection.hard_gate_type,
        hard_gate_status=projection.hard_gate_status,
        hard_gate_reason=projection.hard_gate_reason,
        location_eligibility_status=scored.get("location_eligibility_status", "unknown"),
        location_eligibility_reason=scored.get("location_eligibility_reason", "-"),
        profile_location=scored.get("profile_location", ""),
        profile_location_status=scored.get("profile_location_status", "unknown"),
        applicant_location_requirements=scored.get("applicant_location_requirements", ""),
        location_restriction_type=scored.get("location_restriction_type", "none"),
        job_location_scope=scored.get("job_location_scope", "unknown"),
        job_remote_status=scored.get("job_remote_status", "unknown"),
        location_actionability_cap_required=bool(scored.get("location_actionability_cap_required")),
        location_actionability_cap_applied=bool(scored.get("location_actionability_cap_applied")),
        eligible_for_personalized=eligible_for_personalized,
        language_eligibility_reason=scored.get("language_eligibility_reason", "-"),
        reasons=scored["reasons"],
        positives=positives,
        penalties=penalties,
        signals=signals,
        contradictions=contradictions,
        failure_patterns=failure_patterns,
    )


def project_benchmark_prediction(
    score: int,
    raw_match_label: str,
    raw_section: str,
    scored: dict,
    effective_section: str | None = None,
) -> BenchmarkPrediction:
    """Project product matcher output into a benchmark-only label/section.

    Product ranking uses raw score and section. The benchmark projection is allowed
    to translate decisive structured hard-gate failures into false_positive/exclude
    so golden-set evaluation can represent hard rejections without changing product
    behavior.
    """
    hard_gate = decisive_hard_gate_failure(scored)
    if hard_gate:
        return BenchmarkPrediction(
            evaluation_label="false_positive",
            evaluation_section="exclude",
            raw_match_label=raw_match_label,
            raw_section=raw_section,
            raw_score=score,
            hard_gate_type=hard_gate["type"],
            hard_gate_status=hard_gate["status"],
            hard_gate_reason=hard_gate["reason"],
        )
    return BenchmarkPrediction(
        evaluation_label=label_from_raw_match_label(raw_match_label),
        evaluation_section=effective_section or raw_section,
        raw_match_label=raw_match_label,
        raw_section=raw_section,
        raw_score=score,
    )


def decisive_hard_gate_failure(scored: dict) -> dict | None:
    for gate in scored.get("hard_gates") or scored.get("eligibility_gates") or []:
        projected = normalize_structured_hard_gate(gate)
        if projected:
            return projected

    if scored.get("eligible_for_personalized") is False:
        mode = scored.get("language_requirement_mode")
        detected = set(scored.get("detected_languages") or [])
        # Ambiguous language requirements and unknown/uncertain eligibility are not
        # projected to false_positive/exclude.
        if detected and mode in {"single", "all_required", "any_supported"}:
            return {
                "type": "language",
                "status": "failed",
                "reason": scored.get("language_eligibility_reason")
                or "Explicit language eligibility gate failed.",
            }
    return None


def normalize_structured_hard_gate(gate: dict) -> dict | None:
    if not isinstance(gate, dict):
        return None
    status = str(gate.get("status") or "").strip().lower()
    failed = status in {"failed", "fail", "ineligible", "blocked", "rejected"} or gate.get("eligible") is False
    decisive = bool(gate.get("decisive") or gate.get("hard") or gate.get("is_hard_gate"))
    uncertain = status in {"unknown", "uncertain", "ambiguous", "not_applicable"}
    if not failed or not decisive or uncertain:
        return None
    return {
        "type": str(gate.get("type") or gate.get("name") or "hard_gate"),
        "status": status or "failed",
        "reason": str(gate.get("reason") or "Decisive hard gate failed."),
    }


def label_from_raw_match_label(raw_match_label: str) -> str:
    return {
        "Strong": "strong",
        "Medium": "plausible",
        "Possible": "weak",
    }.get(str(raw_match_label), str(raw_match_label).lower())


def resolve_case_row(case: dict, db_rows: list[dict]) -> tuple[dict, str]:
    source_slug = normalize_slug(case.get("source_slug") or case["source"])
    title_key = normalize_title_key(case["title"])
    url = normalize_url(case.get("url"))
    canonical_id = case.get("canonical_opportunity_id")

    candidates = [
        row
        for row in db_rows
        if normalize_slug(row.get("source_slug") or row.get("source")) == source_slug
        and normalize_title_key(row.get("title") or row.get("canonical_title")) == title_key
    ]

    if url:
        url_matches = [row for row in candidates if normalize_url(row.get("url")) == url]
        if url_matches:
            return url_matches[0], "live_db_url"

    if canonical_id:
        canonical_matches = [
            row for row in candidates if str(row.get("canonical_opportunity_id") or "") == str(canonical_id)
        ]
        if canonical_matches:
            return canonical_matches[0], "live_db_canonical"

    if candidates:
        return candidates[0], "live_db_title"

    return build_snapshot_row(case), "fixture_snapshot"


def build_snapshot_row(case: dict) -> dict:
    source_slug = normalize_slug(case.get("source_slug") or case["source"])
    market_count_policy = case.get("market_count_policy") or MARKET_COUNT_POLICY_COUNT_LIVE
    inventory_model = case.get("inventory_model") or INVENTORY_MODEL_LIVE_FEED
    include_in_live_estimate = case.get("include_in_live_market_estimate")
    if include_in_live_estimate is None:
        include_in_live_estimate = market_count_policy == MARKET_COUNT_POLICY_COUNT_LIVE

    return {
        "job_id": case.get("job_id") or f"fixture:{case['case_id']}",
        "title": case["title"],
        "location": case.get("location") or "Unknown",
        "url": case.get("url") or "",
        "department": case.get("department") or "Unknown",
        "expertise": case.get("expertise") or case.get("department") or "Unknown",
        "commitment": case.get("commitment") or "",
        "opportunity_kind": case.get("opportunity_kind") or "live_posting",
        "availability_basis": case.get("availability_basis") or "api_feed",
        "include_in_live_market_estimate": int(bool(include_in_live_estimate)),
        "canonical_opportunity_id": case.get("canonical_opportunity_id"),
        "source": case["source"],
        "source_slug": source_slug,
        "source_tier": case.get("source_tier") or SOURCE_TIER_CORE,
        "inventory_model": inventory_model,
        "market_count_policy": market_count_policy,
        "canonical_title": case.get("canonical_title"),
        "source_category": case.get("source_category") or case.get("expertise") or case.get("department"),
    }


def row_to_dict(row) -> dict:
    return {key: row[key] for key in row.keys()}


def section_for_score(score: int, eligible_for_personalized: bool = True) -> str:
    if not eligible_for_personalized:
        return "explore_only"
    if score >= DO_THESE_FIRST_SCORE_THRESHOLD:
        return "do_these_first"
    if score >= BEST_MATCH_SCORE_THRESHOLD:
        return "best_matches"
    if score >= VISIBLE_SCORE_THRESHOLD:
        return "also_worth_reviewing"
    return "explore_only"


def expected_section(case: dict) -> str:
    if case.get("expected_section"):
        return case["expected_section"]
    if case["expected_label"] == "strong":
        return "best_matches"
    if case["expected_label"] == "plausible":
        return "also_worth_reviewing"
    if case["expected_label"] == "weak":
        return "explore_only"
    return "exclude"


def explain_score(profile: dict, row: dict, matcher_module=matcher) -> tuple[list[str], list[str]]:
    title = row["title"] or row["canonical_title"] or "Untitled opportunity"
    expertise = row["source_category"] or row["expertise"] or row["department"] or "Unknown"
    text = matcher_module.searchable_text(row, title, expertise)
    positives = []
    penalties = []

    for reason, keywords, points in profile["signals"]:
        hits = [
            keyword
            for keyword in matcher_module.normalize_keywords(keywords)
            if matcher_module.keyword_matches(text, keyword)
        ]
        if hits:
            positives.append(f"+{points} {reason}: {', '.join(hits[:4])}")

    for language in profile["languages"]:
        hits = [
            keyword
            for keyword in matcher_module.language_variants(language)
            if matcher_module.keyword_matches(text, keyword)
        ]
        if hits:
            positives.append(f"+6 {language} language signal: {', '.join(hits[:3])}")

    if matcher_module.wants_remote(profile) and matcher_module.has_remote_signal(row):
        positives.append("+5 Remote/flexible signal")

    if (
        row["market_count_policy"] == MARKET_COUNT_POLICY_COUNT_LIVE
        and row["include_in_live_market_estimate"]
    ):
        positives.append("+3 Live/countable opportunity")
    else:
        positives.append("+2 Reported separately opportunity")

    if row["source_tier"] != matcher_module.SOURCE_TIER_EXPERIMENTAL:
        positives.append("+1 Non-experimental source")

    for keyword in matcher_module.normalize_keywords(profile.get("avoid_keywords", [])):
        if matcher_module.keyword_matches(text, keyword):
            penalties.append(f"-12 Possible requirement mismatch: {keyword}")

    if hasattr(matcher_module, "match_quality_gate_penalties"):
        for reason, penalty in matcher_module.match_quality_gate_penalties(
            profile,
            row,
            matcher_module.quality_gate_text(row, title, expertise),
        ):
            penalties.append(f"-{penalty} {reason}")

    return positives, penalties


def detect_signals(profile: dict, row: dict, case: dict) -> tuple[list[str], list[str]]:
    text = row_text(row, case)
    language_text = row_language_text(row)
    signals = []
    contradictions = []

    language_hits = sorted(detect_explicit_languages(language_text))
    if language_hits:
        signals.append("languages: " + ", ".join(language_hits))
        eligibility = language_eligibility(profile, language_text)
        if not eligibility.eligible_for_personalized:
            contradictions.append(
                "unsupported explicit language: "
                + ", ".join(sorted(eligibility.unsupported_languages or eligibility.detected_languages))
            )

    domain_groups = [
        ("technical", TECHNICAL_TERMS),
        ("legal", LEGAL_TERMS),
        ("finance", FINANCE_TERMS),
        ("science/medical", SCIENCE_TERMS),
    ]
    detected_domains = [
        name
        for name, terms in domain_groups
        if any(word_or_phrase_in_text(text, term) for term in terms)
    ]
    if detected_domains:
        signals.append("domains: " + ", ".join(detected_domains))

    profile_id = profile["profile_id"]
    if profile_id == "phd_history_researcher" and any(
        domain in detected_domains for domain in ("technical", "science/medical", "finance")
    ):
        contradictions.append("domain mismatch for humanities research profile")
    if profile_id == "software_engineer" and detected_domains and "technical" not in detected_domains:
        contradictions.append("non-technical professional domain")
    if profile_id == "lawyer" and detected_domains and "legal" not in detected_domains:
        contradictions.append("non-legal professional domain")
    if profile_id == "finance_professional" and detected_domains and "finance" not in detected_domains:
        contradictions.append("non-finance professional domain")
    if profile_id == "biology_or_medicine_academic" and detected_domains and "science/medical" not in detected_domains:
        contradictions.append("non-science/medical professional domain")
    if profile_id in {"generalist_no_degree", "beginner_bilingual_no_degree"} and any(
        domain in detected_domains for domain in ("technical", "legal", "finance", "science/medical")
    ):
        contradictions.append("specialist credential/domain mismatch")

    if "search" in text and "research" in text:
        signals.append("contains both search and research")

    return unique(signals), unique(contradictions)


def detect_failure_patterns(
    case: dict,
    row: dict,
    positives: list[str],
    contradictions: list[str],
    score: int,
    current_section: str,
) -> list[str]:
    patterns = []
    text = row_text(row, case)
    positive_text = " ".join(positives).lower()

    if case["expected_label"] == "false_positive" and current_section in PERSONALIZED_SECTIONS:
        patterns.append("visible_false_positive")
    if expected_section(case) == "exclude" and current_section in PERSONALIZED_SECTIONS:
        patterns.append("exclude_case_visible")
    if "research" in text and "search" in positive_text:
        patterns.append("search_inside_research")
    if "evaluation" in positive_text and not any(
        word_or_phrase_in_text(text, term)
        for term in ("ai evaluation", "rater", "reviewer", "quality analyst", "annotation")
    ):
        patterns.append("generic_evaluation_evidence")
    if any("unsupported explicit language" in item for item in contradictions):
        patterns.append("unsupported_language")
    if any("technical" in item for item in contradictions):
        patterns.append("technical_mismatch")
    if any("professional domain" in item or "domain mismatch" in item for item in contradictions):
        patterns.append("professional_domain_mismatch")
    if any("credential" in item for item in contradictions):
        patterns.append("credential_or_specialty_mismatch")
    if case.get("regression_rule"):
        current_level = SECTION_LEVELS[current_section]
        expected_level = SECTION_LEVELS[expected_section(case)]
        if current_level > expected_level:
            patterns.append(case["regression_rule"])

    return unique(patterns)


def row_text(row: dict, case: dict | None = None) -> str:
    values = [
        row.get("title"),
        row.get("canonical_title"),
        row.get("source_category"),
        row.get("expertise"),
        row.get("department"),
        row.get("commitment"),
        row.get("location"),
        row.get("opportunity_kind"),
        row.get("availability_basis"),
    ]
    if case:
        values.extend(
            [
                case.get("title"),
                case.get("rationale"),
                case.get("department"),
                case.get("expertise"),
            ]
        )
    return matcher.normalize_text(" ".join(str(value or "") for value in values))


def word_or_phrase_in_text(text: str, term: str) -> bool:
    term = matcher.normalize_text(term)
    if re.fullmatch(r"[\w#+.-]+", term):
        return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text) is not None
    return term in text


def calculate_metrics(evaluated: list[EvaluatedCase]) -> dict:
    headline = [item for item in evaluated if not item.case["review_required"]]
    by_profile = defaultdict(list)
    for item in headline:
        by_profile[item.case["profile_id"]].append(item)

    metrics_by_profile = {}
    for profile_id, items in sorted(by_profile.items()):
        ranked = sorted(
            items,
            key=lambda item: (-item.score, item.case["source"], item.case["title"], item.case["case_id"]),
        )
        metrics_by_profile[profile_id] = {
            "case_count": len(items),
            "label_distribution": Counter(item.case["expected_label"] for item in items),
            "precision_at_4": precision_at(ranked, 4, RELEVANT_LABELS),
            "precision_at_10": precision_at(ranked, 10, RELEVANT_LABELS),
            "strict_precision_at_4": precision_at(ranked, 4, STRICT_RELEVANT_LABELS),
            "strict_precision_at_10": precision_at(ranked, 10, STRICT_RELEVANT_LABELS),
            "false_positive_rate_at_10": false_positive_rate_at(ranked, 10),
            "recall_relevant_visible": recall_visible(items, RELEVANT_LABELS),
            "surfacing_false_positives": [item for item in items if is_surfacing_false_positive(item)],
            "classification_false_positives": [item for item in items if is_classification_false_positive(item)],
            "visible_false_negatives": [item for item in items if is_visible_false_negative(item)],
            "explore_only_false_positives": [item for item in items if is_explore_only_false_positive(item)],
        }

    return {
        "total_cases": len(evaluated),
        "headline_cases": len(headline),
        "review_required_cases": len(evaluated) - len(headline),
        "label_distribution_all": Counter(item.case["expected_label"] for item in evaluated),
        "label_distribution_headline": Counter(item.case["expected_label"] for item in headline),
        "source_status": Counter(item.source_status for item in evaluated),
        "metrics_by_profile": metrics_by_profile,
        "surfacing_false_positives": [item for item in headline if is_surfacing_false_positive(item)],
        "classification_false_positives": [item for item in headline if is_classification_false_positive(item)],
        "visible_false_negatives": [item for item in headline if is_visible_false_negative(item)],
        "explore_only_false_positives": [item for item in headline if is_explore_only_false_positive(item)],
        "section_overpromotions": [item for item in headline if is_section_overpromotion(item)],
        "failure_patterns": Counter(
            pattern
            for item in headline
            if (
                is_surfacing_false_positive(item)
                or is_classification_false_positive(item)
                or is_visible_false_negative(item)
                or is_section_overpromotion(item)
            )
            for pattern in item.failure_patterns
        ),
    }


def precision_at(items: list[EvaluatedCase], k: int, relevant_labels: set[str]) -> float:
    if not items:
        return 0.0
    pool = items[:k]
    if not pool:
        return 0.0
    relevant = sum(1 for item in pool if item.case["expected_label"] in relevant_labels)
    return relevant / len(pool)


def false_positive_rate_at(items: list[EvaluatedCase], k: int) -> float:
    pool = items[:k]
    if not pool:
        return 0.0
    false_positives = sum(1 for item in pool if item.case["expected_label"] == "false_positive")
    return false_positives / len(pool)


def recall_visible(items: list[EvaluatedCase], relevant_labels: set[str]) -> float:
    relevant = [item for item in items if item.case["expected_label"] in relevant_labels]
    if not relevant:
        return 0.0
    visible = [item for item in relevant if item.score >= VISIBLE_SCORE_THRESHOLD]
    return len(visible) / len(relevant)


def is_surfacing_false_positive(item: EvaluatedCase) -> bool:
    return (
        not item.case["review_required"]
        and item.case["expected_label"] == "false_positive"
        and item.evaluation_section in PERSONALIZED_SECTIONS
    )


def is_classification_false_positive(item: EvaluatedCase) -> bool:
    return (
        not item.case["review_required"]
        and item.case["expected_label"] == "false_positive"
        and item.evaluation_label != "false_positive"
        and item.score >= MEDIUM_SCORE_THRESHOLD
    )


def is_visible_false_negative(item: EvaluatedCase) -> bool:
    if item.case["review_required"] or item.case["expected_label"] not in RELEVANT_LABELS:
        return False
    return SECTION_LEVELS[item.evaluation_section] < SECTION_LEVELS[expected_section(item.case)]


def is_explore_only_false_positive(item: EvaluatedCase) -> bool:
    return (
        not item.case["review_required"]
        and item.case["expected_label"] == "false_positive"
        and item.evaluation_section == "explore_only"
    )


def is_section_overpromotion(item: EvaluatedCase) -> bool:
    if item.evaluation_section not in PERSONALIZED_SECTIONS:
        return False
    return SECTION_LEVELS[item.evaluation_section] > SECTION_LEVELS[expected_section(item.case)]


def build_live_snapshot(profiles: dict[str, dict], db_rows: list[dict]) -> dict[str, list[dict]]:
    snapshot = {}
    for profile_id, profile in sorted(profiles.items()):
        ranked = matcher.rank_opportunities(
            profile,
            db_rows,
            True,
            10,
            min_score=0,
            require_personalized_eligible=False,
        )
        snapshot[profile_id] = [
            {
                "title": item["display_title"],
                "source": item["source"],
                "score": item["score"],
                "label": match_strength_from_score(item["score"]),
                "reasons": item["reasons"],
                "signals": detect_signals(profile, scored_to_row(item), {})[0],
                "contradictions": detect_signals(profile, scored_to_row(item), {})[1],
            }
            for item in ranked
        ]
    return snapshot


def build_language_diagnostics(profiles: dict[str, dict], db_rows: list[dict]) -> dict:
    observed = Counter()
    affected_profiles = Counter()
    examples = []

    for row in db_rows:
        text = row_language_text(row)
        languages = sorted(detect_explicit_languages(text))
        if languages:
            observed.update(languages)
        for profile_id, profile in profiles.items():
            eligibility = language_eligibility(profile, text)
            if eligibility.eligible_for_personalized or not eligibility.detected_languages:
                continue
            affected_profiles[profile_id] += 1
            if len(examples) < 20:
                examples.append(
                    {
                        "profile_id": profile_id,
                        "source": row.get("source") or "",
                        "title": row.get("title") or row.get("canonical_title") or "",
                        "languages": sorted(eligibility.detected_languages),
                        "reason": eligibility.reason,
                    }
                )

    return {
        "recognized_languages": sorted(CANONICAL_LANGUAGES),
        "observed_languages": observed,
        "unrecognized_tokens": Counter(),
        "excluded_pair_count": sum(affected_profiles.values()),
        "affected_profiles": affected_profiles,
        "examples": examples,
    }


def scored_to_row(item: dict) -> dict:
    return {
        "title": item["display_title"],
        "canonical_title": None,
        "source_category": item.get("expertise"),
        "expertise": item.get("expertise"),
        "department": item.get("expertise"),
        "commitment": "",
        "location": item.get("location"),
        "opportunity_kind": item.get("opportunity_kind"),
        "availability_basis": item.get("availability_basis"),
        "inventory_model": item.get("inventory_model"),
        "market_count_policy": item.get("market_count_policy"),
        "include_in_live_market_estimate": item.get("include_in_live_market_estimate"),
        "source_tier": SOURCE_TIER_CORE,
    }


def render_quality_report(
    generated_at: datetime,
    fixture: dict,
    evaluated: list[EvaluatedCase],
    metrics: dict,
    live_snapshot: dict[str, list[dict]],
    baseline_hash: str,
    baseline_metrics: dict,
    language_diagnostics: dict,
) -> str:
    lines = [
        "# Matching Quality Benchmark Baseline",
        "",
        f"Generated: {generated_at.isoformat()}",
        "",
        "## Scope",
        "",
        (
            "This report evaluates the current production profile matcher against a "
            "fixture pool containing draft and human-reviewed labels. It does not change scoring, thresholds, planner "
            "logic, crawlers, schema, product-state data, or live market estimates."
        ),
        "",
        (
            "`codex_draft` labels are proposed judgments awaiting review. "
            "`human_reviewed` labels include approved review notes and are treated as calibrated fixture labels."
        ),
        "",
        (
            "Precision, recall, and false-positive metrics below are fixture-pool "
            "metrics only. They are not universal production accuracy estimates."
        ),
        "",
        "## Fixture Summary",
        "",
        f"- Fixture: `{relative(FIXTURE_PATH)}`",
        f"- Baseline matcher commit: `{baseline_hash}`",
        f"- Total cases: {metrics['total_cases']}",
        f"- Headline metric cases: {metrics['headline_cases']}",
        f"- Review-required cases excluded from headline metrics: {metrics['review_required_cases']}",
        f"- DB resolution: {format_counter(metrics['source_status'])}",
        f"- Label distribution, all cases: {format_counter(metrics['label_distribution_all'])}",
        f"- Label distribution, headline cases: {format_counter(metrics['label_distribution_headline'])}",
        "",
        "## Apples-To-Apples Matcher Comparison",
        "",
        (
            "Previous and current matchers are evaluated against the same fixture "
            "pool with the same metric definitions. Surfacing metrics account for "
            "personalized-section eligibility; classification metrics use raw scores."
        ),
        "",
        "| Metric | Previous HEAD | Current Working Tree |",
        "|---|---:|---:|",
        comparison_row("Surfacing false positives", baseline_metrics, metrics, "surfacing_false_positives"),
        comparison_row("Classification false positives", baseline_metrics, metrics, "classification_false_positives"),
        comparison_row("Visible false negatives", baseline_metrics, metrics, "visible_false_negatives"),
        comparison_row("Explore-only false positives", baseline_metrics, metrics, "explore_only_false_positives"),
        "",
        "### Precision Before / After By Profile",
        "",
        "| Profile | P@4 Previous | P@4 Current | P@10 Previous | P@10 Current | Strict P@4 Previous | Strict P@4 Current | Strict P@10 Previous | Strict P@10 Current |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]

    for profile_id in sorted(metrics["metrics_by_profile"]):
        previous = baseline_metrics["metrics_by_profile"].get(profile_id, {})
        current = metrics["metrics_by_profile"][profile_id]
        lines.append(
            "| "
            + " | ".join(
                [
                    profile_id,
                    pct(previous.get("precision_at_4", 0)),
                    pct(current["precision_at_4"]),
                    pct(previous.get("precision_at_10", 0)),
                    pct(current["precision_at_10"]),
                    pct(previous.get("strict_precision_at_4", 0)),
                    pct(current["strict_precision_at_4"]),
                    pct(previous.get("strict_precision_at_10", 0)),
                    pct(current["strict_precision_at_10"]),
                ]
            )
            + " |"
        )

    lines.extend(
        [
        "",
        "## Fixture-Pool Metrics By Profile",
        "",
        "| Profile | Cases | Relevant P@4 | Relevant P@10 | Strict P@4 | Strict P@10 | FP@10 | Relevant Recall | Surfacing FP | Classification FP | Visible FN |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )

    for profile_id, profile_metrics in metrics["metrics_by_profile"].items():
        lines.append(
            "| "
            + " | ".join(
                [
                    profile_id,
                    str(profile_metrics["case_count"]),
                    pct(profile_metrics["precision_at_4"]),
                    pct(profile_metrics["precision_at_10"]),
                    pct(profile_metrics["strict_precision_at_4"]),
                    pct(profile_metrics["strict_precision_at_10"]),
                    pct(profile_metrics["false_positive_rate_at_10"]),
                    pct(profile_metrics["recall_relevant_visible"]),
                    str(len(profile_metrics["surfacing_false_positives"])),
                    str(len(profile_metrics["classification_false_positives"])),
                    str(len(profile_metrics["visible_false_negatives"])),
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## Surfacing False Positives",
            "",
        ]
    )
    if not metrics["surfacing_false_positives"]:
        lines.append("No false-positive fixture cases reached personalized sections.")
    else:
        for item in sorted(metrics["surfacing_false_positives"], key=lambda i: (i.case["profile_id"], -i.score, i.case["case_id"])):
            lines.extend(render_failure_block(item))

    lines.extend(
        [
            "",
            "## Classification False Positives",
            "",
        ]
    )
    if not metrics["classification_false_positives"]:
        lines.append("No false-positive fixture cases scored Medium or Strong.")
    else:
        for item in sorted(metrics["classification_false_positives"], key=lambda i: (i.case["profile_id"], -i.score, i.case["case_id"]))[:25]:
            lines.extend(render_failure_block(item))

    lines.extend(
        [
            "",
            "## Visible False Negatives",
            "",
        ]
    )
    if not metrics["visible_false_negatives"]:
        lines.append("No strong/plausible fixture cases fell below expected personalized visibility.")
    else:
        for item in sorted(metrics["visible_false_negatives"], key=lambda i: (i.case["profile_id"], i.case["case_id"])):
            lines.extend(render_failure_block(item))

    lines.extend(render_language_diagnostics(language_diagnostics))

    lines.extend(
        [
            "",
            "## Section Suitability Overpromotions",
            "",
        ]
    )
    if not metrics["section_overpromotions"]:
        lines.append("No overpromotion against expected sections was detected.")
    else:
        lines.extend(
            [
                "| Profile | Case | Expected Section | Evaluation Section | Raw Section | Score | Evaluation Label | Raw Label |",
                "|---|---|---|---|---|---:|---|---|",
            ]
        )
        for item in sorted(metrics["section_overpromotions"], key=lambda i: (i.case["profile_id"], -i.score, i.case["case_id"])):
            lines.append(
                "| "
                + " | ".join(
                    [
                        item.case["profile_id"],
                        escape_table(f"{item.case['source']} - {item.case['title']}"),
                        expected_section(item.case),
                        item.evaluation_section,
                        item.raw_section,
                        str(item.score),
                        item.evaluation_label,
                        item.raw_match_label,
                    ]
                )
                + " |"
            )

    lines.extend(
        [
            "",
            "## Recurring Failure Patterns",
            "",
        ]
    )
    if metrics["failure_patterns"]:
        for pattern, count in metrics["failure_patterns"].most_common():
            lines.append(f"- `{pattern}`: {count}")
    else:
        lines.append("No recurring failure patterns were detected.")

    lines.extend(
        [
            "",
            "## Live Snapshot For Future Review",
            "",
            (
                "These are the current top 10 live matches by benchmark profile. They "
                "are not labeled truth rows unless they also appear in the fixture."
            ),
            "",
        ]
    )
    for profile_id, rows in live_snapshot.items():
        lines.extend(
            [
                f"### {profile_id}",
                "",
                "| Rank | Source | Title | Score | Label | Reasons | Signals | Contradictions |",
                "|---:|---|---|---:|---|---|---|---|",
            ]
        )
        for rank, row in enumerate(rows, start=1):
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(rank),
                        escape_table(row["source"]),
                        escape_table(row["title"]),
                        str(row["score"]),
                        row["label"],
                        escape_table("; ".join(row["reasons"][:4])),
                        escape_table("; ".join(row["signals"][:4]) or "-"),
                        escape_table("; ".join(row["contradictions"][:4]) or "-"),
                    ]
                )
                + " |"
            )
        lines.append("")

    lines.extend(
        [
            "## Recommended Next Deterministic Gate Work",
            "",
            "- Continue human-reviewing the highest-impact remaining `codex_draft` labels before treating precision as product truth.",
            "- Investigate visible false negatives caused by sparse fixture snapshots or missing live metadata.",
            "- Review the history/humanities profile separately; do not inflate generic writing/search roles just to fill recommendation slots.",
            "- Keep surfacing and classification metrics separate before tuning additional deterministic gates.",
            "",
            "## Methodology Notes",
            "",
            "- `strong` and `plausible` count as relevant for fixture-pool relevant precision/recall.",
            "- Only `strong` counts as relevant for strict fixture-pool precision.",
            "- `review_required: true` cases are visible in review files but excluded from headline metrics.",
            "- Stored fixture snapshots are used only when the live DB row cannot be resolved.",
        ]
    )
    return "\n".join(lines) + "\n"


def comparison_row(label: str, baseline_metrics: dict, current_metrics: dict, key: str) -> str:
    return (
        "| "
        + " | ".join(
            [
                label,
                str(len(baseline_metrics[key])),
                str(len(current_metrics[key])),
            ]
        )
        + " |"
    )


def render_language_diagnostics(language_diagnostics: dict) -> list[str]:
    lines = [
        "",
        "## Language Eligibility Diagnostics",
        "",
        f"- Recognized canonical languages: {', '.join(language_diagnostics['recognized_languages'])}",
        f"- Explicit languages observed in active opportunities: {format_counter(language_diagnostics['observed_languages'])}",
        f"- Profile/opportunity pairs excluded by explicit-language eligibility: {language_diagnostics['excluded_pair_count']}",
    ]
    if language_diagnostics["unrecognized_tokens"]:
        lines.append(
            "- Potential unrecognized language tokens: "
            + format_counter(language_diagnostics["unrecognized_tokens"])
        )
    else:
        lines.append("- Potential unrecognized language tokens: none detected by the heuristic.")

    if language_diagnostics["affected_profiles"]:
        lines.append(
            "- Affected profiles: "
            + format_counter(language_diagnostics["affected_profiles"])
        )
    else:
        lines.append("- Affected profiles: none.")

    lines.extend(["", "| Profile | Source | Title | Detected Languages | Reason |", "|---|---|---|---|---|"])
    for example in language_diagnostics["examples"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    example["profile_id"],
                    escape_table(example["source"]),
                    escape_table(example["title"]),
                    ", ".join(example["languages"]),
                    escape_table(example["reason"]),
                ]
            )
            + " |"
        )
    if not language_diagnostics["examples"]:
        lines.append("| - | - | - | - | - |")
    return lines


def render_failure_block(item: EvaluatedCase) -> list[str]:
    case = item.case
    lines = [
        f"### {case['profile_id']} - {case['case_id']}",
        "",
        f"- Opportunity: {case['source']} - {case['title']}",
        f"- Expected: `{case['expected_label']}` / `{expected_section(case)}`",
        f"- Evaluation: `{item.evaluation_label}` / `{item.evaluation_section}`",
        f"- Raw/product matcher: score {item.score}, `{item.raw_match_label}`, `{item.raw_section}`",
        f"- Decisive hard gate: {item.hard_gate_type or '-'} / {item.hard_gate_status or '-'} ({item.hard_gate_reason or '-'})",
        (
            "- Location eligibility: "
            f"{item.location_eligibility_status} / {item.location_restriction_type} restriction / "
            f"{item.profile_location_status} profile location "
            f"({item.location_eligibility_reason})"
        ),
        f"- Applicant-location restriction: {item.applicant_location_requirements or '-'}",
        f"- Personalized eligibility: {'yes' if item.eligible_for_personalized else 'no'} ({item.language_eligibility_reason})",
        f"- Rationale: {case['rationale']}",
        f"- Regression rule: `{case.get('regression_rule') or '-'}`",
        f"- Failure patterns: {', '.join(f'`{pattern}`' for pattern in item.failure_patterns) or '-'}",
        f"- Positive contributions: {', '.join(item.positives) or '-'}",
        f"- Penalties: {', '.join(item.penalties) or '-'}",
        f"- User-facing reasons: {', '.join(item.reasons) or '-'}",
        f"- Contradictions: {', '.join(item.contradictions) or '-'}",
        "",
    ]
    return lines


def render_review_report(
    generated_at: datetime,
    fixture: dict,
    evaluated: list[EvaluatedCase],
) -> str:
    lines = [
        "# Matching Golden Set Human Review Draft",
        "",
        f"Generated: {generated_at.isoformat()}",
        "",
        (
            "Labels in this file may be `codex_draft` or `human_reviewed`. "
            "Draft labels are intended for human review and correction; human-reviewed labels "
            "include approved review notes."
        ),
        "",
        (
            "Review-required cases are excluded from headline precision, recall, "
            "and false-positive metrics in `matching_quality_report.md`."
        ),
        "",
    ]

    by_profile = defaultdict(list)
    for item in evaluated:
        by_profile[item.case["profile_id"]].append(item)

    for profile_id, items in sorted(by_profile.items()):
        lines.extend(
            [
                f"## {profile_id}",
                "",
                "| Case | Source / Title | Expected | Expected Section | Review Required | Regression Rule | Current Score / Label | Rationale |",
                "|---|---|---|---|---|---|---|---|",
            ]
        )
        for item in sorted(items, key=lambda i: i.case["case_id"]):
            case = item.case
            lines.append(
                "| "
                + " | ".join(
                    [
                        case["case_id"],
                        escape_table(f"{case['source']} - {case['title']}"),
                        case["expected_label"],
                        expected_section(case),
                        "yes" if case["review_required"] else "no",
                        escape_table(case.get("regression_rule") or "-"),
                        f"{item.score} / {item.evaluation_label} (raw: {item.raw_match_label})",
                        escape_table(case["rationale"]),
                    ]
                )
                + " |"
            )
        lines.append("")

    return "\n".join(lines) + "\n"


def print_terminal_summary(fixture: dict, metrics: dict, baseline_hash: str, baseline_metrics: dict) -> None:
    print("Matching Quality Benchmark")
    print("==========================")
    print(f"Baseline commit: {baseline_hash}")
    print(f"Fixture cases: {metrics['total_cases']}")
    print(f"Headline cases: {metrics['headline_cases']}")
    print(f"Review-required cases: {metrics['review_required_cases']}")
    print(
        "Surfacing false positives: "
        f"{len(baseline_metrics['surfacing_false_positives'])} -> "
        f"{len(metrics['surfacing_false_positives'])}"
    )
    print(
        "Classification false positives: "
        f"{len(baseline_metrics['classification_false_positives'])} -> "
        f"{len(metrics['classification_false_positives'])}"
    )
    print(
        "Visible false negatives: "
        f"{len(baseline_metrics['visible_false_negatives'])} -> "
        f"{len(metrics['visible_false_negatives'])}"
    )
    print("")
    print("Fixture-pool metrics by profile")
    for profile_id, profile_metrics in metrics["metrics_by_profile"].items():
        print(
            f"- {profile_id}: "
            f"P@4 {pct(profile_metrics['precision_at_4'])}, "
            f"P@10 {pct(profile_metrics['precision_at_10'])}, "
            f"strict P@4 {pct(profile_metrics['strict_precision_at_4'])}, "
            f"FP@10 {pct(profile_metrics['false_positive_rate_at_10'])}, "
            f"surfacing FP {len(profile_metrics['surfacing_false_positives'])}, "
            f"classification FP {len(profile_metrics['classification_false_positives'])}, "
            f"visible FN {len(profile_metrics['visible_false_negatives'])}"
        )
    print("")
    print(f"Wrote Markdown report to {REPORT_PATH}")
    print(f"Wrote review file to {REVIEW_PATH}")


def pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def format_counter(counter: Counter) -> str:
    if not counter:
        return "-"
    return ", ".join(f"{key}={value}" for key, value in counter.most_common())


def normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def normalize_title_key(value: str | None) -> str:
    normalized = normalize_text(value)
    normalized = (
        normalized.replace("\u2010", "-")
        .replace("\u2011", "-")
        .replace("\u2012", "-")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
    )
    normalized = re.sub(r"[^a-z0-9+#]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def normalize_slug(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "-", normalize_text(value)).strip("-")


def normalize_url(value: str | None) -> str:
    return str(value or "").strip().rstrip("/")


def unique(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def escape_table(value: str) -> str:
    return str(value or "-").replace("|", "\\|").replace("\n", " ")


def relative(path: Path) -> str:
    return str(path.relative_to(ROOT)).replace("\\", "/")


if __name__ == "__main__":
    main()

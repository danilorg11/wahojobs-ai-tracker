import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
import re

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wahojobs.classification import (
    INVENTORY_MODEL_EVERGREEN_APPLICATION,
    INVENTORY_MODEL_PUBLIC_INVENTORY,
    INVENTORY_MODEL_MIXED,
    MARKET_COUNT_POLICY_COUNT_LIVE,
    SOURCE_TIER_EXPERIMENTAL,
)
from wahojobs.db.connection import get_connection
from wahojobs.matching.languages import (
    detect_explicit_languages as detect_explicit_languages_for_text,
    language_eligibility,
    language_variants as language_variants_for_name,
    normalize_language_name as canonical_language_name,
    profile_language_set,
    row_language_text,
)
from wahojobs.matching.evergreen import evergreen_applicability
from wahojobs.matching.locations import LOCATION_INCOMPATIBLE, location_eligibility
from wahojobs.reporting.market import CANONICALIZED_SLUGS, get_market_size_summary


OUTPUT_PATH = Path("exports/profile_match_digest.md")
RECENT_DAYS = 7
TOP_LIVE_LIMIT = 8
REPORT_SEPARATELY_LIMIT = 5
NEW_LIMIT = 5
MAYBE_LIMIT = 5
DO_THESE_FIRST_SCORE_THRESHOLD = 30
BEST_MATCHES_SCORE_THRESHOLD = 22
ALSO_WORTH_REVIEWING_SCORE_THRESHOLD = 18
TECHNICAL_ROLE_TERMS = [
    ".net",
    "api",
    "backend",
    "c#",
    "c++",
    "code evaluation",
    "coding",
    "developer",
    "fastapi",
    "frontend",
    "full stack",
    "full-stack",
    "go engineer",
    "hackerank",
    "java",
    "javascript",
    "programming",
    "python",
    "qa automation",
    "react",
    "repository validation",
    "ruby",
    "rust",
    "software",
    "swe-bench",
    "typescript",
]

SCIENCE_MEDICAL_ROLE_TERMS = [
    "academic dermatologist",
    "biology",
    "biomedical",
    "biophysics",
    "chemistry",
    "clinical",
    "dermatologist",
    "genetics",
    "healthcare",
    "life science",
    "math",
    "mathematics",
    "material science",
    "medical",
    "medicine",
    "microbiology",
    "pharma",
    "pharmacokinetics",
    "physics",
    "physicist",
    "scientist",
    "statistics",
]

LEGAL_ROLE_TERMS = [
    "attorney",
    "contract law",
    "corporate law",
    "employment law",
    "ip law",
    "law",
    "lawyer",
    "legal",
    "litigation",
    "m&a",
    "regulatory law",
]

FINANCE_ROLE_TERMS = [
    "accountant",
    "accounting",
    "banker",
    "banking",
    "equity",
    "finance",
    "financial",
    "investment",
    "tax",
    "underwriter",
]

LANGUAGE_ROLE_TERMS = [
    "adaptation",
    "bilingual",
    "language",
    "linguistic",
    "linguistics",
    "localization",
    "mtpe",
    "translation",
    "translator",
]

LEGAL_SPECIALIZED_SUBTYPE_TERMS = [
    "corporate law",
    "employment law",
    "energy law",
    "intellectual property",
    "ip",
    "labor law",
    "litigation",
    "m&a",
    "real estate",
    "regulatory law",
]

SCIENCE_MEDICAL_SPECIALIZED_SUBTYPE_TERMS = [
    "academic dermatologist",
    "biology research",
    "clinical",
    "dermatologist",
    "dermatology",
    "medicine physician",
    "microbiology",
    "pharma",
    "physician",
    "research scientist",
    "scientist",
]

LANGUAGE_SPECIALIZED_SUBTYPE_TERMS = [
    "audio",
    "voice",
    "translation quality",
]

PROFESSIONAL_DOMAIN_HARD_GATE_REASONS = {
    "Specialized science or medical role does not match this profile",
}

GENERALIST_TASK_TERMS = [
    "ai trainer",
    "ai training",
    "annotation",
    "annotator",
    "content review",
    "content reviewing",
    "data annotation",
    "data labelling",
    "data validation",
    "generalist",
    "rater",
    "search engine",
    "search quality",
    "social media annotation",
    "writing",
    "writer",
]

HUMANITIES_TASK_TERMS = [
    "academic writing",
    "education",
    "fact checking",
    "history",
    "humanities",
    "pronunciation evaluation",
    "source evaluation",
    "teaching",
    "writing",
]

GENERIC_ONLY_TERMS = [
    "ai training",
    "evaluation",
    "evaluator",
    "expert",
    "quality",
    "research",
    "review",
    "reviewer",
]


MOCK_PROFILES = [
    {
        "profile_id": "generalist_no_degree",
        "display_name": "Generalist Remote Starter",
        "summary": (
            "No college degree, intermediate/advanced English, wants remote "
            "flexible work, good at web research and basic writing."
        ),
        "education_level": "no_degree",
        "degrees_or_domains": ["generalist"],
        "languages": ["English"],
        "skills": ["web research", "writing", "review"],
        "work_preferences": ["remote", "flexible"],
        "constraints": ["no college degree"],
        "target_opportunity_types": [
            "AI training",
            "AI evaluation",
            "data annotation",
            "search evaluation",
        ],
        "signals": [
            ("Generalist AI-work signal", ["generalist", "ai trainer", "ai training"], 9),
            ("Writing/review signal", ["writing", "writer", "review", "reviewer"], 7),
            ("Research/search evaluation signal", ["research", "search", "rater"], 7),
            ("Data annotation signal", ["annotation", "annotator", "data validation"], 7),
        ],
        "avoid_keywords": [
            "phd",
            "doctorate",
            "attorney",
            "physicist",
            "scientist",
            "physics",
            "biology",
            "chemistry",
            "medicine",
            "medical",
            "finance",
            "legal",
            "senior software engineer",
        ],
    },
    {
        "profile_id": "portuguese_english_reviewer",
        "display_name": "Portuguese-English AI Reviewer",
        "summary": (
            "Portuguese native, advanced English, reviewer/search quality/AI "
            "evaluation background, prefers remote flexible work."
        ),
        "education_level": "not_specified",
        "degrees_or_domains": ["language", "search quality", "ai evaluation"],
        "languages": ["Portuguese", "English"],
        "skills": ["review", "search quality", "evaluation", "translation"],
        "work_preferences": ["remote", "flexible"],
        "constraints": [],
        "target_opportunity_types": ["AI evaluation", "search quality", "language review"],
        "signals": [
            ("Portuguese language match", ["portuguese", "brazilian"], 12),
            ("Language review signal", ["language", "linguistic", "translation", "localization"], 8),
            ("AI evaluation/reviewer signal", ["evaluation", "reviewer", "rater", "quality"], 8),
            ("Search quality signal", ["search", "ads quality", "quality rater"], 8),
        ],
        "avoid_keywords": [],
    },
    {
        "profile_id": "software_engineer",
        "display_name": "Software Engineer / Coding Evaluator",
        "summary": (
            "Software/Python/coding background, interested in coding evaluation, "
            "AI coding tasks, and technical review."
        ),
        "education_level": "technical",
        "degrees_or_domains": ["software", "coding", "python"],
        "languages": ["English"],
        "skills": ["python", "software engineering", "code review", "testing"],
        "work_preferences": ["remote", "flexible"],
        "constraints": [],
        "target_opportunity_types": ["coding evaluation", "technical review", "AI coding"],
        "signals": [
            ("Coding/technical task match", ["coding", "software", "developer", "programming"], 12),
            ("Python match", ["python"], 10),
            ("Technical review/evaluation signal", ["code review", "technical review", "testing", "qa"], 8),
            ("Benchmark/model-evaluation signal", ["swe-bench", "hackerank", "ai quality"], 8),
        ],
        "avoid_keywords": [],
    },
    {
        "profile_id": "lawyer",
        "display_name": "Legal Expert / Lawyer",
        "summary": (
            "Law/legal background, interested in legal AI training, expert "
            "review, contracts, and legal reasoning tasks."
        ),
        "education_level": "professional",
        "degrees_or_domains": ["law", "legal"],
        "languages": ["English"],
        "skills": ["legal reasoning", "contracts", "expert review"],
        "work_preferences": ["remote", "flexible"],
        "constraints": [],
        "target_opportunity_types": ["legal AI training", "expert review"],
        "signals": [
            ("Legal domain match", ["legal", "law", "lawyer", "attorney"], 14),
            ("Expert review signal", ["expert", "review", "reasoning"], 7),
        ],
        "avoid_keywords": [],
    },
    {
        "profile_id": "finance_professional",
        "display_name": "Finance / Accounting Professional",
        "summary": (
            "Finance, accounting, or investment background, interested in finance "
            "expert AI training and evaluation."
        ),
        "education_level": "professional",
        "degrees_or_domains": ["finance", "accounting", "investment"],
        "languages": ["English"],
        "skills": ["finance", "accounting", "investment analysis", "review"],
        "work_preferences": ["remote", "flexible"],
        "constraints": [],
        "target_opportunity_types": ["finance AI training", "expert evaluation"],
        "signals": [
            ("Finance domain match", ["finance", "financial", "investment", "banker"], 14),
            ("Accounting domain match", ["accounting", "accountant", "tax"], 12),
            ("Expert review signal", ["expert", "review", "analyst"], 7),
        ],
        "avoid_keywords": [],
    },
    {
        "profile_id": "biology_or_medicine_academic",
        "display_name": "Biology / Medicine Academic",
        "summary": (
            "Biology, medicine, or life sciences background with advanced academic "
            "profile, interested in expert AI training tasks."
        ),
        "education_level": "advanced_degree",
        "degrees_or_domains": ["biology", "medicine", "life sciences"],
        "languages": ["English"],
        "skills": ["scientific writing", "medical review", "biology"],
        "work_preferences": ["remote", "flexible"],
        "constraints": [],
        "target_opportunity_types": ["STEM expert AI training", "medical AI review"],
        "signals": [
            ("Biology/life sciences match", ["biology", "life science", "genetics", "biomedical"], 14),
            ("Medicine/clinical match", ["medicine", "medical", "clinical", "healthcare"], 14),
            ("Academic/expert signal", ["phd", "academic", "expert", "scientist"], 7),
            ("Python + science task match", ["python"], 4),
        ],
        "avoid_keywords": [],
    },
    {
        "profile_id": "multilingual_translator",
        "display_name": "Multilingual Translator / Linguist",
        "summary": (
            "Multilingual profile with translation, language review, localization, "
            "or linguistic evaluation experience."
        ),
        "education_level": "not_specified",
        "degrees_or_domains": ["language", "translation", "linguistics"],
        "languages": ["English", "Spanish", "Portuguese", "French"],
        "skills": ["translation", "localization", "language review", "linguistics"],
        "work_preferences": ["remote", "flexible"],
        "constraints": [],
        "target_opportunity_types": ["language review", "translation evaluation", "AI linguistics"],
        "signals": [
            ("Language/linguistics match", ["language", "linguistic", "linguistics"], 12),
            ("Translation/localization match", ["translation", "translator", "localization"], 12),
            ("Spanish language match", ["spanish", "espa\u00f1ol"], 8),
            ("Portuguese language match", ["portuguese", "brazilian"], 8),
            ("French language match", ["french"], 8),
        ],
        "avoid_keywords": [],
    },
]


def main():
    args = parse_args()
    generated_at = datetime.now(timezone.utc).replace(microsecond=0)
    cutoff = (generated_at - timedelta(days=RECENT_DAYS)).isoformat()
    profiles, profile_source = load_profiles(args.profiles_file)
    profiles = select_profiles(profiles, args.profile)

    with get_connection() as conn:
        market_summary = get_market_size_summary(
            conn,
            include_experimental=False,
            include_simulation=False,
        )
        live_rows = get_active_rows(conn, policy=MARKET_COUNT_POLICY_COUNT_LIVE)
        evergreen_rows = get_active_rows(
            conn,
            policy_not=MARKET_COUNT_POLICY_COUNT_LIVE,
            inventory_models=(INVENTORY_MODEL_EVERGREEN_APPLICATION,),
        )
        public_inventory_rows = get_active_rows(
            conn,
            policy_not=MARKET_COUNT_POLICY_COUNT_LIVE,
            inventory_models=(INVENTORY_MODEL_PUBLIC_INVENTORY, INVENTORY_MODEL_MIXED),
        )
        new_rows = get_post_baseline_new_rows(conn, cutoff)

    profile_reports = []
    for profile in profiles:
        profile_reports.append(
            {
                "profile": profile,
                "live": rank_opportunities(profile, live_rows, group_canonical=True, limit=TOP_LIVE_LIMIT),
                "evergreen": rank_opportunities(profile, evergreen_rows, group_canonical=False, limit=REPORT_SEPARATELY_LIMIT),
                "public_inventory": rank_opportunities(profile, public_inventory_rows, group_canonical=False, limit=REPORT_SEPARATELY_LIMIT),
                "new": rank_opportunities(profile, new_rows, group_canonical=True, limit=NEW_LIMIT),
                "maybe": rank_opportunities(
                    profile,
                    live_rows,
                    group_canonical=True,
                    limit=MAYBE_LIMIT,
                    min_score=10,
                    max_score=21,
                ),
            }
        )

    markdown = render_markdown(
        generated_at,
        market_summary,
        profile_reports,
        profile_source,
    )
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(markdown, encoding="utf-8")

    print("")
    print("Wahojobs Profile Match Digest")
    print("=============================")
    print(f"Generated: {generated_at.isoformat()} UTC")
    print(f"Profile source: {profile_source}")
    print(f"Profiles rendered: {len(profiles)}")
    print(
        "Estimated Live Market Opportunities: "
        f"{market_summary['estimated_market_opportunities']}"
    )
    for report in profile_reports[:3]:
        top = report["live"][0] if report["live"] else None
        if top:
            print(
                f"{report['profile']['profile_id']}: "
                f"{console_text(top['display_title'])} "
                f"({console_text(top['source'])}, score {top['score']})"
            )
    print(f"Wrote Markdown report to {OUTPUT_PATH}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate a profile-based Wahojobs opportunity match digest."
    )
    parser.add_argument(
        "--profile",
        help="Generate the digest for one profile_id only.",
    )
    parser.add_argument(
        "--profiles-file",
        type=Path,
        help="Load editable profiles from a JSON file.",
    )
    return parser.parse_args()


def load_profiles(path):
    if path is None:
        return [normalize_profile(profile) for profile in MOCK_PROFILES], "built-in mock profiles"

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"Profile file not found: {path}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Profile file is not valid JSON: {path} ({exc})")

    if isinstance(raw, dict) and "profiles" in raw:
        raw_profiles = raw["profiles"]
    elif isinstance(raw, list):
        raw_profiles = raw
    else:
        raise SystemExit(
            "Profile file must be either a list of profiles or an object with a 'profiles' list."
        )

    if not isinstance(raw_profiles, list) or not raw_profiles:
        raise SystemExit("Profile file must contain at least one profile.")

    profiles = []
    seen_ids = set()
    for index, raw_profile in enumerate(raw_profiles, start=1):
        profile = normalize_profile(raw_profile, source=f"profile #{index}")
        if profile["profile_id"] in seen_ids:
            raise SystemExit(f"Duplicate profile_id in profile file: {profile['profile_id']}")
        seen_ids.add(profile["profile_id"])
        profiles.append(profile)
    return profiles, str(path)


def normalize_profile(raw_profile, source="profile"):
    if not isinstance(raw_profile, dict):
        raise SystemExit(f"Malformed {source}: expected an object.")

    profile_id = require_string(raw_profile, "profile_id", source)
    display_name = require_string(raw_profile, "display_name", source)
    notes = optional_string(raw_profile, "notes", "")

    profile = {
        "profile_id": profile_id,
        "display_name": display_name,
        "summary": optional_string(raw_profile, "summary", notes) or build_profile_summary(raw_profile),
        "education_level": optional_string(raw_profile, "education_level", "not_specified"),
        "degrees_or_domains": require_string_list(raw_profile, "degrees_or_domains", source),
        "languages": require_string_list(raw_profile, "languages", source),
        "skills": require_string_list(raw_profile, "skills", source),
        "work_preferences": require_string_list(raw_profile, "work_preferences", source),
        "constraints": require_string_list(raw_profile, "constraints", source, required=False),
        "target_opportunity_types": require_string_list(
            raw_profile,
            "target_opportunity_types",
            source,
            required=False,
        ),
        "notes": notes,
        "signals": raw_profile.get("signals") or derive_signals(raw_profile),
        "avoid_keywords": require_string_list(
            raw_profile,
            "avoid_keywords",
            source,
            required=False,
        ),
    }
    for field in ("location", "country", "residence", "city", "region"):
        profile[field] = optional_string(raw_profile, field, "")

    if not isinstance(profile["signals"], list) or not profile["signals"]:
        raise SystemExit(f"Malformed {source}: could not derive matching signals.")
    return profile


def require_string(raw_profile, field, source):
    value = raw_profile.get(field)
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(f"Malformed {source}: '{field}' must be a non-empty string.")
    return value.strip()


def optional_string(raw_profile, field, default):
    value = raw_profile.get(field, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise SystemExit(f"Malformed profile: '{field}' must be a string.")
    return value.strip()


def require_string_list(raw_profile, field, source, required=True):
    value = raw_profile.get(field)
    if value is None:
        if required:
            raise SystemExit(f"Malformed {source}: '{field}' must be a list of strings.")
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise SystemExit(f"Malformed {source}: '{field}' must be a list of strings.")
    return [item.strip() for item in value if item.strip()]


def build_profile_summary(raw_profile):
    pieces = []
    for field in ("degrees_or_domains", "skills", "target_opportunity_types"):
        values = raw_profile.get(field) or []
        if values:
            pieces.append(", ".join(values[:3]))
    if pieces:
        return "Profile signals: " + "; ".join(pieces) + "."
    return "Editable user profile."


def derive_signals(raw_profile):
    text = normalize_text(
        " ".join(
            " ".join(raw_profile.get(field) or [])
            for field in (
                "degrees_or_domains",
                "skills",
                "target_opportunity_types",
                "notes",
            )
            if isinstance(raw_profile.get(field), list)
        )
    )
    signals = []

    add_signal_if(
        signals,
        text,
        "Teaching/writing/review signal",
        ["teacher", "teaching", "education", "writing", "review", "content"],
        9,
    )
    if "english" in normalize_text(" ".join(raw_profile.get("languages") or [])) and any(
        keyword_matches(text, keyword)
        for keyword in ("teacher", "teaching", "english teacher")
    ):
        signals.append(
            (
                "English writing/content review signal",
                ["english writing", "content reviewing", "english writing generalist"],
                12,
            )
        )
    add_signal_if(
        signals,
        text,
        "Language/translation signal",
        ["language", "linguistic", "translation", "translator", "localization", "bilingual"],
        10,
    )
    add_signal_if(
        signals,
        text,
        "Research/humanities signal",
        ["history", "historian", "research", "humanities", "academic"],
        10,
    )
    add_signal_if(
        signals,
        text,
        "Coding/technical signal",
        ["software", "coding", "python", "developer", "programming", "technical"],
        12,
    )
    add_signal_if(
        signals,
        text,
        "Legal domain signal",
        ["legal", "law", "lawyer", "attorney"],
        14,
    )
    add_signal_if(
        signals,
        text,
        "Finance/accounting signal",
        ["finance", "financial", "accounting", "investment", "tax"],
        14,
    )
    add_signal_if(
        signals,
        text,
        "Science/medical signal",
        ["biology", "medicine", "medical", "clinical", "chemistry", "physics", "science"],
        14,
    )
    add_signal_if(
        signals,
        text,
        "AI evaluation/training signal",
        ["ai training", "ai evaluation", "evaluation", "rater", "annotation", "data annotation"],
        8,
    )
    add_signal_if(
        signals,
        text,
        "Search/research quality signal",
        ["search", "research", "quality", "ads"],
        7,
    )

    if not signals:
        signals.append(("General profile keyword match", profile_keywords(raw_profile), 6))
    return signals


def add_signal_if(signals, text, reason, keywords, points):
    normalized = normalize_keywords(keywords)
    if any(keyword_matches(text, keyword) for keyword in normalized):
        signals.append((reason, keywords, points))


def profile_keywords(raw_profile):
    keywords = []
    for field in ("degrees_or_domains", "skills", "target_opportunity_types"):
        for value in raw_profile.get(field) or []:
            keywords.extend(split_profile_phrase(value))
    return unique(keywords)[:12] or ["ai training", "review"]


def split_profile_phrase(value):
    return [
        part.strip()
        for part in re.split(r"[,/;]", value)
        if part.strip()
    ]


def select_profiles(profiles, profile_id):
    if not profile_id:
        return profiles

    selected = [profile for profile in profiles if profile["profile_id"] == profile_id]
    if selected:
        return selected

    available = ", ".join(profile["profile_id"] for profile in profiles)
    raise SystemExit(
        f"Profile not found: {profile_id}. Available profile_id values: {available}"
    )


def get_active_rows(conn, policy=None, policy_not=None, inventory_models=None):
    where = [
        "j.is_active = 1",
        "j.title NOT LIKE '[SIMULATION]%'",
        "c.source_tier != ?",
    ]
    params = [SOURCE_TIER_EXPERIMENTAL]

    if policy:
        where.append("c.market_count_policy = ?")
        params.append(policy)
    if policy_not:
        where.append("c.market_count_policy != ?")
        params.append(policy_not)
    if inventory_models:
        placeholders = ",".join("?" for _ in inventory_models)
        where.append(f"c.inventory_model IN ({placeholders})")
        params.extend(inventory_models)

    return conn.execute(
        f"""
        SELECT
          j.id AS job_id,
          j.title,
          j.location,
          j.url,
          j.department,
          j.expertise,
          j.commitment,
          j.opportunity_kind,
          j.availability_basis,
          j.include_in_live_market_estimate,
          j.canonical_opportunity_id,
          c.name AS source,
          c.slug AS source_slug,
          c.source_tier,
          c.inventory_model,
          c.market_count_policy,
          co.canonical_title,
          co.source_category,
          co.language,
          co.language_locale,
          NULL AS required_languages
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        LEFT JOIN canonical_opportunities co ON co.id = j.canonical_opportunity_id
        WHERE {" AND ".join(where)}
        ORDER BY c.name ASC, j.title ASC
        """,
        params,
    ).fetchall()


def get_post_baseline_new_rows(conn, cutoff):
    return conn.execute(
        f"""
        SELECT
          j.id AS job_id,
          j.title,
          j.location,
          j.url,
          j.department,
          j.expertise,
          j.commitment,
          j.opportunity_kind,
          j.availability_basis,
          j.include_in_live_market_estimate,
          j.canonical_opportunity_id,
          c.name AS source,
          c.slug AS source_slug,
          c.source_tier,
          c.inventory_model,
          c.market_count_policy,
          co.canonical_title,
          co.source_category,
          co.language,
          co.language_locale,
          NULL AS required_languages
        FROM job_events je
        JOIN jobs j ON j.id = je.job_id
        JOIN companies c ON c.id = j.company_id
        JOIN ({baseline_crawl_sql()}) b ON b.company_id = c.id
        LEFT JOIN canonical_opportunities co ON co.id = j.canonical_opportunity_id
        WHERE je.created_at >= ?
          AND je.event_type = 'discovered'
          AND je.crawl_run_id != b.baseline_crawl_run_id
          AND j.is_active = 1
          AND j.title NOT LIKE '[SIMULATION]%'
          AND c.source_tier != ?
        ORDER BY je.created_at DESC, je.id DESC
        """,
        (cutoff, SOURCE_TIER_EXPERIMENTAL),
    ).fetchall()


def baseline_crawl_sql():
    return """
        SELECT cr.company_id, cr.id AS baseline_crawl_run_id
        FROM crawl_runs cr
        WHERE cr.status = 'success'
          AND cr.id = (
            SELECT cr2.id
            FROM crawl_runs cr2
            WHERE cr2.company_id = cr.company_id
              AND cr2.status = 'success'
            ORDER BY COALESCE(cr2.started_at, cr2.created_at), cr2.id
            LIMIT 1
          )
    """


def rank_opportunities(
    profile,
    rows,
    group_canonical,
    limit,
    min_score=18,
    max_score=None,
    require_personalized_eligible=True,
):
    grouped = {}
    for row in rows:
        key = opportunity_key(row, group_canonical)
        scored = score_opportunity(profile, row)
        if require_personalized_eligible and not scored["eligible_for_personalized"]:
            continue
        if require_personalized_eligible and scored.get("location_actionability_cap_applied"):
            continue
        if (
            scored["score"] < min_score
            and section_rank(scored.get("effective_product_section")) < section_rank("also_worth_reviewing")
        ):
            continue
        if max_score is not None and scored["score"] > max_score:
            continue

        existing = grouped.get(key)
        if existing is None:
            grouped[key] = scored
            grouped[key]["variant_count"] = 1
            continue

        existing["variant_count"] += 1
        if scored["score"] > existing["score"]:
            scored["variant_count"] = existing["variant_count"]
            grouped[key] = scored

    ranked = sorted(
        grouped.values(),
        key=lambda item: (-item["score"], item["source"], item["display_title"]),
    )
    for item in ranked:
        if item["variant_count"] > 1 and item["is_canonical_representative"]:
            item["reasons"].append(
                f"Representative canonical opportunity ({item['variant_count']} raw variants)"
            )
    return ranked[:limit]


def opportunity_key(row, group_canonical):
    canonical_id = row["canonical_opportunity_id"]
    if group_canonical and canonical_id:
        return ("canonical", canonical_id)
    return ("job", row["job_id"])


def score_opportunity(profile, row):
    title = row["title"] or row["canonical_title"] or "Untitled opportunity"
    expertise = row["source_category"] or row["expertise"] or row["department"] or "Unknown"
    text = searchable_text(row, title, expertise)
    language_check = language_eligibility(profile, row_language_text(row))
    location_check = location_eligibility(profile, row)
    evergreen_check = evergreen_applicability(profile, row, language_check)
    score = 0
    reasons = []

    for reason, keywords, points in profile["signals"]:
        if any(keyword_matches(text, keyword) for keyword in normalize_keywords(keywords)):
            score += points
            reasons.append(reason)

    for language in sorted(profile_language_set(profile)):
        if not language_check.language_signal_allowed:
            continue
        if language in language_check.matched_languages:
            score += 6
            reasons.append(f"{language.title()} language signal")

    if wants_remote(profile) and has_remote_signal(row):
        score += 5
        reasons.append("Remote/flexible signal")

    if row["market_count_policy"] == MARKET_COUNT_POLICY_COUNT_LIVE and row["include_in_live_market_estimate"]:
        score += 3
        reasons.append("Live/countable opportunity")
    else:
        score += 2
        if row["inventory_model"] == INVENTORY_MODEL_EVERGREEN_APPLICATION:
            reasons.append("Evergreen application, useful but not counted in live estimate")
        elif row["inventory_model"] == INVENTORY_MODEL_PUBLIC_INVENTORY:
            reasons.append("Public inventory opportunity, report separately")
        elif row["inventory_model"] == INVENTORY_MODEL_MIXED:
            reasons.append("Mixed/report-separately source, useful but not counted in live estimate")

    if row["source_tier"] != SOURCE_TIER_EXPERIMENTAL:
        score += 1

    for keyword in normalize_keywords(profile.get("avoid_keywords", [])):
        if keyword_matches(text, keyword):
            score -= 12
            reasons.append("Possible requirement mismatch; review carefully")

    quality_penalties = match_quality_gate_penalties(
        profile,
        row,
        quality_gate_text(row, title, expertise),
    )
    for reason, penalty in quality_penalties:
        score -= penalty
        reasons.append(reason)

    score = max(score, 0)
    professional_domain_hard_gate = professional_domain_hard_gate_for_penalties(score, quality_penalties, row)
    direct_domain_floor = direct_domain_label_floor(profile, row, language_check, location_check)
    evergreen_label_floor = evergreen_label_floor_for_applicability(evergreen_check, location_check)
    raw_section = product_section_for_score(score, language_check.eligible_for_personalized)
    evergreen_adjusted_section = raw_section
    evergreen_floor_applied = False
    evergreen_visible_reason_added = False
    if (
        evergreen_check.qualifies
        and raw_section == "explore_only"
        and language_check.eligible_for_personalized
    ):
        evergreen_adjusted_section = "also_worth_reviewing"
        evergreen_floor_applied = True

    effective_section = evergreen_adjusted_section
    specialized_cap = specialized_actionability_cap(profile, row, effective_section)
    specialized_actionability_cap_applied = False
    specialized_actionability_cap_reason = ""
    if specialized_cap:
        effective_section = specialized_cap["section"]
        specialized_actionability_cap_applied = True
        specialized_actionability_cap_reason = specialized_cap["reason"]
        reasons.append(specialized_actionability_cap_reason)

    location_cap_applied = (
        location_check.actionability_cap_required and effective_section != "explore_only"
    )
    if location_cap_applied:
        effective_section = "explore_only"
        reasons.append(location_check.reason)
    elif evergreen_floor_applied:
        reasons.append(evergreen_check.reason)
        evergreen_visible_reason_added = True

    return {
        "score": score,
        "display_title": title,
        "source": row["source"],
        "source_slug": row["source_slug"],
        "location": row["location"] or "Unknown",
        "expertise": expertise,
        "url": row["url"],
        "job_id": row["job_id"],
        "canonical_opportunity_id": row["canonical_opportunity_id"],
        "opportunity_kind": row["opportunity_kind"],
        "availability_basis": row["availability_basis"],
        "inventory_model": row["inventory_model"],
        "market_count_policy": row["market_count_policy"],
        "include_in_live_market_estimate": row["include_in_live_market_estimate"],
        "is_canonical_representative": bool(row["canonical_opportunity_id"]),
        "language": optional_row_value(row, "language"),
        "language_locale": optional_row_value(row, "language_locale"),
        "required_languages": optional_row_value(row, "required_languages"),
        "eligible_for_personalized": language_check.eligible_for_personalized,
        "language_requirement_mode": language_check.requirement_mode,
        "detected_languages": sorted(language_check.detected_languages),
        "matched_languages": sorted(language_check.matched_languages),
        "unsupported_languages": sorted(language_check.unsupported_languages),
        "language_eligibility_reason": language_check.reason,
        "location_eligibility_status": location_check.status,
        "location_eligibility_reason": location_check.reason,
        "profile_location": location_check.profile_location,
        "profile_location_status": location_check.profile_location_status,
        "applicant_location_requirements": location_check.applicant_location_requirements,
        "location_restriction_type": location_check.restriction_type,
        "job_location_scope": location_check.job_location_scope,
        "job_remote_status": location_check.job_remote_status,
        "location_actionability_cap_required": location_check.actionability_cap_required,
        "location_actionability_cap_applied": location_cap_applied,
        "evergreen_applicability_qualifies": evergreen_check.qualifies,
        "evergreen_opportunity_kind": evergreen_check.opportunity_kind,
        "evergreen_profile_kind": evergreen_check.profile_kind,
        "evergreen_applicability_reason": evergreen_check.reason,
        "evergreen_floor_applied": evergreen_floor_applied,
        "evergreen_visible_reason_added": evergreen_visible_reason_added,
        "specialized_actionability_cap_applied": specialized_actionability_cap_applied,
        "specialized_actionability_cap_reason": specialized_actionability_cap_reason,
        "direct_domain_label_floor_applied": bool(direct_domain_floor),
        "direct_domain_label_floor_reason": direct_domain_floor["reason"] if direct_domain_floor else "",
        "direct_domain_label_floor": direct_domain_floor["label"] if direct_domain_floor else "",
        "evergreen_label_floor_applied": bool(evergreen_label_floor),
        "evergreen_label_floor_reason": evergreen_label_floor["reason"] if evergreen_label_floor else "",
        "evergreen_label_floor": evergreen_label_floor["label"] if evergreen_label_floor else "",
        "professional_domain_hard_gate_applied": bool(professional_domain_hard_gate),
        "professional_domain_hard_gate_reason": (
            professional_domain_hard_gate["reason"] if professional_domain_hard_gate else ""
        ),
        "raw_product_section": raw_section,
        "evergreen_adjusted_section": evergreen_adjusted_section,
        "effective_product_section": effective_section,
        "reasons": unique(reasons)[:6],
    }


def product_section_for_score(score, eligible_for_personalized=True):
    if not eligible_for_personalized:
        return "explore_only"
    if score >= DO_THESE_FIRST_SCORE_THRESHOLD:
        return "do_these_first"
    if score >= BEST_MATCHES_SCORE_THRESHOLD:
        return "best_matches"
    if score >= ALSO_WORTH_REVIEWING_SCORE_THRESHOLD:
        return "also_worth_reviewing"
    return "explore_only"


def optional_row_value(row, key):
    if hasattr(row, "get"):
        return row.get(key)
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def section_rank(section):
    return {
        "exclude": 0,
        "explore_only": 1,
        "also_worth_reviewing": 2,
        "best_matches": 3,
        "do_these_first": 4,
    }.get(section or "explore_only", 1)


def direct_domain_label_floor(profile, row, language_check, location_check):
    if not language_check.eligible_for_personalized:
        return None
    if location_check.status == LOCATION_INCOMPATIBLE:
        return None
    if row.get("inventory_model") == INVENTORY_MODEL_EVERGREEN_APPLICATION:
        return None

    structured_text = structured_actionability_text(row)
    profile_features = detect_profile_match_features(profile)
    role_features = detect_role_match_features(structured_text)
    role_domains = role_features["professional_domains"]
    profile_domains = profile_features["professional_domains"]

    if "legal" in profile_domains and "legal" in role_domains:
        return {
            "label": "strong",
            "reason": "Structured legal-domain metadata directly matches this profile.",
        }

    structured_science_role = "science_medical" in role_domains or contains_any(
        structured_text,
        ["science", "sciences medical", "healthcare medical"],
    )
    if "science_medical" in profile_domains and structured_science_role:
        return {
            "label": "strong",
            "reason": "Structured science or medical-domain metadata directly matches this profile.",
        }

    return None


def evergreen_label_floor_for_applicability(evergreen_check, location_check):
    if not evergreen_check.qualifies:
        return None
    if location_check.status == LOCATION_INCOMPATIBLE:
        return None
    if location_check.actionability_cap_required:
        return None
    return {
        "label": "plausible",
        "reason": "Broad evergreen application is relevant enough to keep in the application pipeline.",
    }


def professional_domain_hard_gate_for_penalties(score, quality_penalties, row):
    if score != 0:
        return None
    if row.get("inventory_model") == INVENTORY_MODEL_EVERGREEN_APPLICATION:
        return None
    for reason, _penalty in quality_penalties:
        if reason in PROFESSIONAL_DOMAIN_HARD_GATE_REASONS:
            return {
                "type": "professional_domain",
                "reason": reason,
            }
    return None


def specialized_actionability_cap(profile, row, current_section):
    if current_section != "do_these_first":
        return None

    profile_text = profile_match_text(profile)
    structured_text = structured_actionability_text(row)
    profile_features = detect_profile_match_features(profile)
    role_features = detect_role_match_features(structured_text)
    role_domains = role_features["professional_domains"]
    profile_domains = profile_features["professional_domains"]

    structured_science_role = "science_medical" in role_domains or contains_any(
        structured_text,
        ["science", "sciences medical", "healthcare medical"],
    )
    if "technical" in profile_domains and structured_science_role and "science_medical" not in profile_domains:
        return {
            "section": "also_worth_reviewing",
            "reason": "Specialized cross-domain role; review fit before prioritizing.",
        }

    if "legal" in profile_domains and "legal" in role_domains:
        subtype = first_missing_subtype(
            profile_text,
            structured_text,
            LEGAL_SPECIALIZED_SUBTYPE_TERMS,
        )
        if subtype:
            return {
                "section": "best_matches",
                "reason": "Specialized legal subtype; review fit before making it a top action.",
            }

    if "science_medical" in profile_domains and "science_medical" in role_domains:
        subtype = first_missing_subtype(
            profile_text,
            structured_text,
            SCIENCE_MEDICAL_SPECIALIZED_SUBTYPE_TERMS,
        )
        if subtype:
            return {
                "section": "best_matches",
                "reason": "Specialized science or medical subtype; review fit before making it a top action.",
            }

    language_terms = set(detect_explicit_languages(structured_text))
    profile_languages = profile_language_set(profile)
    if language_terms and language_terms <= profile_languages:
        subtype = first_missing_subtype(
            profile_text,
            structured_text,
            LANGUAGE_SPECIALIZED_SUBTYPE_TERMS,
        )
        if subtype:
            return {
                "section": "best_matches",
                "reason": "Specialized language-work subtype; review fit before making it a top action.",
            }

    return None


def first_missing_subtype(profile_text, structured_text, subtype_terms):
    for term in subtype_terms:
        if keyword_matches(structured_text, term) and not keyword_matches(profile_text, term):
            return term
    return ""


def profile_match_text(profile):
    values = [
        profile.get("profile_id", ""),
        profile.get("display_name", ""),
        profile.get("summary", ""),
        profile.get("education_level", ""),
        " ".join(profile.get("degrees_or_domains") or []),
        " ".join(profile.get("skills") or []),
        " ".join(profile.get("target_opportunity_types") or []),
        profile.get("notes", ""),
    ]
    return normalize_text(" ".join(str(value or "") for value in values))


def structured_actionability_text(row):
    values = [
        row.get("source_category"),
        row.get("expertise"),
        row.get("department"),
        row.get("opportunity_kind"),
        row.get("availability_basis"),
        row.get("required_languages"),
        row.get("language"),
        row.get("language_locale"),
    ]
    return normalize_text(" ".join(str(value or "") for value in values))


def searchable_text(row, title, expertise):
    values = [
        title,
        row["title"],
        expertise,
        row["department"],
        row["expertise"],
        row["commitment"],
        row["location"],
        row["opportunity_kind"],
        row["availability_basis"],
        row["inventory_model"],
    ]
    return normalize_text(" ".join(str(value or "") for value in values))


def quality_gate_text(row, title, expertise):
    values = [
        title,
        row["title"],
        expertise,
        row["department"],
        row["expertise"],
        row["commitment"],
        row["canonical_title"],
        row["source_category"],
    ]
    return normalize_text(" ".join(str(value or "") for value in values))


def normalize_text(value):
    return re.sub(r"\s+", " ", value.lower()).strip()


def normalize_keywords(keywords):
    return [normalize_text(keyword) for keyword in keywords]


def keyword_matches(text, keyword):
    text = normalize_text(text)
    pattern = keyword_match_pattern(keyword)
    if pattern is None:
        return False
    return pattern.search(text) is not None


@lru_cache(maxsize=4096)
def keyword_match_pattern(keyword):
    keyword = normalize_text(keyword)
    if not keyword:
        return None
    parts = re.findall(r"[a-z0-9+#.]+", keyword)
    if not parts:
        return re.compile(re.escape(keyword))

    separator = r"[\s\-/&()+.,:]+"
    pattern = separator.join(re.escape(part) for part in parts)
    return re.compile(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])")


def match_quality_gate_penalties(profile, row, text=None):
    title = row["title"] or row["canonical_title"] or "Untitled opportunity"
    expertise = row["source_category"] or row["expertise"] or row["department"] or "Unknown"
    text = text or quality_gate_text(row, title, expertise)
    profile_features = detect_profile_match_features(profile)
    role_features = detect_role_match_features(text)
    penalties = []

    role_domains = role_features["professional_domains"]
    profile_domains = profile_features["professional_domains"]
    has_matching_professional_domain = bool(role_domains & profile_domains)

    if (
        not has_matching_professional_domain
        and "technical" in role_domains
        and "technical" not in profile_domains
    ):
        if not ("science_medical" in role_domains and "science_medical" in profile_domains):
            penalties.append(("Technical/software role does not match this profile", 28))

    if (
        not has_matching_professional_domain
        and "science_medical" in role_domains
        and "science_medical" not in profile_domains
    ):
        if not ("technical" in role_domains and "technical" in profile_domains):
            penalties.append(("Specialized science or medical role does not match this profile", 28))

    if (
        not has_matching_professional_domain
        and "legal" in role_domains
        and "legal" not in profile_domains
    ):
        if not ("finance" in role_domains and "finance" in profile_domains):
            penalties.append(("Legal role does not match this profile", 28))

    if (
        not has_matching_professional_domain
        and "finance" in role_domains
        and "finance" not in profile_domains
    ):
        if not ("legal" in role_domains and "legal" in profile_domains):
            penalties.append(("Finance or accounting role does not match this profile", 28))

    if not has_meaningful_positive_evidence(profile_features, role_features) and has_generic_only_evidence(text):
        penalties.append(("Match is based mostly on generic AI-work terms", 10))

    return unique_penalties(penalties)


def detect_profile_match_features(profile):
    profile_text = normalize_text(
        " ".join(
            [
                profile.get("profile_id", ""),
                profile.get("display_name", ""),
                profile.get("summary", ""),
                profile.get("education_level", ""),
                " ".join(profile.get("degrees_or_domains") or []),
                " ".join(profile.get("skills") or []),
                " ".join(profile.get("target_opportunity_types") or []),
                profile.get("notes", ""),
            ]
        )
    )
    languages = tuple(sorted(profile_language_set(profile)))
    return detect_profile_match_features_cached(profile_text, languages)


@lru_cache(maxsize=128)
def detect_profile_match_features_cached(profile_text, languages):
    languages = set(languages)
    professional_domains = set()

    if contains_any(profile_text, TECHNICAL_ROLE_TERMS + ["software engineering", "code review"]):
        professional_domains.add("technical")
    if contains_any(profile_text, LEGAL_ROLE_TERMS + ["contracts"]):
        professional_domains.add("legal")
    if contains_any(profile_text, FINANCE_ROLE_TERMS + ["investment analysis"]):
        professional_domains.add("finance")
    if contains_any(profile_text, SCIENCE_MEDICAL_ROLE_TERMS + ["life sciences"]):
        professional_domains.add("science_medical")

    return {
        "languages": languages,
        "professional_domains": professional_domains,
        "language_profile": contains_any(profile_text, LANGUAGE_ROLE_TERMS) or len(languages) > 1,
        "generalist_profile": contains_any(
            profile_text,
            ["generalist", "no college degree", "no degree", "search evaluation", "data annotation"],
        ),
        "humanities_profile": contains_any(profile_text, HUMANITIES_TASK_TERMS + ["humanities", "academic research"]),
        "task_interests": {
            "generalist": contains_any(profile_text, GENERALIST_TASK_TERMS),
            "humanities": contains_any(profile_text, HUMANITIES_TASK_TERMS),
            "language": contains_any(profile_text, LANGUAGE_ROLE_TERMS),
        },
    }


def detect_role_match_features(text):
    text = normalize_text(text)
    explicit_languages = detect_explicit_languages(text)
    professional_domains = set()
    if contains_any(text, TECHNICAL_ROLE_TERMS):
        professional_domains.add("technical")
    if contains_any(text, SCIENCE_MEDICAL_ROLE_TERMS):
        professional_domains.add("science_medical")
    if contains_any(text, LEGAL_ROLE_TERMS):
        professional_domains.add("legal")
    if contains_any(text, FINANCE_ROLE_TERMS):
        professional_domains.add("finance")

    language_role = contains_any(text, LANGUAGE_ROLE_TERMS) or (
        bool(explicit_languages)
        and contains_any(text, ["rater", "reviewer", "writer", "quality analyst", "audio", "voice"])
    )

    return {
        "explicit_languages": explicit_languages,
        "professional_domains": professional_domains,
        "language_role": language_role,
        "generalist_task": contains_any(text, GENERALIST_TASK_TERMS),
        "humanities_task": contains_any(text, HUMANITIES_TASK_TERMS),
        "technical_task": contains_any(text, TECHNICAL_ROLE_TERMS),
    }


def has_meaningful_positive_evidence(profile_features, role_features):
    shared_languages = profile_features["languages"] & role_features["explicit_languages"]
    if shared_languages and (role_features["language_role"] or profile_features["language_profile"]):
        return True

    if profile_features["professional_domains"] & role_features["professional_domains"]:
        return True

    if profile_features["generalist_profile"] and role_features["generalist_task"]:
        return True

    if profile_features["humanities_profile"] and role_features["humanities_task"]:
        return True

    if profile_features["language_profile"] and role_features["language_role"] and shared_languages:
        return True

    return False


def has_generic_only_evidence(text):
    return contains_any(text, GENERIC_ONLY_TERMS)


def contains_any(text, terms):
    text = normalize_text(text)
    for term in terms:
        pattern = keyword_match_pattern(term)
        if pattern is not None and pattern.search(text):
            return True
    return False


def detect_explicit_languages(text):
    return detect_explicit_languages_for_text(text)


def normalize_language_name(value):
    return canonical_language_name(value)


def unique_penalties(penalties):
    seen = set()
    result = []
    for reason, penalty in penalties:
        if reason not in seen:
            seen.add(reason)
            result.append((reason, penalty))
    return result


def language_variants(language):
    return language_variants_for_name(language)


def wants_remote(profile):
    return any(pref in {"remote", "flexible"} for pref in profile["work_preferences"])


def has_remote_signal(row):
    text = normalize_text(
        " ".join(
            str(value or "")
            for value in (row["location"], row["commitment"], row["title"])
        )
    )
    return any(
        keyword in text
        for keyword in ("remote", "freelance", "flexible", "worldwide", "work from home")
    )


def render_markdown(generated_at, market_summary, profile_reports, profile_source):
    lines = [
        "# Profile-Based AI Work Opportunity Match Digest",
        "",
        f"Generated: {generated_at.isoformat()} UTC",
        "",
        "## Prototype Notes",
        "",
        (
            "This read-only prototype uses editable or built-in profiles and deterministic "
            "keyword scoring against current tracker data. It does not call external "
            "AI APIs, change database rows, or change live market estimate semantics."
        ),
        "",
        f"- Profile source: **{escape(profile_source)}**",
        f"- Profiles rendered: **{len(profile_reports)}**",
        f"- Estimated Live Market Opportunities: **{market_summary['estimated_market_opportunities']}**",
        f"- Raw active live postings: **{market_summary['raw_active_postings']}**",
        "- Canonicalized live sources are grouped into representative opportunities where possible.",
        "- Report-separately sources can be useful for job seekers but do not affect the live estimate.",
        "",
    ]

    for report in profile_reports:
        append_profile_report(lines, report)

    return "\n".join(lines)


def append_profile_report(lines, report):
    profile = report["profile"]
    lines.extend(
        [
            f"## {profile['display_name']}",
            "",
            "### Profile Summary",
            "",
            profile["summary"],
            "",
            f"- Profile ID: `{profile['profile_id']}`",
            f"- Education: {profile['education_level']}",
            f"- Domains: {', '.join(profile['degrees_or_domains'])}",
            f"- Languages: {', '.join(profile['languages'])}",
            f"- Skills: {', '.join(profile['skills'])}",
            f"- Preferences: {', '.join(profile['work_preferences'])}",
            "",
        ]
    )

    append_match_table(lines, "Top Recommended Live Opportunities", report["live"])
    append_match_table(lines, "Evergreen / Always-Open Applications", report["evergreen"])
    append_match_table(
        lines,
        "Public Inventory / Report-Separately Matches",
        report["public_inventory"],
    )
    append_match_table(lines, "New Since Baseline", report["new"])
    append_match_table(lines, "Lower Confidence / Maybe Matches", report["maybe"])

    lines.extend(
        [
            "### Why Not Shown / Product Notes",
            "",
            (
                "Results are capped to avoid overwhelming the user. Scores are "
                "simple deterministic signals, not eligibility decisions. Future "
                "versions could parse resumes or LinkedIn profiles, collect user "
                "feedback, and tune profile-specific weights."
            ),
            "",
        ]
    )


def append_match_table(lines, title, matches):
    lines.extend([f"### {title}", ""])
    if title == "Evergreen / Always-Open Applications":
        lines.append("Application surfaces, not live postings; useful but reported separately.")
        lines.append("")
    elif title == "Public Inventory / Report-Separately Matches":
        lines.append("Useful public inventory or mixed-source records; excluded from the live estimate.")
        lines.append("")
    elif title == "New Since Baseline":
        lines.append("Post-baseline discoveries only; initial source backfills are excluded.")
        lines.append("")

    if not matches:
        lines.extend(["No strong matches found in this section.", ""])
        return

    lines.extend(
        [
            "| Score | Title | Source | Location | Expertise/Department | URL | Reasons |",
            "| ---: | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for match in matches:
        reasons = "; ".join(match["reasons"]) or "Potential match; review details"
        lines.append(
            "| "
            f"{match['score']} | "
            f"{escape(match['display_title'])} | "
            f"{escape(match['source'])} | "
            f"{escape(match['location'])} | "
            f"{escape(match['expertise'])} | "
            f"[Open]({match['url']}) | "
            f"{escape(reasons)} |"
        )
    lines.append("")


def unique(values):
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def escape(value):
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def console_text(value):
    return str(value or "").encode("ascii", errors="replace").decode("ascii")


if __name__ == "__main__":
    main()

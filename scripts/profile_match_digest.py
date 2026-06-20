import sys
from datetime import datetime, timedelta, timezone
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
from wahojobs.reporting.market import CANONICALIZED_SLUGS, get_market_size_summary


OUTPUT_PATH = Path("exports/profile_match_digest.md")
RECENT_DAYS = 7
TOP_LIVE_LIMIT = 8
REPORT_SEPARATELY_LIMIT = 5
NEW_LIMIT = 5
MAYBE_LIMIT = 5


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
    generated_at = datetime.now(timezone.utc).replace(microsecond=0)
    cutoff = (generated_at - timedelta(days=RECENT_DAYS)).isoformat()

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
    for profile in MOCK_PROFILES:
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

    markdown = render_markdown(generated_at, market_summary, profile_reports)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(markdown, encoding="utf-8")

    print("")
    print("Wahojobs Profile Match Digest")
    print("=============================")
    print(f"Generated: {generated_at.isoformat()} UTC")
    print(f"Mock profiles: {len(MOCK_PROFILES)}")
    print(
        "Estimated Live Market Opportunities: "
        f"{market_summary['estimated_market_opportunities']}"
    )
    for report in profile_reports[:3]:
        top = report["live"][0] if report["live"] else None
        if top:
            print(
                f"{report['profile']['profile_id']}: "
                f"{top['display_title']} ({top['source']}, score {top['score']})"
            )
    print(f"Wrote Markdown report to {OUTPUT_PATH}")


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
          co.source_category
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
          co.source_category
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


def rank_opportunities(profile, rows, group_canonical, limit, min_score=18, max_score=None):
    grouped = {}
    for row in rows:
        key = opportunity_key(row, group_canonical)
        scored = score_opportunity(profile, row)
        if scored["score"] < min_score:
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
    score = 0
    reasons = []

    for reason, keywords, points in profile["signals"]:
        if any(keyword_matches(text, keyword) for keyword in normalize_keywords(keywords)):
            score += points
            reasons.append(reason)

    for language in profile["languages"]:
        language_keywords = language_variants(language)
        if any(keyword_matches(text, keyword) for keyword in language_keywords):
            score += 6
            reasons.append(f"{language} language signal")

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

    return {
        "score": max(score, 0),
        "display_title": title,
        "source": row["source"],
        "source_slug": row["source_slug"],
        "location": row["location"] or "Unknown",
        "expertise": expertise,
        "url": row["url"],
        "opportunity_kind": row["opportunity_kind"],
        "availability_basis": row["availability_basis"],
        "include_in_live_market_estimate": row["include_in_live_market_estimate"],
        "is_canonical_representative": bool(row["canonical_opportunity_id"]),
        "reasons": unique(reasons)[:6],
    }


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


def normalize_text(value):
    return re.sub(r"\s+", " ", value.lower()).strip()


def normalize_keywords(keywords):
    return [normalize_text(keyword) for keyword in keywords]


def keyword_matches(text, keyword):
    if len(keyword) <= 3 and keyword.isalnum():
        return re.search(rf"\b{re.escape(keyword)}\b", text) is not None
    return keyword in text


def language_variants(language):
    variants = {
        "english": ["english"],
        "portuguese": ["portuguese", "brazilian"],
        "spanish": ["spanish", "espa\u00f1ol"],
        "french": ["french", "fran\u00e7ais"],
    }
    return variants.get(language.lower(), [language.lower()])


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


def render_markdown(generated_at, market_summary, profile_reports):
    lines = [
        "# Profile-Based AI Work Opportunity Match Digest",
        "",
        f"Generated: {generated_at.isoformat()} UTC",
        "",
        "## Prototype Notes",
        "",
        (
            "This read-only prototype uses built-in mock profiles and deterministic "
            "keyword scoring against current tracker data. It does not call external "
            "AI APIs, change database rows, or change live market estimate semantics."
        ),
        "",
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


if __name__ == "__main__":
    main()

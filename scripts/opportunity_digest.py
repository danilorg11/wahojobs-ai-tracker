import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wahojobs.classification import (
    MARKET_COUNT_POLICY_COUNT_LIVE,
    MARKET_COUNT_POLICY_EXCLUDE_LIVE_ESTIMATE,
    SOURCE_TIER_EXPERIMENTAL,
)
from wahojobs.db.connection import get_connection
from wahojobs.reporting.market import (
    CANONICALIZED_SLUGS,
    get_market_size_summary,
)
from wahojobs.tracking.service import (
    MINDRIFT_MIN_REMOVALS_FOR_GUARD,
    MINDRIFT_PARTIAL_DROP_THRESHOLD,
)


OUTPUT_PATH = Path("exports/opportunity_digest.md")
RECENT_DAYS = 7
NEW_LIMIT = 30
REMOVED_LIMIT = 20
EXAMPLE_LIMIT = 15
PER_SOURCE_EVENT_LIMIT = 8


def main():
    generated_at = datetime.now(timezone.utc).replace(microsecond=0)
    cutoff = (generated_at - timedelta(days=RECENT_DAYS)).isoformat()

    with get_connection() as conn:
        market_summary = get_market_size_summary(
            conn,
            include_experimental=False,
            include_simulation=False,
        )
        summary = get_digest_summary(conn, market_summary, cutoff)
        new_jobs = get_post_baseline_events(conn, cutoff, "discovered", NEW_LIMIT)
        removed_jobs = get_recent_events(conn, cutoff, "removed", REMOVED_LIMIT)
        evergreen = get_report_separately_examples(
            conn,
            "evergreen_application",
            EXAMPLE_LIMIT,
        )
        public_inventory = get_report_separately_examples(
            conn,
            "public_inventory",
            EXAMPLE_LIMIT,
        )
        live_by_source = get_live_by_source(conn)
        live_by_expertise = get_live_by_expertise(conn)
        live_by_location = get_live_by_job_dimension(conn, "location", 12)
        interesting = get_potentially_interesting_opportunities(conn)
        health = get_quality_notes(conn)

    markdown = render_markdown(
        generated_at,
        summary,
        new_jobs,
        removed_jobs,
        evergreen,
        public_inventory,
        live_by_source,
        live_by_expertise,
        live_by_location,
        interesting,
        health,
    )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(markdown, encoding="utf-8")

    print("")
    print("Wahojobs AI Work Opportunity Digest")
    print("===================================")
    print(f"Generated: {generated_at.isoformat()} UTC")
    print(
        "Estimated Live Market Opportunities: "
        f"{summary['estimated_live_market_opportunities']}"
    )
    print(f"Raw active live postings: {summary['raw_active_live_postings']}")
    print(
        f"Raw discovered rows in last {RECENT_DAYS} days: "
        f"{summary['raw_recent_discovered']}"
    )
    print(
        f"Post-baseline discovered rows in last {RECENT_DAYS} days: "
        f"{summary['post_baseline_discovered']}"
    )
    print(f"Removed rows in last {RECENT_DAYS} days: {summary['raw_recent_removed']}")
    print(f"Report-separately active rows: {summary['report_separately_total']}")
    print(f"Wrote Markdown report to {OUTPUT_PATH}")


def get_digest_summary(conn, market_summary, cutoff):
    public_inventory = count_active_by_inventory_model(conn, "public_inventory")
    evergreen = count_active_by_inventory_model(conn, "evergreen_application")
    mixed = count_active_by_inventory_model(conn, "mixed")
    experimental_excluded = count_experimental_or_excluded(conn)
    return {
        "estimated_live_market_opportunities": market_summary[
            "estimated_market_opportunities"
        ],
        "raw_active_live_postings": market_summary["raw_active_postings"],
        "raw_recent_discovered": count_recent_events(conn, cutoff, "discovered"),
        "raw_recent_removed": count_recent_events(conn, cutoff, "removed"),
        "raw_recent_reactivated": count_recent_events(conn, cutoff, "reactivated"),
        "post_baseline_discovered": count_post_baseline_events(
            conn,
            cutoff,
            "discovered",
        ),
        "post_baseline_removed": count_post_baseline_events(
            conn,
            cutoff,
            "removed",
        ),
        "post_baseline_reactivated": count_post_baseline_events(
            conn,
            cutoff,
            "reactivated",
        ),
        "baseline_discovered": count_baseline_discovered_events(conn, cutoff),
        "insufficient_history_sources": get_insufficient_history_sources(conn),
        "public_inventory": public_inventory,
        "evergreen": evergreen,
        "mixed": mixed,
        "experimental_excluded": experimental_excluded,
        "report_separately_total": public_inventory + evergreen + mixed,
    }


def count_active_by_inventory_model(conn, inventory_model):
    return scalar(
        conn,
        """
        SELECT COUNT(*)
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        WHERE j.is_active = 1
          AND j.title NOT LIKE '[SIMULATION]%'
          AND c.inventory_model = ?
        """,
        (inventory_model,),
    )


def count_experimental_or_excluded(conn):
    return scalar(
        conn,
        """
        SELECT COUNT(*)
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        WHERE j.is_active = 1
          AND j.title NOT LIKE '[SIMULATION]%'
          AND (
            c.source_tier = ?
            OR c.market_count_policy = ?
          )
        """,
        (SOURCE_TIER_EXPERIMENTAL, MARKET_COUNT_POLICY_EXCLUDE_LIVE_ESTIMATE),
    )


def count_recent_events(conn, cutoff, event_type):
    return scalar(
        conn,
        """
        SELECT COUNT(*)
        FROM job_events je
        JOIN jobs j ON j.id = je.job_id
        WHERE je.created_at >= ?
          AND je.event_type = ?
          AND j.title NOT LIKE '[SIMULATION]%'
        """,
        (cutoff, event_type),
    )


def count_post_baseline_events(conn, cutoff, event_type):
    return scalar(
        conn,
        f"""
        SELECT COUNT(*)
        FROM job_events je
        JOIN jobs j ON j.id = je.job_id
        JOIN companies c ON c.id = j.company_id
        JOIN ({baseline_crawl_sql()}) b ON b.company_id = c.id
        WHERE je.created_at >= ?
          AND je.event_type = ?
          AND je.crawl_run_id != b.baseline_crawl_run_id
          AND j.title NOT LIKE '[SIMULATION]%'
        """,
        (cutoff, event_type),
    )


def count_baseline_discovered_events(conn, cutoff):
    return scalar(
        conn,
        f"""
        SELECT COUNT(*)
        FROM job_events je
        JOIN jobs j ON j.id = je.job_id
        JOIN companies c ON c.id = j.company_id
        JOIN ({baseline_crawl_sql()}) b ON b.company_id = c.id
        WHERE je.created_at >= ?
          AND je.event_type = 'discovered'
          AND je.crawl_run_id = b.baseline_crawl_run_id
          AND j.title NOT LIKE '[SIMULATION]%'
        """,
        (cutoff,),
    )


def get_insufficient_history_sources(conn):
    return conn.execute(
        f"""
        SELECT c.name AS source, COUNT(j.id) AS active_rows
        FROM companies c
        LEFT JOIN jobs j ON j.company_id = c.id
          AND j.is_active = 1
          AND j.title NOT LIKE '[SIMULATION]%'
        LEFT JOIN ({baseline_crawl_sql()}) b ON b.company_id = c.id
        WHERE b.baseline_crawl_run_id IS NULL
        GROUP BY c.id, c.name
        HAVING active_rows > 0
        ORDER BY c.name ASC
        """
    ).fetchall()


def get_recent_events(conn, cutoff, event_type, limit):
    rows = conn.execute(
        """
        SELECT
          je.created_at,
          je.event_type,
          c.name AS source,
          c.slug AS source_slug,
          c.inventory_model,
          c.market_count_policy,
          j.title,
          j.location,
          COALESCE(NULLIF(TRIM(j.expertise), ''), NULLIF(TRIM(j.department), ''), 'Unknown') AS expertise_label,
          j.opportunity_kind,
          j.availability_basis,
          j.include_in_live_market_estimate,
          j.url
        FROM job_events je
        JOIN jobs j ON j.id = je.job_id
        JOIN companies c ON c.id = j.company_id
        WHERE je.created_at >= ?
          AND je.event_type = ?
          AND j.title NOT LIKE '[SIMULATION]%'
        ORDER BY je.created_at DESC, je.id DESC
        LIMIT ?
        """,
        (cutoff, event_type, max(limit * 5, limit)),
    ).fetchall()
    return cap_events_by_source(rows, limit)


def get_post_baseline_events(conn, cutoff, event_type, limit):
    rows = conn.execute(
        f"""
        SELECT
          je.created_at,
          je.event_type,
          c.name AS source,
          c.slug AS source_slug,
          c.inventory_model,
          c.market_count_policy,
          j.title,
          j.location,
          COALESCE(NULLIF(TRIM(j.expertise), ''), NULLIF(TRIM(j.department), ''), 'Unknown') AS expertise_label,
          j.opportunity_kind,
          j.availability_basis,
          j.include_in_live_market_estimate,
          j.url
        FROM job_events je
        JOIN jobs j ON j.id = je.job_id
        JOIN companies c ON c.id = j.company_id
        JOIN ({baseline_crawl_sql()}) b ON b.company_id = c.id
        WHERE je.created_at >= ?
          AND je.event_type = ?
          AND je.crawl_run_id != b.baseline_crawl_run_id
          AND j.title NOT LIKE '[SIMULATION]%'
        ORDER BY je.created_at DESC, je.id DESC
        LIMIT ?
        """,
        (cutoff, event_type, max(limit * 5, limit)),
    ).fetchall()
    return cap_events_by_source(rows, limit)


def cap_events_by_source(rows, limit):
    selected = []
    source_counts = {}
    for row in rows:
        source = row["source"]
        if source_counts.get(source, 0) >= PER_SOURCE_EVENT_LIMIT:
            continue
        selected.append(row)
        source_counts[source] = source_counts.get(source, 0) + 1
        if len(selected) >= limit:
            break
    return selected


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


def get_report_separately_examples(conn, inventory_model, limit):
    return conn.execute(
        """
        SELECT
          c.name AS source,
          c.inventory_model,
          c.market_count_policy,
          j.title,
          j.location,
          COALESCE(NULLIF(TRIM(j.expertise), ''), NULLIF(TRIM(j.department), ''), 'Unknown') AS expertise_label,
          j.opportunity_kind,
          j.availability_basis,
          j.url
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        WHERE j.is_active = 1
          AND j.title NOT LIKE '[SIMULATION]%'
          AND c.inventory_model = ?
          AND c.market_count_policy != ?
        ORDER BY c.name ASC, j.title ASC
        LIMIT ?
        """,
        (inventory_model, MARKET_COUNT_POLICY_COUNT_LIVE, limit),
    ).fetchall()


def get_live_by_source(conn):
    rows = []
    for company in conn.execute(
        """
        SELECT id, name, slug
        FROM companies
        WHERE market_count_policy = ?
        ORDER BY name ASC
        """,
        (MARKET_COUNT_POLICY_COUNT_LIVE,),
    ):
        if company["slug"] in CANONICALIZED_SLUGS:
            count = count_source_canonical_live(conn, company["id"])
        else:
            count = count_source_raw_live(conn, company["id"])
        if count:
            rows.append({"label": company["name"], "count": count})
    return sorted(rows, key=lambda row: (-row["count"], row["label"]))[:15]


def count_source_canonical_live(conn, company_id):
    return scalar(
        conn,
        """
        SELECT COUNT(*)
        FROM canonical_opportunities co
        WHERE co.company_id = ?
          AND co.is_active = 1
          AND EXISTS (
            SELECT 1
            FROM jobs j
            WHERE j.canonical_opportunity_id = co.id
              AND j.is_active = 1
              AND j.include_in_live_market_estimate = 1
              AND j.title NOT LIKE '[SIMULATION]%'
          )
        """,
        (company_id,),
    )


def count_source_raw_live(conn, company_id):
    return scalar(
        conn,
        """
        SELECT COUNT(*)
        FROM jobs
        WHERE company_id = ?
          AND is_active = 1
          AND include_in_live_market_estimate = 1
          AND title NOT LIKE '[SIMULATION]%'
        """,
        (company_id,),
    )


def get_live_by_expertise(conn):
    rows = []
    rows.extend(
        conn.execute(
            f"""
            SELECT COALESCE(NULLIF(TRIM(co.source_category), ''), 'Unknown') AS label,
                   COUNT(DISTINCT co.id) AS count
            FROM canonical_opportunities co
            JOIN companies c ON c.id = co.company_id
            JOIN jobs j ON j.canonical_opportunity_id = co.id
            WHERE c.slug IN ({quote_list(CANONICALIZED_SLUGS)})
              AND c.market_count_policy = ?
              AND co.is_active = 1
              AND j.is_active = 1
              AND j.include_in_live_market_estimate = 1
              AND j.title NOT LIKE '[SIMULATION]%'
            GROUP BY label
            """,
            (MARKET_COUNT_POLICY_COUNT_LIVE,),
        ).fetchall()
    )
    rows.extend(
        conn.execute(
            f"""
            SELECT
              COALESCE(NULLIF(TRIM(j.expertise), ''), NULLIF(TRIM(j.department), ''), 'Unknown') AS label,
              COUNT(*) AS count
            FROM jobs j
            JOIN companies c ON c.id = j.company_id
            WHERE c.slug NOT IN ({quote_list(CANONICALIZED_SLUGS)})
              AND c.market_count_policy = ?
              AND j.is_active = 1
              AND j.include_in_live_market_estimate = 1
              AND j.title NOT LIKE '[SIMULATION]%'
            GROUP BY label
            """,
            (MARKET_COUNT_POLICY_COUNT_LIVE,),
        ).fetchall()
    )
    return combine_count_rows(rows, 15)


def get_live_by_job_dimension(conn, field, limit):
    rows = []
    rows.extend(
        conn.execute(
            f"""
            SELECT COALESCE(NULLIF(TRIM(j.{field}), ''), 'Unknown') AS label,
                   COUNT(DISTINCT co.id) AS count
            FROM canonical_opportunities co
            JOIN companies c ON c.id = co.company_id
            JOIN jobs j ON j.canonical_opportunity_id = co.id
            WHERE c.slug IN ({quote_list(CANONICALIZED_SLUGS)})
              AND c.market_count_policy = ?
              AND co.is_active = 1
              AND j.is_active = 1
              AND j.include_in_live_market_estimate = 1
              AND j.title NOT LIKE '[SIMULATION]%'
            GROUP BY label
            """,
            (MARKET_COUNT_POLICY_COUNT_LIVE,),
        ).fetchall()
    )
    rows.extend(
        conn.execute(
            f"""
            SELECT COALESCE(NULLIF(TRIM(j.{field}), ''), 'Unknown') AS label,
                   COUNT(*) AS count
            FROM jobs j
            JOIN companies c ON c.id = j.company_id
            WHERE c.slug NOT IN ({quote_list(CANONICALIZED_SLUGS)})
              AND c.market_count_policy = ?
              AND j.is_active = 1
              AND j.include_in_live_market_estimate = 1
              AND j.title NOT LIKE '[SIMULATION]%'
            GROUP BY label
            """,
            (MARKET_COUNT_POLICY_COUNT_LIVE,),
        ).fetchall()
    )
    return combine_count_rows(rows, limit)


def get_potentially_interesting_opportunities(conn):
    return conn.execute(
        """
        SELECT
          c.name AS source,
          j.title,
          j.location,
          COALESCE(NULLIF(TRIM(j.expertise), ''), NULLIF(TRIM(j.department), ''), 'Unknown') AS expertise_label,
          j.opportunity_kind,
          j.availability_basis,
          j.url,
          CASE
            WHEN LOWER(COALESCE(j.location, '') || ' ' || COALESCE(j.commitment, '')) LIKE '%remote%' THEN 'remote/flexible signal'
            WHEN LOWER(COALESCE(j.title, '') || ' ' || COALESCE(j.expertise, '') || ' ' || COALESCE(j.department, '')) LIKE '%language%' THEN 'language-related work'
            WHEN LOWER(COALESCE(j.title, '') || ' ' || COALESCE(j.expertise, '') || ' ' || COALESCE(j.department, '')) LIKE '%annotation%' THEN 'annotation/evaluation work'
            WHEN LOWER(COALESCE(j.title, '') || ' ' || COALESCE(j.expertise, '') || ' ' || COALESCE(j.department, '')) LIKE '%evaluation%' THEN 'AI evaluation work'
            WHEN LOWER(COALESCE(j.title, '') || ' ' || COALESCE(j.expertise, '') || ' ' || COALESCE(j.department, '')) LIKE '%trainer%' THEN 'AI training work'
            WHEN LOWER(COALESCE(j.title, '') || ' ' || COALESCE(j.expertise, '') || ' ' || COALESCE(j.department, '')) LIKE '%expert%' THEN 'domain expert work'
            WHEN LOWER(COALESCE(j.expertise, '') || ' ' || COALESCE(j.department, '')) LIKE '%finance%' THEN 'domain expert work'
            WHEN LOWER(COALESCE(j.expertise, '') || ' ' || COALESCE(j.department, '')) LIKE '%legal%' THEN 'domain expert work'
            WHEN LOWER(COALESCE(j.expertise, '') || ' ' || COALESCE(j.department, '')) LIKE '%coding%' THEN 'coding/software evaluation work'
            ELSE 'worth reviewing'
          END AS reason
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        WHERE j.is_active = 1
          AND c.market_count_policy = ?
          AND j.include_in_live_market_estimate = 1
          AND j.title NOT LIKE '[SIMULATION]%'
          AND (
            LOWER(COALESCE(j.location, '') || ' ' || COALESCE(j.commitment, '')) LIKE '%remote%'
            OR LOWER(COALESCE(j.title, '') || ' ' || COALESCE(j.expertise, '') || ' ' || COALESCE(j.department, '')) LIKE '%language%'
            OR LOWER(COALESCE(j.title, '') || ' ' || COALESCE(j.expertise, '') || ' ' || COALESCE(j.department, '')) LIKE '%annotation%'
            OR LOWER(COALESCE(j.title, '') || ' ' || COALESCE(j.expertise, '') || ' ' || COALESCE(j.department, '')) LIKE '%evaluation%'
            OR LOWER(COALESCE(j.title, '') || ' ' || COALESCE(j.expertise, '') || ' ' || COALESCE(j.department, '')) LIKE '%trainer%'
            OR LOWER(COALESCE(j.title, '') || ' ' || COALESCE(j.expertise, '') || ' ' || COALESCE(j.department, '')) LIKE '%expert%'
            OR LOWER(COALESCE(j.expertise, '') || ' ' || COALESCE(j.department, '')) LIKE '%finance%'
            OR LOWER(COALESCE(j.expertise, '') || ' ' || COALESCE(j.department, '')) LIKE '%legal%'
            OR LOWER(COALESCE(j.expertise, '') || ' ' || COALESCE(j.department, '')) LIKE '%coding%'
          )
        ORDER BY
          CASE WHEN LOWER(COALESCE(j.location, '') || ' ' || COALESCE(j.commitment, '')) LIKE '%remote%' THEN 0 ELSE 1 END,
          j.last_seen_at DESC,
          c.name ASC,
          j.title ASC
        LIMIT 20
        """,
        (MARKET_COUNT_POLICY_COUNT_LIVE,),
    ).fetchall()


def get_quality_notes(conn):
    return {
        "largest_reductions": get_largest_canonical_reductions(conn),
        "unlinked": get_unlinked_canonical_rows(conn),
        "unknown_live": get_unknown_taxonomy_live(conn),
        "unknown_report_separately": get_unknown_taxonomy_report_separately(conn),
        "oneforma_duplicate_urls": get_oneforma_duplicate_url_context(conn),
        "encoding_artifacts": count_encoding_artifacts(conn),
        "excluded_sources": get_excluded_sources(conn),
    }


def get_largest_canonical_reductions(conn):
    rows = []
    for company in conn.execute(
        """
        SELECT id, name, slug
        FROM companies
        WHERE slug IN ({})
        ORDER BY name ASC
        """.format(quote_list(CANONICALIZED_SLUGS))
    ):
        raw_active = count_source_raw_active(conn, company["id"])
        canonical = count_source_canonical_live(conn, company["id"])
        reduction = max(raw_active - canonical, 0)
        if reduction:
            rows.append(
                {
                    "source": company["name"],
                    "raw_active": raw_active,
                    "canonical": canonical,
                    "reduction": reduction,
                }
            )
    return sorted(rows, key=lambda row: (-row["reduction"], row["source"]))[:6]


def count_source_raw_active(conn, company_id):
    return scalar(
        conn,
        """
        SELECT COUNT(*)
        FROM jobs
        WHERE company_id = ?
          AND is_active = 1
          AND title NOT LIKE '[SIMULATION]%'
        """,
        (company_id,),
    )


def get_unlinked_canonical_rows(conn):
    return conn.execute(
        f"""
        SELECT c.name AS source, COUNT(*) AS count
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        WHERE c.slug IN ({quote_list(CANONICALIZED_SLUGS)})
          AND j.is_active = 1
          AND j.canonical_opportunity_id IS NULL
          AND j.title NOT LIKE '[SIMULATION]%'
        GROUP BY c.id, c.name
        HAVING count > 0
        ORDER BY count DESC, c.name ASC
        """
    ).fetchall()


def get_unknown_taxonomy_live(conn):
    return conn.execute(
        """
        SELECT c.name AS source, COUNT(*) AS count
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        WHERE j.is_active = 1
          AND c.market_count_policy = ?
          AND j.include_in_live_market_estimate = 1
          AND j.title NOT LIKE '[SIMULATION]%'
          AND COALESCE(NULLIF(TRIM(j.expertise), ''), NULLIF(TRIM(j.department), ''), 'Unknown') = 'Unknown'
        GROUP BY c.id, c.name
        HAVING count > 0
        ORDER BY count DESC, c.name ASC
        """,
        (MARKET_COUNT_POLICY_COUNT_LIVE,),
    ).fetchall()


def get_unknown_taxonomy_report_separately(conn):
    return conn.execute(
        """
        SELECT c.name AS source, c.inventory_model, c.market_count_policy, COUNT(*) AS count
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        WHERE j.is_active = 1
          AND c.market_count_policy != ?
          AND j.title NOT LIKE '[SIMULATION]%'
          AND COALESCE(NULLIF(TRIM(j.expertise), ''), NULLIF(TRIM(j.department), ''), 'Unknown') = 'Unknown'
        GROUP BY c.id, c.name
        HAVING count > 0
        ORDER BY count DESC, c.name ASC
        """,
        (MARKET_COUNT_POLICY_COUNT_LIVE,),
    ).fetchall()


def get_oneforma_duplicate_url_context(conn):
    return conn.execute(
        """
        SELECT COUNT(*) AS groups, COALESCE(SUM(count - 1), 0) AS extras
        FROM (
          SELECT j.url, COUNT(*) AS count
          FROM jobs j
          JOIN companies c ON c.id = j.company_id
          WHERE c.slug = 'oneforma'
            AND j.is_active = 1
            AND j.url IS NOT NULL
            AND TRIM(j.url) != ''
            AND j.title NOT LIKE '[SIMULATION]%'
          GROUP BY j.url
          HAVING COUNT(*) > 1
        )
        """
    ).fetchone()


def get_excluded_sources(conn):
    return conn.execute(
        """
        SELECT c.name AS source, c.source_tier, c.inventory_model, c.market_count_policy, COUNT(j.id) AS count
        FROM companies c
        LEFT JOIN jobs j ON j.company_id = c.id
          AND j.is_active = 1
          AND j.title NOT LIKE '[SIMULATION]%'
        WHERE c.market_count_policy != ?
           OR c.source_tier = ?
        GROUP BY c.id, c.name
        ORDER BY c.name ASC
        """,
        (MARKET_COUNT_POLICY_COUNT_LIVE, SOURCE_TIER_EXPERIMENTAL),
    ).fetchall()


def count_encoding_artifacts(conn):
    count = 0
    fields = ("title", "department", "expertise")
    rows = conn.execute(
        """
        SELECT title, department, expertise
        FROM jobs
        WHERE is_active = 1
          AND title NOT LIKE '[SIMULATION]%'
        """
    ).fetchall()
    for row in rows:
        count += sum(1 for field in fields if has_encoding_artifact(row[field]))

    canonical_rows = conn.execute(
        """
        SELECT canonical_title, source_category
        FROM canonical_opportunities
        WHERE is_active = 1
        """
    ).fetchall()
    for row in canonical_rows:
        count += sum(
            1
            for field in ("canonical_title", "source_category")
            if has_encoding_artifact(row[field])
        )
    return count


def has_encoding_artifact(value):
    if not value:
        return False
    patterns = (
        "\ufffd",
        "Ã¢â‚¬â„¢",
        "Ã¢â‚¬Å“",
        "Ã¢â‚¬ï¿½",
        "Ã¢â‚¬â€œ",
        "Ã¢â‚¬â€",
        "Ãƒ",
        "Ã‚",
    )
    if any(pattern in value for pattern in patterns):
        return True
    return bool(re.search(r"[\u0080-\u009f]", value))


def render_markdown(
    generated_at,
    summary,
    new_jobs,
    removed_jobs,
    evergreen,
    public_inventory,
    live_by_source,
    live_by_expertise,
    live_by_location,
    interesting,
    health,
):
    lines = [
        "# AI Work Opportunity Digest",
        "",
        f"Generated: {generated_at.isoformat()} UTC",
        "",
        "## Digest Summary",
        "",
        f"- Estimated Live Market Opportunities: **{summary['estimated_live_market_opportunities']}**",
        f"- Raw active live postings: **{summary['raw_active_live_postings']}**",
        f"- Raw discovered rows in the last {RECENT_DAYS} days: **{summary['raw_recent_discovered']}**",
        f"- Raw removed rows in the last {RECENT_DAYS} days: **{summary['raw_recent_removed']}**",
        f"- Raw reactivated rows in the last {RECENT_DAYS} days: **{summary['raw_recent_reactivated']}**",
        f"- Post-baseline discovered rows in the last {RECENT_DAYS} days: **{summary['post_baseline_discovered']}**",
        f"- Post-baseline removed rows in the last {RECENT_DAYS} days: **{summary['post_baseline_removed']}**",
        f"- Post-baseline reactivated rows in the last {RECENT_DAYS} days: **{summary['post_baseline_reactivated']}**",
        f"- Baseline/backfill discovered rows in the last {RECENT_DAYS} days: **{summary['baseline_discovered']}**",
        f"- Public inventory opportunities: **{summary['public_inventory']}**",
        f"- Evergreen application opportunities: **{summary['evergreen']}**",
        f"- Mixed/report-separately opportunities: **{summary['mixed']}**",
        f"- Experimental/excluded opportunities: **{summary['experimental_excluded']}**",
        "",
        (
            "Evergreen applications and public inventory records can be useful "
            "job-seeker targets, but they are reported separately and do not "
            "contribute to the Estimated Live Market Opportunities number."
        ),
        "",
        (
            "Raw tracker lifecycle events can include source onboarding, initial "
            "backfills, parser changes, or source reprocessing. The New "
            "Opportunities section below prioritizes post-baseline discoveries."
        ),
        "",
    ]

    append_new_opportunities_section(lines, new_jobs, summary)
    append_event_section(lines, "Recently Removed Opportunities", removed_jobs)

    lines.extend(["## Evergreen / Always-Open Applications", ""])
    lines.append(
        "These are useful application targets, but they are not live postings and do not affect the live market estimate."
    )
    lines.append("")
    append_opportunity_table(lines, evergreen)

    lines.extend(["## Public Inventory Opportunities", ""])
    lines.append(
        "These are public opportunity records, currently reported separately from the conservative live estimate."
    )
    lines.append("")
    append_opportunity_table(lines, public_inventory)

    lines.extend(["## Top Active Live Opportunity Areas", ""])
    append_count_table(lines, "By Source", live_by_source)
    append_count_table(lines, "By Expertise/Department", live_by_expertise)
    append_count_table(lines, "By Location Signal", live_by_location)
    lines.append(
        "Composition tables use canonical opportunities where available. Location rows are directional signals and may not add to the headline estimate."
    )
    lines.append("")

    lines.extend(["## Potentially Interesting Opportunities", ""])
    lines.append(
        "These are heuristic examples worth reviewing; the tracker does not infer eligibility or guarantee fit."
    )
    lines.append("")
    append_interesting_table(lines, interesting)

    append_quality_notes(lines, health)
    return "\n".join(lines)


def append_event_section(lines, title, rows):
    lines.extend([f"## {title}", ""])
    if title.startswith("Recently Removed"):
        lines.append(
            "These are tracker lifecycle events. Treat them as a signal to review, not guaranteed proof of market movement."
        )
        lines.append("")
    if not rows:
        lines.extend(["No recent rows found.", ""])
        return

    current_source = None
    for row in rows:
        if row["source"] != current_source:
            if current_source is not None:
                lines.append("")
            current_source = row["source"]
            lines.append(f"### {escape(current_source)}")
            lines.append("")
        lines.append(f"- **{escape(row['title'])}**")
        lines.append(
            f"  - {escape(row['location'] or 'Unknown location')} | "
            f"{escape(row['expertise_label'])} | "
            f"{escape(row['opportunity_kind'])} / {escape(row['availability_basis'])}"
        )
        lines.append(
            f"  - Live estimate: {yes_no(row['include_in_live_market_estimate'])} | "
            f"[Open]({row['url']})"
        )
    lines.append("")


def append_new_opportunities_section(lines, rows, summary):
    lines.extend(["## New Opportunities", ""])
    lines.append(
        "This section uses post-baseline discovered events, excluding each source's first successful crawl/backfill."
    )
    if summary["insufficient_history_sources"]:
        sources = ", ".join(
            f"{row['source']} ({row['active_rows']})"
            for row in summary["insufficient_history_sources"]
        )
        lines.append(
            f"Sources with active rows but no successful baseline crawl are labeled insufficient history and excluded from post-baseline movement: {sources}."
        )
    lines.append("")

    if not rows:
        lines.extend(
            [
                f"No post-baseline newly discovered rows found in the last {RECENT_DAYS} days.",
                "If raw discovery counts are high, they are likely dominated by baseline/backfill activity.",
                "",
            ]
        )
        return

    current_source = None
    for row in rows:
        if row["source"] != current_source:
            if current_source is not None:
                lines.append("")
            current_source = row["source"]
            lines.append(f"### {escape(current_source)}")
            lines.append("")
        lines.append(f"- **{escape(row['title'])}**")
        lines.append(
            f"  - {escape(row['location'] or 'Unknown location')} | "
            f"{escape(row['expertise_label'])} | "
            f"{escape(row['opportunity_kind'])} / {escape(row['availability_basis'])}"
        )
        lines.append(
            f"  - Live estimate: {yes_no(row['include_in_live_market_estimate'])} | "
            f"[Open]({row['url']})"
        )
    lines.append("")


def append_opportunity_table(lines, rows):
    if not rows:
        lines.extend(["None.", ""])
        return
    lines.extend(
        [
            "| Source | Title | Location | Expertise/Department | Kind | URL |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in rows:
        lines.append(
            "| "
            f"{escape(row['source'])} | "
            f"{escape(row['title'])} | "
            f"{escape(row['location'] or 'Unknown')} | "
            f"{escape(row['expertise_label'])} | "
            f"{escape(row['opportunity_kind'])} / {escape(row['availability_basis'])} | "
            f"[Open]({row['url']}) |"
        )
    lines.append("")


def append_interesting_table(lines, rows):
    if not rows:
        lines.extend(["No heuristic examples found.", ""])
        return
    lines.extend(
        [
            "| Why | Source | Title | Location | Expertise/Department | URL |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in rows:
        lines.append(
            "| "
            f"{escape(row['reason'])} | "
            f"{escape(row['source'])} | "
            f"{escape(row['title'])} | "
            f"{escape(row['location'] or 'Unknown')} | "
            f"{escape(row['expertise_label'])} | "
            f"[Open]({row['url']}) |"
        )
    lines.append("")


def append_quality_notes(lines, health):
    lines.extend(["## Watch / Quality Notes", ""])

    lines.append("### Sources Excluded From Live Estimate")
    lines.append("")
    if not health["excluded_sources"]:
        lines.append("None.")
    else:
        for row in health["excluded_sources"]:
            lines.append(
                f"- {row['source']}: {row['count']} active rows "
                f"({row['source_tier']} / {row['inventory_model']} / "
                f"{row['market_count_policy']})."
            )
    lines.append("")

    lines.append("### Canonicalization Health")
    lines.append("")
    if health["largest_reductions"]:
        for row in health["largest_reductions"]:
            lines.append(
                f"- {row['source']}: {row['raw_active']} raw rows -> "
                f"{row['canonical']} canonical opportunities "
                f"({row['reduction']} variants collapsed)."
            )
    else:
        lines.append("No raw-to-canonical reductions found.")
    lines.append("")

    append_count_table(lines, "Unlinked Active Rows in Canonicalized Sources", health["unlinked"])
    append_count_table(lines, "Unknown Taxonomy Affecting Live Estimate", health["unknown_live"])

    lines.append("### Report-Separately Unknown Taxonomy")
    lines.append("")
    if not health["unknown_report_separately"]:
        lines.append("None.")
    else:
        for row in health["unknown_report_separately"]:
            lines.append(
                f"- {row['source']}: {row['count']} rows "
                f"({row['inventory_model']} / {row['market_count_policy']}); "
                "does not affect the live estimate."
            )
    lines.append("")

    lines.append("### Source-Specific Notes")
    lines.append("")
    lines.append(
        f"- Mindrift lifecycle guard is active: suspicious crawls with more than "
        f"{MINDRIFT_PARTIAL_DROP_THRESHOLD:.0%} drop and at least "
        f"{MINDRIFT_MIN_REMOVALS_FOR_GUARD} implied removals fail before removals are applied."
    )
    oneforma = health["oneforma_duplicate_urls"]
    lines.append(
        f"- OneForma has {oneforma['extras']} duplicate URL extras across "
        f"{oneforma['groups']} groups; this is expected application-variant URL reuse and does not change its canonical live contribution."
    )
    if health["encoding_artifacts"]:
        lines.append(f"- Encoding diagnostics found {health['encoding_artifacts']} possible artifacts.")
    else:
        lines.append("- Encoding diagnostics found no stored text artifacts.")
    lines.append("")


def append_count_table(lines, title, rows):
    lines.extend([f"### {title}", ""])
    if not rows:
        lines.extend(["None.", ""])
        return
    lines.extend(["| Label | Count |", "| --- | ---: |"])
    for row in rows:
        label = row["label"] if "label" in row.keys() else row[0]
        count = row["count"] if "count" in row.keys() else row[1]
        lines.append(f"| {escape(label)} | {count} |")
    lines.append("")


def combine_count_rows(rows, limit):
    counts = {}
    for row in rows:
        label = row["label"] or "Unknown"
        counts[label] = counts.get(label, 0) + row["count"]
    return [
        {"label": label, "count": count}
        for label, count in sorted(
            counts.items(),
            key=lambda item: (-item[1], item[0]),
        )[:limit]
    ]


def quote_list(values):
    return ",".join(f"'{value}'" for value in values)


def scalar(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()[0]


def yes_no(value):
    return "yes" if value else "no"


def escape(value):
    return str(value or "").replace("|", "\\|").replace("\n", " ")


if __name__ == "__main__":
    main()

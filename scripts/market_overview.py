import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wahojobs.classification import (
    MARKET_COUNT_POLICY_COUNT_LIVE,
    MARKET_COUNT_POLICY_EXCLUDE_LIVE_ESTIMATE,
    MARKET_COUNT_POLICY_REPORT_SEPARATELY,
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


OUTPUT_PATH = Path("exports/market_overview.md")
RECENT_DAYS = 7


def main():
    generated_at = datetime.now(timezone.utc).replace(microsecond=0)
    cutoff = (generated_at - timedelta(days=RECENT_DAYS)).isoformat()

    with get_connection() as conn:
        market_summary = get_market_size_summary(
            conn,
            include_experimental=False,
            include_simulation=False,
        )
        executive = get_executive_summary(conn, market_summary)
        source_breakdown = get_source_breakdown(conn)
        live_by_source = get_live_by_source(source_breakdown)
        live_by_expertise = get_live_by_expertise(conn)
        live_by_location = get_live_by_job_dimension(conn, "location")
        live_by_commitment = get_live_by_job_dimension(conn, "commitment")
        report_separately = get_report_separately_sources(source_breakdown)
        recent = get_recent_movement(conn, cutoff)
        health = get_source_health(conn)

    markdown = render_markdown(
        generated_at,
        executive,
        source_breakdown,
        live_by_source,
        live_by_expertise,
        live_by_location,
        live_by_commitment,
        report_separately,
        recent,
        health,
    )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(markdown, encoding="utf-8")

    print("")
    print("Wahojobs AI Work Market Overview")
    print("================================")
    print(f"Generated: {generated_at.isoformat()} UTC")
    print(f"Estimated Live Market Opportunities: {executive['estimated_live']}")
    print(f"Raw active live postings: {executive['raw_live']}")
    print(f"Total observable opportunity rows: {executive['total_observable_rows']}")
    print(f"Public inventory opportunities: {executive['public_inventory']}")
    print(f"Evergreen application opportunities: {executive['evergreen']}")
    print(f"Mixed/report-separately opportunities: {executive['mixed']}")
    print(f"Experimental/excluded opportunities: {executive['experimental_excluded']}")
    print(f"Wrote Markdown report to {OUTPUT_PATH}")


def get_executive_summary(conn, market_summary):
    return {
        "estimated_live": market_summary["estimated_market_opportunities"],
        "raw_live": market_summary["raw_active_postings"],
        "public_inventory": count_active_source_model(conn, "public_inventory"),
        "evergreen": count_active_source_model(conn, "evergreen_application"),
        "mixed": count_active_source_model(conn, "mixed"),
        "experimental_excluded": count_experimental_or_excluded(conn),
        "total_observable_rows": count_all_active_rows(conn),
    }


def count_active_source_model(conn, inventory_model):
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


def count_all_active_rows(conn):
    return scalar(
        conn,
        """
        SELECT COUNT(*)
        FROM jobs
        WHERE is_active = 1
          AND title NOT LIKE '[SIMULATION]%'
        """,
    )


def get_source_breakdown(conn):
    companies = conn.execute(
        """
        SELECT id, name, slug, source_tier, inventory_model, market_count_policy
        FROM companies
        ORDER BY name ASC
        """
    ).fetchall()

    rows = []
    for company in companies:
        raw_active = count_source_active_rows(conn, company["id"])
        canonicalized = company["slug"] in CANONICALIZED_SLUGS
        canonical_opportunities = (
            count_source_canonical_opportunities(conn, company["id"])
            if canonicalized
            else None
        )
        estimated_live = estimate_source_live_contribution(
            conn,
            company["id"],
            canonicalized,
            company["market_count_policy"],
        )
        rows.append(
            {
                "name": company["name"],
                "slug": company["slug"],
                "source_tier": company["source_tier"],
                "inventory_model": company["inventory_model"],
                "market_count_policy": company["market_count_policy"],
                "raw_active": raw_active,
                "canonical_opportunities": canonical_opportunities,
                "estimated_live": estimated_live,
                "report_separately": (
                    raw_active
                    if company["market_count_policy"] == MARKET_COUNT_POLICY_REPORT_SEPARATELY
                    else 0
                ),
                "experimental_excluded": (
                    raw_active
                    if company["source_tier"] == SOURCE_TIER_EXPERIMENTAL
                    or company["market_count_policy"] == MARKET_COUNT_POLICY_EXCLUDE_LIVE_ESTIMATE
                    else 0
                ),
                "variant_reduction": (
                    max(raw_active - canonical_opportunities, 0)
                    if canonical_opportunities is not None
                    else 0
                ),
            }
        )
    return rows


def count_source_active_rows(conn, company_id):
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


def count_source_canonical_opportunities(conn, company_id):
    return scalar(
        conn,
        """
        SELECT COUNT(*)
        FROM canonical_opportunities
        WHERE company_id = ?
          AND is_active = 1
        """,
        (company_id,),
    )


def estimate_source_live_contribution(conn, company_id, canonicalized, policy):
    if policy != MARKET_COUNT_POLICY_COUNT_LIVE:
        return 0

    if canonicalized:
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


def get_live_by_source(source_breakdown):
    return [
        {"label": row["name"], "count": row["estimated_live"]}
        for row in sorted(
            source_breakdown,
            key=lambda row: (-row["estimated_live"], row["name"]),
        )
        if row["estimated_live"] > 0
    ]


def get_live_by_expertise(conn):
    rows = []
    rows.extend(
        conn.execute(
            f"""
            SELECT co.source_category AS label, COUNT(DISTINCT co.id) AS count
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
    return combine_count_rows(rows, limit=20)


def get_live_by_job_dimension(conn, field):
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
    return combine_count_rows(rows, limit=15)


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


def get_report_separately_sources(source_breakdown):
    return [
        row for row in source_breakdown
        if row["market_count_policy"] != MARKET_COUNT_POLICY_COUNT_LIVE
    ]


def get_recent_movement(conn, cutoff):
    return {
        "event_counts": get_recent_event_counts(conn, cutoff),
        "source_movements": get_recent_source_movements(conn, cutoff),
        "notable_crawl_runs": get_notable_crawl_runs(conn, cutoff),
    }


def get_recent_event_counts(conn, cutoff):
    return conn.execute(
        """
        SELECT je.event_type AS label, COUNT(*) AS count
        FROM job_events je
        JOIN jobs j ON j.id = je.job_id
        WHERE je.created_at >= ?
          AND j.title NOT LIKE '[SIMULATION]%'
        GROUP BY je.event_type
        ORDER BY count DESC, label ASC
        """,
        (cutoff,),
    ).fetchall()


def get_recent_source_movements(conn, cutoff):
    return conn.execute(
        """
        SELECT
          c.name AS source,
          SUM(CASE WHEN je.event_type = 'discovered' THEN 1 ELSE 0 END) AS discovered,
          SUM(CASE WHEN je.event_type = 'removed' THEN 1 ELSE 0 END) AS removed,
          SUM(CASE WHEN je.event_type = 'reactivated' THEN 1 ELSE 0 END) AS reactivated
        FROM job_events je
        JOIN jobs j ON j.id = je.job_id
        JOIN companies c ON c.id = j.company_id
        WHERE je.created_at >= ?
          AND j.title NOT LIKE '[SIMULATION]%'
        GROUP BY c.id, c.name
        HAVING discovered > 0 OR removed > 0 OR reactivated > 0
        ORDER BY (discovered + removed + reactivated) DESC, c.name ASC
        LIMIT 12
        """,
        (cutoff,),
    ).fetchall()


def get_notable_crawl_runs(conn, cutoff):
    return conn.execute(
        """
        SELECT
          c.name AS source,
          cr.started_at,
          cr.status,
          cr.jobs_found_count,
          cr.jobs_new_count,
          cr.jobs_removed_count,
          cr.error_message
        FROM crawl_runs cr
        JOIN companies c ON c.id = cr.company_id
        WHERE cr.started_at >= ?
          AND (
            cr.status = 'failed'
            OR cr.jobs_removed_count >= 50
            OR cr.jobs_new_count >= 50
          )
        ORDER BY cr.started_at DESC
        LIMIT 12
        """,
        (cutoff,),
    ).fetchall()


def get_source_health(conn):
    source_rows = get_source_breakdown(conn)
    reductions = [
        row for row in source_rows
        if row["variant_reduction"] > 0
    ]
    reductions.sort(key=lambda row: (-row["variant_reduction"], row["name"]))
    return {
        "largest_reductions": reductions[:8],
        "unlinked": get_unlinked_canonical_rows(conn),
        "unknown_live": get_unknown_taxonomy_live(conn),
        "unknown_report_separately": get_unknown_taxonomy_report_separately(conn),
        "oneforma_duplicate_urls": get_oneforma_duplicate_url_context(conn),
        "encoding_artifacts": count_encoding_artifacts(conn),
    }


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
    row = conn.execute(
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
    watch = conn.execute(
        """
        SELECT
          j.url,
          COUNT(*) AS count,
          COUNT(DISTINCT COALESCE(j.canonical_opportunity_id, -1)) AS canonical_groups,
          GROUP_CONCAT(DISTINCT co.canonical_title) AS canonical_titles
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        LEFT JOIN canonical_opportunities co ON co.id = j.canonical_opportunity_id
        WHERE c.slug = 'oneforma'
          AND j.is_active = 1
          AND j.url IS NOT NULL
          AND TRIM(j.url) != ''
          AND j.title NOT LIKE '[SIMULATION]%'
        GROUP BY j.url
        HAVING COUNT(*) > 1 AND canonical_groups > 1
        ORDER BY j.url ASC
        """
    ).fetchall()
    return {
        "groups": row["groups"],
        "extras": row["extras"],
        "watch": watch,
    }


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
            1 for field in ("canonical_title", "source_category")
            if has_encoding_artifact(row[field])
        )
    return count


def has_encoding_artifact(value):
    if not value:
        return False
    patterns = ("\ufffd", "â€™", "â€œ", "â€�", "â€“", "â€”", "Ã", "Â")
    if any(pattern in value for pattern in patterns):
        return True
    return bool(re.search(r"[\u0080-\u009f]", value))


def render_markdown(
    generated_at,
    executive,
    source_breakdown,
    live_by_source,
    live_by_expertise,
    live_by_location,
    live_by_commitment,
    report_separately,
    recent,
    health,
):
    lines = [
        "# AI Work Market Overview",
        "",
        f"Generated: {generated_at.isoformat()} UTC",
        "",
        "## Executive Summary",
        "",
        f"- Estimated Live Market Opportunities: **{executive['estimated_live']}**",
        f"- Raw active live postings: **{executive['raw_live']}**",
        f"- Public inventory opportunities: **{executive['public_inventory']}**",
        f"- Evergreen application opportunities: **{executive['evergreen']}**",
        f"- Mixed/report-separately opportunities: **{executive['mixed']}**",
        f"- Experimental/excluded opportunities: **{executive['experimental_excluded']}**",
        (
            "- Total observable opportunity rows: "
            f"**{executive['total_observable_rows']}** "
            "(not the same as the live market estimate)"
        ),
        "",
        (
            "The live estimate is intentionally conservative: canonicalized live "
            "sources contribute canonical opportunities, while non-canonicalized "
            "live sources contribute raw active jobs."
        ),
        "",
    ]

    append_source_breakdown(lines, source_breakdown)

    lines.extend(["## Live Market Composition", ""])
    append_count_table(lines, "Live Contribution by Source", live_by_source)
    append_count_table(lines, "Live Opportunities by Expertise/Department", live_by_expertise)
    append_count_table(lines, "Live Opportunity Location Signals", live_by_location)
    append_count_table(lines, "Live Opportunity Commitment Signals", live_by_commitment)
    lines.extend(
        [
            "Location and commitment tables count canonical opportunities where available. "
            "Multi-location opportunities may appear in more than one location row, so those "
            "tables are directional composition signals rather than additive totals.",
            "",
        ]
    )

    append_report_separately(lines, report_separately)
    append_recent_movement(lines, recent)
    append_source_health(lines, health)
    append_backlog(lines)
    append_methodology(lines)
    return "\n".join(lines)


def append_source_breakdown(lines, rows):
    lines.extend(
        [
            "## Source Breakdown",
            "",
            "| Source | Tier | Inventory Model | Count Policy | Raw Active | Canonical | Estimated Live | Report-Separately | Experimental/Excluded |",
            "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in rows:
        lines.append(
            "| "
            f"{escape(row['name'])} | "
            f"{escape(row['source_tier'])} | "
            f"{escape(row['inventory_model'])} | "
            f"{escape(row['market_count_policy'])} | "
            f"{row['raw_active']} | "
            f"{format_optional(row['canonical_opportunities'])} | "
            f"{row['estimated_live']} | "
            f"{row['report_separately']} | "
            f"{row['experimental_excluded']} |"
        )
    lines.append("")


def append_report_separately(lines, rows):
    lines.extend(["## Report-Separately Universe", ""])
    if not rows:
        lines.extend(["No report-separately or excluded sources currently have active rows.", ""])
        return

    lines.extend(
        [
            "| Source | Inventory Model | Policy | Active Rows | Note |",
            "| --- | --- | --- | ---: | --- |",
        ]
    )
    for row in rows:
        note = "Useful job-seeker opportunity/signal; excluded from live estimate."
        if row["slug"] == "invisible":
            note = "Experimental corporate careers source; excluded from live estimate."
        lines.append(
            "| "
            f"{escape(row['name'])} | "
            f"{escape(row['inventory_model'])} | "
            f"{escape(row['market_count_policy'])} | "
            f"{row['raw_active']} | "
            f"{note} |"
        )
    lines.append("")


def append_recent_movement(lines, recent):
    lines.extend(
        [
            "## Recent Movement",
            "",
            f"Window: last {RECENT_DAYS} days. Events are local tracker lifecycle events; they should not be overread as market causality.",
            "",
        ]
    )
    append_count_table(lines, "Lifecycle Events", recent["event_counts"])

    lines.extend(
        [
            "### Largest Source Movements",
            "",
            "| Source | Discovered | Removed | Reactivated |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    if not recent["source_movements"]:
        lines.append("| None | 0 | 0 | 0 |")
    else:
        for row in recent["source_movements"]:
            lines.append(
                "| "
                f"{escape(row['source'])} | "
                f"{row['discovered'] or 0} | "
                f"{row['removed'] or 0} | "
                f"{row['reactivated'] or 0} |"
            )
    lines.append("")

    lines.extend(["### Notable Crawl Runs", ""])
    if not recent["notable_crawl_runs"]:
        lines.extend(["No notable failed or high-movement crawl runs in the window.", ""])
        return

    lines.extend(
        [
            "| Source | Started | Status | Found | New | Removed | Note |",
            "| --- | --- | --- | ---: | ---: | ---: | --- |",
        ]
    )
    for row in recent["notable_crawl_runs"]:
        note = row["error_message"] or "High movement; review source context before inferring causality."
        lines.append(
            "| "
            f"{escape(row['source'])} | "
            f"{escape(row['started_at'])} | "
            f"{escape(row['status'])} | "
            f"{row['jobs_found_count']} | "
            f"{row['jobs_new_count']} | "
            f"{row['jobs_removed_count']} | "
            f"{escape(note)} |"
        )
    lines.append("")


def append_source_health(lines, health):
    lines.extend(["## Source Health Notes", ""])
    lines.extend(["### Largest Raw-to-Canonical Reductions", ""])
    if not health["largest_reductions"]:
        lines.extend(["No canonical reductions currently reported.", ""])
    else:
        for row in health["largest_reductions"]:
            lines.append(
                f"- {row['name']}: {row['variant_reduction']} raw variants collapsed"
            )
        lines.append("")

    append_count_table(lines, "Unlinked Active Rows in Canonicalized Sources", health["unlinked"])
    append_count_table(lines, "Unknown Taxonomy Affecting Live Estimate", health["unknown_live"])

    lines.extend(["### Report-Separately Unknown Taxonomy", ""])
    if not health["unknown_report_separately"]:
        lines.extend(["None.", ""])
    else:
        for row in health["unknown_report_separately"]:
            lines.append(
                f"- {row['source']}: {row['count']} rows "
                f"({row['inventory_model']} / {row['market_count_policy']}); "
                "does not affect the live estimate."
            )
        lines.append("")

    lines.extend(
        [
            "### Mindrift Lifecycle Guard",
            "",
            (
                f"Mindrift has a source-specific lifecycle guard: successful-looking "
                f"crawls with more than {MINDRIFT_PARTIAL_DROP_THRESHOLD:.0%} drop "
                f"and at least {MINDRIFT_MIN_REMOVALS_FOR_GUARD} implied removals "
                "fail before writes/events/removals are applied."
            ),
            "",
            "### OneForma Duplicate URL Reuse",
            "",
            (
                f"OneForma has {health['oneforma_duplicate_urls']['extras']} duplicate URL "
                f"extras across {health['oneforma_duplicate_urls']['groups']} groups. "
                "These are usually expected application-variant URL reuse and do not "
                "change OneForma's canonical live contribution."
            ),
        ]
    )
    watch = health["oneforma_duplicate_urls"]["watch"]
    if watch:
        lines.append("")
        lines.append("Watch items spanning multiple canonical opportunities:")
        for row in watch:
            lines.append(
                f"- {row['url']}: {row['canonical_titles']}"
            )
    lines.extend(
        [
            "",
            "### Encoding Artifact Diagnostics",
            "",
            (
                "No stored encoding artifacts found."
                if health["encoding_artifacts"] == 0
                else f"{health['encoding_artifacts']} possible encoding artifacts found."
            ),
            "",
        ]
    )


def append_backlog(lines):
    lines.extend(
        [
            "## Strategic Backlog / Not Implemented",
            "",
            "- TELUS AI Community: postponed because local HTTP fetches hit Cloudflare/fetch reliability barriers; should affect live estimate only after reliable crawling is confirmed.",
            "- Remotasks: strategic/evergreen candidate only; actual task inventory appears login-gated and overlaps with Outlier.",
            "- Centific Expert Network: strategic/evergreen candidate only; public worker-facing surface overlaps with OneForma ecosystem coverage.",
            "",
        ]
    )


def append_methodology(lines):
    lines.extend(
        [
            "## Methodology Notes",
            "",
            "- `live_feed` sources with `market_count_policy = count_live` contribute to Estimated Live Market Opportunities.",
            "- Canonicalized sources use active canonical opportunities.",
            "- Non-canonicalized live sources use active raw jobs.",
            "- `public_inventory`, `evergreen_application`, `mixed/report_separately`, and experimental sources are reported separately.",
            "- The live estimate is intentionally conservative and should be read as observable live opportunity supply, not total ecosystem size.",
            "",
        ]
    )


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


def quote_list(values):
    return ",".join(f"'{value}'" for value in values)


def scalar(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()[0]


def format_optional(value):
    if value is None:
        return "-"
    return str(value)


def escape(value):
    return str(value or "").replace("|", "\\|").replace("\n", " ")


if __name__ == "__main__":
    main()

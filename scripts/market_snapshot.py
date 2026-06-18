import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wahojobs.db.connection import get_connection
from wahojobs.reporting.market import get_market_size_summary
from wahojobs.reporting.micro1 import get_micro1_metrics


OUTPUT_PATH = Path("exports/market_snapshot.md")
CORE_SOURCES = (
    "alignerr",
    "appen",
    "meridial",
    "mercor",
    "micro1",
    "oneforma",
    "outlier",
    "rws",
    "welocalize",
)
EXPERIMENTAL_SOURCES = ("invisible",)


def main():
    args = parse_args()
    generated_at = datetime.now(timezone.utc)
    report_date = generated_at.date().isoformat()

    with get_connection() as conn:
        market_summary = get_market_size_summary(
            conn,
            include_experimental=args.include_experimental,
            include_simulation=False,
        )
        micro1_metrics = get_micro1_metrics(conn)
        jobs_by_company = get_active_jobs_by_company(conn, args.include_experimental)
        jobs_by_expertise = get_active_jobs_by_expertise(conn, args.include_experimental)
        event_counts = {
            "discovered": count_events(
                conn, report_date, "discovered", args.include_experimental
            ),
            "removed": count_events(
                conn, report_date, "removed", args.include_experimental
            ),
            "reactivated": count_events(
                conn, report_date, "reactivated", args.include_experimental
            ),
        }
        recent_events = get_recent_events(conn, args.include_experimental, limit=10)
        data_quality = get_data_quality_summary(conn, args.include_experimental)

    markdown = render_snapshot(
        generated_at,
        args.include_experimental,
        market_summary,
        micro1_metrics,
        jobs_by_company,
        jobs_by_expertise,
        event_counts,
        recent_events,
        data_quality,
    )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(markdown, encoding="utf-8")
    print(f"Wrote market snapshot to {OUTPUT_PATH}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate a Markdown Wahojobs market snapshot."
    )
    parser.add_argument(
        "--include-experimental",
        action="store_true",
        help="Include non-core/experimental sources such as Invisible.",
    )
    return parser.parse_args()


def experimental_filter(alias, include_experimental):
    if include_experimental:
        return ""
    return f"AND {alias}.slug NOT IN ({quote_list(EXPERIMENTAL_SOURCES)})"


def simulation_filter(alias):
    return f"AND {alias}.title NOT LIKE '[SIMULATION]%'"


def sample_fallback_filter(alias):
    return f"AND COALESCE({alias}.url, '') NOT LIKE 'https://outlier.ai/sample/%'"


def company_label(alias):
    return (
        f"CASE WHEN {alias}.slug IN ({quote_list(EXPERIMENTAL_SOURCES)}) "
        f"THEN {alias}.name || ' [EXPERIMENTAL]' ELSE {alias}.name END"
    )


def quote_list(values):
    return ",".join(f"'{value}'" for value in values)


def get_active_jobs_by_company(conn, include_experimental):
    return conn.execute(
        f"""
        SELECT {company_label("c")} AS label, COUNT(j.id) AS count
        FROM companies c
        LEFT JOIN jobs j
         ON j.company_id = c.id
         AND j.is_active = 1
         {simulation_filter("j")}
         {sample_fallback_filter("j")}
        WHERE 1 = 1
          {experimental_filter("c", include_experimental)}
        GROUP BY c.id, c.name
        HAVING count > 0
        ORDER BY count DESC, c.name ASC
        """
    ).fetchall()


def get_active_jobs_by_expertise(conn, include_experimental):
    return conn.execute(
        f"""
        SELECT
          COALESCE(NULLIF(TRIM(j.expertise), ''), NULLIF(TRIM(j.department), ''), 'Unknown') AS label,
          COUNT(*) AS count
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        WHERE j.is_active = 1
          {simulation_filter("j")}
          {sample_fallback_filter("j")}
          {experimental_filter("c", include_experimental)}
        GROUP BY label
        ORDER BY count DESC, label ASC
        """
    ).fetchall()


def count_events(conn, report_date, event_type, include_experimental):
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM job_events je
        JOIN jobs j ON j.id = je.job_id
        JOIN companies c ON c.id = j.company_id
        WHERE date(je.created_at) = ?
          AND je.event_type = ?
          {simulation_filter("j")}
          {sample_fallback_filter("j")}
          {experimental_filter("c", include_experimental)}
        """,
        (report_date, event_type),
    ).fetchone()
    return row["count"]


def get_recent_events(conn, include_experimental, limit):
    return conn.execute(
        f"""
        SELECT
          je.event_type,
          je.created_at,
          {company_label("c")} AS company,
          j.title,
          j.location,
          COALESCE(NULLIF(TRIM(j.expertise), ''), NULLIF(TRIM(j.department), ''), 'Unknown') AS expertise,
          j.url
        FROM job_events je
        JOIN jobs j ON j.id = je.job_id
        JOIN companies c ON c.id = j.company_id
        WHERE 1 = 1
          {simulation_filter("j")}
          {sample_fallback_filter("j")}
          {experimental_filter("c", include_experimental)}
        ORDER BY je.created_at DESC, je.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def get_data_quality_summary(conn, include_experimental):
    source_filter = experimental_filter("c", include_experimental)
    sim_filter = simulation_filter("j")
    sample_filter = sample_fallback_filter("j")
    checks = [
        (
            "Jobs missing title",
            f"""
            SELECT COUNT(*)
            FROM jobs j JOIN companies c ON c.id = j.company_id
            WHERE (j.title IS NULL OR TRIM(j.title) = '')
              {sim_filter}
              {sample_filter}
              {source_filter}
            """,
        ),
        (
            "Jobs missing URL",
            f"""
            SELECT COUNT(*)
            FROM jobs j JOIN companies c ON c.id = j.company_id
            WHERE (j.url IS NULL OR TRIM(j.url) = '')
              {sim_filter}
              {sample_filter}
              {source_filter}
            """,
        ),
        (
            "Jobs missing location",
            f"""
            SELECT COUNT(*)
            FROM jobs j JOIN companies c ON c.id = j.company_id
            WHERE (j.location IS NULL OR TRIM(j.location) = '')
              {sim_filter}
              {sample_filter}
              {source_filter}
            """,
        ),
        (
            "Active jobs with Unknown expertise/department",
            f"""
            SELECT COUNT(*)
            FROM jobs j JOIN companies c ON c.id = j.company_id
            WHERE j.is_active = 1
              AND COALESCE(NULLIF(TRIM(j.expertise), ''), NULLIF(TRIM(j.department), ''), 'Unknown') = 'Unknown'
              {sim_filter}
              {sample_filter}
              {source_filter}
            """,
        ),
        (
            "Duplicate external_id within same company",
            f"""
            SELECT COUNT(*)
            FROM (
              SELECT j.company_id, j.external_id
              FROM jobs j JOIN companies c ON c.id = j.company_id
              WHERE j.external_id IS NOT NULL
                AND TRIM(j.external_id) != ''
                {sim_filter}
                {sample_filter}
                {source_filter}
              GROUP BY j.company_id, j.external_id
              HAVING COUNT(*) > 1
            )
            """,
        ),
        (
            "Duplicate URL within same company",
            f"""
            SELECT COUNT(*)
            FROM (
              SELECT j.company_id, j.url
              FROM jobs j JOIN companies c ON c.id = j.company_id
              WHERE j.url IS NOT NULL
                AND TRIM(j.url) != ''
                {sim_filter}
                {sample_filter}
                {source_filter}
              GROUP BY j.company_id, j.url
              HAVING COUNT(*) > 1
            )
            """,
        ),
        (
            "Failed crawl runs",
            f"""
            SELECT COUNT(*)
            FROM crawl_runs cr
            JOIN companies c ON c.id = cr.company_id
            WHERE cr.status = 'failed'
              {experimental_filter("c", include_experimental)}
            """,
        ),
    ]

    return [(label, scalar_count(conn, sql)) for label, sql in checks]


def scalar_count(conn, sql):
    return conn.execute(sql).fetchone()[0]


def render_snapshot(
    generated_at,
    include_experimental,
    market_summary,
    micro1_metrics,
    jobs_by_company,
    jobs_by_expertise,
    event_counts,
    recent_events,
    data_quality,
):
    source_names = list(CORE_SOURCES)
    if include_experimental:
        source_names.extend(f"{source} (experimental)" for source in EXPERIMENTAL_SOURCES)

    lines = [
        "# Wahojobs AI Training Market Snapshot",
        "",
        f"Generated: {generated_at.isoformat(timespec='seconds')} UTC",
        "",
        "## Sources",
        "",
        f"Core sources included: {', '.join(source_names)}",
        f"Experimental sources: {'included' if include_experimental else 'excluded'}",
        "Simulation data: excluded",
        "Sample fallback data: excluded",
        "",
        "## Market Size",
        "",
        f"- Raw active postings: **{market_summary['raw_active_postings']}**",
        (
            "- Estimated market opportunities: "
            f"**{market_summary['estimated_market_opportunities']}**"
        ),
        (
            "- Estimate method: canonicalized sources where available, "
            "raw active jobs elsewhere."
        ),
        f"- Alignerr raw postings: **{market_summary['alignerr_raw_postings']}**",
        (
            "- Alignerr canonical opportunities: "
            f"**{market_summary['alignerr_canonical_opportunities']}**"
        ),
        f"- Alignerr posting variants: **{market_summary['alignerr_posting_variants']}**",
        f"- OneForma raw variants: **{market_summary['oneforma_raw_variants']}**",
        (
            "- OneForma canonical opportunities: "
            f"**{market_summary['oneforma_canonical_opportunities']}**"
        ),
        f"- OneForma posting variants: **{market_summary['oneforma_posting_variants']}**",
        f"- Welocalize raw postings: **{market_summary['welocalize_raw_postings']}**",
        (
            "- Welocalize canonical opportunities: "
            f"**{market_summary['welocalize_canonical_opportunities']}**"
        ),
        f"- Welocalize posting variants: **{market_summary['welocalize_posting_variants']}**",
        f"- micro1 active jobs: **{micro1_metrics['active_jobs']}**",
        f"- micro1 unique titles: **{micro1_metrics['unique_titles']}**",
        f"- micro1 duplicate-title count: **{micro1_metrics['duplicate_title_count']}**",
        "",
    ]

    append_count_table(lines, "Active Jobs by Company", jobs_by_company)
    append_count_table(lines, "Active Jobs by Expertise/Department", jobs_by_expertise)

    lines.extend(
        [
            "## Today's Activity",
            "",
            f"- New jobs today: **{event_counts['discovered']}**",
            f"- Removed jobs today: **{event_counts['removed']}**",
            f"- Reactivated jobs today: **{event_counts['reactivated']}**",
            "",
        ]
    )

    append_events(lines, "Top Recent Events", recent_events)
    append_count_table(lines, "Data Quality Summary", data_quality)

    lines.extend(
        [
            "## Notes",
            "",
            (
                "- Estimated market opportunities are an approximation: Alignerr, "
                "OneForma, and Welocalize use canonical opportunity grouping, while other sources "
                "currently count each active raw job as one opportunity."
            ),
            "- Simulation data is excluded from this snapshot.",
            "- Legacy sample fallback rows are excluded from this publishable snapshot.",
            "",
        ]
    )
    return "\n".join(lines)


def append_count_table(lines, title, rows):
    lines.extend([f"## {title}", ""])
    if not rows:
        lines.extend(["None.", ""])
        return

    lines.extend(["| Label | Count |", "| --- | ---: |"])
    for row in rows:
        label = row["label"] if hasattr(row, "keys") and "label" in row.keys() else row[0]
        count = row["count"] if hasattr(row, "keys") and "count" in row.keys() else row[1]
        lines.append(f"| {escape_markdown(label)} | {count} |")
    lines.append("")


def append_events(lines, title, rows):
    lines.extend([f"## {title}", ""])
    if not rows:
        lines.extend(["None.", ""])
        return

    lines.extend(
        [
            "| Event | Time | Company | Title | Expertise/Department | Location | URL |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in rows:
        url = row["url"] or ""
        link = f"[Link]({url})" if url else ""
        lines.append(
            "| "
            f"{escape_markdown(row['event_type'])} | "
            f"{escape_markdown(row['created_at'])} | "
            f"{escape_markdown(row['company'])} | "
            f"{escape_markdown(row['title'])} | "
            f"{escape_markdown(row['expertise'])} | "
            f"{escape_markdown(row['location'] or 'Unknown')} | "
            f"{link} |"
        )
    lines.append("")


def escape_markdown(value):
    text = str(value)
    return text.replace("|", "\\|").replace("\n", " ")


if __name__ == "__main__":
    main()

import sys
from pathlib import Path
import re

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wahojobs.classification import (
    MARKET_COUNT_POLICY_COUNT_LIVE,
    SOURCE_TIER_EXPERIMENTAL,
)
from wahojobs.db.connection import get_connection
from wahojobs.reporting.market import CANONICALIZED_SLUGS


ENCODING_ARTIFACT_PATTERNS = (
    ("replacement character", "\ufffd"),
    ("mojibake", "â€™"),
    ("mojibake", "â€œ"),
    ("mojibake", "â€�"),
    ("mojibake", "â€“"),
    ("mojibake", "â€”"),
    ("mojibake", "Ã"),
    ("mojibake", "Â"),
)


def main():
    with get_connection() as conn:
        source_rows = get_source_health_rows(conn)
        duplicate_title_groups = get_top_duplicate_title_groups(conn)
        canonical_variant_groups = get_top_canonical_variant_groups(conn)
        encoding_artifacts = get_encoding_artifacts(conn)

    print("")
    print("Wahojobs Canonicalization Health")
    print("================================")
    print("Read-only diagnostic report. Experimental and report-separately sources")
    print("are shown, but excluded from the live estimate by policy.")
    print("")

    print_source_health(source_rows)
    print_top_reductions(source_rows)
    print_report_separately_sources(source_rows)
    print_experimental_sources(source_rows)
    print_encoding_artifacts(encoding_artifacts)
    print_duplicate_title_groups(duplicate_title_groups)
    print_canonical_variant_groups(canonical_variant_groups)


def get_source_health_rows(conn):
    rows = []
    companies = conn.execute(
        """
        SELECT id, name, slug, source_tier, inventory_model, market_count_policy
        FROM companies
        ORDER BY name ASC
        """
    ).fetchall()

    for company in companies:
        raw_active = count_raw_active_jobs(conn, company["id"])
        canonicalized = company["slug"] in CANONICALIZED_SLUGS
        canonical_opportunities = (
            count_canonical_opportunities(conn, company["id"])
            if canonicalized
            else None
        )
        estimated_live_contribution = estimate_live_contribution(
            conn,
            company["id"],
            company["slug"],
            company["market_count_policy"],
            canonicalized,
        )
        variant_reduction = (
            max(raw_active - canonical_opportunities, 0)
            if canonical_opportunities is not None
            else 0
        )
        variant_reduction_percent = (
            (variant_reduction / raw_active * 100)
            if raw_active
            else 0
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
                "estimated_live_contribution": estimated_live_contribution,
                "variant_reduction": variant_reduction,
                "variant_reduction_percent": variant_reduction_percent,
                "duplicate_title_extras": count_duplicate_title_extras(
                    conn,
                    company["id"],
                ),
                "duplicate_url_extras": count_duplicate_url_extras(
                    conn,
                    company["id"],
                ),
                "unlinked_active_rows": (
                    count_unlinked_active_rows(conn, company["id"])
                    if canonicalized
                    else None
                ),
                "unknown_taxonomy_count": count_unknown_taxonomy(conn, company["id"]),
            }
        )
    return rows


def count_raw_active_jobs(conn, company_id):
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


def count_canonical_opportunities(conn, company_id):
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


def estimate_live_contribution(conn, company_id, slug, market_count_policy, canonicalized):
    if market_count_policy != MARKET_COUNT_POLICY_COUNT_LIVE:
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

    if slug in CANONICALIZED_SLUGS:
        return 0

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


def count_duplicate_title_extras(conn, company_id):
    return scalar(
        conn,
        """
        SELECT COALESCE(SUM(count - 1), 0)
        FROM (
          SELECT LOWER(TRIM(title)) AS title_key, COUNT(*) AS count
          FROM jobs
          WHERE company_id = ?
            AND is_active = 1
            AND title IS NOT NULL
            AND TRIM(title) != ''
            AND title NOT LIKE '[SIMULATION]%'
          GROUP BY title_key
          HAVING COUNT(*) > 1
        )
        """,
        (company_id,),
    )


def count_duplicate_url_extras(conn, company_id):
    return scalar(
        conn,
        """
        SELECT COALESCE(SUM(count - 1), 0)
        FROM (
          SELECT url, COUNT(*) AS count
          FROM jobs
          WHERE company_id = ?
            AND is_active = 1
            AND url IS NOT NULL
            AND TRIM(url) != ''
            AND title NOT LIKE '[SIMULATION]%'
          GROUP BY url
          HAVING COUNT(*) > 1
        )
        """,
        (company_id,),
    )


def count_unlinked_active_rows(conn, company_id):
    return scalar(
        conn,
        """
        SELECT COUNT(*)
        FROM jobs
        WHERE company_id = ?
          AND is_active = 1
          AND canonical_opportunity_id IS NULL
          AND title NOT LIKE '[SIMULATION]%'
        """,
        (company_id,),
    )


def count_unknown_taxonomy(conn, company_id):
    return scalar(
        conn,
        """
        SELECT COUNT(*)
        FROM jobs
        WHERE company_id = ?
          AND is_active = 1
          AND COALESCE(
            NULLIF(TRIM(expertise), ''),
            NULLIF(TRIM(department), ''),
            'Unknown'
          ) = 'Unknown'
          AND title NOT LIKE '[SIMULATION]%'
        """,
        (company_id,),
    )


def get_top_duplicate_title_groups(conn):
    return conn.execute(
        """
        SELECT
          c.name AS company,
          j.title,
          COUNT(*) AS count,
          COUNT(*) - 1 AS extras
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        WHERE j.is_active = 1
          AND j.title IS NOT NULL
          AND TRIM(j.title) != ''
          AND j.title NOT LIKE '[SIMULATION]%'
        GROUP BY c.id, LOWER(TRIM(j.title))
        HAVING COUNT(*) > 1
        ORDER BY extras DESC, c.name ASC, j.title ASC
        LIMIT 25
        """
    ).fetchall()


def get_top_canonical_variant_groups(conn):
    return conn.execute(
        """
        SELECT
          c.name AS company,
          co.canonical_title,
          co.source_category,
          co.variant_count
        FROM canonical_opportunities co
        JOIN companies c ON c.id = co.company_id
        WHERE co.is_active = 1
          AND co.variant_count > 1
        ORDER BY co.variant_count DESC, c.name ASC, co.canonical_title ASC
        LIMIT 30
        """
    ).fetchall()


def get_encoding_artifacts(conn):
    examples = []
    examples.extend(get_job_encoding_artifacts(conn))
    examples.extend(get_canonical_encoding_artifacts(conn))
    return examples


def get_job_encoding_artifacts(conn):
    fields = ("title", "department", "expertise")
    rows = conn.execute(
        """
        SELECT c.name AS company, j.id, j.title, j.department, j.expertise
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        WHERE j.is_active = 1
          AND j.title NOT LIKE '[SIMULATION]%'
        ORDER BY c.name ASC, j.id ASC
        """
    ).fetchall()

    examples = []
    for row in rows:
        for field in fields:
            value = row[field]
            artifact = detect_encoding_artifact(value)
            if artifact:
                examples.append(
                    {
                        "source": row["company"],
                        "record_type": "job",
                        "record_id": row["id"],
                        "field": field,
                        "artifact": artifact,
                        "value": value,
                    }
                )
    return examples


def get_canonical_encoding_artifacts(conn):
    fields = ("canonical_title", "source_category")
    rows = conn.execute(
        """
        SELECT c.name AS company, co.id, co.canonical_title, co.source_category
        FROM canonical_opportunities co
        JOIN companies c ON c.id = co.company_id
        WHERE co.is_active = 1
        ORDER BY c.name ASC, co.id ASC
        """
    ).fetchall()

    examples = []
    for row in rows:
        for field in fields:
            value = row[field]
            artifact = detect_encoding_artifact(value)
            if artifact:
                examples.append(
                    {
                        "source": row["company"],
                        "record_type": "canonical",
                        "record_id": row["id"],
                        "field": field,
                        "artifact": artifact,
                        "value": value,
                    }
                )
    return examples


def detect_encoding_artifact(value):
    if not value:
        return None
    for label, pattern in ENCODING_ARTIFACT_PATTERNS:
        if pattern in value:
            return label
    if re.search(r"[\u0080-\u009f]", value):
        return "C1 control character"
    return None


def scalar(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()[0]


def print_source_health(rows):
    print("Source Health")
    print("-------------")
    print_table(
        [
            [
                "Source",
                "Tier",
                "Inventory",
                "Policy",
                "Raw",
                "Canon",
                "Est",
                "Reduction",
                "Dup Titles",
                "Dup URLs",
                "Unlinked",
                "Unknown",
            ],
            *[
                [
                    source_label(row),
                    row["source_tier"],
                    row["inventory_model"],
                    row["market_count_policy"],
                    row["raw_active"],
                    format_optional(row["canonical_opportunities"]),
                    row["estimated_live_contribution"],
                    format_reduction(row),
                    row["duplicate_title_extras"],
                    row["duplicate_url_extras"],
                    format_optional(row["unlinked_active_rows"]),
                    row["unknown_taxonomy_count"],
                ]
                for row in rows
            ],
        ]
    )
    print("")


def print_top_reductions(rows):
    reduced = [
        row for row in rows
        if row["canonical_opportunities"] is not None and row["variant_reduction"] > 0
    ]
    reduced.sort(
        key=lambda row: (
            row["variant_reduction"],
            row["variant_reduction_percent"],
            row["name"],
        ),
        reverse=True,
    )

    print("Top Sources by Raw-to-Canonical Reduction")
    print("-----------------------------------------")
    if not reduced:
        print("  None")
        print("")
        return
    for row in reduced:
        print(
            f"  {row['name']}: {row['variant_reduction']} fewer "
            f"({row['variant_reduction_percent']:.1f}%)"
        )
    print("")


def print_report_separately_sources(rows):
    report_separately = [
        row for row in rows
        if row["market_count_policy"] != MARKET_COUNT_POLICY_COUNT_LIVE
        and row["source_tier"] != SOURCE_TIER_EXPERIMENTAL
    ]
    print("Report-Separately Sources")
    print("-------------------------")
    if not report_separately:
        print("  None")
        print("")
        return
    for row in report_separately:
        print(
            f"  {row['name']}: {row['raw_active']} active rows, "
            f"{row['inventory_model']} / {row['market_count_policy']}, "
            "excluded from live estimate"
        )
    print("")


def print_experimental_sources(rows):
    experimental = [
        row for row in rows
        if row["source_tier"] == SOURCE_TIER_EXPERIMENTAL
    ]
    print("Experimental Sources")
    print("--------------------")
    if not experimental:
        print("  None")
        print("")
        return
    for row in experimental:
        print(
            f"  {row['name']}: {row['raw_active']} active rows, "
            f"{row['inventory_model']} / {row['market_count_policy']}, "
            "excluded from live estimate"
        )
    print("")


def print_encoding_artifacts(rows):
    print("Encoding Artifact Diagnostics")
    print("-----------------------------")
    if not rows:
        print("  None found")
        print("")
        return

    for row in rows[:25]:
        value = " ".join(str(row["value"]).split())
        if len(value) > 140:
            value = value[:137] + "..."
        print(
            f"  {row['source']} {row['record_type']}:{row['record_id']} "
            f"{row['field']} ({row['artifact']}): {value}"
        )
    if len(rows) > 25:
        print(f"  ... {len(rows) - 25} more")
    print("")


def print_duplicate_title_groups(rows):
    print("Top Duplicate Title Groups")
    print("--------------------------")
    if not rows:
        print("  None")
        print("")
        return
    for row in rows:
        print(
            f"  {row['company']}: {row['title']} "
            f"({row['count']} rows, {row['extras']} extras)"
        )
    print("")


def print_canonical_variant_groups(rows):
    print("Top Canonical Variant Groups")
    print("----------------------------")
    if not rows:
        print("  None")
        print("")
        return
    for row in rows:
        print(
            f"  {row['company']}: {row['canonical_title']} "
            f"({row['source_category']}) - {row['variant_count']} variants"
        )
    print("")


def source_label(row):
    labels = []
    if row["source_tier"] == SOURCE_TIER_EXPERIMENTAL:
        labels.append("EXPERIMENTAL")
    if row["market_count_policy"] != MARKET_COUNT_POLICY_COUNT_LIVE:
        labels.append("REPORT-SEPARATELY")
    if not labels:
        return row["name"]
    return f"{row['name']} [{' / '.join(labels)}]"


def format_optional(value):
    if value is None:
        return "-"
    return value


def format_reduction(row):
    if row["canonical_opportunities"] is None:
        return "-"
    return f"{row['variant_reduction']} ({row['variant_reduction_percent']:.1f}%)"


def print_table(rows):
    widths = [
        max(len(str(row[index])) for row in rows)
        for index in range(len(rows[0]))
    ]
    for row_index, row in enumerate(rows):
        print(
            "  "
            + " | ".join(
                str(value).ljust(widths[index])
                for index, value in enumerate(row)
            )
        )
        if row_index == 0:
            print(
                "  "
                + "-+-".join("-" * width for width in widths)
            )


if __name__ == "__main__":
    main()

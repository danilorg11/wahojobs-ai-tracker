import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wahojobs.classification import SOURCE_TIER_EXPERIMENTAL
from wahojobs.db.connection import get_connection


DEFAULT_OUTPUT = Path("exports/jobs.csv")


def main():
    args = parse_args()
    output_path = Path(args.output)

    with get_connection() as conn:
        rows = get_jobs(
            conn,
            include_inactive=args.include_inactive,
            include_simulation=args.include_simulation,
            include_experimental=args.include_experimental,
        )

    write_csv(output_path, rows)
    print(f"Exported {len(rows)} jobs to {output_path}")


def parse_args():
    parser = argparse.ArgumentParser(description="Export Wahojobs jobs to CSV.")
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help=f"Output CSV path. Defaults to {DEFAULT_OUTPUT}.",
    )
    parser.add_argument(
        "--include-inactive",
        action="store_true",
        help="Include inactive/removed jobs.",
    )
    parser.add_argument(
        "--include-simulation",
        action="store_true",
        help="Include local simulation jobs.",
    )
    parser.add_argument(
        "--include-experimental",
        action="store_true",
        help="Include non-core/experimental sources such as Invisible.",
    )
    return parser.parse_args()


def get_jobs(conn, include_inactive=False, include_simulation=False, include_experimental=False):
    where = []
    if not include_inactive:
        where.append("j.is_active = 1")
    if not include_simulation:
        where.append("j.title NOT LIKE '[SIMULATION]%'")
    if not include_experimental:
        where.append(f"c.source_tier != '{SOURCE_TIER_EXPERIMENTAL}'")

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    return conn.execute(
        f"""
        SELECT
          CASE WHEN c.source_tier = '{SOURCE_TIER_EXPERIMENTAL}'
               THEN c.name || ' [EXPERIMENTAL]'
               ELSE c.name
          END AS company,
          j.title,
          j.location,
          COALESCE(NULLIF(TRIM(j.expertise), ''), NULLIF(TRIM(j.department), ''), 'Unknown') AS expertise_department,
          j.commitment,
          j.url,
          j.first_seen_at,
          j.last_seen_at,
          j.is_active,
          c.source_tier,
          c.inventory_model,
          c.market_count_policy,
          j.opportunity_kind,
          j.availability_basis,
          j.include_in_live_market_estimate
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        {where_sql}
        ORDER BY c.name ASC, j.title ASC
        """
    ).fetchall()


def write_csv(output_path, rows):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "company",
        "title",
        "location",
        "expertise/department",
        "commitment",
        "url",
        "first_seen_at",
        "last_seen_at",
        "is_active",
        "source_tier",
        "inventory_model",
        "market_count_policy",
        "opportunity_kind",
        "availability_basis",
        "include_in_live_market_estimate",
    ]

    with output_path.open("w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "company": row["company"],
                    "title": row["title"],
                    "location": row["location"] or "",
                    "expertise/department": row["expertise_department"],
                    "commitment": row["commitment"] or "",
                    "url": row["url"],
                    "first_seen_at": row["first_seen_at"],
                    "last_seen_at": row["last_seen_at"],
                    "is_active": row["is_active"],
                    "source_tier": row["source_tier"],
                    "inventory_model": row["inventory_model"],
                    "market_count_policy": row["market_count_policy"],
                    "opportunity_kind": row["opportunity_kind"],
                    "availability_basis": row["availability_basis"],
                    "include_in_live_market_estimate": row[
                        "include_in_live_market_estimate"
                    ],
                }
            )


if __name__ == "__main__":
    main()

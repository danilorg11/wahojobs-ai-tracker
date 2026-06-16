import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wahojobs.db.connection import get_connection


DEFAULT_OUTPUT = Path("exports/events.csv")


def main():
    args = parse_args()
    output_path = Path(args.output)

    with get_connection() as conn:
        rows = get_events(conn, include_simulation=args.include_simulation)

    write_csv(output_path, rows)
    print(f"Exported {len(rows)} events to {output_path}")


def parse_args():
    parser = argparse.ArgumentParser(description="Export Wahojobs job events to CSV.")
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help=f"Output CSV path. Defaults to {DEFAULT_OUTPUT}.",
    )
    parser.add_argument(
        "--include-simulation",
        action="store_true",
        help="Include local simulation events.",
    )
    return parser.parse_args()


def get_events(conn, include_simulation=False):
    simulation_filter = "" if include_simulation else "WHERE j.title NOT LIKE '[SIMULATION]%'"

    return conn.execute(
        f"""
        SELECT
          je.event_type,
          je.created_at,
          c.name AS company,
          j.title,
          j.location,
          COALESCE(NULLIF(TRIM(j.expertise), ''), NULLIF(TRIM(j.department), ''), 'Unknown') AS expertise_department,
          j.url
        FROM job_events je
        JOIN jobs j ON j.id = je.job_id
        JOIN companies c ON c.id = j.company_id
        {simulation_filter}
        ORDER BY je.created_at DESC, je.id DESC
        """
    ).fetchall()


def write_csv(output_path, rows):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "event_type",
        "created_at",
        "company",
        "title",
        "location",
        "expertise/department",
        "url",
    ]

    with output_path.open("w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "event_type": row["event_type"],
                    "created_at": row["created_at"],
                    "company": row["company"],
                    "title": row["title"],
                    "location": row["location"] or "",
                    "expertise/department": row["expertise_department"],
                    "url": row["url"],
                }
            )


if __name__ == "__main__":
    main()

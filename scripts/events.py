import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wahojobs.db.connection import get_connection


EVENT_TYPES = {"discovered", "removed", "reactivated"}


def main():
    args = parse_args()
    today = datetime.now(timezone.utc).date().isoformat()

    with get_connection() as conn:
        rows = get_events(
            conn,
            today=today if args.today else None,
            event_type=args.event_type,
            company_slug=args.company,
            limit=args.limit,
        )

    print("")
    print("Wahojobs Job Events")
    print("===================")
    if args.today:
        print(f"Date: {today} UTC")
    if args.event_type:
        print(f"Type: {args.event_type}")
    if args.company:
        print(f"Company: {args.company}")
    print(f"Limit: {args.limit}")
    print("")

    if not rows:
        print("No events found.")
        print("")
        return

    for row in rows:
        print(f"[{row['event_type']}] {row['created_at']}")
        print(f"Company:   {row['company_name']}")
        print(f"Title:     {row['title']}")
        print(f"Location:  {row['location'] or 'Unknown'}")
        print(f"Expertise: {row['expertise_label']}")
        print(f"URL:       {row['url']}")
        print("")


def parse_args():
    parser = argparse.ArgumentParser(description="Show recent Wahojobs lifecycle events.")
    parser.add_argument(
        "--today",
        action="store_true",
        help="Only show events created today in UTC.",
    )
    parser.add_argument(
        "--type",
        dest="event_type",
        choices=sorted(EVENT_TYPES),
        help="Only show one event type.",
    )
    parser.add_argument(
        "--company",
        help="Only show events for a company slug, such as appen or meridial.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of events to show. Defaults to 20.",
    )
    return parser.parse_args()


def get_events(conn, today=None, event_type=None, company_slug=None, limit=20):
    where = []
    params = []

    if today:
        where.append("date(je.created_at) = ?")
        params.append(today)
    if event_type:
        where.append("je.event_type = ?")
        params.append(event_type)
    if company_slug:
        where.append("c.slug = ?")
        params.append(company_slug)

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    params.append(max(1, limit))

    return conn.execute(
        f"""
        SELECT
          je.event_type,
          je.created_at,
          c.name AS company_name,
          j.title,
          j.location,
          COALESCE(NULLIF(TRIM(j.expertise), ''), NULLIF(TRIM(j.department), ''), 'Unknown') AS expertise_label,
          j.url
        FROM job_events je
        JOIN jobs j ON j.id = je.job_id
        JOIN companies c ON c.id = j.company_id
        {where_sql}
        ORDER BY je.created_at DESC, je.id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()


if __name__ == "__main__":
    main()

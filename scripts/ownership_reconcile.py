import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wahojobs.ownership_reconciliation import reconcile_ownership  # noqa: E402


def main():
    args = parse_args()
    path = Path(args.db).resolve()
    if not path.is_file():
        raise SystemExit(f"Database does not exist: {path}")
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            report = reconcile_ownership(conn)
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise SystemExit(
            f"Invalid or unreadable SQLite database: {type(exc).__name__}"
        ) from None
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_human(report)
    raise SystemExit(1 if report["blocking"] else 0)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Strictly read-only reconciliation for dormant ownership migration 003."
    )
    parser.add_argument("--db", required=True, help="SQLite database to inspect read-only.")
    parser.add_argument("--json", action="store_true", help="Emit stable JSON output.")
    return parser.parse_args()


def print_human(report):
    print("Product Principal Ownership Reconciliation")
    print("==========================================")
    print("Mode: read-only")
    for name, value in sorted(report["counts"].items()):
        shown = value if value is not None else "unavailable"
        print(f"{name.replace('_', ' ').title()}: {shown}")
    print(
        "Migration marker: "
        + ("present" if report["schema"]["migration_marker_present"] else "missing")
    )
    for name, rows in sorted(report["checks"].items()):
        print(f"Blocking - {name.replace('_', ' ').title()}: {len(rows)}")
    for name, rows in sorted(report["informational"].items()):
        print(f"Information - {name.replace('_', ' ').title()}: {len(rows)}")
    print("Result: " + ("BLOCKING DRIFT" if report["blocking"] else "clean"))


if __name__ == "__main__":
    main()

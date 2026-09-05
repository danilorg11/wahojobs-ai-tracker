"""Recover explicit saved per-record details into a NEW disposable database.

No HTTP, models, lifecycle refresh, canonical merge or enrichment/ranking run.
The input manifest is a list of job_id, url, body_path, observed_at, status.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wahojobs.crawler.provider_details import DetailResponse, reprocess_saved_detail
from wahojobs.db.connection import get_connection
from wahojobs.db.repository import verify_job_source_acceptance_integrity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-database", type=Path, required=True)
    parser.add_argument("--output-database", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    if args.output_database.exists():
        parser.error("Output must be a new disposable database")
    records = json.loads(args.manifest.read_text(encoding="utf-8"))
    if not isinstance(records, list) or not records or len({r["job_id"] for r in records}) != len(records):
        parser.error("Manifest must identify distinct records explicitly")
    args.output_database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(args.source_database.resolve().as_uri()+"?mode=ro", uri=True) as source:
        with sqlite3.connect(args.output_database) as target:
            source.backup(target)
    connection = get_connection(args.output_database)
    results = []
    try:
        with connection:
            for row in records:
                body_path = Path(row["body_path"])
                if not body_path.is_absolute():
                    body_path = args.manifest.parent / body_path
                response = DetailResponse(row["url"], body_path.read_bytes(), row["observed_at"], row["status"])
                result = reprocess_saved_detail(connection, row["job_id"], response)
                if not result.accepted:
                    raise ValueError(f"Source conflict held record {row['job_id']}: {result.promotion_decision}")
                verify_job_source_acceptance_integrity(connection, row["job_id"])
                results.append({"job_id": row["job_id"], "capture_id": result.capture_id,
                                "decision": result.promotion_decision, "observed_at": response.observed_at})
    finally:
        connection.close()
    print(json.dumps({"output_database": str(args.output_database.resolve()), "results": results}, indent=2))


if __name__ == "__main__":
    main()

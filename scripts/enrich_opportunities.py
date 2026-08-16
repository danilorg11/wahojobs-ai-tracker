import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wahojobs.canonical.service import sync_fallback_canonical_opportunities
from wahojobs.config import DB_PATH
from wahojobs.db.connection import get_connection
from wahojobs.db.repository import install_base_schema
from wahojobs.matching.metadata_overlay import DEFAULT_OVERLAY_PATH
from wahojobs.opportunity_enrichment import (
    canonical_coverage,
    enrich_all_opportunities,
    import_reviewed_overlay,
)


def main():
    args = parse_args()
    result = run_backfill(args.database, args.import_overlay)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    print_report(result)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Create deterministic V2 enrichment for canonical opportunities."
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=DB_PATH,
        help=f"SQLite database path (default: {DB_PATH}).",
    )
    parser.add_argument(
        "--import-overlay",
        nargs="?",
        const=DEFAULT_OVERLAY_PATH,
        type=Path,
        help=(
            "Import the reviewed matching metadata overlay as field-level overrides; "
            f"defaults to {DEFAULT_OVERLAY_PATH} when no path is supplied."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print a machine-readable report.",
    )
    return parser.parse_args()


def run_backfill(database: Path, overlay_path: Path | None = None) -> dict:
    with get_connection(database) as conn:
        install_base_schema(conn)
        fallback_jobs_linked = 0
        company_rows = conn.execute("SELECT id FROM companies ORDER BY id").fetchall()
        for company in company_rows:
            fallback_jobs_linked += sync_fallback_canonical_opportunities(
                conn, company["id"]
            )

        enrichment = enrich_all_opportunities(conn)
        overlay = (
            import_reviewed_overlay(conn, overlay_path)
            if overlay_path is not None
            else None
        )
        coverage = canonical_coverage(conn)

    total = enrichment["total"]
    unknown_field_coverage = {
        path: {
            "records_unknown": count,
            "percent_unknown": round((count / total) * 100, 1) if total else 0.0,
        }
        for path, count in enrichment.pop("unknown_field_counts").items()
    }
    return {
        "database": str(Path(database)),
        "fallback_jobs_linked": fallback_jobs_linked,
        "coverage": coverage,
        "enrichment": enrichment,
        "unknown_field_coverage": unknown_field_coverage,
        "overlay_import": overlay,
    }


def print_report(result: dict) -> None:
    enrichment = result["enrichment"]
    coverage = result["coverage"]
    print("Opportunity enrichment V2 backfill")
    print(f"Database: {result['database']}")
    print(f"Fallback jobs linked: {result['fallback_jobs_linked']}")
    print(
        "Canonical coverage: "
        f"{coverage['jobs_canonicalized']}/{coverage['jobs_total']} jobs; "
        f"{coverage['enriched_total']}/{coverage['canonical_total']} opportunities enriched"
    )
    print(
        "Enrichment: "
        f"{enrichment['created']} created, {enrichment['updated']} updated, "
        f"{enrichment['unchanged']} unchanged; "
        f"{enrichment['partial']} partial, {enrichment['complete']} complete, "
        f"{enrichment['failed']} failed"
    )
    if result["overlay_import"] is not None:
        overlay = result["overlay_import"]
        print(
            "Overlay: "
            f"{overlay['records_imported']}/{overlay['records_total']} records imported; "
            f"{overlay['fields_created']} fields created, "
            f"{overlay['fields_updated']} updated, "
            f"{overlay['fields_unchanged']} unchanged"
        )
    print("Unknown-field coverage:")
    for path, item in result["unknown_field_coverage"].items():
        print(
            f"  {path}: {item['records_unknown']} "
            f"({item['percent_unknown']}%)"
        )


if __name__ == "__main__":
    main()

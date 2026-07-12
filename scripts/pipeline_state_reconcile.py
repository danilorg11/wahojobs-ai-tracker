import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wahojobs.pipeline_reconciliation import reconcile_pipeline_state


REASON_DESCRIPTIONS = {
    "applicant_action_binding_mismatch": (
        "Applicant metadata does not describe the effect required by its pipeline action."
    ),
    "applicant_result_receipt_mismatch": (
        "Applicant result and immutable operation receipt do not agree."
    ),
    "noop_state_binding_mismatch": (
        "Repeated-action metadata does not match the unchanged pipeline state."
    ),
    "user_initialization_action_binding_mismatch": (
        "User initialization does not match its terminal creation action."
    ),
    "user_initialization_fingerprint_mismatch": (
        "User initialization and terminal operation fingerprints do not agree."
    ),
    "user_initialization_internal_key_mismatch": (
        "User initialization key does not match its terminal operation."
    ),
    "user_initialization_terminal_link_missing": (
        "User initialization is not referenced by a terminal creation operation."
    ),
    "user_initialization_terminal_link_ambiguous": (
        "More than one terminal operation references this user initialization."
    ),
}


def main():
    args = parse_args()
    path = Path(args.db).resolve()
    if not path.is_file():
        raise SystemExit(f"Database does not exist: {path}")
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            report = reconcile_pipeline_state(conn)
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise SystemExit(f"Invalid or unreadable SQLite database: {path}: {exc}") from None

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_human_report(path, report)
    raise SystemExit(1 if report["blocking"] else 0)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Read-only reconciliation for normalized pipeline state."
    )
    parser.add_argument("--db", required=True, help="SQLite database to inspect read-only.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    return parser.parse_args()


def print_human_report(path, report):
    print("Pipeline State Reconciliation")
    print("=============================")
    print(f"Database: {path}")
    print("Mode: read-only")
    for name, value in sorted(report["counts"].items()):
        print(f"{name.replace('_', ' ').title()}: {value if value is not None else 'unavailable'}")
    schema = report["schema"]
    print(
        "Migration marker: "
        + ("present" if schema["migration_marker_present"] else "missing")
    )
    print(
        "Missing required objects: "
        + (", ".join(schema["required_objects_missing"]) or "none")
    )
    for name, rows in sorted(report["checks"].items()):
        print(f"{name.replace('_', ' ').title()}: {len(rows)}")
    if "unknown_legacy_workflows" in report:
        print(f"Unknown legacy workflows: {report['unknown_legacy_workflows']['total']}")
    if "transition_classes" in report:
        for name, count in sorted(report["transition_classes"].items()):
            print(f"{name.replace('_', ' ').title()}: {count}")
    blocking_findings = []
    blocking_checks = set(report.get("blocking_reasons", []))
    for check_name, rows in report["checks"].items():
        if check_name not in blocking_checks:
            continue
        for row in rows:
            reason = row.get("reason") if isinstance(row, dict) else None
            blocking_findings.append((reason or check_name, row if isinstance(row, dict) else {}))
    if blocking_findings:
        print(f"Blocking drift: {len(blocking_findings)}")
        grouped = {}
        for reason, row in blocking_findings:
            grouped.setdefault(reason, []).append(row)
        for reason in sorted(grouped):
            rows = sorted(
                grouped[reason],
                key=lambda row: (
                    str(row.get("pipeline_item_id", "")),
                    str(row.get("transition_id", "")),
                ),
            )
            print(f"{reason}: {len(rows)}")
            print(
                REASON_DESCRIPTIONS.get(
                    reason,
                    "Review this reconciliation finding before allowing pipeline mutations.",
                )
            )
            for row in rows:
                identifiers = []
                for field in ("pipeline_item_id", "transition_id"):
                    value = row.get(field)
                    if value not in (None, ""):
                        identifiers.append(f"{field}={value}")
                if identifiers:
                    print(" ".join(identifiers))
    else:
        print("Blocking drift: none")
    print("Result: " + ("BLOCKING DRIFT" if report["blocking"] else "clean"))
    if report.get("blocking_reasons"):
        print("Blocking checks: " + ", ".join(report["blocking_reasons"]))


if __name__ == "__main__":
    main()

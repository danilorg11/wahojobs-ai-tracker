import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from applicant_signal_report import load_updates
from profile_match_digest import load_profiles
from user_pipeline_digest import load_pipeline
from wahojobs.config import DB_PATH
from wahojobs.db.connection import get_connection


SCHEMA_PATH = Path(__file__).resolve().parent.parent / "wahojobs" / "db" / "schema.sql"
DEFAULT_EXPORT_PATH = Path("exports/product_state_export.json")


def main():
    args = parse_args()
    if args.command == "init":
        initialize_product_state_schema()
        print(f"Initialized product-state tables in {DB_PATH}")
        return

    initialize_product_state_schema()
    if args.command == "import-profiles":
        count = import_profiles(args.path)
        print(f"Imported/updated {count} user profile(s) from {args.path}")
    elif args.command == "import-pipeline":
        count = import_pipeline(args.path)
        print(f"Imported/updated {count} pipeline item(s) from {args.path}")
    elif args.command == "import-applicant-updates":
        count = import_applicant_updates(args.path)
        print(f"Imported/updated {count} applicant status update(s) from {args.path}")
    elif args.command == "summary":
        print_summary()
    elif args.command == "validate":
        validate_product_state(verbose=True)
    elif args.command == "export":
        export_product_state(args.out)
        print(f"Exported product-state data to {args.out}")
    else:
        raise SystemExit(f"Unsupported command: {args.command}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Manage local SQLite product-state prototype tables."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Create product-state tables if missing.")

    import_profiles_parser = subparsers.add_parser(
        "import-profiles",
        help="Import editable sample profiles into SQLite.",
    )
    import_profiles_parser.add_argument("path", type=Path)

    import_pipeline_parser = subparsers.add_parser(
        "import-pipeline",
        help="Import sample user pipeline items into SQLite.",
    )
    import_pipeline_parser.add_argument("path", type=Path)

    import_updates_parser = subparsers.add_parser(
        "import-applicant-updates",
        help="Import sample applicant status updates into SQLite.",
    )
    import_updates_parser.add_argument("path", type=Path)

    subparsers.add_parser("summary", help="Show product-state table counts.")
    subparsers.add_parser("validate", help="Validate product-state table consistency.")

    export_parser = subparsers.add_parser(
        "export",
        help="Export product-state tables to JSON.",
    )
    export_parser.add_argument("--out", type=Path, default=DEFAULT_EXPORT_PATH)
    return parser.parse_args()


def initialize_product_state_schema():
    with get_connection() as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def import_profiles(path):
    profiles, _ = load_profiles(path)
    with get_connection() as conn:
        for profile in profiles:
            upsert_profile(conn, profile)
    return len(profiles)


def import_pipeline(path):
    records, _ = load_pipeline(path)
    with get_connection() as conn:
        for record in records:
            ensure_profile_exists(conn, record["profile_id"])
            pipeline_item_id = record.get("pipeline_item_id") or stable_pipeline_item_id(record)
            conn.execute(
                """
                INSERT INTO user_pipeline_items (
                  pipeline_item_id,
                  user_id,
                  profile_id,
                  source,
                  opportunity_title,
                  opportunity_url,
                  opportunity_external_id,
                  canonical_id,
                  status,
                  status_date,
                  user_priority,
                  reminder_date,
                  notes,
                  last_user_action,
                  is_sample
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(pipeline_item_id) DO UPDATE SET
                  user_id = excluded.user_id,
                  profile_id = excluded.profile_id,
                  source = excluded.source,
                  opportunity_title = excluded.opportunity_title,
                  opportunity_url = excluded.opportunity_url,
                  opportunity_external_id = excluded.opportunity_external_id,
                  canonical_id = excluded.canonical_id,
                  status = excluded.status,
                  status_date = excluded.status_date,
                  user_priority = excluded.user_priority,
                  reminder_date = excluded.reminder_date,
                  notes = excluded.notes,
                  last_user_action = excluded.last_user_action,
                  is_sample = excluded.is_sample,
                  updated_at = CURRENT_TIMESTAMP
                """,
                (
                    pipeline_item_id,
                    record.get("user_id") or record["profile_id"],
                    record["profile_id"],
                    record["source"],
                    record["title"],
                    record.get("url") or "",
                    record.get("opportunity_id") or "",
                    parse_optional_int(record.get("canonical_id")),
                    record["status"],
                    record.get("status_date") or "",
                    record.get("user_priority") or "",
                    record.get("reminder_date") or "",
                    record.get("notes") or "",
                    record.get("last_user_action") or "",
                ),
            )
    return len(records)


def import_applicant_updates(path):
    updates, _ = load_updates(path)
    with get_connection() as conn:
        for update in updates:
            ensure_profile_exists(conn, update["profile_id"])
            conn.execute(
                """
                INSERT INTO applicant_status_updates (
                  update_id,
                  user_id,
                  anonymous_user_key,
                  profile_id,
                  source,
                  opportunity_title,
                  opportunity_url,
                  opportunity_external_id,
                  canonical_id,
                  status,
                  previous_status,
                  status_date,
                  reported_at,
                  evidence_type,
                  confidence_level,
                  notes,
                  is_sample
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(update_id) DO UPDATE SET
                  user_id = excluded.user_id,
                  anonymous_user_key = excluded.anonymous_user_key,
                  profile_id = excluded.profile_id,
                  source = excluded.source,
                  opportunity_title = excluded.opportunity_title,
                  opportunity_url = excluded.opportunity_url,
                  opportunity_external_id = excluded.opportunity_external_id,
                  canonical_id = excluded.canonical_id,
                  status = excluded.status,
                  previous_status = excluded.previous_status,
                  status_date = excluded.status_date,
                  reported_at = excluded.reported_at,
                  evidence_type = excluded.evidence_type,
                  confidence_level = excluded.confidence_level,
                  notes = excluded.notes,
                  is_sample = excluded.is_sample,
                  updated_at = CURRENT_TIMESTAMP
                """,
                (
                    update["update_id"],
                    update.get("user_id") or "",
                    update.get("anonymous_user_key") or update.get("user_id") or "",
                    update["profile_id"],
                    update["source"],
                    update["opportunity_title"],
                    update.get("opportunity_url") or "",
                    update.get("opportunity_id") or "",
                    parse_optional_int(update.get("canonical_id")),
                    update["status"],
                    update.get("previous_status") or "",
                    update["status_date"],
                    update["reported_at"],
                    update["evidence_type"],
                    update["confidence_level"],
                    update.get("notes") or "",
                ),
            )
    return len(updates)


def upsert_profile(conn, profile):
    conn.execute(
        """
        INSERT INTO user_profiles (
          user_id,
          profile_id,
          display_name,
          education_level,
          degrees_or_domains_json,
          languages_json,
          skills_json,
          work_preferences_json,
          constraints_json,
          target_opportunity_types_json,
          notes,
          is_sample
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(profile_id) DO UPDATE SET
          user_id = excluded.user_id,
          display_name = excluded.display_name,
          education_level = excluded.education_level,
          degrees_or_domains_json = excluded.degrees_or_domains_json,
          languages_json = excluded.languages_json,
          skills_json = excluded.skills_json,
          work_preferences_json = excluded.work_preferences_json,
          constraints_json = excluded.constraints_json,
          target_opportunity_types_json = excluded.target_opportunity_types_json,
          notes = excluded.notes,
          is_sample = excluded.is_sample,
          updated_at = CURRENT_TIMESTAMP
        """,
        (
            profile.get("user_id") or profile["profile_id"],
            profile["profile_id"],
            profile["display_name"],
            profile.get("education_level") or "not_specified",
            dumps_list(profile.get("degrees_or_domains")),
            dumps_list(profile.get("languages")),
            dumps_list(profile.get("skills")),
            dumps_list(profile.get("work_preferences")),
            dumps_list(profile.get("constraints")),
            dumps_list(profile.get("target_opportunity_types")),
            profile.get("notes") or profile.get("summary") or "",
        ),
    )


def ensure_profile_exists(conn, profile_id):
    existing = conn.execute(
        "SELECT id FROM user_profiles WHERE profile_id = ?",
        (profile_id,),
    ).fetchone()
    if existing is not None:
        return

    profile = built_in_profiles_by_id().get(profile_id)
    if profile is None:
        profile = {
            "user_id": profile_id,
            "profile_id": profile_id,
            "display_name": profile_id.replace("_", " ").title(),
            "education_level": "not_specified",
            "degrees_or_domains": [],
            "languages": [],
            "skills": [],
            "work_preferences": [],
            "constraints": [],
            "target_opportunity_types": [],
            "notes": "Auto-created placeholder from imported product-state sample data.",
        }
    upsert_profile(conn, profile)


def built_in_profiles_by_id():
    profiles, _ = load_profiles(None)
    return {profile["profile_id"]: profile for profile in profiles}


def print_summary():
    with get_connection() as conn:
        counts = table_counts(conn)
        pipeline_statuses = count_by(conn, "user_pipeline_items", "status")
        update_statuses = count_by(conn, "applicant_status_updates", "status")
        update_confidence = count_by(conn, "applicant_status_updates", "confidence_level")
        validation_errors = validate_product_state_errors(conn)

    print("")
    print("Wahojobs Product State Summary")
    print("==============================")
    print(f"Database: {DB_PATH}")
    print(f"User profiles: {counts['user_profiles']}")
    print(f"Pipeline items: {counts['user_pipeline_items']}")
    print(f"Applicant status updates: {counts['applicant_status_updates']}")
    print("")
    print("Pipeline statuses")
    print("-----------------")
    print_counter(pipeline_statuses)
    print("")
    print("Applicant update statuses")
    print("-------------------------")
    print_counter(update_statuses)
    print("")
    print("Applicant confidence")
    print("--------------------")
    print_counter(update_confidence)
    print("")
    if validation_errors:
        print("Validation")
        print("----------")
        for error in validation_errors:
            print(f"  ERROR: {error}")
        raise SystemExit(1)
    print("Validation: OK")


def validate_product_state(verbose=False):
    with get_connection() as conn:
        errors = validate_product_state_errors(conn)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        raise SystemExit(1)
    if verbose:
        print("Product-state validation: OK")


def validate_product_state_errors(conn):
    errors = []
    required_tables = (
        "user_profiles",
        "user_pipeline_items",
        "applicant_status_updates",
    )
    for table in required_tables:
        if not table_exists(conn, table):
            errors.append(f"Missing table: {table}")

    if errors:
        return errors

    pipeline_missing_profiles = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM user_pipeline_items upi
        LEFT JOIN user_profiles up ON up.profile_id = upi.profile_id
        WHERE up.id IS NULL
        """
    ).fetchone()["count"]
    if pipeline_missing_profiles:
        errors.append(
            f"Pipeline items referencing missing profiles: {pipeline_missing_profiles}"
        )

    updates_missing_profiles = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM applicant_status_updates asu
        LEFT JOIN user_profiles up ON up.profile_id = asu.profile_id
        WHERE up.id IS NULL
        """
    ).fetchone()["count"]
    if updates_missing_profiles:
        errors.append(
            f"Applicant updates referencing missing profiles: {updates_missing_profiles}"
        )

    return errors


def export_product_state(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with get_connection() as conn:
        payload = {
            "profiles": export_profiles(conn),
            "pipeline": export_pipeline(conn),
            "applicant_updates": export_applicant_updates(conn),
        }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def export_profiles(conn):
    rows = conn.execute(
        """
        SELECT *
        FROM user_profiles
        ORDER BY profile_id
        """
    ).fetchall()
    return [
        {
            "user_id": row["user_id"],
            "profile_id": row["profile_id"],
            "display_name": row["display_name"],
            "education_level": row["education_level"],
            "degrees_or_domains": loads_list(row["degrees_or_domains_json"]),
            "languages": loads_list(row["languages_json"]),
            "skills": loads_list(row["skills_json"]),
            "work_preferences": loads_list(row["work_preferences_json"]),
            "constraints": loads_list(row["constraints_json"]),
            "target_opportunity_types": loads_list(row["target_opportunity_types_json"]),
            "notes": row["notes"] or "",
            "is_sample": bool(row["is_sample"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def export_pipeline(conn):
    rows = conn.execute(
        """
        SELECT *
        FROM user_pipeline_items
        ORDER BY profile_id, source, opportunity_title
        """
    ).fetchall()
    return [
        {
            "pipeline_item_id": row["pipeline_item_id"],
            "user_id": row["user_id"],
            "profile_id": row["profile_id"],
            "source": row["source"],
            "title": row["opportunity_title"],
            "url": row["opportunity_url"] or "",
            "opportunity_external_id": row["opportunity_external_id"] or "",
            "canonical_id": row["canonical_id"],
            "status": row["status"],
            "status_date": row["status_date"] or "",
            "user_priority": row["user_priority"] or "",
            "reminder_date": row["reminder_date"] or "",
            "notes": row["notes"] or "",
            "last_user_action": row["last_user_action"] or "",
            "is_sample": bool(row["is_sample"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def export_applicant_updates(conn):
    rows = conn.execute(
        """
        SELECT *
        FROM applicant_status_updates
        ORDER BY reported_at, update_id
        """
    ).fetchall()
    return [
        {
            "update_id": row["update_id"],
            "user_id": row["user_id"] or "",
            "anonymous_user_key": row["anonymous_user_key"] or "",
            "profile_id": row["profile_id"],
            "source": row["source"],
            "opportunity_title": row["opportunity_title"],
            "opportunity_url": row["opportunity_url"] or "",
            "opportunity_external_id": row["opportunity_external_id"] or "",
            "canonical_id": row["canonical_id"],
            "status": row["status"],
            "previous_status": row["previous_status"] or "",
            "status_date": row["status_date"],
            "reported_at": row["reported_at"],
            "evidence_type": row["evidence_type"],
            "confidence_level": row["confidence_level"],
            "notes": row["notes"] or "",
            "is_sample": bool(row["is_sample"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def table_counts(conn):
    return {
        table: scalar(conn, f"SELECT COUNT(*) FROM {table}")
        for table in (
            "user_profiles",
            "user_pipeline_items",
            "applicant_status_updates",
        )
    }


def count_by(conn, table, field):
    return conn.execute(
        f"""
        SELECT {field} AS label, COUNT(*) AS count
        FROM {table}
        GROUP BY {field}
        ORDER BY count DESC, label ASC
        """
    ).fetchall()


def print_counter(rows):
    if not rows:
        print("  None")
        return
    for row in rows:
        print(f"  {row['label']}: {row['count']}")


def table_exists(conn, table):
    return conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = ?
        """,
        (table,),
    ).fetchone() is not None


def scalar(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()[0]


def stable_pipeline_item_id(record):
    values = [
        record["profile_id"],
        record["source"],
        record["title"],
        record.get("url") or "",
    ]
    digest = hashlib.sha1("|".join(values).encode("utf-8")).hexdigest()[:16]
    return f"pipeline::{digest}"


def dumps_list(values):
    return json.dumps(list(values or []), ensure_ascii=False, sort_keys=True)


def loads_list(value):
    try:
        loaded = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return loaded if isinstance(loaded, list) else []


def parse_optional_int(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    main()

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile

from wahojobs.crawler.providers.greenhouse import (
    GreenhouseSourceRecord,
    fetch_greenhouse_snapshot,
    validate_job_url,
)
from wahojobs.crawler.source_registry import SourceRegistryEntry
from wahojobs.crawler.types import (
    ProviderOutcome,
    evaluate_removal_authorization,
)
from wahojobs.canonical.service import sync_meridial_canonical_opportunities
from wahojobs.db.repository import create_crawl_run
from wahojobs.tracking.service import track_crawl_result


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "db" / "schema.sql"
FUTURE_ROLE_MARKER = "future roles"
GLOBAL_LOCATION_MARKERS = ("global", "worldwide", "world wide")
REGIONAL_LOCATION_MARKERS = (
    "americas",
    "amer",
    "emea",
    "apac",
    "remote us",
    "remote, us",
    "remote united states",
    "remote germany",
)
_UNSET = object()


def fetch_snapshot_sequence(entry, count=1, fetcher=fetch_greenhouse_snapshot):
    if not entry.connector_enabled_for_dry_run:
        raise PermissionError(f"{entry.registry_id} is not enabled for dry-run fetching.")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 3:
        raise ValueError("Snapshot count must be between 1 and 3.")
    config = entry.greenhouse_config()
    snapshots = []
    previous_accepted_count = entry.last_accepted_complete_count
    for _attempt in range(count):
        fetched = fetcher(config, configured_url=entry.careers_url)
        assessed = apply_count_drop_policy(
            entry,
            fetched,
            previous_accepted_count=previous_accepted_count,
        )
        snapshots.append(assessed)
        if snapshot_is_complete(assessed):
            previous_accepted_count = assessed.normalized_record_count
    return tuple(snapshots)


def apply_count_drop_policy(entry, result, *, previous_accepted_count=_UNSET):
    previous = (
        entry.last_accepted_complete_count
        if previous_accepted_count is _UNSET
        else previous_accepted_count
    )
    if not snapshot_is_complete(result) or previous is None:
        return result
    policy = entry.count_drop_policy
    current = result.normalized_record_count
    if previous < policy.minimum_previous_count:
        return result
    retained_fraction = current / previous if previous else 1.0
    if retained_fraction >= policy.minimum_retained_fraction:
        return result
    warning = (
        "Snapshot count-drop anomaly: accepted rows fell below the registry retention "
        "threshold; closures are disabled pending review."
    )
    return replace(
        result,
        outcome=ProviderOutcome.ANOMALOUS,
        source_message=f"{result.source_message} Count-drop anomaly; review required.",
        warnings=tuple((*result.warnings, warning)),
    )


def snapshot_metrics(
    entry,
    snapshots,
    *,
    relevant_external_ids=None,
    lifecycle=None,
    coverage_has_new_leakage=None,
    canonical_metrics=None,
):
    snapshots = tuple(snapshots)
    if not snapshots:
        raise ValueError("At least one snapshot is required.")
    latest = snapshots[-1]
    records = tuple(latest.source_records)
    jobs = tuple(latest.jobs)
    raw_count = latest.raw_record_count
    external_ids = [job.external_id for job in jobs if job.external_id]
    exact_titles = [str(job.title or "").strip() for job in jobs]
    normalized_titles = [normalize_exact_title(job.title) for job in jobs]
    unique_exact_titles = len(set(exact_titles))
    unique_normalized_titles = len(set(normalized_titles))
    config = entry.greenhouse_config()
    safe_urls = sum(
        1
        for job in jobs
        if job.external_id
        and validate_job_url(job.url, int(job.external_id), config) is None
    )
    global_count = sum(1 for record in records if location_scope(record.location) == "global")
    constrained_count = sum(
        1 for record in records if location_scope(record.location) == "constrained"
    )
    future_roles = sum(1 for record in records if is_future_role(record.title))
    completed = [snapshot_is_complete(item) for item in snapshots]
    count_anomaly_safe = latest.outcome != ProviderOutcome.ANOMALOUS
    closure_authorized = evaluate_removal_authorization(latest).authorized
    lifecycle_safe = bool(lifecycle and lifecycle.get("closure_safe"))
    gate_failures = []
    if not completed[-1]:
        gate_failures.append("current_snapshot_not_complete")
    if entry.acceptance_review_status != "approved":
        gate_failures.append("independent_acceptance_not_recorded")
    if not entry.terms_approved:
        gate_failures.append("company_terms_not_approved")
    if entry.consecutive_complete_snapshots < 3:
        gate_failures.append("three_consecutive_complete_snapshots_not_recorded")
    if (
        entry.temporary_closure_status != "passed"
        and not lifecycle_safe
    ):
        gate_failures.append("temporary_database_closure_not_passed")
    if (
        entry.persona_coverage_status != "passed"
        and coverage_has_new_leakage is not False
    ):
        gate_failures.append("persona_coverage_leakage_not_cleared")

    return {
        "registry_id": entry.registry_id,
        "company_id": entry.company_id,
        "company_name": entry.company_name,
        "board_identifier": entry.board_identifier,
        "technical_status": {
            "connector_technically_valid": structurally_complete(latest),
            "outcome": latest.outcome.value,
            "snapshot_structurally_complete": latest.snapshot_complete,
            "snapshot_count_anomaly_safe": count_anomaly_safe,
            "closure_authorized": closure_authorized,
            "rejected_record_count": latest.rejected_record_count,
            "fetch_attempts_this_invocation": len(snapshots),
            "accepted_attempts_this_invocation": sum(completed),
            "readiness_observations_recorded_this_invocation": 0,
        },
        "raw_record_count": raw_count,
        "accepted_source_record_count": latest.normalized_record_count,
        "relevant_posting_count": (
            len(set(relevant_external_ids))
            if relevant_external_ids is not None
            else None
        ),
        "relevant_posting_count_status": (
            "measured_persona_admission"
            if relevant_external_ids is not None
            else "unmeasured"
        ),
        "stable_identity_count": len(set(external_ids)),
        "stable_identity_rate": rate(len(set(external_ids)), raw_count),
        "exact_title_unique_count": unique_exact_titles,
        "normalized_title_unique_count": unique_normalized_titles,
        "normalized_title_repetition_rate": rate(
            len(jobs) - unique_normalized_titles, len(jobs)
        ),
        "canonical_count": (canonical_metrics or {}).get("canonical_count"),
        "canonical_yield": (canonical_metrics or {}).get("canonical_yield"),
        "canonical_yield_status": (
            (canonical_metrics or {}).get("canonical_yield_status") or "unmeasured"
        ),
        "canonical_consolidation_count": (canonical_metrics or {}).get(
            "canonical_consolidation_count"
        ),
        "canonical_duplicate_count": (canonical_metrics or {}).get(
            "canonical_duplicate_count"
        ),
        "canonicalization_input_fingerprint": (canonical_metrics or {}).get(
            "canonicalization_input_fingerprint"
        ),
        "canonicalization_output_fingerprint": (canonical_metrics or {}).get(
            "canonicalization_output_fingerprint"
        ),
        "safe_url_rate": rate(safe_urls, raw_count),
        "metadata_completeness": {
            "source_record_rate": rate(len(records), len(jobs)),
            "description_rate": rate(
                sum(bool(record.description_html) for record in records), raw_count
            ),
            "location_rate": rate(sum(bool(record.location) for record in records), raw_count),
            "departments_present_rate": rate(
                sum(bool(record.departments) for record in records), raw_count
            ),
            "offices_present_rate": rate(
                sum(bool(record.offices) for record in records), raw_count
            ),
            "updated_at_rate": rate(sum(bool(record.updated_at) for record in records), raw_count),
        },
        "freshness": freshness_summary(records),
        "location_policy": {
            "global_count": global_count,
            "regional_or_country_constrained_count": constrained_count,
            "unspecified_remote_count": len(records) - global_count - constrained_count,
            "regional_labels_are_not_worldwide": True,
        },
        "application_model": {
            "future_role_count": future_roles,
            "future_roles_treated_as_public_leads": True,
        },
        "closure_safety": lifecycle or {"closure_safe": False, "not_run": True},
        "enablement": {
            "connector_enabled_for_dry_run": entry.connector_enabled_for_dry_run,
            "product_enabled": entry.product_enabled,
            "production_crawl_enabled": entry.production_crawl_enabled,
            "terms_review_status": entry.terms_review_status,
            "acceptance_review_status": entry.acceptance_review_status,
            "historical_readiness_streak": entry.consecutive_complete_snapshots,
            "historical_readiness_observation_count": len(entry.readiness_observations),
            "current_invocation_attempts_do_not_extend_readiness": True,
            "temporary_closure_status": entry.temporary_closure_status,
            "persona_coverage_status": entry.persona_coverage_status,
            "coverage_go": coverage_has_new_leakage is False,
            "production_ready": not gate_failures,
            "production_readiness_failures": gate_failures,
        },
    }


def run_temporary_canonicalization_probe(entries, latest_results):
    report = {
        entry.registry_id: {
            "canonical_count": None,
            "canonical_yield": None,
            "canonical_yield_status": "unmeasured",
            "canonical_consolidation_count": None,
            "canonical_duplicate_count": None,
            "canonicalization_input_fingerprint": None,
            "canonicalization_output_fingerprint": None,
        }
        for entry in entries
    }
    meridial_entries = [entry for entry in entries if entry.company_id == "meridial"]
    if not meridial_entries:
        return report
    entry = meridial_entries[0]
    result = latest_results[entry.registry_id]
    if not snapshot_is_complete(result):
        return report
    with tempfile.TemporaryDirectory(prefix="wahojobs-greenhouse-canonical-") as temp_dir:
        conn = sqlite3.connect(Path(temp_dir) / "canonical.sqlite")
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            company_id = seed_temporary_companies(conn, (entry,))[entry.registry_id]
            track_snapshot(conn, company_id, result, 1)
            input_rows = conn.execute(
                "SELECT external_id, title, url FROM jobs WHERE company_id = ? ORDER BY external_id",
                (company_id,),
            ).fetchall()
            sync_meridial_canonical_opportunities(conn, company_id)
            output_rows = conn.execute(
                """
                SELECT canonical_key, canonical_title, variant_count
                FROM canonical_opportunities
                WHERE company_id = ? AND is_active = 1
                ORDER BY canonical_key
                """,
                (company_id,),
            ).fetchall()
            canonical_count = len(output_rows)
            report[entry.registry_id] = {
                "canonical_count": canonical_count,
                "canonical_yield": rate(canonical_count, len(input_rows)),
                "canonical_yield_status": "measured_temporary_database",
                "canonical_consolidation_count": len(input_rows) - canonical_count,
                "canonical_duplicate_count": sum(
                    max(int(row["variant_count"]) - 1, 0) for row in output_rows
                ),
                "canonicalization_input_fingerprint": rows_fingerprint(input_rows),
                "canonicalization_output_fingerprint": rows_fingerprint(output_rows),
            }
        finally:
            conn.close()
    return report


def run_temporary_lifecycle_probe(entries, latest_results):
    entries = tuple(entries)
    if not entries:
        return {}
    with tempfile.TemporaryDirectory(prefix="wahojobs-greenhouse-pilot-") as temp_dir:
        db_path = Path(temp_dir) / "pilot.sqlite"
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            company_ids = seed_temporary_companies(conn, entries)
            sequence = 0
            for entry in entries:
                sequence += 1
                track_snapshot(
                    conn,
                    company_ids[entry.registry_id],
                    latest_results[entry.registry_id],
                    sequence,
                )
            conn.commit()

            report = {}
            for entry in entries:
                result = latest_results[entry.registry_id]
                company_id = company_ids[entry.registry_id]
                if not snapshot_is_complete(result):
                    report[entry.registry_id] = {
                        "closure_safe": False,
                        "reason": "source_snapshot_is_not_complete",
                    }
                    continue
                if len(result.jobs) < 2:
                    report[entry.registry_id] = {
                        "closure_safe": False,
                        "reason": "at_least_two_jobs_required_for_nonempty_closure_probe",
                    }
                    continue
                initial_active = active_count(conn, company_id)
                other_before = active_counts(conn, company_ids, exclude=entry.registry_id)

                sequence += 1
                partial_summary = track_snapshot(
                    conn,
                    company_id,
                    derived_snapshot(result, remove_count=1, complete=False),
                    sequence,
                )
                active_after_partial = active_count(conn, company_id)

                sequence += 1
                complete_summary = track_snapshot(
                    conn,
                    company_id,
                    derived_snapshot(result, remove_count=1, complete=True),
                    sequence,
                )
                active_after_complete = active_count(conn, company_id)
                other_after = active_counts(conn, company_ids, exclude=entry.registry_id)
                report[entry.registry_id] = {
                    "temporary_database": True,
                    "initial_active": initial_active,
                    "partial_removed": partial_summary.jobs_removed,
                    "active_after_partial": active_after_partial,
                    "complete_removed": complete_summary.jobs_removed,
                    "active_after_complete": active_after_complete,
                    "other_sources_unchanged": other_before == other_after,
                    "closure_safe": (
                        partial_summary.jobs_removed == 0
                        and active_after_partial == initial_active
                        and complete_summary.jobs_removed == 1
                        and active_after_complete == initial_active - 1
                        and other_before == other_after
                    ),
                }
            conn.rollback()
            return report
        finally:
            conn.close()


def seed_temporary_companies(conn, entries):
    result = {}
    for entry in entries:
        cursor = conn.execute(
            """
            INSERT INTO companies (
              name, slug, careers_url,
              source_tier, inventory_model, market_count_policy
            ) VALUES (?, ?, ?, 'experimental', 'corporate_careers', 'exclude_live_estimate')
            """,
            (entry.company_name, entry.company_id, entry.careers_url),
        )
        result[entry.registry_id] = cursor.lastrowid
    conn.commit()
    return result


def track_snapshot(conn, company_id, result, sequence):
    timestamp = f"2026-07-16T12:{sequence:02d}:00+00:00"
    run_id = create_crawl_run(conn, company_id, timestamp)
    summary = track_crawl_result(conn, company_id, run_id, result, timestamp)
    conn.commit()
    return summary


def derived_snapshot(result, *, remove_count, complete):
    kept_jobs = list(result.jobs[:-remove_count])
    kept_ids = {job.external_id for job in kept_jobs}
    kept_records = tuple(
        record for record in result.source_records if record.external_id in kept_ids
    )
    return replace(
        result,
        jobs=kept_jobs,
        outcome=ProviderOutcome.SUCCESS if complete else ProviderOutcome.PARTIAL,
        snapshot_complete=complete,
        pagination_complete=complete,
        raw_record_count=len(kept_jobs),
        normalized_record_count=len(kept_jobs),
        source_records=kept_records,
        warnings=() if complete else ("Synthetic partial lifecycle probe.",),
    )


def active_count(conn, company_id):
    return conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE company_id = ? AND is_active = 1",
        (company_id,),
    ).fetchone()[0]


def active_counts(conn, company_ids, *, exclude):
    return {
        registry_id: active_count(conn, company_id)
        for registry_id, company_id in company_ids.items()
        if registry_id != exclude
    }


def snapshot_is_complete(result):
    return bool(
        result.outcome == ProviderOutcome.SUCCESS
        and result.snapshot_complete
        and result.pagination_complete
        and result.rejected_record_count == 0
        and result.normalized_record_count == len(result.jobs)
        and result.jobs
    )


def structurally_complete(result):
    return bool(
        result.snapshot_complete
        and result.pagination_complete
        and result.rejected_record_count == 0
        and result.normalized_record_count == len(result.jobs)
        and result.jobs
        and result.outcome not in {ProviderOutcome.PARTIAL, ProviderOutcome.CONTRACT_DRIFT}
    )


def is_future_role(title):
    return FUTURE_ROLE_MARKER in str(title or "").casefold()


def location_scope(location):
    normalized = " ".join(str(location or "").casefold().replace("-", " ").split())
    if any(marker in normalized for marker in GLOBAL_LOCATION_MARKERS):
        return "global"
    if any(marker in normalized for marker in REGIONAL_LOCATION_MARKERS):
        return "constrained"
    if "remote" in normalized and normalized != "remote":
        return "constrained"
    return "unspecified_remote"


def freshness_summary(records):
    values = sorted(record.updated_at for record in records if record.updated_at)
    return {
        "oldest_source_updated_at": values[0] if values else None,
        "newest_source_updated_at": values[-1] if values else None,
        "updated_timestamp_preserved": len(values) == len(records),
    }


def normalize_exact_title(value):
    return " ".join(str(value or "").casefold().split())


def rate(numerator, denominator):
    if not denominator:
        return 0.0
    return round(numerator / denominator, 4)


def rows_fingerprint(rows):
    payload = [list(row) for row in rows]
    serialized = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def source_record_to_dict(record: GreenhouseSourceRecord):
    return {
        "source_name": record.source_name,
        "company_id": record.company_id,
        "board_token": record.board_token,
        "greenhouse_job_id": record.greenhouse_job_id,
        "external_id": record.external_id,
        "title": record.title,
        "url": record.url,
        "application_url": record.application_url,
        "location": record.location,
        "additional_locations": list(record.additional_locations),
        "description_html": record.description_html,
        "updated_at": record.updated_at,
        "internal_job_id": record.internal_job_id,
        "requisition_id": record.requisition_id,
        "first_published": record.first_published,
        "application_deadline": record.application_deadline,
        "language": record.language,
        "company_name": record.company_name,
        "metadata": json.loads(record.metadata_json),
        "education": json.loads(record.education_json),
        "data_compliance": json.loads(record.compliance_json),
        "compensation": json.loads(record.compensation_json),
        "raw_public_payload": json.loads(record.raw_public_payload_json),
        "departments": [
            {
                "id": item.department_id,
                "name": item.name,
                "parent_id": item.parent_id,
                "child_ids": list(item.child_ids),
            }
            for item in record.departments
        ],
        "offices": [
            {
                "id": item.office_id,
                "name": item.name,
                "location": item.location,
                "parent_id": item.parent_id,
                "child_ids": list(item.child_ids),
            }
            for item in record.offices
        ],
    }


def dump_source_records(result):
    return json.dumps(
        [source_record_to_dict(record) for record in result.source_records],
        indent=2,
        sort_keys=True,
    )

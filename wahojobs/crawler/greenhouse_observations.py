from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import threading
from urllib.error import HTTPError, URLError
import uuid

from wahojobs.crawler.source_registry import (
    ACCEPTANCE_REVIEW_STATUSES,
    SUPPORTED_PARSER_VERSIONS,
    TERMS_REVIEW_STATUSES,
    VALIDATION_STATUSES,
    SourceRegistryEntry,
)


BUNDLE_SCHEMA_VERSION = "greenhouse_pilot_observation_bundle_v2"
RECEIPT_SCHEMA_VERSION = "greenhouse_pilot_observation_receipt_v1"
READINESS_REPORT_VERSION = "greenhouse_pilot_operational_readiness_v1"
TOOL_VERSION = "greenhouse-pilot-observation-ledger-v1"
COMMAND_MODE = "record_observation"
LOCK_NAME = ".ledger-lock"
BUNDLES_DIR_NAME = "bundles"
RECEIPTS_DIR_NAME = "receipts"
WORKING_DIR_NAME = "working"
MAX_BOARDS_PER_INVOCATION = 64
MAX_CODES_PER_BOARD = 64
MAX_CODE_LENGTH = 96
MAX_SAFE_DIAGNOSTIC_TEXT_LENGTH = 512
MAX_BOUNDED_METRIC_ENTRIES = 128
MAX_IDENTIFIER_LENGTH = 128
MAX_BUNDLE_BYTES = 256 * 1024
MAX_RECEIPT_BYTES = 16 * 1024
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PREFIXED_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
ID_PATTERN = re.compile(
    r"^(?:ledger|run|bundle|receipt|observation)_[0-9a-f]{32}$"
)
SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
DIAGNOSTIC_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,95}$")
WORKING_FILE_PATTERN = re.compile(
    r"^staging_(?:bundle|receipt)_(?:bundle|receipt)_[0-9a-f]{32}_[0-9a-f]{32}\.tmp$"
)
SAFE_FAILURE_MESSAGES = {
    "network_timeout": "The board request timed out.",
    "http_failure": "The board request failed.",
    "contract_validation_failure": (
        "The board response did not satisfy the Greenhouse contract."
    ),
    "unexpected_board_failure": "The board could not be processed.",
    "registry_or_configuration_invalid": "The pilot configuration is invalid.",
    "observation_recording_failed": "The observation could not be recorded.",
    "observation_history_invalid": (
        "The observation history failed integrity verification."
    ),
    "ledger_clock_not_monotonic": (
        "The ledger clock is not monotonic; retry after the clock advances."
    ),
    "invocation_aborted_before_metrics": (
        "The board was not measured because the invocation stopped early."
    ),
}
SNAPSHOT_OUTCOMES = frozenset(
    {"success", "partial", "anomalous", "failed", "contract_drift"}
)
METRICS_STATUSES = frozenset({"measured", "unmeasured"})
LIFECYCLE_STATUSES = frozenset({"passed", "failed", "not_run"})
CANONICAL_STATUSES = frozenset({"measured_temporary_database", "unmeasured"})
COVERAGE_STATUSES = frozenset({"measured_28_persona_comparison", "unmeasured"})
METADATA_FIELDS = {
    "source_record_rate",
    "description_rate",
    "location_rate",
    "departments_present_rate",
    "offices_present_rate",
    "updated_at_rate",
}
LIFECYCLE_FIELDS = {
    "status",
    "temporary_database",
    "initial_active",
    "partial_removed",
    "active_after_partial",
    "complete_removed",
    "active_after_complete",
    "other_sources_unchanged",
    "closure_safe",
    "diagnostic_codes",
}
CANONICAL_FIELDS = {
    "status",
    "canonical_count",
    "canonical_yield",
    "consolidation_count",
    "duplicate_count",
    "input_fingerprint",
    "output_fingerprint",
}
COVERAGE_FIELDS = {
    "status",
    "persona_count",
    "relevant_posting_count",
    "new_eligibility_leakage_count",
    "current_comparison_no_new_leakage",
    "admitted_result_delta",
    "strong_family_delta",
    "company_diversity_delta",
    "regional_rejection_count",
    "regional_rejection_delta",
    "qualification_rejection_count",
    "qualification_rejection_delta",
    "new_titles",
    "new_titles_truncated",
    "new_titles_omitted_count",
    "new_titles_omitted_sha256",
    "new_companies",
    "new_companies_truncated",
    "new_companies_omitted_count",
    "new_companies_omitted_sha256",
    "coverage_approved",
}
OBSERVATION_FIELDS = {
    "observation_id",
    "run_id",
    "registry_id",
    "company_id",
    "board_identifier",
    "ats_provider",
    "parser_version",
    "registry_sha256",
    "registry_contract_sha256",
    "observed_at",
    "metrics_status",
    "raw_record_count",
    "accepted_source_record_count",
    "stable_identity_count",
    "stable_identity_rate",
    "safe_url_count",
    "safe_url_rate",
    "exact_title_unique_count",
    "normalized_title_unique_count",
    "normalized_title_repetition_rate",
    "metadata_completeness",
    "snapshot_outcome",
    "snapshot_structurally_complete",
    "snapshot_count_anomaly_safe",
    "prior_accepted_count_used_for_anomaly_check",
    "closure_authorized",
    "lifecycle_probe",
    "technical_success",
    "operational_failure_reasons",
    "canonical_yield",
    "coverage",
    "connector_enabled_for_dry_run",
    "terms_review_status",
    "acceptance_review_status",
    "temporary_closure_status",
    "persona_coverage_status",
    "product_enabled",
    "production_crawl_enabled",
}
BUNDLE_FIELDS = {
    "schema_version",
    "ledger_id",
    "ledger_sequence",
    "bundle_id",
    "run_id",
    "started_at",
    "completed_at",
    "tool_version",
    "command_mode",
    "registry_sha256",
    "source_report_sha256",
    "invocation_fingerprint",
    "requested_registry_ids",
    "parser_versions",
    "previous_bundle_sha256",
    "invocation_status",
    "observations",
    "bundle_content_sha256",
}
RECEIPT_FIELDS = {
    "schema_version",
    "ledger_id",
    "ledger_sequence",
    "receipt_id",
    "bundle_id",
    "run_id",
    "bundle_sha256",
    "previous_receipt_sha256",
    "previous_bundle_sha256",
    "published_at",
    "receipt_content_sha256",
}
INVOCATION_STATUSES = frozenset(
    {"complete_success", "partial_success", "complete_failure"}
)


class ObservationLedgerError(ValueError):
    pass


class ObservationValidationError(ObservationLedgerError):
    pass


class ObservationClockError(ObservationValidationError):
    pass


class ObservationConflictError(ObservationLedgerError):
    pass


class ObservationLockError(ObservationLedgerError):
    pass


class ObservationIncompletePublicationError(ObservationLedgerError):
    pass


@dataclass(frozen=True)
class RecordedObservationBundle:
    path: Path
    receipt_path: Path
    ledger_id: str
    ledger_sequence: int
    bundle_id: str
    receipt_id: str
    run_id: str
    bundle_content_sha256: str
    receipt_content_sha256: str
    invocation_status: str
    observation_ids: tuple[str, ...]


@dataclass(frozen=True)
class VerifiedObservationHistory:
    ledger_id: str | None
    bundles: tuple[dict, ...]
    receipts: tuple[dict, ...]
    working_residue_count: int = 0
    working_residue_fingerprint: str | None = None


_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.Lock] = {}


def canonical_json(value) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_value(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def format_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ObservationValidationError("Observation timestamps must be timezone-aware.")
    normalized = value.astimezone(timezone.utc)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_utc(value, label) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise ObservationValidationError(f"{label} must be a canonical UTC timestamp.")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ObservationValidationError(f"{label} is not a valid UTC timestamp.") from exc
    if format_utc(parsed) != value:
        raise ObservationValidationError(f"{label} must use canonical UTC formatting.")
    return parsed


def safe_failure_diagnostic(error=None, *, default_code=None):
    """Return an allowlisted failure result without inspecting exception text."""
    if isinstance(error, ObservationClockError):
        code = "ledger_clock_not_monotonic"
    elif default_code is not None:
        code = default_code
    elif isinstance(error, (TimeoutError, socket.timeout)):
        code = "network_timeout"
    elif isinstance(error, (HTTPError, URLError)):
        code = "http_failure"
    elif isinstance(error, (ObservationValidationError, json.JSONDecodeError, ValueError)):
        code = "contract_validation_failure"
    elif type(error) is dict:
        candidate = error.get("failure_code")
        code = candidate if candidate in SAFE_FAILURE_MESSAGES else "unexpected_board_failure"
    else:
        code = "unexpected_board_failure"
    if code not in SAFE_FAILURE_MESSAGES:
        raise ObservationValidationError("Failure diagnostic code is not allowlisted.")
    message = SAFE_FAILURE_MESSAGES[code]
    if len(code) > MAX_CODE_LENGTH or not DIAGNOSTIC_PATTERN.fullmatch(code):
        raise ObservationValidationError("Failure diagnostic code is malformed.")
    if len(message) > MAX_SAFE_DIAGNOSTIC_TEXT_LENGTH:
        raise ObservationValidationError("Failure diagnostic message is oversized.")
    return {"failure_code": code, "safe_message": message}


def registry_contract_sha256(entry: SourceRegistryEntry) -> str:
    contract = {
        "registry_id": entry.registry_id,
        "company_id": entry.company_id,
        "source_family": entry.source_family,
        "ats_provider": entry.ats_provider,
        "board_identifier": entry.board_identifier,
        "careers_url": entry.careers_url,
        "allowed_job_hosts": list(entry.allowed_job_hosts),
        "target_families": list(entry.target_families),
        "target_countries": list(entry.target_countries),
        "target_languages": list(entry.target_languages),
        "crawl_cadence_hours": entry.crawl_cadence_hours,
        "freshness_sla_hours": entry.freshness_sla_hours,
        "parser_version": entry.parser_version,
        "count_drop_policy": {
            "minimum_previous_count": entry.count_drop_policy.minimum_previous_count,
            "minimum_retained_fraction": entry.count_drop_policy.minimum_retained_fraction,
        },
        "root_department_id": entry.root_department_id,
    }
    return sha256_value(contract)


def compute_bundle_sha256(bundle) -> str:
    unsigned = dict(bundle)
    unsigned.pop("bundle_content_sha256", None)
    return sha256_value(unsigned)


def compute_receipt_sha256(receipt) -> str:
    unsigned = dict(receipt)
    unsigned.pop("receipt_content_sha256", None)
    return sha256_value(unsigned)


def expected_bundle_filename(bundle) -> str:
    return f"{bundle['ledger_sequence']:06d}--{bundle['bundle_id']}.json"


def expected_receipt_filename(receipt) -> str:
    return f"{receipt['ledger_sequence']:06d}--{receipt['receipt_id']}.json"


def build_observation_bundle(
    *,
    report,
    entries,
    started_at,
    completed_at,
    registry_sha256,
    previous_bundle_sha256,
    ledger_sequence,
    ledger_id=None,
    run_id=None,
    bundle_id=None,
):
    entries = tuple(entries)
    if len(entries) > MAX_BOARDS_PER_INVOCATION:
        raise ObservationValidationError(
            f"An observation invocation may contain at most {MAX_BOARDS_PER_INVOCATION} boards."
        )
    by_registry = {entry.registry_id: entry for entry in entries}
    requested = sorted(by_registry)
    ledger_id = ledger_id or f"ledger_{uuid.uuid4().hex}"
    run_id = run_id or f"run_{uuid.uuid4().hex}"
    bundle_id = bundle_id or f"bundle_{uuid.uuid4().hex}"
    started_text = format_utc(started_at)
    completed_text = format_utc(completed_at)
    observed_text = _report_observed_at(report, started_at, completed_at)
    source_report_sha256 = sha256_value(report)
    invocation_fingerprint = sha256_value(
        {
            "started_at": started_text,
            "completed_at": completed_text,
            "requested_registry_ids": requested,
            "source_report_sha256": source_report_sha256,
        }
    )
    sources = {source["registry_id"]: source for source in report.get("sources") or []}
    failures = report.get("board_failures") or {}
    observations = []
    for entry in sorted(entries, key=lambda item: item.registry_id):
        source = sources.get(entry.registry_id)
        if source is None:
            observations.append(
                _failed_observation(
                    entry,
                    run_id=run_id,
                    registry_sha256=registry_sha256,
                    observed_at=observed_text,
                    failure=failures.get(entry.registry_id),
                )
            )
        else:
            observations.append(
                _measured_observation(
                    entry,
                    source,
                    report,
                    run_id=run_id,
                    registry_sha256=registry_sha256,
                    observed_at=observed_text,
                )
            )
    successes = sum(bool(item["technical_success"]) for item in observations)
    if successes == len(observations):
        invocation_status = "complete_success"
    elif successes:
        invocation_status = "partial_success"
    else:
        invocation_status = "complete_failure"
    bundle = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "ledger_id": ledger_id,
        "ledger_sequence": ledger_sequence,
        "bundle_id": bundle_id,
        "run_id": run_id,
        "started_at": started_text,
        "completed_at": completed_text,
        "tool_version": TOOL_VERSION,
        "command_mode": COMMAND_MODE,
        "registry_sha256": registry_sha256,
        "source_report_sha256": source_report_sha256,
        "invocation_fingerprint": invocation_fingerprint,
        "requested_registry_ids": requested,
        "parser_versions": sorted({item["parser_version"] for item in observations}),
        "previous_bundle_sha256": previous_bundle_sha256,
        "invocation_status": invocation_status,
        "observations": observations,
        "bundle_content_sha256": "",
    }
    bundle["bundle_content_sha256"] = compute_bundle_sha256(bundle)
    validate_observation_bundle(bundle, registry_entries=entries)
    return bundle


def build_observation_receipt(
    *,
    bundle,
    previous_receipt_sha256,
    published_at,
    receipt_id=None,
):
    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "ledger_id": bundle["ledger_id"],
        "ledger_sequence": bundle["ledger_sequence"],
        "receipt_id": receipt_id or f"receipt_{uuid.uuid4().hex}",
        "bundle_id": bundle["bundle_id"],
        "run_id": bundle["run_id"],
        "bundle_sha256": bundle["bundle_content_sha256"],
        "previous_receipt_sha256": previous_receipt_sha256,
        "previous_bundle_sha256": bundle["previous_bundle_sha256"],
        "published_at": format_utc(published_at),
        "receipt_content_sha256": "",
    }
    receipt["receipt_content_sha256"] = compute_receipt_sha256(receipt)
    validate_observation_receipt(receipt, bundle=bundle)
    return receipt


def _report_observed_at(report, started_at, completed_at):
    value = report.get("evaluated_at")
    if value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ObservationValidationError("Dry-run report has an invalid evaluated_at.") from exc
    else:
        parsed = completed_at
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ObservationValidationError("Dry-run evaluated_at must be timezone-aware.")
    parsed = parsed.astimezone(timezone.utc)
    if not started_at.astimezone(timezone.utc) <= parsed <= completed_at.astimezone(timezone.utc):
        raise ObservationValidationError(
            "Dry-run evaluated_at must fall within the recorded invocation interval."
        )
    return format_utc(parsed)


def _measured_observation(entry, source, report, *, run_id, registry_sha256, observed_at):
    technical = source["technical_status"]
    enablement = source["enablement"]
    metadata = dict(source["metadata_completeness"])
    lifecycle = _normalize_lifecycle(source.get("closure_safety"))
    canonical = _normalize_canonical(source)
    coverage = _coverage_for_source(entry, source, report)
    technical_success = bool(
        technical["connector_technically_valid"]
        and technical["snapshot_structurally_complete"]
        and technical["snapshot_count_anomaly_safe"]
        and technical["outcome"] == "success"
        and technical["closure_authorized"]
    )
    reasons = list(enablement.get("production_readiness_failures") or [])
    if not technical_success and "current_snapshot_not_complete" not in reasons:
        reasons.insert(0, "current_snapshot_not_complete")
    raw_count = source["raw_record_count"]
    safe_url_count = source.get("safe_url_count")
    if safe_url_count is None:
        safe_url_count = int(round(float(source["safe_url_rate"]) * raw_count))
    return {
        "observation_id": f"observation_{uuid.uuid5(uuid.NAMESPACE_URL, run_id + ':' + entry.registry_id).hex}",
        "run_id": run_id,
        "registry_id": entry.registry_id,
        "company_id": entry.company_id,
        "board_identifier": entry.board_identifier,
        "ats_provider": entry.ats_provider,
        "parser_version": entry.parser_version,
        "registry_sha256": registry_sha256,
        "registry_contract_sha256": registry_contract_sha256(entry),
        "observed_at": observed_at,
        "metrics_status": "measured",
        "raw_record_count": raw_count,
        "accepted_source_record_count": source["accepted_source_record_count"],
        "stable_identity_count": source["stable_identity_count"],
        "stable_identity_rate": source["stable_identity_rate"],
        "safe_url_count": safe_url_count,
        "safe_url_rate": source["safe_url_rate"],
        "exact_title_unique_count": source["exact_title_unique_count"],
        "normalized_title_unique_count": source["normalized_title_unique_count"],
        "normalized_title_repetition_rate": source["normalized_title_repetition_rate"],
        "metadata_completeness": metadata,
        "snapshot_outcome": technical["outcome"],
        "snapshot_structurally_complete": technical["snapshot_structurally_complete"],
        "snapshot_count_anomaly_safe": technical["snapshot_count_anomaly_safe"],
        "prior_accepted_count_used_for_anomaly_check": technical.get(
            "prior_accepted_count_used_for_latest_anomaly_check"
        ),
        "closure_authorized": technical["closure_authorized"],
        "lifecycle_probe": lifecycle,
        "technical_success": technical_success,
        "operational_failure_reasons": _bounded_codes(reasons),
        "canonical_yield": canonical,
        "coverage": coverage,
        "connector_enabled_for_dry_run": entry.connector_enabled_for_dry_run,
        "terms_review_status": entry.terms_review_status,
        "acceptance_review_status": entry.acceptance_review_status,
        "temporary_closure_status": entry.temporary_closure_status,
        "persona_coverage_status": entry.persona_coverage_status,
        "product_enabled": entry.product_enabled,
        "production_crawl_enabled": entry.production_crawl_enabled,
    }


def _failed_observation(entry, *, run_id, registry_sha256, observed_at, failure):
    diagnostic = safe_failure_diagnostic(
        failure,
        default_code=None if failure else "invocation_aborted_before_metrics",
    )["failure_code"]
    return {
        "observation_id": f"observation_{uuid.uuid5(uuid.NAMESPACE_URL, run_id + ':' + entry.registry_id).hex}",
        "run_id": run_id,
        "registry_id": entry.registry_id,
        "company_id": entry.company_id,
        "board_identifier": entry.board_identifier,
        "ats_provider": entry.ats_provider,
        "parser_version": entry.parser_version,
        "registry_sha256": registry_sha256,
        "registry_contract_sha256": registry_contract_sha256(entry),
        "observed_at": observed_at,
        "metrics_status": "unmeasured",
        "raw_record_count": None,
        "accepted_source_record_count": None,
        "stable_identity_count": None,
        "stable_identity_rate": None,
        "safe_url_count": None,
        "safe_url_rate": None,
        "exact_title_unique_count": None,
        "normalized_title_unique_count": None,
        "normalized_title_repetition_rate": None,
        "metadata_completeness": {key: None for key in sorted(METADATA_FIELDS)},
        "snapshot_outcome": "failed",
        "snapshot_structurally_complete": False,
        "snapshot_count_anomaly_safe": None,
        "prior_accepted_count_used_for_anomaly_check": entry.last_accepted_complete_count,
        "closure_authorized": False,
        "lifecycle_probe": _empty_lifecycle("not_run", [diagnostic]),
        "technical_success": False,
        "operational_failure_reasons": [diagnostic],
        "canonical_yield": _empty_canonical(),
        "coverage": _empty_coverage(entry),
        "connector_enabled_for_dry_run": entry.connector_enabled_for_dry_run,
        "terms_review_status": entry.terms_review_status,
        "acceptance_review_status": entry.acceptance_review_status,
        "temporary_closure_status": entry.temporary_closure_status,
        "persona_coverage_status": entry.persona_coverage_status,
        "product_enabled": entry.product_enabled,
        "production_crawl_enabled": entry.production_crawl_enabled,
    }


def _normalize_lifecycle(value):
    value = value or {}
    if value.get("not_run"):
        return _empty_lifecycle("not_run", [])
    closure_safe = bool(value.get("closure_safe"))
    diagnostic_codes = []
    if value.get("reason"):
        diagnostic_codes.append(str(value["reason"]))
    return {
        "status": "passed" if closure_safe else "failed",
        "temporary_database": bool(value.get("temporary_database", True)),
        "initial_active": value.get("initial_active"),
        "partial_removed": value.get("partial_removed"),
        "active_after_partial": value.get("active_after_partial"),
        "complete_removed": value.get("complete_removed"),
        "active_after_complete": value.get("active_after_complete"),
        "other_sources_unchanged": value.get("other_sources_unchanged"),
        "closure_safe": closure_safe,
        "diagnostic_codes": _bounded_codes(diagnostic_codes),
    }


def _empty_lifecycle(status, diagnostics):
    return {
        "status": status,
        "temporary_database": False,
        "initial_active": None,
        "partial_removed": None,
        "active_after_partial": None,
        "complete_removed": None,
        "active_after_complete": None,
        "other_sources_unchanged": None,
        "closure_safe": False,
        "diagnostic_codes": _bounded_codes(diagnostics),
    }


def _normalize_canonical(source):
    status = source.get("canonical_yield_status") or "unmeasured"
    return {
        "status": status,
        "canonical_count": source.get("canonical_count"),
        "canonical_yield": source.get("canonical_yield"),
        "consolidation_count": source.get("canonical_consolidation_count"),
        "duplicate_count": source.get("canonical_duplicate_count"),
        "input_fingerprint": source.get("canonicalization_input_fingerprint"),
        "output_fingerprint": source.get("canonicalization_output_fingerprint"),
    }


def _empty_canonical():
    return {
        "status": "unmeasured",
        "canonical_count": None,
        "canonical_yield": None,
        "consolidation_count": None,
        "duplicate_count": None,
        "input_fingerprint": None,
        "output_fingerprint": None,
    }


def _coverage_for_source(entry, source, report):
    coverage = report.get("persona_coverage")
    if not coverage:
        return _empty_coverage(entry)
    board = (coverage.get("per_board") or {}).get(entry.registry_id, {})
    totals = {
        "admitted_result_delta": 0,
        "strong_family_delta": 0,
        "company_diversity_delta": 0,
        "regional_rejection_count": 0,
        "regional_rejection_delta": 0,
        "qualification_rejection_count": 0,
        "qualification_rejection_delta": 0,
    }
    titles = set()
    companies = set()
    for persona in coverage.get("persona_deltas") or []:
        metrics = (persona.get("per_board_metrics") or {}).get(entry.registry_id)
        if not metrics:
            continue
        for key in totals:
            source_key = {
                "qualification_rejection_count": "credential_language_seniority_rejection_count",
                "qualification_rejection_delta": "credential_language_seniority_rejection_delta",
            }.get(key, key)
            totals[key] += int(metrics.get(source_key) or 0)
        titles.update(str(value) for value in metrics.get("new_titles") or [])
        companies.update(str(value) for value in metrics.get("new_companies") or [])
    title_values, title_truncation = _bounded_strings(titles)
    company_values, company_truncation = _bounded_strings(companies)
    leak_count = int(board.get("new_eligibility_leakage_count") or 0)
    return {
        "status": "measured_28_persona_comparison",
        "persona_count": int(coverage["persona_count"]),
        "relevant_posting_count": source.get("relevant_posting_count"),
        "new_eligibility_leakage_count": leak_count,
        "current_comparison_no_new_leakage": leak_count == 0,
        **totals,
        "new_titles": title_values,
        "new_titles_truncated": title_truncation["truncated"],
        "new_titles_omitted_count": title_truncation["omitted_count"],
        "new_titles_omitted_sha256": title_truncation["omitted_sha256"],
        "new_companies": company_values,
        "new_companies_truncated": company_truncation["truncated"],
        "new_companies_omitted_count": company_truncation["omitted_count"],
        "new_companies_omitted_sha256": company_truncation["omitted_sha256"],
        "coverage_approved": entry.persona_coverage_status == "passed",
    }


def _empty_coverage(entry):
    return {
        "status": "unmeasured",
        "persona_count": None,
        "relevant_posting_count": None,
        "new_eligibility_leakage_count": None,
        "current_comparison_no_new_leakage": None,
        "admitted_result_delta": None,
        "strong_family_delta": None,
        "company_diversity_delta": None,
        "regional_rejection_count": None,
        "regional_rejection_delta": None,
        "qualification_rejection_count": None,
        "qualification_rejection_delta": None,
        "new_titles": [],
        "new_titles_truncated": False,
        "new_titles_omitted_count": 0,
        "new_titles_omitted_sha256": None,
        "new_companies": [],
        "new_companies_truncated": False,
        "new_companies_omitted_count": 0,
        "new_companies_omitted_sha256": None,
        "coverage_approved": entry.persona_coverage_status == "passed",
    }


def _bounded_strings(values, limit=MAX_BOUNDED_METRIC_ENTRIES):
    cleaned = []
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        if len(text) > MAX_SAFE_DIAGNOSTIC_TEXT_LENGTH:
            raise ObservationValidationError(
                "A retained metric label exceeds the safe diagnostic text limit."
            )
        cleaned.append(text)
    ordered = sorted(set(cleaned), key=lambda item: (item.casefold(), item))
    omitted = ordered[limit:]
    return ordered[:limit], {
        "truncated": bool(omitted),
        "omitted_count": len(omitted),
        "omitted_sha256": sha256_value(omitted) if omitted else None,
    }


def _unique_strings(values):
    result = []
    for value in values:
        text = str(value)
        if text not in result:
            result.append(text)
    return result


def _bounded_codes(values):
    result = _unique_strings(values)
    if len(result) > MAX_CODES_PER_BOARD:
        raise ObservationValidationError(
            f"A board observation may contain at most {MAX_CODES_PER_BOARD} diagnostic codes."
        )
    for value in result:
        if len(value) > MAX_CODE_LENGTH or not DIAGNOSTIC_PATTERN.fullmatch(value):
            raise ObservationValidationError("A diagnostic code is malformed or oversized.")
    return result


def record_observation_bundle(
    directory,
    *,
    report,
    entries,
    registry_entries=None,
    started_at,
    completed_at,
    registry_path,
    failure_injector=None,
    run_id=None,
    bundle_id=None,
    receipt_id=None,
    published_at=None,
):
    directory = Path(directory)
    entries = tuple(entries)
    registry_entries = tuple(registry_entries or entries)
    _inject(failure_injector, "before_directory_creation")
    _prepare_ledger_root(directory)
    with _ledger_lock(directory, create=True):
        _inject(failure_injector, "after_lock_acquisition")
        if _ledger_contains_artifacts(directory):
            history = _load_verified_ledger_unlocked(
                directory, registry_entries=registry_entries
            )
            _prepare_artifact_directories(directory)
        else:
            _validate_ledger_layout(directory)
            _prepare_artifact_directories(directory)
            history = VerifiedObservationHistory(None, (), ())
        _cleanup_working_residue(directory)
        previous_bundle = (
            history.bundles[-1]["bundle_content_sha256"]
            if history.bundles
            else None
        )
        previous_receipt = (
            history.receipts[-1]["receipt_content_sha256"]
            if history.receipts
            else None
        )
        ledger_id = history.ledger_id or f"ledger_{uuid.uuid4().hex}"
        bundle = build_observation_bundle(
            report=report,
            entries=entries,
            started_at=started_at,
            completed_at=completed_at,
            registry_sha256=sha256_file(registry_path),
            previous_bundle_sha256=previous_bundle,
            ledger_sequence=len(history.bundles) + 1,
            ledger_id=ledger_id,
            run_id=run_id,
            bundle_id=bundle_id,
        )
        publication_time = published_at or datetime.now(timezone.utc)
        receipt = build_observation_receipt(
            bundle=bundle,
            previous_receipt_sha256=previous_receipt,
            published_at=publication_time,
            receipt_id=receipt_id,
        )
        _validate_append_plan(
            history,
            bundle,
            receipt,
            registry_entries=registry_entries,
        )
        _inject(failure_injector, "before_final_serialization")
        path = _publish_artifact(
            directory / BUNDLES_DIR_NAME,
            directory / WORKING_DIR_NAME,
            expected_bundle_filename(bundle),
            bundle["bundle_id"],
            bundle,
            max_bytes=MAX_BUNDLE_BYTES,
            artifact_kind="bundle",
            failure_injector=failure_injector,
        )
        try:
            _inject(failure_injector, "after_bundle_publication")
            receipt_path = _publish_artifact(
                directory / RECEIPTS_DIR_NAME,
                directory / WORKING_DIR_NAME,
                expected_receipt_filename(receipt),
                receipt["receipt_id"],
                receipt,
                max_bytes=MAX_RECEIPT_BYTES,
                artifact_kind="receipt",
                failure_injector=failure_injector,
            )
        except Exception as exc:
            raise ObservationIncompletePublicationError(
                "Observation bundle was published without its receipt; history requires reviewed recovery."
            ) from exc
        _fsync_directory(directory)
        _inject(failure_injector, "after_receipt_publication")
        _inject(failure_injector, "after_publication_before_reporting")
    return RecordedObservationBundle(
        path=path,
        receipt_path=receipt_path,
        ledger_id=ledger_id,
        ledger_sequence=bundle["ledger_sequence"],
        bundle_id=bundle["bundle_id"],
        receipt_id=receipt["receipt_id"],
        run_id=bundle["run_id"],
        bundle_content_sha256=bundle["bundle_content_sha256"],
        receipt_content_sha256=receipt["receipt_content_sha256"],
        invocation_status=bundle["invocation_status"],
        observation_ids=tuple(item["observation_id"] for item in bundle["observations"]),
    )


def _reject_replay(history, bundle, receipt):
    run_ids = {item["run_id"] for item in history.bundles}
    bundle_ids = {item["bundle_id"] for item in history.bundles}
    receipt_ids = {item["receipt_id"] for item in history.receipts}
    fingerprints = {item["invocation_fingerprint"] for item in history.bundles}
    observation_ids = {
        observation["observation_id"]
        for item in history.bundles
        for observation in item["observations"]
    }
    if bundle["run_id"] in run_ids:
        raise ObservationConflictError("Observation run ID already exists.")
    if bundle["bundle_id"] in bundle_ids:
        raise ObservationConflictError("Observation bundle ID already exists.")
    if receipt["receipt_id"] in receipt_ids:
        raise ObservationConflictError("Observation receipt ID already exists.")
    if bundle["invocation_fingerprint"] in fingerprints:
        raise ObservationConflictError("This invocation result is already recorded.")
    if observation_ids.intersection(
        item["observation_id"] for item in bundle["observations"]
    ):
        raise ObservationConflictError("Observation ID already exists.")


def _validate_append_plan(history, bundle, receipt, *, registry_entries):
    validate_observation_bundle(bundle, registry_entries=registry_entries)
    validate_observation_receipt(receipt, bundle=bundle)
    _validate_append_chronology(history, bundle)
    if history.bundles:
        previous_bundle = history.bundles[-1]
        previous_receipt = history.receipts[-1]
        if bundle["ledger_sequence"] != previous_bundle["ledger_sequence"] + 1:
            raise ObservationValidationError("Observation append sequence is not contiguous.")
        if receipt["ledger_sequence"] != bundle["ledger_sequence"]:
            raise ObservationValidationError("Observation receipt sequence is inconsistent.")
        if bundle["ledger_id"] != previous_bundle["ledger_id"]:
            raise ObservationValidationError("Observation append changes ledger identity.")
        if receipt["ledger_id"] != previous_receipt["ledger_id"]:
            raise ObservationValidationError("Observation receipt changes ledger identity.")
        if bundle["previous_bundle_sha256"] != previous_bundle["bundle_content_sha256"]:
            raise ObservationValidationError("Observation append does not reference the ledger head.")
        if receipt["previous_bundle_sha256"] != previous_bundle["bundle_content_sha256"]:
            raise ObservationValidationError("Observation receipt does not reference the bundle head.")
        if receipt["previous_receipt_sha256"] != previous_receipt["receipt_content_sha256"]:
            raise ObservationValidationError("Observation receipt does not reference the receipt head.")
        published = parse_utc(receipt["published_at"], "published_at")
        previous_published = parse_utc(
            previous_receipt["published_at"], "published_at"
        )
        if published <= previous_published:
            raise ObservationClockError(
                "Observation receipt publication time must increase."
            )
    elif bundle["ledger_sequence"] != 1 or receipt["ledger_sequence"] != 1:
        raise ObservationValidationError("A new observation ledger must start at sequence one.")
    _reject_replay(history, bundle, receipt)


def _publish_artifact(
    directory,
    working_directory,
    filename,
    artifact_id,
    payload,
    *,
    max_bytes,
    artifact_kind,
    failure_injector=None,
):
    final_path = Path(directory) / filename
    temp_path = Path(working_directory) / (
        f"staging_{artifact_kind}_{artifact_id}_{uuid.uuid4().hex}.tmp"
    )
    encoded = (canonical_json(payload) + "\n").encode("utf-8")
    if len(encoded) > max_bytes:
        raise ObservationValidationError(
            f"Serialized observation {artifact_kind} exceeds its size limit."
        )
    fd = None
    published = False
    try:
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            fd = None
            midpoint = max(1, len(encoded) // 2)
            handle.write(encoded[:midpoint])
            handle.flush()
            _inject(failure_injector, "during_file_write")
            _inject(failure_injector, f"during_{artifact_kind}_file_write")
            handle.write(encoded[midpoint:])
            handle.flush()
            _inject(failure_injector, "before_fsync")
            _inject(failure_injector, f"before_{artifact_kind}_fsync")
            os.fsync(handle.fileno())
        _inject(failure_injector, "before_atomic_publication")
        _inject(failure_injector, f"before_{artifact_kind}_publication")
        try:
            os.link(temp_path, final_path)
        except FileExistsError as exc:
            raise ObservationConflictError(
                f"Observation {artifact_kind} target already exists."
            ) from exc
        published = True
        temp_path.unlink()
        _fsync_directory(Path(directory))
        return final_path
    finally:
        if fd is not None:
            os.close(fd)
        if temp_path.exists():
            temp_path.unlink()
        if not published and final_path.exists():
            # A pre-existing create-only target is never removed.
            pass


def verify_observation_history(directory, *, registry_entries):
    try:
        history = load_verified_ledger(directory, registry_entries=registry_entries)
    except (OSError, ObservationLedgerError) as exc:
        diagnostic = safe_failure_diagnostic(
            exc,
            default_code="observation_history_invalid",
        )
        return {
            "report_version": "greenhouse_pilot_observation_verification_v1",
            "history_dir": str(Path(directory)),
            "valid": False,
            "ledger_id": None,
            "bundle_count": 0,
            "receipt_count": 0,
            "errors": [diagnostic["failure_code"]],
            "failure_diagnostics": [diagnostic],
            "warnings": [],
            "history_fingerprint": None,
            "bundles": [],
        }
    return _verification_report(directory, history)


def _verification_report(directory, history):
    residue_count = history.working_residue_count
    return {
        "report_version": "greenhouse_pilot_observation_verification_v1",
        "history_dir": str(Path(directory)),
        "valid": True,
        "ledger_id": history.ledger_id,
        "bundle_count": len(history.bundles),
        "receipt_count": len(history.receipts),
        "errors": [],
        "failure_diagnostics": [],
        "warnings": (
            [f"Non-authoritative working residue is present: {residue_count} file(s)."]
            if residue_count
            else []
        ),
        "working_residue_present": residue_count > 0,
        "working_residue_count": residue_count,
        "working_residue_fingerprint": history.working_residue_fingerprint,
        "history_fingerprint": sha256_value(
            [
                {
                    "bundle": bundle["bundle_content_sha256"],
                    "receipt": receipt["receipt_content_sha256"],
                }
                for bundle, receipt in zip(history.bundles, history.receipts)
            ]
        ),
        "bundles": [
            {
                "ledger_sequence": item["ledger_sequence"],
                "bundle_id": item["bundle_id"],
                "run_id": item["run_id"],
                "started_at": item["started_at"],
                "completed_at": item["completed_at"],
                "bundle_content_sha256": item["bundle_content_sha256"],
                "receipt_id": receipt["receipt_id"],
                "receipt_content_sha256": receipt["receipt_content_sha256"],
                "registry_ids": item["requested_registry_ids"],
            }
            for item, receipt in zip(history.bundles, history.receipts)
        ],
    }


def load_verified_history(directory, *, registry_entries):
    return load_verified_ledger(
        directory, registry_entries=registry_entries
    ).bundles


def load_verified_ledger(directory, *, registry_entries):
    directory = Path(directory)
    if not directory.exists():
        return VerifiedObservationHistory(None, (), ())
    _validate_ledger_root(directory)
    lock_path = directory / LOCK_NAME
    if os.path.lexists(lock_path):
        with _ledger_lock(directory, create=False):
            return _load_verified_ledger_unlocked(
                directory, registry_entries=registry_entries
            )
    _validate_ledger_layout(directory)
    if _ledger_contains_artifacts(directory):
        raise ObservationValidationError("Observation history is missing its ledger lock.")
    return VerifiedObservationHistory(None, (), ())


def _load_verified_ledger_unlocked(directory, *, registry_entries):
    directory = Path(directory)
    _validate_ledger_layout(directory)
    residue = _working_residue_summary(directory)
    if not _ledger_contains_artifacts(directory):
        return VerifiedObservationHistory(
            None,
            (),
            (),
            residue["count"],
            residue["fingerprint"],
        )
    bundle_paths = _regular_json_paths(directory / BUNDLES_DIR_NAME, "bundle")
    receipt_paths = _regular_json_paths(directory / RECEIPTS_DIR_NAME, "receipt")
    bundles = []
    receipts = []
    for path in bundle_paths:
        payload = _read_json_artifact(path, "bundle", MAX_BUNDLE_BYTES)
        validate_observation_bundle(payload, registry_entries=registry_entries)
        if path.name != expected_bundle_filename(payload):
            raise ObservationValidationError(
                f"Observation filename does not match bundle identity: {path.name}."
            )
        bundles.append(payload)
    for path in receipt_paths:
        payload = _read_json_artifact(path, "receipt", MAX_RECEIPT_BYTES)
        receipts.append(payload)
    bundles.sort(key=lambda item: item["ledger_sequence"])
    receipts.sort(key=lambda item: item.get("ledger_sequence", -1))
    if len(bundles) != len(receipts):
        if len(bundles) > len(receipts):
            raise ObservationValidationError(
                "Observation history contains an unreceipted bundle."
            )
        raise ObservationValidationError(
            "Observation history contains a receipt without its bundle."
        )
    for bundle, receipt in zip(bundles, receipts):
        validate_observation_receipt(receipt, bundle=bundle)
    _validate_history_continuity(bundles, receipts)
    ledger_id = bundles[0]["ledger_id"] if bundles else None
    return VerifiedObservationHistory(
        ledger_id,
        tuple(bundles),
        tuple(receipts),
        residue["count"],
        residue["fingerprint"],
    )


def _read_json_artifact(path, kind, max_bytes):
    try:
        if path.stat().st_size > max_bytes:
            raise ObservationValidationError(
                f"Serialized observation {kind} exceeds its size limit."
            )
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except ObservationLedgerError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ObservationValidationError(
            f"Malformed observation {kind} {path.name}."
        ) from exc


def _reject_duplicate_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ObservationValidationError(f"Duplicate JSON field {key!r}.")
        result[key] = value
    return result


def _validate_history_continuity(bundles, receipts):
    run_ids = set()
    bundle_ids = set()
    receipt_ids = set()
    invocation_fingerprints = set()
    observation_ids = set()
    ledger_ids = set()
    previous_bundle = None
    previous_receipt = None
    previous_completed = None
    previous_published = None
    for expected_sequence, (bundle, receipt) in enumerate(
        zip(bundles, receipts), start=1
    ):
        if bundle["ledger_sequence"] != expected_sequence:
            raise ObservationValidationError("Observation ledger sequence has a gap.")
        if receipt["ledger_sequence"] != expected_sequence:
            raise ObservationValidationError("Observation receipt sequence has a gap.")
        if bundle["previous_bundle_sha256"] != previous_bundle:
            raise ObservationValidationError("Observation bundle hash chain is broken.")
        if receipt["previous_bundle_sha256"] != previous_bundle:
            raise ObservationValidationError("Observation receipt bundle chain is broken.")
        if receipt["previous_receipt_sha256"] != previous_receipt:
            raise ObservationValidationError("Observation receipt hash chain is broken.")
        if receipt["bundle_sha256"] != bundle["bundle_content_sha256"]:
            raise ObservationValidationError("Observation receipt refers to the wrong bundle.")
        if receipt["bundle_id"] != bundle["bundle_id"] or receipt["run_id"] != bundle["run_id"]:
            raise ObservationValidationError("Observation receipt identity is inconsistent.")
        ledger_ids.add(bundle["ledger_id"])
        ledger_ids.add(receipt["ledger_id"])
        if len(ledger_ids) != 1:
            raise ObservationValidationError("Observation history mixes ledger identities.")
        if bundle["run_id"] in run_ids:
            raise ObservationValidationError("Duplicate observation run ID.")
        if bundle["bundle_id"] in bundle_ids:
            raise ObservationValidationError("Duplicate observation bundle ID.")
        if receipt["receipt_id"] in receipt_ids:
            raise ObservationValidationError("Duplicate observation receipt ID.")
        if bundle["invocation_fingerprint"] in invocation_fingerprints:
            raise ObservationValidationError("Duplicate invocation fingerprint.")
        run_ids.add(bundle["run_id"])
        bundle_ids.add(bundle["bundle_id"])
        receipt_ids.add(receipt["receipt_id"])
        invocation_fingerprints.add(bundle["invocation_fingerprint"])
        for observation in bundle["observations"]:
            if observation["observation_id"] in observation_ids:
                raise ObservationValidationError("Duplicate board observation ID.")
            observation_ids.add(observation["observation_id"])
        started = parse_utc(bundle["started_at"], "started_at")
        completed = parse_utc(bundle["completed_at"], "completed_at")
        published = parse_utc(receipt["published_at"], "published_at")
        if previous_completed is not None and started <= previous_completed:
            raise ObservationValidationError(
                "Observation invocation intervals overlap or move backward."
            )
        if previous_published is not None and published <= previous_published:
            raise ObservationValidationError(
                "Observation receipt publication timestamps are not increasing."
            )
        previous_completed = completed
        previous_published = published
        previous_bundle = bundle["bundle_content_sha256"]
        previous_receipt = receipt["receipt_content_sha256"]


def _validate_append_chronology(history, bundle):
    if not history.bundles:
        return
    previous = history.bundles[-1]
    started = parse_utc(bundle["started_at"], "started_at")
    previous_completed = parse_utc(previous["completed_at"], "completed_at")
    if started <= previous_completed:
        raise ObservationClockError(
            "A new observation invocation must start after the previous invocation completed."
        )


def validate_observation_bundle(bundle, *, registry_entries):
    _require_exact_dict(bundle, BUNDLE_FIELDS, "Observation bundle")
    if bundle["schema_version"] != BUNDLE_SCHEMA_VERSION:
        raise ObservationValidationError("Unsupported observation bundle schema version.")
    _require_id(bundle["ledger_id"], "ledger_id", "ledger_")
    _require_positive_int(bundle["ledger_sequence"], "ledger_sequence")
    _require_id(bundle["bundle_id"], "bundle_id", "bundle_")
    _require_id(bundle["run_id"], "run_id", "run_")
    started = parse_utc(bundle["started_at"], "started_at")
    completed = parse_utc(bundle["completed_at"], "completed_at")
    if completed <= started:
        raise ObservationValidationError("Observation completion must follow its start.")
    if bundle["tool_version"] != TOOL_VERSION or bundle["command_mode"] != COMMAND_MODE:
        raise ObservationValidationError("Observation tool or command mode is unsupported.")
    for field in (
        "registry_sha256",
        "source_report_sha256",
        "invocation_fingerprint",
        "bundle_content_sha256",
    ):
        _require_sha256(bundle[field], field)
    if bundle["previous_bundle_sha256"] is not None:
        _require_sha256(bundle["previous_bundle_sha256"], "previous_bundle_sha256")
    requested = _require_string_list(
        bundle["requested_registry_ids"],
        "requested_registry_ids",
        max_items=MAX_BOARDS_PER_INVOCATION,
        max_length=MAX_IDENTIFIER_LENGTH,
    )
    if requested != sorted(requested):
        raise ObservationValidationError("Requested registry IDs must be sorted.")
    for value in requested:
        _require_safe_identifier(value, "requested registry ID")
    parsers = _require_string_list(
        bundle["parser_versions"],
        "parser_versions",
        max_items=MAX_BOARDS_PER_INVOCATION,
        max_length=MAX_IDENTIFIER_LENGTH,
    )
    if parsers != sorted(parsers) or not set(parsers).issubset(SUPPORTED_PARSER_VERSIONS):
        raise ObservationValidationError("Observation parser versions are invalid.")
    for value in parsers:
        _require_safe_identifier(value, "parser version")
    observations = bundle["observations"]
    if (
        type(observations) is not list
        or not observations
        or len(observations) > MAX_BOARDS_PER_INVOCATION
    ):
        raise ObservationValidationError("Observation bundle must contain board observations.")
    _require_enum(bundle["invocation_status"], INVOCATION_STATUSES, "invocation_status")
    registry_map = {entry.registry_id: entry for entry in registry_entries}
    seen = set()
    seen_observation_ids = set()
    for observation in observations:
        validate_board_observation(
            observation,
            bundle=bundle,
            registry_map=registry_map,
            started=started,
            completed=completed,
        )
        if observation["registry_id"] in seen:
            raise ObservationValidationError("Duplicate board observation in one invocation.")
        if observation["observation_id"] in seen_observation_ids:
            raise ObservationValidationError("Duplicate observation ID in one invocation.")
        seen.add(observation["registry_id"])
        seen_observation_ids.add(observation["observation_id"])
    if sorted(seen) != requested:
        raise ObservationValidationError(
            "Requested boards and recorded board observations do not agree."
        )
    if sorted({item["parser_version"] for item in observations}) != parsers:
        raise ObservationValidationError("Bundle parser summary is inconsistent.")
    successes = sum(bool(item["technical_success"]) for item in observations)
    expected_status = (
        "complete_success"
        if successes == len(observations)
        else "partial_success"
        if successes
        else "complete_failure"
    )
    if bundle["invocation_status"] != expected_status:
        raise ObservationValidationError("Invocation status contradicts board observations.")
    if compute_bundle_sha256(bundle) != bundle["bundle_content_sha256"]:
        raise ObservationValidationError("Observation bundle content fingerprint is invalid.")
    if len((canonical_json(bundle) + "\n").encode("utf-8")) > MAX_BUNDLE_BYTES:
        raise ObservationValidationError("Serialized observation bundle exceeds its size limit.")


def validate_observation_receipt(receipt, *, bundle):
    _require_exact_dict(receipt, RECEIPT_FIELDS, "Observation receipt")
    if receipt["schema_version"] != RECEIPT_SCHEMA_VERSION:
        raise ObservationValidationError("Unsupported observation receipt schema version.")
    _require_id(receipt["ledger_id"], "ledger_id", "ledger_")
    _require_positive_int(receipt["ledger_sequence"], "ledger_sequence")
    _require_id(receipt["receipt_id"], "receipt_id", "receipt_")
    _require_id(receipt["bundle_id"], "bundle_id", "bundle_")
    _require_id(receipt["run_id"], "run_id", "run_")
    for field in ("bundle_sha256", "receipt_content_sha256"):
        _require_sha256(receipt[field], field)
    for field in ("previous_receipt_sha256", "previous_bundle_sha256"):
        if receipt[field] is not None:
            _require_sha256(receipt[field], field)
    published = parse_utc(receipt["published_at"], "published_at")
    completed = parse_utc(bundle["completed_at"], "completed_at")
    if published < completed:
        raise ObservationClockError(
            "Observation receipt publication precedes invocation completion."
        )
    expected = {
        "ledger_id": bundle["ledger_id"],
        "ledger_sequence": bundle["ledger_sequence"],
        "bundle_id": bundle["bundle_id"],
        "run_id": bundle["run_id"],
        "bundle_sha256": bundle["bundle_content_sha256"],
        "previous_bundle_sha256": bundle["previous_bundle_sha256"],
    }
    if any(receipt[field] != value for field, value in expected.items()):
        raise ObservationValidationError("Observation receipt does not match its bundle.")
    if compute_receipt_sha256(receipt) != receipt["receipt_content_sha256"]:
        raise ObservationValidationError("Observation receipt fingerprint is invalid.")
    if len((canonical_json(receipt) + "\n").encode("utf-8")) > MAX_RECEIPT_BYTES:
        raise ObservationValidationError("Serialized observation receipt exceeds its size limit.")


def validate_board_observation(observation, *, bundle, registry_map, started, completed):
    _require_exact_dict(observation, OBSERVATION_FIELDS, "Board observation")
    _require_id(observation["observation_id"], "observation_id", "observation_")
    if observation["run_id"] != bundle["run_id"]:
        raise ObservationValidationError("Board observation run ID is inconsistent.")
    registry_id = _require_safe_identifier(observation["registry_id"], "registry_id")
    entry = registry_map.get(registry_id)
    if entry is None:
        raise ObservationValidationError(f"Unknown registry identity {registry_id!r}.")
    identity = {
        "company_id": entry.company_id,
        "board_identifier": entry.board_identifier,
        "ats_provider": entry.ats_provider,
    }
    if any(observation[field] != value for field, value in identity.items()):
        raise ObservationValidationError("Board observation identity conflicts with registry.")
    for field in ("company_id", "board_identifier", "ats_provider", "parser_version"):
        _require_safe_identifier(observation[field], field)
    if observation["parser_version"] not in SUPPORTED_PARSER_VERSIONS:
        raise ObservationValidationError("Board observation parser version is unsupported.")
    _require_sha256(observation["registry_sha256"], "registry_sha256")
    if observation["registry_sha256"] != bundle["registry_sha256"]:
        raise ObservationValidationError("Board registry fingerprint is inconsistent.")
    _require_sha256(observation["registry_contract_sha256"], "registry_contract_sha256")
    observed = parse_utc(observation["observed_at"], "observed_at")
    if not started <= observed <= completed:
        raise ObservationValidationError("Board observation timestamp is outside invocation.")
    _require_enum(observation["metrics_status"], METRICS_STATUSES, "metrics_status")
    _validate_metrics(observation)
    _require_enum(observation["snapshot_outcome"], SNAPSHOT_OUTCOMES, "snapshot_outcome")
    _require_bool(observation["snapshot_structurally_complete"], "snapshot_structurally_complete")
    _require_optional_bool(
        observation["snapshot_count_anomaly_safe"], "snapshot_count_anomaly_safe"
    )
    _require_optional_nonnegative_int(
        observation["prior_accepted_count_used_for_anomaly_check"],
        "prior_accepted_count_used_for_anomaly_check",
    )
    _require_bool(observation["closure_authorized"], "closure_authorized")
    _validate_lifecycle(observation["lifecycle_probe"])
    _require_bool(observation["technical_success"], "technical_success")
    reasons = _require_string_list(
        observation["operational_failure_reasons"],
        "operational_failure_reasons",
        allow_empty=True,
        max_items=MAX_CODES_PER_BOARD,
        max_length=MAX_CODE_LENGTH,
    )
    if any(not DIAGNOSTIC_PATTERN.fullmatch(value) for value in reasons):
        raise ObservationValidationError("Operational failure reason is malformed.")
    _validate_canonical(observation["canonical_yield"])
    _validate_coverage(observation["coverage"])
    _require_bool(
        observation["connector_enabled_for_dry_run"],
        "connector_enabled_for_dry_run",
    )
    _require_enum(
        observation["terms_review_status"], TERMS_REVIEW_STATUSES, "terms_review_status"
    )
    _require_enum(
        observation["acceptance_review_status"],
        ACCEPTANCE_REVIEW_STATUSES,
        "acceptance_review_status",
    )
    _require_enum(
        observation["temporary_closure_status"],
        VALIDATION_STATUSES,
        "temporary_closure_status",
    )
    _require_enum(
        observation["persona_coverage_status"],
        VALIDATION_STATUSES,
        "persona_coverage_status",
    )
    for field in ("product_enabled", "production_crawl_enabled"):
        _require_bool(observation[field], field)
    expected_technical_success = bool(
        observation["metrics_status"] == "measured"
        and observation["snapshot_outcome"] == "success"
        and observation["snapshot_structurally_complete"]
        and observation["snapshot_count_anomaly_safe"] is True
        and observation["closure_authorized"]
    )
    if observation["technical_success"] != expected_technical_success:
        raise ObservationValidationError("Technical success contradicts snapshot status.")
    if observation["metrics_status"] == "unmeasured" and observation[
        "snapshot_outcome"
    ] != "failed":
        raise ObservationValidationError("Unmeasured snapshot outcome is contradictory.")


def _validate_metrics(observation):
    count_fields = (
        "raw_record_count",
        "accepted_source_record_count",
        "stable_identity_count",
        "safe_url_count",
        "exact_title_unique_count",
        "normalized_title_unique_count",
    )
    rate_fields = (
        "stable_identity_rate",
        "safe_url_rate",
        "normalized_title_repetition_rate",
    )
    metadata = observation["metadata_completeness"]
    _require_exact_dict(metadata, METADATA_FIELDS, "Metadata completeness")
    if observation["metrics_status"] == "unmeasured":
        if any(observation[field] is not None for field in (*count_fields, *rate_fields)):
            raise ObservationValidationError("Unmeasured observation contains measured values.")
        if any(metadata[field] is not None for field in METADATA_FIELDS):
            raise ObservationValidationError("Unmeasured observation contains metadata rates.")
        return
    for field in count_fields:
        _require_nonnegative_int(observation[field], field)
    for field in rate_fields:
        _require_rate(observation[field], field)
    for field in METADATA_FIELDS:
        _require_rate(metadata[field], f"metadata_completeness.{field}")
    raw = observation["raw_record_count"]
    accepted = observation["accepted_source_record_count"]
    if accepted > raw:
        raise ObservationValidationError("Accepted record count exceeds raw record count.")
    for field in (
        "stable_identity_count",
        "safe_url_count",
        "exact_title_unique_count",
        "normalized_title_unique_count",
    ):
        if observation[field] > accepted:
            raise ObservationValidationError(f"{field} exceeds accepted record count.")
    expected_stable = _rate(observation["stable_identity_count"], raw)
    expected_safe = _rate(observation["safe_url_count"], raw)
    expected_repetition = _rate(accepted - observation["normalized_title_unique_count"], accepted)
    for actual, expected, label in (
        (observation["stable_identity_rate"], expected_stable, "stable identity rate"),
        (observation["safe_url_rate"], expected_safe, "safe URL rate"),
        (
            observation["normalized_title_repetition_rate"],
            expected_repetition,
            "title repetition rate",
        ),
    ):
        if actual != expected:
            raise ObservationValidationError(f"Observation {label} is inconsistent.")


def _validate_lifecycle(value):
    _require_exact_dict(value, LIFECYCLE_FIELDS, "Lifecycle probe")
    _require_enum(value["status"], LIFECYCLE_STATUSES, "lifecycle status")
    _require_bool(value["temporary_database"], "lifecycle temporary_database")
    for field in (
        "initial_active",
        "partial_removed",
        "active_after_partial",
        "complete_removed",
        "active_after_complete",
    ):
        _require_optional_nonnegative_int(value[field], f"lifecycle {field}")
    _require_optional_bool(value["other_sources_unchanged"], "other_sources_unchanged")
    _require_bool(value["closure_safe"], "lifecycle closure_safe")
    diagnostics = _require_string_list(
        value["diagnostic_codes"],
        "lifecycle diagnostic_codes",
        allow_empty=True,
        max_items=MAX_CODES_PER_BOARD,
        max_length=MAX_CODE_LENGTH,
    )
    if any(not DIAGNOSTIC_PATTERN.fullmatch(item) for item in diagnostics):
        raise ObservationValidationError("Lifecycle diagnostic code is malformed.")
    if value["status"] == "passed" and not (
        value["temporary_database"]
        and value["closure_safe"]
        and value["other_sources_unchanged"] is True
        and value["partial_removed"] == 0
        and value["complete_removed"] == 1
    ):
        raise ObservationValidationError("Passed lifecycle probe has contradictory metrics.")
    if value["status"] == "not_run" and (
        value["temporary_database"] or value["closure_safe"]
    ):
        raise ObservationValidationError("Not-run lifecycle probe is contradictory.")


def _validate_canonical(value):
    _require_exact_dict(value, CANONICAL_FIELDS, "Canonical yield")
    _require_enum(value["status"], CANONICAL_STATUSES, "canonical status")
    if value["status"] == "unmeasured":
        if any(value[field] is not None for field in CANONICAL_FIELDS - {"status"}):
            raise ObservationValidationError("Unmeasured canonical yield contains values.")
        return
    for field in ("canonical_count", "consolidation_count", "duplicate_count"):
        _require_nonnegative_int(value[field], f"canonical {field}")
    _require_rate(value["canonical_yield"], "canonical_yield")
    for field in ("input_fingerprint", "output_fingerprint"):
        if type(value[field]) is not str or not PREFIXED_SHA256_PATTERN.fullmatch(value[field]):
            raise ObservationValidationError(f"Canonical {field} is invalid.")


def _validate_coverage(value):
    _require_exact_dict(value, COVERAGE_FIELDS, "Coverage metrics")
    _require_enum(value["status"], COVERAGE_STATUSES, "coverage status")
    for field in (
        "new_titles_truncated",
        "new_companies_truncated",
        "coverage_approved",
    ):
        _require_bool(value[field], f"coverage {field}")
    _require_string_list(
        value["new_titles"],
        "coverage new_titles",
        allow_empty=True,
        max_items=MAX_BOUNDED_METRIC_ENTRIES,
        max_length=MAX_SAFE_DIAGNOSTIC_TEXT_LENGTH,
    )
    _require_string_list(
        value["new_companies"],
        "coverage new_companies",
        allow_empty=True,
        max_items=MAX_BOUNDED_METRIC_ENTRIES,
        max_length=MAX_SAFE_DIAGNOSTIC_TEXT_LENGTH,
    )
    for prefix in ("new_titles", "new_companies"):
        _require_nonnegative_int(
            value[f"{prefix}_omitted_count"],
            f"coverage {prefix}_omitted_count",
        )
        omitted_hash = value[f"{prefix}_omitted_sha256"]
        if omitted_hash is not None:
            _require_sha256(omitted_hash, f"coverage {prefix}_omitted_sha256")
        truncated = value[f"{prefix}_truncated"]
        omitted_count = value[f"{prefix}_omitted_count"]
        if truncated != bool(omitted_count):
            raise ObservationValidationError(
                f"Coverage {prefix} truncation metadata is inconsistent."
            )
        if bool(omitted_hash) != bool(omitted_count):
            raise ObservationValidationError(
                f"Coverage {prefix} omitted digest is inconsistent."
            )
    measured_fields = (
        "persona_count",
        "relevant_posting_count",
        "new_eligibility_leakage_count",
        "admitted_result_delta",
        "strong_family_delta",
        "company_diversity_delta",
        "regional_rejection_count",
        "regional_rejection_delta",
        "qualification_rejection_count",
        "qualification_rejection_delta",
    )
    if value["status"] == "unmeasured":
        if any(value[field] is not None for field in measured_fields):
            raise ObservationValidationError("Unmeasured coverage contains measured values.")
        if value["current_comparison_no_new_leakage"] is not None:
            raise ObservationValidationError("Unmeasured coverage has a comparison result.")
        if (
            value["new_titles"]
            or value["new_companies"]
            or value["new_titles_truncated"]
            or value["new_companies_truncated"]
            or value["new_titles_omitted_count"]
            or value["new_companies_omitted_count"]
            or value["new_titles_omitted_sha256"] is not None
            or value["new_companies_omitted_sha256"] is not None
        ):
            raise ObservationValidationError(
                "Unmeasured coverage contains retained or truncated evidence."
            )
        return
    _require_positive_int(value["persona_count"], "coverage persona_count")
    for field in measured_fields[1:]:
        if field.endswith("_delta") or field in (
            "admitted_result_delta",
            "strong_family_delta",
            "company_diversity_delta",
        ):
            _require_int(value[field], f"coverage {field}")
        else:
            _require_nonnegative_int(value[field], f"coverage {field}")
    _require_bool(
        value["current_comparison_no_new_leakage"],
        "coverage current_comparison_no_new_leakage",
    )
    if value["current_comparison_no_new_leakage"] != (
        value["new_eligibility_leakage_count"] == 0
    ):
        raise ObservationValidationError("Coverage leakage status is contradictory.")


def evaluate_operational_readiness(directory, *, registry_entries, requested_registry_ids=None):
    try:
        history = load_verified_ledger(directory, registry_entries=registry_entries)
    except (OSError, ObservationLedgerError) as exc:
        diagnostic = safe_failure_diagnostic(
            exc,
            default_code="observation_history_invalid",
        )
        verification = {
            "report_version": "greenhouse_pilot_observation_verification_v1",
            "history_dir": str(Path(directory)),
            "valid": False,
            "ledger_id": None,
            "bundle_count": 0,
            "receipt_count": 0,
            "errors": [diagnostic["failure_code"]],
            "failure_diagnostics": [diagnostic],
            "warnings": [],
            "history_fingerprint": None,
            "bundles": [],
        }
        return {
            "report_version": READINESS_REPORT_VERSION,
            "history_verification": verification,
            "production_ready": False,
            "boards": {},
        }
    verification = _verification_report(directory, history)
    entries = {entry.registry_id: entry for entry in registry_entries}
    requested = (
        sorted(requested_registry_ids)
        if requested_registry_ids is not None
        else sorted(entry.registry_id for entry in registry_entries if entry.connector_enabled_for_dry_run)
    )
    unknown = sorted(set(requested) - set(entries))
    if unknown:
        raise ObservationValidationError(
            "Unknown readiness registry IDs: " + ", ".join(unknown)
        )
    by_registry = {registry_id: [] for registry_id in requested}
    for bundle in history.bundles:
        for observation in bundle["observations"]:
            if observation["registry_id"] in by_registry:
                by_registry[observation["registry_id"]].append(
                    {
                        "observation": observation,
                        "started_at": bundle["started_at"],
                        "completed_at": bundle["completed_at"],
                    }
                )
    boards = {}
    for registry_id in requested:
        boards[registry_id] = _evaluate_board_readiness(
            entries[registry_id], by_registry[registry_id]
        )
    return {
        "report_version": READINESS_REPORT_VERSION,
        "history_verification": verification,
        "production_ready": bool(boards) and all(
            board["production_ready"] for board in boards.values()
        ),
        "boards": boards,
    }


def _evaluate_board_readiness(entry, observations):
    technical_trailing = []
    operational_trailing = []
    for record in observations:
        observation = record["observation"]
        if not _technical_observation_valid(observation):
            technical_trailing = []
            operational_trailing = []
            continue
        if technical_trailing and not _same_contract(
            technical_trailing[-1]["observation"], observation
        ):
            technical_trailing = []
            operational_trailing = []
        technical_trailing.append(record)
        if _closure_valid(observation):
            operational_trailing.append(record)
        else:
            operational_trailing = []
    technical_span = _span_hours(technical_trailing)
    operational_span = _span_hours(operational_trailing)
    current_contract = registry_contract_sha256(entry)
    latest_record = observations[-1] if observations else None
    latest = latest_record["observation"] if latest_record else None
    current_contract_observed = bool(
        latest
        and latest["registry_contract_sha256"] == current_contract
        and latest["parser_version"] == entry.parser_version
    )
    distinct_run_ids = len(
        {item["observation"]["run_id"] for item in operational_trailing}
    )
    distinct_observation_ids = len(
        {item["observation"]["observation_id"] for item in operational_trailing}
    )
    distinct_intervals = len(
        {(item["started_at"], item["completed_at"]) for item in operational_trailing}
    )
    operational_snapshot_ready = bool(
        len(operational_trailing) >= 3
        and distinct_run_ids >= 3
        and distinct_observation_ids >= 3
        and distinct_intervals >= 3
        and operational_span >= 24.0
        and current_contract_observed
    )
    terms_approved = entry.terms_review_status == "approved"
    coverage_approved = entry.persona_coverage_status == "passed"
    independent_acceptance_approved = entry.acceptance_review_status == "approved"
    closure_approved = bool(
        entry.temporary_closure_status == "passed"
        or (
            operational_trailing
            and all(
                _closure_valid(item["observation"])
                for item in operational_trailing
            )
        )
    )
    failures = []
    if not operational_snapshot_ready:
        failures.append("operational_snapshot_history_incomplete")
    if not current_contract_observed and observations:
        failures.append("current_registry_contract_not_observed")
    if not terms_approved:
        failures.append("company_terms_not_approved")
    if not coverage_approved:
        failures.append("persona_coverage_not_approved")
    if not closure_approved:
        failures.append("closure_not_approved")
    if not independent_acceptance_approved:
        failures.append("independent_acceptance_not_approved")
    if not entry.product_enabled:
        failures.append("product_not_enabled")
    if not entry.production_crawl_enabled:
        failures.append("production_crawl_not_enabled")
    production_ready = not failures
    return {
        "registry_id": entry.registry_id,
        "observation_count": len(observations),
        "technical_snapshot_streak": len(technical_trailing),
        "operational_snapshot_streak": len(operational_trailing),
        "distinct_operational_run_count": distinct_run_ids,
        "distinct_operational_observation_count": distinct_observation_ids,
        "distinct_operational_interval_count": distinct_intervals,
        "observation_span_hours": operational_span,
        "technical_observation_span_hours": technical_span,
        "operational_snapshot_ready": operational_snapshot_ready,
        "current_registry_contract_observed": current_contract_observed,
        "current_registry_contract_sha256": current_contract,
        "latest_observed_at": latest["observed_at"] if latest else None,
        "parser_version": entry.parser_version,
        "independent_acceptance_approved": independent_acceptance_approved,
        "terms_approved": terms_approved,
        "coverage_approved": coverage_approved,
        "closure_approved": closure_approved,
        "product_enabled": entry.product_enabled,
        "production_crawl_enabled": entry.production_crawl_enabled,
        "production_ready": production_ready,
        "production_readiness_failures": failures,
    }


def _technical_observation_valid(observation):
    return bool(
        observation["technical_success"]
        and observation["snapshot_structurally_complete"]
        and observation["snapshot_count_anomaly_safe"] is True
        and observation["snapshot_outcome"] == "success"
    )


def _closure_valid(observation):
    return bool(
        observation["temporary_closure_status"] == "passed"
        or observation["lifecycle_probe"]["status"] == "passed"
    )


def _same_contract(left, right):
    return bool(
        left["registry_id"] == right["registry_id"]
        and left["parser_version"] == right["parser_version"]
        and left["registry_contract_sha256"] == right["registry_contract_sha256"]
    )


def _span_hours(observations):
    if len(observations) < 2:
        return 0.0
    first = parse_utc(
        observations[0]["observation"]["observed_at"], "observed_at"
    )
    last = parse_utc(
        observations[-1]["observation"]["observed_at"], "observed_at"
    )
    return round((last - first).total_seconds() / 3600.0, 6)


def _prepare_ledger_root(directory):
    directory = Path(directory)
    if os.path.lexists(directory):
        _require_real_directory(directory, "Observation history")
        return
    directory.mkdir(parents=True, exist_ok=False)
    _require_real_directory(directory, "Observation history")
    _fsync_directory(directory.parent)


def _prepare_artifact_directories(directory):
    for name in (BUNDLES_DIR_NAME, RECEIPTS_DIR_NAME, WORKING_DIR_NAME):
        path = Path(directory) / name
        if os.path.lexists(path):
            _require_real_directory(path, f"Observation {name}")
        else:
            path.mkdir()
            _fsync_directory(Path(directory))


def _ledger_contains_artifacts(directory):
    directory = Path(directory)
    return bool(
        os.path.lexists(directory / BUNDLES_DIR_NAME)
        or os.path.lexists(directory / RECEIPTS_DIR_NAME)
    )


def _validate_ledger_root(directory):
    _require_real_directory(Path(directory), "Observation history")


def _validate_ledger_layout(directory):
    directory = Path(directory)
    _validate_ledger_root(directory)
    allowed = {BUNDLES_DIR_NAME, RECEIPTS_DIR_NAME, WORKING_DIR_NAME, LOCK_NAME}
    unexpected = sorted(path.name for path in directory.iterdir() if path.name not in allowed)
    if unexpected:
        raise ObservationValidationError(
            "Observation history contains unexpected filesystem entries: "
            + ", ".join(unexpected)
        )
    bundles = directory / BUNDLES_DIR_NAME
    receipts = directory / RECEIPTS_DIR_NAME
    if os.path.lexists(bundles) != os.path.lexists(receipts):
        raise ObservationValidationError(
            "Observation history must contain both bundle and receipt directories."
        )
    if os.path.lexists(bundles):
        _require_real_directory(bundles, "Observation bundles")
        _require_real_directory(receipts, "Observation receipts")
    working = directory / WORKING_DIR_NAME
    if os.path.lexists(working):
        _require_real_directory(working, "Observation working directory")
        _working_residue_paths(directory)
    lock_path = directory / LOCK_NAME
    if (
        _ledger_contains_artifacts(directory) or os.path.lexists(working)
    ) and not os.path.lexists(lock_path):
        raise ObservationValidationError("Observation history is missing its ledger lock.")
    if os.path.lexists(lock_path):
        _require_regular_file(lock_path, "Observation ledger lock", allow_hard_links=False)


def _regular_json_paths(directory, kind):
    paths = []
    for path in Path(directory).iterdir():
        if path.suffix != ".json":
            raise ObservationValidationError(
                f"Observation {kind} directory contains a stale or unexpected entry: {path.name}."
            )
        _require_regular_file(path, f"Observation {kind}", allow_hard_links=False)
        paths.append(path)
    return paths


def _working_residue_paths(directory):
    working = Path(directory) / WORKING_DIR_NAME
    if not os.path.lexists(working):
        return []
    _require_real_directory(working, "Observation working directory")
    paths = []
    for path in working.iterdir():
        _require_regular_file(
            path,
            "Observation working residue",
            allow_hard_links=False,
        )
        if not WORKING_FILE_PATTERN.fullmatch(path.name):
            raise ObservationValidationError(
                f"Observation working directory contains an unexpected entry: {path.name}."
            )
        limit = MAX_BUNDLE_BYTES if path.name.startswith("staging_bundle_") else MAX_RECEIPT_BYTES
        if path.stat().st_size > limit:
            raise ObservationValidationError("Observation working residue is oversized.")
        paths.append(path)
    return sorted(paths, key=lambda item: item.name)


def _working_residue_summary(directory):
    paths = _working_residue_paths(directory)
    names = [path.name for path in paths]
    return {
        "count": len(paths),
        "fingerprint": sha256_value(names) if names else None,
        "warnings": (
            [f"Non-authoritative working residue is present: {len(paths)} file(s)."]
            if paths
            else []
        ),
    }


def _cleanup_working_residue(directory):
    paths = _working_residue_paths(directory)
    for path in paths:
        path.unlink()
    if paths:
        _fsync_directory(Path(directory) / WORKING_DIR_NAME)


def _require_real_directory(path, label):
    try:
        details = os.lstat(path)
    except OSError as exc:
        raise ObservationValidationError(f"{label} cannot be inspected.") from exc
    if stat.S_ISLNK(details.st_mode):
        raise ObservationValidationError(f"{label} may not be a symlink.")
    if not stat.S_ISDIR(details.st_mode):
        raise ObservationValidationError(f"{label} path is not a directory.")


def _require_regular_file(path, label, *, allow_hard_links):
    try:
        details = os.lstat(path)
    except OSError as exc:
        raise ObservationValidationError(f"{label} cannot be inspected.") from exc
    if stat.S_ISLNK(details.st_mode):
        raise ObservationValidationError(f"{label} may not be a symlink.")
    if not stat.S_ISREG(details.st_mode):
        raise ObservationValidationError(f"{label} is not a regular file.")
    if not allow_hard_links and details.st_nlink != 1:
        raise ObservationValidationError(f"{label} has an unsafe hard-link identity.")


@contextmanager
def _ledger_lock(directory, *, create):
    directory = Path(directory)
    key = str(Path(directory).resolve()).casefold()
    with _THREAD_LOCKS_GUARD:
        local_lock = _THREAD_LOCKS.setdefault(key, threading.Lock())
    local_lock.acquire()
    handle = None
    locked = False
    try:
        path = directory / LOCK_NAME
        if not create and not os.path.lexists(path):
            if _ledger_contains_artifacts(directory):
                raise ObservationValidationError(
                    "Observation history is missing its ledger lock."
                )
            yield
            return
        flags = os.O_RDWR | (os.O_CREAT if create else 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags, 0o600)
        except OSError as exc:
            raise ObservationLockError("Observation ledger lock cannot be opened.") from exc
        handle = os.fdopen(fd, "r+b", buffering=0)
        _require_regular_file(path, "Observation ledger lock", allow_hard_links=False)
        if handle.seek(0, os.SEEK_END) == 0 and create:
            handle.write(b"\0")
            os.fsync(handle.fileno())
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            elif os.name == "posix":
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            else:  # pragma: no cover - supported runtime families are tested
                raise ObservationLockError(
                    "This platform has no supported cross-process ledger lock."
                )
        except OSError as exc:
            raise ObservationLockError("Observation ledger lock failed.") from exc
        locked = True
        yield
    finally:
        if handle is not None:
            if locked:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        local_lock.release()


def _fsync_directory(directory):
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _inject(callback, point):
    if callback is not None:
        callback(point)


def _require_exact_dict(value, fields, label):
    if type(value) is not dict or set(value) != set(fields):
        raise ObservationValidationError(
            f"{label} must contain exactly {sorted(fields)}."
        )


def _require_id(value, label, prefix):
    if type(value) is not str or not ID_PATTERN.fullmatch(value) or not value.startswith(prefix):
        raise ObservationValidationError(f"{label} is invalid.")


def _require_sha256(value, label):
    if type(value) is not str or not SHA256_PATTERN.fullmatch(value):
        raise ObservationValidationError(f"{label} is not a SHA-256 value.")


def _require_enum(value, allowed, label):
    if type(value) is not str or value not in allowed:
        raise ObservationValidationError(f"{label} is unsupported.")


def _require_nonempty_string(value, label):
    if type(value) is not str or not value.strip() or value != value.strip():
        raise ObservationValidationError(f"{label} is invalid.")
    return value


def _require_safe_identifier(value, label):
    value = _require_nonempty_string(value, label)
    if (
        len(value) > MAX_IDENTIFIER_LENGTH
        or not SAFE_IDENTIFIER_PATTERN.fullmatch(value)
        or "/" in value
        or "\\" in value
        or value in {".", ".."}
    ):
        raise ObservationValidationError(f"{label} is not a safe identifier.")
    return value


def _require_string_list(
    value,
    label,
    *,
    allow_empty=False,
    max_items=MAX_BOUNDED_METRIC_ENTRIES,
    max_length=MAX_SAFE_DIAGNOSTIC_TEXT_LENGTH,
):
    if type(value) is not list or (not value and not allow_empty):
        raise ObservationValidationError(f"{label} must be a string list.")
    if len(value) > max_items:
        raise ObservationValidationError(
            f"{label} exceeds its maximum of {max_items} entries."
        )
    if any(
        type(item) is not str
        or not item.strip()
        or item != item.strip()
        or len(item) > max_length
        for item in value
    ):
        raise ObservationValidationError(
            f"{label} must contain clean, bounded strings."
        )
    if len(value) != len(set(value)):
        raise ObservationValidationError(f"{label} contains duplicates.")
    return value


def _require_bool(value, label):
    if type(value) is not bool:
        raise ObservationValidationError(f"{label} must be boolean.")


def _require_optional_bool(value, label):
    if value is not None:
        _require_bool(value, label)


def _require_int(value, label):
    if type(value) is not int:
        raise ObservationValidationError(f"{label} must be an integer.")


def _require_positive_int(value, label):
    _require_int(value, label)
    if value <= 0:
        raise ObservationValidationError(f"{label} must be positive.")


def _require_nonnegative_int(value, label):
    _require_int(value, label)
    if value < 0:
        raise ObservationValidationError(f"{label} must be nonnegative.")


def _require_optional_nonnegative_int(value, label):
    if value is not None:
        _require_nonnegative_int(value, label)


def _require_rate(value, label):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ObservationValidationError(f"{label} must be a finite rate from 0 to 1.")


def _rate(numerator, denominator):
    if not denominator:
        return 0.0
    return round(numerator / denominator, 4)

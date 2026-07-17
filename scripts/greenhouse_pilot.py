#!/usr/bin/env python3
"""Dry-run-only Greenhouse source-registry pilot and acceptance diagnostics."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import html
import json
from pathlib import Path
import re
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import local_product_app as product_app  # noqa: E402
import profile_matching_coverage as coverage  # noqa: E402
import profile_to_matches_preview as preview  # noqa: E402
import profile_match_digest as matcher  # noqa: E402
from wahojobs.classification import (  # noqa: E402
    INVENTORY_MODEL_EVERGREEN_APPLICATION,
    INVENTORY_MODEL_MIXED,
    INVENTORY_MODEL_PUBLIC_INVENTORY,
    MARKET_COUNT_POLICY_COUNT_LIVE,
)
from wahojobs.config import DB_PATH  # noqa: E402
from wahojobs.crawler.greenhouse_pilot import (  # noqa: E402
    fetch_snapshot_sequence,
    is_future_role,
    run_temporary_canonicalization_probe,
    run_temporary_lifecycle_probe,
    snapshot_is_complete,
    snapshot_metrics,
)
from wahojobs.crawler.greenhouse_observations import (  # noqa: E402
    ObservationLedgerError,
    evaluate_operational_readiness,
    record_observation_bundle,
    safe_failure_diagnostic,
    verify_observation_history,
)
from wahojobs.crawler.source_registry import (  # noqa: E402
    REGISTRY_PATH,
    dry_run_entries,
    load_source_registry,
)
from wahojobs.matching.locations import regions_in_location  # noqa: E402
from wahojobs.profiles.canonical import canonical_to_matcher_profile  # noqa: E402


PERSONALIZED_SECTIONS = (
    "do_these_first",
    "best_matches",
    "also_worth_reviewing",
)
LEAK_FIELDS = {
    "specialist_mismatch": "specialist_mismatches",
    "unsupported_language": "unsupported_language_leaks",
    "location": "location_leaks",
    "credential": "credential_leaks",
}


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Fetch registry-approved Greenhouse boards without using the production "
            "crawler or workspace database. Results are printed to stdout only."
        )
    )
    parser.add_argument("--registry", type=Path, default=REGISTRY_PATH)
    parser.add_argument(
        "--board",
        action="append",
        dest="boards",
        help="Registry ID to fetch. Repeat as needed; defaults to control plus all pilot boards.",
    )
    parser.add_argument(
        "--require-production-ready",
        action="store_true",
        help="Return exit 1 when any selected source has unmet production-readiness gates.",
    )
    parser.add_argument(
        "--baseline-db",
        type=Path,
        default=DB_PATH,
        help="Read-only current Wahojobs inventory used for persona coverage.",
    )
    parser.add_argument(
        "--snapshots",
        type=int,
        default=1,
        choices=(1, 2, 3),
        help="Consecutive snapshots to fetch in this invocation.",
    )
    parser.add_argument(
        "--lifecycle-probe",
        action="store_true",
        help="Exercise partial/closure/source-isolation behavior in an automatic temporary database.",
    )
    parser.add_argument(
        "--skip-coverage",
        action="store_true",
        help="Skip the in-memory 28-persona control-versus-pilot comparison.",
    )
    parser.add_argument(
        "--evaluated-at",
        help="UTC ISO timestamp used for deterministic coverage diagnostics.",
    )
    parser.add_argument(
        "--format",
        choices=("json", "human"),
        default="json",
        dest="output_format",
        help="Print the same dry-run report as JSON or a compact human summary.",
    )
    parser.add_argument(
        "--record-observation-dir",
        type=Path,
        help="Create one immutable observation bundle after this dry-run invocation.",
    )
    parser.add_argument(
        "--history-dir",
        type=Path,
        help="Local observation-ledger directory to verify or evaluate.",
    )
    history_mode = parser.add_mutually_exclusive_group()
    history_mode.add_argument(
        "--verify-history",
        action="store_true",
        help="Verify durable local observation history without network or database access.",
    )
    history_mode.add_argument(
        "--evaluate-readiness",
        action="store_true",
        help="Evaluate per-board readiness from verified durable history only.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    output_format = getattr(args, "output_format", "json")
    invocation_started_at = datetime.now(timezone.utc)
    try:
        validate_ledger_cli_args(args, argv=sys.argv[1:])
        all_entries = tuple(load_source_registry(args.registry))
        if getattr(args, "verify_history", False):
            report = verify_observation_history(
                args.history_dir,
                registry_entries=all_entries,
            )
            emit_report(report, output_format)
            return 0 if report["valid"] else 1
        if getattr(args, "evaluate_readiness", False):
            report = evaluate_operational_readiness(
                args.history_dir,
                registry_entries=all_entries,
                requested_registry_ids=getattr(args, "boards", None),
            )
            emit_report(report, output_format)
            if not report["history_verification"]["valid"]:
                return 1
            if getattr(args, "require_production_ready", False) and not report[
                "production_ready"
            ]:
                return 1
            return 0
        entries = select_entries(all_entries, getattr(args, "boards", None))
    except (OSError, ValueError, json.JSONDecodeError, SystemExit) as exc:
        diagnostic = safe_failure_diagnostic(
            exc,
            default_code="registry_or_configuration_invalid",
        )
        emit_report(
            {"error": diagnostic["failure_code"], **diagnostic},
            output_format,
        )
        return 2
    evaluated_at = parse_evaluated_at(args.evaluated_at)
    sequences = {}
    board_failures = {}
    for entry in entries:
        try:
            sequences[entry.registry_id] = fetch_snapshot_sequence(entry, args.snapshots)
        except Exception as exc:
            diagnostic = safe_failure_diagnostic(exc)
            board_failures[entry.registry_id] = {
                "registry_id": entry.registry_id,
                "board_identifier": entry.board_identifier,
                "attempted": True,
                "success": False,
                **diagnostic,
            }
    successful_entries = tuple(
        entry for entry in entries if entry.registry_id in sequences
    )
    latest = {registry_id: values[-1] for registry_id, values in sequences.items()}
    lifecycle = (
        run_temporary_lifecycle_probe(successful_entries, latest)
        if args.lifecycle_probe and successful_entries
        else {}
    )
    coverage_report = (
        None
        if args.skip_coverage or not successful_entries
        else compare_persona_coverage(
            successful_entries,
            latest,
            evaluated_at=evaluated_at,
            baseline_rows=load_full_baseline_rows(args.baseline_db),
        )
    )
    canonical = (
        run_temporary_canonicalization_probe(successful_entries, latest)
        if successful_entries
        else {}
    )
    relevant = (
        coverage_report.get("relevant_external_ids_by_registry", {})
        if coverage_report
        else None
    )
    has_new_leakage = (
        coverage_report.get("new_eligibility_leakage_count", 0) > 0
        if coverage_report
        else None
    )
    sources = []
    for entry in successful_entries:
        metrics = snapshot_metrics(
            entry,
            sequences[entry.registry_id],
            relevant_external_ids=(
                relevant.get(entry.registry_id, []) if relevant is not None else None
            ),
            lifecycle=lifecycle.get(entry.registry_id),
            coverage_has_new_leakage=has_new_leakage,
            canonical_metrics=canonical.get(entry.registry_id),
        )
        metrics["relevant_external_ids"] = (
            relevant.get(entry.registry_id, []) if relevant is not None else None
        )
        sources.append(metrics)

    pilot_entries = [entry for entry in entries if entry.is_pilot]
    report = {
        "report_version": "greenhouse_registry_pilot_v2",
        "evaluated_at": evaluated_at.isoformat(),
        "safety": {
            "dry_run_only": True,
            "workspace_database_opened": not args.skip_coverage,
            "workspace_database_access_mode": (
                "read_only" if not args.skip_coverage else "not_opened"
            ),
            "workspace_database_written": False,
            "ordinary_production_crawler_used": False,
            "normal_inventory_imported": False,
            "temporary_lifecycle_database_used": bool(args.lifecycle_probe),
            "production_enablement_allowed": False,
        },
        "registry_path": str(args.registry),
        "boards_evaluated": [entry.registry_id for entry in entries],
        "pilot_enablement": {
            entry.registry_id: {
                "connector_enabled_for_dry_run": entry.connector_enabled_for_dry_run,
                "product_enabled": entry.product_enabled,
                "production_crawl_enabled": entry.production_crawl_enabled,
                "terms_review_status": entry.terms_review_status,
                "acceptance_review_status": entry.acceptance_review_status,
                "historical_readiness_streak": entry.consecutive_complete_snapshots,
                "temporary_closure_status": entry.temporary_closure_status,
                "persona_coverage_status": entry.persona_coverage_status,
            }
            for entry in pilot_entries
        },
        "sources": sources,
        "board_failures": board_failures,
        "persona_coverage": coverage_report,
        "technical_dry_run_passed": bool(successful_entries)
        and len(successful_entries) == len(entries)
        and all(snapshot_is_complete(result) for result in latest.values()),
        "production_enablement_changed": False,
    }
    technical_failed = not report["technical_dry_run_passed"]
    readiness_failed = any(
        not source["enablement"]["production_ready"] for source in sources
    )
    exit_code = int(
        technical_failed
        or (getattr(args, "require_production_ready", False) and readiness_failed)
    )
    return finalize_operational_invocation(
        args,
        report,
        selected_entries=entries,
        registry_entries=all_entries,
        invocation_started_at=invocation_started_at,
        technical_exit_code=exit_code,
    )


def validate_ledger_cli_args(args, *, argv=None):
    record_dir = getattr(args, "record_observation_dir", None)
    history_dir = getattr(args, "history_dir", None)
    verify = getattr(args, "verify_history", False)
    evaluate = getattr(args, "evaluate_readiness", False)
    explicit_options = {
        token.split("=", 1)[0]
        for token in (argv if argv is not None else sys.argv[1:])
        if token.startswith("--")
    }
    if verify:
        allowed = {"--history-dir", "--verify-history", "--format"}
        disallowed = sorted(explicit_options - allowed)
        if disallowed:
            raise ValueError(
                "--verify-history does not accept: " + ", ".join(disallowed)
            )
    if evaluate:
        allowed = {
            "--history-dir",
            "--evaluate-readiness",
            "--format",
            "--require-production-ready",
        }
        disallowed = sorted(explicit_options - allowed)
        if disallowed:
            raise ValueError(
                "--evaluate-readiness does not accept: " + ", ".join(disallowed)
            )
    if (verify or evaluate) and history_dir is None:
        raise ValueError("--history-dir is required for history verification or readiness.")
    if history_dir is not None and not (verify or evaluate):
        raise ValueError("--history-dir requires --verify-history or --evaluate-readiness.")
    if record_dir is not None and (verify or evaluate):
        raise ValueError("Recording cannot be combined with a read-only history mode.")
    if record_dir is not None and getattr(args, "evaluated_at", None):
        raise ValueError("Recorded observations cannot use a caller-supplied timestamp.")


def finalize_operational_invocation(
    args,
    report,
    *,
    selected_entries,
    registry_entries,
    invocation_started_at,
    technical_exit_code,
):
    record_dir = getattr(args, "record_observation_dir", None)
    if record_dir is not None:
        try:
            completed_at = max(
                datetime.now(timezone.utc),
                invocation_started_at + timedelta(microseconds=1),
            )
            recorded = record_observation_bundle(
                record_dir,
                report=report,
                entries=selected_entries,
                registry_entries=registry_entries,
                started_at=invocation_started_at,
                completed_at=completed_at,
                registry_path=args.registry,
            )
        except (OSError, ObservationLedgerError) as exc:
            diagnostic = safe_failure_diagnostic(
                exc,
                default_code="observation_recording_failed",
            )
            report["observation_ledger"] = {
                "recorded": False,
                "error": diagnostic["failure_code"],
                **diagnostic,
            }
            emit_report(report, getattr(args, "output_format", "json"))
            return 2
        report["observation_ledger"] = {
            "recorded": True,
            "path": str(recorded.path),
            "receipt_path": str(recorded.receipt_path),
            "ledger_id": recorded.ledger_id,
            "ledger_sequence": recorded.ledger_sequence,
            "bundle_id": recorded.bundle_id,
            "receipt_id": recorded.receipt_id,
            "run_id": recorded.run_id,
            "bundle_content_sha256": recorded.bundle_content_sha256,
            "receipt_content_sha256": recorded.receipt_content_sha256,
            "invocation_status": recorded.invocation_status,
            "observation_ids": list(recorded.observation_ids),
            "boards": _observation_board_summary(report, selected_entries),
        }
    emit_report(report, getattr(args, "output_format", "json"))
    return technical_exit_code


def _observation_board_summary(report, selected_entries):
    sources = {item["registry_id"]: item for item in report.get("sources") or []}
    failures = report.get("board_failures") or {}
    result = {}
    for entry in selected_entries:
        failure = failures.get(entry.registry_id)
        value = {
            "registry_id": entry.registry_id,
            "board_identifier": entry.board_identifier,
            "attempted": True,
            "technical_success": bool(
                entry.registry_id in sources
                and sources[entry.registry_id]["technical_status"][
                    "connector_technically_valid"
                ]
                and sources[entry.registry_id]["technical_status"][
                    "snapshot_structurally_complete"
                ]
                and sources[entry.registry_id]["technical_status"][
                    "snapshot_count_anomaly_safe"
                ]
                and sources[entry.registry_id]["technical_status"]["outcome"]
                == "success"
                and sources[entry.registry_id]["technical_status"][
                    "closure_authorized"
                ]
            ),
            "status": "failed" if entry.registry_id in failures else "measured",
        }
        if failure:
            value.update(safe_failure_diagnostic(failure))
        result[entry.registry_id] = value
    return result


def emit_report(report, output_format):
    if output_format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
        return
    if output_format != "human":
        raise ValueError(f"Unsupported output format: {output_format!r}.")
    print(render_human_report(report))


def render_human_report(report):
    if report.get("error"):
        return (
            "Greenhouse pilot configuration error\n"
            + str(report.get("safe_message") or "The request could not be completed.")
        )
    if report.get("report_version") == "greenhouse_pilot_observation_verification_v1":
        lines = [
            "Greenhouse observation history verification",
            "History valid: " + yes_no(report["valid"]),
            f"Ledger ID: {report.get('ledger_id') or '-'}",
            f"Bundles verified: {report['bundle_count']}",
            f"Receipts verified: {report.get('receipt_count', 0)}",
            f"History fingerprint: {report.get('history_fingerprint') or '-'}",
            f"Working residue count: {report.get('working_residue_count', 0)}",
        ]
        lines.extend(f"- warning: {value}" for value in report.get("warnings") or [])
        diagnostics = report.get("failure_diagnostics") or []
        if diagnostics:
            lines.extend(
                f"- error: {value['failure_code']} - {value['safe_message']}"
                for value in diagnostics
            )
        else:
            lines.extend(f"- error: {value}" for value in report.get("errors") or [])
        return "\n".join(lines)
    if report.get("report_version") == "greenhouse_pilot_operational_readiness_v1":
        verification = report["history_verification"]
        lines = [
            "Greenhouse operational readiness",
            "History valid: " + yes_no(verification["valid"]),
            f"Working residue count: {verification.get('working_residue_count', 0)}",
            "Overall production ready: " + yes_no(report["production_ready"]),
            "",
            "Boards",
        ]
        lines.extend(
            f"- warning: {value}" for value in verification.get("warnings") or []
        )
        for registry_id, board in sorted(report.get("boards", {}).items()):
            lines.extend(
                [
                    f"- {registry_id}",
                    f"  technical snapshot streak: {board['technical_snapshot_streak']}",
                    f"  observation span hours: {board['observation_span_hours']}",
                    "  operational snapshot ready: "
                    + yes_no(board["operational_snapshot_ready"]),
                    "  terms approved: " + yes_no(board["terms_approved"]),
                    "  coverage approved: " + yes_no(board["coverage_approved"]),
                    "  independent acceptance approved: "
                    + yes_no(board["independent_acceptance_approved"]),
                    "  closure approved: " + yes_no(board["closure_approved"]),
                    "  product enabled: " + yes_no(board["product_enabled"]),
                    "  production crawl enabled: "
                    + yes_no(board["production_crawl_enabled"]),
                    "  production ready: " + yes_no(board["production_ready"]),
                ]
            )
        return "\n".join(lines)
    if report.get("board_failures"):
        lines = ["Greenhouse pilot technical dry run", "Technical dry run passed: no"]
        for source in report.get("sources") or []:
            lines.append(f"- {source['registry_id']}: measured successfully")
        for registry_id, failure in sorted(report["board_failures"].items()):
            lines.append(
                f"- {registry_id}: failed ({failure['failure_code']}) - "
                + failure["safe_message"]
            )
        _append_ledger_human(lines, report.get("observation_ledger"))
        return "\n".join(lines)

    lines = [
        "Greenhouse pilot technical dry run",
        f"Evaluated at: {report.get('evaluated_at') or '-'}",
        "Technical dry run passed: "
        + ("yes" if report.get("technical_dry_run_passed") else "no"),
        "Production enablement changed: no",
        "",
        "Sources",
    ]
    for source in report.get("sources") or []:
        technical = source["technical_status"]
        enablement = source["enablement"]
        canonical = (
            str(source["canonical_count"])
            if source.get("canonical_yield_status") != "unmeasured"
            else "unmeasured"
        )
        lines.extend(
            [
                f"- {source['company_name']} ({source['registry_id']})",
                "  connector technically valid: "
                + yes_no(technical["connector_technically_valid"]),
                "  snapshot structurally complete: "
                + yes_no(technical["snapshot_structurally_complete"]),
                "  snapshot count-anomaly safe: "
                + yes_no(technical["snapshot_count_anomaly_safe"]),
                "  closure authorized: " + yes_no(technical["closure_authorized"]),
                f"  historical readiness streak: {enablement['historical_readiness_streak']}",
                f"  company terms status: {enablement['terms_review_status']}",
                "  product enabled: " + yes_no(enablement["product_enabled"]),
                "  production crawl enabled: "
                + yes_no(enablement["production_crawl_enabled"]),
                "  coverage GO: " + yes_no(enablement["coverage_go"]),
                f"  raw / accepted / relevant: {source['raw_record_count']} / "
                f"{source['accepted_source_record_count']} / "
                f"{source['relevant_posting_count'] if source['relevant_posting_count'] is not None else 'unmeasured'}",
                f"  stable identity / safe URL rates: {source['stable_identity_rate']} / "
                f"{source['safe_url_rate']}",
                f"  canonical count: {canonical}",
            ]
        )
    coverage_report = report.get("persona_coverage")
    if coverage_report:
        lines.extend(
            [
                "",
                "Persona coverage",
                f"- personas: {coverage_report['persona_count']}",
                f"- baseline rows: {coverage_report['baseline_row_count']}",
                f"- combined rows: {coverage_report['combined_row_count']}",
                f"- new eligibility leakage: {coverage_report['new_eligibility_leakage_count']}",
            ]
        )
        for registry_id, board in sorted(coverage_report["per_board"].items()):
            lines.append(
                f"- {registry_id}: rows={board['pilot_row_count']}, "
                f"new_leaks={board['new_eligibility_leakage_count']}"
            )
    _append_ledger_human(lines, report.get("observation_ledger"))
    return "\n".join(lines)


def _append_ledger_human(lines, ledger):
    if not ledger:
        return
    lines.extend(
        [
            "",
            "Durable observation",
            "- recorded: " + yes_no(ledger["recorded"]),
        ]
    )
    if not ledger["recorded"]:
        lines.append(
            f"- error: {ledger.get('failure_code') or 'observation_recording_failed'}"
            f" - {ledger.get('safe_message') or 'The observation could not be recorded.'}"
        )
        return
    lines.extend(
        [
            f"- evidence path: {ledger['path']}",
            f"- receipt path: {ledger['receipt_path']}",
            f"- ledger ID: {ledger['ledger_id']}",
            f"- ledger sequence: {ledger['ledger_sequence']}",
            f"- run ID: {ledger['run_id']}",
            f"- bundle ID: {ledger['bundle_id']}",
            f"- receipt ID: {ledger['receipt_id']}",
            f"- bundle fingerprint: {ledger['bundle_content_sha256']}",
            f"- receipt fingerprint: {ledger['receipt_content_sha256']}",
            f"- invocation status: {ledger['invocation_status']}",
        ]
    )
    for registry_id, value in sorted((ledger.get("boards") or {}).items()):
        lines.append(
            f"- {registry_id}: "
            + ("success" if value["technical_success"] else "failed")
        )


def yes_no(value):
    return "yes" if value else "no"


def select_entries(entries, requested):
    available = {entry.registry_id: entry for entry in entries}
    selected = dry_run_entries(entries, include_control=True)
    if requested:
        unknown = sorted(set(requested) - set(available))
        if unknown:
            raise SystemExit(f"Unknown registry IDs: {', '.join(unknown)}")
        selected = tuple(available[value] for value in requested)
        disabled = [entry.registry_id for entry in selected if not entry.connector_enabled_for_dry_run]
        if disabled:
            raise SystemExit(
                "Boards are disabled for connector dry runs: " + ", ".join(disabled)
            )
    return selected


def load_full_baseline_rows(db_path=DB_PATH):
    resolved = Path(db_path).resolve()
    uri = f"file:{resolved.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        live_rows = matcher.get_active_rows(conn, policy=MARKET_COUNT_POLICY_COUNT_LIVE)
        evergreen_rows = matcher.get_active_rows(
            conn,
            policy_not=MARKET_COUNT_POLICY_COUNT_LIVE,
            inventory_models=(INVENTORY_MODEL_EVERGREEN_APPLICATION,),
        )
        public_rows = matcher.get_active_rows(
            conn,
            policy_not=MARKET_COUNT_POLICY_COUNT_LIVE,
            inventory_models=(INVENTORY_MODEL_PUBLIC_INVENTORY, INVENTORY_MODEL_MIXED),
        )
        rows = [dict(row) for row in (*live_rows, *evergreen_rows, *public_rows)]
    finally:
        conn.close()
    overlay = preview.load_overlay()
    return preview.apply_overlay_to_rows(rows, overlay)


def compare_persona_coverage(
    entries,
    latest_results,
    *,
    evaluated_at,
    baseline_rows=None,
):
    baseline_rows = (
        load_full_baseline_rows() if baseline_rows is None else list(baseline_rows)
    )
    fetched_rows = rows_from_snapshots(entries, latest_results, evaluated_at=evaluated_at)
    pilot_rows = [
        row
        for row in fetched_rows
        if next(
            entry for entry in entries if entry.registry_id == row["pilot_registry_id"]
        ).is_pilot
    ]
    pilot_rows_by_registry = {
        entry.registry_id: [
            row for row in pilot_rows if row["pilot_registry_id"] == entry.registry_id
        ]
        for entry in entries
        if entry.is_pilot
    }
    fetched_rows_by_registry = {
        entry.registry_id: [
            row for row in fetched_rows if row["pilot_registry_id"] == entry.registry_id
        ]
        for entry in entries
    }
    scenarios = {
        "baseline": baseline_rows,
        **{
            registry_id: [*baseline_rows, *rows]
            for registry_id, rows in pilot_rows_by_registry.items()
        },
        "combined_pilots": [*baseline_rows, *pilot_rows],
    }
    suite = coverage.load_persona_suite(coverage.DEFAULT_SUITE)
    reports_by_scenario = {key: [] for key in scenarios}
    relevant = measure_source_relevance(
        entries,
        fetched_rows,
        evaluated_at=evaluated_at,
        suite=suite,
    )
    persona_deltas = []
    improved = []
    for persona in suite["personas"]:
        canonical = coverage.canonical_profile_for_persona(persona)
        profile = canonical_to_matcher_profile(canonical)
        persona_reports = {}
        persona_presented = {}
        persona_grouped = {}
        for scenario, scenario_rows in scenarios.items():
            report, presented, grouped = evaluate_persona_rows(
                persona,
                canonical,
                profile,
                scenario_rows,
                evaluated_at=evaluated_at,
            )
            reports_by_scenario[scenario].append(report)
            persona_reports[scenario] = report
            persona_presented[scenario] = presented
            persona_grouped[scenario] = grouped
        persona_id = persona["persona_id"]
        before = persona_reports["baseline"]
        after = persona_reports["combined_pilots"]
        delta = after["personalized_result_count"] - before["personalized_result_count"]
        before_presented = persona_presented["baseline"]
        after_presented = persona_presented["combined_pilots"]
        per_board_metrics = {
            registry_id: coverage_delta(
                before,
                persona_reports[registry_id],
                before_presented,
                persona_presented[registry_id],
                persona_grouped["baseline"],
                persona_grouped[registry_id],
            )
            for registry_id in pilot_rows_by_registry
        }
        combined_delta = coverage_delta(
            before,
            after,
            before_presented,
            after_presented,
            persona_grouped["baseline"],
            persona_grouped["combined_pilots"],
        )
        persona_deltas.append(
            {
                "persona_id": persona_id,
                "baseline_admitted_result_count": before["personalized_result_count"],
                "pilot_combined_admitted_result_count": after["personalized_result_count"],
                "per_board_result_delta": {
                    registry_id: values["admitted_result_delta"]
                    for registry_id, values in per_board_metrics.items()
                },
                "per_board_metrics": per_board_metrics,
                "result_count_delta": delta,
                **combined_delta,
                "baseline_diagnosis": before["coverage_diagnosis"],
                "combined_diagnosis": after["coverage_diagnosis"],
            }
        )
        if delta > 0:
            improved.append(persona_id)

    baseline_reports = reports_by_scenario["baseline"]
    combined_reports = reports_by_scenario["combined_pilots"]
    baseline = {"personas": baseline_reports}
    combined = {"personas": combined_reports}

    baseline_leaks = leak_keys(baseline)
    combined_leaks = leak_keys(combined)
    new_leaks = sorted(combined_leaks - baseline_leaks)
    per_board = {}
    for registry_id in pilot_rows_by_registry:
        board_report = {"personas": reports_by_scenario[registry_id]}
        board_leaks = sorted(leak_keys(board_report) - baseline_leaks)
        per_board[registry_id] = {
            "pilot_row_count": len(pilot_rows_by_registry[registry_id]),
            "new_eligibility_leakage_count": len(board_leaks),
            "new_eligibility_leakage": [
                {"persona_id": item[0], "category": item[1], "title": item[2]}
                for item in board_leaks
            ],
            "issue_counts": issue_counts(reports_by_scenario[registry_id]),
        }
    return {
        "comparison_basis": "full_read_only_product_inventory_vs_each_pilot_and_combined",
        "persona_count": len(suite["personas"]),
        "baseline_row_count": len(baseline_rows),
        "combined_row_count": len(scenarios["combined_pilots"]),
        "per_board": per_board,
        "personas_with_more_personalized_results": improved,
        "persona_deltas": persona_deltas,
        "new_eligibility_leakage_count": len(new_leaks),
        "new_eligibility_leakage": [
            {"persona_id": item[0], "category": item[1], "title": item[2]}
            for item in new_leaks
        ],
        "baseline_issue_counts": issue_counts(baseline_reports),
        "combined_issue_counts": issue_counts(combined_reports),
        "relevant_external_ids_by_registry": {
            registry_id: sorted(relevant.get(registry_id, ()), key=numeric_text_key)
            for registry_id in fetched_rows_by_registry
        },
        "matching_or_ranking_changed": False,
    }


def measure_source_relevance(entries, fetched_rows, *, evaluated_at, suite=None):
    """Return source IDs admitted for at least one reviewed persona."""
    entries = tuple(entries)
    suite = suite or coverage.load_persona_suite(coverage.DEFAULT_SUITE)
    rows_by_registry = {
        entry.registry_id: [
            row for row in fetched_rows if row["pilot_registry_id"] == entry.registry_id
        ]
        for entry in entries
    }
    by_job_id = {int(row["job_id"]): row for row in fetched_rows}
    relevant = {entry.registry_id: set() for entry in entries}
    for persona in suite["personas"]:
        canonical = coverage.canonical_profile_for_persona(persona)
        profile = canonical_to_matcher_profile(canonical)
        for registry_id, source_rows in rows_by_registry.items():
            _report, presented, _grouped = evaluate_persona_rows(
                persona,
                canonical,
                profile,
                source_rows,
                evaluated_at=evaluated_at,
            )
            for match in presented:
                row = by_job_id.get(int(match["job_id"]))
                if row is not None:
                    relevant[registry_id].add(row["external_id"])
    return relevant


def evaluate_persona_rows(persona, canonical, profile, rows, *, evaluated_at):
    grouped = preview.build_grouped_matches_from_rows(
        profile,
        rows,
        max(1, len(rows)),
        evaluated_at=evaluated_at,
    )
    report = coverage.evaluate_persona(
        persona,
        canonical,
        grouped,
        5,
        synthetic_contract=None,
        total_inventory_candidates=len(rows),
        display_limit=max(1, len(rows)),
    )
    limit = sum(len(grouped.get(section, [])) for section in PERSONALIZED_SECTIONS)
    presented = product_app.build_browser_presentation_matches(
        {"matches": grouped},
        limit=max(1, limit),
    )
    return report, presented, grouped


def match_titles(matches):
    return {str(match.get("display_title") or "") for match in matches}


def match_sources(matches):
    return {str(match.get("source") or "") for match in matches}


def source_diversity(matches):
    return len(match_sources(matches))


def source_concentration(matches):
    if not matches:
        return 0.0
    counts = {}
    for match in matches:
        source = str(match.get("source") or "")
        counts[source] = counts.get(source, 0) + 1
    return round(max(counts.values()) / len(matches), 4)


def coverage_delta(
    before,
    after,
    before_presented,
    after_presented,
    before_grouped,
    after_grouped,
):
    before_region_rejected = region_rejection_count(before_grouped)
    after_region_rejected = region_rejection_count(after_grouped)
    before_qualification_rejected = qualification_rejection_count(before_grouped)
    after_qualification_rejected = qualification_rejection_count(after_grouped)
    return {
        "admitted_result_delta": (
            after["personalized_result_count"] - before["personalized_result_count"]
        ),
        "strong_family_delta": (
            after["live_inventory_strong_family_coverage"]["strong_family_results"]
            - before["live_inventory_strong_family_coverage"]["strong_family_results"]
        ),
        "company_diversity_delta": source_diversity(after_presented)
        - source_diversity(before_presented),
        "source_diversity_delta": source_diversity(after_presented)
        - source_diversity(before_presented),
        "location_compatible_delta": (
            after["coverage_funnel"]["location_compatible_candidates"]
            - before["coverage_funnel"]["location_compatible_candidates"]
        ),
        "region_rejected_count": after_region_rejected,
        "region_rejected_delta": after_region_rejected - before_region_rejected,
        "credential_language_seniority_rejection_count": after_qualification_rejected,
        "credential_language_seniority_rejection_delta": (
            after_qualification_rejected - before_qualification_rejected
        ),
        "new_titles": sorted(match_titles(after_presented) - match_titles(before_presented)),
        "new_companies": sorted(match_sources(after_presented) - match_sources(before_presented)),
        "source_concentration": source_concentration(after_presented),
    }


def grouped_matches(grouped):
    return [
        match
        for section in preview.SECTION_ORDER
        for match in grouped.get(section, [])
    ]


def region_rejection_count(grouped):
    return sum(
        1
        for match in grouped_matches(grouped)
        if match.get("location_eligibility_status") == "incompatible"
        and regions_in_location(
            match.get("applicant_location_requirements") or match.get("location")
        )
    )


def qualification_rejection_count(grouped):
    rejected = 0
    for match in grouped_matches(grouped):
        cap_reasons = set(match.get("actionability_cap_reasons") or [])
        admission_reasons = set(match.get("primary_admission_reasons") or [])
        language_rejected = (
            match.get("eligible_for_personalized") is False
            or bool(match.get("unsupported_languages"))
        )
        credential_rejected = (
            "explicit_credential_incompatibility" in cap_reasons
            or bool(match.get("professional_domain_hard_gate_applied"))
        )
        seniority_rejected = any(
            "senior" in str(reason).casefold()
            or "experience" in str(reason).casefold()
            for reason in (*cap_reasons, *admission_reasons)
        )
        if language_rejected or credential_rejected or seniority_rejected:
            rejected += 1
    return rejected


def issue_counts(reports):
    return {
        "specialist_mismatches": sum(len(row["specialist_mismatches"]) for row in reports),
        "unsupported_language_leaks": sum(
            len(row["unsupported_language_leaks"]) for row in reports
        ),
        "location_leaks": sum(len(row["location_leaks"]) for row in reports),
        "credential_leaks": sum(len(row["credential_leaks"]) for row in reports),
        "explanation_quality_findings": sum(
            len(row["explanation_quality_findings"]) for row in reports
        ),
    }


def leak_keys(report):
    keys = set()
    for persona in report["personas"]:
        for category, field in LEAK_FIELDS.items():
            for item in persona[field]:
                keys.add((persona["persona_id"], category, item.get("title") or ""))
    return keys


def rows_from_snapshots(entries, latest_results, *, evaluated_at):
    rows = []
    observed = evaluated_at.isoformat()
    for source_index, entry in enumerate(entries, start=1):
        records = {
            record.external_id: record
            for record in latest_results[entry.registry_id].source_records
        }
        for candidate in latest_results[entry.registry_id].jobs:
            record = records[candidate.external_id]
            future_role = is_future_role(candidate.title)
            local_field_role = is_testlio_local_field_role(entry, candidate.title, record)
            report_separately = future_role or local_field_role
            location_requirements = combined_location_requirements(record)
            rows.append(
                {
                    "job_id": source_index * 10_000_000_000 + int(candidate.external_id),
                    "external_id": candidate.external_id,
                    "title": candidate.title,
                    "canonical_title": candidate.title,
                    "location": candidate.location,
                    "applicant_location_requirements": location_requirements,
                    "url": candidate.url,
                    "department": candidate.department,
                    "expertise": candidate.expertise,
                    "commitment": candidate.commitment,
                    "source_category": candidate.expertise or candidate.department or "Unknown",
                    "source": entry.company_name,
                    "source_slug": entry.company_id,
                    "source_tier": "core",
                    "inventory_model": (
                        "evergreen_application" if future_role else "corporate_careers"
                    ),
                    "market_count_policy": (
                        "report_separately" if report_separately else "count_live"
                    ),
                    "opportunity_kind": (
                        "application_portal"
                        if future_role
                        else "local_field_project"
                        if local_field_role
                        else "live_posting"
                    ),
                    "availability_basis": "always_open" if future_role else "api_feed",
                    "include_in_live_market_estimate": 0 if report_separately else 1,
                    "canonical_opportunity_id": None,
                    "job_is_active": True,
                    "canonical_is_active": True,
                    "job_last_seen_at": observed,
                    "latest_successful_source_run_at": observed,
                    "source_run_started_at": observed,
                    "source_run_id": source_index,
                    "source_run_qualifies": True,
                    "language": None,
                    "language_locale": None,
                    "required_languages": None,
                    "description": plain_text(record.description_html),
                    "pilot_registry_id": entry.registry_id,
                }
            )
    return rows


def plain_text(value):
    without_tags = re.sub(r"<[^>]+>", " ", str(value or ""))
    return " ".join(html.unescape(without_tags).split())


def combined_location_requirements(record):
    values = [
        value
        for value in (record.location, *record.additional_locations)
        if str(value or "").strip().casefold() not in {"remote", "distributed"}
    ]
    return " | ".join(dict.fromkeys(value for value in values if value))


def is_testlio_local_field_role(entry, title, record):
    if entry.company_id != "testlio":
        return False
    text = " ".join(
        (str(title or ""), str(record.location or ""), plain_text(record.description_html))
    ).casefold()
    return "airport" in text and any(
        marker in text for marker in ("on-site", "onsite", "in person", "field")
    )


def parse_evaluated_at(value):
    if not value:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def numeric_text_key(value):
    text = str(value)
    return (0, int(text)) if text.isdigit() else (1, text)


if __name__ == "__main__":
    raise SystemExit(main())

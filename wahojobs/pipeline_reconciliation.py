from __future__ import annotations

import json

from wahojobs import pipeline_state
from wahojobs import pipeline_transition_metadata as transition_metadata
from wahojobs.pipeline_actions import (
    UnresolvedLegacyWorkflow,
    legacy_compatibility_from_state,
)


MIGRATION_VERSION = "001_pipeline_state"
REQUIRED_OBJECTS = {
    ("index", "idx_user_pipeline_items_pipeline_profile"),
    ("table", "user_pipeline_state"),
    ("table", "user_pipeline_transitions"),
    ("index", "idx_user_pipeline_transitions_pipeline_occurred"),
    ("index", "idx_user_pipeline_transitions_profile_occurred"),
    ("index", "idx_user_pipeline_transitions_undo"),
    ("index", "idx_user_pipeline_transitions_correction"),
    ("index", "idx_user_pipeline_transitions_occurred"),
    ("table", "wahojobs_schema_migrations"),
    ("trigger", "trg_user_pipeline_transitions_no_update"),
    ("trigger", "trg_user_pipeline_transitions_no_delete"),
}
BLOCKING_CHECKS = {
    "missing_projections",
    "duplicate_projections",
    "orphan_projections",
    "owner_mismatches",
    "projection_version_mismatches",
    "latest_transition_state_mismatches",
    "non_contiguous_version_chains",
    "duplicate_transition_ids",
    "duplicate_idempotency_keys",
    "invalid_transition_references",
    "malformed_transition_states",
    "transition_before_state_mismatches",
    "invalid_transition_dimensions",
    "invalid_initialization_transitions",
    "invalid_operation_noops",
    "invalid_terminal_operation_metadata",
    "invalid_undo_references",
    "invalid_correction_references",
    "branching_transition_references",
    "cyclic_transition_references",
    "legacy_status_mismatches",
    "reminder_mirror_mismatches",
    "visible_unresolved_workflows",
    "applicant_update_expectation_mismatches",
}
COMPATIBILITY_MIRROR_CHECKS = frozenset(
    {
        "legacy_status_mismatches",
        "reminder_mirror_mismatches",
    }
)


def normalized_read_blocking_reasons(report: dict) -> list[str]:
    """Return findings that make authoritative normalized reads unsafe."""
    return [
        reason
        for reason in report.get("blocking_reasons", [])
        if reason not in COMPATIBILITY_MIRROR_CHECKS
    ]


def is_safe_for_normalized_reads(report: dict) -> bool:
    return not normalized_read_blocking_reasons(report)


def _finalize_report(report: dict, blocking_reasons: list[str]) -> dict:
    compatibility_reasons = [
        reason for reason in blocking_reasons if reason in COMPATIBILITY_MIRROR_CHECKS
    ]
    normalized_reasons = [
        reason for reason in blocking_reasons if reason not in COMPATIBILITY_MIRROR_CHECKS
    ]
    report["blocking"] = bool(blocking_reasons)
    report["blocking_reasons"] = blocking_reasons
    report["fully_reconciled"] = not blocking_reasons
    report["safe_for_normalized_reads"] = not normalized_reasons
    report["normalized_read_blocking_reasons"] = normalized_reasons
    report["compatibility_mirror_drift_reasons"] = compatibility_reasons
    return report


def reconcile_pipeline_state(conn) -> dict:
    """Inspect pipeline state without installing, backfilling, or repairing it."""
    objects = {
        (row["type"], row["name"])
        for row in conn.execute(
            "SELECT type, name FROM sqlite_master WHERE type IN ('table','index','trigger','view')"
        )
    }
    missing_objects = sorted(
        {f"{kind}:{name}" for kind, name in REQUIRED_OBJECTS - objects}
    )
    present_objects = sorted(
        {f"{kind}:{name}" for kind, name in REQUIRED_OBJECTS & objects}
    )
    marker_present = False
    if ("table", "wahojobs_schema_migrations") in objects:
        marker_present = conn.execute(
            "SELECT 1 FROM wahojobs_schema_migrations WHERE version = ?",
            (MIGRATION_VERSION,),
        ).fetchone() is not None

    report = {
        "schema": {
            "migration_version": MIGRATION_VERSION,
            "migration_marker_present": marker_present,
            "required_objects_present": present_objects,
            "required_objects_missing": missing_objects,
        },
        "counts": {
            "pipeline_items": _table_count(conn, objects, "user_pipeline_items"),
            "projections": _table_count(conn, objects, "user_pipeline_state"),
            "transitions": _table_count(conn, objects, "user_pipeline_transitions"),
            "applicant_updates": _table_count(conn, objects, "applicant_status_updates"),
        },
        "checks": {},
        "blocking": False,
    }
    if missing_objects or not marker_present:
        report["checks"] = _empty_checks()
        return _finalize_report(report, ["migration_schema_incomplete"])

    checks = _empty_checks()
    checks["missing_projections"] = _rows(
        conn,
        """
        SELECT i.pipeline_item_id
        FROM user_pipeline_items i
        LEFT JOIN user_pipeline_state s ON s.pipeline_item_id = i.pipeline_item_id
        WHERE s.pipeline_item_id IS NULL
        ORDER BY i.pipeline_item_id
        """,
    )
    checks["duplicate_projections"] = _rows(
        conn,
        """
        SELECT pipeline_item_id, COUNT(*) AS count
        FROM user_pipeline_state
        GROUP BY pipeline_item_id HAVING COUNT(*) > 1
        ORDER BY pipeline_item_id
        """,
    )
    checks["orphan_projections"] = _rows(
        conn,
        """
        SELECT s.pipeline_item_id
        FROM user_pipeline_state s
        LEFT JOIN user_pipeline_items i ON i.pipeline_item_id = s.pipeline_item_id
        WHERE i.pipeline_item_id IS NULL
        ORDER BY s.pipeline_item_id
        """,
    )
    checks["owner_mismatches"] = _rows(
        conn,
        """
        SELECT DISTINCT t.pipeline_item_id, i.profile_id AS item_profile_id,
               t.profile_id AS transition_profile_id
        FROM user_pipeline_transitions t
        JOIN user_pipeline_items i ON i.pipeline_item_id = t.pipeline_item_id
        WHERE t.profile_id <> i.profile_id
        ORDER BY t.pipeline_item_id, t.profile_id
        """,
    )
    checks["projection_version_mismatches"] = _rows(
        conn,
        """
        SELECT s.pipeline_item_id, s.version AS projection_version,
               COALESCE(MAX(t.state_version_after), 0) AS ledger_version
        FROM user_pipeline_state s
        LEFT JOIN user_pipeline_transitions t ON t.pipeline_item_id = s.pipeline_item_id
        GROUP BY s.pipeline_item_id, s.version
        HAVING s.version <> COALESCE(MAX(t.state_version_after), 0)
        ORDER BY s.pipeline_item_id
        """,
    )
    checks.update(_ledger_diagnostics(conn))
    checks["duplicate_transition_ids"] = _rows(
        conn,
        """
        SELECT transition_id, COUNT(*) AS count
        FROM user_pipeline_transitions
        GROUP BY transition_id HAVING COUNT(*) > 1
        ORDER BY transition_id
        """,
    )
    checks["duplicate_idempotency_keys"] = _rows(
        conn,
        """
        SELECT idempotency_key, COUNT(*) AS count
        FROM user_pipeline_transitions
        GROUP BY idempotency_key HAVING COUNT(*) > 1
        ORDER BY idempotency_key
        """,
    )
    mirror = _mirror_diagnostics(conn)
    checks.update(mirror)
    checks["applicant_update_expectation_mismatches"] = _applicant_mismatches(conn)
    report["unknown_legacy_workflows"] = _unknown_workflow_counts(conn)
    report["transition_classes"] = _transition_class_counts(conn)
    report["checks"] = checks
    blocking_reasons = [name for name in sorted(BLOCKING_CHECKS) if checks[name]]
    return _finalize_report(report, blocking_reasons)


def _empty_checks():
    return {name: [] for name in sorted(BLOCKING_CHECKS)}


def _table_count(conn, objects, table):
    if ("table", table) not in objects:
        return None
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _rows(conn, sql, params=()):
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _ledger_diagnostics(conn):
    names = {
        "latest_transition_state_mismatches": [],
        "non_contiguous_version_chains": [],
        "invalid_transition_references": [],
        "malformed_transition_states": [],
        "transition_before_state_mismatches": [],
        "invalid_transition_dimensions": [],
        "invalid_initialization_transitions": [],
        "invalid_operation_noops": [],
        "invalid_terminal_operation_metadata": [],
        "invalid_undo_references": [],
        "invalid_correction_references": [],
        "branching_transition_references": [],
        "cyclic_transition_references": [],
    }
    raw_rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT * FROM user_pipeline_transitions
            ORDER BY pipeline_item_id, state_version_after, occurred_at, transition_id
            """
        )
    ]
    entries = []
    for row in raw_rows:
        try:
            before_raw = json.loads(row["before_state_json"])
            after_raw = json.loads(row["after_state_json"])
            metadata = json.loads(row["metadata_json"] or "{}")
            before = None if before_raw is None else pipeline_state.validate_state(before_raw)
            after = pipeline_state.validate_state(after_raw)
            if not isinstance(metadata, dict):
                raise ValueError("metadata is not an object")
        except (json.JSONDecodeError, TypeError, ValueError, pipeline_state.PipelineStateError):
            names["malformed_transition_states"].append(
                {"pipeline_item_id": row["pipeline_item_id"], "transition_id": row["transition_id"]}
            )
            before = after = None
            metadata = {}
        entries.append(
            {
                "row": row,
                "before": before,
                "after": after,
                "metadata": metadata,
                "parsed": after is not None,
            }
        )

    by_item = {}
    by_id = {}
    for entry in entries:
        row = entry["row"]
        by_item.setdefault(row["pipeline_item_id"], []).append(entry)
        by_id.setdefault(row["transition_id"], entry)

    for pipeline_item_id, history in sorted(by_item.items()):
        persisted_item = conn.execute(
            "SELECT * FROM user_pipeline_items WHERE pipeline_item_id = ?",
            (pipeline_item_id,),
        ).fetchone()
        previous = None
        expected_before_version = 0
        for index, entry in enumerate(history):
            row = entry["row"]
            before = entry["before"]
            after = entry["after"]
            transition_class = entry["metadata"].get("transition_class")
            if (
                row["state_version_before"] != expected_before_version
                or row["state_version_after"] != expected_before_version + 1
            ):
                names["non_contiguous_version_chains"].append(
                    {
                        "pipeline_item_id": pipeline_item_id,
                        "transition_id": row["transition_id"],
                        "expected_version_before": expected_before_version,
                        "actual_version_before": row["state_version_before"],
                        "actual_version_after": row["state_version_after"],
                    }
                )
            expected_before_version = row["state_version_after"]
            if not entry["parsed"]:
                previous = entry
                continue
            if index == 0:
                if before is not None:
                    names["transition_before_state_mismatches"].append(
                        {"pipeline_item_id": pipeline_item_id, "transition_id": row["transition_id"]}
                    )
                initialization_error = _initialization_error(entry, persisted_item)
                if initialization_error is not None:
                    names["invalid_initialization_transitions"].append(
                        {
                            "pipeline_item_id": pipeline_item_id,
                            "transition_id": row["transition_id"],
                            "reason": initialization_error,
                        }
                    )
            else:
                previous_after = previous["after"] if previous else None
                if previous_after is None or pipeline_state.canonical_json(before) != pipeline_state.canonical_json(previous_after):
                    names["transition_before_state_mismatches"].append(
                        {"pipeline_item_id": pipeline_item_id, "transition_id": row["transition_id"]}
                    )
                if transition_class == "user_initialization":
                    names["invalid_initialization_transitions"].append(
                        {"pipeline_item_id": pipeline_item_id, "transition_id": row["transition_id"]}
                    )
            if transition_class == "operation_noop":
                if not _valid_operation_noop(entry):
                    names["invalid_operation_noops"].append(
                        {"pipeline_item_id": pipeline_item_id, "transition_id": row["transition_id"]}
                    )
            elif transition_class != "user_initialization" and row["affected_dimension"] != "baseline":
                if not _valid_dimension_change(entry):
                    names["invalid_transition_dimensions"].append(
                        {
                            "pipeline_item_id": pipeline_item_id,
                            "transition_id": row["transition_id"],
                            "affected_dimension": row["affected_dimension"],
                        }
                    )
            if _is_product_terminal(entry):
                try:
                    product = entry["metadata"].get("pipeline_action")
                    snapshot = product.get("result_snapshot") if type(product) is dict else None
                    preparatory_ids = (
                        snapshot.get("preparatory_transition_ids")
                        if type(snapshot) is dict
                        else []
                    )
                    preparatory_entries = (
                        [by_id[value] for value in preparatory_ids if value in by_id]
                        if type(preparatory_ids) is list
                        else []
                    )
                    transition_metadata.validate_terminal_operation_metadata(
                        metadata=entry["metadata"],
                        transition=_entry_transition(entry),
                        persisted_item=persisted_item,
                        preparatory_transitions=[
                            _entry_transition(value) for value in preparatory_entries
                        ],
                    )
                except transition_metadata.TransitionMetadataError as exc:
                    names["invalid_terminal_operation_metadata"].append(
                        {
                            "pipeline_item_id": pipeline_item_id,
                            "transition_id": row["transition_id"],
                            "reason": exc.code,
                        }
                    )
            previous = entry

        projection = conn.execute(
            "SELECT * FROM user_pipeline_state WHERE pipeline_item_id = ?",
            (pipeline_item_id,),
        ).fetchone()
        latest = history[-1] if history else None
        if projection is not None and latest is not None:
            if latest["after"] is None or (
                pipeline_state.canonical_json(pipeline_state.projection_state(projection))
                != pipeline_state.canonical_json(latest["after"])
                or projection["version"] != latest["row"]["state_version_after"]
            ):
                names["latest_transition_state_mismatches"].append(
                    {"pipeline_item_id": pipeline_item_id, "transition_id": latest["row"]["transition_id"]}
                )
        owner = conn.execute(
            "SELECT profile_id FROM user_pipeline_items WHERE pipeline_item_id = ?",
            (pipeline_item_id,),
        ).fetchone()
        if owner is not None:
            try:
                pipeline_state.list_effective_funnel_transitions(
                    conn, pipeline_item_id, owner["profile_id"]
                )
            except (
                pipeline_state.PipelineStateError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
            ):
                names["invalid_transition_references"].append(
                    {
                        "pipeline_item_id": pipeline_item_id,
                        "reason": "effective_history_validation_failed",
                    }
                )

    children = {}
    for entry in entries:
        row = entry["row"]
        for kind, reference in (
            ("undo", row["undo_of_transition_id"]),
            ("correction", row["correction_of_transition_id"]),
        ):
            if reference is None:
                continue
            children.setdefault(reference, []).append(row["transition_id"])
            parent = by_id.get(reference)
            invalid = _invalid_reference(entry, parent, kind)
            if invalid:
                target = "invalid_undo_references" if kind == "undo" else "invalid_correction_references"
                detail = {
                    "transition_id": row["transition_id"],
                    "referenced_transition_id": reference,
                    "reason": invalid,
                }
                names[target].append(detail)
                names["invalid_transition_references"].append(detail)
    for parent, child_ids in sorted(children.items()):
        if len(child_ids) > 1:
            names["branching_transition_references"].append(
                {"referenced_transition_id": parent, "child_transition_ids": sorted(child_ids)}
            )
    names["cyclic_transition_references"] = _reference_cycles(entries, by_id)
    names["invalid_transition_references"].extend(
        names["branching_transition_references"] + names["cyclic_transition_references"]
    )
    names["invalid_initialization_transitions"].extend(
        _initialization_binding_diagnostics(conn, entries)
    )
    return names


def _initialization_error(entry, persisted_item):
    row = entry["row"]
    metadata = entry["metadata"]
    transition = _entry_transition(entry)
    try:
        if row["affected_dimension"] == "baseline":
            transition_metadata.validate_legacy_baseline_metadata(
                metadata=metadata, transition=transition
            )
            return None
        if metadata.get("transition_class") == "user_initialization":
            transition_metadata.validate_user_initialization_metadata(
                metadata=metadata, transition=transition
            )
            return None if persisted_item is not None else "missing_initialization_item"
    except transition_metadata.TransitionMetadataError as exc:
        return exc.code
    return "unsupported_initialization_shape"


def _valid_operation_noop(entry):
    row = entry["row"]
    before = entry["before"]
    after = entry["after"]
    return (
        row["affected_dimension"] == "workflow"
        and row["action_name"].startswith("product_noop_")
        and before is not None
        and after is not None
        and pipeline_state.canonical_json(before) == pipeline_state.canonical_json(after)
        and type(entry["metadata"].get("pipeline_action")) is dict
    )


def _entry_transition(entry):
    row = entry["row"]
    return {
        "transition_id": row["transition_id"],
        "pipeline_item_id": row["pipeline_item_id"],
        "profile_id": row["profile_id"],
        "affected_dimension": row["affected_dimension"],
        "action_name": row["action_name"],
        "before_state": entry["before"],
        "after_state": entry["after"],
        "occurred_at": row["occurred_at"],
        "actor_source": row["actor_source"],
        "idempotency_key": row["idempotency_key"],
        "request_fingerprint": row["request_fingerprint"],
        "state_version_before": row["state_version_before"],
        "state_version_after": row["state_version_after"],
        "undo_of_transition_id": row["undo_of_transition_id"],
        "correction_of_transition_id": row["correction_of_transition_id"],
        "metadata": entry["metadata"],
    }


def _is_product_terminal(entry):
    row = entry["row"]
    metadata = entry["metadata"]
    return (
        metadata.get("transition_class") == "operation_noop"
        or "pipeline_action" in metadata
        or (
            row["actor_source"] == "product_action"
            and metadata.get("transition_class") != "user_initialization"
            and metadata.get("resolution") != "post_migration_user_action"
        )
    )


def _valid_dimension_change(entry):
    row = entry["row"]
    before = entry["before"]
    after = entry["after"]
    if before is None or after is None:
        return False
    changed = {key for key in before if before[key] != after[key]}
    dimension = row["affected_dimension"]
    if dimension == "workflow":
        if not changed or not changed.issubset({"workflow_status", "workflow_status_provenance"}):
            return False
        before_status = before["workflow_status"]
        after_status = after["workflow_status"]
        if before_status is None:
            return (
                before["workflow_status_provenance"] == "unknown_legacy"
                and after_status in pipeline_state.WORKFLOW_STATUSES
                and after["workflow_status_provenance"] == "known"
                and entry["metadata"].get("resolution") == "post_migration_user_action"
            )
        return after_status in pipeline_state.WORKFLOW_TRANSITIONS.get(before_status, set())
    if dimension == "visibility":
        return changed == {"visibility"}
    if dimension == "reminder":
        return changed == {"reminder_at"}
    if dimension in {"undo", "correction"}:
        return True
    return False


def _invalid_reference(child, parent, kind):
    row = child["row"]
    if parent is None:
        return "missing_reference"
    parent_row = parent["row"]
    if parent_row["pipeline_item_id"] != row["pipeline_item_id"]:
        return "cross_item_reference"
    if parent_row["profile_id"] != row["profile_id"]:
        return "cross_owner_reference"
    if parent_row["state_version_after"] >= row["state_version_after"]:
        return "reference_is_not_earlier"
    parent_class = parent["metadata"].get("transition_class")
    if parent_row["affected_dimension"] == "baseline":
        return "migration_baseline_reference"
    if parent_class == "user_initialization":
        return "user_initialization_reference"
    if parent_class == "operation_noop":
        return "operation_noop_reference"
    if kind == "undo" and parent_row["affected_dimension"] in {"baseline", "undo"}:
        return "invalid_undo_target"
    if kind == "correction" and parent_row["affected_dimension"] not in {"workflow", "correction"}:
        return "invalid_correction_target"
    return None


def _reference_cycles(entries, by_id):
    cycles = []
    for entry in entries:
        seen = set()
        current = entry
        while current is not None:
            transition_id = current["row"]["transition_id"]
            if transition_id in seen:
                cycles.append({"transition_id": entry["row"]["transition_id"]})
                break
            seen.add(transition_id)
            reference = current["row"]["undo_of_transition_id"] or current["row"]["correction_of_transition_id"]
            current = by_id.get(reference) if reference else None
    return sorted(cycles, key=lambda row: row["transition_id"])


def _initialization_binding_diagnostics(conn, entries):
    diagnostics = []
    references = {}
    for entry in entries:
        metadata = entry["metadata"]
        product = metadata.get("pipeline_action") if type(metadata) is dict else None
        snapshot = product.get("result_snapshot") if type(product) is dict else None
        preparatory_ids = (
            snapshot.get("preparatory_transition_ids") if type(snapshot) is dict else None
        )
        if type(preparatory_ids) is list:
            for transition_id in preparatory_ids:
                if type(transition_id) is str:
                    references.setdefault(transition_id, []).append(entry)

    for initialization in entries:
        metadata = initialization["metadata"]
        if metadata.get("transition_class") != "user_initialization":
            continue
        row = initialization["row"]
        safe = {
            "pipeline_item_id": row["pipeline_item_id"],
            "transition_id": row["transition_id"],
        }
        terminals = references.get(row["transition_id"], [])
        if not terminals:
            diagnostics.append(
                {**safe, "reason": "user_initialization_terminal_link_missing"}
            )
            continue
        if len(terminals) != 1:
            diagnostics.append(
                {**safe, "reason": "user_initialization_terminal_link_ambiguous"}
            )
            continue
        persisted_item = conn.execute(
            "SELECT * FROM user_pipeline_items WHERE pipeline_item_id = ?",
            (row["pipeline_item_id"],),
        ).fetchone()
        try:
            transition_metadata.validate_user_initialization_binding(
                initialization_transition=_entry_transition(initialization),
                terminal_transition=_entry_transition(terminals[0]),
                persisted_item=persisted_item,
            )
        except transition_metadata.TransitionMetadataError as exc:
            diagnostics.append({**safe, "reason": exc.code})
    return diagnostics


def _mirror_diagnostics(conn):
    status_mismatches = []
    reminder_mismatches = []
    visible_unresolved = []
    rows = conn.execute(
        """
        SELECT i.pipeline_item_id, i.status, i.reminder_date, s.*
        FROM user_pipeline_items i
        JOIN user_pipeline_state s ON s.pipeline_item_id = i.pipeline_item_id
        ORDER BY i.pipeline_item_id
        """
    ).fetchall()
    for row in rows:
        state = pipeline_state.projection_state(row)
        if (
            state["workflow_status"] is None
            and state["visibility"] == "visible"
            and state["reminder_at"] is None
        ):
            visible_unresolved.append({"pipeline_item_id": row["pipeline_item_id"]})
        try:
            expected = legacy_compatibility_from_state(state)
        except UnresolvedLegacyWorkflow:
            continue
        if row["status"] != expected["status"]:
            status_mismatches.append(
                {
                    "pipeline_item_id": row["pipeline_item_id"],
                    "actual": row["status"],
                    "expected": expected["status"],
                }
            )
        actual_reminder = row["reminder_date"] or ""
        if actual_reminder != expected["reminder_date"]:
            reminder_mismatches.append(
                {
                    "pipeline_item_id": row["pipeline_item_id"],
                    "actual": actual_reminder,
                    "expected": expected["reminder_date"],
                }
            )
    return {
        "legacy_status_mismatches": status_mismatches,
        "reminder_mirror_mismatches": reminder_mismatches,
        "visible_unresolved_workflows": visible_unresolved,
    }


def _applicant_mismatches(conn):
    mismatches = []
    rows = conn.execute(
        """
        SELECT t.*, i.user_id AS item_user_id, i.profile_id AS item_profile_id,
               i.source AS item_source, i.opportunity_title AS item_title,
               i.opportunity_url AS item_url, i.id AS item_id,
               i.opportunity_external_id AS item_external_id,
               i.canonical_id AS item_canonical_id
        FROM user_pipeline_transitions t
        JOIN user_pipeline_items i ON i.pipeline_item_id = t.pipeline_item_id
        ORDER BY t.state_version_after, t.transition_id
        """
    ).fetchall()
    expectations = {}
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
            before_raw = json.loads(row["before_state_json"])
            after_raw = json.loads(row["after_state_json"])
            before = None if before_raw is None else pipeline_state.validate_state(before_raw)
            after = pipeline_state.validate_state(after_raw)
        except (json.JSONDecodeError, TypeError, pipeline_state.PipelineStateError):
            continue
        entry = {
            "row": dict(row),
            "before": before,
            "after": after,
            "metadata": metadata,
        }
        if not _is_product_terminal(entry):
            continue
        try:
            validated = transition_metadata.validate_terminal_operation_metadata(
                metadata=metadata,
                transition=_entry_transition(entry),
                persisted_item={
                    "id": row["item_id"],
                    "pipeline_item_id": row["pipeline_item_id"],
                    "user_id": row["item_user_id"],
                    "profile_id": row["item_profile_id"],
                    "source": row["item_source"],
                    "opportunity_title": row["item_title"],
                    "opportunity_url": row["item_url"],
                    "opportunity_external_id": row["item_external_id"],
                    "canonical_id": row["item_canonical_id"],
                },
            )
        except transition_metadata.TransitionMetadataError as exc:
            mismatches.append(
                {
                    "transition_id": row["transition_id"],
                    "reason": exc.code,
                }
            )
            continue
        receipt = validated["applicant_receipt"]
        if receipt is None:
            continue
        applicant = receipt["applicant_update"]
        update_id = applicant["update_id"]
        ordering = (row["state_version_after"], row["transition_id"])
        previous = expectations.get(update_id)
        if previous is None or ordering > previous["ordering"]:
            expectations[update_id] = {
                "ordering": ordering,
                "transition_id": row["transition_id"],
                "expected": applicant,
            }

    for update_id, expectation in sorted(expectations.items()):
        current = conn.execute(
            "SELECT * FROM applicant_status_updates WHERE update_id = ?",
            (update_id,),
        ).fetchone()
        if current is None:
            mismatches.append(
                {
                    "transition_id": expectation["transition_id"],
                    "expected_applicant_update_id": update_id,
                    "reason": "missing_applicant_row",
                }
            )
            continue
        changed = [
            field
            for field in transition_metadata.deterministic_applicant_fields()
            if current[field] != expectation["expected"][field]
        ]
        if changed:
            mismatches.append(
                {
                    "transition_id": expectation["transition_id"],
                    "expected_applicant_update_id": update_id,
                    "reason": "applicant_content_mismatch",
                    "fields": changed,
                }
            )
    return mismatches


def _transition_class_counts(conn):
    counts = {
        "migration_baselines": 0,
        "user_initializations": 0,
        "operation_noops": 0,
    }
    rows = conn.execute(
        """
        SELECT t.*, i.id AS item_id, i.user_id AS item_user_id,
               i.profile_id AS item_profile_id, i.source AS item_source,
               i.opportunity_title AS item_title, i.opportunity_url AS item_url,
               i.opportunity_external_id AS item_external_id, i.canonical_id AS item_canonical_id
        FROM user_pipeline_transitions t
        JOIN user_pipeline_items i ON i.pipeline_item_id = t.pipeline_item_id
        ORDER BY t.pipeline_item_id, t.state_version_after, t.transition_id
        """
    ).fetchall()
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
            before_raw = json.loads(row["before_state_json"])
            after_raw = json.loads(row["after_state_json"])
            before = None if before_raw is None else pipeline_state.validate_state(before_raw)
            after = pipeline_state.validate_state(after_raw)
        except (json.JSONDecodeError, TypeError, pipeline_state.PipelineStateError):
            continue
        if type(metadata) is not dict:
            continue
        transition = _entry_transition(
            {"row": dict(row), "before": before, "after": after, "metadata": metadata}
        )
        try:
            if row["affected_dimension"] == "baseline":
                transition_metadata.validate_legacy_baseline_metadata(
                    metadata=metadata, transition=transition
                )
                counts["migration_baselines"] += 1
            elif metadata.get("transition_class") == "user_initialization":
                transition_metadata.validate_user_initialization_metadata(
                    metadata=metadata, transition=transition
                )
                counts["user_initializations"] += 1
            elif metadata.get("transition_class") == "operation_noop":
                persisted_item = {
                    "id": row["item_id"],
                    "pipeline_item_id": row["pipeline_item_id"],
                    "user_id": row["item_user_id"],
                    "profile_id": row["item_profile_id"],
                    "source": row["item_source"],
                    "opportunity_title": row["item_title"],
                    "opportunity_url": row["item_url"],
                    "opportunity_external_id": row["item_external_id"],
                    "canonical_id": row["item_canonical_id"],
                }
                transition_metadata.validate_terminal_operation_metadata(
                    metadata=metadata,
                    transition=transition,
                    persisted_item=persisted_item,
                )
                counts["operation_noops"] += 1
        except transition_metadata.TransitionMetadataError:
            continue
    return counts


def _unknown_workflow_counts(conn):
    rows = conn.execute(
        """
        SELECT visibility,
               CASE WHEN reminder_at IS NULL THEN 'without_reminder' ELSE 'with_reminder' END AS reminder_shape,
               COUNT(*) AS count
        FROM user_pipeline_state
        WHERE workflow_status IS NULL
        GROUP BY visibility, reminder_shape
        ORDER BY visibility, reminder_shape
        """
    ).fetchall()
    return {
        "total": sum(row["count"] for row in rows),
        "groups": [dict(row) for row in rows],
    }

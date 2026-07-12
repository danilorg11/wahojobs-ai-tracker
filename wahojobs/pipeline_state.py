from __future__ import annotations

import hashlib
import itertools
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone


WORKFLOW_STATUSES = {
    "recommended",
    "saved",
    "applied",
    "waiting",
    "assessment_invited",
    "assessment_started",
    "assessment_completed",
    "accepted",
    "active_worker",
    "paid_task_received",
    "rejected",
    "expired",
}
WORKFLOW_PROVENANCE = {"known", "inferred_legacy", "unknown_legacy"}
VISIBILITIES = {"visible", "hidden"}
WORKFLOW_TRANSITIONS = {
    "recommended": {"saved", "applied"},
    "saved": {"applied"},
    "applied": {"waiting", "assessment_invited", "assessment_started"},
    "waiting": {"assessment_invited", "assessment_started"},
    "assessment_invited": {"assessment_started"},
    "assessment_started": {"assessment_completed"},
    "assessment_completed": {"waiting", "accepted", "rejected"},
    "accepted": {"active_worker"},
    "active_worker": {"paid_task_received"},
    "paid_task_received": set(),
    "rejected": set(),
    "expired": set(),
}


class PipelineStateError(Exception):
    pass


class OwnershipError(PipelineStateError):
    pass


class ProjectionNotInitialized(PipelineStateError):
    pass


class ProjectionAlreadyInitialized(PipelineStateError):
    pass


class InvalidTransition(PipelineStateError):
    pass


class StaleStateVersion(PipelineStateError):
    pass


class InvalidExpectedVersion(StaleStateVersion):
    pass


class IdempotencyConflict(PipelineStateError):
    pass


@dataclass(frozen=True)
class MutationResult:
    state: dict
    transition: dict
    replayed: bool = False


_SAVEPOINTS = itertools.count(1)


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def validate_state(state: dict) -> dict:
    normalized = {
        "workflow_status": state.get("workflow_status"),
        "workflow_status_provenance": state.get("workflow_status_provenance"),
        "visibility": state.get("visibility"),
        "reminder_at": state.get("reminder_at"),
    }
    workflow_status = normalized["workflow_status"]
    if workflow_status is not None and workflow_status not in WORKFLOW_STATUSES:
        raise InvalidTransition(f"Unknown workflow status: {workflow_status}")
    if normalized["workflow_status_provenance"] not in WORKFLOW_PROVENANCE:
        raise InvalidTransition(
            "Unknown workflow provenance: "
            f"{normalized['workflow_status_provenance']}"
        )
    if normalized["visibility"] not in VISIBILITIES:
        raise InvalidTransition(f"Unknown visibility: {normalized['visibility']}")
    normalized["reminder_at"] = normalize_reminder(normalized["reminder_at"])
    return normalized


def normalize_reminder(value):
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidTransition(f"Invalid reminder timestamp: {text}") from exc
    return text


def projection_state(row) -> dict:
    return validate_state(
        {
            "workflow_status": row["workflow_status"],
            "workflow_status_provenance": row["workflow_status_provenance"],
            "visibility": row["visibility"],
            "reminder_at": row["reminder_at"],
        }
    )


def public_state(pipeline_item_id: str, state: dict, version: int) -> dict:
    return {
        "pipeline_item_id": pipeline_item_id,
        **validate_state(state),
        "version": version,
    }


def transition_dict(row) -> dict:
    return {
        "transition_id": row["transition_id"],
        "pipeline_item_id": row["pipeline_item_id"],
        "profile_id": row["profile_id"],
        "affected_dimension": row["affected_dimension"],
        "action_name": row["action_name"],
        "before_state": json.loads(row["before_state_json"]),
        "after_state": json.loads(row["after_state_json"]),
        "occurred_at": row["occurred_at"],
        "actor_source": row["actor_source"],
        "idempotency_key": row["idempotency_key"],
        "request_fingerprint": row["request_fingerprint"],
        "state_version_before": row["state_version_before"],
        "state_version_after": row["state_version_after"],
        "undo_of_transition_id": row["undo_of_transition_id"],
        "correction_of_transition_id": row["correction_of_transition_id"],
        "metadata": json.loads(row["metadata_json"]),
    }


@contextmanager
def atomic(conn):
    if conn.in_transaction:
        savepoint = f"pipeline_state_{next(_SAVEPOINTS)}"
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            yield
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def require_owned_pipeline_item(conn, pipeline_item_id: str, owner_profile_id: str):
    row = conn.execute(
        """
        SELECT pipeline_item_id, profile_id
        FROM user_pipeline_items
        WHERE pipeline_item_id = ?
        """,
        (pipeline_item_id,),
    ).fetchone()
    if row is None:
        raise OwnershipError(f"Unknown pipeline item: {pipeline_item_id}")
    if row["profile_id"] != owner_profile_id:
        raise OwnershipError("Pipeline item belongs to a different profile.")
    return row


def get_current_state(conn, pipeline_item_id: str, owner_profile_id: str) -> dict:
    require_owned_pipeline_item(conn, pipeline_item_id, owner_profile_id)
    row = conn.execute(
        "SELECT * FROM user_pipeline_state WHERE pipeline_item_id = ?",
        (pipeline_item_id,),
    ).fetchone()
    if row is None:
        raise ProjectionNotInitialized(
            f"Pipeline state is not initialized: {pipeline_item_id}"
        )
    return public_state(pipeline_item_id, projection_state(row), row["version"])


def initialize_projection(
    conn,
    *,
    pipeline_item_id: str,
    owner_profile_id: str,
    workflow_status,
    workflow_status_provenance: str,
    visibility: str,
    reminder_at,
    idempotency_key: str,
    action_name: str = "initialize_projection",
    actor_source: str = "system",
    metadata: dict | None = None,
    occurred_at: str | None = None,
    affected_dimension: str = "baseline",
) -> MutationResult:
    if affected_dimension not in {"baseline", "workflow"}:
        raise InvalidTransition(
            f"Unsupported projection initialization dimension: {affected_dimension}"
        )
    target = validate_state(
        {
            "workflow_status": workflow_status,
            "workflow_status_provenance": workflow_status_provenance,
            "visibility": visibility,
            "reminder_at": reminder_at,
        }
    )
    request = {
        "operation": "initialize",
        "pipeline_item_id": pipeline_item_id,
        "owner_profile_id": owner_profile_id,
        "target": target,
        "action_name": action_name,
        "actor_source": actor_source,
        "metadata": metadata or {},
        "affected_dimension": affected_dimension,
    }
    fingerprint = request_fingerprint(request)
    metadata = metadata or {}
    with atomic(conn):
        replay = replay_result(
            conn,
            pipeline_item_id,
            owner_profile_id,
            idempotency_key,
            fingerprint,
        )
        if replay:
            return replay
        require_owned_pipeline_item(conn, pipeline_item_id, owner_profile_id)
        try:
            with atomic(conn):
                existing = conn.execute(
                    "SELECT 1 FROM user_pipeline_state WHERE pipeline_item_id = ?",
                    (pipeline_item_id,),
                ).fetchone()
                if existing:
                    raise ProjectionAlreadyInitialized(
                        f"Pipeline state is already initialized: {pipeline_item_id}"
                    )
                conn.execute(
                    """
                    INSERT INTO user_pipeline_state (
                      pipeline_item_id,
                      workflow_status,
                      workflow_status_provenance,
                      visibility,
                      reminder_at,
                      version
                    )
                    VALUES (?, ?, ?, ?, ?, 1)
                    """,
                    (
                        pipeline_item_id,
                        target["workflow_status"],
                        target["workflow_status_provenance"],
                        target["visibility"],
                        target["reminder_at"],
                    ),
                )
                transition = insert_transition(
                    conn,
                    pipeline_item_id=pipeline_item_id,
                    profile_id=owner_profile_id,
                    affected_dimension=affected_dimension,
                    action_name=action_name,
                    before_state=None,
                    after_state=target,
                    occurred_at=occurred_at or now_utc(),
                    actor_source=actor_source,
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                    version_before=0,
                    version_after=1,
                    metadata=metadata,
                )
        except sqlite3.IntegrityError:
            replay = replay_result(
                conn,
                pipeline_item_id,
                owner_profile_id,
                idempotency_key,
                fingerprint,
            )
            if replay:
                return replay
            raise
    return MutationResult(
        state=public_state(pipeline_item_id, target, 1),
        transition=transition,
    )


def change_workflow_status(
    conn,
    *,
    pipeline_item_id: str,
    owner_profile_id: str,
    workflow_status: str,
    expected_version: int,
    idempotency_key: str,
    action_name: str | None = None,
    actor_source: str = "system",
    metadata: dict | None = None,
) -> MutationResult:
    if workflow_status not in WORKFLOW_STATUSES:
        raise InvalidTransition(f"Unknown workflow status: {workflow_status}")

    def build_target(_conn, current):
        current_status = current["workflow_status"]
        if current_status is None:
            raise InvalidTransition(
                "Unknown legacy workflow status requires an explicit correction."
            )
        if workflow_status not in WORKFLOW_TRANSITIONS[current_status]:
            raise InvalidTransition(
                f"Workflow transition is not allowed: {current_status} -> {workflow_status}"
            )
        return {
            **current,
            "workflow_status": workflow_status,
            "workflow_status_provenance": "known",
        }, None, None

    return apply_mutation(
        conn,
        pipeline_item_id=pipeline_item_id,
        owner_profile_id=owner_profile_id,
        affected_dimension="workflow",
        action_name=action_name or f"set_workflow_{workflow_status}",
        expected_version=expected_version,
        idempotency_key=idempotency_key,
        request_payload={"workflow_status": workflow_status},
        state_builder=build_target,
        actor_source=actor_source,
        metadata=metadata,
    )


def resolve_unknown_workflow(
    conn,
    *,
    pipeline_item_id: str,
    owner_profile_id: str,
    workflow_status: str,
    expected_version: int,
    idempotency_key: str,
    action_name: str | None = None,
    actor_source: str = "system",
    metadata: dict | None = None,
) -> MutationResult:
    """Resolve a migrated unknown workflow through an explicit user action."""
    if workflow_status not in WORKFLOW_STATUSES:
        raise InvalidTransition(f"Unknown workflow status: {workflow_status}")

    def build_target(_conn, current):
        if (
            current["workflow_status"] is not None
            or current["workflow_status_provenance"] != "unknown_legacy"
        ):
            raise InvalidTransition(
                "Only an unknown migrated workflow can be explicitly resolved."
            )
        return {
            **current,
            "workflow_status": workflow_status,
            "workflow_status_provenance": "known",
        }, None, None

    return apply_mutation(
        conn,
        pipeline_item_id=pipeline_item_id,
        owner_profile_id=owner_profile_id,
        affected_dimension="workflow",
        action_name=action_name or f"resolve_workflow_{workflow_status}",
        expected_version=expected_version,
        idempotency_key=idempotency_key,
        request_payload={
            "workflow_status": workflow_status,
            "resolution": "post_migration_user_action",
        },
        state_builder=build_target,
        actor_source=actor_source,
        metadata=metadata,
    )


def set_visibility(
    conn,
    *,
    pipeline_item_id: str,
    owner_profile_id: str,
    visibility: str,
    expected_version: int,
    idempotency_key: str,
    action_name: str | None = None,
    actor_source: str = "system",
    metadata: dict | None = None,
) -> MutationResult:
    if visibility not in VISIBILITIES:
        raise InvalidTransition(f"Unknown visibility: {visibility}")

    def build_target(_conn, current):
        return {**current, "visibility": visibility}, None, None

    return apply_mutation(
        conn,
        pipeline_item_id=pipeline_item_id,
        owner_profile_id=owner_profile_id,
        affected_dimension="visibility",
        action_name=action_name or ("hide" if visibility == "hidden" else "show"),
        expected_version=expected_version,
        idempotency_key=idempotency_key,
        request_payload={"visibility": visibility},
        state_builder=build_target,
        actor_source=actor_source,
        metadata=metadata,
    )


def hide_item(conn, **kwargs) -> MutationResult:
    return set_visibility(conn, visibility="hidden", **kwargs)


def show_item(conn, **kwargs) -> MutationResult:
    return set_visibility(conn, visibility="visible", **kwargs)


def set_reminder(
    conn,
    *,
    pipeline_item_id: str,
    owner_profile_id: str,
    reminder_at,
    expected_version: int,
    idempotency_key: str,
    action_name: str = "set_reminder",
    actor_source: str = "system",
    metadata: dict | None = None,
) -> MutationResult:
    reminder_at = normalize_reminder(reminder_at)
    if reminder_at is None:
        raise InvalidTransition("Use clear_reminder to remove a reminder.")

    def build_target(_conn, current):
        return {**current, "reminder_at": reminder_at}, None, None

    return apply_mutation(
        conn,
        pipeline_item_id=pipeline_item_id,
        owner_profile_id=owner_profile_id,
        affected_dimension="reminder",
        action_name=action_name,
        expected_version=expected_version,
        idempotency_key=idempotency_key,
        request_payload={"reminder_at": reminder_at},
        state_builder=build_target,
        actor_source=actor_source,
        metadata=metadata,
    )


def clear_reminder(
    conn,
    *,
    pipeline_item_id: str,
    owner_profile_id: str,
    expected_version: int,
    idempotency_key: str,
    action_name: str = "clear_reminder",
    actor_source: str = "system",
    metadata: dict | None = None,
) -> MutationResult:
    def build_target(_conn, current):
        return {**current, "reminder_at": None}, None, None

    return apply_mutation(
        conn,
        pipeline_item_id=pipeline_item_id,
        owner_profile_id=owner_profile_id,
        affected_dimension="reminder",
        action_name=action_name,
        expected_version=expected_version,
        idempotency_key=idempotency_key,
        request_payload={"reminder_at": None},
        state_builder=build_target,
        actor_source=actor_source,
        metadata=metadata,
    )


def undo_transition(
    conn,
    *,
    pipeline_item_id: str,
    owner_profile_id: str,
    transition_id: str,
    expected_version: int,
    idempotency_key: str,
    actor_source: str = "system",
    metadata: dict | None = None,
) -> MutationResult:
    def build_target(inner_conn, current):
        original = inner_conn.execute(
            "SELECT * FROM user_pipeline_transitions WHERE transition_id = ?",
            (transition_id,),
        ).fetchone()
        if (
            original is None
            or original["pipeline_item_id"] != pipeline_item_id
            or original["profile_id"] != owner_profile_id
        ):
            raise InvalidTransition("Unknown transition for this pipeline item.")
        if original["affected_dimension"] in {"baseline", "undo"}:
            raise InvalidTransition("That transition cannot be undone.")
        if protected_transition_class(original) is not None:
            raise InvalidTransition("That transition cannot be undone.")
        require_transition_has_no_child(inner_conn, original["transition_id"])
        if original["state_version_after"] != expected_version:
            raise InvalidTransition(
                "Only the latest compatible transition can be undone."
            )
        if canonical_json(current) != original["after_state_json"]:
            raise InvalidTransition(
                "Current state no longer matches the transition being undone."
            )
        before = json.loads(original["before_state_json"])
        if before is None:
            raise InvalidTransition("Transition has no restorable prior state.")
        return validate_state(before), transition_id, None

    return apply_mutation(
        conn,
        pipeline_item_id=pipeline_item_id,
        owner_profile_id=owner_profile_id,
        affected_dimension="undo",
        action_name="undo",
        expected_version=expected_version,
        idempotency_key=idempotency_key,
        request_payload={"undo_of_transition_id": transition_id},
        state_builder=build_target,
        actor_source=actor_source,
        metadata=metadata,
    )


def correct_state(
    conn,
    *,
    pipeline_item_id: str,
    owner_profile_id: str,
    corrected_state: dict,
    correction_of_transition_id: str,
    expected_version: int,
    idempotency_key: str,
    actor_source: str = "system",
    metadata: dict | None = None,
) -> MutationResult:
    target = validate_state(corrected_state)

    def build_target(inner_conn, _current):
        original = inner_conn.execute(
            "SELECT * FROM user_pipeline_transitions WHERE transition_id = ?",
            (correction_of_transition_id,),
        ).fetchone()
        if (
            original is None
            or original["pipeline_item_id"] != pipeline_item_id
            or original["profile_id"] != owner_profile_id
        ):
            raise InvalidTransition("Unknown transition for this correction.")
        if original["affected_dimension"] not in {"workflow", "correction"}:
            raise InvalidTransition(
                "Only a workflow transition or its terminal correction may be corrected."
            )
        if protected_transition_class(original) is not None:
            raise InvalidTransition("That transition cannot be corrected.")
        require_transition_has_no_child(inner_conn, original["transition_id"])
        later_funnel_row = inner_conn.execute(
            """
            SELECT transition_id
            FROM user_pipeline_transitions
            WHERE pipeline_item_id = ?
              AND state_version_after > ?
              AND affected_dimension IN ('workflow', 'undo', 'correction')
            ORDER BY state_version_after, transition_id
            LIMIT 1
            """,
            (pipeline_item_id, original["state_version_after"]),
        ).fetchone()
        if later_funnel_row is not None:
            raise InvalidTransition(
                "Only the terminal effective workflow transition may be corrected."
            )
        return target, None, correction_of_transition_id

    return apply_mutation(
        conn,
        pipeline_item_id=pipeline_item_id,
        owner_profile_id=owner_profile_id,
        affected_dimension="correction",
        action_name="correction",
        expected_version=expected_version,
        idempotency_key=idempotency_key,
        request_payload={
            "correction_of_transition_id": correction_of_transition_id,
            "corrected_state": target,
        },
        state_builder=build_target,
        actor_source=actor_source,
        metadata=metadata,
    )


def apply_mutation(
    conn,
    *,
    pipeline_item_id: str,
    owner_profile_id: str,
    affected_dimension: str,
    action_name: str,
    expected_version: int,
    idempotency_key: str,
    request_payload: dict,
    state_builder,
    actor_source: str,
    metadata: dict | None,
) -> MutationResult:
    require_expected_version(expected_version)
    request = {
        "operation": affected_dimension,
        "pipeline_item_id": pipeline_item_id,
        "owner_profile_id": owner_profile_id,
        "action_name": action_name,
        "expected_version": expected_version,
        "payload": request_payload,
        "actor_source": actor_source,
        "metadata": metadata or {},
    }
    fingerprint = request_fingerprint(request)
    with atomic(conn):
        replay = replay_result(
            conn,
            pipeline_item_id,
            owner_profile_id,
            idempotency_key,
            fingerprint,
        )
        if replay:
            return replay
        require_owned_pipeline_item(conn, pipeline_item_id, owner_profile_id)
        row = conn.execute(
            "SELECT * FROM user_pipeline_state WHERE pipeline_item_id = ?",
            (pipeline_item_id,),
        ).fetchone()
        if row is None:
            raise ProjectionNotInitialized(
                f"Pipeline state is not initialized: {pipeline_item_id}"
            )
        if row["version"] != expected_version:
            raise StaleStateVersion(
                f"Expected version {expected_version}, found {row['version']}."
            )
        before = projection_state(row)
        after, undo_of, correction_of = state_builder(conn, before)
        after = validate_state(after)
        if canonical_json(before) == canonical_json(after):
            raise InvalidTransition("Requested mutation does not change state.")
        try:
            transition = insert_transition(
                conn,
                pipeline_item_id=pipeline_item_id,
                profile_id=owner_profile_id,
                affected_dimension=affected_dimension,
                action_name=action_name,
                before_state=before,
                after_state=after,
                occurred_at=now_utc(),
                actor_source=actor_source,
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
                version_before=expected_version,
                version_after=expected_version + 1,
                undo_of_transition_id=undo_of,
                correction_of_transition_id=correction_of,
                metadata=metadata or {},
            )
        except sqlite3.IntegrityError:
            replay = replay_result(
                conn,
                pipeline_item_id,
                owner_profile_id,
                idempotency_key,
                fingerprint,
            )
            if replay:
                return replay
            raise
        cursor = conn.execute(
            """
            UPDATE user_pipeline_state
            SET workflow_status = ?,
                workflow_status_provenance = ?,
                visibility = ?,
                reminder_at = ?,
                version = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE pipeline_item_id = ? AND version = ?
            """,
            (
                after["workflow_status"],
                after["workflow_status_provenance"],
                after["visibility"],
                after["reminder_at"],
                expected_version + 1,
                pipeline_item_id,
                expected_version,
            ),
        )
        if cursor.rowcount != 1:
            raise StaleStateVersion("Projection changed during the mutation.")
    return MutationResult(
        state=public_state(pipeline_item_id, after, expected_version + 1),
        transition=transition,
    )


def record_operation_noop(
    conn,
    *,
    pipeline_item_id: str,
    owner_profile_id: str,
    expected_version: int,
    idempotency_key: str,
    action_name: str,
    actor_source: str = "system",
    metadata: dict | None = None,
) -> MutationResult:
    """Record an accepted product operation that intentionally changes no dimension."""
    require_expected_version(expected_version)
    metadata = dict(metadata or {})
    if metadata.get("transition_class") != "operation_noop":
        raise InvalidTransition("Operation no-op metadata is missing its transition class.")
    request = {
        "operation": "operation_noop",
        "pipeline_item_id": pipeline_item_id,
        "owner_profile_id": owner_profile_id,
        "action_name": action_name,
        "expected_version": expected_version,
        "actor_source": actor_source,
        "metadata": metadata,
    }
    fingerprint = request_fingerprint(request)
    with atomic(conn):
        replay = replay_result(
            conn,
            pipeline_item_id,
            owner_profile_id,
            idempotency_key,
            fingerprint,
        )
        if replay:
            return replay
        require_owned_pipeline_item(conn, pipeline_item_id, owner_profile_id)
        row = conn.execute(
            "SELECT * FROM user_pipeline_state WHERE pipeline_item_id = ?",
            (pipeline_item_id,),
        ).fetchone()
        if row is None:
            raise ProjectionNotInitialized(
                f"Pipeline state is not initialized: {pipeline_item_id}"
            )
        if row["version"] != expected_version:
            raise StaleStateVersion(
                f"Expected version {expected_version}, found {row['version']}."
            )
        state = projection_state(row)
        transition = insert_transition(
            conn,
            pipeline_item_id=pipeline_item_id,
            profile_id=owner_profile_id,
            affected_dimension="workflow",
            action_name=action_name,
            before_state=state,
            after_state=state,
            occurred_at=now_utc(),
            actor_source=actor_source,
            idempotency_key=idempotency_key,
            fingerprint=fingerprint,
            version_before=expected_version,
            version_after=expected_version + 1,
            metadata=metadata,
        )
        cursor = conn.execute(
            """
            UPDATE user_pipeline_state
            SET version = ?, updated_at = CURRENT_TIMESTAMP
            WHERE pipeline_item_id = ? AND version = ?
            """,
            (expected_version + 1, pipeline_item_id, expected_version),
        )
        if cursor.rowcount != 1:
            raise StaleStateVersion("Projection changed during the mutation.")
    return MutationResult(
        state=public_state(pipeline_item_id, state, expected_version + 1),
        transition=transition,
    )


def insert_transition(
    conn,
    *,
    pipeline_item_id,
    profile_id,
    affected_dimension,
    action_name,
    before_state,
    after_state,
    occurred_at,
    actor_source,
    idempotency_key,
    fingerprint,
    version_before,
    version_after,
    undo_of_transition_id=None,
    correction_of_transition_id=None,
    metadata=None,
):
    transition_id = stable_transition_id(pipeline_item_id, idempotency_key)
    conn.execute(
        """
        INSERT INTO user_pipeline_transitions (
          transition_id,
          pipeline_item_id,
          profile_id,
          affected_dimension,
          action_name,
          before_state_json,
          after_state_json,
          occurred_at,
          actor_source,
          idempotency_key,
          request_fingerprint,
          state_version_before,
          state_version_after,
          undo_of_transition_id,
          correction_of_transition_id,
          metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            transition_id,
            pipeline_item_id,
            profile_id,
            affected_dimension,
            action_name,
            canonical_json(before_state),
            canonical_json(after_state),
            occurred_at,
            actor_source,
            idempotency_key,
            fingerprint,
            version_before,
            version_after,
            undo_of_transition_id,
            correction_of_transition_id,
            canonical_json(metadata or {}),
        ),
    )
    row = conn.execute(
        "SELECT * FROM user_pipeline_transitions WHERE transition_id = ?",
        (transition_id,),
    ).fetchone()
    return transition_dict(row)


def replay_result(
    conn,
    pipeline_item_id,
    owner_profile_id,
    idempotency_key,
    fingerprint,
):
    if not str(idempotency_key or "").strip():
        raise InvalidTransition("An idempotency key is required.")
    row = conn.execute(
        "SELECT * FROM user_pipeline_transitions WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()
    if row is None:
        return None
    if row["request_fingerprint"] != fingerprint:
        raise IdempotencyConflict(
            "Idempotency key was already used for a different mutation."
        )
    require_owned_pipeline_item(conn, pipeline_item_id, owner_profile_id)
    if row["pipeline_item_id"] != pipeline_item_id or row["profile_id"] != owner_profile_id:
        raise IdempotencyConflict(
            "Idempotency key was already used for a different mutation."
        )
    transition = transition_dict(row)
    return MutationResult(
        state=public_state(
            pipeline_item_id,
            transition["after_state"],
            transition["state_version_after"],
        ),
        transition=transition,
        replayed=True,
    )


def require_expected_version(version):
    if type(version) is not int or version < 1:
        raise InvalidExpectedVersion(
            "A built-in positive integer expected state version is required."
        )


def request_fingerprint(payload: dict) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def require_transition_has_no_child(conn, transition_id: str):
    child = conn.execute(
        """
        SELECT transition_id
        FROM user_pipeline_transitions
        WHERE undo_of_transition_id = ? OR correction_of_transition_id = ?
        ORDER BY state_version_after, transition_id
        LIMIT 1
        """,
        (transition_id, transition_id),
    ).fetchone()
    if child is not None:
        raise InvalidTransition("Transition has already been superseded.")


def stable_transition_id(pipeline_item_id: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(
        f"{pipeline_item_id}\x1f{idempotency_key}".encode("utf-8")
    ).hexdigest()[:24]
    return f"pipeline-transition::{digest}"


def protected_transition_class(row) -> str | None:
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return None
    transition_class = metadata.get("transition_class")
    if transition_class in {"user_initialization", "operation_noop"}:
        return transition_class
    return None


def list_transition_history(conn, pipeline_item_id: str, owner_profile_id: str) -> list[dict]:
    require_owned_pipeline_item(conn, pipeline_item_id, owner_profile_id)
    rows = conn.execute(
        """
        SELECT *
        FROM user_pipeline_transitions
        WHERE pipeline_item_id = ?
        ORDER BY state_version_after, transition_id
        """,
        (pipeline_item_id,),
    ).fetchall()
    return [transition_dict(row) for row in rows]


def list_effective_funnel_transitions(
    conn,
    pipeline_item_id: str,
    owner_profile_id: str,
) -> list[dict]:
    history = list_transition_history(conn, pipeline_item_id, owner_profile_id)
    by_id = {row["transition_id"]: row for row in history}
    children = {}
    for row in history:
        reference = row["undo_of_transition_id"] or row["correction_of_transition_id"]
        if reference is None:
            if row["affected_dimension"] in {"undo", "correction"}:
                raise InvalidTransition("Malformed compensating transition without a reference.")
            continue
        target = by_id.get(reference)
        if target is None:
            raise InvalidTransition("Compensating transition references missing history.")
        if target["pipeline_item_id"] != row["pipeline_item_id"] or target["profile_id"] != row["profile_id"]:
            raise InvalidTransition("Compensating transition crosses pipeline ownership.")
        if target["state_version_after"] >= row["state_version_after"]:
            raise InvalidTransition("Compensating transition does not follow its target.")
        if reference in children:
            raise InvalidTransition("Ambiguous transition history contains a branch.")
        if row["affected_dimension"] == "correction" and target["affected_dimension"] not in {
            "workflow",
            "correction",
        }:
            raise InvalidTransition("Correction chain does not originate from workflow history.")
        if row["affected_dimension"] == "undo" and target["affected_dimension"] in {
            "baseline",
            "undo",
        }:
            raise InvalidTransition("Undo references a non-undoable transition.")
        children[reference] = row

    effective = []
    for root in history:
        if root["affected_dimension"] != "workflow":
            continue
        current = root
        visited = set()
        while current["transition_id"] in children:
            if current["transition_id"] in visited:
                raise InvalidTransition("Cycle detected in transition history.")
            visited.add(current["transition_id"])
            child = children[current["transition_id"]]
            if child["affected_dimension"] == "undo":
                current = None
                break
            if child["affected_dimension"] != "correction":
                raise InvalidTransition("Unsupported transition-chain child.")
            current = child
        if current is not None and is_genuine_workflow_transition(current):
            effective.append(current)
    return sorted(
        effective,
        key=lambda row: (row["state_version_after"], row["transition_id"]),
    )


def is_genuine_workflow_transition(transition: dict) -> bool:
    if transition.get("metadata", {}).get("transition_class") in {
        "user_initialization",
        "operation_noop",
    }:
        return False
    before_status = transition["before_state"].get("workflow_status")
    after_status = transition["after_state"].get("workflow_status")
    return (
        before_status in WORKFLOW_TRANSITIONS
        and after_status in WORKFLOW_TRANSITIONS[before_status]
    )


def legacy_projection(row) -> tuple[dict, dict, str]:
    raw_status = str(row["status"] or "").strip()
    raw_reminder = str(row["reminder_date"] or "").strip()
    reminder = None
    reminder_valid = not raw_reminder
    if raw_reminder:
        try:
            reminder = normalize_reminder(raw_reminder)
            reminder_valid = True
        except InvalidTransition:
            reminder = None

    if raw_status in WORKFLOW_STATUSES:
        workflow_status = raw_status
        provenance = "inferred_legacy"
        visibility = "visible"
        classification = "known_workflow"
    elif raw_status == "not_interested":
        workflow_status = None
        provenance = "unknown_legacy"
        visibility = "hidden"
        classification = "hidden_unknown_workflow"
    elif raw_status == "remind_later":
        workflow_status = None
        provenance = "unknown_legacy"
        visibility = "visible"
        classification = "reminder_unknown_workflow"
    else:
        workflow_status = None
        provenance = "unknown_legacy"
        visibility = "visible"
        classification = "unknown_status"

    state = validate_state(
        {
            "workflow_status": workflow_status,
            "workflow_status_provenance": provenance,
            "visibility": visibility,
            "reminder_at": reminder,
        }
    )
    metadata = {
        "legacy_snapshot": True,
        "raw_legacy_status": raw_status,
        "raw_legacy_reminder_date": raw_reminder,
        "legacy_reminder_valid": reminder_valid,
        "legacy_classification": classification,
    }
    return state, metadata, classification


def plan_legacy_backfill(conn) -> list[dict]:
    if table_exists(conn, "user_pipeline_state"):
        rows = conn.execute(
            """
            SELECT
              upi.pipeline_item_id,
              upi.profile_id,
              upi.status,
              upi.reminder_date,
              upi.created_at,
              upi.updated_at
            FROM user_pipeline_items upi
            LEFT JOIN user_pipeline_state ups
              ON ups.pipeline_item_id = upi.pipeline_item_id
            WHERE ups.pipeline_item_id IS NULL
            ORDER BY upi.pipeline_item_id
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT
              pipeline_item_id,
              profile_id,
              status,
              reminder_date,
              created_at,
              updated_at
            FROM user_pipeline_items
            ORDER BY pipeline_item_id
            """
        ).fetchall()
    plan = []
    for row in rows:
        state, metadata, classification = legacy_projection(row)
        plan.append(
            {
                "pipeline_item_id": row["pipeline_item_id"],
                "profile_id": row["profile_id"],
                "state": state,
                "metadata": metadata,
                "classification": classification,
                "idempotency_key": f"legacy-baseline:v1:{row['pipeline_item_id']}",
                "occurred_at": row["updated_at"] or row["created_at"] or now_utc(),
            }
        )
    return plan


def backfill_legacy_pipeline_state(
    conn,
    *,
    dry_run: bool = True,
    on_item_migrated=None,
) -> dict:
    plan = plan_legacy_backfill(conn)
    existing = (
        conn.execute("SELECT COUNT(*) FROM user_pipeline_state").fetchone()[0]
        if table_exists(conn, "user_pipeline_state")
        else 0
    )
    classifications = {}
    malformed_reminders = 0
    for item in plan:
        key = item["classification"]
        classifications[key] = classifications.get(key, 0) + 1
        if not item["metadata"]["legacy_reminder_valid"]:
            malformed_reminders += 1
    summary = {
        "dry_run": dry_run,
        "planned": len(plan),
        "migrated": 0,
        "already_initialized": existing,
        "classifications": classifications,
        "malformed_reminders": malformed_reminders,
    }
    if dry_run or not plan:
        return summary

    with atomic(conn):
        for index, item in enumerate(plan, start=1):
            state = item["state"]
            initialize_projection(
                conn,
                pipeline_item_id=item["pipeline_item_id"],
                owner_profile_id=item["profile_id"],
                workflow_status=state["workflow_status"],
                workflow_status_provenance=state["workflow_status_provenance"],
                visibility=state["visibility"],
                reminder_at=state["reminder_at"],
                idempotency_key=item["idempotency_key"],
                action_name="legacy_snapshot",
                actor_source="legacy_migration",
                metadata=item["metadata"],
                occurred_at=item["occurred_at"],
            )
            if on_item_migrated is not None:
                on_item_migrated(index, len(plan), item)
    summary["migrated"] = len(plan)
    return summary


def table_exists(conn, table_name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        is not None
    )

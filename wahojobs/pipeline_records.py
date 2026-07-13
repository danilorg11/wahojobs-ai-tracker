from __future__ import annotations

from dataclasses import dataclass

from wahojobs import pipeline_state
from wahojobs.pipeline_actions import legacy_compatibility_from_state


class PipelineRecordError(pipeline_state.PipelineStateError):
    pass


class PipelineRecordInvariant(PipelineRecordError):
    pass


@dataclass(frozen=True)
class PipelineRecord:
    pipeline_item: dict
    persisted_owner: dict
    opportunity: dict
    normalized_state: dict | None
    compatibility: dict
    display: dict
    diagnostics: dict

    def as_dict(self) -> dict:
        return {
            "pipeline_item": self.pipeline_item,
            "persisted_owner": self.persisted_owner,
            "opportunity": self.opportunity,
            "normalized_state": self.normalized_state,
            "compatibility": self.compatibility,
            "display": self.display,
            "diagnostics": self.diagnostics,
        }


def require_pipeline_state_schema(conn):
    required_tables = {
        "user_pipeline_items",
        "user_pipeline_state",
        "user_pipeline_transitions",
        "wahojobs_schema_migrations",
    }
    present_tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        if row["name"] in required_tables
    }
    marker = (
        conn.execute(
            "SELECT 1 FROM wahojobs_schema_migrations WHERE version = ?",
            ("001_pipeline_state",),
        ).fetchone()
        if "wahojobs_schema_migrations" in present_tables
        else None
    )
    if present_tables != required_tables or marker is None:
        raise PipelineRecordInvariant(
            "Pipeline-state migration is not completely installed."
        )


def list_pipeline_records(
    conn,
    owner_profile_id: str,
    *,
    mutation_grade: bool = False,
) -> list[PipelineRecord]:
    """Load all records for one persisted owner from normalized state."""
    require_pipeline_state_schema(conn)
    rows = conn.execute(
        """
        SELECT pipeline_item_id
        FROM user_pipeline_items
        WHERE profile_id = ?
        ORDER BY updated_at DESC, id DESC
        """,
        (owner_profile_id,),
    ).fetchall()
    return [
        load_pipeline_record(
            conn,
            row["pipeline_item_id"],
            owner_profile_id=owner_profile_id,
            mutation_grade=mutation_grade,
        )
        for row in rows
    ]


def load_pipeline_record(
    conn,
    pipeline_item_id: str,
    *,
    owner_profile_id: str | None = None,
    mutation_grade: bool = False,
) -> PipelineRecord:
    """Load normalized and compatibility state without installing or repairing it."""
    require_pipeline_state_schema(conn)
    item = conn.execute(
        "SELECT * FROM user_pipeline_items WHERE pipeline_item_id = ?",
        (pipeline_item_id,),
    ).fetchone()
    if item is None:
        raise pipeline_state.OwnershipError(f"Unknown pipeline item: {pipeline_item_id}")
    if owner_profile_id is not None and item["profile_id"] != owner_profile_id:
        raise pipeline_state.OwnershipError(
            "Pipeline item belongs to a different profile."
        )

    projection_rows = conn.execute(
        "SELECT * FROM user_pipeline_state WHERE pipeline_item_id = ?",
        (pipeline_item_id,),
    ).fetchall()
    invariants = []
    normalized_state = None
    if not projection_rows:
        invariants.append("missing_projection")
    elif len(projection_rows) > 1:
        invariants.append("duplicate_projection")
    else:
        projection = projection_rows[0]
        state = pipeline_state.projection_state(projection)
        normalized_state = pipeline_state.public_state(
            pipeline_item_id, state, projection["version"]
        )
        normalized_state["created_at"] = projection["created_at"]
        normalized_state["updated_at"] = projection["updated_at"]
        if state["workflow_status"] is None:
            invariants.append("unresolved_legacy_workflow")
            if state["visibility"] == "visible" and state["reminder_at"] is None:
                invariants.append("visible_unknown_without_reminder")

    transition_profiles = {
        row["profile_id"]
        for row in conn.execute(
            "SELECT DISTINCT profile_id FROM user_pipeline_transitions WHERE pipeline_item_id = ?",
            (pipeline_item_id,),
        )
    }
    if normalized_state is not None and not transition_profiles:
        invariants.append("missing_transition_history")
    if any(profile_id != item["profile_id"] for profile_id in transition_profiles):
        invariants.append("projection_owner_mismatch")

    blocking = {
        "missing_projection",
        "duplicate_projection",
        "projection_owner_mismatch",
        "missing_transition_history",
        "visible_unknown_without_reminder",
    }
    if mutation_grade and blocking.intersection(invariants):
        raise PipelineRecordInvariant(
            "Pipeline record is not mutation-grade consistent: "
            + ", ".join(sorted(blocking.intersection(invariants)))
        )

    mirror_expected = None
    mirror_matches = None
    if normalized_state is not None:
        try:
            mirror_expected = legacy_compatibility_from_state(normalized_state)
            mirror_matches = (
                mirror_expected["status"] == item["status"]
                and mirror_expected["reminder_date"] == (item["reminder_date"] or "")
            )
        except pipeline_state.PipelineStateError:
            mirror_matches = False

    return PipelineRecord(
        pipeline_item={
            "id": item["id"],
            "pipeline_item_id": item["pipeline_item_id"],
            "created_at": item["created_at"],
            "updated_at": item["updated_at"],
        },
        persisted_owner={
            "user_id": item["user_id"],
            "profile_id": item["profile_id"],
        },
        opportunity={
            "source": item["source"],
            "title": item["opportunity_title"],
            "url": item["opportunity_url"] or "",
            "external_id": item["opportunity_external_id"] or "",
            "canonical_id": item["canonical_id"],
        },
        normalized_state=normalized_state,
        compatibility={
            "status": item["status"],
            "status_date": item["status_date"],
            "reminder_date": item["reminder_date"] or "",
            "last_user_action": item["last_user_action"],
            "expected": mirror_expected,
            "matches_normalized": mirror_matches,
        },
        display={
            "notes": item["notes"],
            "user_priority": item["user_priority"],
            "is_sample": item["is_sample"],
        },
        diagnostics={
            "invariants": sorted(set(invariants)),
            "unresolved_workflow": "unresolved_legacy_workflow" in invariants,
            "mutation_grade": not bool(blocking.intersection(invariants)),
            "transition_owner_profiles": sorted(transition_profiles),
        },
    )

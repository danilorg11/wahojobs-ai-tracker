from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable

from wahojobs import pipeline_state
from wahojobs import pipeline_transition_metadata as transition_metadata


WORKFLOW_ACTIONS = dict(transition_metadata.WORKFLOW_ACTIONS)
APPLICANT_ACTIONS = set(transition_metadata.APPLICANT_EFFECTS)
SUPPORTED_ACTIONS = set(WORKFLOW_ACTIONS) | {
    "remind_later",
    "not_interested",
    "show_again",
    "show_again_as_saved",
}
DEFAULT_NOTES = {
    "save": "Saved from local UI",
    "applied": "Marked applied from local UI",
    "assessment_started": "Marked assessment started from local UI",
    "assessment_completed": "Marked assessment completed from local UI",
    "remind_later": "Reminder set from local UI",
    "not_interested": "Marked not interested from local UI",
    "show_again": "Shown again from local UI",
    "show_again_as_saved": "Shown again as saved from local UI",
    "accepted": "Marked accepted from local UI",
    "rejected": "Marked rejected from local UI",
}
ORCHESTRATION_METADATA_VERSION = transition_metadata.ORCHESTRATION_METADATA_VERSION
RESULT_SNAPSHOT_VERSION = transition_metadata.RESULT_SNAPSHOT_VERSION
INTERNAL_IDEMPOTENCY_PREFIX = transition_metadata.INTERNAL_IDEMPOTENCY_PREFIX
EXPECTED_VERSION_ABSENT = object()


class PipelineActionError(pipeline_state.PipelineStateError):
    pass


class PipelineActionValidationError(PipelineActionError):
    pass


class UnresolvedLegacyWorkflow(PipelineActionError):
    pass


class PipelineInvariantError(PipelineActionError):
    pass


@dataclass(frozen=True)
class PipelineActionResult:
    pipeline_item: dict
    state: dict
    transition: dict
    compatibility_state: dict
    applicant_update: dict | None
    created: bool
    replayed: bool = False


def legacy_compatibility_from_state(state: dict) -> dict:
    """Return the deterministic old-reader representation of normalized state."""
    try:
        return transition_metadata.deterministic_compatibility_from_state(state)
    except transition_metadata.TransitionMetadataError as exc:
        raise UnresolvedLegacyWorkflow(
            "Visible unknown workflow without a reminder cannot be mirrored."
        ) from exc


def stable_pipeline_item_id(*, profile_id: str, source: str, title: str, url: str) -> str:
    values = [profile_id, source, title, url or ""]
    digest = hashlib.sha1("|".join(values).encode("utf-8")).hexdigest()[:16]
    return f"pipeline::{digest}"


def stable_applicant_update_id(
    *,
    profile_id: str,
    source: str,
    title: str,
    status: str,
    status_date: str,
    note: str,
) -> str:
    return transition_metadata.stable_applicant_update_id(
        profile_id=profile_id,
        source=source,
        title=title,
        status=status,
        status_date=status_date,
        note=note,
    )


def merge_note(existing, note) -> str:
    existing = str(existing or "").strip()
    note = str(note or "").strip()
    if not note:
        return existing
    if not existing:
        return note
    if note in existing:
        return existing
    return f"{existing}\n{note}"


def perform_pipeline_action(
    conn,
    *,
    action: str,
    owner_profile_id: str,
    idempotency_key: str,
    match_run_id: str,
    expected_version=EXPECTED_VERSION_ABSENT,
    pipeline_item_id: str | None = None,
    source: str | None = None,
    title: str | None = None,
    url: str = "",
    opportunity_external_id: str = "",
    canonical_id: int | None = None,
    reminder_at: str | None = None,
    note: str | None = None,
    actor_source: str = "product_action",
    is_sample: int | None = None,
    failure_injector: Callable[[str], None] | None = None,
) -> PipelineActionResult:
    """Apply one complete product action under a single atomic boundary.

    The caller's key is reserved for the terminal transition. Preparatory
    transitions use derived keys and are committed only with that terminal
    transition, which acts as the complete-operation replay marker.
    """
    item_identity_supplied = pipeline_item_id is not None
    expected_version_was_supplied = expected_version is not EXPECTED_VERSION_ABSENT
    action = str(action or "").strip()
    if action not in SUPPORTED_ACTIONS:
        raise PipelineActionValidationError(f"Unsupported pipeline action: {action}")
    owner_profile_id = _required_text(owner_profile_id, "owner_profile_id")
    match_run_id = _required_text(match_run_id, "match_run_id")
    idempotency_key = _required_text(idempotency_key, "idempotency_key")
    if len(idempotency_key) < 16:
        raise PipelineActionValidationError(
            "Product action idempotency keys must be unpredictable and at least 16 characters."
        )
    if idempotency_key.startswith(INTERNAL_IDEMPOTENCY_PREFIX):
        raise PipelineActionValidationError(
            "Caller idempotency keys cannot use the reserved internal namespace."
        )
    expected_version = _validate_product_expected_version(
        expected_version,
        allow_absent_creation=not item_identity_supplied,
    )
    is_sample = _normalize_optional_is_sample(is_sample)
    note = str(note if note is not None else DEFAULT_NOTES[action]).strip()
    reminder_at = (
        pipeline_state.normalize_reminder(reminder_at)
        if reminder_at not in (None, "")
        else None
    )
    if action == "remind_later" and reminder_at is None:
        raise PipelineActionValidationError("Remind later requires reminder_at.")

    opportunity = _normalize_opportunity(
        source=source,
        title=title,
        url=url,
        opportunity_external_id=opportunity_external_id,
        canonical_id=canonical_id,
    )
    if pipeline_item_id is None:
        if opportunity is None:
            raise PipelineActionValidationError(
                "An existing pipeline_item_id or complete opportunity identity is required."
            )
        pipeline_item_id = stable_pipeline_item_id(
            profile_id=owner_profile_id,
            source=opportunity["source"],
            title=opportunity["title"],
            url=opportunity["url"],
        )
    pipeline_item_id = _required_text(pipeline_item_id, "pipeline_item_id")

    request = {
        "schema_version": ORCHESTRATION_METADATA_VERSION,
        "action": action,
        "owner_profile_id": owner_profile_id,
        "pipeline_item_id": pipeline_item_id,
        "match_run_id": match_run_id,
        "expected_version": expected_version,
        "expected_version_was_supplied": expected_version_was_supplied,
        "identity_mode": "pipeline_item" if item_identity_supplied else "opportunity",
        "opportunity": opportunity,
        "reminder_at": reminder_at,
        "requested_effect": _requested_effect(action, reminder_at),
        "note": note,
        "actor_source": actor_source,
        "is_sample": is_sample,
        "applicant_payload": _applicant_payload(action, note),
    }
    operation_fingerprint = pipeline_state.request_fingerprint(request)

    with pipeline_state.atomic(conn):
        replay = _replay_complete_operation(
            conn,
            pipeline_item_id=pipeline_item_id,
            owner_profile_id=owner_profile_id,
            idempotency_key=idempotency_key,
            operation_fingerprint=operation_fingerprint,
        )
        if replay is not None:
            return replay

        profile = _require_profile(conn, owner_profile_id)
        item = _load_item(conn, pipeline_item_id)
        created = item is None
        if created:
            if action not in transition_metadata.CREATION_ACTION_CONTRACTS:
                raise pipeline_state.InvalidTransition(
                    f"A new item cannot start with action {action}. Save, apply, or hide it first."
                )
            if expected_version != 0:
                raise pipeline_state.StaleStateVersion(
                    "A new pipeline item requires expected_version=0."
                )
            if opportunity is None:
                raise pipeline_state.OwnershipError(
                    f"Unknown pipeline item: {pipeline_item_id}"
                )
            item = _insert_pipeline_item(
                conn,
                pipeline_item_id=pipeline_item_id,
                profile=profile,
                opportunity=opportunity,
                occurred_at=pipeline_state.now_utc(),
            )
            _inject(failure_injector, "after_pipeline_item_insert")
        else:
            _require_item_owner(item, owner_profile_id)
            if opportunity is not None and not _same_opportunity(item, opportunity):
                raise pipeline_state.IdempotencyConflict(
                    "Idempotency key was already used for a different mutation."
                )
            if expected_version == 0:
                raise pipeline_state.StaleStateVersion(
                    "An existing pipeline item requires its positive current version."
                )

        current = None
        preparatory_count = 0
        preparatory_transition_ids = []
        if created:
            initial_key = _derived_idempotency_key(
                caller_key=idempotency_key,
                operation_fingerprint=operation_fingerprint,
                step="initialize",
                pipeline_item_id=pipeline_item_id,
            )
            initial_metadata = {
                "schema_version": ORCHESTRATION_METADATA_VERSION,
                "metadata_schema": transition_metadata.INITIALIZATION_SCHEMA,
                "transition_class": "user_initialization",
                "initialization_kind": "user_created",
                "creation_origin": "product_action",
                "first_requested_action": action,
                "legacy_snapshot": False,
                "product_action_fingerprint": operation_fingerprint,
                "pipeline_item_id": pipeline_item_id,
                "owner_profile_id": owner_profile_id,
                "match_run_id": match_run_id,
                "internal_step": "initialize",
            }
            initialized = pipeline_state.initialize_projection(
                conn,
                pipeline_item_id=pipeline_item_id,
                owner_profile_id=owner_profile_id,
                workflow_status="saved",
                workflow_status_provenance="known",
                visibility="visible",
                reminder_at=None,
                idempotency_key=initial_key,
                action_name="user_created",
                actor_source=actor_source,
                metadata=initial_metadata,
                affected_dimension="workflow",
            )
            current = initialized.state
            preparatory_count = 1
            preparatory_transition_ids.append(initialized.transition["transition_id"])
            _inject(failure_injector, "after_projection_initialization")
            _inject(failure_injector, "after_first_normalized_transition")
        else:
            current = pipeline_state.get_current_state(
                conn, pipeline_item_id, owner_profile_id
            )
            if current["version"] != expected_version:
                raise pipeline_state.StaleStateVersion(
                    f"Expected version {expected_version}, found {current['version']}."
                )
            _validate_current_invariants(current)

        target, terminal_kind, resolution_required = _plan_target(
            action=action,
            current=current,
            created=created,
            reminder_at=reminder_at,
            legacy_status=item["status"],
        )
        if action == "show_again_as_saved":
            resolution = pipeline_state.resolve_unknown_workflow(
                conn,
                pipeline_item_id=pipeline_item_id,
                owner_profile_id=owner_profile_id,
                workflow_status="saved",
                expected_version=current["version"],
                idempotency_key=_derived_idempotency_key(
                    caller_key=idempotency_key,
                    operation_fingerprint=operation_fingerprint,
                    step="resolve_unknown_workflow",
                    pipeline_item_id=pipeline_item_id,
                ),
                action_name="resolve_unknown_workflow_as_saved",
                actor_source=actor_source,
                metadata={
                    "resolution": "post_migration_user_action",
                    "requested_action": "show_again_as_saved",
                },
            )
            current = resolution.state
            preparatory_count += 1
            preparatory_transition_ids.append(resolution.transition["transition_id"])
            _inject(failure_injector, "after_first_normalized_transition")

        action_time = pipeline_state.now_utc()
        status_date = action_time[:10]
        prior_legacy_status = item["status"]
        operation_noop = terminal_kind == "noop"
        if operation_noop:
            compatibility_after = _compatibility_snapshot(item)
            notes_after = item["notes"]
            sample_after = item["is_sample"]
        else:
            notes_after = merge_note(item["notes"], note)
            sample_after = item["is_sample"] if is_sample is None else is_sample
            mirror = legacy_compatibility_from_state(target)
            compatibility_after = {
                "status": mirror["status"],
                "status_date": (
                    item["status_date"]
                    if action == "remind_later" and not created
                    else status_date
                ),
                "reminder_date": mirror["reminder_date"],
                "notes": notes_after,
                "last_user_action": note,
                "is_sample": sample_after,
                "updated_at": action_time,
            }
        applicant_update_id = None
        applicant_effect = transition_metadata.APPLICANT_EFFECTS.get(action)
        if applicant_effect is not None and not operation_noop:
            applicant_update_id = stable_applicant_update_id(
                profile_id=owner_profile_id,
                source=item["source"],
                title=item["opportunity_title"],
                status=applicant_effect["status"],
                status_date=status_date,
                note=note,
            )

        applicant = None
        applicant_write_outcome = None
        if applicant_update_id is not None:
            applicant, applicant_write_outcome = _upsert_applicant_update(
                conn,
                update_id=applicant_update_id,
                profile=profile,
                item=item,
                status=applicant_effect["status"],
                previous_status=prior_legacy_status,
                status_date=status_date,
                reported_at=action_time,
                evidence_type=applicant_effect["evidence_type"],
                confidence_level=applicant_effect["confidence_level"],
                note=note,
            )
            _inject(failure_injector, "after_applicant_update")

        if not operation_noop:
            _update_legacy_status(
                conn,
                item_id=item["id"],
                status=compatibility_after["status"],
                status_date=compatibility_after["status_date"],
            )
            _inject(failure_injector, "after_legacy_status_mirror")
            _update_legacy_reminder(
                conn,
                item_id=item["id"],
                reminder_date=compatibility_after["reminder_date"],
            )
            _inject(failure_injector, "after_reminder_mirror")
            _update_legacy_action_fields(
                conn,
                item_id=item["id"],
                notes=notes_after,
                last_user_action=note,
                is_sample=sample_after,
                updated_at=action_time,
            )
            _inject(failure_injector, "after_notes_timestamp_update")

        item_after_mirror = _load_item(conn, pipeline_item_id)
        final_version = current["version"] + 1
        terminal_transition_id = pipeline_state.stable_transition_id(
            pipeline_item_id, idempotency_key
        )
        result_snapshot = {
            "schema_version": RESULT_SNAPSHOT_VERSION,
            "pipeline_item": _public_item_identity(item_after_mirror),
            "normalized_state": {
                "pipeline_item_id": pipeline_item_id,
                **pipeline_state.validate_state(target),
                "version": final_version,
            },
            "compatibility_state": dict(compatibility_after),
            "applicant_update": dict(applicant) if applicant is not None else None,
            "created": created,
            "terminal_transition_id": terminal_transition_id,
            "preparatory_transition_ids": list(preparatory_transition_ids),
        }
        applicant_receipt = (
            transition_metadata.build_applicant_receipt(
                action=action,
                pipeline_item_id=pipeline_item_id,
                applicant=applicant,
                write_outcome=applicant_write_outcome,
            )
            if applicant is not None
            else None
        )
        operation_metadata = {
            "pipeline_action": {
                "schema_version": ORCHESTRATION_METADATA_VERSION,
                "metadata_schema": (
                    transition_metadata.NOOP_SCHEMA
                    if operation_noop
                    else transition_metadata.OPERATION_SCHEMA
                ),
                "fingerprint": operation_fingerprint,
                "action": action,
                "match_run_id": match_run_id,
                "operation_request": request,
                "applicant_receipt": applicant_receipt,
                "result_snapshot": result_snapshot,
            }
        }
        if operation_noop:
            operation_metadata["transition_class"] = "operation_noop"
        if terminal_kind == "resolve":
            operation_metadata["resolution"] = "post_migration_user_action"

        terminal = _apply_terminal_transition(
            conn,
            action=action,
            terminal_kind=terminal_kind,
            resolution_required=resolution_required,
            current=current,
            target=target,
            pipeline_item_id=pipeline_item_id,
            owner_profile_id=owner_profile_id,
            idempotency_key=idempotency_key,
            actor_source=actor_source,
            metadata=operation_metadata,
        )
        if preparatory_count:
            _inject(failure_injector, "after_second_normalized_transition")
        else:
            _inject(failure_injector, "after_first_normalized_transition")

        item_after = _load_item(conn, pipeline_item_id)
        preparatory_transitions = [
            pipeline_state.transition_dict(
                conn.execute(
                    "SELECT * FROM user_pipeline_transitions WHERE transition_id = ?",
                    (transition_id,),
                ).fetchone()
            )
            for transition_id in preparatory_transition_ids
        ]
        try:
            transition_metadata.validate_terminal_operation_metadata(
                metadata=terminal.transition["metadata"],
                transition=terminal.transition,
                persisted_item=item_after,
                preparatory_transitions=preparatory_transitions,
            )
        except transition_metadata.TransitionMetadataError as exc:
            raise PipelineInvariantError(
                "Committed pipeline action result snapshot is malformed."
            ) from exc
        _validate_post_action(
            item_after=item_after,
            state=terminal.state,
            expected_compatibility=compatibility_after,
            applicant_update=applicant,
            conn=conn,
        )
        _inject(failure_injector, "after_reconciliation_validation")
        result = PipelineActionResult(
            pipeline_item=_public_item_identity(item_after),
            state=terminal.state,
            transition=terminal.transition,
            compatibility_state=compatibility_after,
            applicant_update=dict(applicant) if applicant is not None else None,
            created=created,
            replayed=False,
        )
        _inject(failure_injector, "before_outer_transaction_release")
        return result


orchestrate_pipeline_action = perform_pipeline_action


def _plan_target(*, action, current, created, reminder_at, legacy_status):
    state = pipeline_state.validate_state(current)
    if action in WORKFLOW_ACTIONS:
        target_status = WORKFLOW_ACTIONS[action]
        if created and action != "save" and target_status != "applied":
            raise pipeline_state.InvalidTransition(
                f"A new item cannot start with action {action}. Save or apply it first."
            )
        if state["workflow_status"] is None:
            return {
                **state,
                "workflow_status": target_status,
                "workflow_status_provenance": "known",
            }, "resolve", True
        if state["workflow_status"] == target_status:
            if transition_metadata.accepted_noop_state(
                action=action,
                before_state=state,
                after_state=state,
                reminder_at=reminder_at,
            ):
                return state, "noop", False
            raise pipeline_state.InvalidTransition(
                "Repeated workflow action is unavailable while the item is hidden."
            )
        if target_status not in pipeline_state.WORKFLOW_TRANSITIONS[state["workflow_status"]]:
            raise pipeline_state.InvalidTransition(
                f"Workflow transition is not allowed: {state['workflow_status']} -> {target_status}"
            )
        return {**state, "workflow_status": target_status, "workflow_status_provenance": "known"}, "workflow", False
    if action == "remind_later":
        if transition_metadata.accepted_noop_state(
            action=action,
            before_state=state,
            after_state=state,
            reminder_at=reminder_at,
        ):
            return state, "noop", False
        return {**state, "reminder_at": reminder_at}, "reminder", False
    if action == "not_interested":
        if transition_metadata.accepted_noop_state(
            action=action,
            before_state=state,
            after_state=state,
            reminder_at=reminder_at,
        ):
            return state, "noop", False
        return {**state, "visibility": "hidden"}, "hide", False
    if action == "show_again":
        if state["workflow_status"] is None:
            raise UnresolvedLegacyWorkflow(
                "Unknown migrated workflow requires Show again as Saved."
            )
        if state["visibility"] == "visible":
            if transition_metadata.accepted_noop_state(
                action=action,
                before_state=state,
                after_state=state,
                reminder_at=reminder_at,
            ):
                return state, "noop", False
            raise pipeline_state.InvalidTransition(
                "Show again is unavailable for an already-visible non-saved item."
            )
        return {**state, "visibility": "visible"}, "show", False
    if action == "show_again_as_saved":
        if state["workflow_status"] is not None or state["visibility"] != "hidden":
            raise pipeline_state.InvalidTransition(
                "Show again as Saved requires a hidden unknown migrated workflow."
            )
        return {
            **state,
            "workflow_status": "saved",
            "workflow_status_provenance": "known",
            "visibility": "visible",
        }, "show", True
    raise PipelineActionError(f"Unsupported pipeline action: {action}")


def _apply_terminal_transition(
    conn,
    *,
    action,
    terminal_kind,
    resolution_required,
    current,
    target,
    pipeline_item_id,
    owner_profile_id,
    idempotency_key,
    actor_source,
    metadata,
):
    common = {
        "pipeline_item_id": pipeline_item_id,
        "owner_profile_id": owner_profile_id,
        "idempotency_key": idempotency_key,
        "actor_source": actor_source,
        "metadata": metadata,
    }
    if terminal_kind == "noop":
        return pipeline_state.record_operation_noop(
            conn,
            expected_version=current["version"],
            action_name=f"product_noop_{action}",
            **common,
        )
    if terminal_kind == "resolve":
        return pipeline_state.resolve_unknown_workflow(
            conn,
            workflow_status=target["workflow_status"],
            expected_version=current["version"],
            action_name=f"resolve_unknown_workflow_{target['workflow_status']}",
            **common,
        )
    if terminal_kind == "workflow":
        return pipeline_state.change_workflow_status(
            conn,
            workflow_status=target["workflow_status"],
            expected_version=current["version"],
            action_name=f"product_{action}",
            **common,
        )
    if terminal_kind == "reminder":
        return pipeline_state.set_reminder(
            conn,
            reminder_at=target["reminder_at"],
            expected_version=current["version"],
            action_name="product_remind_later",
            **common,
        )
    if terminal_kind == "hide":
        return pipeline_state.hide_item(
            conn,
            expected_version=current["version"],
            action_name="product_not_interested",
            **common,
        )
    if terminal_kind == "show":
        return pipeline_state.show_item(
            conn,
            expected_version=current["version"],
            action_name=(
                "product_show_again_after_resolution"
                if resolution_required
                else "product_show_again"
            ),
            **common,
        )
    raise PipelineInvariantError(f"Unknown terminal transition kind: {terminal_kind}")


def _replay_complete_operation(
    conn,
    *,
    pipeline_item_id,
    owner_profile_id,
    idempotency_key,
    operation_fingerprint,
):
    row = conn.execute(
        "SELECT * FROM user_pipeline_transitions WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()
    if row is None:
        return None
    try:
        transition = pipeline_state.transition_dict(row)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise PipelineInvariantError(
            "Committed pipeline action result snapshot is malformed."
        ) from exc
    metadata = transition.get("metadata")
    product = metadata.get("pipeline_action") if type(metadata) is dict else None
    if not isinstance(product, dict):
        pipeline_state.require_owned_pipeline_item(
            conn, pipeline_item_id, owner_profile_id
        )
        if row["pipeline_item_id"] != pipeline_item_id or row["profile_id"] != owner_profile_id:
            raise pipeline_state.IdempotencyConflict(
                "Idempotency key was already used for a different mutation."
            )
        raise PipelineInvariantError(
            "Committed pipeline action metadata is missing or malformed."
        )
    if product.get("fingerprint") != operation_fingerprint:
        raise pipeline_state.IdempotencyConflict(
            "Idempotency key was already used for a different mutation."
        )
    pipeline_state.require_owned_pipeline_item(
        conn, pipeline_item_id, owner_profile_id
    )
    if row["pipeline_item_id"] != pipeline_item_id or row["profile_id"] != owner_profile_id:
        raise pipeline_state.IdempotencyConflict(
            "Idempotency key was already used for a different mutation."
        )
    item = _load_item(conn, pipeline_item_id)
    snapshot = product.get("result_snapshot") if type(product) is dict else None
    preparatory_ids = (
        snapshot.get("preparatory_transition_ids") if type(snapshot) is dict else None
    )
    try:
        preparatory_transitions = []
        if type(preparatory_ids) is list and all(
            type(value) is str for value in preparatory_ids
        ):
            for transition_id in preparatory_ids:
                preparatory_row = conn.execute(
                    "SELECT * FROM user_pipeline_transitions WHERE transition_id = ?",
                    (transition_id,),
                ).fetchone()
                if preparatory_row is not None:
                    preparatory_transitions.append(
                        pipeline_state.transition_dict(preparatory_row)
                    )
        validated = transition_metadata.validate_terminal_operation_metadata(
            metadata=transition["metadata"],
            transition=transition,
            persisted_item=item,
            preparatory_transitions=preparatory_transitions,
        )
    except (
        transition_metadata.TransitionMetadataError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as exc:
        raise PipelineInvariantError(
            "Committed pipeline action result snapshot is malformed."
        ) from exc
    stored = validated["result_snapshot"]
    return PipelineActionResult(
        pipeline_item=dict(stored["pipeline_item"]),
        state=dict(stored["normalized_state"]),
        transition=transition,
        compatibility_state=dict(stored["compatibility_state"]),
        applicant_update=(
            dict(stored["applicant_update"])
            if stored["applicant_update"] is not None
            else None
        ),
        created=stored["created"],
        replayed=True,
    )


def _normalize_opportunity(*, source, title, url, opportunity_external_id, canonical_id):
    if source is None and title is None:
        return None
    source = _required_text(source, "source")
    title = _required_text(title, "title")
    url = str(url or "").strip()
    external_id = str(opportunity_external_id or "").strip()
    if canonical_id not in (None, ""):
        try:
            canonical_id = int(canonical_id)
        except (TypeError, ValueError) as exc:
            raise PipelineActionError("canonical_id must be an integer or null.") from exc
    else:
        canonical_id = None
    return {
        "source": source,
        "title": title,
        "url": url,
        "opportunity_external_id": external_id,
        "canonical_id": canonical_id,
    }


def _validate_product_expected_version(value, *, allow_absent_creation):
    if value is EXPECTED_VERSION_ABSENT:
        if allow_absent_creation:
            return 0
        raise pipeline_state.InvalidExpectedVersion(
            "An expected state version is required for an existing pipeline item."
        )
    if type(value) is not int or value < 0:
        raise pipeline_state.InvalidExpectedVersion(
            "Expected version must be a built-in non-negative integer."
        )
    return value


def _normalize_optional_is_sample(value):
    if value is None:
        return None
    if type(value) is not int or value not in {0, 1}:
        raise PipelineActionValidationError("is_sample must be 0, 1, or omitted.")
    return value


def _requested_effect(action, reminder_at):
    return transition_metadata.requested_effect(action, reminder_at)


def _required_text(value, field):
    text = str(value or "").strip()
    if not text:
        raise PipelineActionValidationError(f"{field} is required.")
    return text


def _require_profile(conn, profile_id):
    row = conn.execute(
        "SELECT * FROM user_profiles WHERE profile_id = ?", (profile_id,)
    ).fetchone()
    if row is None:
        raise pipeline_state.OwnershipError(f"Unknown profile: {profile_id}")
    return row


def _load_item(conn, pipeline_item_id):
    return conn.execute(
        "SELECT * FROM user_pipeline_items WHERE pipeline_item_id = ?",
        (pipeline_item_id,),
    ).fetchone()


def _require_item_owner(item, owner_profile_id):
    if item["profile_id"] != owner_profile_id:
        raise pipeline_state.OwnershipError(
            "Pipeline item belongs to a different profile."
        )


def _same_opportunity(item, opportunity):
    return (
        item["source"] == opportunity["source"]
        and item["opportunity_title"] == opportunity["title"]
        and (item["opportunity_url"] or "") == opportunity["url"]
        and (item["opportunity_external_id"] or "")
        == opportunity["opportunity_external_id"]
        and item["canonical_id"] == opportunity["canonical_id"]
    )


def _insert_pipeline_item(conn, *, pipeline_item_id, profile, opportunity, occurred_at):
    conn.execute(
        """
        INSERT INTO user_pipeline_items (
          pipeline_item_id, user_id, profile_id, source, opportunity_title,
          opportunity_url, opportunity_external_id, canonical_id, status,
          status_date, user_priority, reminder_date, notes, last_user_action,
          is_sample, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'saved', ?, 'medium', '', '',
                'Saved from local UI', 0, ?, ?)
        """,
        (
            pipeline_item_id,
            profile["user_id"],
            profile["profile_id"],
            opportunity["source"],
            opportunity["title"],
            opportunity["url"],
            opportunity["opportunity_external_id"],
            opportunity["canonical_id"],
            occurred_at[:10],
            occurred_at,
            occurred_at,
        ),
    )
    return _load_item(conn, pipeline_item_id)


def _update_legacy_status(conn, *, item_id, status, status_date):
    conn.execute(
        "UPDATE user_pipeline_items SET status = ?, status_date = ? WHERE id = ?",
        (status, status_date, item_id),
    )


def _update_legacy_reminder(conn, *, item_id, reminder_date):
    conn.execute(
        "UPDATE user_pipeline_items SET reminder_date = ? WHERE id = ?",
        (reminder_date, item_id),
    )


def _update_legacy_action_fields(
    conn, *, item_id, notes, last_user_action, is_sample, updated_at
):
    conn.execute(
        """
        UPDATE user_pipeline_items
        SET notes = ?, last_user_action = ?, is_sample = ?, updated_at = ?
        WHERE id = ?
        """,
        (notes, last_user_action, is_sample, updated_at, item_id),
    )


def _upsert_applicant_update(
    conn,
    *,
    update_id,
    profile,
    item,
    status,
    previous_status,
    status_date,
    reported_at,
    evidence_type,
    confidence_level,
    note,
):
    existed = conn.execute(
        "SELECT 1 FROM applicant_status_updates WHERE update_id = ?", (update_id,)
    ).fetchone() is not None
    conn.execute(
        """
        INSERT INTO applicant_status_updates (
          update_id, user_id, anonymous_user_key, profile_id, source,
          opportunity_title, opportunity_url, opportunity_external_id,
          canonical_id, status, previous_status, status_date, reported_at,
          evidence_type, confidence_level, notes, is_sample
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        ON CONFLICT(update_id) DO UPDATE SET
          previous_status = excluded.previous_status,
          reported_at = excluded.reported_at,
          notes = excluded.notes,
          updated_at = CURRENT_TIMESTAMP
        """,
        (
            update_id,
            profile["user_id"],
            profile["user_id"],
            profile["profile_id"],
            item["source"],
            item["opportunity_title"],
            item["opportunity_url"] or "",
            "",
            None,
            status,
            previous_status or "",
            status_date,
            reported_at,
            evidence_type,
            confidence_level,
            note,
        ),
    )
    row = dict(
        conn.execute(
            "SELECT * FROM applicant_status_updates WHERE update_id = ?",
            (update_id,),
        ).fetchone()
    )
    return row, ("updated" if existed else "created")


def _public_item_identity(item):
    return {
        "id": item["id"],
        "pipeline_item_id": item["pipeline_item_id"],
        "user_id": item["user_id"],
        "profile_id": item["profile_id"],
        "source": item["source"],
        "opportunity_title": item["opportunity_title"],
        "opportunity_url": item["opportunity_url"] or "",
        "opportunity_external_id": item["opportunity_external_id"] or "",
        "canonical_id": item["canonical_id"],
    }


def _compatibility_snapshot(item):
    return {
        "status": item["status"],
        "status_date": item["status_date"],
        "reminder_date": item["reminder_date"] or "",
        "notes": item["notes"],
        "last_user_action": item["last_user_action"],
        "is_sample": item["is_sample"],
        "updated_at": item["updated_at"],
    }


def _validate_current_invariants(state):
    if (
        state["workflow_status"] is None
        and state["visibility"] == "visible"
        and state["reminder_at"] is None
    ):
        raise PipelineInvariantError(
            "Visible unknown workflow without a reminder is invalid."
        )


def _validate_post_action(
    *, item_after, state, expected_compatibility, applicant_update, conn
):
    actual = legacy_compatibility_from_state(state)
    if actual["status"] != item_after["status"]:
        raise PipelineInvariantError("Legacy status mirror does not match normalized state.")
    if actual["reminder_date"] != (item_after["reminder_date"] or ""):
        raise PipelineInvariantError("Legacy reminder mirror does not match normalized state.")
    for field in ("status", "status_date", "reminder_date", "notes", "last_user_action", "is_sample", "updated_at"):
        actual_value = item_after[field]
        if field == "reminder_date":
            actual_value = actual_value or ""
        if actual_value != expected_compatibility[field]:
            raise PipelineInvariantError(f"Legacy compatibility field drifted: {field}")
    if applicant_update is not None:
        row = conn.execute(
            "SELECT * FROM applicant_status_updates WHERE update_id = ?",
            (applicant_update["update_id"],),
        ).fetchone()
        if row is None:
            raise PipelineInvariantError("Expected applicant update is missing.")
        for field in deterministic_applicant_fields():
            if row[field] != applicant_update[field]:
                raise PipelineInvariantError(
                    f"Applicant compatibility field drifted: {field}"
                )


def _applicant_payload(action, note):
    effect = transition_metadata.APPLICANT_EFFECTS.get(action)
    if effect is None:
        return None
    return {
        "status": effect["status"],
        "evidence_type": effect["evidence_type"],
        "confidence_level": effect["confidence_level"],
        "note": note,
    }


def deterministic_applicant_fields():
    return transition_metadata.deterministic_applicant_fields()


def _derived_idempotency_key(
    *, caller_key, operation_fingerprint, step, pipeline_item_id
):
    return transition_metadata.derive_internal_idempotency_key(
        caller_key=caller_key,
        operation_fingerprint=operation_fingerprint,
        step=step,
        pipeline_item_id=pipeline_item_id,
    )


def _inject(callback, point):
    if callback is not None:
        callback(point)

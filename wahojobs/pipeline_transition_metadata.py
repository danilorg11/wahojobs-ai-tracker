from __future__ import annotations

import hashlib
import json
from datetime import date, datetime

from wahojobs import pipeline_state


ORCHESTRATION_METADATA_VERSION = 1
RESULT_SNAPSHOT_VERSION = 1
RECEIPT_SCHEMA_VERSION = 1
OPERATION_SCHEMA = "pipeline_action_operation_v1"
NOOP_SCHEMA = "pipeline_action_noop_v1"
INITIALIZATION_SCHEMA = "user_initialization_v1"
LEGACY_BASELINE_SCHEMA = "legacy_migration_snapshot_v1"
APPLICANT_RECEIPT_SCHEMA = "applicant_operation_receipt_v1"
INTERNAL_IDEMPOTENCY_PREFIX = "wahojobs-internal:pipeline-action:v1:"

WORKFLOW_ACTIONS = {
    "save": "saved",
    "applied": "applied",
    "assessment_started": "assessment_started",
    "assessment_completed": "assessment_completed",
    "accepted": "accepted",
    "rejected": "rejected",
}
SUPPORTED_ACTIONS = set(WORKFLOW_ACTIONS) | {
    "remind_later",
    "not_interested",
    "show_again",
    "show_again_as_saved",
}
APPLICANT_EFFECTS = {
    action: {
        "status": status,
        "evidence_type": "self_reported",
        "confidence_level": "medium",
    }
    for action, status in WORKFLOW_ACTIONS.items()
    if action != "save"
}
NOOP_ACTION_CONTRACTS = {
    "save": {"workflow_status": "saved"},
    "applied": {"workflow_status": "applied"},
    "assessment_started": {
        "workflow_status": "assessment_started",
    },
    "assessment_completed": {
        "workflow_status": "assessment_completed",
    },
    "accepted": {"workflow_status": "accepted"},
    "rejected": {"workflow_status": "rejected"},
    "remind_later": {"reminder_matches_request": True},
    "not_interested": {"visibility": "hidden"},
    "show_again": {
        "workflow_status": "saved",
        "visibility": "visible",
    },
}
CREATION_ACTION_CONTRACTS = {
    "save": {
        "terminal_dimension": "workflow",
        "terminal_action_name": "product_noop_save",
        "terminal_class": "operation_noop",
        "workflow_status": "saved",
        "visibility": "visible",
        "applicant_required": False,
    },
    "applied": {
        "terminal_dimension": "workflow",
        "terminal_action_name": "product_applied",
        "terminal_class": None,
        "workflow_status": "applied",
        "visibility": "visible",
        "applicant_required": True,
    },
    "not_interested": {
        "terminal_dimension": "visibility",
        "terminal_action_name": "product_not_interested",
        "terminal_class": None,
        "workflow_status": "saved",
        "visibility": "hidden",
        "applicant_required": False,
    },
}

PIPELINE_ITEM_FIELDS = {
    "id",
    "pipeline_item_id",
    "user_id",
    "profile_id",
    "source",
    "opportunity_title",
    "opportunity_url",
    "opportunity_external_id",
    "canonical_id",
}
NORMALIZED_STATE_FIELDS = {
    "pipeline_item_id",
    "workflow_status",
    "workflow_status_provenance",
    "visibility",
    "reminder_at",
    "version",
}
COMPATIBILITY_FIELDS = {
    "status",
    "status_date",
    "reminder_date",
    "notes",
    "last_user_action",
    "is_sample",
    "updated_at",
}
APPLICANT_FIELDS = {
    "id",
    "update_id",
    "user_id",
    "anonymous_user_key",
    "profile_id",
    "source",
    "opportunity_title",
    "opportunity_url",
    "opportunity_external_id",
    "canonical_id",
    "status",
    "previous_status",
    "status_date",
    "reported_at",
    "evidence_type",
    "confidence_level",
    "notes",
    "is_sample",
    "created_at",
    "updated_at",
}
DETERMINISTIC_APPLICANT_FIELDS = (
    "update_id",
    "user_id",
    "anonymous_user_key",
    "profile_id",
    "source",
    "opportunity_title",
    "opportunity_url",
    "opportunity_external_id",
    "canonical_id",
    "status",
    "previous_status",
    "status_date",
    "reported_at",
    "evidence_type",
    "confidence_level",
    "notes",
    "is_sample",
)
STATE_FIELDS = {
    "workflow_status",
    "workflow_status_provenance",
    "visibility",
    "reminder_at",
}


class TransitionMetadataError(Exception):
    """Generic fail-closed metadata error with a non-sensitive diagnostic code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__("Pipeline transition metadata is malformed.")


def _fail(code: str):
    raise TransitionMetadataError(code)


def _exact_dict(value, fields, code):
    if type(value) is not dict or set(value) != set(fields):
        _fail(code)
    return value


def _string(value, code, *, nullable=False, nonempty=False):
    if value is None and nullable:
        return value
    if type(value) is not str or (nonempty and not value.strip()):
        _fail(code)
    return value


def _integer(value, code, *, nullable=False, minimum=None):
    if value is None and nullable:
        return value
    if type(value) is not int or (minimum is not None and value < minimum):
        _fail(code)
    return value


def _boolean(value, code):
    if type(value) is not bool:
        _fail(code)
    return value


def _timestamp(value, code, *, nullable=False):
    if value is None and nullable:
        return value
    _string(value, code, nonempty=True)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _fail(code)
    return value


def _date_or_empty(value, code, *, nullable=False):
    if value is None and nullable:
        return value
    _string(value, code)
    if value:
        try:
            date.fromisoformat(value)
        except ValueError:
            _fail(code)
    return value


def _hex_digest(value, code):
    _string(value, code, nonempty=True)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        _fail(code)
    return value


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def request_fingerprint(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def derive_internal_idempotency_key(
    *, caller_key, operation_fingerprint, step, pipeline_item_id
):
    payload = {
        "caller_key": caller_key,
        "operation_fingerprint": operation_fingerprint,
        "pipeline_item_id": pipeline_item_id,
        "step": step,
    }
    return f"{INTERNAL_IDEMPOTENCY_PREFIX}{request_fingerprint(payload)}"


def stable_applicant_update_id(
    *, profile_id: str, source: str, title: str, status: str, status_date: str, note: str
) -> str:
    values = [
        profile_id,
        source,
        title,
        status,
        status_date,
        "self_reported",
        "medium",
        note,
    ]
    digest = hashlib.sha1("|".join(values).encode("utf-8")).hexdigest()[:16]
    return f"applicant-update::{digest}"


def deterministic_applicant_fields():
    return DETERMINISTIC_APPLICANT_FIELDS


def requested_effect(action, reminder_at):
    return {
        "workflow_status": WORKFLOW_ACTIONS.get(action),
        "visibility": (
            "hidden"
            if action == "not_interested"
            else "visible" if action in {"show_again", "show_again_as_saved"} else None
        ),
        "reminder_at": reminder_at if action == "remind_later" else None,
        "resolution_mode": (
            "unknown_workflow_as_saved" if action == "show_again_as_saved" else None
        ),
    }


def accepted_noop_state(*, action, before_state, after_state, reminder_at):
    contract = NOOP_ACTION_CONTRACTS.get(action)
    if contract is None:
        return False
    before = pipeline_state.validate_state(before_state)
    after = pipeline_state.validate_state(after_state)
    if canonical_json(before) != canonical_json(after):
        return False
    if contract.get("workflow_status") is not None and before["workflow_status"] != contract[
        "workflow_status"
    ]:
        return False
    if contract.get("visibility") is not None and before["visibility"] != contract["visibility"]:
        return False
    if contract.get("reminder_matches_request") and (
        before["reminder_at"] != reminder_at or reminder_at is None
    ):
        return False
    return True


def deterministic_compatibility_from_state(state: dict) -> dict:
    normalized = pipeline_state.validate_state(state)
    if normalized["visibility"] == "hidden":
        status = "not_interested"
    elif normalized["workflow_status"] is not None:
        status = normalized["workflow_status"]
    elif normalized["reminder_at"] is not None:
        status = "remind_later"
    else:
        _fail("visible_unknown_without_reminder")
    reminder = normalized["reminder_at"]
    return {"status": status, "reminder_date": reminder[:10] if reminder else ""}


def _validate_pipeline_item(value):
    item = _exact_dict(value, PIPELINE_ITEM_FIELDS, "invalid_pipeline_item_snapshot")
    _integer(item["id"], "invalid_pipeline_item_snapshot", minimum=1)
    for field in (
        "pipeline_item_id",
        "user_id",
        "profile_id",
        "source",
        "opportunity_title",
    ):
        _string(item[field], "invalid_pipeline_item_snapshot", nonempty=True)
    for field in ("opportunity_url", "opportunity_external_id"):
        _string(item[field], "invalid_pipeline_item_snapshot")
    _integer(item["canonical_id"], "invalid_pipeline_item_snapshot", nullable=True, minimum=1)
    return item


def _validate_normalized_state(value):
    state = _exact_dict(value, NORMALIZED_STATE_FIELDS, "invalid_normalized_state_snapshot")
    _string(state["pipeline_item_id"], "invalid_normalized_state_snapshot", nonempty=True)
    if state["workflow_status"] is not None:
        _string(state["workflow_status"], "invalid_normalized_state_snapshot")
        if state["workflow_status"] not in pipeline_state.WORKFLOW_STATUSES:
            _fail("invalid_normalized_state_snapshot")
    if state["workflow_status_provenance"] not in pipeline_state.WORKFLOW_PROVENANCE:
        _fail("invalid_normalized_state_snapshot")
    if state["visibility"] not in pipeline_state.VISIBILITIES:
        _fail("invalid_normalized_state_snapshot")
    _timestamp(state["reminder_at"], "invalid_normalized_state_snapshot", nullable=True)
    _integer(state["version"], "invalid_normalized_state_snapshot", minimum=1)
    return state


def _validate_compatibility(value):
    compatibility = _exact_dict(value, COMPATIBILITY_FIELDS, "invalid_compatibility_snapshot")
    _string(compatibility["status"], "invalid_compatibility_snapshot", nonempty=True)
    _date_or_empty(compatibility["status_date"], "invalid_compatibility_snapshot", nullable=True)
    _date_or_empty(compatibility["reminder_date"], "invalid_compatibility_snapshot")
    _string(compatibility["notes"], "invalid_compatibility_snapshot", nullable=True)
    _string(compatibility["last_user_action"], "invalid_compatibility_snapshot", nullable=True)
    sample = _integer(compatibility["is_sample"], "invalid_compatibility_snapshot")
    if sample not in {0, 1}:
        _fail("invalid_compatibility_snapshot")
    _timestamp(compatibility["updated_at"], "invalid_compatibility_snapshot")
    return compatibility


def _validate_applicant(value):
    applicant = _exact_dict(value, APPLICANT_FIELDS, "invalid_applicant_snapshot")
    _integer(applicant["id"], "invalid_applicant_snapshot", minimum=1)
    for field in ("update_id", "profile_id", "source", "opportunity_title", "status"):
        _string(applicant[field], "invalid_applicant_snapshot", nonempty=True)
    for field in (
        "user_id",
        "anonymous_user_key",
        "opportunity_url",
        "opportunity_external_id",
        "previous_status",
        "notes",
    ):
        _string(applicant[field], "invalid_applicant_snapshot", nullable=True)
    _integer(applicant["canonical_id"], "invalid_applicant_snapshot", nullable=True, minimum=1)
    _date_or_empty(applicant["status_date"], "invalid_applicant_snapshot")
    _timestamp(applicant["reported_at"], "invalid_applicant_snapshot")
    _string(applicant["evidence_type"], "invalid_applicant_snapshot", nonempty=True)
    _string(applicant["confidence_level"], "invalid_applicant_snapshot", nonempty=True)
    sample = _integer(applicant["is_sample"], "invalid_applicant_snapshot")
    if sample not in {0, 1}:
        _fail("invalid_applicant_snapshot")
    _timestamp(applicant["created_at"], "invalid_applicant_snapshot")
    _timestamp(applicant["updated_at"], "invalid_applicant_snapshot")
    return applicant


def _validate_operation_request(value):
    fields = {
        "schema_version",
        "action",
        "owner_profile_id",
        "pipeline_item_id",
        "match_run_id",
        "expected_version",
        "expected_version_was_supplied",
        "identity_mode",
        "opportunity",
        "reminder_at",
        "requested_effect",
        "note",
        "actor_source",
        "is_sample",
        "applicant_payload",
    }
    request = _exact_dict(value, fields, "invalid_operation_request")
    if request["schema_version"] != ORCHESTRATION_METADATA_VERSION:
        _fail("invalid_operation_request")
    if request["action"] not in SUPPORTED_ACTIONS:
        _fail("invalid_operation_request")
    for field in ("owner_profile_id", "pipeline_item_id", "match_run_id", "actor_source"):
        _string(request[field], "invalid_operation_request", nonempty=True)
    _integer(request["expected_version"], "invalid_operation_request", minimum=0)
    _boolean(request["expected_version_was_supplied"], "invalid_operation_request")
    if request["identity_mode"] not in {"pipeline_item", "opportunity"}:
        _fail("invalid_operation_request")
    _timestamp(request["reminder_at"], "invalid_operation_request", nullable=True)
    _string(request["note"], "invalid_operation_request")
    sample = request["is_sample"]
    if sample is not None and (type(sample) is not int or sample not in {0, 1}):
        _fail("invalid_operation_request")
    opportunity = request["opportunity"]
    if opportunity is not None:
        opportunity = _exact_dict(
            opportunity,
            {"source", "title", "url", "opportunity_external_id", "canonical_id"},
            "invalid_operation_request",
        )
        for field in ("source", "title"):
            _string(opportunity[field], "invalid_operation_request", nonempty=True)
        for field in ("url", "opportunity_external_id"):
            _string(opportunity[field], "invalid_operation_request")
        _integer(opportunity["canonical_id"], "invalid_operation_request", nullable=True, minimum=1)
    effect = _exact_dict(
        request["requested_effect"],
        {"workflow_status", "visibility", "reminder_at", "resolution_mode"},
        "invalid_operation_request",
    )
    for field in ("workflow_status", "visibility", "resolution_mode"):
        _string(effect[field], "invalid_operation_request", nullable=True)
    _timestamp(effect["reminder_at"], "invalid_operation_request", nullable=True)
    payload = request["applicant_payload"]
    if payload is not None:
        payload = _exact_dict(
            payload,
            {"status", "evidence_type", "confidence_level", "note"},
            "invalid_operation_request",
        )
        for field in payload:
            _string(payload[field], "invalid_operation_request")
    return request


def _validate_applicant_receipt(value, *, action, pipeline_item_id):
    receipt = _exact_dict(
        value,
        {
            "schema_version",
            "metadata_schema",
            "action",
            "write_outcome",
            "pipeline_item_id",
            "applicant_update",
        },
        "malformed_applicant_receipt",
    )
    if (
        receipt["schema_version"] != RECEIPT_SCHEMA_VERSION
        or receipt["metadata_schema"] != APPLICANT_RECEIPT_SCHEMA
        or receipt["action"] != action
        or receipt["write_outcome"] not in {"created", "updated"}
        or receipt["pipeline_item_id"] != pipeline_item_id
    ):
        _fail("malformed_applicant_receipt")
    applicant = _exact_dict(
        receipt["applicant_update"],
        DETERMINISTIC_APPLICANT_FIELDS,
        "malformed_applicant_receipt",
    )
    complete = {field: applicant[field] for field in DETERMINISTIC_APPLICANT_FIELDS}
    complete.update({"id": 1, "created_at": "2000-01-01 00:00:00", "updated_at": "2000-01-01 00:00:00"})
    _validate_applicant(complete)
    return receipt


def build_applicant_receipt(*, action, pipeline_item_id, applicant, write_outcome):
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "metadata_schema": APPLICANT_RECEIPT_SCHEMA,
        "action": action,
        "write_outcome": write_outcome,
        "pipeline_item_id": pipeline_item_id,
        "applicant_update": {
            field: applicant[field] for field in DETERMINISTIC_APPLICANT_FIELDS
        },
    }


def validate_terminal_operation_metadata(
    *, metadata, transition, persisted_item, preparatory_transitions=None
):
    """Validate and return a complete immutable product-operation contract."""
    if type(metadata) is not dict or type(transition) is not dict:
        _fail("malformed_terminal_operation_metadata")
    transition_class = metadata.get("transition_class")
    expected_outer = {"pipeline_action"}
    expected_schema = OPERATION_SCHEMA
    if transition_class == "operation_noop":
        expected_outer.add("transition_class")
        expected_schema = NOOP_SCHEMA
    elif transition_class is not None:
        _fail("malformed_terminal_operation_metadata")
    if metadata.get("resolution") == "post_migration_user_action":
        expected_outer.add("resolution")
    _exact_dict(metadata, expected_outer, "malformed_terminal_operation_metadata")
    product = _exact_dict(
        metadata["pipeline_action"],
        {
            "schema_version",
            "metadata_schema",
            "fingerprint",
            "action",
            "match_run_id",
            "operation_request",
            "applicant_receipt",
            "result_snapshot",
        },
        "malformed_terminal_operation_metadata",
    )
    if (
        product["schema_version"] != ORCHESTRATION_METADATA_VERSION
        or product["metadata_schema"] != expected_schema
        or product["action"] not in SUPPORTED_ACTIONS
    ):
        _fail("malformed_terminal_operation_metadata")
    _string(product["match_run_id"], "malformed_terminal_operation_metadata", nonempty=True)
    _hex_digest(product["fingerprint"], "malformed_terminal_operation_metadata")
    request = _validate_operation_request(product["operation_request"])
    if (
        request_fingerprint(request) != product["fingerprint"]
        or request["action"] != product["action"]
        or request["match_run_id"] != product["match_run_id"]
        or request["pipeline_item_id"] != transition.get("pipeline_item_id")
        or request["owner_profile_id"] != transition.get("profile_id")
    ):
        _fail("operation_fingerprint_mismatch")
    if request["requested_effect"] != requested_effect(
        product["action"], request["reminder_at"]
    ):
        _fail("operation_effect_mismatch")
    expected_payload = None
    if product["action"] in APPLICANT_EFFECTS:
        effect = APPLICANT_EFFECTS[product["action"]]
        expected_payload = {
            "status": effect["status"],
            "evidence_type": effect["evidence_type"],
            "confidence_level": effect["confidence_level"],
            "note": request["note"],
        }
    if request["applicant_payload"] != expected_payload:
        _fail("operation_applicant_payload_mismatch")

    snapshot = _exact_dict(
        product["result_snapshot"],
        {
            "schema_version",
            "pipeline_item",
            "normalized_state",
            "compatibility_state",
            "applicant_update",
            "created",
            "terminal_transition_id",
            "preparatory_transition_ids",
        },
        "malformed_result_snapshot",
    )
    if snapshot["schema_version"] != RESULT_SNAPSHOT_VERSION:
        _fail("malformed_result_snapshot")
    item = _validate_pipeline_item(snapshot["pipeline_item"])
    state = _validate_normalized_state(snapshot["normalized_state"])
    compatibility = _validate_compatibility(snapshot["compatibility_state"])
    created = _boolean(snapshot["created"], "malformed_result_snapshot")
    _string(snapshot["terminal_transition_id"], "malformed_result_snapshot", nonempty=True)
    if type(snapshot["preparatory_transition_ids"]) is not list:
        _fail("malformed_result_snapshot")
    preparatory_ids = snapshot["preparatory_transition_ids"]
    if not all(type(value) is str and value.strip() for value in preparatory_ids):
        _fail("malformed_result_snapshot")
    if len(preparatory_ids) != len(set(preparatory_ids)):
        _fail("malformed_result_snapshot")

    persisted = dict(persisted_item)
    if (
        item["pipeline_item_id"] != transition.get("pipeline_item_id")
        or item["profile_id"] != transition.get("profile_id")
        or state["pipeline_item_id"] != transition.get("pipeline_item_id")
        or state["version"] != transition.get("state_version_after")
        or snapshot["terminal_transition_id"] != transition.get("transition_id")
        or canonical_json({key: state[key] for key in STATE_FIELDS})
        != canonical_json(transition.get("after_state"))
    ):
        _fail("result_snapshot_transition_mismatch")
    for snapshot_field, persisted_field in (
        ("id", "id"),
        ("pipeline_item_id", "pipeline_item_id"),
        ("user_id", "user_id"),
        ("profile_id", "profile_id"),
        ("source", "source"),
        ("opportunity_title", "opportunity_title"),
        ("opportunity_url", "opportunity_url"),
        ("opportunity_external_id", "opportunity_external_id"),
        ("canonical_id", "canonical_id"),
    ):
        persisted_value = persisted.get(persisted_field)
        if persisted_field in {"opportunity_url", "opportunity_external_id"}:
            persisted_value = persisted_value or ""
        if item[snapshot_field] != persisted_value:
            _fail("result_snapshot_item_mismatch")
    if request["opportunity"] is not None:
        opportunity = request["opportunity"]
        if (
            opportunity["source"] != item["source"]
            or opportunity["title"] != item["opportunity_title"]
            or opportunity["url"] != item["opportunity_url"]
            or opportunity["opportunity_external_id"] != item["opportunity_external_id"]
            or opportunity["canonical_id"] != item["canonical_id"]
        ):
            _fail("operation_opportunity_mismatch")
    mirror = deterministic_compatibility_from_state(state)
    if (
        compatibility["status"] != mirror["status"]
        or compatibility["reminder_date"] != mirror["reminder_date"]
    ):
        _fail("result_snapshot_compatibility_mismatch")
    if created != (
        request["identity_mode"] == "opportunity" and request["expected_version"] == 0
    ):
        _fail("result_snapshot_creation_mismatch")
    expected_preparatory_count = 1 if created or product["action"] == "show_again_as_saved" else 0
    if len(preparatory_ids) != expected_preparatory_count:
        _fail("result_snapshot_transition_identity_mismatch")
    if transition.get("state_version_before") != request["expected_version"] + len(
        preparatory_ids
    ):
        _fail("operation_expected_version_mismatch")
    if snapshot["terminal_transition_id"] in preparatory_ids:
        _fail("result_snapshot_transition_identity_mismatch")
    if preparatory_transitions is not None:
        if type(preparatory_transitions) is not list or len(preparatory_transitions) != len(
            preparatory_ids
        ):
            _fail("result_snapshot_transition_identity_mismatch")
        by_id = {value.get("transition_id"): value for value in preparatory_transitions}
        if set(by_id) != set(preparatory_ids):
            _fail("result_snapshot_transition_identity_mismatch")
        for preparatory_id in preparatory_ids:
            preparatory = by_id[preparatory_id]
            if (
                preparatory.get("pipeline_item_id") != transition.get("pipeline_item_id")
                or preparatory.get("profile_id") != transition.get("profile_id")
                or preparatory.get("state_version_after") != transition.get("state_version_before")
            ):
                _fail("result_snapshot_transition_identity_mismatch")
        if created and preparatory_ids:
            preparatory = by_id[preparatory_ids[0]]
            if preparatory.get("metadata", {}).get("transition_class") != "user_initialization":
                _fail("result_snapshot_transition_identity_mismatch")
        if product["action"] == "show_again_as_saved" and preparatory_ids:
            preparatory = by_id[preparatory_ids[0]]
            if preparatory.get("action_name") != "resolve_unknown_workflow_as_saved":
                _fail("result_snapshot_transition_identity_mismatch")
    if transition_class == "operation_noop" and transition.get("action_name") != (
        f"product_noop_{product['action']}"
    ):
        _fail("operation_noop_action_mismatch")
    if transition_class == "operation_noop":
        if not accepted_noop_state(
            action=product["action"],
            before_state=transition.get("before_state"),
            after_state=transition.get("after_state"),
            reminder_at=request["reminder_at"],
        ):
            _fail("noop_state_binding_mismatch")
        expected_transition_fingerprint = request_fingerprint(
            {
                "operation": "operation_noop",
                "pipeline_item_id": transition["pipeline_item_id"],
                "owner_profile_id": transition["profile_id"],
                "action_name": transition["action_name"],
                "expected_version": transition["state_version_before"],
                "actor_source": transition["actor_source"],
                "metadata": metadata,
            }
        )
        if transition.get("request_fingerprint") != expected_transition_fingerprint:
            _fail("noop_request_fingerprint_mismatch")

    applicant = snapshot["applicant_update"]
    receipt = product["applicant_receipt"]
    applicant_effect = APPLICANT_EFFECTS.get(product["action"])
    applicant_expected = applicant_effect is not None and transition_class != "operation_noop"
    if not applicant_expected:
        if applicant is not None or receipt is not None:
            _fail("forbidden_applicant_receipt")
    else:
        if applicant is None:
            _fail("missing_applicant_result")
        applicant = _validate_applicant(applicant)
        if receipt is None:
            _fail("missing_applicant_receipt")
        receipt = _validate_applicant_receipt(
            receipt,
            action=product["action"],
            pipeline_item_id=transition["pipeline_item_id"],
        )
        receipt_applicant = receipt["applicant_update"]
        if any(applicant[field] != receipt_applicant[field] for field in DETERMINISTIC_APPLICANT_FIELDS):
            _fail("applicant_result_receipt_mismatch")
        expected_status = applicant_effect["status"]
        expected_id = stable_applicant_update_id(
            profile_id=item["profile_id"],
            source=item["source"],
            title=item["opportunity_title"],
            status=expected_status,
            status_date=applicant["status_date"],
            note=applicant["notes"] or "",
        )
        if (
            applicant["update_id"] != expected_id
            or applicant["status"] != expected_status
            or applicant["evidence_type"] != applicant_effect["evidence_type"]
            or applicant["confidence_level"] != applicant_effect["confidence_level"]
            or applicant["profile_id"] != item["profile_id"]
            or applicant["user_id"] != item["user_id"]
            or applicant["anonymous_user_key"] != item["user_id"]
            or applicant["source"] != item["source"]
            or applicant["opportunity_title"] != item["opportunity_title"]
            or (applicant["opportunity_url"] or "") != item["opportunity_url"]
        ):
            _fail("applicant_action_binding_mismatch")
    return {
        "pipeline_action": product,
        "operation_request": request,
        "result_snapshot": snapshot,
        "applicant_receipt": receipt,
        "action": product["action"],
        "transition_class": transition_class,
    }


def validate_user_initialization_metadata(*, metadata, transition):
    fields = {
        "schema_version",
        "metadata_schema",
        "transition_class",
        "initialization_kind",
        "creation_origin",
        "first_requested_action",
        "legacy_snapshot",
        "product_action_fingerprint",
        "pipeline_item_id",
        "owner_profile_id",
        "match_run_id",
        "internal_step",
    }
    value = _exact_dict(metadata, fields, "invalid_user_initialization_metadata")
    if (
        value["schema_version"] != ORCHESTRATION_METADATA_VERSION
        or value["metadata_schema"] != INITIALIZATION_SCHEMA
        or value["transition_class"] != "user_initialization"
        or value["initialization_kind"] != "user_created"
        or value["creation_origin"] != "product_action"
        or type(value["legacy_snapshot"]) is not bool
        or value["legacy_snapshot"]
        or value["pipeline_item_id"] != transition.get("pipeline_item_id")
        or value["owner_profile_id"] != transition.get("profile_id")
    ):
        _fail("invalid_user_initialization_metadata")
    if value["first_requested_action"] not in CREATION_ACTION_CONTRACTS:
        _fail("user_initialization_action_binding_mismatch")
    if value["internal_step"] != "initialize":
        _fail("user_initialization_internal_key_mismatch")
    _hex_digest(value["product_action_fingerprint"], "invalid_user_initialization_metadata")
    _string(value["match_run_id"], "invalid_user_initialization_metadata", nonempty=True)
    if (
        not str(transition.get("idempotency_key") or "").startswith(
            INTERNAL_IDEMPOTENCY_PREFIX
        )
        or transition.get("transition_id")
        != pipeline_state.stable_transition_id(
            transition.get("pipeline_item_id"), transition.get("idempotency_key")
        )
    ):
        _fail("user_initialization_internal_key_mismatch")
    if (
        transition.get("affected_dimension") != "workflow"
        or transition.get("action_name") != "user_created"
        or transition.get("before_state") is not None
        or transition.get("state_version_before") != 0
        or transition.get("state_version_after") != 1
        or canonical_json(transition.get("after_state"))
        != canonical_json(
            {
                "workflow_status": "saved",
                "workflow_status_provenance": "known",
                "visibility": "visible",
                "reminder_at": None,
            }
        )
    ):
        _fail("invalid_user_initialization_transition")
    return value


def validate_user_initialization_binding(
    *, initialization_transition, terminal_transition, persisted_item
):
    initialization_metadata = validate_user_initialization_metadata(
        metadata=initialization_transition.get("metadata"),
        transition=initialization_transition,
    )
    terminal_metadata = terminal_transition.get("metadata")
    if type(terminal_metadata) is not dict:
        _fail("user_initialization_terminal_link_missing")
    product = terminal_metadata.get("pipeline_action")
    snapshot = product.get("result_snapshot") if type(product) is dict else None
    preparatory_ids = (
        snapshot.get("preparatory_transition_ids") if type(snapshot) is dict else None
    )
    if (
        type(preparatory_ids) is not list
        or preparatory_ids != [initialization_transition.get("transition_id")]
    ):
        _fail("user_initialization_terminal_link_missing")
    validated_terminal = validate_terminal_operation_metadata(
        metadata=terminal_metadata,
        transition=terminal_transition,
        persisted_item=persisted_item,
        preparatory_transitions=[initialization_transition],
    )
    product = validated_terminal["pipeline_action"]
    request = validated_terminal["operation_request"]
    result = validated_terminal["result_snapshot"]
    first_action = initialization_metadata["first_requested_action"]
    if product["action"] != first_action:
        _fail("user_initialization_action_binding_mismatch")
    if initialization_metadata["product_action_fingerprint"] != product["fingerprint"]:
        _fail("user_initialization_fingerprint_mismatch")
    if (
        initialization_metadata["pipeline_item_id"] != terminal_transition.get("pipeline_item_id")
        or initialization_metadata["owner_profile_id"] != terminal_transition.get("profile_id")
        or initialization_metadata["match_run_id"] != product["match_run_id"]
        or initialization_transition.get("pipeline_item_id")
        != terminal_transition.get("pipeline_item_id")
        or initialization_transition.get("profile_id") != terminal_transition.get("profile_id")
    ):
        _fail("user_initialization_action_binding_mismatch")
    expected_internal_key = derive_internal_idempotency_key(
        caller_key=terminal_transition.get("idempotency_key"),
        operation_fingerprint=product["fingerprint"],
        step=initialization_metadata["internal_step"],
        pipeline_item_id=terminal_transition.get("pipeline_item_id"),
    )
    if initialization_transition.get("idempotency_key") != expected_internal_key:
        _fail("user_initialization_internal_key_mismatch")
    contract = CREATION_ACTION_CONTRACTS[first_action]
    terminal_class = terminal_metadata.get("transition_class")
    after_state = pipeline_state.validate_state(terminal_transition.get("after_state"))
    if (
        terminal_transition.get("affected_dimension") != contract["terminal_dimension"]
        or terminal_transition.get("action_name") != contract["terminal_action_name"]
        or terminal_class != contract["terminal_class"]
        or after_state["workflow_status"] != contract["workflow_status"]
        or after_state["visibility"] != contract["visibility"]
        or (validated_terminal["applicant_receipt"] is not None)
        != contract["applicant_required"]
        or request["identity_mode"] != "opportunity"
        or request["expected_version"] != 0
        or result["created"] is not True
        or canonical_json(terminal_transition.get("before_state"))
        != canonical_json(initialization_transition.get("after_state"))
        or terminal_transition.get("state_version_before")
        != initialization_transition.get("state_version_after")
    ):
        _fail("user_initialization_action_binding_mismatch")
    return {
        "initialization": initialization_metadata,
        "terminal": validated_terminal,
    }


def validate_legacy_baseline_metadata(*, metadata, transition):
    fields = {
        "legacy_snapshot",
        "raw_legacy_status",
        "raw_legacy_reminder_date",
        "legacy_reminder_valid",
        "legacy_classification",
    }
    value = _exact_dict(metadata, fields, "invalid_migration_baseline_metadata")
    if type(value["legacy_snapshot"]) is not bool or not value["legacy_snapshot"]:
        _fail("invalid_migration_baseline_metadata")
    _string(value["raw_legacy_status"], "invalid_migration_baseline_metadata")
    _string(value["raw_legacy_reminder_date"], "invalid_migration_baseline_metadata")
    _boolean(value["legacy_reminder_valid"], "invalid_migration_baseline_metadata")
    _string(value["legacy_classification"], "invalid_migration_baseline_metadata", nonempty=True)
    if (
        transition.get("affected_dimension") != "baseline"
        or transition.get("action_name") != "legacy_snapshot"
        or transition.get("actor_source") != "legacy_migration"
        or not str(transition.get("idempotency_key") or "").startswith("legacy-baseline:v1:")
        or transition.get("before_state") is not None
        or transition.get("state_version_before") != 0
        or transition.get("state_version_after") != 1
    ):
        _fail("invalid_migration_baseline_transition")
    expected_state, expected_metadata, _ = pipeline_state.legacy_projection(
        {
            "status": value["raw_legacy_status"],
            "reminder_date": value["raw_legacy_reminder_date"],
        }
    )
    if expected_metadata != value or canonical_json(expected_state) != canonical_json(
        transition.get("after_state")
    ):
        _fail("migration_baseline_state_mismatch")
    return value

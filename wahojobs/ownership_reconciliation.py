from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime

from wahojobs.ownership import (
    ALIAS_KINDS,
    ALIAS_SOURCES,
    BINDING_ACTOR_TYPES,
    BINDING_EVENT_TYPES,
    BINDING_ROLES,
    BINDING_STATUSES,
    CLAIM_POLICIES,
    MIGRATION_VERSION,
    OWNERSHIP_TRIGGERS,
    PRINCIPAL_STATUSES,
    PRINCIPAL_TYPES,
    OwnershipValidationError,
    alias_family,
    discover_legacy_owners,
    event_request_fingerprint,
    report_local_references,
    validate_alias_id,
    validate_binding_event_id,
    validate_binding_id,
    validate_environment_namespace,
    validate_legacy_alias,
    validate_metadata_document,
    validate_principal_id,
    validate_sha256,
)
from wahojobs.ownership_schema import attest_ownership_schema, ownership_table_columns


APPEND_ONLY_TRIGGERS = frozenset(
    {
        "trg_legacy_owner_aliases_no_update",
        "trg_legacy_owner_aliases_no_delete",
        "trg_ownership_binding_events_no_update",
        "trg_ownership_binding_events_no_delete",
    }
)


def reconcile_ownership(conn) -> dict:
    """Inspect dormant ownership state without installing or repairing anything."""
    attestation = attest_ownership_schema(conn)
    objects = {
        (row[0], row[1])
        for row in conn.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE type IN ('table', 'index', 'trigger', 'view')"
        )
    }
    checks = _empty_checks()
    informational = _empty_information()
    checks["schema_attestation_failures"].extend(attestation["findings"])
    if not attestation["migration_marker_present"]:
        checks["migration_marker_missing"].append({"version": MIGRATION_VERSION})
    for item in attestation["findings"]:
        if item["reason"] == "missing_object":
            checks["required_objects_missing"].append(
                {"object": f"{item['expected_type']}:{item['object']}"}
            )
        if item["reason"] in {
            "same_name_conflicting_object",
            "unexpected_ownership_object",
        }:
            checks["unexpected_object_conflicts"].append(dict(item))
    for trigger in sorted(APPEND_ONLY_TRIGGERS):
        if ("trigger", trigger) not in objects:
            checks["append_only_protection_missing"].append({"trigger": trigger})

    discovery = discover_legacy_owners(conn)
    checks["malformed_legacy_owners"].extend(
        issue.public_dict() for issue in discovery.issues
    )
    _check_legacy_product_consistency(conn, objects, checks, informational)

    if _available(
        conn,
        "product_principals",
        {
            "principal_id",
            "environment_namespace",
            "principal_type",
            "lifecycle_status",
            "claim_policy",
            "exclusive_account_binding",
            "version",
            "created_at",
            "updated_at",
            "provenance_json",
        },
        "principal_rows",
        checks,
    ):
        _check_principals(conn, checks)

    aliases_available = _available(
        conn,
        "legacy_owner_aliases",
        {
            "alias_id",
            "principal_id",
            "environment_namespace",
            "alias_kind",
            "alias_value",
            "claimability",
            "discovered_from",
            "created_at",
            "provenance_json",
        },
        "alias_rows",
        checks,
    )
    if aliases_available:
        _check_aliases(conn, checks)
        _check_unregistered_aliases(conn, discovery.observations, informational)
    else:
        informational["unregistered_legacy_owners"].extend(
            item.public_dict() for item in discovery.observations
        )

    bindings_available = _available(
        conn,
        "principal_account_bindings",
        {
            "binding_id",
            "principal_id",
            "user_id",
            "environment_namespace",
            "binding_role",
            "binding_status",
            "version",
            "latest_event_version",
            "created_at",
            "updated_at",
            "suspended_at",
            "provenance_json",
        },
        "binding_rows",
        checks,
    )
    events_available = _available(
        conn,
        "ownership_binding_events",
        {
            "event_id",
            "principal_id",
            "user_id",
            "binding_id",
            "environment_namespace",
            "event_version",
            "event_type",
            "prior_status",
            "resulting_status",
            "actor_type",
            "reason_code",
            "approval_reference",
            "idempotency_key",
            "request_fingerprint",
            "occurred_at",
            "metadata_json",
        },
        "binding_event_rows",
        checks,
    )
    principals_available = _table_has_columns(
        conn,
        "product_principals",
        {
            "principal_id",
            "environment_namespace",
            "exclusive_account_binding",
            "created_at",
            "lifecycle_status",
            "claim_policy",
        },
    )
    users_available = _table_has_columns(
        conn, "users", {"user_id", "lifecycle_status", "created_at"}
    )
    if bindings_available and events_available and principals_available and users_available:
        _check_bindings_and_events(conn, checks)
    elif bindings_available or events_available:
        checks["check_unavailable"].append(
            {
                "check": "binding_relationships_and_event_projection",
                "reason": "related_table_columns_unavailable",
            }
        )

    try:
        for row in conn.execute("PRAGMA foreign_key_check"):
            checks["foreign_key_violations"].append(
                {
                    "table": row[0],
                    "rowid": row[1],
                    "parent": row[2],
                    "fk_index": row[3],
                }
            )
    except Exception:
        checks["check_unavailable"].append(
            {"check": "foreign_key_check", "reason": "database_check_unavailable"}
        )
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            checks["integrity_errors"].append({"result": "integrity_check_failed"})
    except Exception:
        checks["check_unavailable"].append(
            {"check": "integrity_check", "reason": "database_check_unavailable"}
        )

    counts = {
        "principals": _count_if_present(conn, objects, "product_principals"),
        "aliases": _count_if_present(conn, objects, "legacy_owner_aliases"),
        "bindings": _count_if_present(conn, objects, "principal_account_bindings"),
        "binding_events": _count_if_present(conn, objects, "ownership_binding_events"),
        "distinct_raw_value_count": discovery.distinct_raw_value_count,
        "distinct_kind_value_pair_count": discovery.distinct_kind_value_pair_count,
        "observation_count": discovery.observation_count,
        "kind_value_pair_classification_counts": discovery.kind_value_pair_classification_counts,
        "observation_classification_counts": discovery.observation_classification_counts,
        "development_kind_value_pair_count": discovery.development_kind_value_pair_count,
        "development_observation_count": discovery.development_observation_count,
        "local_singleton_kind_value_pair_count": discovery.local_singleton_kind_value_pair_count,
        "local_singleton_observation_count": discovery.local_singleton_observation_count,
        "sample_applicant_owner_variation_count": len(
            informational["sample_applicant_owner_variants"]
        ),
        "legacy_alias_discovery_issue_count": len(discovery.issues),
    }

    checks = _sorted_groups(checks)
    informational = _sorted_groups(informational)
    blocking_reasons = [name for name, rows in checks.items() if rows]
    informational_reasons = [name for name, rows in informational.items() if rows]
    return {
        "schema": attestation,
        "counts": counts,
        "checks": checks,
        "informational": informational,
        "blocking_reasons": blocking_reasons,
        "informational_reasons": informational_reasons,
        "blocking": bool(blocking_reasons),
        "fully_reconciled": not blocking_reasons,
        "read_only": True,
    }


def _empty_checks():
    names = (
        "schema_attestation_failures",
        "check_unavailable",
        "migration_marker_missing",
        "required_objects_missing",
        "unexpected_object_conflicts",
        "append_only_protection_missing",
        "malformed_principals",
        "malformed_principal_provenance",
        "malformed_aliases",
        "malformed_alias_provenance",
        "orphan_aliases",
        "duplicate_aliases",
        "legacy_alias_principal_split",
        "alias_environment_mismatches",
        "alias_claimability_mismatches",
        "malformed_bindings",
        "malformed_binding_provenance",
        "orphan_bindings",
        "binding_environment_mismatches",
        "bindings_to_unavailable_accounts",
        "bindings_to_unavailable_principals",
        "exclusive_owner_binding_conflicts",
        "malformed_events",
        "malformed_event_metadata",
        "event_relation_mismatches",
        "event_environment_mismatches",
        "event_version_chain_errors",
        "event_timestamp_errors",
        "ownership_event_request_fingerprint_mismatch",
        "ownership_event_idempotency_conflict",
        "ownership_binding_projection_mismatch",
        "malformed_legacy_owners",
        "pipeline_item_profile_inconsistencies",
        "transition_owner_mismatches",
        "applicant_owner_inconsistencies",
        "foreign_key_violations",
        "integrity_errors",
    )
    return {name: [] for name in names}


def _empty_information():
    return {"unregistered_legacy_owners": [], "sample_applicant_owner_variants": []}


def _check_principals(conn, checks):
    for row in conn.execute(
        "SELECT principal_id, environment_namespace, principal_type, lifecycle_status, "
        "claim_policy, exclusive_account_binding, version, created_at, updated_at, "
        "provenance_json FROM product_principals ORDER BY principal_id"
    ):
        principal_id = row[0]
        reasons = []
        try:
            validate_principal_id(principal_id)
            validate_environment_namespace(row[1])
        except OwnershipValidationError as exc:
            reasons.append(str(exc))
        if row[2] not in PRINCIPAL_TYPES:
            reasons.append("invalid_principal_type")
        if row[3] not in PRINCIPAL_STATUSES:
            reasons.append("invalid_lifecycle_status")
        if row[4] not in CLAIM_POLICIES:
            reasons.append("invalid_claim_policy")
        if not _principal_claim_policy_valid(row[2], row[4]):
            reasons.append("principal_type_claim_policy_mismatch")
        if row[5] not in (0, 1) or not isinstance(row[6], int) or row[6] < 1:
            reasons.append("invalid_projection_version")
        if not _ordered_timestamps(row[7], row[8]):
            reasons.append("invalid_principal_timestamps")
        if reasons:
            checks["malformed_principals"].append(
                {"principal_id": principal_id, "reasons": sorted(set(reasons))}
            )
        try:
            validate_metadata_document(row[9], field_name="provenance")
        except (OwnershipValidationError, RecursionError):
            checks["malformed_principal_provenance"].append(
                {"principal_id": principal_id, "reason": "invalid_provenance"}
            )


def _check_aliases(conn, checks):
    principals = {}
    if _table_has_columns(
        conn,
        "product_principals",
        {"principal_id", "environment_namespace", "claim_policy"},
    ):
        principals = {
            row[0]: (row[1], row[2])
            for row in conn.execute(
                "SELECT principal_id, environment_namespace, claim_policy FROM product_principals"
            )
        }
    seen = set()
    family_owners = defaultdict(set)
    family_aliases = defaultdict(list)
    for row in conn.execute(
        "SELECT alias_id, principal_id, environment_namespace, alias_kind, alias_value, "
        "claimability, discovered_from, created_at, provenance_json "
        "FROM legacy_owner_aliases ORDER BY alias_id"
    ):
        alias_id, principal_id, environment, kind, value = row[:5]
        reasons = []
        try:
            validate_alias_id(alias_id)
            validate_environment_namespace(environment)
            validate_legacy_alias(value)
            family = alias_family(kind)
        except OwnershipValidationError as exc:
            reasons.append(str(exc))
            family = "invalid"
        if kind not in ALIAS_KINDS:
            reasons.append("invalid_alias_kind")
        if row[5] not in CLAIM_POLICIES:
            reasons.append("invalid_claimability")
        if row[6] not in ALIAS_SOURCES:
            reasons.append("invalid_discovery_source")
        if not _canonical_timestamp(row[7]):
            reasons.append("invalid_created_at")
        if reasons:
            checks["malformed_aliases"].append(
                {"alias_id": alias_id, "reasons": sorted(set(reasons))}
            )
        principal = principals.get(principal_id)
        if principal is None:
            checks["orphan_aliases"].append({"alias_id": alias_id})
        else:
            if environment != principal[0]:
                checks["alias_environment_mismatches"].append({"alias_id": alias_id})
            if row[5] != principal[1]:
                checks["alias_claimability_mismatches"].append({"alias_id": alias_id})
        key = (environment, kind, value)
        if key in seen:
            checks["duplicate_aliases"].append({"alias_id": alias_id})
        seen.add(key)
        family_key = (environment, family, value)
        family_owners[family_key].add(principal_id)
        family_aliases[family_key].append(alias_id)
        try:
            validate_metadata_document(row[8], field_name="provenance")
        except (OwnershipValidationError, RecursionError):
            checks["malformed_alias_provenance"].append(
                {"alias_id": alias_id, "reason": "invalid_provenance"}
            )

    split_keys = [key for key, owners in family_owners.items() if len(owners) > 1]
    split_refs = report_local_references(
        [json.dumps(key, ensure_ascii=True, default=str) for key in split_keys],
        "legacy-owner-split",
    )
    for key in sorted(
        split_keys, key=lambda item: json.dumps(item, ensure_ascii=True, default=str)
    ):
        checks["legacy_alias_principal_split"].append(
            {
                "report_reference": split_refs[
                    json.dumps(key, ensure_ascii=True, default=str)
                ],
                "alias_family": key[1],
                "alias_count": len(family_aliases[key]),
                "principal_count": len(family_owners[key]),
            }
        )


def _check_bindings_and_events(conn, checks):
    principals = {
        row[0]: {
            "environment": row[1],
            "exclusive": bool(row[2]),
            "created_at": row[3],
            "status": row[4],
            "claim_policy": row[5],
        }
        for row in conn.execute(
            "SELECT principal_id, environment_namespace, exclusive_account_binding, "
            "created_at, lifecycle_status, claim_policy FROM product_principals"
        )
    }
    users = {
        row[0]: {"status": row[1], "created_at": row[2]}
        for row in conn.execute("SELECT user_id, lifecycle_status, created_at FROM users")
    }
    bindings = {}
    active_owners = defaultdict(list)
    for row in conn.execute(
        "SELECT binding_id, principal_id, user_id, environment_namespace, binding_role, "
        "binding_status, version, latest_event_version, created_at, updated_at, suspended_at, "
        "provenance_json FROM principal_account_bindings ORDER BY binding_id"
    ):
        binding_id, principal_id, user_id = row[:3]
        reasons = []
        try:
            validate_binding_id(binding_id)
            validate_environment_namespace(row[3])
        except OwnershipValidationError as exc:
            reasons.append(str(exc))
        if row[4] not in BINDING_ROLES or row[5] not in BINDING_STATUSES:
            reasons.append("invalid_binding_role_or_status")
        if not isinstance(row[6], int) or row[6] < 1 or row[7] != row[6]:
            reasons.append("invalid_binding_version")
        if not _ordered_timestamps(row[8], row[9]):
            reasons.append("invalid_binding_timestamps")
        if row[5] == "suspended" and not _canonical_timestamp(row[10]):
            reasons.append("invalid_suspension_timestamp")
        if row[5] != "suspended" and row[10] is not None:
            reasons.append("unexpected_suspension_timestamp")
        if reasons:
            checks["malformed_bindings"].append(
                {"binding_id": binding_id, "reasons": sorted(set(reasons))}
            )
        principal = principals.get(principal_id)
        user = users.get(user_id)
        if principal is None or user is None:
            checks["orphan_bindings"].append(
                {"binding_id": binding_id, "missing": "principal" if principal is None else "account"}
            )
        else:
            if row[3] != principal["environment"]:
                checks["binding_environment_mismatches"].append({"binding_id": binding_id})
            if user["status"] != "active":
                checks["bindings_to_unavailable_accounts"].append(
                    {"binding_id": binding_id, "lifecycle_status": user["status"]}
                )
            if row[5] == "active" and (
                principal["status"] != "active" or principal["claim_policy"] == "nonclaimable"
            ):
                checks["bindings_to_unavailable_principals"].append({"binding_id": binding_id})
            if not _timestamp_not_before(row[8], principal["created_at"]) or not _timestamp_not_before(
                row[8], user["created_at"]
            ):
                checks["malformed_bindings"].append(
                    {"binding_id": binding_id, "reasons": ["binding_predates_identity"]}
                )
            if principal["exclusive"] and row[4] == "owner" and row[5] == "active":
                active_owners[principal_id].append(binding_id)
        try:
            validate_metadata_document(row[11], field_name="provenance")
        except (OwnershipValidationError, RecursionError):
            checks["malformed_binding_provenance"].append(
                {"binding_id": binding_id, "reason": "invalid_provenance"}
            )
        bindings[binding_id] = {
            "principal_id": principal_id,
            "user_id": user_id,
            "environment": row[3],
            "status": row[5],
            "version": row[6],
            "created_at": row[8],
        }
    for principal_id, binding_ids in sorted(active_owners.items()):
        if len(binding_ids) > 1:
            checks["exclusive_owner_binding_conflicts"].append(
                {"principal_id": principal_id, "binding_count": len(binding_ids)}
            )

    events_by_binding = defaultdict(list)
    idempotency = defaultdict(list)
    for row in conn.execute(
        "SELECT event_id, principal_id, user_id, binding_id, environment_namespace, "
        "event_version, event_type, prior_status, resulting_status, actor_type, reason_code, "
        "approval_reference, idempotency_key, request_fingerprint, occurred_at, metadata_json "
        "FROM ownership_binding_events ORDER BY binding_id, event_version, event_id"
    ):
        event_id, principal_id, user_id, binding_id = row[:4]
        event = {
            "event_id": event_id,
            "principal_id": principal_id,
            "user_id": user_id,
            "binding_id": binding_id,
            "environment": row[4],
            "version": row[5],
            "type": row[6],
            "prior": row[7],
            "result": row[8],
            "actor": row[9],
            "reason": row[10],
            "approval": row[11],
            "idempotency": row[12],
            "fingerprint": row[13],
            "occurred_at": row[14],
        }
        reasons = []
        try:
            validate_binding_event_id(event_id)
            validate_environment_namespace(row[4])
        except OwnershipValidationError as exc:
            reasons.append(str(exc))
        if row[6] not in BINDING_EVENT_TYPES or row[9] not in BINDING_ACTOR_TYPES:
            reasons.append("invalid_event_type_or_actor")
        if not _event_transition_valid(row[6], row[7], row[8]):
            reasons.append("invalid_event_status_transition")
        if not isinstance(row[5], int) or row[5] < 1 or not _canonical_timestamp(row[14]):
            reasons.append("invalid_event_version_or_timestamp")
        if type(row[10]) is not str or not (1 <= len(row[10]) <= 128):
            reasons.append("invalid_reason_code")
        if type(row[12]) is not str or not (16 <= len(row[12]) <= 256) or row[12] != row[12].strip():
            reasons.append("invalid_idempotency_key")
        try:
            validate_sha256(row[13], field_name="request_fingerprint")
        except OwnershipValidationError as exc:
            reasons.append(str(exc))
        if reasons:
            checks["malformed_events"].append(
                {"event_id": event_id, "reasons": sorted(set(reasons))}
            )
        binding = bindings.get(binding_id)
        principal = principals.get(principal_id)
        user = users.get(user_id)
        if binding is None or binding["principal_id"] != principal_id or binding["user_id"] != user_id:
            checks["event_relation_mismatches"].append(
                {"event_id": event_id, "binding_id": binding_id}
            )
        elif row[4] != binding["environment"]:
            checks["event_environment_mismatches"].append(
                {"event_id": event_id, "binding_id": binding_id}
            )
        if principal and row[4] != principal["environment"]:
            checks["event_environment_mismatches"].append(
                {"event_id": event_id, "binding_id": binding_id}
            )
        boundaries = [
            item
            for item in (
                binding and binding["created_at"],
                principal and principal["created_at"],
                user and user["created_at"],
            )
            if item
        ]
        if any(not _timestamp_not_before(row[14], boundary) for boundary in boundaries):
            checks["event_timestamp_errors"].append(
                {"event_id": event_id, "binding_id": binding_id}
            )
        metadata = None
        try:
            metadata = validate_metadata_document(row[15], field_name="metadata")
        except (OwnershipValidationError, RecursionError):
            checks["malformed_event_metadata"].append(
                {"event_id": event_id, "reason": "invalid_metadata"}
            )
        if metadata is not None:
            try:
                durable = event_request_fingerprint(
                    principal_id=principal_id,
                    binding_id=binding_id,
                    user_id=user_id,
                    expected_event_version=row[5],
                    event_type=row[6],
                    prior_status=row[7],
                    resulting_status=row[8],
                    actor_type=row[9],
                    reason_code=row[10],
                    approval_reference=row[11],
                    occurred_at=row[14],
                    metadata=metadata,
                )
            except (OwnershipValidationError, TypeError, ValueError, RecursionError):
                checks["malformed_events"].append(
                    {"event_id": event_id, "reasons": ["fingerprint_recomputation_unavailable"]}
                )
            else:
                if row[13] != durable:
                    checks["ownership_event_request_fingerprint_mismatch"].append(
                        {"event_id": event_id, "binding_id": binding_id}
                    )
        events_by_binding[binding_id].append(event)
        idempotency[(principal_id, row[12])].append((event_id, row[13]))

    conflict_keys = [key for key, entries in idempotency.items() if len(entries) > 1]
    conflict_refs = report_local_references(
        [json.dumps(key, ensure_ascii=True, default=str) for key in conflict_keys],
        "idempotency",
    )
    for key in sorted(
        conflict_keys, key=lambda item: json.dumps(item, ensure_ascii=True, default=str)
    ):
        entries = idempotency[key]
        checks["ownership_event_idempotency_conflict"].append(
            {
                "report_reference": conflict_refs[
                    json.dumps(key, ensure_ascii=True, default=str)
                ],
                "event_count": len(entries),
                "changed_fingerprint": len({item[1] for item in entries}) > 1,
            }
        )
    for binding_id, binding in sorted(bindings.items()):
        events = events_by_binding.get(binding_id, [])
        chain_error = _binding_chain_error(events)
        if chain_error:
            checks["event_version_chain_errors"].append(
                {"binding_id": binding_id, "reason": chain_error}
            )
        if (
            not events
            or events[-1]["version"] != binding["version"]
            or events[-1]["result"] != binding["status"]
        ):
            checks["ownership_binding_projection_mismatch"].append(
                {"binding_id": binding_id, "event_count": len(events)}
            )


def _binding_chain_error(events):
    if not events:
        return "missing_event_history"
    expected_prior = None
    prior_time = None
    for expected_version, event in enumerate(events, 1):
        if event["version"] != expected_version:
            return "non_contiguous_event_version"
        if expected_version == 1:
            if event["type"] != "binding_activated" or event["prior"] is not None or event["result"] != "active":
                return "invalid_first_event"
        elif event["prior"] != expected_prior:
            return "prior_status_mismatch"
        if prior_time and not _timestamp_not_before(event["occurred_at"], prior_time):
            return "event_time_regression"
        expected_prior = event["result"]
        prior_time = event["occurred_at"]
    return None


def _check_unregistered_aliases(conn, observations, informational):
    registered = {
        (row[0], row[1])
        for row in conn.execute("SELECT alias_kind, alias_value FROM legacy_owner_aliases")
    }
    informational["unregistered_legacy_owners"].extend(
        item.public_dict()
        for item in observations
        if (item.alias_kind, item.private_alias_value) not in registered
    )


def _check_legacy_product_consistency(conn, objects, checks, informational):
    profiles = {}
    profile_rows = []
    item_rows = []
    transition_rows = []
    applicant_rows = []
    if ("table", "user_profiles") in objects:
        profile_rows = list(
            conn.execute("SELECT profile_id, user_id, is_sample FROM user_profiles")
        )
        profiles = {
            row[0]: {"user_id": row[1], "is_sample": bool(row[2])} for row in profile_rows
        }
    if ("table", "user_pipeline_items") in objects:
        item_rows = list(
            conn.execute(
                "SELECT pipeline_item_id, profile_id, user_id FROM user_pipeline_items ORDER BY pipeline_item_id"
            )
        )
    if ("table", "user_pipeline_transitions") in objects:
        transition_rows = list(
            conn.execute(
                "SELECT transition_id, pipeline_item_id, profile_id FROM user_pipeline_transitions ORDER BY transition_id"
            )
        )
    if ("table", "applicant_status_updates") in objects:
        applicant_rows = list(
            conn.execute(
                "SELECT update_id, profile_id, user_id, anonymous_user_key, is_sample "
                "FROM applicant_status_updates ORDER BY update_id"
            )
        )

    profile_refs = report_local_references(
        [row[0] for row in profile_rows] + [row[1] for row in item_rows] + [row[1] for row in applicant_rows],
        "profile",
    )
    item_refs = report_local_references(
        [row[0] for row in item_rows] + [row[1] for row in transition_rows], "pipeline-item"
    )
    transition_refs = report_local_references([row[0] for row in transition_rows], "transition")
    applicant_refs = report_local_references([row[0] for row in applicant_rows], "applicant-update")

    items = {}
    for row in item_rows:
        items[row[0]] = {"profile_id": row[1], "user_id": row[2]}
        profile = profiles.get(row[1])
        if profile is None or profile["user_id"] != row[2]:
            checks["pipeline_item_profile_inconsistencies"].append(
                {
                    "pipeline_item_reference": item_refs[str(row[0])],
                    "profile_reference": profile_refs[str(row[1])],
                }
            )
    for row in transition_rows:
        item = items.get(row[1])
        if item is None or item["profile_id"] != row[2]:
            checks["transition_owner_mismatches"].append(
                {
                    "transition_reference": transition_refs[str(row[0])],
                    "pipeline_item_reference": item_refs[str(row[1])],
                }
            )
    for row in applicant_rows:
        profile = profiles.get(row[1])
        mismatch = profile is None
        if profile is not None:
            expected = profile["user_id"]
            mismatch = (row[2] not in (None, "", expected)) or (
                row[3] not in (None, "", expected)
            )
        if mismatch:
            finding = {
                "applicant_update_reference": applicant_refs[str(row[0])],
                "profile_reference": profile_refs[str(row[1])],
            }
            if bool(row[4]) or (profile and profile["is_sample"]):
                informational["sample_applicant_owner_variants"].append(finding)
            else:
                checks["applicant_owner_inconsistencies"].append(finding)


def _available(conn, table, columns, check_name, checks):
    if _table_has_columns(conn, table, columns):
        return True
    checks["check_unavailable"].append(
        {"check": check_name, "reason": "required_table_or_columns_unavailable"}
    )
    return False


def _table_has_columns(conn, table, columns):
    return columns <= ownership_table_columns(conn, table)


def _principal_claim_policy_valid(principal_type, claim_policy):
    if principal_type == "legacy_profile":
        return claim_policy in {"nonclaimable", "manual_approval"}
    if principal_type == "account_native":
        return claim_policy == "account_native"
    if principal_type in {"development", "sample", "system"}:
        return claim_policy == "nonclaimable"
    return False


def _event_transition_valid(event_type, prior_status, resulting_status):
    if event_type == "binding_activated":
        return prior_status is None and resulting_status == "active"
    if event_type == "binding_suspended":
        return prior_status == "active" and resulting_status == "suspended"
    if event_type == "binding_reactivated":
        return prior_status == "suspended" and resulting_status == "active"
    if event_type == "binding_released":
        return prior_status in {"active", "suspended"} and resulting_status == "released"
    if event_type == "administrative_correction":
        return prior_status in BINDING_STATUSES and resulting_status in BINDING_STATUSES
    return False


def _canonical_timestamp(value):
    if type(value) is not str or len(value) != 25 or not value.endswith("+00:00"):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.utcoffset() is not None and parsed.isoformat(timespec="seconds") == value


def _ordered_timestamps(created, updated):
    return _canonical_timestamp(created) and _canonical_timestamp(updated) and _timestamp_not_before(updated, created)


def _timestamp_not_before(value, boundary):
    if not _canonical_timestamp(value) or not _canonical_timestamp(boundary):
        return False
    return datetime.fromisoformat(value) >= datetime.fromisoformat(boundary)


def _count_if_present(conn, objects, table):
    if ("table", table) not in objects:
        return None
    try:
        return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    except Exception:
        return None


def _sorted_groups(groups):
    return {
        name: sorted(
            [_public_safe(row) for row in rows],
            key=lambda row: json.dumps(
                row, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ),
        )
        for name, rows in sorted(groups.items())
    }


def _public_safe(value):
    if type(value) is dict:
        return {str(key): _public_safe(child) for key, child in value.items()}
    if type(value) in {list, tuple}:
        return [_public_safe(child) for child in value]
    if value is None or type(value) in {str, int, float, bool}:
        return value
    return "invalid-value"

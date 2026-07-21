from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from types import MappingProxyType

from wahojobs.accounts import (
    PROVIDERS,
    InvalidAccountInput,
    normalize_email,
    validate_account_metadata,
)


MIGRATION_VERSION = "002_accounts_sessions"
EXPECTED_ACCOUNT_OBJECTS = {
    ("table", "users"),
    ("table", "auth_identities"),
    ("table", "account_invitations"),
    ("table", "account_sessions"),
    ("table", "account_session_rotations"),
    ("table", "consent_events"),
    ("table", "account_lifecycle_events"),
    ("table", "account_deletion_requests"),
    ("index", "idx_auth_identities_user"),
    ("index", "idx_account_invitations_status"),
    ("index", "idx_account_sessions_user_active"),
    ("index", "idx_account_session_rotations_user_time"),
    ("index", "idx_consent_events_user_purpose"),
    ("index", "idx_account_lifecycle_events_user_version"),
    ("index", "idx_account_deletion_requests_user"),
    ("index", "idx_account_deletion_requests_one_open"),
    ("trigger", "trg_auth_identities_immutable_identity"),
    ("trigger", "trg_users_created_at_immutable"),
    ("trigger", "trg_account_sessions_user_time_guard"),
    ("trigger", "trg_account_sessions_core_immutable"),
    ("trigger", "trg_account_sessions_rotation_state_guard"),
    ("trigger", "trg_account_session_rotations_insert_guard"),
    ("trigger", "trg_account_session_rotations_no_update"),
    ("trigger", "trg_account_session_rotations_no_delete"),
    ("trigger", "trg_account_invitations_consumption_time_guard"),
    ("trigger", "trg_consent_events_user_time_guard"),
    ("trigger", "trg_account_lifecycle_events_user_time_guard"),
    ("trigger", "trg_account_deletion_requests_user_time_guard"),
    ("trigger", "trg_consent_events_contiguous"),
    ("trigger", "trg_account_lifecycle_events_contiguous"),
    ("trigger", "trg_consent_events_no_update"),
    ("trigger", "trg_consent_events_no_delete"),
    ("trigger", "trg_account_lifecycle_events_no_update"),
    ("trigger", "trg_account_lifecycle_events_no_delete"),
}
ACCOUNT_SCHEMA_DEFINITION_FINGERPRINTS = MappingProxyType(
    {
        ("index", "idx_account_deletion_requests_one_open"): "f6b4200b3dddd17042a92aaf4b055c1d452756143cb8d14eb5534dd9157e56fe",
        ("index", "idx_account_deletion_requests_user"): "90aaf688b607c8ca37e850eebb8a4da0d31f55e1f7ee937b824e76ffdf514383",
        ("index", "idx_account_invitations_status"): "7887b2c4a171206d0936adec3a9d091f8dc0d9c1ce40fac5a8c68f69cd5031cd",
        ("index", "idx_account_lifecycle_events_user_version"): "5530e2fcc6b52ebca9ad7108d8782a62329c0060c9ae1b06b3f58fc3743e0590",
        ("index", "idx_account_session_rotations_user_time"): "8d923b9da3ca0c52fdfda469195bf8e7d3a5846e82a379987c71108e6fa1c161",
        ("index", "idx_account_sessions_user_active"): "985b2af3e9ea6eb0595b7af7f3e0e6c5e4069c8b1627d4dbb9da7900a08a29d1",
        ("index", "idx_auth_identities_user"): "23e442e734a88711de29574cff2537a034006ea6e3b6dfbe6077a05e4f41be6f",
        ("index", "idx_consent_events_user_purpose"): "25ed85e8483ca3a25fc4592872e7a1bc03909113e6d8e918a1050351df5fdf9c",
        ("table", "account_deletion_requests"): "f06293c951b6b15bb4f8474734158ea76c6c9897ecae60ceca2f12c3a1b6d168",
        ("table", "account_invitations"): "ba3adbe05c539a16b8caf23815fc029e73fbde5b9de6d2790d74725ae446ddad",
        ("table", "account_lifecycle_events"): "e72dad9b91772d384f1980387f585157a4b4f794d88316d3a1fc385f46229078",
        ("table", "account_session_rotations"): "2baf69f46e9881cc93adc832327585505a372f5688df49b5db54296981489590",
        ("table", "account_sessions"): "ca311df4aaeab32f70175e568e64cc7041e8be3b1579974e762fc7e774c7b5ff",
        ("table", "auth_identities"): "9a911e76dd24a04018e6bb502ea63e4e284e219650eb13d631488df6b216e7c1",
        ("table", "consent_events"): "19f08ba24e053ca21bc92ef03e337c83870c66ebda8441e8e092192958b16ee6",
        ("table", "users"): "d4a3b728de53816c32a71499a9f3318a2dc32a5affc0cfb7196537ea40884352",
        ("trigger", "trg_account_deletion_requests_user_time_guard"): "e15dbcde8085f007f5c8e2316f6c756b64e2cd866f8b2b521d923750802c1f07",
        ("trigger", "trg_account_invitations_consumption_time_guard"): "925532d5ddd4b9da6bc6ea7cf08ea7f70eb5f464b94245f2b69ff2f9dde5a3cf",
        ("trigger", "trg_account_lifecycle_events_contiguous"): "bd18c8f945852eef948d974ad58c928af23b112432a66ae87c173e376f58d4d0",
        ("trigger", "trg_account_lifecycle_events_no_delete"): "96b736b1cffc21ac68900eb4b8a40092b0fc5eb8cfd6afae4be1329d11538b3d",
        ("trigger", "trg_account_lifecycle_events_no_update"): "b9f2f08e5565930d1588a9ef08277b445c595a600bc5d70e5e20976d9c4c5d13",
        ("trigger", "trg_account_lifecycle_events_user_time_guard"): "1f770e6bf441a1d21c0607839b7d09ab64700dfa0977898f217200c3e5ecf9b7",
        ("trigger", "trg_account_session_rotations_insert_guard"): "b085931099c519aa25526139b9b57f5f287f6149458cf659c5cf4bdd7b9b0f3e",
        ("trigger", "trg_account_session_rotations_no_delete"): "9b09646130a11ff30270037f720d4a8d70ae3b4cfc5737cf8df4307af69bd296",
        ("trigger", "trg_account_session_rotations_no_update"): "35423d024678c50d8d283560e235eec19d00ed26c6c7fa6ad5e2c7419a75bbe8",
        ("trigger", "trg_account_sessions_core_immutable"): "7595685c75973989285855ee512c50d2a7b6b4bb4769f2cb72cb702c84bcdeff",
        ("trigger", "trg_account_sessions_rotation_state_guard"): "2d461d4f2fb33880d4f7f6ac12437538a30f04f9684caa4e43a0026b89391334",
        ("trigger", "trg_account_sessions_user_time_guard"): "041e4cdc8fec1b2c7e38176e2603ce043c2d8c3000df5eec17a6c98d95a22cda",
        ("trigger", "trg_auth_identities_immutable_identity"): "c40b22d128f82a72e5d96dfdfeb6ee0f4fa300106cdb6b00c1a101f294279e5e",
        ("trigger", "trg_consent_events_contiguous"): "86389491a8f47c1fd13f4e8f967a1f058a46d926cc8f3342887869707d676d0f",
        ("trigger", "trg_consent_events_no_delete"): "a13379106e2ab0e2aef6d3f414ec3020a4dc80269f1cbf69f54c9d0dd2302779",
        ("trigger", "trg_consent_events_no_update"): "0a80414ca438422b015c4494105745395c4587c9333309c68cc51f329e8b6434",
        ("trigger", "trg_consent_events_user_time_guard"): "2df5f3f11a23328bfd02f84320add1a5c30088d10e4fe8ecabf5587ceef9c68e",
        ("trigger", "trg_users_created_at_immutable"): "f5dcd1b04b48a02352a755086624e9fb109c1b7afc1f7dadaa02e3bab482a91e",
    }
)


def expected_account_schema_fingerprints():
    """Return the immutable committed Migration-002 object fingerprints."""
    return ACCOUNT_SCHEMA_DEFINITION_FINGERPRINTS
APPEND_ONLY_TRIGGERS = {
    "trg_consent_events_no_update",
    "trg_consent_events_no_delete",
    "trg_account_lifecycle_events_no_update",
    "trg_account_lifecycle_events_no_delete",
    "trg_account_session_rotations_no_update",
    "trg_account_session_rotations_no_delete",
}
HEX_64 = re.compile(r"^[0-9a-f]{64}$")
AUTH_IDENTITY_ID = re.compile(r"^auth_[0-9a-f]{32}$")
ACCOUNT_ID = re.compile(r"^usr_[0-9a-f]{32}$")
TIMESTAMP_FIELDS = {
    "users": (
        "created_at",
        "updated_at",
        "deletion_requested_at",
        "deactivated_at",
    ),
    "auth_identities": ("created_at", "last_authenticated_at", "disabled_at"),
    "account_invitations": ("created_at", "expires_at", "consumed_at", "revoked_at"),
    "account_sessions": (
        "created_at",
        "last_seen_at",
        "idle_expires_at",
        "absolute_expires_at",
        "rotated_at",
        "revoked_at",
    ),
    "account_session_rotations": ("rotated_at", "created_at"),
    "consent_events": ("occurred_at",),
    "account_lifecycle_events": ("occurred_at",),
    "account_deletion_requests": (
        "requested_at",
        "cooling_period_ends_at",
        "purge_eligible_at",
        "cancelled_at",
        "deactivated_at",
    ),
}


def reconcile_accounts(conn, *, now: datetime | None = None) -> dict:
    now = _require_aware(now or datetime.now(timezone.utc))
    objects = {
        (row["type"], row["name"])
        for row in conn.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE type IN ('table', 'index', 'trigger')"
        )
    }
    missing = sorted(f"{kind}:{name}" for kind, name in EXPECTED_ACCOUNT_OBJECTS - objects)
    marker_present = False
    if _table_exists(objects, "wahojobs_schema_migrations"):
        marker_present = (
            conn.execute(
                "SELECT 1 FROM wahojobs_schema_migrations WHERE version = ?",
                (MIGRATION_VERSION,),
            ).fetchone()
            is not None
        )

    checks = _empty_checks()
    counts = {
        "users": None,
        "auth_identities": None,
        "invitations": None,
        "sessions": None,
        "session_rotations": None,
        "consent_events": None,
        "lifecycle_events": None,
        "deletion_requests": None,
        "users_active": None,
        "users_suspended": None,
        "users_deletion_requested": None,
        "users_deactivated_pending_purge": None,
    }
    schema_complete = not missing and marker_present
    if not schema_complete:
        if not marker_present:
            checks["migration_marker_missing"].append({"version": MIGRATION_VERSION})
        for item in missing:
            checks["required_objects_missing"].append({"object": item})
        return _finish(counts, checks, marker_present, missing)

    counts.update(
        {
            "users": _count(conn, "users"),
            "auth_identities": _count(conn, "auth_identities"),
            "invitations": _count(conn, "account_invitations"),
            "sessions": _count(conn, "account_sessions"),
            "session_rotations": _count(conn, "account_session_rotations"),
            "consent_events": _count(conn, "consent_events"),
            "lifecycle_events": _count(conn, "account_lifecycle_events"),
            "deletion_requests": _count(conn, "account_deletion_requests"),
        }
    )
    for status in ("active", "suspended", "deletion_requested", "deactivated_pending_purge"):
        counts[f"users_{status}"] = conn.execute(
            "SELECT COUNT(*) FROM users WHERE lifecycle_status = ?", (status,)
        ).fetchone()[0]

    _check_orphans(conn, checks)
    _check_duplicate_identities(conn, checks)
    _check_auth_identity_rows(conn, checks)
    _check_invitation_hashes(conn, checks)
    _check_session_hashes(conn, checks)
    _check_session_state(conn, checks, now)
    _check_rotation_lineage(conn, checks)
    _check_deletion_state(conn, checks)
    _check_lifecycle(conn, checks)
    _check_consent(conn, checks)
    _check_timestamps(conn, checks)
    _check_user_creation_boundaries(conn, checks)
    _check_privacy(conn, checks)
    missing_triggers = APPEND_ONLY_TRIGGERS - {name for kind, name in objects if kind == "trigger"}
    for name in sorted(missing_triggers):
        checks["append_only_triggers_missing"].append({"trigger": name})
    for row in conn.execute("PRAGMA foreign_key_check"):
        checks["foreign_key_violations"].append(
            {"table": row[0], "rowid": row[1], "parent": row[2], "fk_index": row[3]}
        )
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        checks["integrity_errors"].append({"result": integrity})
    return _finish(counts, checks, marker_present, missing)


def _empty_checks() -> dict[str, list[dict]]:
    names = (
        "migration_marker_missing",
        "required_objects_missing",
        "orphan_auth_identities",
        "malformed_auth_identities",
        "orphan_sessions",
        "orphan_invitations",
        "duplicate_provider_subjects",
        "invalid_invitation_hashes",
        "invalid_session_hashes",
        "duplicate_csrf_hash",
        "missing_csrf_hash",
        "malformed_csrf_hash",
        "csrf_session_binding_mismatch",
        "active_sessions_expired",
        "invalid_session_temporal_order",
        "session_valid_before_creation",
        "active_sessions_for_inactive_users",
        "session_rotation_cross_user",
        "session_rotation_self_reference",
        "session_rotation_fork",
        "session_rotation_reverse_fork",
        "session_rotation_cycle",
        "session_rotation_missing_predecessor",
        "session_rotation_missing_replacement",
        "session_rotation_temporal_mismatch",
        "predecessor_not_revoked",
        "active_predecessor_with_active_replacement",
        "consent_event_predates_user",
        "lifecycle_event_predates_user",
        "deletion_request_predates_user",
        "session_predates_user",
        "invitation_consumption_predates_user",
        "lifecycle_projection_predates_user",
        "deletion_state_without_request",
        "deletion_request_lifecycle_mismatch",
        "misleading_purge_state",
        "lifecycle_projection_mismatch",
        "account_version_gaps",
        "invalid_consent_chains",
        "append_only_triggers_missing",
        "invalid_timestamps",
        "privacy_sensitive_fields",
        "foreign_key_violations",
        "integrity_errors",
    )
    return {name: [] for name in names}


def _finish(counts, checks, marker_present, missing):
    checks = {name: sorted(rows, key=_stable_row_key) for name, rows in sorted(checks.items())}
    blocking_reasons = [name for name, rows in checks.items() if rows]
    return {
        "schema": {
            "migration_version": MIGRATION_VERSION,
            "migration_marker_present": marker_present,
            "required_objects_missing": missing,
        },
        "counts": counts,
        "checks": checks,
        "blocking_reasons": blocking_reasons,
        "blocking": bool(blocking_reasons),
        "fully_reconciled": not blocking_reasons,
        "read_only": True,
    }


def _stable_row_key(row):
    return json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _table_exists(objects, name):
    return ("table", name) in objects


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _check_orphans(conn, checks):
    queries = {
        "orphan_auth_identities": (
            "SELECT auth_identity_id AS id FROM auth_identities a "
            "LEFT JOIN users u ON u.user_id = a.user_id WHERE u.user_id IS NULL"
        ),
        "orphan_sessions": (
            "SELECT session_id AS id FROM account_sessions s "
            "LEFT JOIN users u ON u.user_id = s.user_id WHERE u.user_id IS NULL"
        ),
        "orphan_invitations": (
            "SELECT invitation_id AS id FROM account_invitations i "
            "LEFT JOIN users u ON u.user_id = i.consumed_by_user_id "
            "WHERE i.consumed_by_user_id IS NOT NULL AND u.user_id IS NULL"
        ),
    }
    for name, query in queries.items():
        checks[name].extend(dict(row) for row in conn.execute(query))


def _check_duplicate_identities(conn, checks):
    rows = conn.execute(
        "SELECT provider, provider_subject, COUNT(*) AS count "
        "FROM auth_identities GROUP BY provider, provider_subject HAVING COUNT(*) > 1"
    )
    checks["duplicate_provider_subjects"].extend(
        {"provider": row["provider"], "count": row["count"]} for row in rows
    )


def authoritative_auth_identity_row_valid(
    row,
    *,
    expected_user_id=None,
    account_created_at=None,
) -> bool:
    """Validate one durable identity row using the Migration-002 contract."""
    try:
        identity_id = row["auth_identity_id"]
        user_id = row["user_id"]
        provider = row["provider"]
        provider_subject = row["provider_subject"]
        verified_email = row["verified_email"]
        email_verified = row["email_verified"]
        created_at = _parse_timestamp(row["created_at"])
        last_authenticated_at = _parse_timestamp(row["last_authenticated_at"])
        disabled_at = (
            _parse_timestamp(row["disabled_at"])
            if row["disabled_at"] is not None
            else None
        )
        link_idempotency_key = row["link_idempotency_key"]
        request_fingerprint = row["request_fingerprint"]
    except (KeyError, IndexError, TypeError):
        return False

    if (
        type(identity_id) is not str
        or AUTH_IDENTITY_ID.fullmatch(identity_id) is None
        or type(user_id) is not str
        or ACCOUNT_ID.fullmatch(user_id) is None
        or (expected_user_id is not None and user_id != expected_user_id)
        or type(provider) is not str
        or provider not in PROVIDERS
        or type(provider_subject) is not str
        or provider_subject != provider_subject.strip()
        or not (1 <= len(provider_subject) <= 1024)
        or any(ord(char) < 32 for char in provider_subject)
        or type(email_verified) is not int
        or email_verified not in {0, 1}
        or created_at is None
        or last_authenticated_at is None
        or (row["disabled_at"] is not None and disabled_at is None)
        or (disabled_at is not None and disabled_at < created_at)
        or type(link_idempotency_key) is not str
        or link_idempotency_key != link_idempotency_key.strip()
        or not (8 <= len(link_idempotency_key) <= 256)
        or any(ord(char) < 32 for char in link_idempotency_key)
        or type(request_fingerprint) is not str
        or HEX_64.fullmatch(request_fingerprint) is None
    ):
        return False
    if account_created_at is not None:
        account_created = _parse_timestamp(account_created_at)
        if account_created is None or created_at < account_created:
            return False
    if email_verified and verified_email is None:
        return False
    if verified_email is not None:
        if type(verified_email) is not str:
            return False
        try:
            if normalize_email(verified_email) != verified_email:
                return False
        except InvalidAccountInput:
            return False
    return True


def _check_auth_identity_rows(conn, checks):
    users = {
        row["user_id"]: row["created_at"]
        for row in conn.execute("SELECT user_id, created_at FROM users ORDER BY user_id")
    }
    for row in conn.execute(
        "SELECT rowid AS _rowid, auth_identity_id, user_id, provider, "
        "provider_subject, verified_email, email_verified, created_at, "
        "last_authenticated_at, disabled_at, link_idempotency_key, "
        "request_fingerprint FROM auth_identities ORDER BY rowid"
    ):
        if not authoritative_auth_identity_row_valid(
            row,
            expected_user_id=row["user_id"],
            account_created_at=users.get(row["user_id"]),
        ):
            checks["malformed_auth_identities"].append(
                {"rowid": row["_rowid"], "reason": "invalid_identity_row"}
            )


def _check_invitation_hashes(conn, checks):
    for row in conn.execute(
        "SELECT invitation_id, invited_email_hmac, invitation_secret_hmac, hash_version, request_fingerprint "
        "FROM account_invitations ORDER BY invitation_id"
    ):
        reasons = []
        if row["hash_version"] != "hmac_sha256_v1":
            reasons.append("invalid_hash_version")
        for field in ("invited_email_hmac", "invitation_secret_hmac", "request_fingerprint"):
            if not HEX_64.fullmatch(row[field] or ""):
                reasons.append(f"invalid_{field}")
        if reasons:
            checks["invalid_invitation_hashes"].append(
                {"invitation_id": row["invitation_id"], "reasons": reasons}
            )


def _check_session_hashes(conn, checks):
    for row in conn.execute(
        "SELECT session_id, token_hash, token_hash_version, csrf_secret_hash, csrf_hash_version "
        "FROM account_sessions ORDER BY session_id"
    ):
        reasons = []
        if row["token_hash_version"] != "sha256_v1" or not HEX_64.fullmatch(row["token_hash"] or ""):
            reasons.append("invalid_token_hash")
        if row["csrf_secret_hash"] is None or row["csrf_secret_hash"] == "":
            checks["missing_csrf_hash"].append({"session_id": row["session_id"]})
        elif row["csrf_hash_version"] != "sha256_v1" or not HEX_64.fullmatch(row["csrf_secret_hash"]):
            reasons.append("invalid_csrf_hash")
            checks["malformed_csrf_hash"].append({"session_id": row["session_id"]})
        if reasons:
            checks["invalid_session_hashes"].append(
                {"session_id": row["session_id"], "reasons": reasons}
            )
    for row in conn.execute(
        "SELECT csrf_secret_hash, COUNT(*) AS count, COUNT(DISTINCT user_id) AS users "
        "FROM account_sessions GROUP BY csrf_secret_hash HAVING COUNT(*) > 1"
    ):
        checks["duplicate_csrf_hash"].append({"count": row["count"]})
        checks["csrf_session_binding_mismatch"].append(
            {"session_count": row["count"], "user_count": row["users"]}
        )


def _check_session_state(conn, checks, now):
    for row in conn.execute(
        "SELECT s.session_id, s.created_at, s.last_seen_at, s.idle_expires_at, "
        "s.absolute_expires_at, s.rotated_at, s.revoked_at, "
        "u.lifecycle_status FROM account_sessions s JOIN users u ON u.user_id = s.user_id "
        "ORDER BY s.session_id"
    ):
        created = _parse_timestamp(row["created_at"])
        last_seen = _parse_timestamp(row["last_seen_at"])
        idle = _parse_timestamp(row["idle_expires_at"])
        absolute = _parse_timestamp(row["absolute_expires_at"])
        rotated = _parse_timestamp(row["rotated_at"]) if row["rotated_at"] else None
        revoked = _parse_timestamp(row["revoked_at"]) if row["revoked_at"] else None
        invalid = (
            None in {created, last_seen, idle, absolute}
            or (created is not None and last_seen is not None and last_seen < created)
            or (created is not None and idle is not None and idle <= created)
            or (created is not None and absolute is not None and absolute <= created)
            or (idle is not None and absolute is not None and idle > absolute)
            or (rotated is not None and created is not None and rotated < created)
            or (revoked is not None and created is not None and revoked < created)
        )
        if invalid:
            checks["invalid_session_temporal_order"].append({"session_id": row["session_id"]})
        active = row["revoked_at"] is None and row["rotated_at"] is None
        if active and created is not None and now < created:
            checks["session_valid_before_creation"].append({"session_id": row["session_id"]})
        if active and ((idle is not None and idle <= now) or (absolute is not None and absolute <= now)):
            checks["active_sessions_expired"].append({"session_id": row["session_id"]})
        if active and row["lifecycle_status"] != "active":
            checks["active_sessions_for_inactive_users"].append(
                {"session_id": row["session_id"], "lifecycle_status": row["lifecycle_status"]}
            )


def _check_rotation_lineage(conn, checks):
    sessions = [
        dict(row)
        for row in conn.execute(
            "SELECT session_id, user_id, created_at, absolute_expires_at, rotated_at, "
            "revoked_at, revoke_reason FROM account_sessions ORDER BY session_id"
        )
    ]
    edges = [
        dict(row)
        for row in conn.execute(
            "SELECT rotation_id, user_id, predecessor_session_id, replacement_session_id, "
            "rotated_at, created_at FROM account_session_rotations ORDER BY rotation_id"
        )
    ]
    by_id = {row["session_id"]: row for row in sessions}
    by_predecessor: dict[str, list[dict]] = {}
    by_replacement: dict[str, list[dict]] = {}
    for edge in edges:
        edge_id = edge["rotation_id"]
        predecessor_id = edge["predecessor_session_id"]
        replacement_id = edge["replacement_session_id"]
        by_predecessor.setdefault(predecessor_id, []).append(edge)
        by_replacement.setdefault(replacement_id, []).append(edge)
        predecessor = by_id.get(predecessor_id)
        replacement = by_id.get(replacement_id)
        if predecessor_id == replacement_id:
            checks["session_rotation_self_reference"].append({"rotation_id": edge_id})
        if predecessor is None:
            checks["session_rotation_missing_predecessor"].append({"rotation_id": edge_id})
        if replacement is None:
            checks["session_rotation_missing_replacement"].append({"rotation_id": edge_id})
        if (
            predecessor is not None
            and replacement is not None
            and (
                predecessor["user_id"] != edge["user_id"]
                or replacement["user_id"] != edge["user_id"]
                or predecessor["user_id"] != replacement["user_id"]
            )
        ):
            checks["session_rotation_cross_user"].append({"rotation_id": edge_id})
        if predecessor is None or replacement is None:
            continue
        predecessor_created = _parse_timestamp(predecessor["created_at"])
        replacement_created = _parse_timestamp(replacement["created_at"])
        rotated = _parse_timestamp(edge["rotated_at"])
        edge_created = _parse_timestamp(edge["created_at"])
        if (
            None in {predecessor_created, replacement_created, rotated, edge_created}
            or replacement_created < predecessor_created
            or rotated < predecessor_created
            or rotated < replacement_created
            or edge_created < rotated
        ):
            checks["session_rotation_temporal_mismatch"].append({"rotation_id": edge_id})
        if (
            predecessor["rotated_at"] != edge["rotated_at"]
            or predecessor["revoked_at"] != edge["rotated_at"]
            or predecessor["revoke_reason"] != "session_rotated"
        ):
            checks["predecessor_not_revoked"].append({"rotation_id": edge_id})
        predecessor_active = predecessor["revoked_at"] is None and predecessor["rotated_at"] is None
        replacement_active = replacement["revoked_at"] is None and replacement["rotated_at"] is None
        if predecessor_active and replacement_active:
            checks["active_predecessor_with_active_replacement"].append(
                {"rotation_id": edge_id}
            )

    for predecessor_id, matching in sorted(by_predecessor.items()):
        if len(matching) > 1:
            checks["session_rotation_fork"].append(
                {"session_id": predecessor_id, "edge_count": len(matching)}
            )
    for replacement_id, matching in sorted(by_replacement.items()):
        if len(matching) > 1:
            checks["session_rotation_reverse_fork"].append(
                {"session_id": replacement_id, "edge_count": len(matching)}
            )
    for session in sessions:
        if session["rotated_at"] is not None and session["session_id"] not in by_predecessor:
            checks["session_rotation_missing_replacement"].append(
                {"session_id": session["session_id"]}
            )

    next_sessions = {
        predecessor: sorted(
            {edge["replacement_session_id"] for edge in matching}
        )
        for predecessor, matching in by_predecessor.items()
    }
    reported_cycles = set()
    colors: dict[str, int] = {}
    for start in sorted(set(next_sessions) | set(by_replacement)):
        if colors.get(start, 0) != 0:
            continue
        path = [start]
        positions = {start: 0}
        colors[start] = 1
        stack = [(start, iter(next_sessions.get(start, ())))]
        while stack:
            current, successors = stack[-1]
            try:
                successor = next(successors)
            except StopIteration:
                colors[current] = 2
                stack.pop()
                positions.pop(current, None)
                path.pop()
                continue
            successor_color = colors.get(successor, 0)
            if successor_color == 0:
                colors[successor] = 1
                positions[successor] = len(path)
                path.append(successor)
                stack.append((successor, iter(next_sessions.get(successor, ()))))
            elif successor_color == 1:
                cycle = tuple(sorted(path[positions[successor] :]))
                if cycle not in reported_cycles:
                    reported_cycles.add(cycle)
                    checks["session_rotation_cycle"].append({"session_id": min(cycle)})


def _check_deletion_state(conn, checks):
    deletion_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(account_deletion_requests)")
    }
    for unsupported in sorted({"completed_at", "purged_at"} & deletion_columns):
        checks["misleading_purge_state"].append(
            {"field": unsupported, "reason": "unsupported_erasure_claim"}
        )
    for row in conn.execute(
        "SELECT u.user_id FROM users u LEFT JOIN account_deletion_requests d "
        "ON d.user_id = u.user_id AND d.status = 'pending_cooling' "
        "WHERE u.lifecycle_status = 'deletion_requested' "
        "GROUP BY u.user_id HAVING COUNT(d.deletion_request_id) <> 1"
    ):
        checks["deletion_state_without_request"].append({"user_id": row["user_id"]})
    for row in conn.execute(
        "SELECT u.user_id FROM users u LEFT JOIN account_deletion_requests d "
        "ON d.user_id = u.user_id AND d.status = 'deactivated_pending_purge' "
        "WHERE u.lifecycle_status = 'deactivated_pending_purge' "
        "GROUP BY u.user_id HAVING COUNT(d.deletion_request_id) <> 1"
    ):
        checks["deletion_state_without_request"].append({"user_id": row["user_id"]})
    for row in conn.execute(
        "SELECT d.deletion_request_id, u.lifecycle_status "
        "FROM account_deletion_requests d JOIN users u ON u.user_id = d.user_id "
        "WHERE (d.status = 'pending_cooling' AND u.lifecycle_status <> 'deletion_requested') "
        "OR (d.status = 'deactivated_pending_purge' AND u.lifecycle_status <> 'deactivated_pending_purge')"
    ):
        checks["deletion_request_lifecycle_mismatch"].append(dict(row))
    for row in conn.execute(
        "SELECT deletion_request_id, status FROM account_deletion_requests "
        "WHERE lower(status) LIKE '%purged%' OR lower(status) LIKE '%completed%'"
    ):
        checks["misleading_purge_state"].append(dict(row))


def _check_lifecycle(conn, checks):
    expected_status = {
        "account_created": "active",
        "account_suspended": "suspended",
        "account_reactivated": "active",
        "deletion_requested": "deletion_requested",
        "deletion_cancelled": None,
        "account_deactivated_pending_purge": "deactivated_pending_purge",
    }
    for user in conn.execute("SELECT user_id, lifecycle_status, row_version FROM users ORDER BY user_id"):
        events = conn.execute(
            "SELECT lifecycle_event_id, event_type, occurred_at, account_version_before, account_version_after, metadata_json "
            "FROM account_lifecycle_events WHERE user_id = ? "
            "ORDER BY account_version_after, lifecycle_event_id",
            (user["user_id"],),
        ).fetchall()
        previous = 0
        previous_time = None
        gap = False
        for event in events:
            occurred = _parse_timestamp(event["occurred_at"])
            if event["account_version_before"] != previous or event["account_version_after"] != previous + 1:
                gap = True
            if occurred is None or (previous_time is not None and occurred < previous_time):
                gap = True
            previous = event["account_version_after"]
            previous_time = occurred
        if not events or gap or previous != user["row_version"]:
            checks["account_version_gaps"].append({"user_id": user["user_id"]})
            continue
        latest = events[-1]
        target = expected_status[latest["event_type"]]
        if latest["event_type"] == "deletion_cancelled":
            try:
                target = json.loads(latest["metadata_json"]).get("restored_status")
            except (TypeError, json.JSONDecodeError):
                target = None
        if target != user["lifecycle_status"]:
            checks["lifecycle_projection_mismatch"].append({"user_id": user["user_id"]})


def _check_consent(conn, checks):
    groups = conn.execute(
        "SELECT DISTINCT user_id, purpose FROM consent_events ORDER BY user_id, purpose"
    ).fetchall()
    for group in groups:
        events = [
            dict(row)
            for row in conn.execute(
                "SELECT action, occurred_at, consent_version_before, consent_version_after "
                "FROM consent_events WHERE user_id = ? AND purpose = ? "
                "ORDER BY consent_version_after",
                (group["user_id"], group["purpose"]),
            )
        ]
        actions = [row["action"] for row in events]
        invalid = bool(actions and actions[0] != "granted")
        previous_version = 0
        previous_time = None
        for event in events:
            occurred = _parse_timestamp(event["occurred_at"])
            if (
                event["consent_version_before"] != previous_version
                or event["consent_version_after"] != previous_version + 1
                or (previous_time is not None and (occurred is None or occurred < previous_time))
            ):
                invalid = True
            previous_version = event["consent_version_after"]
            previous_time = occurred
        invalid = invalid or any(left == right for left, right in zip(actions, actions[1:]))
        if invalid:
            checks["invalid_consent_chains"].append(
                {"user_id": group["user_id"], "purpose": group["purpose"]}
            )


def _check_timestamps(conn, checks):
    for table, fields in TIMESTAMP_FIELDS.items():
        columns = ", ".join(["rowid AS _rowid", *fields])
        for row in conn.execute(f"SELECT {columns} FROM {table} ORDER BY rowid"):
            for field in fields:
                value = row[field]
                if value is not None and _parse_timestamp(value) is None:
                    checks["invalid_timestamps"].append(
                        {"table": table, "rowid": row["_rowid"], "field": field}
                    )


def _check_user_creation_boundaries(conn, checks):
    users = {
        row["user_id"]: _parse_timestamp(row["created_at"])
        for row in conn.execute("SELECT user_id, created_at FROM users ORDER BY user_id")
    }

    relationships = (
        (
            "consent_events",
            "consent_event_id",
            "occurred_at",
            "consent_event_predates_user",
        ),
        (
            "account_lifecycle_events",
            "lifecycle_event_id",
            "occurred_at",
            "lifecycle_event_predates_user",
        ),
        (
            "account_deletion_requests",
            "deletion_request_id",
            "requested_at",
            "deletion_request_predates_user",
        ),
        (
            "account_sessions",
            "session_id",
            "created_at",
            "session_predates_user",
        ),
    )
    for table, id_field, time_field, reason in relationships:
        for row in conn.execute(
            f"SELECT {id_field}, user_id, {time_field} FROM {table} ORDER BY {id_field}"
        ):
            owner_created = users.get(row["user_id"])
            occurred = _parse_timestamp(row[time_field])
            if owner_created is not None and occurred is not None and occurred < owner_created:
                checks[reason].append({id_field: row[id_field], "user_id": row["user_id"]})

    for row in conn.execute(
        "SELECT invitation_id, consumed_by_user_id, consumed_at FROM account_invitations "
        "WHERE consumed_by_user_id IS NOT NULL ORDER BY invitation_id"
    ):
        owner_created = users.get(row["consumed_by_user_id"])
        consumed = _parse_timestamp(row["consumed_at"])
        if owner_created is not None and consumed is not None and consumed < owner_created:
            checks["invitation_consumption_predates_user"].append(
                {"invitation_id": row["invitation_id"], "user_id": row["consumed_by_user_id"]}
            )

    for row in conn.execute(
        "SELECT user_id, created_at, updated_at, deletion_requested_at, deactivated_at "
        "FROM users ORDER BY user_id"
    ):
        created = _parse_timestamp(row["created_at"])
        invalid_fields = []
        for field in ("updated_at", "deletion_requested_at", "deactivated_at"):
            value = _parse_timestamp(row[field]) if row[field] is not None else None
            if created is not None and value is not None and value < created:
                invalid_fields.append(field)
        if invalid_fields:
            checks["lifecycle_projection_predates_user"].append(
                {"user_id": row["user_id"], "fields": invalid_fields}
            )


def _check_privacy(conn, checks):
    forbidden_columns = {"raw_token", "session_token", "invitation_secret", "provider_token", "oauth_claims"}
    for table in TIMESTAMP_FIELDS:
        for row in conn.execute(f"PRAGMA table_info({table})"):
            if row["name"].lower() in forbidden_columns:
                checks["privacy_sensitive_fields"].append(
                    {"table": table, "field": row["name"], "reason": "forbidden_schema_field"}
                )
    metadata_tables = {
        "account_invitations": ("invitation_id", "source_metadata_json"),
        "consent_events": ("consent_event_id", "metadata_json"),
        "account_lifecycle_events": ("lifecycle_event_id", "metadata_json"),
        "account_deletion_requests": ("deletion_request_id", "deactivation_evidence_json"),
    }
    for table, (id_field, metadata_field) in metadata_tables.items():
        for row in conn.execute(f"SELECT {id_field}, {metadata_field} FROM {table}"):
            try:
                metadata = json.loads(row[metadata_field])
            except (TypeError, json.JSONDecodeError):
                checks["privacy_sensitive_fields"].append(
                    {"table": table, "id": row[id_field], "reason": "invalid_metadata_json"}
                )
                continue
            try:
                validated = validate_account_metadata(metadata)
            except (InvalidAccountInput, RecursionError):
                checks["privacy_sensitive_fields"].append(
                    {"table": table, "id": row[id_field], "reason": "unsafe_metadata"}
                )
                continue
            canonical = json.dumps(
                validated, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            )
            if canonical != row[metadata_field]:
                checks["privacy_sensitive_fields"].append(
                    {"table": table, "id": row[id_field], "reason": "noncanonical_metadata"}
                )


def _parse_timestamp(value):
    if type(value) is not str or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    canonical = parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    if parsed.microsecond != 0 or value != canonical:
        return None
    return parsed.astimezone(timezone.utc)


def _require_aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TypeError("now must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)

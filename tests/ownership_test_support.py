import hashlib
import json
import sqlite3
import sys
from pathlib import Path

from tests.accounts_test_support import NOW, create_user, install_accounts


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import ownership_migration  # noqa: E402
from wahojobs.ownership import event_request_fingerprint  # noqa: E402


EMPTY_JSON = "{}"
EMPTY_HASH = hashlib.sha256(EMPTY_JSON.encode("utf-8")).hexdigest()
NOW_TEXT = NOW.isoformat(timespec="seconds")


def install_ownership(path):
    conn = install_accounts(path)
    ownership_migration.apply_ownership_migration(conn)
    return conn


def add_active_user(conn, suffix="owner"):
    _, created = create_user(conn, suffix)
    return created.user.user_id


def add_principal(
    conn,
    *,
    suffix="1",
    environment="test",
    principal_type="legacy_profile",
    status="active",
    claim_policy="manual_approval",
    exclusive=1,
):
    principal_id = f"prn_{int(suffix):032x}"
    conn.execute(
        "INSERT INTO product_principals "
        "(principal_id, environment_namespace, principal_type, lifecycle_status, "
        "claim_policy, exclusive_account_binding, version, created_at, updated_at, "
        "provenance_json) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
        (
            principal_id,
            environment,
            principal_type,
            status,
            claim_policy,
            exclusive,
            NOW_TEXT,
            NOW_TEXT,
            EMPTY_JSON,
        ),
    )
    return principal_id


def add_alias(
    conn,
    principal_id,
    *,
    suffix="1",
    environment="test",
    kind="profile_id",
    value="profile-one",
    claimability="manual_approval",
):
    alias_id = f"loa_{int(suffix):032x}"
    conn.execute(
        "INSERT INTO legacy_owner_aliases "
        "(alias_id, principal_id, environment_namespace, alias_kind, alias_value, "
        "claimability, discovered_from, created_at, provenance_json) "
        "VALUES (?, ?, ?, ?, ?, ?, 'manual_review', ?, ?)",
        (
            alias_id,
            principal_id,
            environment,
            kind,
            value,
            claimability,
            NOW_TEXT,
            EMPTY_JSON,
        ),
    )
    return alias_id


def add_binding(conn, principal_id, user_id, *, suffix="1", environment="test"):
    binding_id = f"pab_{int(suffix):032x}"
    conn.execute(
        "INSERT INTO principal_account_bindings "
        "(binding_id, principal_id, user_id, environment_namespace, binding_role, "
        "binding_status, version, latest_event_version, created_at, updated_at, "
        "suspended_at, provenance_json) "
        "VALUES (?, ?, ?, ?, 'owner', 'active', 1, 1, ?, ?, NULL, ?)",
        (
            binding_id,
            principal_id,
            user_id,
            environment,
            NOW_TEXT,
            NOW_TEXT,
            EMPTY_JSON,
        ),
    )
    return binding_id


def add_activation_event(
    conn, principal_id, user_id, binding_id, *, suffix="1", environment="test"
):
    event_id = f"obe_{int(suffix):032x}"
    idempotency_key = f"binding-activation-{suffix}"
    fingerprint = event_request_fingerprint(
        principal_id=principal_id,
        binding_id=binding_id,
        user_id=user_id,
        expected_event_version=1,
        event_type="binding_activated",
        prior_status=None,
        resulting_status="active",
        actor_type="administrator",
        reason_code="manual_approval",
        approval_reference="review-reference",
        occurred_at=NOW_TEXT,
        metadata={},
    )
    conn.execute(
        "INSERT INTO ownership_binding_events "
        "(event_id, principal_id, user_id, binding_id, environment_namespace, "
        "event_version, event_type, prior_status, resulting_status, actor_type, "
        "reason_code, approval_reference, idempotency_key, request_fingerprint, "
        "occurred_at, metadata_json) "
        "VALUES (?, ?, ?, ?, ?, 1, 'binding_activated', NULL, 'active', "
        "'administrator', 'manual_approval', 'review-reference', ?, ?, ?, ?)",
        (
            event_id,
            principal_id,
            user_id,
            binding_id,
            environment,
            idempotency_key,
            fingerprint,
            NOW_TEXT,
            EMPTY_JSON,
        ),
    )
    return event_id


def ownership_object_names(conn):
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE '%ownership%' "
            "OR name LIKE '%principal%' OR name LIKE '%legacy_owner%'"
        )
    }


def database_snapshot(conn):
    result = {}
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    for table in tables:
        rows = [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        result[table] = hashlib.sha256(
            json.dumps(rows, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
    return result

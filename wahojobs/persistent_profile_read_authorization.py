"""Dormant durable authorization for read-only persistent-profile access."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import re
import sqlite3

from wahojobs.account_reconciliation import (
    EXPECTED_ACCOUNT_OBJECTS,
    MIGRATION_VERSION as ACCOUNTS_MIGRATION_VERSION,
    attest_account_schema,
    authoritative_auth_identity_row_valid,
    expected_account_schema_fingerprints,
)
from wahojobs.accounts import LIFECYCLE_STATUSES
from wahojobs.ownership import (
    ACCOUNT_ID_PATTERN,
    APPROVAL_REFERENCE_PATTERN,
    BINDING_ACTOR_TYPES,
    BINDING_EVENT_TYPES,
    BINDING_ROLES,
    BINDING_STATUSES,
    CLAIM_POLICIES,
    PRINCIPAL_STATUSES,
    PRINCIPAL_TYPES,
    REASON_CODE_PATTERN,
    SHA256_PATTERN,
    validate_binding_id,
    validate_binding_event_id,
    validate_environment_namespace,
    validate_event_request_fingerprint,
    validate_metadata_document,
    validate_principal_id,
    ownership_timestamp_not_before,
)
from wahojobs.ownership_schema import attest_ownership_schema


_OWNERSHIP_MIGRATION_VERSION = "003_product_principals"
_AUTHORIZED_PRINCIPAL_TYPE = "account_native"
_AUTHORIZED_CLAIM_POLICY = "account_native"
_AUTHORIZED_BINDING_ROLE = "owner"
_AUTHORIZED_LIFECYCLE = "active"
_SCOPE = "persistent_profile_read"
_MAX_ACCOUNT_BINDINGS = 64
MAX_AUTHORIZATION_IDENTITIES = 16
MAX_AUTHORIZATION_EVENTS_PER_BINDING = 128


@dataclass(frozen=True, slots=True, repr=False)
class PersistentProfileReadAuthorizationDecision:
    """Sanitized result that retains no account or ownership details."""

    state: str
    _grant: object | None = field(default=None, repr=False)

    def __post_init__(self):
        from wahojobs.persistent_profiles_application import TrustedProfileReadGrant

        if self.state not in {"authorized", "denied", "unavailable"}:
            raise ValueError("invalid_persistent_profile_read_authorization_result")
        if self.state == "authorized":
            if type(self._grant) is not TrustedProfileReadGrant:
                raise ValueError("invalid_persistent_profile_read_authorization_result")
        elif self._grant is not None:
            raise ValueError("invalid_persistent_profile_read_authorization_result")

    def grant_for_application(self):
        return self._grant if self.state == "authorized" else None

    def __repr__(self) -> str:
        return (
            "PersistentProfileReadAuthorizationDecision("
            f"state={self.state!r}, grant=<redacted>)"
        )

    def __reduce_ex__(self, _protocol):
        raise TypeError("authorization_decision_not_serializable")

    def __copy__(self):
        raise TypeError("authorization_decision_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("authorization_decision_not_copyable")


class DurablePersistentProfileReadAuthorizationGateway:
    """Resolve one account-native read principal from attested durable state."""

    __slots__ = ()

    @property
    def scope(self) -> str:
        return _SCOPE

    def authorize_persistent_profile_read(
        self,
        connection: sqlite3.Connection,
        authenticated_actor,
    ) -> PersistentProfileReadAuthorizationDecision:
        from wahojobs.persistent_profiles_application import (
            TrustedAuthenticatedBrowserActor,
        )

        if (
            not isinstance(connection, sqlite3.Connection)
            or type(authenticated_actor) is not TrustedAuthenticatedBrowserActor
        ):
            return _unavailable()

        failed = False
        decision = None
        account_reference = None
        try:
            account_reference = authenticated_actor.account_reference_for_authorization()
            if account_reference is None:
                decision = _unavailable()
            elif not _account_reference_valid(account_reference):
                decision = _unavailable()
            elif connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                decision = _unavailable()
            elif connection.execute("PRAGMA query_only").fetchone()[0] != 1:
                decision = _unavailable()
            elif not _authorization_schema_available(connection):
                decision = _unavailable()
            else:
                decision = _authorize_account_reference(
                    connection,
                    account_reference,
                )
        except Exception:
            failed = True
        account_reference = None
        if failed or type(decision) is not PersistentProfileReadAuthorizationDecision:
            return _unavailable()
        return decision

    def __repr__(self) -> str:
        return "DurablePersistentProfileReadAuthorizationGateway(scope='persistent_profile_read')"


def _authorize_account_reference(connection, account_reference):
    account_id, environment_namespace = account_reference
    account_rows = _rows(
        connection,
        "SELECT user_id, lifecycle_status, row_version, created_at, updated_at, "
        "deletion_requested_at, deactivated_at FROM users WHERE user_id = ? LIMIT 2",
        (account_id,),
    )
    if not account_rows:
        return _denied()
    if len(account_rows) != 1:
        return _unavailable()
    account = account_rows[0]
    lifecycle = account.get("lifecycle_status")
    if not _account_row_valid(account, account_id):
        return _unavailable()
    if lifecycle != _AUTHORIZED_LIFECYCLE:
        return _denied()

    identities = _rows(
        connection,
        "SELECT auth_identity_id, user_id, provider, provider_subject, "
        "verified_email, email_verified, created_at, last_authenticated_at, "
        "disabled_at, link_idempotency_key, request_fingerprint "
        "FROM auth_identities WHERE user_id = ? "
        "ORDER BY auth_identity_id LIMIT ?",
        (account_id, MAX_AUTHORIZATION_IDENTITIES + 1),
    )
    if not identities or len(identities) > MAX_AUTHORIZATION_IDENTITIES:
        return _unavailable()
    if any(
        not authoritative_auth_identity_row_valid(
            row,
            expected_user_id=account_id,
            account_created_at=account["created_at"],
        )
        for row in identities
    ):
        return _unavailable()

    bindings = _rows(
        connection,
        "SELECT binding_id, principal_id, user_id, environment_namespace, "
        "binding_role, binding_status, version, latest_event_version, created_at, "
        "updated_at, suspended_at, provenance_json "
        "FROM principal_account_bindings WHERE user_id = ? "
        "ORDER BY binding_id LIMIT ?",
        (account_id, _MAX_ACCOUNT_BINDINGS + 1),
    )
    if len(bindings) > _MAX_ACCOUNT_BINDINGS:
        return _unavailable()
    if any(not _binding_row_valid(row, account_id) for row in bindings):
        return _unavailable()
    principals = {}
    for principal_id in sorted({row["principal_id"] for row in bindings}):
        principal_rows = _rows(
            connection,
            "SELECT principal_id, environment_namespace, principal_type, "
            "lifecycle_status, claim_policy, exclusive_account_binding, version, "
            "created_at, updated_at, provenance_json FROM product_principals "
            "WHERE principal_id = ? LIMIT 2",
            (principal_id,),
        )
        if len(principal_rows) != 1 or not _principal_row_valid(principal_rows[0]):
            return _unavailable()
        principals[principal_id] = principal_rows[0]
    for row in bindings:
        principal = principals[row["principal_id"]]
        if not _binding_identity_boundaries_valid(row, account, principal):
            return _unavailable()
        if not _binding_principal_relationship_valid(
            row,
            principal,
            defer_candidate_availability=(
                row["environment_namespace"] == environment_namespace
                and row["binding_role"] == _AUTHORIZED_BINDING_ROLE
                and row["binding_status"] == _AUTHORIZED_LIFECYCLE
            ),
        ):
            return _unavailable()
        if not _binding_lineage_current(connection, row, account, principal):
            return _unavailable()
    candidates = [
        row
        for row in bindings
        if row["environment_namespace"] == environment_namespace
        and row["binding_role"] == _AUTHORIZED_BINDING_ROLE
        and row["binding_status"] == _AUTHORIZED_LIFECYCLE
    ]
    if not candidates:
        return _denied()
    if len(candidates) != 1:
        return _unavailable()
    binding = candidates[0]

    principal = principals[binding["principal_id"]]
    if principal["principal_type"] != _AUTHORIZED_PRINCIPAL_TYPE:
        return _denied()
    if principal["lifecycle_status"] != _AUTHORIZED_LIFECYCLE:
        return _denied()
    if (
        principal["claim_policy"] != _AUTHORIZED_CLAIM_POLICY
        or principal["exclusive_account_binding"] != 1
    ):
        return _unavailable()
    if (
        binding["environment_namespace"] != environment_namespace
        or principal["environment_namespace"] != environment_namespace
    ):
        return _unavailable()
    active_owners = _rows(
        connection,
        "SELECT binding_id, principal_id, user_id, environment_namespace, "
        "binding_role, binding_status, version, latest_event_version, created_at, "
        "updated_at, suspended_at, provenance_json "
        "FROM principal_account_bindings "
        "WHERE principal_id = ? AND binding_role = 'owner' AND binding_status = 'active' "
        "ORDER BY binding_id LIMIT 2",
        (principal["principal_id"],),
    )
    if (
        len(active_owners) != 1
        or active_owners[0]["binding_id"] != binding["binding_id"]
        or active_owners[0]["user_id"] != account_id
        or active_owners[0]["environment_namespace"] != environment_namespace
    ):
        return _unavailable()

    return _authorized(principal)


def _account_reference_valid(value) -> bool:
    if type(value) is not tuple or len(value) != 2:
        return False
    account_id, environment_namespace = value
    if type(account_id) is not str or ACCOUNT_ID_PATTERN.fullmatch(account_id) is None:
        return False
    try:
        validate_environment_namespace(environment_namespace)
    except Exception:
        return False
    return True


def _account_row_valid(row, account_id) -> bool:
    lifecycle = row.get("lifecycle_status")
    created_at = row.get("created_at")
    updated_at = row.get("updated_at")
    deletion_requested_at = row.get("deletion_requested_at")
    deactivated_at = row.get("deactivated_at")
    if (
        row.get("user_id") != account_id
        or type(account_id) is not str
        or ACCOUNT_ID_PATTERN.fullmatch(account_id) is None
        or lifecycle not in LIFECYCLE_STATUSES
        or type(row.get("row_version")) is not int
        or row["row_version"] < 1
        or not _canonical_timestamp(created_at)
        or not _canonical_timestamp(updated_at)
        or updated_at < created_at
        or not _optional_timestamp_after(deletion_requested_at, created_at)
        or not _optional_timestamp_after(deactivated_at, deletion_requested_at)
    ):
        return False
    if lifecycle in {"active", "suspended"}:
        return deletion_requested_at is None and deactivated_at is None
    if lifecycle == "deletion_requested":
        return deletion_requested_at is not None and deactivated_at is None
    return deletion_requested_at is not None and deactivated_at is not None


def _binding_row_valid(row, account_id) -> bool:
    try:
        validate_binding_id(row.get("binding_id"))
        validate_principal_id(row.get("principal_id"))
        validate_environment_namespace(row.get("environment_namespace"))
    except Exception:
        return False
    created_at = row.get("created_at")
    updated_at = row.get("updated_at")
    suspended_at = row.get("suspended_at")
    status = row.get("binding_status")
    return (
        type(row.get("user_id")) is str
        and ACCOUNT_ID_PATTERN.fullmatch(row["user_id"]) is not None
        and row["user_id"] == account_id
        and row.get("binding_role") in BINDING_ROLES
        and status in BINDING_STATUSES
        and type(row.get("version")) is int
        and row["version"] >= 1
        and row.get("latest_event_version") == row["version"]
        and _canonical_timestamp(created_at)
        and _canonical_timestamp(updated_at)
        and updated_at >= created_at
        and _optional_timestamp_after(suspended_at, created_at)
        and ((status == "suspended") == (suspended_at is not None))
        and _ownership_metadata_valid(row.get("provenance_json"), "provenance")
    )


def _principal_row_valid(row) -> bool:
    try:
        validate_principal_id(row.get("principal_id"))
        validate_environment_namespace(row.get("environment_namespace"))
    except Exception:
        return False
    principal_type = row.get("principal_type")
    claim_policy = row.get("claim_policy")
    created_at = row.get("created_at")
    updated_at = row.get("updated_at")
    policy_coherent = (
        (principal_type == "legacy_profile" and claim_policy in {"nonclaimable", "manual_approval"})
        or (principal_type == "account_native" and claim_policy == "account_native")
        or (principal_type in {"development", "sample", "system"} and claim_policy == "nonclaimable")
    )
    return (
        principal_type in PRINCIPAL_TYPES
        and row.get("lifecycle_status") in PRINCIPAL_STATUSES
        and claim_policy in CLAIM_POLICIES
        and policy_coherent
        and type(row.get("exclusive_account_binding")) is int
        and row["exclusive_account_binding"] in {0, 1}
        and type(row.get("version")) is int
        and row["version"] >= 1
        and _canonical_timestamp(created_at)
        and _canonical_timestamp(updated_at)
        and updated_at >= created_at
        and _ownership_metadata_valid(row.get("provenance_json"), "provenance")
    )


def _binding_identity_boundaries_valid(binding, account, principal) -> bool:
    return ownership_timestamp_not_before(
        binding.get("created_at"), account.get("created_at")
    ) and ownership_timestamp_not_before(
        binding.get("created_at"), principal.get("created_at")
    )


def _binding_principal_relationship_valid(
    binding,
    principal,
    *,
    defer_candidate_availability=False,
) -> bool:
    if binding.get("environment_namespace") != principal.get(
        "environment_namespace"
    ):
        return False
    if binding.get("binding_status") != "active":
        return True
    if defer_candidate_availability:
        return True
    return (
        principal.get("lifecycle_status") == "active"
        and principal.get("claim_policy") != "nonclaimable"
    )


def _binding_lineage_current(connection, binding, account, principal) -> bool:
    expected_version = binding["latest_event_version"]
    events = _rows(
        connection,
        "SELECT event_id, principal_id, user_id, binding_id, environment_namespace, "
        "event_version, event_type, prior_status, resulting_status, actor_type, "
        "reason_code, approval_reference, idempotency_key, request_fingerprint, "
        "occurred_at, metadata_json FROM ownership_binding_events "
        "WHERE binding_id = ? ORDER BY event_version LIMIT ?",
        (binding["binding_id"], MAX_AUTHORIZATION_EVENTS_PER_BINDING + 1),
    )
    if (
        len(events) > MAX_AUTHORIZATION_EVENTS_PER_BINDING
        or len(events) != expected_version
    ):
        return False
    prior_status = None
    prior_time = None
    for event_version, event in enumerate(events, start=1):
        if not _binding_event_valid(
            event,
            binding,
            account,
            principal,
            event_version=event_version,
            prior_status=prior_status,
            prior_time=prior_time,
        ):
            return False
        prior_status = event["resulting_status"]
        prior_time = event["occurred_at"]
    return prior_status == binding["binding_status"]


def _binding_event_valid(
    event,
    binding,
    account,
    principal,
    *,
    event_version,
    prior_status,
    prior_time,
):
    try:
        validate_binding_event_id(event.get("event_id"))
        validate_principal_id(event.get("principal_id"))
        validate_binding_id(event.get("binding_id"))
        validate_environment_namespace(event.get("environment_namespace"))
    except Exception:
        return False
    event_type = event.get("event_type")
    resulting_status = event.get("resulting_status")
    event_prior = event.get("prior_status")
    transition_valid = (
        (event_type == "binding_activated" and event_prior is None and resulting_status == "active")
        or (event_type == "binding_suspended" and event_prior == "active" and resulting_status == "suspended")
        or (event_type == "binding_reactivated" and event_prior == "suspended" and resulting_status == "active")
        or (event_type == "binding_released" and event_prior in {"active", "suspended"} and resulting_status == "released")
        or (event_type == "administrative_correction" and event_prior is not None)
    )
    occurred_at = event.get("occurred_at")
    approval_reference = event.get("approval_reference")
    structurally_valid = (
        event.get("event_version") == event_version
        and event.get("principal_id") == binding["principal_id"]
        and event.get("user_id") == binding["user_id"]
        and event.get("binding_id") == binding["binding_id"]
        and event.get("environment_namespace") == binding["environment_namespace"]
        and event_type in BINDING_EVENT_TYPES
        and event_prior == prior_status
        and resulting_status in BINDING_STATUSES
        and transition_valid
        and event.get("actor_type") in BINDING_ACTOR_TYPES
        and type(event.get("reason_code")) is str
        and REASON_CODE_PATTERN.fullmatch(event["reason_code"]) is not None
        and (
            approval_reference is None
            or (
                type(approval_reference) is str
                and APPROVAL_REFERENCE_PATTERN.fullmatch(approval_reference) is not None
            )
        )
        and type(event.get("idempotency_key")) is str
        and 16 <= len(event["idempotency_key"]) <= 256
        and event["idempotency_key"] == event["idempotency_key"].strip()
        and type(event.get("request_fingerprint")) is str
        and SHA256_PATTERN.fullmatch(event["request_fingerprint"]) is not None
        and _canonical_timestamp(occurred_at)
        and ownership_timestamp_not_before(occurred_at, account.get("created_at"))
        and ownership_timestamp_not_before(occurred_at, principal.get("created_at"))
        and ownership_timestamp_not_before(occurred_at, binding.get("created_at"))
        and (
            prior_time is None
            or ownership_timestamp_not_before(occurred_at, prior_time)
        )
    )
    if not structurally_valid:
        return False
    try:
        metadata = validate_metadata_document(
            event.get("metadata_json"), field_name="metadata"
        )
        validate_event_request_fingerprint(
            event["request_fingerprint"],
            principal_id=event["principal_id"],
            binding_id=event["binding_id"],
            user_id=event["user_id"],
            expected_event_version=event["event_version"],
            event_type=event["event_type"],
            prior_status=event["prior_status"],
            resulting_status=event["resulting_status"],
            actor_type=event["actor_type"],
            reason_code=event["reason_code"],
            approval_reference=event["approval_reference"],
            occurred_at=event["occurred_at"],
            metadata=metadata,
        )
    except Exception:
        return False
    return True


def _canonical_timestamp(value) -> bool:
    if type(value) is not str or len(value) != 25:
        return False
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S+00:00")
    except ValueError:
        return False
    return parsed.strftime("%Y-%m-%dT%H:%M:%S+00:00") == value


def _optional_timestamp_after(value, floor) -> bool:
    if value is None:
        return True
    return _canonical_timestamp(value) and type(floor) is str and value >= floor


def _ownership_metadata_valid(value, field_name) -> bool:
    try:
        validate_metadata_document(value, field_name=field_name)
    except Exception:
        return False
    return True


def _authorization_schema_available(connection) -> bool:
    if not _migration_marker_exact(connection, ACCOUNTS_MIGRATION_VERSION):
        return False
    if not _migration_marker_exact(connection, _OWNERSHIP_MIGRATION_VERSION):
        return False
    if not _accounts_schema_attested(connection):
        return False
    ownership = attest_ownership_schema(connection)
    return type(ownership) is dict and ownership.get("state") == "correctly_installed"


def _migration_marker_exact(connection, version) -> bool:
    row = connection.execute(
        "SELECT COUNT(*) FROM wahojobs_schema_migrations WHERE version = ?",
        (version,),
    ).fetchone()
    return row is not None and row[0] == 1


def _accounts_schema_attested(connection) -> bool:
    expected = _expected_accounts_manifest()
    if set(expected) != EXPECTED_ACCOUNT_OBJECTS:
        return False
    return attest_account_schema(connection)


def _expected_accounts_manifest():
    return expected_account_schema_fingerprints()


def _normalize_sql(value):
    if type(value) is not str:
        return None
    return re.sub(r"\s+", " ", value.strip().rstrip(";"))


def _definition_fingerprint(value):
    if type(value) is not str:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _rows(connection, sql, parameters):
    cursor = connection.execute(sql, parameters)
    names = tuple(item[0] for item in cursor.description)
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _authorized(row):
    from wahojobs.persistent_profiles import TrustedPrincipalContext
    from wahojobs.persistent_profiles_application import (
        _DURABLE_PROFILE_READ_GRANT_ISSUER,
    )

    principal = TrustedPrincipalContext(
        principal_id=row["principal_id"],
        environment_namespace=row["environment_namespace"],
        principal_type=row["principal_type"],
        lifecycle_status=row["lifecycle_status"],
        claim_policy=row["claim_policy"],
        exclusive_account_binding=True,
        eligibility_mode="account_native",
        active_owner_binding=True,
    )
    return PersistentProfileReadAuthorizationDecision(
        "authorized",
        _DURABLE_PROFILE_READ_GRANT_ISSUER.issue(principal),
    )


def _denied():
    return PersistentProfileReadAuthorizationDecision("denied")


def _unavailable():
    return PersistentProfileReadAuthorizationDecision("unavailable")

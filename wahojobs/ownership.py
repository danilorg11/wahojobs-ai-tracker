from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import unicodedata
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import NewType

from wahojobs.accounts import InvalidAccountInput, validate_account_metadata


MIGRATION_VERSION = "003_product_principals"
ENVIRONMENT_NAMESPACE_PATTERN = re.compile(r"^[a-z0-9_.-]{1,64}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
REASON_CODE_PATTERN = re.compile(r"^[a-z0-9_.-]{1,128}$")
APPROVAL_REFERENCE_PATTERN = re.compile(r"^[0-9A-Za-z_.:-]{1,128}$")
ACCOUNT_ID_PATTERN = re.compile(r"^usr_[0-9a-f]{32}$")

MAX_METADATA_NODES = 512
OWNER_RESOURCE_ALIAS_KINDS = frozenset(
    {"profile_id", "pipeline_owner", "applicant_user_id", "legacy_user_id"}
)
ANONYMOUS_ALIAS_KINDS = frozenset({"anonymous_user_key"})
ALIAS_FAMILIES = frozenset({"owner_resource", "anonymous"})

OWNERSHIP_SENSITIVE_METADATA_NAMES = frozenset(
    {
        "authorization",
        "authorizationheader",
        "authenticationheader",
        "bearer",
        "cookie",
        "password",
        "secret",
        "token",
        "sessiontoken",
        "csrf",
        "csrfmaterial",
        "invitationhmac",
        "providersubject",
        "resume",
        "resumecontent",
        "rawclaims",
        "rawhtml",
        "rawapplicationcontent",
        "applicationcontent",
        "databasepath",
        "email",
        "oauth",
        "oauthclaim",
        "oauthclaims",
        "sql",
        "sqlquery",
        "credential",
        "tokenhash",
        "tokenmaterial",
        "tokensecret",
        "tokenvalue",
    }
)
OWNERSHIP_SENSITIVE_METADATA_PREFIXES = (
    "authorization",
    "authenticationheader",
    "csrf",
    "email",
    "invitationhmac",
    "oauth",
    "providersubject",
    "resume",
    "rawclaim",
    "rawapplicationcontent",
    "applicationcontent",
    "sessiontoken",
)

PRINCIPAL_TYPES = frozenset(
    {"legacy_profile", "account_native", "development", "sample", "system"}
)
PRINCIPAL_STATUSES = frozenset({"dormant", "active", "suspended", "retired"})
CLAIM_POLICIES = frozenset({"nonclaimable", "manual_approval", "account_native"})
ALIAS_KINDS = OWNER_RESOURCE_ALIAS_KINDS | ANONYMOUS_ALIAS_KINDS
ALIAS_SOURCES = frozenset(
    {
        "user_profiles",
        "user_pipeline_items",
        "user_pipeline_transitions",
        "applicant_status_updates",
        "manual_review",
        "account_creation",
        "system",
    }
)
BINDING_ROLES = frozenset({"owner", "delegated", "support"})
BINDING_STATUSES = frozenset({"active", "suspended", "released"})
BINDING_EVENT_TYPES = frozenset(
    {
        "binding_activated",
        "binding_suspended",
        "binding_reactivated",
        "binding_released",
        "administrative_correction",
    }
)
BINDING_ACTOR_TYPES = frozenset(
    {"authenticated_user", "administrator", "system", "migration"}
)

OWNERSHIP_TABLES = (
    "product_principals",
    "legacy_owner_aliases",
    "principal_account_bindings",
    "ownership_binding_events",
)
OWNERSHIP_INDEXES = (
    "idx_product_principals_environment_type",
    "idx_legacy_owner_aliases_principal",
    "idx_legacy_owner_aliases_family_coherence",
    "idx_principal_account_bindings_user_status",
    "idx_principal_account_bindings_active_identity",
    "idx_ownership_binding_events_principal_version",
    "idx_ownership_binding_events_binding_time",
)
OWNERSHIP_TRIGGERS = (
    "trg_product_principals_insert_guard",
    "trg_product_principals_identity_immutable",
    "trg_product_principals_update_guard",
    "trg_product_principals_no_delete",
    "trg_legacy_owner_aliases_insert_guard",
    "trg_legacy_owner_aliases_no_update",
    "trg_legacy_owner_aliases_no_delete",
    "trg_principal_account_bindings_insert_guard",
    "trg_principal_account_bindings_update_guard",
    "trg_principal_account_bindings_no_delete",
    "trg_ownership_binding_events_insert_guard",
    "trg_ownership_binding_events_no_update",
    "trg_ownership_binding_events_no_delete",
)
OWNERSHIP_OBJECTS = OWNERSHIP_TABLES + OWNERSHIP_INDEXES + OWNERSHIP_TRIGGERS

PrincipalId = NewType("PrincipalId", str)
LegacyOwnerAliasId = NewType("LegacyOwnerAliasId", str)
PrincipalAccountBindingId = NewType("PrincipalAccountBindingId", str)
OwnershipBindingEventId = NewType("OwnershipBindingEventId", str)


class OwnershipValidationError(ValueError):
    pass


class OwnershipEventFingerprintMismatch(OwnershipValidationError):
    pass


class OwnershipStateConflict(RuntimeError):
    def __init__(self):
        super().__init__("Ownership state could not be changed.")


class OwnershipIdempotencyConflict(OwnershipStateConflict):
    pass


@dataclass(frozen=True)
class PublicPrincipal:
    principal_id: str
    environment_namespace: str
    principal_type: str
    lifecycle_status: str
    claim_policy: str
    exclusive_account_binding: bool
    version: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PublicLegacyOwnerAlias:
    alias_id: str
    principal_id: str
    environment_namespace: str
    alias_kind: str
    alias_family: str
    claimability: str
    safe_descriptor: str
    discovered_from: str
    created_at: str


@dataclass(frozen=True)
class PublicPrincipalAccountBinding:
    binding_id: str
    principal_id: str
    account_reference: str
    environment_namespace: str
    binding_role: str
    binding_status: str
    version: int
    created_at: str
    updated_at: str
    suspended_at: str | None


@dataclass(frozen=True)
class PublicOwnershipBindingEvent:
    event_id: str
    principal_id: str
    binding_id: str
    account_reference: str
    environment_namespace: str
    event_version: int
    event_type: str
    prior_status: str | None
    resulting_status: str
    actor_type: str
    reason_code: str
    occurred_at: str


@dataclass(frozen=True)
class LegacyOwnerObservation:
    report_reference: str
    alias_kind: str
    alias_family: str
    safe_descriptor: str
    classification: str
    recommended_claimability: str
    source_tables: tuple[str, ...]
    occurrence_count: int
    private_alias_value: str

    def public_dict(self) -> dict:
        return {
            "report_reference": self.report_reference,
            "alias_kind": self.alias_kind,
            "alias_family": self.alias_family,
            "safe_descriptor": self.safe_descriptor,
            "classification": self.classification,
            "recommended_claimability": self.recommended_claimability,
            "source_tables": list(self.source_tables),
            "occurrence_count": self.occurrence_count,
        }


@dataclass(frozen=True)
class LegacyDiscoveryIssue:
    report_reference: str
    alias_kind: str
    alias_family: str
    safe_descriptor: str
    source_table: str
    reason: str
    private_alias_value: str

    def public_dict(self) -> dict:
        return {
            "report_reference": self.report_reference,
            "alias_kind": self.alias_kind,
            "alias_family": self.alias_family,
            "safe_descriptor": self.safe_descriptor,
            "source_table": self.source_table,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class LegacyDiscoveryResult:
    observations: tuple[LegacyOwnerObservation, ...]
    issues: tuple[LegacyDiscoveryIssue, ...]
    distinct_raw_value_count: int
    distinct_kind_value_pair_count: int
    observation_count: int
    kind_value_pair_classification_counts: dict[str, int]
    observation_classification_counts: dict[str, int]
    development_kind_value_pair_count: int
    development_observation_count: int
    local_singleton_kind_value_pair_count: int
    local_singleton_observation_count: int

    def public_dict(self) -> dict:
        return {
            "summary": {
                "distinct_raw_value_count": self.distinct_raw_value_count,
                "distinct_kind_value_pair_count": self.distinct_kind_value_pair_count,
                "observation_count": self.observation_count,
                "kind_value_pair_classification_counts": dict(
                    sorted(self.kind_value_pair_classification_counts.items())
                ),
                "observation_classification_counts": dict(
                    sorted(self.observation_classification_counts.items())
                ),
                "development_kind_value_pair_count": self.development_kind_value_pair_count,
                "development_observation_count": self.development_observation_count,
                "local_singleton_kind_value_pair_count": self.local_singleton_kind_value_pair_count,
                "local_singleton_observation_count": self.local_singleton_observation_count,
                "units_overlap": (
                    "Raw values deduplicate across alias kinds; kind/value pairs retain "
                    "alias-kind semantics; observations count repeated source rows."
                ),
            },
            "observations": [item.public_dict() for item in self.observations],
            "issues": [item.public_dict() for item in self.issues],
        }


@dataclass(frozen=True)
class BindingEventCommand:
    principal_id: str
    binding_id: str
    user_id: str
    expected_event_version: int
    event_type: str
    prior_status: str | None
    resulting_status: str
    actor_type: str
    reason_code: str
    approval_reference: str | None
    idempotency_key: str
    occurred_at: str
    metadata: dict | None = None


@dataclass(frozen=True)
class CreateBindingCommand:
    principal_id: str
    user_id: str
    binding_role: str
    actor_type: str
    reason_code: str
    approval_reference: str | None
    idempotency_key: str
    occurred_at: str
    metadata: dict | None = None


@dataclass(frozen=True)
class OwnershipEventResult:
    event_id: str
    principal_id: str
    binding_id: str
    account_reference: str
    event_version: int
    event_type: str
    prior_status: str | None
    resulting_status: str
    occurred_at: str
    replayed: bool


@dataclass(frozen=True)
class AccountNativePrincipalBootstrapResult:
    principal_id: str
    binding_id: str
    initial_event_id: str
    environment_namespace: str
    created: bool


def new_principal_id() -> PrincipalId:
    return PrincipalId(_random_id("prn"))


def new_alias_id() -> LegacyOwnerAliasId:
    return LegacyOwnerAliasId(_random_id("loa"))


def new_binding_id() -> PrincipalAccountBindingId:
    return PrincipalAccountBindingId(_random_id("pab"))


def new_binding_event_id() -> OwnershipBindingEventId:
    return OwnershipBindingEventId(_random_id("obe"))


def validate_principal_id(value) -> str:
    return _validate_prefixed_id(value, "principal_id", "prn")


def validate_alias_id(value) -> str:
    return _validate_prefixed_id(value, "alias_id", "loa")


def validate_binding_id(value) -> str:
    return _validate_prefixed_id(value, "binding_id", "pab")


def validate_binding_event_id(value) -> str:
    return _validate_prefixed_id(value, "event_id", "obe")


def validate_environment_namespace(value) -> str:
    if type(value) is not str or ENVIRONMENT_NAMESPACE_PATTERN.fullmatch(value) is None:
        raise OwnershipValidationError("Environment namespace is invalid.")
    return value


def alias_family(alias_kind) -> str:
    if alias_kind in OWNER_RESOURCE_ALIAS_KINDS:
        return "owner_resource"
    if alias_kind in ANONYMOUS_ALIAS_KINDS:
        return "anonymous"
    raise OwnershipValidationError("Legacy owner alias kind is invalid.")


def validate_legacy_alias(value) -> str:
    if type(value) is not str or not (1 <= len(value) <= 512):
        raise OwnershipValidationError("Legacy owner alias is invalid.")
    if value != value.strip() or any(unicodedata.category(char) == "Cc" for char in value):
        raise OwnershipValidationError("Legacy owner alias is invalid.")
    return value


def validate_sha256(value, *, field_name="sha256") -> str:
    if type(value) is not str or SHA256_PATTERN.fullmatch(value) is None:
        raise OwnershipValidationError(f"{field_name} is invalid.")
    return value


def canonical_metadata(metadata) -> tuple[dict, str]:
    """Validate ownership metadata and encode it deterministically for services."""
    try:
        validated = validate_account_metadata({} if metadata is None else metadata)
    except InvalidAccountInput as exc:
        raise OwnershipValidationError(str(exc)) from exc
    _validate_ownership_metadata(validated)
    encoded = json.dumps(validated, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return validated, encoded


def validate_metadata_document(encoded, *, field_name="metadata") -> dict:
    if type(encoded) is not str:
        raise OwnershipValidationError(f"{field_name} must be bounded JSON.")
    try:
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise OwnershipValidationError(f"{field_name} must be bounded JSON.") from exc
    if type(decoded) is not dict:
        raise OwnershipValidationError(f"{field_name} must be a JSON object.")
    validated, _ = canonical_metadata(decoded)
    return validated


def report_local_references(values, prefix: str) -> dict[str, str]:
    """Create deterministic per-report references without hashing source identities."""
    return {
        value: f"{prefix}-{index:04d}"
        for index, value in enumerate(sorted({str(value) for value in values}), 1)
    }


def discover_legacy_owners(conn) -> LegacyDiscoveryResult:
    """Read legacy owner strings without registering or exposing any identity."""
    aggregates: dict[tuple[str, str], dict] = {}
    malformed: dict[tuple[str, str], dict] = {}
    raw_values: set[str] = set()
    observation_count = 0

    def observe(alias_kind, raw_value, source_table, is_sample):
        nonlocal observation_count
        if raw_value is None or raw_value == "":
            return
        raw = raw_value if type(raw_value) is str else str(raw_value)
        raw_values.add(raw)
        observation_count += 1
        try:
            value = validate_legacy_alias(raw_value)
        except OwnershipValidationError:
            entry = malformed.setdefault(
                (alias_kind, raw),
                {"sources": set(), "count": 0, "classification": "malformed"},
            )
            entry["sources"].add(source_table)
            entry["count"] += 1
            return
        entry = aggregates.setdefault(
            (alias_kind, value),
            {"sources": set(), "sample_flags": [], "count": 0},
        )
        entry["sources"].add(source_table)
        if is_sample is not None:
            entry["sample_flags"].append(bool(is_sample))
        entry["count"] += 1

    if _table_exists(conn, "user_profiles"):
        for row in conn.execute("SELECT profile_id, user_id, is_sample FROM user_profiles"):
            observe("profile_id", row[0], "user_profiles", row[2])
            observe("legacy_user_id", row[1], "user_profiles", row[2])

    if _table_exists(conn, "user_pipeline_items"):
        for row in conn.execute(
            "SELECT profile_id, user_id, is_sample FROM user_pipeline_items"
        ):
            observe("pipeline_owner", row[0], "user_pipeline_items", row[2])
            observe("legacy_user_id", row[1], "user_pipeline_items", row[2])

    if _table_exists(conn, "user_pipeline_transitions"):
        for row in conn.execute("SELECT profile_id FROM user_pipeline_transitions"):
            observe("pipeline_owner", row[0], "user_pipeline_transitions", None)

    if _table_exists(conn, "applicant_status_updates"):
        for row in conn.execute(
            "SELECT profile_id, user_id, anonymous_user_key, is_sample "
            "FROM applicant_status_updates"
        ):
            observe("profile_id", row[0], "applicant_status_updates", row[3])
            observe("applicant_user_id", row[1], "applicant_status_updates", row[3])
            observe("anonymous_user_key", row[2], "applicant_status_updates", row[3])

    all_pairs = set(aggregates) | set(malformed)
    pair_refs = {
        pair: f"legacy-owner-{index:04d}"
        for index, pair in enumerate(sorted(all_pairs), 1)
    }
    observations = []
    pair_classifications = Counter()
    observation_classifications = Counter()
    local_pairs = 0
    local_observations = 0
    for (alias_kind, alias_value), evidence in sorted(aggregates.items()):
        classification = _legacy_classification(alias_value, evidence["sample_flags"])
        pair_classifications[classification] += 1
        observation_classifications[classification] += evidence["count"]
        if alias_value == "local_user":
            local_pairs += 1
            local_observations += evidence["count"]
        observations.append(
            LegacyOwnerObservation(
                report_reference=pair_refs[(alias_kind, alias_value)],
                alias_kind=alias_kind,
                alias_family=alias_family(alias_kind),
                safe_descriptor=_safe_legacy_descriptor(alias_kind, classification),
                classification=classification,
                recommended_claimability=(
                    "nonclaimable"
                    if classification in {"development", "sample"}
                    else "manual_approval"
                ),
                source_tables=tuple(sorted(evidence["sources"])),
                occurrence_count=evidence["count"],
                private_alias_value=alias_value,
            )
        )

    issues = []
    for (kind, raw), item in sorted(malformed.items()):
        pair_classifications["malformed"] += 1
        observation_classifications["malformed"] += item["count"]
        issues.append(
            LegacyDiscoveryIssue(
                report_reference=pair_refs[(kind, raw)],
                alias_kind=kind,
                alias_family=alias_family(kind),
                safe_descriptor=_safe_legacy_descriptor(kind, "malformed"),
                source_table=", ".join(sorted(item["sources"])),
                reason="malformed_legacy_owner",
                private_alias_value=raw,
            )
        )

    return LegacyDiscoveryResult(
        observations=tuple(observations),
        issues=tuple(issues),
        distinct_raw_value_count=len(raw_values),
        distinct_kind_value_pair_count=len(all_pairs),
        observation_count=observation_count,
        kind_value_pair_classification_counts=dict(pair_classifications),
        observation_classification_counts=dict(observation_classifications),
        development_kind_value_pair_count=pair_classifications["development"],
        development_observation_count=observation_classifications["development"],
        local_singleton_kind_value_pair_count=local_pairs,
        local_singleton_observation_count=local_observations,
    )


def event_request_fingerprint(
    *,
    principal_id: str,
    binding_id: str,
    user_id: str,
    expected_event_version: int,
    event_type: str,
    prior_status: str | None,
    resulting_status: str,
    actor_type: str,
    reason_code: str,
    approval_reference: str | None,
    occurred_at: str,
    metadata: dict | None,
) -> str:
    _, metadata_json = canonical_metadata(metadata)
    payload = {
        "actor_type": actor_type,
        "approval_reference": approval_reference,
        "binding_id": binding_id,
        "event_type": event_type,
        "expected_event_version": expected_event_version,
        "metadata": json.loads(metadata_json),
        "occurred_at": occurred_at,
        "principal_id": principal_id,
        "prior_status": prior_status,
        "reason_code": reason_code,
        "resulting_status": resulting_status,
        "user_id": user_id,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def validate_event_request_fingerprint(
    stored_fingerprint: str,
    *,
    principal_id: str,
    binding_id: str,
    user_id: str,
    expected_event_version: int,
    event_type: str,
    prior_status: str | None,
    resulting_status: str,
    actor_type: str,
    reason_code: str,
    approval_reference: str | None,
    occurred_at: str,
    metadata: dict,
) -> str:
    """Verify one stored event digest with the authoritative M003 contract."""
    validate_sha256(stored_fingerprint, field_name="request_fingerprint")
    expected = event_request_fingerprint(
        principal_id=principal_id,
        binding_id=binding_id,
        user_id=user_id,
        expected_event_version=expected_event_version,
        event_type=event_type,
        prior_status=prior_status,
        resulting_status=resulting_status,
        actor_type=actor_type,
        reason_code=reason_code,
        approval_reference=approval_reference,
        occurred_at=occurred_at,
        metadata=metadata,
    )
    if not secrets.compare_digest(stored_fingerprint, expected):
        raise OwnershipEventFingerprintMismatch(
            "Ownership event fingerprint is invalid."
        )
    return stored_fingerprint


def ownership_timestamp_not_before(value, boundary) -> bool:
    """Apply M003's inclusive canonical UTC timestamp boundary."""
    try:
        _validate_timestamp(value)
        _validate_timestamp(boundary)
    except OwnershipValidationError:
        return False
    return datetime.fromisoformat(value) >= datetime.fromisoformat(boundary)


def ensure_account_native_principal(
    conn,
    *,
    user_id: str,
    environment_namespace: str,
    occurred_at: str,
    failure_injector=None,
) -> AccountNativePrincipalBootstrapResult:
    """Atomically create or resolve one account-native owner lineage."""
    user_id = _validate_account_id(user_id)
    environment_namespace = validate_environment_namespace(environment_namespace)
    occurred_at = _validate_timestamp(occurred_at)
    try:
        with _ownership_transaction(conn):
            _require_bootstrap_database_state(conn)
            account = _canonical_active_account(conn, user_id)
            if not ownership_timestamp_not_before(occurred_at, account["created_at"]):
                raise OwnershipValidationError("Ownership timestamp predates the account.")

            existing = _account_native_bootstrap_result(
                conn,
                user_id=user_id,
                environment_namespace=environment_namespace,
                created=False,
            )
            if existing is not None:
                return existing

            principal_id = str(new_principal_id())
            _, empty_json = canonical_metadata({})
            conn.execute(
                "INSERT INTO product_principals "
                "(principal_id, environment_namespace, principal_type, lifecycle_status, "
                "claim_policy, exclusive_account_binding, version, created_at, updated_at, "
                "provenance_json) VALUES (?, ?, 'account_native', 'active', "
                "'account_native', 1, 1, ?, ?, ?)",
                (
                    principal_id,
                    environment_namespace,
                    occurred_at,
                    occurred_at,
                    empty_json,
                ),
            )
            _inject(failure_injector, "after_principal_insert")
            created_event = create_binding_with_initial_event(
                conn,
                CreateBindingCommand(
                    principal_id=principal_id,
                    user_id=user_id,
                    binding_role="owner",
                    actor_type="system",
                    reason_code="account_native_bootstrap",
                    approval_reference=None,
                    idempotency_key="account-native-bootstrap-v1",
                    occurred_at=occurred_at,
                    metadata={},
                ),
                failure_injector=failure_injector,
            )
            if created_event.replayed:
                raise OwnershipStateConflict()
            _require_bootstrap_database_state(conn)
            resolved = _account_native_bootstrap_result(
                conn,
                user_id=user_id,
                environment_namespace=environment_namespace,
                created=True,
            )
            if (
                resolved is None
                or resolved.principal_id != principal_id
                or resolved.binding_id != created_event.binding_id
                or resolved.initial_event_id != created_event.event_id
            ):
                raise OwnershipStateConflict()
            return resolved
    except sqlite3.IntegrityError:
        raise OwnershipStateConflict() from None


def append_binding_event(conn, command: BindingEventCommand, *, failure_injector=None):
    """Append history and update its projection atomically, with exact replay."""
    command = _validate_binding_event_command(command)
    try:
        with _ownership_transaction(conn):
            existing = _existing_event(conn, command.principal_id, command.idempotency_key)
            if existing is not None:
                return _replay_event(conn, existing, command)
            binding = _binding_row(conn, command.binding_id)
            if (
                binding is None
                or binding["principal_id"] != command.principal_id
                or binding["user_id"] != command.user_id
                or binding["version"] != command.expected_event_version - 1
                or binding["latest_event_version"] != command.expected_event_version - 1
                or binding["binding_status"] != command.prior_status
            ):
                raise OwnershipStateConflict()
            fingerprint = _command_fingerprint(command)
            event_id = str(new_binding_event_id())
            _, metadata_json = canonical_metadata(command.metadata)
            conn.execute(
                "INSERT INTO ownership_binding_events "
                "(event_id, principal_id, user_id, binding_id, environment_namespace, "
                "event_version, event_type, prior_status, resulting_status, actor_type, "
                "reason_code, approval_reference, idempotency_key, request_fingerprint, "
                "occurred_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    command.principal_id,
                    command.user_id,
                    command.binding_id,
                    binding["environment_namespace"],
                    command.expected_event_version,
                    command.event_type,
                    command.prior_status,
                    command.resulting_status,
                    command.actor_type,
                    command.reason_code,
                    command.approval_reference,
                    command.idempotency_key,
                    fingerprint,
                    command.occurred_at,
                    metadata_json,
                ),
            )
            _inject(failure_injector, "after_event_insert")
            suspended_at = (
                command.occurred_at if command.resulting_status == "suspended" else None
            )
            updated = conn.execute(
                "UPDATE principal_account_bindings SET binding_status = ?, version = ?, "
                "latest_event_version = ?, updated_at = ?, suspended_at = ? "
                "WHERE binding_id = ? AND principal_id = ? AND user_id = ? "
                "AND version = ? AND latest_event_version = ? AND binding_status = ?",
                (
                    command.resulting_status,
                    command.expected_event_version,
                    command.expected_event_version,
                    command.occurred_at,
                    suspended_at,
                    command.binding_id,
                    command.principal_id,
                    command.user_id,
                    command.expected_event_version - 1,
                    command.expected_event_version - 1,
                    command.prior_status,
                ),
            )
            if updated.rowcount != 1:
                raise OwnershipStateConflict()
            _inject(failure_injector, "after_projection_update")
            return _event_result(
                _event_row(conn, event_id),
                account_reference="account-bound",
                replayed=False,
            )
    except sqlite3.IntegrityError:
        existing = _existing_event(conn, command.principal_id, command.idempotency_key)
        if existing is not None:
            return _replay_event(conn, existing, command)
        raise OwnershipStateConflict() from None


def create_binding_with_initial_event(
    conn, command: CreateBindingCommand, *, failure_injector=None
):
    """Create a binding and its authoritative first event in one transaction."""
    command = _validate_create_binding_command(command)
    try:
        with _ownership_transaction(conn):
            existing = _existing_event(conn, command.principal_id, command.idempotency_key)
            if existing is not None:
                binding = _binding_row(conn, existing["binding_id"])
                if binding is None or binding["binding_role"] != command.binding_role:
                    raise OwnershipIdempotencyConflict()
                replay_command = _initial_event_command(command, existing["binding_id"])
                return _replay_event(conn, existing, replay_command)
            principal = _principal_row(conn, command.principal_id)
            if principal is None:
                raise OwnershipStateConflict()
            binding_id = str(new_binding_id())
            event_command = _initial_event_command(command, binding_id)
            _, empty_json = canonical_metadata({})
            conn.execute(
                "INSERT INTO principal_account_bindings "
                "(binding_id, principal_id, user_id, environment_namespace, binding_role, "
                "binding_status, version, latest_event_version, created_at, updated_at, "
                "suspended_at, provenance_json) VALUES (?, ?, ?, ?, ?, 'active', 1, 1, ?, ?, NULL, ?)",
                (
                    binding_id,
                    command.principal_id,
                    command.user_id,
                    principal["environment_namespace"],
                    command.binding_role,
                    command.occurred_at,
                    command.occurred_at,
                    empty_json,
                ),
            )
            _inject(failure_injector, "after_binding_insert")
            event_id = str(new_binding_event_id())
            _, metadata_json = canonical_metadata(command.metadata)
            conn.execute(
                "INSERT INTO ownership_binding_events "
                "(event_id, principal_id, user_id, binding_id, environment_namespace, "
                "event_version, event_type, prior_status, resulting_status, actor_type, "
                "reason_code, approval_reference, idempotency_key, request_fingerprint, "
                "occurred_at, metadata_json) VALUES (?, ?, ?, ?, ?, 1, 'binding_activated', "
                "NULL, 'active', ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    command.principal_id,
                    command.user_id,
                    binding_id,
                    principal["environment_namespace"],
                    command.actor_type,
                    command.reason_code,
                    command.approval_reference,
                    command.idempotency_key,
                    _command_fingerprint(event_command),
                    command.occurred_at,
                    metadata_json,
                ),
            )
            _inject(failure_injector, "after_initial_event_insert")
            return _event_result(
                _event_row(conn, event_id),
                account_reference="account-bound",
                replayed=False,
            )
    except sqlite3.IntegrityError:
        existing = _existing_event(conn, command.principal_id, command.idempotency_key)
        if existing is not None:
            binding = _binding_row(conn, existing["binding_id"])
            if binding is not None and binding["binding_role"] == command.binding_role:
                return _replay_event(
                    conn,
                    existing,
                    _initial_event_command(command, existing["binding_id"]),
                )
            raise OwnershipIdempotencyConflict() from None
        raise OwnershipStateConflict() from None


def _validate_ownership_metadata(metadata) -> None:
    nodes = 0

    def walk(value):
        nonlocal nodes
        nodes += 1
        if nodes > MAX_METADATA_NODES:
            raise OwnershipValidationError("Metadata contains too many values.")
        if type(value) is dict:
            for key, child in value.items():
                normalized = unicodedata.normalize("NFKC", key).casefold()
                if any(ord(char) < 32 or ord(char) > 126 for char in key):
                    raise OwnershipValidationError("Metadata contains an invalid key.")
                semantic = re.sub(r"[-_.\s/:]+", "", normalized)
                if (
                    semantic in OWNERSHIP_SENSITIVE_METADATA_NAMES
                    or semantic.endswith("token")
                    or any(
                        semantic.startswith(prefix)
                        for prefix in OWNERSHIP_SENSITIVE_METADATA_PREFIXES
                    )
                ):
                    raise OwnershipValidationError(
                        "Metadata contains privacy-sensitive fields."
                    )
                walk(child)
        elif type(value) is list:
            for child in value:
                walk(child)

    walk(metadata)


def _validate_binding_event_command(command):
    if type(command) is not BindingEventCommand:
        raise OwnershipValidationError("Ownership event command is invalid.")
    validate_principal_id(command.principal_id)
    validate_binding_id(command.binding_id)
    _validate_account_id(command.user_id)
    if type(command.expected_event_version) is not int or command.expected_event_version < 2:
        raise OwnershipValidationError("Ownership event version is invalid.")
    _validate_event_fields(command)
    return command


def _validate_create_binding_command(command):
    if type(command) is not CreateBindingCommand:
        raise OwnershipValidationError("Ownership binding command is invalid.")
    validate_principal_id(command.principal_id)
    _validate_account_id(command.user_id)
    if command.binding_role not in BINDING_ROLES:
        raise OwnershipValidationError("Ownership binding role is invalid.")
    _validate_event_fields(_initial_event_command(command, "pab_" + "01" * 16))
    return command


def _validate_event_fields(command):
    if command.event_type not in BINDING_EVENT_TYPES:
        raise OwnershipValidationError("Ownership event type is invalid.")
    if command.actor_type not in BINDING_ACTOR_TYPES:
        raise OwnershipValidationError("Ownership event actor is invalid.")
    if not _event_transition_valid(
        command.event_type, command.prior_status, command.resulting_status
    ):
        raise OwnershipValidationError("Ownership event transition is invalid.")
    if type(command.reason_code) is not str or REASON_CODE_PATTERN.fullmatch(
        command.reason_code
    ) is None:
        raise OwnershipValidationError("Ownership event reason is invalid.")
    if command.approval_reference is not None and (
        type(command.approval_reference) is not str
        or APPROVAL_REFERENCE_PATTERN.fullmatch(command.approval_reference) is None
    ):
        raise OwnershipValidationError("Ownership approval reference is invalid.")
    _validate_idempotency_key(command.idempotency_key)
    _validate_timestamp(command.occurred_at)
    canonical_metadata(command.metadata)


def _validate_idempotency_key(value):
    if (
        type(value) is not str
        or not (16 <= len(value) <= 256)
        or value != value.strip()
        or any(unicodedata.category(char) == "Cc" for char in value)
    ):
        raise OwnershipValidationError("Ownership idempotency key is invalid.")
    return value


def _validate_timestamp(value):
    if type(value) is not str or len(value) != 25 or not value.endswith("+00:00"):
        raise OwnershipValidationError("Ownership timestamp is invalid.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise OwnershipValidationError("Ownership timestamp is invalid.") from exc
    if parsed.utcoffset() is None or parsed.isoformat(timespec="seconds") != value:
        raise OwnershipValidationError("Ownership timestamp is invalid.")
    return value


def _validate_account_id(value):
    if type(value) is not str or ACCOUNT_ID_PATTERN.fullmatch(value) is None:
        raise OwnershipValidationError("Account reference is invalid.")
    return value


def _initial_event_command(command, binding_id):
    return BindingEventCommand(
        principal_id=command.principal_id,
        binding_id=binding_id,
        user_id=command.user_id,
        expected_event_version=1,
        event_type="binding_activated",
        prior_status=None,
        resulting_status="active",
        actor_type=command.actor_type,
        reason_code=command.reason_code,
        approval_reference=command.approval_reference,
        idempotency_key=command.idempotency_key,
        occurred_at=command.occurred_at,
        metadata=command.metadata,
    )


def _command_fingerprint(command):
    return event_request_fingerprint(
        principal_id=command.principal_id,
        binding_id=command.binding_id,
        user_id=command.user_id,
        expected_event_version=command.expected_event_version,
        event_type=command.event_type,
        prior_status=command.prior_status,
        resulting_status=command.resulting_status,
        actor_type=command.actor_type,
        reason_code=command.reason_code,
        approval_reference=command.approval_reference,
        occurred_at=command.occurred_at,
        metadata=command.metadata,
    )


def _replay_event(conn, existing, command):
    binding = _binding_row(conn, existing["binding_id"])
    if (
        binding is None
        or binding["principal_id"] != existing["principal_id"]
        or binding["user_id"] != existing["user_id"]
    ):
        raise OwnershipStateConflict()
    stored_metadata = validate_metadata_document(
        existing["metadata_json"], field_name="event metadata"
    )
    durable_fingerprint = event_request_fingerprint(
        principal_id=existing["principal_id"],
        binding_id=existing["binding_id"],
        user_id=existing["user_id"],
        expected_event_version=existing["event_version"],
        event_type=existing["event_type"],
        prior_status=existing["prior_status"],
        resulting_status=existing["resulting_status"],
        actor_type=existing["actor_type"],
        reason_code=existing["reason_code"],
        approval_reference=existing["approval_reference"],
        occurred_at=existing["occurred_at"],
        metadata=stored_metadata,
    )
    if existing["request_fingerprint"] != durable_fingerprint:
        raise OwnershipStateConflict()
    if _command_fingerprint(command) != durable_fingerprint:
        raise OwnershipIdempotencyConflict()
    return _event_result(existing, account_reference="account-bound", replayed=True)


def _event_result(event, *, account_reference, replayed):
    return OwnershipEventResult(
        event_id=event["event_id"],
        principal_id=event["principal_id"],
        binding_id=event["binding_id"],
        account_reference=account_reference,
        event_version=event["event_version"],
        event_type=event["event_type"],
        prior_status=event["prior_status"],
        resulting_status=event["resulting_status"],
        occurred_at=event["occurred_at"],
        replayed=replayed,
    )


def _principal_row(conn, principal_id):
    return _fetch_dict(
        conn,
        "SELECT principal_id, environment_namespace, lifecycle_status, claim_policy "
        "FROM product_principals WHERE principal_id = ?",
        (principal_id,),
    )


def _binding_row(conn, binding_id):
    return _fetch_dict(
        conn,
        "SELECT binding_id, principal_id, user_id, environment_namespace, binding_role, "
        "binding_status, version, latest_event_version, created_at, updated_at, suspended_at "
        "FROM principal_account_bindings WHERE binding_id = ?",
        (binding_id,),
    )


def _event_row(conn, event_id):
    return _fetch_dict(
        conn,
        "SELECT event_id, principal_id, user_id, binding_id, environment_namespace, "
        "event_version, event_type, prior_status, resulting_status, actor_type, reason_code, "
        "approval_reference, idempotency_key, request_fingerprint, occurred_at, metadata_json "
        "FROM ownership_binding_events WHERE event_id = ?",
        (event_id,),
    )


def _existing_event(conn, principal_id, idempotency_key):
    return _fetch_dict(
        conn,
        "SELECT event_id, principal_id, user_id, binding_id, environment_namespace, "
        "event_version, event_type, prior_status, resulting_status, actor_type, reason_code, "
        "approval_reference, idempotency_key, request_fingerprint, occurred_at, metadata_json "
        "FROM ownership_binding_events WHERE principal_id = ? AND idempotency_key = ?",
        (principal_id, idempotency_key),
    )


def _require_bootstrap_database_state(conn):
    foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()
    if foreign_keys is None or foreign_keys[0] != 1:
        raise OwnershipStateConflict()
    try:
        from wahojobs.ownership_reconciliation import reconcile_ownership

        report = reconcile_ownership(conn)
    except Exception:
        raise OwnershipStateConflict() from None
    if type(report) is not dict or report.get("blocking") is not False:
        raise OwnershipStateConflict()


def _canonical_active_account(conn, user_id):
    from wahojobs.account_reconciliation import authoritative_account_row_valid

    account = _fetch_dict(
        conn,
        "SELECT user_id, lifecycle_status, row_version, created_at, updated_at, "
        "deletion_requested_at, deactivated_at FROM users WHERE user_id = ?",
        (user_id,),
    )
    if (
        account is None
        or account.get("lifecycle_status") != "active"
        or not authoritative_account_row_valid(account, expected_user_id=user_id)
    ):
        raise OwnershipStateConflict()
    return account


def _account_native_bootstrap_result(
    conn,
    *,
    user_id,
    environment_namespace,
    created,
):
    bindings = _fetch_dict_rows(
        conn,
        "SELECT binding_id, principal_id, user_id, environment_namespace, "
        "binding_role, binding_status, version, latest_event_version, "
        "created_at, updated_at, suspended_at, provenance_json "
        "FROM principal_account_bindings WHERE user_id = ? "
        "ORDER BY binding_id LIMIT 65",
        (user_id,),
    )
    if len(bindings) > 64:
        raise OwnershipStateConflict()
    owner_lineages = [
        binding
        for binding in bindings
        if binding["environment_namespace"] == environment_namespace
        and binding["binding_role"] == "owner"
    ]
    if not owner_lineages:
        return None
    if len(owner_lineages) != 1:
        raise OwnershipStateConflict()
    binding = owner_lineages[0]
    if binding["binding_status"] != "active":
        raise OwnershipStateConflict()

    principal = _fetch_dict(
        conn,
        "SELECT principal_id, environment_namespace, principal_type, lifecycle_status, "
        "claim_policy, exclusive_account_binding, version, created_at, updated_at, "
        "provenance_json FROM product_principals WHERE principal_id = ?",
        (binding["principal_id"],),
    )
    if (
        principal is None
        or principal["environment_namespace"] != environment_namespace
        or principal["principal_type"] != "account_native"
        or principal["lifecycle_status"] != "active"
        or principal["claim_policy"] != "account_native"
        or principal["exclusive_account_binding"] != 1
    ):
        raise OwnershipStateConflict()

    events = _fetch_dict_rows(
        conn,
        "SELECT event_id, event_version FROM ownership_binding_events "
        "WHERE binding_id = ? ORDER BY event_version, event_id LIMIT 129",
        (binding["binding_id"],),
    )
    if (
        not events
        or len(events) > 128
        or events[0]["event_version"] != 1
        or events[-1]["event_version"] != binding["latest_event_version"]
    ):
        raise OwnershipStateConflict()
    return AccountNativePrincipalBootstrapResult(
        principal_id=principal["principal_id"],
        binding_id=binding["binding_id"],
        initial_event_id=events[0]["event_id"],
        environment_namespace=environment_namespace,
        created=created,
    )


def _row_dict(description, row):
    return {item[0]: row[index] for index, item in enumerate(description)}


def _fetch_dict_rows(conn, sql, parameters):
    cursor = conn.execute(sql, parameters)
    return [_row_dict(cursor.description, row) for row in cursor]


def _fetch_dict(conn, sql, parameters):
    cursor = conn.execute(sql, parameters)
    row = cursor.fetchone()
    if row is None:
        return None
    return {description[0]: row[index] for index, description in enumerate(cursor.description)}


@contextmanager
def _ownership_transaction(conn):
    if conn.in_transaction:
        savepoint = "ownership_" + secrets.token_hex(8)
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            yield
        except BaseException:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
            raise
        else:
            conn.execute(f"RELEASE {savepoint}")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


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


def _random_id(prefix) -> str:
    return f"{prefix}_{secrets.token_hex(16)}"


def _validate_prefixed_id(value, field_name, prefix) -> str:
    pattern = re.compile(rf"^{re.escape(prefix)}_[0-9a-f]{{32}}$")
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise OwnershipValidationError(f"{field_name} is invalid.")
    payload = value[4:]
    if len(set(payload)) == 1:
        raise OwnershipValidationError(f"{field_name} is degenerate.")
    return value


def _table_exists(conn, name) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


def _legacy_classification(alias_value, sample_flags) -> str:
    if alias_value == "local_user":
        return "development"
    if sample_flags and all(sample_flags):
        return "sample"
    return "legacy"


def _safe_legacy_descriptor(alias_kind, classification) -> str:
    if classification == "development":
        return "development legacy owner"
    if classification == "sample":
        return f"sample {alias_kind.replace('_', ' ')}"
    if classification == "malformed":
        return f"malformed {alias_kind.replace('_', ' ')}"
    return f"legacy {alias_kind.replace('_', ' ')}"


def _inject(callback, point):
    if callback is not None:
        callback(point)

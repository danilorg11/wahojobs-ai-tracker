"""Dormant SQLite repository services for persistent product profiles.

The caller owns the SQLite connection and any outer transaction.  This module
does not open databases, install schemas, or integrate with product runtime.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass, field
from typing import Callable

from wahojobs.persistent_profile_canonical_v2_schema import (
    attest_persistent_profile_canonical_v2_schema,
)
from wahojobs.persistent_profiles import (
    MIGRATION_005_CAPABILITIES,
    AppendProfileRevisionCommand,
    CreatePersistentProfileCommand,
    CurrentProfileSummary,
    PersistentProfileDomainError,
    PersistentProfileSchemaCapabilities,
    ProfileCreatedResult,
    ProfileHistoryItem,
    ProfileRevisionResult,
    PurgePersistentProfileCommand,
    PurgeResult,
    TrustedPrincipalContext,
    classify_replay,
    generate_revision_id,
    generate_source_id,
    source_bundle_hash,
    source_content_hash,
    validate_profile_id,
)
from wahojobs.profiles.canonical_v2 import (
    CanonicalProfileV2Error,
    SCHEMA_VERSION as CANONICAL_PROFILE_V2_SCHEMA_VERSION,
    canonical_profile_v2_json_bytes,
    parse_canonical_profile_v2_json,
)


DEFAULT_HISTORY_PAGE_SIZE = 25
MAX_HISTORY_PAGE_SIZE = 100
MAX_HISTORY_RESPONSE_BYTES = 1_048_576
_LOWERCASE_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ACCOUNT_ID = re.compile(r"^usr_[0-9a-f]{32}$")
_PRINCIPAL_ID = re.compile(r"^prn_[0-9a-f]{32}$")
_BINDING_ID = re.compile(r"^pab_[0-9a-f]{32}$")
_BINDING_EVENT_ID = re.compile(r"^obe_[0-9a-f]{32}$")
_ENVIRONMENT = re.compile(r"^[a-z0-9_.-]{1,64}$")

CREATE_FAILURE_BOUNDARIES = (
    "create.after_profile_insert",
    "create.after_source_insert",
    "create.after_revision_insert",
    "create.after_view_verification",
    "create.before_finish",
)
APPEND_FAILURE_BOUNDARIES = (
    "append.after_source_insert",
    "append.after_revision_insert",
    "append.after_view_verification",
    "append.before_finish",
)
PURGE_FAILURE_BOUNDARIES = (
    "purge.after_container_delete",
    "purge.after_cascade_verification",
    "purge.before_finish",
)

_BUSY_CODES = {
    sqlite3.SQLITE_BUSY,
    sqlite3.SQLITE_LOCKED,
}


def _error(reason_code: str) -> PersistentProfileDomainError:
    return PersistentProfileDomainError(reason_code)


class PersistentProfileRepositoryDefiniteRollback(PersistentProfileDomainError):
    """An operational repository failure with a proved no-commit outcome."""


class PersistentProfileRepositoryOutcomeUncertain(PersistentProfileDomainError):
    """A repository invocation whose commit outcome cannot be proved locally."""

    def __init__(self):
        super().__init__("internal_consistency_failure")


_PROFILE_CREATE_LINEAGE_ISSUER = object()


@dataclass(frozen=True, slots=True, repr=False, init=False)
class TrustedProfileCreateLineage:
    """Server-private identity of one exact account-native ownership lineage."""

    account_id: str
    account_row_version: int
    principal_id: str
    principal_version: int
    environment_namespace: str
    binding_id: str
    binding_version: int
    latest_event_version: int
    latest_event_id: str
    lineage_sha256: str
    _issuer: object = field(repr=False, compare=False)

    def _validate(self):
        if (
            self._issuer is not _PROFILE_CREATE_LINEAGE_ISSUER
            or
            type(self.account_id) is not str
            or _ACCOUNT_ID.fullmatch(self.account_id) is None
            or type(self.account_row_version) is not int
            or self.account_row_version < 1
            or type(self.principal_id) is not str
            or _PRINCIPAL_ID.fullmatch(self.principal_id) is None
            or type(self.principal_version) is not int
            or self.principal_version < 1
            or type(self.environment_namespace) is not str
            or _ENVIRONMENT.fullmatch(self.environment_namespace) is None
            or type(self.binding_id) is not str
            or _BINDING_ID.fullmatch(self.binding_id) is None
            or type(self.binding_version) is not int
            or self.binding_version < 1
            or type(self.latest_event_version) is not int
            or self.latest_event_version != self.binding_version
            or type(self.latest_event_id) is not str
            or _BINDING_EVENT_ID.fullmatch(self.latest_event_id) is None
            or type(self.lineage_sha256) is not str
            or _LOWERCASE_SHA256.fullmatch(self.lineage_sha256) is None
        ):
            raise _error("ineligible_principal")

    def artifact_binding(self, session_id, purpose):
        return (
            self.account_id,
            session_id,
            self.environment_namespace,
            self.principal_id,
            self.binding_id,
            self.binding_version,
            self.latest_event_version,
            self.latest_event_id,
            self.lineage_sha256,
            purpose,
        )

    def __repr__(self):
        return "TrustedProfileCreateLineage(<redacted>)"

    def __reduce_ex__(self, _protocol):
        raise TypeError("trusted_profile_create_lineage_not_serializable")


def _issue_profile_create_lineage(**values):
    lineage = object.__new__(TrustedProfileCreateLineage)
    for name, value in {
        **values,
        "_issuer": _PROFILE_CREATE_LINEAGE_ISSUER,
    }.items():
        object.__setattr__(lineage, name, value)
    lineage._validate()
    return lineage


def _require_connection(connection) -> sqlite3.Connection:
    if not isinstance(connection, sqlite3.Connection):
        raise _error("invalid_command")
    return connection


def _row_dicts(connection, sql, parameters):
    cursor = connection.execute(sql, parameters)
    names = tuple(item[0] for item in cursor.description)
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def capture_profile_create_lineage(
    connection,
    *,
    account_id,
    environment_namespace,
    principal_id,
):
    """Capture one already-authorized ownership lineage without mutating it."""

    connection = _require_connection(connection)
    if (
        type(account_id) is not str
        or _ACCOUNT_ID.fullmatch(account_id) is None
        or type(environment_namespace) is not str
        or _ENVIRONMENT.fullmatch(environment_namespace) is None
        or type(principal_id) is not str
        or _PRINCIPAL_ID.fullmatch(principal_id) is None
    ):
        raise _error("ineligible_principal")
    accounts = _row_dicts(
        connection,
        "SELECT user_id, lifecycle_status, row_version, created_at, updated_at, "
        "deletion_requested_at, deactivated_at FROM users WHERE user_id=? LIMIT 2",
        (account_id,),
    )
    principals = _row_dicts(
        connection,
        "SELECT principal_id, environment_namespace, principal_type, lifecycle_status, "
        "claim_policy, exclusive_account_binding, version, created_at, updated_at, "
        "provenance_json FROM product_principals WHERE principal_id=? LIMIT 2",
        (principal_id,),
    )
    bindings = _row_dicts(
        connection,
        "SELECT binding_id, principal_id, user_id, environment_namespace, binding_role, "
        "binding_status, version, latest_event_version, created_at, updated_at, "
        "suspended_at, provenance_json FROM principal_account_bindings "
        "WHERE principal_id=? AND user_id=? AND environment_namespace=? "
        "AND binding_role='owner' AND binding_status='active' ORDER BY binding_id LIMIT 2",
        (principal_id, account_id, environment_namespace),
    )
    active_owners = _row_dicts(
        connection,
        "SELECT binding_id, user_id, environment_namespace FROM principal_account_bindings "
        "WHERE principal_id=? AND binding_role='owner' AND binding_status='active' "
        "ORDER BY binding_id LIMIT 2",
        (principal_id,),
    )
    if len(accounts) != 1 or len(principals) != 1 or len(bindings) != 1:
        raise _error("ineligible_principal")
    account = accounts[0]
    principal = principals[0]
    binding = bindings[0]
    if (
        account["lifecycle_status"] != "active"
        or account["deletion_requested_at"] is not None
        or account["deactivated_at"] is not None
        or principal["environment_namespace"] != environment_namespace
        or principal["principal_type"] != "account_native"
        or principal["lifecycle_status"] != "active"
        or principal["claim_policy"] != "account_native"
        or principal["exclusive_account_binding"] != 1
        or binding["binding_id"] is None
        or binding["version"] != binding["latest_event_version"]
        or len(active_owners) != 1
        or active_owners[0]
        != {
            "binding_id": binding["binding_id"],
            "user_id": account_id,
            "environment_namespace": environment_namespace,
        }
    ):
        raise _error("ineligible_principal")
    events = _row_dicts(
        connection,
        "SELECT event_id, principal_id, user_id, binding_id, environment_namespace, "
        "event_version, event_type, prior_status, resulting_status, actor_type, "
        "reason_code, approval_reference, idempotency_key, request_fingerprint, "
        "occurred_at, metadata_json FROM ownership_binding_events WHERE binding_id=? "
        "ORDER BY event_version LIMIT 129",
        (binding["binding_id"],),
    )
    if (
        not events
        or len(events) > 128
        or tuple(event["event_version"] for event in events)
        != tuple(range(1, binding["latest_event_version"] + 1))
        or any(
            event["principal_id"] != principal_id
            or event["user_id"] != account_id
            or event["binding_id"] != binding["binding_id"]
            or event["environment_namespace"] != environment_namespace
            for event in events
        )
        or events[-1]["resulting_status"] != "active"
    ):
        raise _error("ineligible_principal")
    lineage_sha256 = hashlib.sha256(
        json.dumps(
            {
                "version": 1,
                "account": account,
                "principal": principal,
                "binding": binding,
                "events": events,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return _issue_profile_create_lineage(
        account_id=account_id,
        account_row_version=account["row_version"],
        principal_id=principal_id,
        principal_version=principal["version"],
        environment_namespace=environment_namespace,
        binding_id=binding["binding_id"],
        binding_version=binding["version"],
        latest_event_version=binding["latest_event_version"],
        latest_event_id=events[-1]["event_id"],
        lineage_sha256=lineage_sha256,
    )


def _sqlite_reason(error_code: int | None) -> str:
    if type(error_code) is int and (error_code & 0xFF) in _BUSY_CODES:
        return "temporary_contention"
    return "internal_consistency_failure"


def _validated_durable_profile(
    *,
    profile_id,
    canonical_schema_version,
    structured_profile_json,
    structured_profile_sha256,
) -> tuple[dict, bytes]:
    """Validate one exact canonical V2 snapshot loaded from durable storage."""
    validation_failed = False
    profile = None
    canonical_bytes = b""
    stored_bytes = b""
    try:
        if (
            type(profile_id) is not str
            or canonical_schema_version != CANONICAL_PROFILE_V2_SCHEMA_VERSION
            or type(structured_profile_json) is not str
            or type(structured_profile_sha256) is not str
            or _LOWERCASE_SHA256.fullmatch(structured_profile_sha256) is None
        ):
            validation_failed = True
        else:
            stored_bytes = structured_profile_json.encode("utf-8")
            profile = parse_canonical_profile_v2_json(stored_bytes)
            canonical_bytes = canonical_profile_v2_json_bytes(profile)
    except (CanonicalProfileV2Error, UnicodeEncodeError):
        validation_failed = True
        profile = None
        canonical_bytes = b""
        stored_bytes = b""
    if not validation_failed:
        validation_failed = (
            profile["identity"]["profile_id"] != profile_id
            or stored_bytes != canonical_bytes
            or not hmac.compare_digest(
                hashlib.sha256(canonical_bytes).hexdigest(),
                structured_profile_sha256,
            )
        )
    if validation_failed:
        raise _error("internal_consistency_failure")
    return profile, canonical_bytes


def _serialize_history_page_for_size(
    items,
    *,
    include_structured_profile: bool,
) -> bytes:
    """Serialize the exact compact trusted history-page array used for sizing."""
    payload = [
        item.trusted_dict(include_structured_profile=include_structured_profile)
        for item in items
    ]
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (UnicodeEncodeError, ValueError, TypeError):
        raise _error("internal_consistency_failure")


@dataclass(frozen=True)
class _TransactionState:
    nested: bool
    savepoint: str | None


class _MutationController:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.state: _TransactionState | None = None
        self.commit_started = False
        self.commit_completed = False

    def begin(self) -> None:
        nested = self.connection.in_transaction
        savepoint = None
        if nested:
            savepoint = "pprepo_" + secrets.token_hex(12)
            self.connection.execute(f'SAVEPOINT "{savepoint}"')
        else:
            self.connection.execute("BEGIN IMMEDIATE")
        self.state = _TransactionState(nested=nested, savepoint=savepoint)

    def succeed(self) -> None:
        if self.state is None:
            raise RuntimeError("transaction controller not started")
        self.commit_started = True
        if self.state.nested:
            self.connection.execute(f'RELEASE SAVEPOINT "{self.state.savepoint}"')
        else:
            self.connection.commit()
        self.commit_completed = True

    def fail(self) -> bool:
        if self.state is None:
            # No transaction or commit was started by this controller.  In
            # particular, do not probe a connection that may have failed while
            # answering the initial in_transaction query.
            return True
        if self.commit_completed:
            return False
        try:
            if self.state.nested:
                self.connection.execute(
                    f'ROLLBACK TO SAVEPOINT "{self.state.savepoint}"'
                )
                self.connection.execute(
                    f'RELEASE SAVEPOINT "{self.state.savepoint}"'
                )
            else:
                if not self.connection.in_transaction:
                    return not self.commit_started
                self.connection.rollback()
        except sqlite3.Error:
            # The public error remains generic; caller-owned outer work is never
            # intentionally committed or rolled back here.
            return False
        return not self.connection.in_transaction if not self.state.nested else True


class PersistentProfileRepository:
    """Caller-connection repository for the installed Migration-005 schema."""

    def __init__(
        self,
        *,
        capabilities: PersistentProfileSchemaCapabilities = MIGRATION_005_CAPABILITIES,
        _failure_injector: Callable[[str], None] | None = None,
    ):
        if type(capabilities) is not PersistentProfileSchemaCapabilities:
            raise _error("schema_capability_unavailable")
        if _failure_injector is not None and not callable(_failure_injector):
            raise _error("invalid_command")
        self._capabilities = capabilities
        self._failure_injector = _failure_injector

    def _hook(self, boundary: str) -> None:
        if self._failure_injector is not None:
            self._failure_injector(boundary)

    def _attest(self, connection: sqlite3.Connection) -> None:
        if self._capabilities != MIGRATION_005_CAPABILITIES:
            raise _error("schema_capability_unavailable")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise _error("schema_capability_unavailable")
        attestation = attest_persistent_profile_canonical_v2_schema(connection)
        if (
            attestation.get("state") != "correctly_installed"
            or attestation.get("migration_version")
            != MIGRATION_005_CAPABILITIES.migration_version
            or not attestation.get("migration_marker_present")
        ):
            raise _error("schema_capability_unavailable")

    def _mutate(self, connection, operation):
        connection = _require_connection(connection)
        controller = _MutationController(connection)
        failure = None
        try:
            controller.begin()
            result = operation(connection)
            controller.succeed()
        except PersistentProfileDomainError as exc:
            if controller.fail():
                failure = exc
            else:
                failure = PersistentProfileRepositoryOutcomeUncertain()
        except sqlite3.Error as exc:
            reason = _sqlite_reason(getattr(exc, "sqlite_errorcode", None))
            if controller.fail():
                failure = PersistentProfileRepositoryDefiniteRollback(reason)
            else:
                failure = PersistentProfileRepositoryOutcomeUncertain()
        except Exception:
            if controller.fail():
                failure = PersistentProfileRepositoryDefiniteRollback(
                    "internal_consistency_failure"
                )
            else:
                failure = PersistentProfileRepositoryOutcomeUncertain()
        except BaseException:
            controller.fail()
            raise
        if failure is not None:
            failure.__cause__ = None
            failure.__context__ = None
            raise failure from None
        return result

    @staticmethod
    def _structured_bytes(command) -> bytes:
        return canonical_profile_v2_json_bytes(command.trusted_structured_profile())

    @staticmethod
    def _find_revision_replay(connection, principal_id, idempotency_key):
        return connection.execute(
            "SELECT revision_id, profile_id, revision_number, revision_kind, "
            "lifecycle_status, created_at, request_fingerprint "
            "FROM product_profile_revisions "
            "WHERE principal_id=? AND idempotency_key=?",
            (principal_id, idempotency_key),
        ).fetchone()

    @staticmethod
    def _require_profile_relationship(connection, reference):
        row = connection.execute(
            "SELECT profile_id, principal_id, environment_namespace, created_at "
            "FROM product_profiles WHERE profile_id=? AND principal_id=? "
            "AND environment_namespace=?",
            (
                reference.profile_id,
                reference.principal_id,
                reference.environment_namespace,
            ),
        ).fetchone()
        if row is None:
            raise _error("profile_not_found")
        return row

    @staticmethod
    def _require_context_relationship(connection, principal, profile_id=None):
        if type(principal) is not TrustedPrincipalContext:
            raise _error("ineligible_principal")
        if profile_id is not None:
            validate_profile_id(profile_id)
            row = connection.execute(
                "SELECT profile_id, principal_id, environment_namespace FROM product_profiles "
                "WHERE profile_id=? AND principal_id=? AND environment_namespace=?",
                (profile_id, principal.principal_id, principal.environment_namespace),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT profile_id, principal_id, environment_namespace FROM product_profiles "
                "WHERE principal_id=? AND environment_namespace=?",
                (principal.principal_id, principal.environment_namespace),
            ).fetchone()
        if row is None:
            raise _error("profile_not_found")
        return row

    @staticmethod
    def _require_durable_principal(connection, principal, *, full: bool) -> None:
        if type(principal) is not TrustedPrincipalContext:
            raise _error("ineligible_principal")
        row = connection.execute(
            "SELECT environment_namespace, principal_type, lifecycle_status, "
            "claim_policy, exclusive_account_binding FROM product_principals "
            "WHERE principal_id=?",
            (principal.principal_id,),
        ).fetchone()
        if row is None or row[0] != principal.environment_namespace:
            raise _error("ineligible_principal")
        if not full:
            return
        durable = {
            "environment": row[0],
            "principal_type": row[1],
            "lifecycle": row[2],
            "claim_policy": row[3],
            "exclusive": row[4],
        }
        if (
            durable["principal_type"] != principal.principal_type
            or durable["lifecycle"] != "active"
            or durable["lifecycle"] != principal.lifecycle_status
            or durable["claim_policy"] != principal.claim_policy
            or bool(durable["exclusive"]) != principal.exclusive_account_binding
        ):
            raise _error("ineligible_principal")
        bindings = connection.execute(
            "SELECT binding.binding_status, binding.binding_role, "
            "binding.environment_namespace, account.lifecycle_status "
            "FROM principal_account_bindings binding "
            "JOIN users account ON account.user_id=binding.user_id "
            "WHERE binding.principal_id=? AND binding.binding_role='owner' "
            "AND binding.binding_status='active'",
            (principal.principal_id,),
        ).fetchall()
        if principal.eligibility_mode == "account_native":
            if not (
                durable["principal_type"] == "account_native"
                and durable["claim_policy"] == "account_native"
                and durable["exclusive"] == 1
                and principal.active_owner_binding is True
                and len(bindings) == 1
                and bindings[0][1] == "owner"
                and bindings[0][2] == principal.environment_namespace
                and bindings[0][3] == "active"
            ):
                raise _error("ineligible_principal")
        elif not (
            durable["principal_type"] == "development"
            and durable["claim_policy"] == "nonclaimable"
            and durable["exclusive"] == 0
            and principal.environment_namespace in {"development", "test"}
            and principal.active_owner_binding in {False, None}
            and not bindings
        ):
            raise _error("ineligible_principal")

    @staticmethod
    def _generate_revision_id(connection) -> str:
        return generate_revision_id(
            is_available=lambda candidate: connection.execute(
                "SELECT 1 FROM product_profile_revisions WHERE revision_id=?",
                (candidate,),
            ).fetchone()
            is None
        )

    @staticmethod
    def _generate_source_id(connection) -> str:
        return generate_source_id(
            is_available=lambda candidate: connection.execute(
                "SELECT 1 FROM product_profile_sources WHERE source_id=?", (candidate,)
            ).fetchone()
            is None
        )

    def _insert_sources(self, connection, command, revision_id, profile_id):
        source_ids = []
        for ordinal, source in enumerate(command.sources, start=1):
            if type(command) is CreatePersistentProfileCommand:
                if (
                    len(command.source_ids) != len(command.sources)
                    or len(command.source_content_sha256s) != len(command.sources)
                    or source_content_hash(source)
                    != command.source_content_sha256s[ordinal - 1]
                ):
                    raise _error("internal_consistency_failure")
                source_id = command.source_ids[ordinal - 1]
            else:
                source_id = self._generate_source_id(connection)
            connection.execute(
                "INSERT INTO product_profile_sources "
                "(source_id, revision_id, profile_id, principal_id, "
                "environment_namespace, source_ordinal, source_type, source_format, "
                "source_content, source_content_sha256, source_schema_version, "
                "parser_version, accepted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source_id,
                    revision_id,
                    profile_id,
                    command.principal.principal_id,
                    command.principal.environment_namespace,
                    ordinal,
                    source.source_type,
                    source.source_format,
                    source.content,
                    source_content_hash(source),
                    source.source_schema_version,
                    source.parser_version,
                    source.confirmed_at,
                ),
            )
            source_ids.append(source_id)
            prefix = "create" if type(command) is CreatePersistentProfileCommand else "append"
            self._hook(f"{prefix}.after_source_insert")
        return tuple(source_ids)

    @staticmethod
    def _verify_sources(connection, command, revision_id) -> None:
        rows = connection.execute(
            "SELECT source_ordinal, source_type, source_format, source_content, "
            "source_content_sha256, source_schema_version, parser_version, accepted_at "
            "FROM product_profile_sources WHERE revision_id=? ORDER BY source_ordinal",
            (revision_id,),
        ).fetchall()
        if len(rows) != len(command.sources):
            raise _error("internal_consistency_failure")
        for ordinal, (row, source) in enumerate(zip(rows, command.sources), start=1):
            if tuple(row) != (
                ordinal,
                source.source_type,
                source.source_format,
                source.content,
                source_content_hash(source),
                source.source_schema_version,
                source.parser_version,
                source.confirmed_at,
            ):
                raise _error("internal_consistency_failure")
        if source_bundle_hash(command.sources) != command.source_bundle_sha256:
            raise _error("internal_consistency_failure")

    @staticmethod
    def _require_create_identities_available(connection, command):
        if (
            len(command.source_ids) != len(command.sources)
            or len(set(command.source_ids)) != len(command.source_ids)
            or connection.execute(
                "SELECT 1 FROM product_profiles WHERE profile_id=?",
                (command.profile_id,),
            ).fetchone()
            is not None
            or connection.execute(
                "SELECT 1 FROM product_profile_revisions WHERE revision_id=?",
                (command.revision_id,),
            ).fetchone()
            is not None
        ):
            raise _error("internal_consistency_failure")
        for source_id in command.source_ids:
            if connection.execute(
                "SELECT 1 FROM product_profile_sources WHERE source_id=?",
                (source_id,),
            ).fetchone() is not None:
                raise _error("internal_consistency_failure")

    @staticmethod
    def _require_exact_create_lineage(connection, command, account_lineage):
        if (
            type(account_lineage) is not TrustedProfileCreateLineage
            or getattr(account_lineage, "_issuer", None)
            is not _PROFILE_CREATE_LINEAGE_ISSUER
        ):
            raise _error("ineligible_principal")
        if (
            account_lineage.principal_id != command.principal.principal_id
            or account_lineage.environment_namespace
            != command.principal.environment_namespace
        ):
            raise _error("ineligible_principal")
        current = capture_profile_create_lineage(
            connection,
            account_id=account_lineage.account_id,
            environment_namespace=account_lineage.environment_namespace,
            principal_id=account_lineage.principal_id,
        )
        if current != account_lineage:
            raise _error("ineligible_principal")

    @staticmethod
    def _verify_current(connection, profile_id, expected):
        row = connection.execute(
            "SELECT current_revision_id, current_revision_number, current_revision_kind, "
            "lifecycle_status, canonical_schema_version, structured_profile_sha256, revised_at "
            "FROM current_product_profiles WHERE profile_id=?",
            (profile_id,),
        ).fetchone()
        if row is None or tuple(row) != expected:
            raise _error("internal_consistency_failure")

    def create(self, connection, command):
        return self._create(connection, command, account_lineage=None)

    def create_account_native(self, connection, command, *, account_lineage):
        if (
            type(command) is not CreatePersistentProfileCommand
            or command.principal.eligibility_mode != "account_native"
        ):
            raise _error("invalid_command")
        self._require_exact_create_lineage_authority(command, account_lineage)
        return self._create(connection, command, account_lineage=account_lineage)

    @staticmethod
    def _require_exact_create_lineage_authority(command, account_lineage):
        if (
            type(account_lineage) is not TrustedProfileCreateLineage
            or getattr(account_lineage, "_issuer", None)
            is not _PROFILE_CREATE_LINEAGE_ISSUER
            or account_lineage.principal_id != command.principal.principal_id
            or account_lineage.environment_namespace
            != command.principal.environment_namespace
        ):
            raise _error("ineligible_principal")

    def _create(self, connection, command, *, account_lineage):
        if type(command) is not CreatePersistentProfileCommand:
            raise _error("invalid_command")

        def operation(conn):
            self._attest(conn)
            if account_lineage is not None:
                self._require_exact_create_lineage(conn, command, account_lineage)
            self._require_durable_principal(conn, command.principal, full=True)
            principal_id, idempotency_key = command.idempotency_scope()
            replay = self._find_revision_replay(conn, principal_id, idempotency_key)
            if replay is not None:
                if classify_replay(replay[6], command.request_fingerprint) != "exact_replay":
                    raise _error("idempotency_conflict")
                if replay[3] != "initial" or replay[2] != 1:
                    raise _error("internal_consistency_failure")
                return ProfileCreatedResult(
                    replay[1], replay[0], replay[2], replay[4], replay[5], replayed=True
                )
            if conn.execute(
                "SELECT 1 FROM product_profiles WHERE principal_id=?", (principal_id,)
            ).fetchone() is not None:
                raise _error("profile_already_exists")
            self._require_create_identities_available(conn, command)
            structured = self._structured_bytes(command)
            if hashlib.sha256(structured).hexdigest() != command.structured_profile_sha256:
                raise _error("internal_consistency_failure")
            revision_id = command.revision_id
            profile_created_at = min(
                [command.accepted_at, *(source.confirmed_at for source in command.sources)]
            )
            conn.execute(
                "INSERT INTO product_profiles "
                "(profile_id, principal_id, environment_namespace, initial_revision_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    command.profile_id,
                    principal_id,
                    command.principal.environment_namespace,
                    revision_id,
                    profile_created_at,
                ),
            )
            self._hook("create.after_profile_insert")
            self._insert_sources(conn, command, revision_id, command.profile_id)
            conn.execute(
                "INSERT INTO product_profile_revisions "
                "(revision_id, profile_id, principal_id, environment_namespace, revision_number, "
                "previous_revision_id, correction_of_revision_id, revision_kind, lifecycle_status, "
                "canonical_schema_version, structured_profile_json, structured_profile_sha256, "
                "source_count, source_bundle_sha256, normalizer_version, reviewer_version, actor_type, "
                "reason_code, idempotency_key, request_fingerprint, created_at) "
                "VALUES (?, ?, ?, ?, 1, NULL, NULL, 'initial', 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision_id,
                    command.profile_id,
                    principal_id,
                    command.principal.environment_namespace,
                    command.canonical_schema_version,
                    structured.decode("utf-8"),
                    command.structured_profile_sha256,
                    len(command.sources),
                    command.source_bundle_sha256,
                    command.normalizer_version,
                    command.reviewer_version,
                    command.actor_type,
                    command.reason_code,
                    idempotency_key,
                    command.request_fingerprint,
                    command.accepted_at,
                ),
            )
            self._hook("create.after_revision_insert")
            self._verify_sources(conn, command, revision_id)
            self._verify_current(
                conn,
                command.profile_id,
                (
                    revision_id,
                    1,
                    "initial",
                    "active",
                    command.canonical_schema_version,
                    command.structured_profile_sha256,
                    command.accepted_at,
                ),
            )
            self._hook("create.after_view_verification")
            self._hook("create.before_finish")
            return ProfileCreatedResult(
                command.profile_id, revision_id, 1, "active", command.accepted_at
            )

        return self._mutate(connection, operation)

    def append(self, connection, command):
        if type(command) is not AppendProfileRevisionCommand:
            raise _error("invalid_command")

        def operation(conn):
            self._attest(conn)
            profile_row = self._require_profile_relationship(conn, command.profile)
            principal_id, idempotency_key = command.idempotency_scope()
            replay = self._find_revision_replay(conn, principal_id, idempotency_key)
            if replay is not None:
                if classify_replay(replay[6], command.request_fingerprint) != "exact_replay":
                    raise _error("idempotency_conflict")
                if replay[3] == "initial":
                    raise _error("internal_consistency_failure")
                return ProfileRevisionResult(
                    replay[1], replay[0], replay[2], replay[3], replay[4], replay[5], replayed=True
                )
            current = conn.execute(
                "SELECT revision_id, revision_number, revision_kind, lifecycle_status, "
                "canonical_schema_version, structured_profile_json, structured_profile_sha256, created_at "
                "FROM product_profile_revisions WHERE profile_id=? "
                "ORDER BY revision_number DESC LIMIT 1",
                (command.profile.profile_id,),
            ).fetchone()
            if current is None:
                raise _error("internal_consistency_failure")
            if current[1] != command.expected_current_revision_number:
                raise _error("stale_revision")
            if current[3] == "deletion_requested":
                raise _error("lifecycle_conflict")
            full_eligibility = command.revision_kind != "deletion_request"
            self._require_durable_principal(conn, command.principal, full=full_eligibility)
            resulting_lifecycle = current[3]
            if command.revision_kind == "archive":
                if current[3] != "active":
                    raise _error("lifecycle_conflict")
                resulting_lifecycle = "archived"
            elif command.revision_kind == "reactivate":
                if current[3] != "archived":
                    raise _error("lifecycle_conflict")
                resulting_lifecycle = "active"
            elif command.revision_kind == "deletion_request":
                if current[3] not in {"active", "archived"}:
                    raise _error("lifecycle_conflict")
                resulting_lifecycle = "deletion_requested"
            expected_marker = (
                "preserve_current"
                if command.revision_kind in {"edit", "correction"}
                else resulting_lifecycle
            )
            if command.resulting_lifecycle != expected_marker:
                raise _error("lifecycle_conflict")
            structured = self._structured_bytes(command)
            if hashlib.sha256(structured).hexdigest() != command.structured_profile_sha256:
                raise _error("internal_consistency_failure")
            if command.accepted_at < current[7]:
                raise _error("invalid_command")
            if any(source.confirmed_at < profile_row[3] for source in command.sources):
                raise _error("invalid_command")
            if command.revision_kind in {"archive", "reactivate", "deletion_request"}:
                if (
                    structured.decode("utf-8") != current[5]
                    or command.structured_profile_sha256 != current[6]
                    or command.canonical_schema_version != current[4]
                ):
                    raise _error("lifecycle_conflict")
            if command.revision_kind == "correction":
                target = conn.execute(
                    "SELECT revision_number FROM product_profile_revisions "
                    "WHERE revision_id=? AND profile_id=?",
                    (command.correction_of_revision_id, command.profile.profile_id),
                ).fetchone()
                if target is None or target[0] >= current[1] + 1:
                    raise _error("invalid_command")
            revision_id = self._generate_revision_id(conn)
            self._insert_sources(conn, command, revision_id, command.profile.profile_id)
            revision_number = current[1] + 1
            conn.execute(
                "INSERT INTO product_profile_revisions "
                "(revision_id, profile_id, principal_id, environment_namespace, revision_number, "
                "previous_revision_id, correction_of_revision_id, revision_kind, lifecycle_status, "
                "canonical_schema_version, structured_profile_json, structured_profile_sha256, "
                "source_count, source_bundle_sha256, normalizer_version, reviewer_version, actor_type, "
                "reason_code, idempotency_key, request_fingerprint, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision_id,
                    command.profile.profile_id,
                    principal_id,
                    command.principal.environment_namespace,
                    revision_number,
                    current[0],
                    command.correction_of_revision_id,
                    command.revision_kind,
                    resulting_lifecycle,
                    command.canonical_schema_version,
                    structured.decode("utf-8"),
                    command.structured_profile_sha256,
                    len(command.sources),
                    command.source_bundle_sha256,
                    command.normalizer_version,
                    command.reviewer_version,
                    command.actor_type,
                    command.reason_code,
                    idempotency_key,
                    command.request_fingerprint,
                    command.accepted_at,
                ),
            )
            self._hook("append.after_revision_insert")
            self._verify_sources(conn, command, revision_id)
            self._verify_current(
                conn,
                command.profile.profile_id,
                (
                    revision_id,
                    revision_number,
                    command.revision_kind,
                    resulting_lifecycle,
                    command.canonical_schema_version,
                    command.structured_profile_sha256,
                    command.accepted_at,
                ),
            )
            self._hook("append.after_view_verification")
            self._hook("append.before_finish")
            return ProfileRevisionResult(
                command.profile.profile_id,
                revision_id,
                revision_number,
                command.revision_kind,
                resulting_lifecycle,
                command.accepted_at,
            )

        return self._mutate(connection, operation)

    def read_current(
        self,
        connection,
        principal,
        *,
        profile_id: str | None = None,
        include_structured_profile: bool = False,
    ):
        connection = _require_connection(connection)
        if type(include_structured_profile) is not bool:
            raise _error("invalid_command")
        failure = None
        try:
            self._attest(connection)
            relationship = self._require_context_relationship(
                connection, principal, profile_id
            )
            row = connection.execute(
                "SELECT profile_id, principal_id, environment_namespace, "
                "current_revision_id, current_revision_number, lifecycle_status, "
                "canonical_schema_version, structured_profile_json, "
                "structured_profile_sha256, revised_at "
                "FROM current_product_profiles WHERE profile_id=?",
                (relationship[0],),
            ).fetchone()
            if row is None:
                raise _error("profile_not_found")
            if tuple(row[:3]) != tuple(relationship):
                raise _error("internal_consistency_failure")
            _, structured_bytes = _validated_durable_profile(
                profile_id=row[0],
                canonical_schema_version=row[6],
                structured_profile_json=row[7],
                structured_profile_sha256=row[8],
            )
            result = CurrentProfileSummary.from_trusted(
                profile_id=row[0],
                revision_id=row[3],
                revision_number=row[4],
                lifecycle_status=row[5],
                structured_profile_json=(
                    structured_bytes if include_structured_profile else None
                ),
                updated_at=row[9],
            )
            return result
        except PersistentProfileDomainError:
            raise
        except sqlite3.Error as exc:
            failure = _sqlite_reason(getattr(exc, "sqlite_errorcode", None))
        except Exception:
            failure = "internal_consistency_failure"
        if failure is not None:
            raise _error(failure)

    def read_history(
        self,
        connection,
        principal,
        *,
        profile_id: str | None = None,
        page_size: int = DEFAULT_HISTORY_PAGE_SIZE,
        before_revision_number: int | None = None,
        include_structured_profile: bool = False,
    ) -> tuple[ProfileHistoryItem, ...]:
        connection = _require_connection(connection)
        if type(page_size) is not int or not 1 <= page_size <= MAX_HISTORY_PAGE_SIZE:
            raise _error("invalid_command")
        if before_revision_number is not None and (
            type(before_revision_number) is not int or before_revision_number < 1
        ):
            raise _error("invalid_command")
        if type(include_structured_profile) is not bool:
            raise _error("invalid_command")
        failure = None
        try:
            self._attest(connection)
            relationship = self._require_context_relationship(
                connection, principal, profile_id
            )
            sql = (
                "SELECT profile_id, principal_id, environment_namespace, revision_id, "
                "revision_number, revision_kind, lifecycle_status, created_at, "
                "canonical_schema_version, structured_profile_json, "
                "structured_profile_sha256 "
                "FROM product_profile_revisions WHERE profile_id=?"
            )
            parameters: list[object] = [relationship[0]]
            if before_revision_number is not None:
                sql += " AND revision_number < ?"
                parameters.append(before_revision_number)
            sql += " ORDER BY revision_number DESC LIMIT ?"
            parameters.append(page_size)
            rows = connection.execute(sql, tuple(parameters)).fetchall()
            items = []
            for row in rows:
                if tuple(row[:3]) != tuple(relationship):
                    raise _error("internal_consistency_failure")
                _, structured_bytes = _validated_durable_profile(
                    profile_id=row[0],
                    canonical_schema_version=row[8],
                    structured_profile_json=row[9],
                    structured_profile_sha256=row[10],
                )
                item = ProfileHistoryItem.from_trusted(
                    profile_id=row[0],
                    revision_id=row[3],
                    revision_number=row[4],
                    revision_kind=row[5],
                    lifecycle_status=row[6],
                    created_at=row[7],
                    structured_profile_json=(
                        structured_bytes if include_structured_profile else None
                    ),
                )
                candidate = (*items, item)
                candidate_bytes = _serialize_history_page_for_size(
                    candidate,
                    include_structured_profile=include_structured_profile
                )
                if items and len(candidate_bytes) > MAX_HISTORY_RESPONSE_BYTES:
                    break
                if len(candidate_bytes) > MAX_HISTORY_RESPONSE_BYTES:
                    raise _error("internal_consistency_failure")
                items.append(item)
            return tuple(items)
        except PersistentProfileDomainError:
            raise
        except sqlite3.Error as exc:
            failure = _sqlite_reason(getattr(exc, "sqlite_errorcode", None))
        except Exception:
            failure = "internal_consistency_failure"
        if failure is not None:
            raise _error(failure)

    def purge(self, connection, command):
        if type(command) is not PurgePersistentProfileCommand:
            raise _error("invalid_command")

        def operation(conn):
            self._attest(conn)
            row = conn.execute(
                "SELECT profile_id, principal_id, environment_namespace FROM product_profiles "
                "WHERE profile_id=? AND principal_id=? AND environment_namespace=?",
                (
                    command.profile.profile_id,
                    command.profile.principal_id,
                    command.profile.environment_namespace,
                ),
            ).fetchone()
            if row is None:
                return PurgeResult()
            current = conn.execute(
                "SELECT lifecycle_status FROM current_product_profiles WHERE profile_id=?",
                (command.profile.profile_id,),
            ).fetchone()
            if current is None:
                raise _error("internal_consistency_failure")
            if current[0] != "deletion_requested":
                raise _error("purge_not_allowed")
            conn.execute(
                "DELETE FROM product_profiles WHERE profile_id=?",
                (command.profile.profile_id,),
            )
            self._hook("purge.after_container_delete")
            counts = [
                conn.execute(f"SELECT COUNT(*) FROM {name} WHERE profile_id=?", (command.profile.profile_id,)).fetchone()[0]
                for name in (
                    "product_profiles",
                    "product_profile_revisions",
                    "product_profile_sources",
                    "current_product_profiles",
                )
            ]
            if any(counts):
                raise _error("internal_consistency_failure")
            self._hook("purge.after_cascade_verification")
            self._hook("purge.before_finish")
            return PurgeResult()

        return self._mutate(connection, operation)


def create_persistent_profile(connection, command):
    return PersistentProfileRepository().create(connection, command)


def append_profile_revision(connection, command):
    return PersistentProfileRepository().append(connection, command)


def read_current_profile(
    connection,
    trusted_principal,
    *,
    profile_id=None,
    include_structured_profile=False,
):
    return PersistentProfileRepository().read_current(
        connection,
        trusted_principal,
        profile_id=profile_id,
        include_structured_profile=include_structured_profile,
    )


def read_profile_history(
    connection,
    trusted_principal,
    *,
    profile_id=None,
    page_size=DEFAULT_HISTORY_PAGE_SIZE,
    before_revision_number=None,
    include_structured_profile=False,
):
    return PersistentProfileRepository().read_history(
        connection,
        trusted_principal,
        profile_id=profile_id,
        page_size=page_size,
        before_revision_number=before_revision_number,
        include_structured_profile=include_structured_profile,
    )


def purge_persistent_profile(connection, command):
    return PersistentProfileRepository().purge(connection, command)

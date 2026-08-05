"""Fail-closed, offline private-beta invitation operations.

PB-OPS-1 trusts the executing project/package root and the pinned version-1
private-beta configuration selected for the current invocation, together with
the operator's explicit matching database and invitation-key paths.
Invocation-time file identities detect replacement after selection.  They do
not prove deployment authenticity, historical freshness, backup lineage,
global clone discovery, or a complete and internally consistent bundle
substituted before invocation and explicitly selected by the operator.
Stronger provenance needs an external trust root and is deliberately deferred.

Importing this module performs no configuration, filesystem, database,
terminal, network, provider, runtime, listener, crawler, or matcher work.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import ctypes
import errno
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import threading
from urllib.parse import quote

from wahojobs.account_reconciliation import attest_account_schema
from wahojobs.accounts import (
    AuthenticationUnavailable,
    InvalidAccountInput,
    create_invitation,
    invitation_creation_request_fingerprint,
    invitation_secret_hmac,
    normalize_email,
    revoke_invitation,
)
from wahojobs.database_lifetime_ownership import (
    DatabaseLifetimeOwnershipError,
    ROLE_OFFLINE_OPERATOR,
    acquire_database_lifetime_ownership,
    database_lifetime_ownership_is_released,
    release_database_lifetime_ownership,
    require_database_lifetime_ownership,
)
from wahojobs.google_oidc_authorization_transaction_schema import (
    MIGRATION_VERSION as GOOGLE_OIDC_MIGRATION_VERSION,
    PREREQUISITE_MIGRATION_VERSIONS as GOOGLE_OIDC_PREREQUISITE_MIGRATIONS,
    attest_google_oidc_authorization_transaction_schema,
)


CONFIGURATION_VERSION = 1
PRIVATE_BETA_ENVIRONMENT = "private_beta"
OPERATOR_ACTOR = "private_beta_offline_operator"
CREATE_PROTOCOL = "pb_ops_1_create_v1"
IDEMPOTENCY_PREFIX = "pb-ops-1:create:v1:"
ENVELOPE_FORMAT = "wahojobs-private-beta-invitation-v1"
ENVELOPE_AUTHENTICATION_DOMAIN = b"wahojobs-pb-ops-1-envelope-v1\x00"
TARGET_BINDING_PROTOCOL = "pb_ops_1_target_binding_v1"
OUTPUT_BINDING_PROTOCOL = "pb_ops_1_output_binding_v1"

CONFIGURATION_MAX_BYTES = 65_536
KEY_MIN_BYTES = 32
KEY_MAX_BYTES = 512
ENVELOPE_MAX_BYTES = 4_096
MAX_PATH_BYTES = 4_096
_SQLITE_HEADER_BYTES = 100
_PATH_TYPE = type(Path())
_WINDOWS_REPARSE_ATTRIBUTE = getattr(
    stat,
    "FILE_ATTRIBUTE_REPARSE_POINT",
    0x400,
)

_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{15,127}$")
_INVITATION_REFERENCE = re.compile(r"^inv_[0-9a-f]{32}$")
_EXPIRY = re.compile(
    r"^(?P<year>[0-9]{4})-(?P<month>[0-9]{2})-(?P<day>[0-9]{2})"
    r"T(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}):(?P<second>[0-9]{2})Z$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EMAIL_HINT = re.compile(r"^[^@\x00-\x20]{1}\*\*\*@[^@\x00-\x20]{1,255}$")

_EXPECTED_SCHEMA_OBJECT_COUNT = 174
_EXPECTED_SCHEMA_FINGERPRINT = (
    "f45e9d4c8c0f487a8437fdf1f5a323010d7c0b56c5d4a61a07ee4fe1f4f53735"
)
_EXPECTED_MIGRATION_MARKERS = (
    *GOOGLE_OIDC_PREREQUISITE_MIGRATIONS,
    GOOGLE_OIDC_MIGRATION_VERSION,
)
_MAX_SCHEMA_SQL_BYTES = 1_048_576

_PUBLIC_ERROR_PAIRS = frozenset(
    {
        ("INVALID_INPUT", 2),
        ("CONSOLE_UNAVAILABLE", 2),
        ("EMAIL_INVALID", 2),
        ("EMAIL_MISMATCH", 2),
        ("EXPIRY_INVALID", 2),
        ("INVITATION_REFERENCE_INVALID", 2),
        ("OUTPUT_FORM_INVALID", 2),
        ("CONFIGURATION_INVALID", 3),
        ("TARGET_VALIDATION_FAILED", 3),
        ("DATABASE_ATTESTATION_FAILED", 3),
        ("KEY_SOURCE_INVALID", 3),
        ("OWNERSHIP_BUSY", 4),
        ("DATABASE_BUSY", 4),
        ("REQUEST_ID_CONFLICT", 5),
        ("INVITATION_UNKNOWN", 5),
        ("INVITATION_NOT_PENDING", 5),
        ("CREDENTIAL_DESTINATION_UNAVAILABLE", 6),
        ("CREDENTIAL_PUBLICATION_FAILED", 6),
        ("CREDENTIAL_RECOVERY_UNAVAILABLE", 6),
        ("CLEANUP_INCOMPLETE", 7),
        ("OWNERSHIP_LOST", 7),
        ("INTERNAL_FAILURE", 7),
        ("COMMITTED_RETRY_REQUIRED", 8),
    }
)


@dataclass(slots=True, repr=False)
class _OperationState:
    operation: str
    durable_preexisting: bool = False
    database_commit_attempted: bool = False
    database_commit_confirmed: bool = False
    credential_publication_attempted: bool = False
    credential_publication_confirmed: bool = False
    ownership_release_attempted: bool = False
    ownership_release_confirmed: bool = False
    result_delivery_attempted: bool = False
    result_delivery_confirmed: bool = False
    cleanup_incomplete: bool = False
    raw_ownership_retained: bool = False

    @property
    def no_irreversible_action_attempted(self) -> bool:
        return not (
            self.durable_preexisting
            or self.database_commit_attempted
            or self.credential_publication_attempted
        )

    @property
    def durable_mutation_may_have_occurred(self) -> bool:
        return bool(
            self.durable_preexisting
            or self.database_commit_attempted
            or self.database_commit_confirmed
            or self.credential_publication_attempted
            or self.credential_publication_confirmed
        )

    @property
    def indeterminate_durable_boundary(self) -> bool:
        return bool(
            self.database_commit_attempted
            or self.credential_publication_attempted
        )

    def note_preexisting_durable_mutation(self):
        self.durable_preexisting = True

    def begin_database_commit(self):
        self.database_commit_attempted = True

    def confirm_database_commit(self):
        if not self.database_commit_attempted:
            raise AssertionError("database_commit_confirmation_without_attempt")
        self.database_commit_confirmed = True

    def begin_credential_publication(self):
        self.credential_publication_attempted = True

    def confirm_credential_publication(self):
        if not self.credential_publication_attempted:
            raise AssertionError("credential_publication_confirmation_without_attempt")
        self.credential_publication_confirmed = True

    def begin_ownership_release(self):
        self.ownership_release_attempted = True

    def confirm_ownership_release(self):
        if not self.ownership_release_attempted:
            raise AssertionError("ownership_release_confirmation_without_attempt")
        self.ownership_release_confirmed = True

    def begin_result_delivery(self):
        self.result_delivery_attempted = True

    def confirm_result_delivery(self):
        if not self.result_delivery_attempted:
            raise AssertionError("result_delivery_confirmation_without_attempt")
        self.result_delivery_confirmed = True


@dataclass(frozen=True, slots=True, repr=False)
class _CloseReport:
    terminal: bool
    exception_observed: bool

    def combine(self, other: "_CloseReport") -> "_CloseReport":
        return _CloseReport(
            terminal=other.terminal,
            exception_observed=(
                self.exception_observed or other.exception_observed
            ),
        )


class PrivateBetaInvitationOperationError(Exception):
    """A fixed, redacted PB-OPS-1 failure."""

    __slots__ = ("_code", "_exit_code", "_status", "_cleanup_incomplete")

    def __init__(
        self,
        code: str,
        exit_code: int,
        *,
        status: str | None = None,
        cleanup_incomplete: bool = False,
    ):
        if (
            type(code) is not str
            or type(exit_code) is not int
            or (code, exit_code) not in _PUBLIC_ERROR_PAIRS
        ):
            code = "INTERNAL_FAILURE"
            exit_code = 7
            status = None
            cleanup_incomplete = False
        if status not in {None, "pending", "expired", "consumed", "revoked"}:
            status = None
        if code != "COMMITTED_RETRY_REQUIRED":
            cleanup_incomplete = code == "CLEANUP_INCOMPLETE"
        self._code = code
        self._exit_code = exit_code
        self._status = status
        self._cleanup_incomplete = bool(cleanup_incomplete)
        super().__init__(code)

    @property
    def code(self) -> str:
        return self._code

    @property
    def exit_code(self) -> int:
        return self._exit_code

    @property
    def status(self) -> str | None:
        return self._status

    @property
    def cleanup_incomplete(self) -> bool:
        return self._cleanup_incomplete

    def __repr__(self):
        return (
            "PrivateBetaInvitationOperationError("
            f"code={self._code!r}, exit_code={self._exit_code!r}, "
            f"cleanup_incomplete={self._cleanup_incomplete!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class PrivateBetaInvitationResult:
    operation: str
    outcome: str
    invitation_reference: str
    email_hint: str
    created_at: str | None
    expires_at: str
    status: str
    _state: _OperationState

    def approved_fields(self) -> dict[str, str]:
        values = {
            "operation": self.operation,
            "outcome": self.outcome,
            "invitation_reference": self.invitation_reference,
            "email_hint": self.email_hint,
            "expires_at": self.expires_at,
            "status": self.status,
        }
        if self.operation == "status" and self.created_at is not None:
            values["created_at"] = self.created_at
        return values

    def __repr__(self):
        return (
            "PrivateBetaInvitationResult("
            f"operation={self.operation!r}, outcome={self.outcome!r}, "
            f"invitation_reference={self.invitation_reference!r}, "
            f"email_hint={self.email_hint!r}, expires_at={self.expires_at!r}, "
            f"status={self.status!r})"
        )

    def _begin_delivery(self):
        self._state.begin_result_delivery()

    def _confirm_delivery(self):
        self._state.confirm_result_delivery()

    @property
    def _durable_delivery(self) -> bool:
        return self._state.durable_mutation_may_have_occurred


@dataclass(frozen=True, slots=True, repr=False)
class _FileIdentity:
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int
    mode: int
    links: int

    @property
    def object_key(self):
        return (self.device, self.inode, stat.S_IFMT(self.mode), self.links)

    @property
    def stable_key(self):
        return (
            self.device,
            self.inode,
            self.size,
            self.modified_ns,
            None if os.name == "nt" else self.changed_ns,
            stat.S_IFMT(self.mode),
            self.links,
        )


@dataclass(slots=True, repr=False)
class _PinnedConfiguration:
    path: Path
    identity: _FileIdentity
    handle: io.BufferedReader
    environment: str
    database_path: Path
    key_path: Path

    def close(self, *, checkpoint=None) -> _CloseReport:
        handle = self.handle
        if handle is None:
            return _CloseReport(True, False)
        exception_observed = _checkpoint_exception(
            checkpoint,
            "before_configuration_close",
        )
        try:
            handle.close()
        except BaseException:
            exception_observed = True
        terminal = _python_file_handle_is_terminal(handle)
        if terminal:
            self.handle = None
        exception_observed = (
            _checkpoint_exception(checkpoint, "after_configuration_close")
            or exception_observed
        )
        return _CloseReport(terminal, exception_observed)


@dataclass(frozen=True, slots=True, repr=False)
class _TargetSet:
    configuration: _PinnedConfiguration
    database_path: Path
    database_identity: _FileIdentity
    key_path: Path
    key_identity: _FileIdentity
    coordination_path: Path
    project_root: Path
    configuration_binding: str


@dataclass(slots=True, repr=False)
class _OutputTarget:
    final_path: Path
    stage_path: Path
    parent_path: Path
    parent_identity: _FileIdentity
    output_binding: str
    directory_descriptor: int | None = None
    windows_directory_handle: int | None = None

    def close(self, *, checkpoint=None) -> _CloseReport:
        if (
            self.directory_descriptor is None
            and self.windows_directory_handle in {None, 0, -1}
        ):
            return _CloseReport(True, False)
        exception_observed = _checkpoint_exception(
            checkpoint,
            "before_output_close",
        )
        descriptor = self.directory_descriptor
        if descriptor is not None:
            try:
                os.close(descriptor)
            except BaseException:
                exception_observed = True
            if _descriptor_is_terminal(descriptor):
                self.directory_descriptor = None
        handle = self.windows_directory_handle
        if handle not in {None, 0, -1}:
            try:
                _windows_close_handle(handle)
            except BaseException:
                exception_observed = True
            if _windows_handle_is_terminal(handle):
                self.windows_directory_handle = None
        exception_observed = (
            _checkpoint_exception(checkpoint, "after_output_close")
            or exception_observed
        )
        terminal = (
            self.directory_descriptor is None
            and self.windows_directory_handle in {None, 0, -1}
        )
        return _CloseReport(terminal, exception_observed)


@dataclass(slots=True, repr=False)
class _CallerAcknowledgedHandoff:
    kind: str
    destination: object
    acknowledged: bool = False

    def acknowledge(self, destination, *, checkpoint=None):
        if self.acknowledged or destination is not self.destination:
            raise AssertionError("invalid_caller_acknowledgement")
        _emit_checkpoint(
            checkpoint,
            f"before_{self.kind}_acknowledgement_transition",
        )
        # This single state transition is the ownership linearization point.
        # The caller has already installed ``destination`` in its cleanup path,
        # so an interruption after this assignment leaves that exact object as
        # the sole effective owner.  Before it, the raw authority still owns.
        self.acknowledged = True


@dataclass(slots=True, repr=False)
class _RawResourceCleanup:
    kind: str
    descriptor: int | None = None
    descriptor_identity: _FileIdentity | None = None
    handle: object | None = None
    handle_descriptor: int | None = None
    connection: sqlite3.Connection | None = None
    session: object | None = None
    handoff: _CallerAcknowledgedHandoff | None = None

    def adopt_descriptor(self, descriptor: int):
        if (
            type(descriptor) is not int
            or self.descriptor is not None
            or self.handle is not None
            or self.connection is not None
            or self.session is not None
            or self.handoff is not None
        ):
            raise AssertionError("invalid_raw_descriptor_adoption")
        self.descriptor = descriptor
        self.descriptor_identity = _identity(os.fstat(descriptor))

    def adopt_file_handle(self, handle):
        descriptor = self.descriptor
        if (
            descriptor is None
            or self.handle is not None
            or self.session is not None
            or self.handoff is not None
        ):
            raise AssertionError("invalid_raw_handle_adoption")
        # os.fdopen() has already transferred the descriptor to this exact
        # handle.  Record that transfer before performing fallible validation
        # so cleanup never has two owners or an ownership gap.
        self.handle = handle
        self.handle_descriptor = descriptor
        self.descriptor = None
        if handle.fileno() != descriptor:
            raise AssertionError("raw_handle_descriptor_mismatch")
        opened = _identity(os.fstat(descriptor))
        if not _same_object_identity(self.descriptor_identity, opened):
            raise AssertionError("raw_handle_identity_mismatch")

    def adopt_connection(self, connection: sqlite3.Connection):
        if (
            self.connection is not None
            or self.session is not None
            or self.handoff is not None
        ):
            raise AssertionError("invalid_raw_connection_adoption")
        self.connection = connection

    def stage_database_session(self, session):
        if (
            self.session is not None
            or self.handoff is not None
            or session.connection is not self.connection
            or session.descriptor != self.descriptor
            or not _same_object_identity(
                self.descriptor_identity,
                session.descriptor_identity,
            )
        ):
            raise AssertionError("invalid_database_session_adoption")
        self.session = session
        self.connection = None
        self.descriptor = None
        self.handoff = _CallerAcknowledgedHandoff(
            kind="database_session",
            destination=session,
        )

    def stage_configuration_handle(self, configuration):
        if (
            self.handle is None
            or configuration.handle is not self.handle
            or self.session is not None
            or self.handoff is not None
        ):
            raise AssertionError("invalid_configuration_handoff")
        self.handoff = _CallerAcknowledgedHandoff(
            kind="pinned_configuration",
            destination=configuration,
        )

    def acknowledge_database_session(self, session, *, checkpoint=None):
        handoff = self.handoff
        if (
            handoff is None
            or handoff.kind != "database_session"
            or handoff.destination is not session
            or self.session is not session
        ):
            raise AssertionError("database_session_acknowledgement_without_adoption")
        handoff.acknowledge(session, checkpoint=checkpoint)
        self.session = None
        self.descriptor_identity = None
        _emit_checkpoint(
            checkpoint,
            "after_database_session_acknowledgement_transition",
        )

    def acknowledge_configuration_handle(self, configuration, *, checkpoint=None):
        handoff = self.handoff
        if (
            handoff is None
            or handoff.kind != "pinned_configuration"
            or handoff.destination is not configuration
            or self.handle is not configuration.handle
        ):
            raise AssertionError("configuration_acknowledgement_without_adoption")
        handoff.acknowledge(configuration, checkpoint=checkpoint)
        self.handle = None
        self.handle_descriptor = None
        self.descriptor_identity = None
        _emit_checkpoint(
            checkpoint,
            "after_pinned_configuration_acknowledgement_transition",
        )

    def handoff_acknowledged_for(self, destination) -> bool:
        handoff = self.handoff
        return bool(
            handoff is not None
            and handoff.destination is destination
            and handoff.acknowledged
        )

    def close(self, *, checkpoint=None) -> _CloseReport:
        handoff = self.handoff
        if handoff is not None and handoff.acknowledged:
            # The exact destination is already installed in the caller's
            # cleanup path.  This source is now only a stale alias and must not
            # close, retain, or otherwise compete with the destination owner.
            return _CloseReport(True, False)
        exception_observed = _checkpoint_exception(
            checkpoint,
            f"before_{self.kind}_raw_close",
        )
        session = self.session
        if session is not None:
            try:
                report = _coerce_close_report(session.close(checkpoint=None))
            except BaseException:
                report = _CloseReport(False, True)
            exception_observed = exception_observed or report.exception_observed
            if report.terminal:
                self.session = None
                self.descriptor_identity = None
        else:
            connection = self.connection
            if connection is not None:
                try:
                    connection.close()
                except BaseException:
                    exception_observed = True
                if _sqlite_connection_is_terminal(connection):
                    self.connection = None

            handle = self.handle
            if handle is not None:
                safe_to_close = True
                descriptor = self.handle_descriptor
                if descriptor is None:
                    safe_to_close = False
                    exception_observed = True
                else:
                    descriptor_state, actual = _raw_descriptor_state(
                        descriptor,
                        self.descriptor_identity,
                    )
                    if descriptor_state == "live":
                        if self.descriptor_identity is None:
                            self.descriptor_identity = actual
                    elif descriptor_state == "terminal":
                        self.handle = None
                        self.handle_descriptor = None
                        self.descriptor_identity = None
                        safe_to_close = False
                    else:
                        # An identity mismatch means the original descriptor is
                        # gone and its integer was reused.  Keep the file object
                        # reachable rather than closing the unrelated resource.
                        safe_to_close = False
                        exception_observed = True
                if safe_to_close:
                    try:
                        handle.close()
                    except BaseException:
                        exception_observed = True
                    if _python_file_handle_is_terminal(handle):
                        self.handle = None
                        self.handle_descriptor = None
                        self.descriptor_identity = None
                    elif descriptor is not None:
                        descriptor_state, _actual = _raw_descriptor_state(
                            descriptor,
                            self.descriptor_identity,
                        )
                        if descriptor_state == "terminal":
                            self.handle = None
                            self.handle_descriptor = None
                            self.descriptor_identity = None

            descriptor = self.descriptor
            if descriptor is not None:
                descriptor_state, actual = _raw_descriptor_state(
                    descriptor,
                    self.descriptor_identity,
                )
                if descriptor_state in {"terminal", "reused"}:
                    self.descriptor = None
                    self.descriptor_identity = None
                    exception_observed = (
                        exception_observed or descriptor_state == "reused"
                    )
                elif descriptor_state == "unresolved":
                    exception_observed = True
                else:
                    if self.descriptor_identity is None:
                        self.descriptor_identity = actual
                    try:
                        os.close(descriptor)
                    except BaseException:
                        exception_observed = True
                    descriptor_state, _actual = _raw_descriptor_state(
                        descriptor,
                        self.descriptor_identity,
                    )
                    if descriptor_state in {"terminal", "reused"}:
                        self.descriptor = None
                        self.descriptor_identity = None
                        exception_observed = (
                            exception_observed or descriptor_state == "reused"
                        )
                    elif descriptor_state == "unresolved":
                        exception_observed = True

        exception_observed = (
            _checkpoint_exception(
                checkpoint,
                f"after_{self.kind}_raw_close",
            )
            or exception_observed
        )
        return _CloseReport(
            terminal=all(
                value is None
                for value in (
                    self.descriptor,
                    self.handle,
                    self.connection,
                    self.session,
                )
            ),
            exception_observed=exception_observed,
        )


@dataclass(slots=True, repr=False)
class _RetainedCleanup:
    raw: object | None = None
    session: object | None = None
    output: object | None = None
    configuration: object | None = None
    ownership: object | None = None
    database_path: Path | None = None
    state: _OperationState | None = None
    durable: bool = False


_RETAINED_CLEANUP_LOCK = threading.Lock()
_RETAINED_CLEANUPS: list[_RetainedCleanup] = []


def _retain_cleanup_authorities(**values):
    retained = _RetainedCleanup(**values)
    if all(
        authority is None
        for authority in (
            retained.raw,
            retained.session,
            retained.output,
            retained.configuration,
            retained.ownership,
        )
    ):
        return
    with _RETAINED_CLEANUP_LOCK:
        _RETAINED_CLEANUPS.append(retained)


def _error(
    code: str,
    exit_code: int,
    *,
    status: str | None = None,
    cleanup_incomplete: bool = False,
):
    return PrivateBetaInvitationOperationError(
        code,
        exit_code,
        status=status,
        cleanup_incomplete=cleanup_incomplete,
    )


def _committed_retry_required(*, cleanup_incomplete=False):
    return _error(
        "COMMITTED_RETRY_REQUIRED",
        8,
        cleanup_incomplete=cleanup_incomplete,
    )


def _checkpoint_exception(callback, name: str) -> bool:
    try:
        _emit_checkpoint(callback, name)
    except BaseException:
        return True
    return False


def _python_file_handle_is_terminal(handle) -> bool:
    try:
        if handle.closed:
            return True
    except BaseException:
        return False
    return False


def _descriptor_is_terminal(descriptor: int) -> bool:
    try:
        os.fstat(descriptor)
    except OSError as exc:
        return exc.errno == errno.EBADF
    except BaseException:
        return False
    return False


def _raw_descriptor_state(
    descriptor: int,
    expected: _FileIdentity | None,
):
    try:
        actual = _identity(os.fstat(descriptor))
    except OSError as exc:
        if exc.errno == errno.EBADF:
            return "terminal", None
        return "unresolved", None
    except BaseException:
        return "unresolved", None
    if expected is not None and not _same_object_identity(expected, actual):
        # The exact resource formerly associated with this integer is terminal;
        # never close the unrelated descriptor that subsequently reused it.
        return "reused", actual
    return "live", actual


def _coerce_close_report(value) -> _CloseReport:
    if type(value) is _CloseReport:
        return value
    if value is True:
        return _CloseReport(True, False)
    return _CloseReport(False, True)


def _close_with_one_retry(authority, *, checkpoint=None) -> _CloseReport:
    try:
        first = _coerce_close_report(authority.close(checkpoint=checkpoint))
    except BaseException:
        first = _CloseReport(False, True)
    if first.terminal:
        return first
    try:
        second = _coerce_close_report(authority.close(checkpoint=None))
    except BaseException:
        second = _CloseReport(False, True)
    return first.combine(second)


def _close_or_retain_raw(
    authority: _RawResourceCleanup,
    *,
    checkpoint=None,
    ownership=None,
    database_path: Path | None = None,
    state: _OperationState | None = None,
) -> _CloseReport:
    report = _close_with_one_retry(authority, checkpoint=checkpoint)
    if report.terminal:
        return report
    if ownership is not None and (
        database_path is None or type(state) is not _OperationState
    ):
        raise AssertionError("raw_database_cleanup_missing_ownership_context")
    _retain_cleanup_authorities(
        raw=authority,
        ownership=ownership,
        database_path=database_path,
        state=state,
        durable=False,
    )
    if ownership is not None:
        state.raw_ownership_retained = True
    return report


def _identity(metadata) -> _FileIdentity:
    return _FileIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        size=metadata.st_size,
        modified_ns=getattr(
            metadata,
            "st_mtime_ns",
            int(metadata.st_mtime * 1_000_000_000),
        ),
        changed_ns=getattr(
            metadata,
            "st_ctime_ns",
            int(metadata.st_ctime * 1_000_000_000),
        ),
        mode=metadata.st_mode,
        links=metadata.st_nlink,
    )


def _is_reparse(metadata) -> bool:
    return bool(
        getattr(metadata, "st_file_attributes", 0)
        & _WINDOWS_REPARSE_ATTRIBUTE
    )


def _canonical_existing_path(value, *, code="TARGET_VALIDATION_FAILED") -> Path:
    path = _validated_absolute_path(value, code=code)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise _error(code, 3) from None
    if type(resolved) is not _PATH_TYPE or resolved != path:
        raise _error(code, 3)
    return resolved


def _validated_absolute_path(value, *, code="INVALID_INPUT") -> Path:
    if type(value) not in {str, _PATH_TYPE}:
        raise _error(code, 2 if code in {"INVALID_INPUT", "OUTPUT_FORM_INVALID"} else 3)
    try:
        text = os.fspath(value)
        encoded = text.encode("utf-8", "strict")
    except (TypeError, UnicodeError):
        raise _error(code, 2 if code in {"INVALID_INPUT", "OUTPUT_FORM_INVALID"} else 3) from None
    if (
        not text
        or "\x00" in text
        or len(encoded) > MAX_PATH_BYTES
        or (os.name == "nt" and ":" in os.path.splitdrive(text)[1])
    ):
        raise _error(code, 2 if code in {"INVALID_INPUT", "OUTPUT_FORM_INVALID"} else 3)
    path = Path(text)
    if (
        not path.is_absolute()
        or any(part in {".", ".."} for part in path.parts)
        or Path(os.path.abspath(text)) != path
    ):
        raise _error(code, 2 if code in {"INVALID_INPUT", "OUTPUT_FORM_INVALID"} else 3)
    return path


def _require_safe_components(path: Path, *, allow_sticky_shared=True):
    current = path
    while True:
        try:
            metadata = os.lstat(current)
        except OSError:
            raise _error("TARGET_VALIDATION_FAILED", 3) from None
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
            raise _error("TARGET_VALIDATION_FAILED", 3)
        if os.name == "posix" and stat.S_ISDIR(metadata.st_mode):
            owner = metadata.st_uid
            current_user = os.geteuid()
            shared_write = bool(metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH))
            sticky = bool(metadata.st_mode & stat.S_ISVTX)
            if owner not in {0, current_user} or (
                shared_write and not (allow_sticky_shared and sticky)
            ):
                raise _error("TARGET_VALIDATION_FAILED", 3)
        parent = current.parent
        if parent == current:
            return
        current = parent


def _capture_regular_file(
    path: Path,
    *,
    secret=False,
    database=False,
    configuration=False,
) -> _FileIdentity:
    _require_safe_components(path)
    try:
        metadata = os.lstat(path)
    except OSError:
        raise _error("TARGET_VALIDATION_FAILED", 3) from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or _is_reparse(metadata)
        or (database and metadata.st_size < _SQLITE_HEADER_BYTES)
    ):
        raise _error("TARGET_VALIDATION_FAILED", 3)
    if os.name == "posix":
        current_user = os.geteuid()
        if metadata.st_uid != current_user:
            raise _error("TARGET_VALIDATION_FAILED", 3)
        if secret and metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise _error("KEY_SOURCE_INVALID", 3)
        if configuration and metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise _error("CONFIGURATION_INVALID", 3)
    return _identity(metadata)


def _same_stable_identity(expected: _FileIdentity, actual: _FileIdentity) -> bool:
    return type(expected) is _FileIdentity and expected.stable_key == actual.stable_key


def _same_object_identity(expected: _FileIdentity, actual: _FileIdentity) -> bool:
    return type(expected) is _FileIdentity and expected.object_key == actual.object_key


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath(
            (os.path.normcase(os.fspath(path)), os.path.normcase(os.fspath(root)))
        ) == os.path.normcase(os.fspath(root))
    except ValueError:
        return False


def _trusted_project_root() -> Path:
    try:
        source = Path(__file__)
        if not source.is_absolute():
            raise ValueError()
        resolved_source = source.resolve(strict=True)
    except (OSError, RuntimeError, UnicodeError, ValueError):
        raise _error("TARGET_VALIDATION_FAILED", 3) from None
    if source != resolved_source:
        raise _error("TARGET_VALIDATION_FAILED", 3)
    root = resolved_source.parents[1]
    _require_safe_components(root)
    try:
        root_metadata = os.lstat(root)
    except OSError:
        raise _error("TARGET_VALIDATION_FAILED", 3) from None
    if not stat.S_ISDIR(root_metadata.st_mode) or _is_reparse(root_metadata):
        raise _error("TARGET_VALIDATION_FAILED", 3)

    checked = 0
    for name, module in tuple(sys.modules.items()):
        if not (
            name in {"scripts", "tests", "wahojobs"}
            or name.startswith(("scripts.", "tests.", "wahojobs."))
        ):
            continue
        module_source = getattr(module, "__file__", None)
        if module_source is None:
            continue
        try:
            module_path = Path(os.fspath(module_source))
            if not module_path.is_absolute():
                raise ValueError()
            resolved_module = module_path.resolve(strict=True)
        except (OSError, RuntimeError, UnicodeError, TypeError, ValueError):
            raise _error("TARGET_VALIDATION_FAILED", 3) from None
        if (
            module_path != resolved_module
            or not _path_is_within(resolved_module, root)
        ):
            raise _error("TARGET_VALIDATION_FAILED", 3)
        checked += 1
    if checked == 0:
        raise _error("TARGET_VALIDATION_FAILED", 3)
    return root


def _outside_project_root(path: Path, root: Path) -> bool:
    return not _path_is_within(path, root)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise _error("CONFIGURATION_INVALID", 3)
        result[key] = value
    return result


def _reject_json_constant(_value):
    raise _error("CONFIGURATION_INVALID", 3)


def _read_exact_pinned_file(
    path: Path,
    identity: _FileIdentity,
    *,
    minimum: int,
    maximum: int,
    retain_handle: bool,
    checkpoint=None,
    authority=None,
):
    if not minimum <= identity.size <= maximum:
        raise _error("CONFIGURATION_INVALID" if retain_handle else "KEY_SOURCE_INVALID", 3)
    caller_owned_authority = authority is not None
    if retain_handle:
        if (
            type(authority) is not _RawResourceCleanup
            or authority.kind != "pinned_file"
            or any(
                value is not None
                for value in (
                    authority.descriptor,
                    authority.handle,
                    authority.connection,
                    authority.session,
                    authority.handoff,
                )
            )
        ):
            raise AssertionError("pinned_file_handoff_authority_required")
    elif caller_owned_authority:
        raise AssertionError("unexpected_pinned_file_handoff_authority")
    else:
        authority = _RawResourceCleanup(kind="pinned_file")
    handle = None
    buffer = bytearray(identity.size)
    view = None
    completed = False
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        authority.adopt_descriptor(descriptor)
        os.set_inheritable(descriptor, False)
        if os.get_inheritable(descriptor):
            raise OSError()
        handle = os.fdopen(descriptor, "rb", buffering=0)
        authority.adopt_file_handle(handle)
        opened = _identity(os.fstat(handle.fileno()))
        if not _same_stable_identity(identity, opened):
            raise OSError()
        view = memoryview(buffer)
        offset = 0
        while offset < len(buffer):
            count = handle.readinto(view[offset:])
            if type(count) is not int or count <= 0:
                raise OSError()
            offset += count
        if handle.read(1) != b"":
            raise OSError()
        after = _identity(os.fstat(handle.fileno()))
        current = _identity(os.lstat(path))
        if not _same_stable_identity(identity, after) or not _same_stable_identity(identity, current):
            raise OSError()
        view.release()
        view = None
        if retain_handle:
            _emit_checkpoint(checkpoint, "before_pinned_file_delivery")
            completed = True
            return buffer, handle, authority
        report = _close_or_retain_raw(
            authority,
            checkpoint=checkpoint,
        )
        authority = None
        handle = None
        if not report.terminal or report.exception_observed:
            completed = True
            raise _error("CLEANUP_INCOMPLETE", 7) from None
        completed = True
        return buffer, None, None
    except PrivateBetaInvitationOperationError:
        raise
    except BaseException:
        raise _error("CONFIGURATION_INVALID" if retain_handle else "KEY_SOURCE_INVALID", 3) from None
    finally:
        if view is not None:
            view.release()
        if authority is not None and not completed and not caller_owned_authority:
            report = _close_or_retain_raw(
                authority,
                checkpoint=checkpoint,
            )
            authority = None
            handle = None
            if not report.terminal or report.exception_observed:
                raise _error("CLEANUP_INCOMPLETE", 7) from None


def _clear_buffer(value):
    if isinstance(value, bytearray):
        value[:] = b"\x00" * len(value)


def _load_pinned_configuration(
    configuration_path,
    *,
    checkpoint=None,
) -> _PinnedConfiguration:
    path = _canonical_existing_path(configuration_path, code="CONFIGURATION_INVALID")
    identity = _capture_regular_file(path, configuration=True)
    raw = None
    handle = None
    authority = _RawResourceCleanup(kind="pinned_file")
    returned_authority = None
    pinned = None
    document = None
    pure = None
    completed = False
    try:
        raw, handle, returned_authority = _read_exact_pinned_file(
            path,
            identity,
            minimum=1,
            maximum=CONFIGURATION_MAX_BYTES,
            retain_handle=True,
            checkpoint=checkpoint,
            authority=authority,
        )
        if returned_authority is not authority:
            raise _error("INTERNAL_FAILURE", 7)
        _emit_checkpoint(
            checkpoint,
            "after_pinned_file_delivery_before_acknowledgement",
        )
        document = json.loads(
            bytes(raw),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
        # Reuse only the dormant runtime's pure, filesystem-free validator.
        # Its resolver is intentionally not used: PB-OWN must precede the first
        # database descriptor open in this invocation.
        from wahojobs.durable_google_login_runtime import (
            DurableGoogleLoginConfigurationError,
            _validated_configuration,
        )

        try:
            pure = _validated_configuration(document)
        except DurableGoogleLoginConfigurationError:
            raise _error("CONFIGURATION_INVALID", 3) from None
        if (
            pure.version != CONFIGURATION_VERSION
            or pure.environment != PRIVATE_BETA_ENVIRONMENT
            or pure.account_invitation_lookup_key_path_text is None
        ):
            raise _error("CONFIGURATION_INVALID", 3)
        database_path = _canonical_existing_path(
            pure.database_path_text,
            code="CONFIGURATION_INVALID",
        )
        key_path = _canonical_existing_path(
            pure.account_invitation_lookup_key_path_text,
            code="CONFIGURATION_INVALID",
        )
        pinned = _PinnedConfiguration(
            path=path,
            identity=identity,
            handle=handle,
            environment=pure.environment,
            database_path=database_path,
            key_path=key_path,
        )
        authority.stage_configuration_handle(pinned)
        _emit_checkpoint(checkpoint, "before_pinned_configuration_adoption")
        authority.acknowledge_configuration_handle(
            pinned,
            checkpoint=checkpoint,
        )
        _emit_checkpoint(checkpoint, "after_pinned_configuration_adoption")
        authority = None
        handle = None
        completed = True
        return pinned
    except PrivateBetaInvitationOperationError:
        raise
    except BaseException:
        raise _error("CONFIGURATION_INVALID", 3) from None
    finally:
        _clear_buffer(raw)
        cleanup_report = _CloseReport(True, False)
        if (
            not completed
            and authority is not None
            and pinned is not None
            and authority.handoff_acknowledged_for(pinned)
        ):
            # Acknowledgement is the ownership transition.  If interruption
            # occurs before the caller retires this local source alias, the
            # installed pinned wrapper remains the sole cleanup authority.
            authority = None
            handle = None
        if not completed and authority is not None:
            if pinned is not None and pinned.handle is handle:
                pinned.handle = None
            cleanup_report = _close_or_retain_raw(
                authority,
                checkpoint=checkpoint,
            )
            authority = None
            handle = None
        elif not completed and pinned is not None:
            cleanup_report = _close_with_one_retry(
                pinned,
                checkpoint=checkpoint,
            )
            if not cleanup_report.terminal:
                _retain_cleanup_authorities(configuration=pinned)
        document = None
        pure = None
        if not cleanup_report.terminal or cleanup_report.exception_observed:
            raise _error("CLEANUP_INCOMPLETE", 7) from None


def _revalidate_configuration(configuration: _PinnedConfiguration):
    if type(configuration) is not _PinnedConfiguration or configuration.handle is None:
        raise _error("CONFIGURATION_INVALID", 3)
    try:
        opened = _identity(os.fstat(configuration.handle.fileno()))
        current = _identity(os.lstat(configuration.path))
    except OSError:
        raise _error("CONFIGURATION_INVALID", 3) from None
    if (
        configuration.handle.closed
        or os.get_inheritable(configuration.handle.fileno())
        or not _same_stable_identity(configuration.identity, opened)
        or not _same_stable_identity(configuration.identity, current)
    ):
        raise _error("CONFIGURATION_INVALID", 3)


def _semantic_digest(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _resolve_targets(
    configuration_path,
    database_path,
    key_path,
    *,
    checkpoint=None,
) -> _TargetSet:
    project_root = _trusted_project_root()
    configuration = _load_pinned_configuration(
        configuration_path,
        checkpoint=checkpoint,
    )
    try:
        explicit_database = _canonical_existing_path(database_path)
        explicit_key = _canonical_existing_path(key_path)
        if (
            explicit_database != configuration.database_path
            or explicit_key != configuration.key_path
        ):
            raise _error("TARGET_VALIDATION_FAILED", 3)
        if not all(
            _outside_project_root(path, project_root)
            for path in (
                configuration.path,
                explicit_database,
                explicit_key,
            )
        ):
            raise _error("TARGET_VALIDATION_FAILED", 3)
        database_identity = _capture_regular_file(explicit_database, database=True)
        key_identity = _capture_regular_file(explicit_key, secret=True)
        coordination_path = explicit_database.with_name(
            explicit_database.name + ".wahojobs-lifetime.lock"
        )
        canonical_paths = {
            os.path.normcase(os.fspath(configuration.path)),
            os.path.normcase(os.fspath(explicit_database)),
            os.path.normcase(os.fspath(explicit_key)),
            os.path.normcase(os.fspath(coordination_path)),
        }
        if len(canonical_paths) != 4:
            raise _error("TARGET_VALIDATION_FAILED", 3)
        identities = {
            (configuration.identity.device, configuration.identity.inode),
            (database_identity.device, database_identity.inode),
            (key_identity.device, key_identity.inode),
        }
        if len(identities) != 3:
            raise _error("TARGET_VALIDATION_FAILED", 3)
        binding = _semantic_digest(
            {
                "configuration_path": os.fspath(configuration.path),
                "database_path": os.fspath(explicit_database),
                "environment": PRIVATE_BETA_ENVIRONMENT,
                "invitation_key_path": os.fspath(explicit_key),
                "protocol": TARGET_BINDING_PROTOCOL,
                "version": CONFIGURATION_VERSION,
            }
        )
        _revalidate_configuration(configuration)
        return _TargetSet(
            configuration=configuration,
            database_path=explicit_database,
            database_identity=database_identity,
            key_path=explicit_key,
            key_identity=key_identity,
            coordination_path=coordination_path,
            project_root=project_root,
            configuration_binding=binding,
        )
    except BaseException as primary:
        report = _close_with_one_retry(
            configuration,
            checkpoint=checkpoint,
        )
        if not report.terminal:
            _retain_cleanup_authorities(configuration=configuration)
        if not report.terminal or report.exception_observed:
            raise _error("CLEANUP_INCOMPLETE", 7) from None
        if isinstance(primary, PrivateBetaInvitationOperationError):
            raise primary
        raise


def _revalidate_targets(targets: _TargetSet, *, database_stable=True):
    _revalidate_configuration(targets.configuration)
    try:
        database = _identity(os.lstat(targets.database_path))
        key = _identity(os.lstat(targets.key_path))
    except OSError:
        raise _error("TARGET_VALIDATION_FAILED", 3) from None
    database_matches = (
        _same_stable_identity(targets.database_identity, database)
        if database_stable
        else _same_object_identity(targets.database_identity, database)
    )
    if (
        not database_matches
        or not _same_stable_identity(targets.key_identity, key)
        or not stat.S_ISREG(database.mode)
        or not stat.S_ISREG(key.mode)
        or database.links != 1
        or key.links != 1
        or _is_reparse(os.lstat(targets.database_path))
        or _is_reparse(os.lstat(targets.key_path))
    ):
        raise _error("TARGET_VALIDATION_FAILED", 3)


def _parse_request_id(value) -> str:
    if type(value) is not str or _REQUEST_ID.fullmatch(value) is None:
        raise _error("INVALID_INPUT", 2)
    return value


def _parse_invitation_reference(value) -> str:
    if type(value) is not str or _INVITATION_REFERENCE.fullmatch(value) is None:
        raise _error("INVITATION_REFERENCE_INVALID", 2)
    return value


def _parse_expiry(value) -> datetime:
    if type(value) is not str:
        raise _error("EXPIRY_INVALID", 2)
    matched = _EXPIRY.fullmatch(value)
    if matched is None:
        raise _error("EXPIRY_INVALID", 2)
    try:
        parsed = datetime(
            *(int(matched.group(name)) for name in ("year", "month", "day", "hour", "minute", "second")),
            tzinfo=timezone.utc,
        )
    except ValueError:
        raise _error("EXPIRY_INVALID", 2) from None
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise _error("EXPIRY_INVALID", 2)
    return parsed


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _emit_checkpoint(callback, name: str):
    if callback is not None:
        callback(name)


def _sqlite_sidecars(database_path: Path) -> tuple[str, ...]:
    database_name = database_path.name
    direct = {
        os.path.normcase(database_name + "-journal"),
        os.path.normcase(database_name + "-wal"),
        os.path.normcase(database_name + "-shm"),
    }
    master = os.path.normcase(database_name + "-mj")
    super_journal = os.path.normcase(database_name + "-super-journal")
    found = []
    try:
        with os.scandir(database_path.parent) as entries:
            for entry in entries:
                comparable = os.path.normcase(entry.name)
                if comparable in direct or comparable.startswith(master) or comparable.startswith(super_journal):
                    found.append(entry.name)
    except OSError:
        raise _error("TARGET_VALIDATION_FAILED", 3) from None
    return tuple(sorted(found))


def _require_no_sqlite_sidecars(database_path: Path):
    if _sqlite_sidecars(database_path):
        raise _error("DATABASE_ATTESTATION_FAILED", 3)


def _validate_recoverable_hot_journal(
    database_path: Path,
    sidecars: tuple[str, ...],
):
    expected_name = database_path.name + "-journal"
    if sidecars != (expected_name,):
        raise _error("DATABASE_ATTESTATION_FAILED", 3)
    journal_path = database_path.with_name(expected_name)
    _require_safe_components(journal_path)
    try:
        metadata = os.lstat(journal_path)
        database_metadata = os.lstat(database_path)
    except OSError:
        raise _error("DATABASE_ATTESTATION_FAILED", 3) from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or _is_reparse(metadata)
        or metadata.st_size < 512
        or metadata.st_dev != database_metadata.st_dev
    ):
        raise _error("DATABASE_ATTESTATION_FAILED", 3)
    if os.name == "posix" and (
        metadata.st_uid != os.geteuid()
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise _error("DATABASE_ATTESTATION_FAILED", 3)


def _retire_nonhot_recovery_journal(database_path: Path):
    journal_path = database_path.with_name(database_path.name + "-journal")
    try:
        before_metadata = os.lstat(journal_path)
        before = _identity(before_metadata)
    except OSError:
        raise _error("DATABASE_ATTESTATION_FAILED", 3) from None
    if (
        not stat.S_ISREG(before.mode)
        or before.links != 1
        or before.size < 512
        or _is_reparse(before_metadata)
    ):
        raise _error("DATABASE_ATTESTATION_FAILED", 3)
    descriptor = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(journal_path, flags)
        os.set_inheritable(descriptor, False)
        opened = _identity(os.fstat(descriptor))
        header = os.read(descriptor, 8)
        current = _identity(os.lstat(journal_path))
        if (
            os.get_inheritable(descriptor)
            or not _same_stable_identity(before, opened)
            or not _same_stable_identity(before, current)
            or header != b"\x00" * 8
        ):
            raise _error("DATABASE_ATTESTATION_FAILED", 3)
        os.close(descriptor)
        descriptor = None
        if not _same_stable_identity(before, _identity(os.lstat(journal_path))):
            raise _error("DATABASE_ATTESTATION_FAILED", 3)
        os.unlink(journal_path)
        if os.name == "posix":
            parent_descriptor = None
            try:
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                flags |= getattr(os, "O_CLOEXEC", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                parent_descriptor = os.open(database_path.parent, flags)
                os.fsync(parent_descriptor)
            finally:
                if parent_descriptor is not None:
                    os.close(parent_descriptor)
    except PrivateBetaInvitationOperationError:
        raise
    except BaseException:
        raise _error("DATABASE_ATTESTATION_FAILED", 3) from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except BaseException:
                raise _error("CLEANUP_INCOMPLETE", 7) from None
            if not _descriptor_is_terminal(descriptor):
                raise _error("CLEANUP_INCOMPLETE", 7)


def _prepare_output_target(
    value,
    targets: _TargetSet,
    *,
    checkpoint=None,
) -> _OutputTarget:
    final_path = _validated_absolute_path(value, code="OUTPUT_FORM_INVALID")
    try:
        parent_path = final_path.parent.resolve(strict=True)
    except (OSError, RuntimeError):
        raise _error("OUTPUT_FORM_INVALID", 2) from None
    if parent_path != final_path.parent or not final_path.name:
        raise _error("OUTPUT_FORM_INVALID", 2)
    if not _outside_project_root(final_path, targets.project_root):
        raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6)
    try:
        _require_safe_components(parent_path, allow_sticky_shared=False)
    except BaseException:
        raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6) from None
    try:
        parent_metadata = os.lstat(parent_path)
    except OSError:
        raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6) from None
    if not stat.S_ISDIR(parent_metadata.st_mode) or _is_reparse(parent_metadata):
        raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6)
    parent_identity = _identity(parent_metadata)
    output_binding = _semantic_digest(
        {
            "credential_output": os.fspath(final_path),
            "protocol": OUTPUT_BINDING_PROTOCOL,
        }
    )
    stage_path = parent_path / (
        ".pb-ops-1-"
        + hashlib.sha256(os.fspath(final_path).encode("utf-8")).hexdigest()
        + ".pending"
    )
    path_keys = {
        os.path.normcase(os.fspath(final_path)),
        os.path.normcase(os.fspath(stage_path)),
        os.path.normcase(os.fspath(targets.configuration.path)),
        os.path.normcase(os.fspath(targets.database_path)),
        os.path.normcase(os.fspath(targets.key_path)),
        os.path.normcase(os.fspath(targets.coordination_path)),
    }
    if len(path_keys) != 6:
        raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6)
    output = _OutputTarget(
        final_path=final_path,
        stage_path=stage_path,
        parent_path=parent_path,
        parent_identity=parent_identity,
        output_binding=output_binding,
    )
    try:
        if os.name == "posix":
            if (
                parent_metadata.st_uid != os.geteuid()
                or parent_metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
            ):
                raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6)
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(parent_path, flags)
            os.set_inheritable(descriptor, False)
            opened = _identity(os.fstat(descriptor))
            if os.get_inheritable(descriptor) or not _same_stable_identity(parent_identity, opened):
                os.close(descriptor)
                raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6)
            output.directory_descriptor = descriptor
            _require_posix_publication_support()
        elif os.name == "nt":
            _windows_require_supported_local_volume(parent_path)
            _windows_validate_private_dacl(parent_path)
            output.windows_directory_handle = _windows_open_directory(parent_path)
        else:
            raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6)
        _revalidate_output_parent(output)
        _validate_initial_output_files(output)
        return output
    except PrivateBetaInvitationOperationError as exc:
        report = _close_with_one_retry(output, checkpoint=checkpoint)
        if not report.terminal:
            _retain_cleanup_authorities(output=output)
        if not report.terminal or report.exception_observed:
            raise _error("CLEANUP_INCOMPLETE", 7) from None
        if exc.exit_code in {6, 7}:
            raise
        raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6) from None
    except BaseException:
        report = _close_with_one_retry(output, checkpoint=checkpoint)
        if not report.terminal:
            _retain_cleanup_authorities(output=output)
        if not report.terminal or report.exception_observed:
            raise _error("CLEANUP_INCOMPLETE", 7) from None
        raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6) from None


def _revalidate_output_parent(output: _OutputTarget):
    try:
        current_metadata = os.lstat(output.parent_path)
        current = _identity(current_metadata)
    except OSError:
        raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6) from None
    if (
        not stat.S_ISDIR(current.mode)
        or _is_reparse(current_metadata)
        or not _same_object_identity(output.parent_identity, current)
    ):
        raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6)
    if os.name == "posix":
        descriptor = output.directory_descriptor
        if descriptor is None:
            raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6)
        try:
            opened = _identity(os.fstat(descriptor))
        except OSError:
            raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6) from None
        if (
            os.get_inheritable(descriptor)
            or not _same_object_identity(output.parent_identity, opened)
            or current_metadata.st_uid != os.geteuid()
            or current_metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        ):
            raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6)
    elif os.name == "nt":
        _windows_require_supported_local_volume(output.parent_path)
        _windows_validate_private_dacl(output.parent_path)
        _windows_verify_directory_handle(
            output.windows_directory_handle,
            output.parent_identity,
        )
    else:
        raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6)


def _optional_lstat(path: Path):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6) from None


def _validate_optional_output_file(output: _OutputTarget, *, final: bool):
    path = output.final_path if final else output.stage_path
    metadata = _optional_lstat(path)
    if metadata is None:
        return None
    return _validate_output_file(path, output=output)


def _validate_initial_output_files(output: _OutputTarget):
    final_metadata = _optional_lstat(output.final_path)
    stage_metadata = _optional_lstat(output.stage_path)
    if (
        os.name == "posix"
        and final_metadata is not None
        and stage_metadata is not None
        and stat.S_ISREG(final_metadata.st_mode)
        and stat.S_ISREG(stage_metadata.st_mode)
        and not _is_reparse(final_metadata)
        and not _is_reparse(stage_metadata)
        and final_metadata.st_dev == stage_metadata.st_dev
        and final_metadata.st_ino == stage_metadata.st_ino
        and final_metadata.st_nlink == 2
        and stage_metadata.st_nlink == 2
        and final_metadata.st_size == stage_metadata.st_size
        and 1 <= final_metadata.st_size <= ENVELOPE_MAX_BYTES
        and final_metadata.st_uid == os.geteuid()
        and stage_metadata.st_uid == os.geteuid()
        and stat.S_IMODE(final_metadata.st_mode) == 0o600
        and stat.S_IMODE(stage_metadata.st_mode) == 0o600
    ):
        # This is only provisional admission to the operation.  The pair is
        # opened without symlink following, authenticated against the exact
        # committed row, and revalidated before either name may be removed.
        return
    if final_metadata is not None:
        _validate_output_file(output.final_path, output=output)
    if stage_metadata is not None:
        _validate_output_file(output.stage_path, output=output)


def _validate_output_file(path: Path, *, output: _OutputTarget) -> _FileIdentity:
    _revalidate_output_parent(output)
    try:
        metadata = os.lstat(path)
    except OSError:
        raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6) from None
    actual = _identity(metadata)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or _is_reparse(metadata)
        or metadata.st_size <= 0
        or metadata.st_size > ENVELOPE_MAX_BYTES
    ):
        raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6)
    if os.name == "posix":
        if (
            metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6)
        descriptor = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(
                path.name,
                flags,
                dir_fd=output.directory_descriptor,
            )
            os.set_inheritable(descriptor, False)
            opened = _identity(os.fstat(descriptor))
            current = _identity(os.lstat(path))
            if (
                os.get_inheritable(descriptor)
                or not _same_stable_identity(actual, opened)
                or not _same_stable_identity(actual, current)
            ):
                raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6)
        finally:
            if descriptor is not None:
                os.close(descriptor)
    elif os.name == "nt":
        _windows_validate_private_dacl(path)
        handle = _windows_open_existing_file(path, write=False)
        try:
            _windows_verify_file_handle(handle, path, actual)
        finally:
            _windows_close_handle(handle)
    else:
        raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6)
    return actual


def _flush_output_directory(output: _OutputTarget):
    _revalidate_output_parent(output)
    if os.name == "posix":
        try:
            os.fsync(output.directory_descriptor)
        except OSError:
            raise _error("CREDENTIAL_PUBLICATION_FAILED", 6) from None
    elif os.name == "nt":
        # Windows exposes durable file flush and write-through rename but no
        # portable directory-fsync contract.  The retained native directory
        # handle is still revalidated at every publication boundary.
        _windows_verify_directory_handle(
            output.windows_directory_handle,
            output.parent_identity,
        )
    else:
        raise _error("CREDENTIAL_PUBLICATION_FAILED", 6)


def _create_stage_file(output: _OutputTarget, payload: bytes, *, checkpoint=None):
    created = []
    try:
        return _create_stage_file_impl(
            output,
            payload,
            checkpoint=checkpoint,
            created=created,
        )
    except BaseException:
        if created:
            try:
                _discard_partial_stage(output, created[0])
            except BaseException:
                raise _error("CLEANUP_INCOMPLETE", 7) from None
        raise


def _create_stage_file_impl(
    output: _OutputTarget,
    payload: bytes,
    *,
    checkpoint=None,
    created,
):
    if type(payload) is not bytes or not 1 <= len(payload) <= ENVELOPE_MAX_BYTES:
        raise _error("INTERNAL_FAILURE", 7)
    _revalidate_output_parent(output)
    _emit_checkpoint(checkpoint, "before_stage_create")
    if os.name == "posix":
        descriptor = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(
                output.stage_path.name,
                flags,
                0o600,
                dir_fd=output.directory_descriptor,
            )
            created.append(_identity(os.fstat(descriptor)))
            os.set_inheritable(descriptor, False)
            os.fchmod(descriptor, 0o600)
            if os.get_inheritable(descriptor):
                raise OSError()
            # Establish an authenticated reclaimable stage before exposing any
            # abrupt-death checkpoint.  A short-write failure still leaves an
            # unauthenticated file that is never reclaimed automatically.
            _write_all(descriptor, payload)
            _emit_checkpoint(checkpoint, "after_stage_create")
            _emit_checkpoint(checkpoint, "mid_stage_write")
            _emit_checkpoint(checkpoint, "after_stage_write")
            os.fsync(descriptor)
            _emit_checkpoint(checkpoint, "after_stage_flush")
            opened = _identity(os.fstat(descriptor))
            if (
                not stat.S_ISREG(opened.mode)
                or opened.links != 1
                or opened.size != len(payload)
                or stat.S_IMODE(opened.mode) != 0o600
            ):
                raise OSError()
            os.close(descriptor)
            descriptor = None
            _emit_checkpoint(checkpoint, "after_stage_close")
        except FileExistsError:
            raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6) from None
        except PrivateBetaInvitationOperationError:
            raise
        except BaseException:
            raise _error("CREDENTIAL_PUBLICATION_FAILED", 6) from None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except BaseException:
                    raise _error("CLEANUP_INCOMPLETE", 7) from None
                if not _descriptor_is_terminal(descriptor):
                    raise _error("CLEANUP_INCOMPLETE", 7)
    elif os.name == "nt":
        handle = _windows_create_private_file(output.stage_path)
        created.append(_identity(os.lstat(output.stage_path)))
        try:
            _windows_write_all(handle, payload)
            _emit_checkpoint(checkpoint, "after_stage_create")
            _emit_checkpoint(checkpoint, "mid_stage_write")
            _emit_checkpoint(checkpoint, "after_stage_write")
            _windows_flush_file(handle)
            _emit_checkpoint(checkpoint, "after_stage_flush")
            _windows_verify_file_handle(
                handle,
                output.stage_path,
                _identity(os.lstat(output.stage_path)),
                expected_size=len(payload),
            )
        except PrivateBetaInvitationOperationError:
            raise
        except BaseException:
            raise _error("CREDENTIAL_PUBLICATION_FAILED", 6) from None
        finally:
            _windows_close_handle(handle)
        _emit_checkpoint(checkpoint, "after_stage_close")
    else:
        raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6)
    _validate_output_file(output.stage_path, output=output)
    _flush_output_directory(output)
    _emit_checkpoint(checkpoint, "after_stage_directory_flush")


def _discard_partial_stage(output: _OutputTarget, created: _FileIdentity):
    _revalidate_output_parent(output)
    metadata = _optional_lstat(output.stage_path)
    if metadata is None:
        return
    current = _identity(metadata)
    if (
        not _same_object_identity(created, current)
        or not stat.S_ISREG(current.mode)
        or current.links != 1
        or _is_reparse(metadata)
    ):
        raise _error("CLEANUP_INCOMPLETE", 7)
    if os.name == "posix":
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise _error("CLEANUP_INCOMPLETE", 7)
        descriptor = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(
                output.stage_path.name,
                flags,
                dir_fd=output.directory_descriptor,
            )
            opened = _identity(os.fstat(descriptor))
            if not _same_stable_identity(current, opened):
                raise _error("CLEANUP_INCOMPLETE", 7)
        finally:
            if descriptor is not None:
                os.close(descriptor)
        os.unlink(output.stage_path.name, dir_fd=output.directory_descriptor)
    elif os.name == "nt":
        _windows_validate_private_dacl(output.stage_path)
        handle = _windows_open_existing_file(output.stage_path, write=False)
        try:
            _windows_verify_file_handle(handle, output.stage_path, current)
        finally:
            _windows_close_handle(handle)
        if not _same_object_identity(current, _identity(os.lstat(output.stage_path))):
            raise _error("CLEANUP_INCOMPLETE", 7)
        os.unlink(output.stage_path)
    else:
        raise _error("CLEANUP_INCOMPLETE", 7)
    _flush_output_directory(output)


def _write_all(descriptor: int, payload: bytes):
    offset = 0
    while offset < len(payload):
        count = os.write(descriptor, payload[offset:])
        if type(count) is not int or count <= 0:
            raise OSError()
        offset += count


def _read_output_file(path: Path, *, output: _OutputTarget) -> bytes:
    expected = _validate_output_file(path, output=output)
    if os.name == "posix":
        descriptor = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(
                path.name,
                flags,
                dir_fd=output.directory_descriptor,
            )
            os.set_inheritable(descriptor, False)
            opened = _identity(os.fstat(descriptor))
            if os.get_inheritable(descriptor) or not _same_stable_identity(expected, opened):
                raise OSError()
            payload = bytearray()
            while len(payload) <= ENVELOPE_MAX_BYTES:
                chunk = os.read(descriptor, min(1024, ENVELOPE_MAX_BYTES + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
            after = _identity(os.fstat(descriptor))
            if not _same_stable_identity(expected, after) or len(payload) != expected.size:
                raise OSError()
            return bytes(payload)
        except PrivateBetaInvitationOperationError:
            raise
        except BaseException:
            raise _error("CREDENTIAL_RECOVERY_UNAVAILABLE", 6) from None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except BaseException:
                    raise _error("CLEANUP_INCOMPLETE", 7) from None
                if not _descriptor_is_terminal(descriptor):
                    raise _error("CLEANUP_INCOMPLETE", 7)
    if os.name == "nt":
        handle = _windows_open_existing_file(path, write=False)
        try:
            _windows_verify_file_handle(handle, path, expected)
            payload = _windows_read_bounded(handle, ENVELOPE_MAX_BYTES)
            _windows_verify_file_handle(handle, path, expected)
            if len(payload) != expected.size:
                raise OSError()
            return payload
        except PrivateBetaInvitationOperationError:
            raise
        except BaseException:
            raise _error("CREDENTIAL_RECOVERY_UNAVAILABLE", 6) from None
        finally:
            _windows_close_handle(handle)
    raise _error("CREDENTIAL_RECOVERY_UNAVAILABLE", 6)


def _read_posix_double_link_payload(
    output: _OutputTarget,
) -> tuple[bytes, _FileIdentity]:
    if os.name != "posix":
        raise _error("CREDENTIAL_RECOVERY_UNAVAILABLE", 6)
    _revalidate_output_parent(output)
    descriptors = []
    try:
        stage_metadata = os.lstat(output.stage_path)
        final_metadata = os.lstat(output.final_path)
        stage = _identity(stage_metadata)
        final = _identity(final_metadata)
        if (
            not stat.S_ISREG(stage.mode)
            or not stat.S_ISREG(final.mode)
            or stat.S_ISLNK(stage.mode)
            or stat.S_ISLNK(final.mode)
            or _is_reparse(stage_metadata)
            or _is_reparse(final_metadata)
            or stage.device != final.device
            or stage.inode != final.inode
            or stage.links != 2
            or final.links != 2
            or stage.size != final.size
            or not 1 <= stage.size <= ENVELOPE_MAX_BYTES
            or stage_metadata.st_uid != os.geteuid()
            or final_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(stage.mode) != 0o600
            or stat.S_IMODE(final.mode) != 0o600
        ):
            raise _error("CREDENTIAL_RECOVERY_UNAVAILABLE", 6)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        for name in (output.stage_path.name, output.final_path.name):
            descriptor = os.open(
                name,
                flags,
                dir_fd=output.directory_descriptor,
            )
            descriptors.append(descriptor)
            os.set_inheritable(descriptor, False)
        opened = tuple(_identity(os.fstat(item)) for item in descriptors)
        if any(os.get_inheritable(item) for item in descriptors) or any(
            value.device != stage.device
            or value.inode != stage.inode
            or value.links != 2
            or value.size != stage.size
            or not stat.S_ISREG(value.mode)
            for value in opened
        ):
            raise _error("CREDENTIAL_RECOVERY_UNAVAILABLE", 6)
        payload = bytearray()
        while len(payload) <= ENVELOPE_MAX_BYTES:
            chunk = os.read(
                descriptors[1],
                min(1024, ENVELOPE_MAX_BYTES + 1 - len(payload)),
            )
            if not chunk:
                break
            payload.extend(chunk)
        stage_after = _identity(os.lstat(output.stage_path))
        final_after = _identity(os.lstat(output.final_path))
        opened_after = tuple(_identity(os.fstat(item)) for item in descriptors)
        if (
            len(payload) != stage.size
            or not _same_stable_identity(stage, stage_after)
            or not _same_stable_identity(final, final_after)
            or any(
                not _same_stable_identity(stage, value)
                for value in opened_after
            )
        ):
            raise _error("CREDENTIAL_RECOVERY_UNAVAILABLE", 6)
        return bytes(payload), stage
    except PrivateBetaInvitationOperationError:
        raise
    except BaseException:
        raise _error("CREDENTIAL_RECOVERY_UNAVAILABLE", 6) from None
    finally:
        cleanup_incomplete = False
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except BaseException:
                cleanup_incomplete = True
            if not _descriptor_is_terminal(descriptor):
                cleanup_incomplete = True
        if cleanup_incomplete:
            raise _error("CLEANUP_INCOMPLETE", 7) from None


def _recover_posix_double_link(
    output: _OutputTarget,
    *,
    targets: _TargetSet,
    ownership,
    session: _DatabaseSession,
    key: bytes,
    request_id: str,
    request_fingerprint: str,
    row,
    state: _OperationState,
    checkpoint=None,
):
    payload, expected = _read_posix_double_link_payload(output)
    _emit_checkpoint(checkpoint, "during_double_link_recovery_validation")
    _authenticate_envelope(
        payload,
        targets=targets,
        output=output,
        key=key,
        request_id=request_id,
        request_fingerprint=request_fingerprint,
        expected_row=row,
    )
    _require_authentic_ownership(ownership, targets)
    _revalidate_targets(targets)
    _validate_database_descriptor(session, targets, stable=True)
    _revalidate_output_parent(output)
    stage = _identity(os.lstat(output.stage_path))
    final = _identity(os.lstat(output.final_path))
    if (
        not _same_stable_identity(expected, stage)
        or not _same_stable_identity(expected, final)
        or stage.links != 2
        or final.links != 2
    ):
        raise _error("CREDENTIAL_RECOVERY_UNAVAILABLE", 6)
    state.begin_credential_publication()
    _emit_checkpoint(checkpoint, "before_double_link_stage_unlink")
    try:
        os.unlink(
            output.stage_path.name,
            dir_fd=output.directory_descriptor,
        )
    except BaseException:
        raise _error("CREDENTIAL_PUBLICATION_FAILED", 6) from None
    _emit_checkpoint(checkpoint, "after_double_link_unlink_before_directory_flush")
    _flush_output_directory(output)
    _emit_checkpoint(checkpoint, "after_double_link_directory_flush_before_confirmation")
    final_payload = _read_output_file(output.final_path, output=output)
    _authenticate_envelope(
        final_payload,
        targets=targets,
        output=output,
        key=key,
        request_id=request_id,
        request_fingerprint=request_fingerprint,
        expected_row=row,
    )
    final_identity = _validate_output_file(output.final_path, output=output)
    if final_identity.links != 1 or _optional_lstat(output.stage_path) is not None:
        raise _error("CREDENTIAL_PUBLICATION_FAILED", 6)
    state.confirm_credential_publication()
    _emit_checkpoint(checkpoint, "after_double_link_recovery_confirmed")


def _remove_stage(output: _OutputTarget):
    expected = _validate_output_file(output.stage_path, output=output)
    try:
        if os.name == "posix":
            current = _identity(os.lstat(output.stage_path))
            if not _same_stable_identity(expected, current):
                raise OSError()
            os.unlink(output.stage_path.name, dir_fd=output.directory_descriptor)
        elif os.name == "nt":
            current = _identity(os.lstat(output.stage_path))
            if not _same_stable_identity(expected, current):
                raise OSError()
            os.unlink(output.stage_path)
        else:
            raise OSError()
        _flush_output_directory(output)
    except PrivateBetaInvitationOperationError:
        raise
    except BaseException:
        raise _error("CREDENTIAL_PUBLICATION_FAILED", 6) from None


def _publish_stage(
    output: _OutputTarget,
    *,
    state: _OperationState,
    checkpoint=None,
):
    _revalidate_output_parent(output)
    _validate_output_file(output.stage_path, output=output)
    if _optional_lstat(output.final_path) is not None:
        raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6)
    state.begin_credential_publication()
    _emit_checkpoint(checkpoint, "before_publication")
    try:
        if os.name == "nt":
            _windows_move_no_replace_write_through(
                output.stage_path,
                output.final_path,
            )
        elif os.name == "posix":
            _posix_rename_no_replace(output, checkpoint=checkpoint)
        else:
            raise OSError()
    except FileExistsError:
        raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6) from None
    except PrivateBetaInvitationOperationError:
        raise
    except BaseException:
        raise _error("CREDENTIAL_PUBLICATION_FAILED", 6) from None
    _emit_checkpoint(checkpoint, "after_publication_syscall")
    _flush_output_directory(output)
    _emit_checkpoint(checkpoint, "after_publication_directory_flush")
    _validate_output_file(output.final_path, output=output)
    if _optional_lstat(output.stage_path) is not None:
        raise _error("CREDENTIAL_PUBLICATION_FAILED", 6)
    state.confirm_credential_publication()
    _emit_checkpoint(checkpoint, "after_publication_confirmed")


def _require_posix_publication_support():
    if os.name != "posix" or not hasattr(os, "link") or not hasattr(os, "unlink"):
        raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6)


def _posix_rename_no_replace(output: _OutputTarget, *, checkpoint=None):
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is not None:
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            output.directory_descriptor,
            os.fsencode(output.stage_path.name),
            output.directory_descriptor,
            os.fsencode(output.final_path.name),
            1,
        )
        if result == 0:
            return
        error_number = ctypes.get_errno()
        if error_number in {17, 183}:
            raise FileExistsError()
        if error_number not in {38, 95}:
            raise OSError(error_number, "rename_no_replace_failed")
    # linkat creates the final name atomically and never replaces it.  The
    # immediately following unlink retires the same-directory stage name.
    try:
        os.link(
            output.stage_path.name,
            output.final_path.name,
            src_dir_fd=output.directory_descriptor,
            dst_dir_fd=output.directory_descriptor,
            follow_symlinks=False,
        )
    except FileExistsError:
        raise
    _emit_checkpoint(checkpoint, "after_posix_link_before_unlink")
    os.unlink(output.stage_path.name, dir_fd=output.directory_descriptor)
    _emit_checkpoint(checkpoint, "after_posix_unlink_before_directory_flush")


if os.name == "nt":
    from ctypes import wintypes

    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ADVAPI32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _FILE_READ_ATTRIBUTES = 0x80
    _FILE_SHARE_READ = 0x1
    _FILE_SHARE_WRITE = 0x2
    _FILE_SHARE_DELETE = 0x4
    _CREATE_NEW = 1
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_NORMAL = 0x80
    _FILE_FLAG_WRITE_THROUGH = 0x80000000
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _MOVEFILE_WRITE_THROUGH = 0x8
    _HANDLE_FLAG_INHERIT = 0x1
    _TOKEN_QUERY = 0x8
    _TOKEN_USER = 1
    _SE_FILE_OBJECT = 1
    _OWNER_SECURITY_INFORMATION = 0x1
    _DACL_SECURITY_INFORMATION = 0x4
    _PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
    _SE_DACL_PROTECTED = 0x1000
    _ACCESS_ALLOWED_ACE_TYPE = 0
    _INHERITED_ACE = 0x10
    _FILE_ALL_ACCESS = 0x001F01FF
    _GENERIC_ALL = 0x10000000
    _SDDL_REVISION_1 = 1

    class _SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        ]

    class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    class _FILE_ID_128(ctypes.Structure):
        _fields_ = [("Identifier", ctypes.c_ubyte * 16)]

    class _FILE_ID_INFO(ctypes.Structure):
        _fields_ = [
            ("VolumeSerialNumber", ctypes.c_ulonglong),
            ("FileId", _FILE_ID_128),
        ]

    class _ACL(ctypes.Structure):
        _fields_ = [
            ("AclRevision", ctypes.c_ubyte),
            ("Sbz1", ctypes.c_ubyte),
            ("AclSize", wintypes.WORD),
            ("AceCount", wintypes.WORD),
            ("Sbz2", wintypes.WORD),
        ]

    class _ACE_HEADER(ctypes.Structure):
        _fields_ = [
            ("AceType", ctypes.c_ubyte),
            ("AceFlags", ctypes.c_ubyte),
            ("AceSize", wintypes.WORD),
        ]

    _KERNEL32.GetCurrentProcess.argtypes = []
    _KERNEL32.GetCurrentProcess.restype = wintypes.HANDLE
    _KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
    _KERNEL32.CloseHandle.restype = wintypes.BOOL
    _KERNEL32.GetHandleInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _KERNEL32.GetHandleInformation.restype = wintypes.BOOL
    _KERNEL32.SetHandleInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    _KERNEL32.SetHandleInformation.restype = wintypes.BOOL
    _KERNEL32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_SECURITY_ATTRIBUTES),
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _KERNEL32.CreateFileW.restype = wintypes.HANDLE
    _KERNEL32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION),
    ]
    _KERNEL32.GetFileInformationByHandle.restype = wintypes.BOOL
    _KERNEL32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _KERNEL32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    _KERNEL32.WriteFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    _KERNEL32.WriteFile.restype = wintypes.BOOL
    _KERNEL32.ReadFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    _KERNEL32.ReadFile.restype = wintypes.BOOL
    _KERNEL32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    _KERNEL32.FlushFileBuffers.restype = wintypes.BOOL
    _KERNEL32.SetFilePointerEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_longlong,
        ctypes.POINTER(ctypes.c_longlong),
        wintypes.DWORD,
    ]
    _KERNEL32.SetFilePointerEx.restype = wintypes.BOOL
    _KERNEL32.MoveFileExW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
    ]
    _KERNEL32.MoveFileExW.restype = wintypes.BOOL
    _KERNEL32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    _KERNEL32.GetDriveTypeW.restype = wintypes.UINT
    _KERNEL32.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    _KERNEL32.GetVolumeInformationW.restype = wintypes.BOOL
    _KERNEL32.LocalFree.argtypes = [wintypes.HLOCAL]
    _KERNEL32.LocalFree.restype = wintypes.HLOCAL

    _ADVAPI32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    _ADVAPI32.OpenProcessToken.restype = wintypes.BOOL
    _ADVAPI32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _ADVAPI32.GetTokenInformation.restype = wintypes.BOOL
    _ADVAPI32.ConvertSidToStringSidW.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    _ADVAPI32.ConvertSidToStringSidW.restype = wintypes.BOOL
    _ADVAPI32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.ULONG),
    ]
    _ADVAPI32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    _ADVAPI32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
    ]
    _ADVAPI32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    _ADVAPI32.GetSecurityDescriptorControl.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    _ADVAPI32.GetSecurityDescriptorControl.restype = wintypes.BOOL
    _ADVAPI32.GetAce.argtypes = [
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
    ]
    _ADVAPI32.GetAce.restype = wintypes.BOOL
    _ADVAPI32.SetFileSecurityW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
    ]
    _ADVAPI32.SetFileSecurityW.restype = wintypes.BOOL


def _windows_raise():
    raise OSError(ctypes.get_last_error(), "windows_native_operation_failed")


def _windows_close_handle(handle):
    if os.name != "nt" or handle in {None, 0, -1, _INVALID_HANDLE_VALUE}:
        return
    if not _KERNEL32.CloseHandle(wintypes.HANDLE(handle)):
        _windows_raise()


def _windows_handle_is_terminal(handle) -> bool:
    if handle in {None, 0, -1}:
        return True
    if os.name != "nt":
        return False
    flags = wintypes.DWORD()
    if _KERNEL32.GetHandleInformation(
        wintypes.HANDLE(handle),
        ctypes.byref(flags),
    ):
        return False
    return ctypes.get_last_error() == 6


def _windows_set_non_inheritable(handle):
    if not _KERNEL32.SetHandleInformation(
        wintypes.HANDLE(handle),
        _HANDLE_FLAG_INHERIT,
        0,
    ):
        _windows_raise()


def _windows_current_user_sid() -> str:
    if os.name != "nt":
        raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6)
    process = _KERNEL32.GetCurrentProcess()
    token = wintypes.HANDLE()
    if not _ADVAPI32.OpenProcessToken(process, _TOKEN_QUERY, ctypes.byref(token)):
        _windows_raise()
    try:
        required = wintypes.DWORD()
        _ADVAPI32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(required))
        if required.value == 0:
            _windows_raise()
        buffer = ctypes.create_string_buffer(required.value)
        if not _ADVAPI32.GetTokenInformation(
            token,
            _TOKEN_USER,
            buffer,
            required,
            ctypes.byref(required),
        ):
            _windows_raise()
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        return _windows_sid_string(sid_pointer)
    finally:
        _windows_close_handle(token.value)


def _windows_sid_string(sid_pointer) -> str:
    text_pointer = wintypes.LPWSTR()
    if not _ADVAPI32.ConvertSidToStringSidW(
        ctypes.c_void_p(sid_pointer),
        ctypes.byref(text_pointer),
    ):
        _windows_raise()
    try:
        return text_pointer.value
    finally:
        _KERNEL32.LocalFree(text_pointer)


def _windows_private_security_descriptor():
    sid = _windows_current_user_sid()
    sddl = f"O:{sid}G:{sid}D:P(A;;FA;;;{sid})"
    descriptor = ctypes.c_void_p()
    size = wintypes.ULONG()
    if not _ADVAPI32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        _SDDL_REVISION_1,
        ctypes.byref(descriptor),
        ctypes.byref(size),
    ):
        _windows_raise()
    return descriptor


def _windows_validate_private_dacl(path: Path):
    if os.name != "nt":
        return
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    result = _ADVAPI32.GetNamedSecurityInfoW(
        os.fspath(path),
        _SE_FILE_OBJECT,
        _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if result != 0:
        raise OSError(result, "windows_security_query_failed")
    try:
        current_sid = _windows_current_user_sid()
        if _windows_sid_string(owner.value) != current_sid or not dacl.value:
            raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6)
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not _ADVAPI32.GetSecurityDescriptorControl(
            descriptor,
            ctypes.byref(control),
            ctypes.byref(revision),
        ) or not control.value & _SE_DACL_PROTECTED:
            raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6)
        acl = ctypes.cast(dacl, ctypes.POINTER(_ACL)).contents
        allowed_sids = {
            current_sid,
            "S-1-5-18",
            "S-1-5-32-544",
        }
        current_full_control = False
        for index in range(acl.AceCount):
            ace = ctypes.c_void_p()
            if not _ADVAPI32.GetAce(dacl, index, ctypes.byref(ace)):
                _windows_raise()
            header = ctypes.cast(ace, ctypes.POINTER(_ACE_HEADER)).contents
            if (
                header.AceType != _ACCESS_ALLOWED_ACE_TYPE
                or header.AceFlags & _INHERITED_ACE
                or header.AceSize < 12
            ):
                raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6)
            mask = ctypes.c_uint32.from_address(ace.value + 4).value
            sid_text = _windows_sid_string(ace.value + 8)
            if sid_text not in allowed_sids:
                raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6)
            if sid_text == current_sid and (
                mask & _GENERIC_ALL or mask & _FILE_ALL_ACCESS == _FILE_ALL_ACCESS
            ):
                current_full_control = True
        if not current_full_control:
            raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6)
    finally:
        if descriptor.value:
            _KERNEL32.LocalFree(descriptor)


def _windows_require_supported_local_volume(path: Path):
    if os.name != "nt":
        return
    text = os.fspath(path)
    if text.startswith("\\\\"):
        raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6)
    drive, tail = os.path.splitdrive(text)
    if not drive or ":" in tail:
        raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6)
    root = drive + "\\"
    if _KERNEL32.GetDriveTypeW(root) != 3:
        raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6)
    filesystem = ctypes.create_unicode_buffer(64)
    if not _KERNEL32.GetVolumeInformationW(
        root,
        None,
        0,
        None,
        None,
        None,
        filesystem,
        len(filesystem),
    ):
        _windows_raise()
    if filesystem.value.upper() not in {"NTFS", "REFS"}:
        raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6)


def _windows_open_directory(path: Path):
    handle = _KERNEL32.CreateFileW(
        os.fspath(path),
        _FILE_READ_ATTRIBUTES,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        _windows_raise()
    try:
        _windows_set_non_inheritable(handle)
        return handle
    except BaseException:
        _windows_close_handle(handle)
        raise


def _windows_handle_information(handle):
    information = _BY_HANDLE_FILE_INFORMATION()
    if not _KERNEL32.GetFileInformationByHandle(
        wintypes.HANDLE(handle),
        ctypes.byref(information),
    ):
        _windows_raise()
    return information


def _windows_file_identity(handle):
    identity = _FILE_ID_INFO()
    if not _KERNEL32.GetFileInformationByHandleEx(
        wintypes.HANDLE(handle),
        18,
        ctypes.byref(identity),
        ctypes.sizeof(identity),
    ):
        _windows_raise()
    inode = int.from_bytes(bytes(identity.FileId.Identifier), "little")
    return identity.VolumeSerialNumber, inode


def _windows_verify_directory_handle(handle, expected: _FileIdentity):
    if os.name != "nt" or handle in {None, 0, -1, _INVALID_HANDLE_VALUE}:
        raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6)
    information = _windows_handle_information(handle)
    device, inode = _windows_file_identity(handle)
    if (
        information.dwFileAttributes & 0x10 == 0
        or information.dwFileAttributes & _WINDOWS_REPARSE_ATTRIBUTE
        or device != expected.device
        or inode != expected.inode
    ):
        raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6)


def _windows_create_private_file(path: Path):
    descriptor = _windows_private_security_descriptor()
    attributes = _SECURITY_ATTRIBUTES(
        ctypes.sizeof(_SECURITY_ATTRIBUTES),
        descriptor,
        False,
    )
    try:
        handle = _KERNEL32.CreateFileW(
            os.fspath(path),
            _GENERIC_READ | _GENERIC_WRITE,
            0,
            ctypes.byref(attributes),
            _CREATE_NEW,
            _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_WRITE_THROUGH,
            None,
        )
    finally:
        _KERNEL32.LocalFree(descriptor)
    if handle == _INVALID_HANDLE_VALUE:
        error_number = ctypes.get_last_error()
        if error_number in {80, 183}:
            raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6)
        raise OSError(error_number, "windows_file_creation_failed")
    try:
        _windows_set_non_inheritable(handle)
        _windows_validate_private_dacl(path)
        information = _windows_handle_information(handle)
        if (
            information.dwFileAttributes & (0x10 | _WINDOWS_REPARSE_ATTRIBUTE)
            or information.nNumberOfLinks != 1
        ):
            raise OSError()
        return handle
    except BaseException:
        _windows_close_handle(handle)
        raise


def _windows_open_existing_file(path: Path, *, write: bool):
    access = _GENERIC_READ | (_GENERIC_WRITE if write else 0)
    handle = _KERNEL32.CreateFileW(
        os.fspath(path),
        access,
        _FILE_SHARE_READ,
        None,
        _OPEN_EXISTING,
        _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        _windows_raise()
    try:
        _windows_set_non_inheritable(handle)
        return handle
    except BaseException:
        _windows_close_handle(handle)
        raise


def _windows_verify_file_handle(
    handle,
    path: Path,
    expected: _FileIdentity,
    *,
    expected_size: int | None = None,
):
    information = _windows_handle_information(handle)
    current = _identity(os.lstat(path))
    device, inode = _windows_file_identity(handle)
    size = (information.nFileSizeHigh << 32) | information.nFileSizeLow
    if (
        information.dwFileAttributes & (0x10 | _WINDOWS_REPARSE_ATTRIBUTE)
        or information.nNumberOfLinks != 1
        or device != expected.device
        or inode != expected.inode
        or not _same_stable_identity(expected, current)
        or (expected_size is not None and size != expected_size)
    ):
        raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6)


def _windows_write_all(handle, payload: bytes):
    offset = 0
    while offset < len(payload):
        chunk = payload[offset : offset + 65_536]
        written = wintypes.DWORD()
        buffer = ctypes.create_string_buffer(chunk)
        if not _KERNEL32.WriteFile(
            wintypes.HANDLE(handle),
            buffer,
            len(chunk),
            ctypes.byref(written),
            None,
        ) or written.value <= 0:
            _windows_raise()
        offset += written.value


def _windows_read_bounded(handle, maximum: int) -> bytes:
    position = wintypes.LARGE_INTEGER(0)
    if not _KERNEL32.SetFilePointerEx(
        wintypes.HANDLE(handle),
        position,
        None,
        0,
    ):
        _windows_raise()
    payload = bytearray()
    while len(payload) <= maximum:
        amount = min(1024, maximum + 1 - len(payload))
        buffer = ctypes.create_string_buffer(amount)
        read = wintypes.DWORD()
        if not _KERNEL32.ReadFile(
            wintypes.HANDLE(handle),
            buffer,
            amount,
            ctypes.byref(read),
            None,
        ):
            _windows_raise()
        if read.value == 0:
            return bytes(payload)
        payload.extend(buffer.raw[: read.value])
    raise OSError()


def _windows_flush_file(handle):
    if not _KERNEL32.FlushFileBuffers(wintypes.HANDLE(handle)):
        _windows_raise()


def _windows_move_no_replace_write_through(source: Path, destination: Path):
    if source.parent != destination.parent:
        raise OSError()
    if not _KERNEL32.MoveFileExW(
        os.fspath(source),
        os.fspath(destination),
        _MOVEFILE_WRITE_THROUGH,
    ):
        error_number = ctypes.get_last_error()
        if error_number in {80, 183}:
            raise FileExistsError()
        raise OSError(error_number, "windows_publication_failed")


def _harden_private_output_directory_for_testing(path):
    """Test support: apply the exact Windows private DACL PB-OPS accepts."""
    if os.name == "posix":
        os.chmod(path, 0o700)
        return Path(path)
    if os.name != "nt":
        raise OSError()
    descriptor = _windows_private_security_descriptor()
    try:
        if not _ADVAPI32.SetFileSecurityW(
            os.fspath(path),
            _OWNER_SECURITY_INFORMATION
            | _DACL_SECURITY_INFORMATION
            | _PROTECTED_DACL_SECURITY_INFORMATION,
            descriptor,
        ):
            _windows_raise()
    finally:
        _KERNEL32.LocalFree(descriptor)
    _windows_validate_private_dacl(Path(path))
    return Path(path)


@dataclass(slots=True, repr=False)
class _DatabaseSession:
    connection: sqlite3.Connection | None
    descriptor: int | None
    descriptor_identity: _FileIdentity
    writable: bool

    def close(self, *, checkpoint=None) -> _CloseReport:
        exception_observed = _checkpoint_exception(
            checkpoint,
            "before_database_close",
        )
        connection = self.connection
        if connection is not None:
            try:
                if connection.in_transaction:
                    connection.rollback()
            except BaseException:
                exception_observed = True
            try:
                connection.close()
            except BaseException:
                exception_observed = True
            if _sqlite_connection_is_terminal(connection):
                self.connection = None
        descriptor = self.descriptor
        if descriptor is not None:
            descriptor_state, _actual = _raw_descriptor_state(
                descriptor,
                self.descriptor_identity,
            )
            if descriptor_state == "live":
                try:
                    os.close(descriptor)
                except BaseException:
                    exception_observed = True
                descriptor_state, _actual = _raw_descriptor_state(
                    descriptor,
                    self.descriptor_identity,
                )
            if descriptor_state in {"terminal", "reused"}:
                self.descriptor = None
                exception_observed = (
                    exception_observed or descriptor_state == "reused"
                )
            elif descriptor_state == "unresolved":
                exception_observed = True
        exception_observed = (
            _checkpoint_exception(checkpoint, "after_database_close")
            or exception_observed
        )
        return _CloseReport(
            terminal=(self.connection is None and self.descriptor is None),
            exception_observed=exception_observed,
        )


def _sqlite_connection_is_terminal(connection) -> bool:
    try:
        connection.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    except BaseException:
        return False
    return False


def _open_database_session(
    targets: _TargetSet,
    ownership,
    *,
    writable: bool,
    state: _OperationState,
    checkpoint=None,
    allow_hot_journal_recovery: bool = False,
    authority: _RawResourceCleanup,
) -> _RawResourceCleanup:
    _require_authentic_ownership(ownership, targets)
    _revalidate_targets(targets)
    sidecars = _sqlite_sidecars(targets.database_path)
    if sidecars:
        if not allow_hot_journal_recovery:
            raise _error("DATABASE_ATTESTATION_FAILED", 3)
        _validate_recoverable_hot_journal(targets.database_path, sidecars)
    if (
        type(authority) is not _RawResourceCleanup
        or authority.kind != "database"
        or any(
            value is not None
            for value in (
                authority.descriptor,
                authority.handle,
                authority.connection,
                authority.session,
                authority.handoff,
            )
        )
    ):
        raise AssertionError("database_handoff_authority_required")
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(targets.database_path, flags)
        authority.adopt_descriptor(descriptor)
        os.set_inheritable(descriptor, False)
        opened = _identity(os.fstat(descriptor))
        if (
            os.get_inheritable(descriptor)
            or not _same_stable_identity(targets.database_identity, opened)
        ):
            raise _error("TARGET_VALIDATION_FAILED", 3)
        os.lseek(descriptor, 0, os.SEEK_SET)
        header = os.read(descriptor, _SQLITE_HEADER_BYTES)
        if (
            len(header) != _SQLITE_HEADER_BYTES
            or header[:16] != b"SQLite format 3\x00"
            or header[18:20] != b"\x01\x01"
        ):
            raise _error("DATABASE_ATTESTATION_FAILED", 3)
        _require_authentic_ownership(ownership, targets)
        _revalidate_targets(targets)
        mode = "rw" if writable else "ro"
        uri = "file:" + quote(targets.database_path.as_posix(), safe="/:") + f"?mode={mode}"
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=0,
            isolation_level=None,
            check_same_thread=True,
        )
        authority.adopt_connection(connection)
        connection.row_factory = None
        connection.text_factory = str
        connection.execute("PRAGMA busy_timeout = 0")
        if not writable:
            connection.execute("PRAGMA query_only = ON")
            if connection.execute("PRAGMA query_only").fetchone() != (1,):
                raise _error("DATABASE_ATTESTATION_FAILED", 3)
        connection.execute("PRAGMA foreign_keys = ON")
        if connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
            raise _error("DATABASE_ATTESTATION_FAILED", 3)
        if writable:
            connection.execute("PRAGMA synchronous = FULL")
            if connection.execute("PRAGMA synchronous").fetchone() not in {(2,), (3,)}:
                raise _error("DATABASE_ATTESTATION_FAILED", 3)
            if os.name == "posix":
                connection.execute("PRAGMA fullfsync = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute("PRAGMA temp_store = MEMORY")
        # A create retry may encounter SQLite's own hot rollback journal after
        # abrupt death.  Opening the exact database under authentic PB-OWN is
        # the only permitted recovery path; every other sidecar shape fails.
        if sidecars:
            connection.execute("SELECT COUNT(*) FROM main.account_invitations").fetchone()
            if _sqlite_sidecars(targets.database_path):
                _retire_nonhot_recovery_journal(targets.database_path)
            _require_no_sqlite_sidecars(targets.database_path)
        _verify_open_database(connection, targets)
        _require_authentic_ownership(ownership, targets)
        _revalidate_targets(targets)
        session = _DatabaseSession(
            connection=connection,
            descriptor=descriptor,
            descriptor_identity=opened,
            writable=writable,
        )
        authority.stage_database_session(session)
        _emit_checkpoint(checkpoint, "before_database_session_delivery")
        return authority
    except PrivateBetaInvitationOperationError:
        raise
    except sqlite3.OperationalError as exc:
        if _sqlite_is_busy(exc):
            raise _error("DATABASE_BUSY", 4) from None
        raise _error("DATABASE_ATTESTATION_FAILED", 3) from None
    except BaseException:
        raise _error("DATABASE_ATTESTATION_FAILED", 3) from None


def _verify_open_database(connection: sqlite3.Connection, targets: _TargetSet):
    cursor = connection.cursor()
    try:
        rows = cursor.execute("PRAGMA database_list").fetchall()
    finally:
        cursor.close()
    if type(rows) is not list or len(rows) != 1:
        raise _error("DATABASE_ATTESTATION_FAILED", 3)
    row = rows[0]
    if (
        type(row) is not tuple
        or len(row) != 3
        or row[0] != 0
        or row[1] != "main"
        or type(row[2]) is not str
    ):
        raise _error("DATABASE_ATTESTATION_FAILED", 3)
    try:
        opened_path = Path(row[2]).resolve(strict=True)
        opened_identity = _identity(os.stat(opened_path))
    except (OSError, RuntimeError):
        raise _error("DATABASE_ATTESTATION_FAILED", 3) from None
    if (
        opened_path != targets.database_path
        or not _same_object_identity(targets.database_identity, opened_identity)
    ):
        raise _error("DATABASE_ATTESTATION_FAILED", 3)


def _attest_database(connection: sqlite3.Connection, targets: _TargetSet):
    if connection.in_transaction:
        raise _error("DATABASE_ATTESTATION_FAILED", 3)
    _require_no_sqlite_sidecars(targets.database_path)
    cursor = connection.cursor()
    rows = []
    total_sql_bytes = 0
    try:
        journal = cursor.execute("PRAGMA main.journal_mode").fetchone()
        foreign_keys = cursor.execute("PRAGMA foreign_keys").fetchone()
        if journal != ("delete",) or foreign_keys != (1,):
            raise _error("DATABASE_ATTESTATION_FAILED", 3)
        cursor.execute(
            "SELECT CAST(type AS BLOB), CAST(name AS BLOB), "
            "CAST(tbl_name AS BLOB), CAST(sql AS BLOB) "
            "FROM main.sqlite_schema "
            "WHERE type IN ('table','index','trigger','view') "
            "ORDER BY type, name, tbl_name "
            f"LIMIT {_EXPECTED_SCHEMA_OBJECT_COUNT + 1}"
        )
        for raw in cursor.fetchall():
            if (
                type(raw) is not tuple
                or len(raw) != 4
                or any(type(value) is not bytes for value in raw[:3])
                or (raw[3] is not None and type(raw[3]) is not bytes)
            ):
                raise _error("DATABASE_ATTESTATION_FAILED", 3)
            kind, name, table_name = (
                value.decode("utf-8", "strict") for value in raw[:3]
            )
            sql = None
            if raw[3] is not None:
                total_sql_bytes += len(raw[3])
                if total_sql_bytes > _MAX_SCHEMA_SQL_BYTES:
                    raise _error("DATABASE_ATTESTATION_FAILED", 3)
                sql = raw[3].decode("utf-8", "strict")
            rows.append((kind, name, table_name, sql))
        marker_rows = cursor.execute(
            "SELECT CAST(version AS BLOB) "
            "FROM main.wahojobs_schema_migrations "
            "ORDER BY version LIMIT 7"
        ).fetchall()
        temporary_count = cursor.execute(
            "SELECT COUNT(*) FROM temp.sqlite_schema"
        ).fetchone()
        quick = cursor.execute("PRAGMA quick_check(1)").fetchone()
        foreign_key_violation = cursor.execute("PRAGMA foreign_key_check").fetchone()
    except (UnicodeError, sqlite3.Error):
        raise _error("DATABASE_ATTESTATION_FAILED", 3) from None
    finally:
        cursor.close()
    markers = []
    for row in marker_rows:
        if type(row) is not tuple or len(row) != 1 or type(row[0]) is not bytes:
            raise _error("DATABASE_ATTESTATION_FAILED", 3)
        try:
            markers.append(row[0].decode("utf-8", "strict"))
        except UnicodeError:
            raise _error("DATABASE_ATTESTATION_FAILED", 3) from None
    payload = json.dumps(rows, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    if (
        len(rows) != _EXPECTED_SCHEMA_OBJECT_COUNT
        or tuple(markers) != _EXPECTED_MIGRATION_MARKERS
        or temporary_count != (0,)
        or quick != ("ok",)
        or foreign_key_violation is not None
        or not hmac.compare_digest(
            hashlib.sha256(payload).hexdigest(),
            _EXPECTED_SCHEMA_FINGERPRINT,
        )
    ):
        raise _error("DATABASE_ATTESTATION_FAILED", 3)
    try:
        account_valid = attest_account_schema(connection)
        oidc_attestation = attest_google_oidc_authorization_transaction_schema(connection)
    except BaseException:
        raise _error("DATABASE_ATTESTATION_FAILED", 3) from None
    if (
        account_valid is not True
        or type(oidc_attestation) is not dict
        or oidc_attestation.get("state") != "correctly_installed"
        or oidc_attestation.get("blocking") is not False
        or oidc_attestation.get("migration_marker_present") is not True
        or connection.in_transaction
    ):
        raise _error("DATABASE_ATTESTATION_FAILED", 3)
    _require_no_sqlite_sidecars(targets.database_path)


def _sqlite_is_busy(error: BaseException) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    return code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or (
        isinstance(error, sqlite3.OperationalError)
        and str(error).casefold() in {"database is locked", "database table is locked"}
    )


def _require_authentic_ownership(ownership, targets: _TargetSet):
    try:
        result = require_database_lifetime_ownership(
            ownership,
            role=ROLE_OFFLINE_OPERATOR,
            database_path=targets.database_path,
        )
    except DatabaseLifetimeOwnershipError:
        raise _error("OWNERSHIP_LOST", 7) from None
    if result is not True:
        raise _error("OWNERSHIP_LOST", 7)


def _require_coordination_identity_distinct(targets: _TargetSet):
    try:
        metadata = os.lstat(targets.coordination_path)
    except OSError:
        raise _error("OWNERSHIP_LOST", 7) from None
    identity = _identity(metadata)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size != 0
        or _is_reparse(metadata)
        or (identity.device, identity.inode)
        in {
            (targets.configuration.identity.device, targets.configuration.identity.inode),
            (targets.database_identity.device, targets.database_identity.inode),
            (targets.key_identity.device, targets.key_identity.inode),
        }
    ):
        raise _error("OWNERSHIP_LOST", 7)


def _read_invitation_key(targets: _TargetSet) -> bytearray:
    _revalidate_targets(targets)
    raw, handle, authority = _read_exact_pinned_file(
        targets.key_path,
        targets.key_identity,
        minimum=KEY_MIN_BYTES,
        maximum=KEY_MAX_BYTES,
        retain_handle=False,
    )
    if handle is not None or authority is not None:
        _clear_buffer(raw)
        raise _error("INTERNAL_FAILURE", 7)
    _revalidate_targets(targets)
    return raw


def _canonical_envelope_payload(document: dict) -> bytes:
    payload = json.dumps(
        document,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii") + b"\n"
    if not 1 <= len(payload) <= ENVELOPE_MAX_BYTES:
        raise _error("INTERNAL_FAILURE", 7)
    return payload


def _recovery_tag(document_without_tag: dict, key: bytes) -> str:
    canonical = json.dumps(
        document_without_tag,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hmac.new(
        key,
        ENVELOPE_AUTHENTICATION_DOMAIN + canonical,
        hashlib.sha256,
    ).hexdigest()


def _build_envelope(
    *,
    targets: _TargetSet,
    output: _OutputTarget,
    key: bytes,
    request_id: str,
    request_fingerprint: str,
    invitation_reference: str,
    invitation_credential: str,
    email_hint: str,
    expires_at_z: str,
) -> bytes:
    document = {
        "configuration_binding_sha256": targets.configuration_binding,
        "email_hint": email_hint,
        "expires_at": expires_at_z,
        "format": ENVELOPE_FORMAT,
        "invitation_credential": invitation_credential,
        "invitation_reference": invitation_reference,
        "output_binding_sha256": output.output_binding,
        "request_fingerprint": request_fingerprint,
        "request_id_sha256": hashlib.sha256(request_id.encode("ascii")).hexdigest(),
    }
    document["recovery_tag"] = _recovery_tag(document, key)
    return _canonical_envelope_payload(document)


def _authenticate_envelope(
    payload: bytes,
    *,
    targets: _TargetSet,
    output: _OutputTarget,
    key: bytes,
    request_id: str,
    request_fingerprint: str,
    expected_row=None,
) -> dict:
    if (
        type(payload) is not bytes
        or not payload.endswith(b"\n")
        or payload.endswith(b"\n\n")
        or not 1 <= len(payload) <= ENVELOPE_MAX_BYTES
    ):
        raise _error("CREDENTIAL_RECOVERY_UNAVAILABLE", 6)
    try:
        document = json.loads(
            payload[:-1],
            object_pairs_hook=_unique_envelope_object,
            parse_constant=_reject_envelope_constant,
        )
    except PrivateBetaInvitationOperationError:
        raise
    except BaseException:
        raise _error("CREDENTIAL_RECOVERY_UNAVAILABLE", 6) from None
    expected_fields = {
        "configuration_binding_sha256",
        "email_hint",
        "expires_at",
        "format",
        "invitation_credential",
        "invitation_reference",
        "output_binding_sha256",
        "request_fingerprint",
        "request_id_sha256",
        "recovery_tag",
    }
    if type(document) is not dict or set(document) != expected_fields:
        raise _error("CREDENTIAL_RECOVERY_UNAVAILABLE", 6)
    values_are_strings = all(type(value) is str for value in document.values())
    reference = document.get("invitation_reference")
    credential = document.get("invitation_credential")
    try:
        credential_reference, raw_secret = credential.split(".", 1)
    except (AttributeError, ValueError):
        raise _error("CREDENTIAL_RECOVERY_UNAVAILABLE", 6) from None
    without_tag = dict(document)
    supplied_tag = without_tag.pop("recovery_tag", None)
    expected_tag = _recovery_tag(without_tag, key)
    if (
        not values_are_strings
        or document["format"] != ENVELOPE_FORMAT
        or document["configuration_binding_sha256"] != targets.configuration_binding
        or document["output_binding_sha256"] != output.output_binding
        or document["request_fingerprint"] != request_fingerprint
        or document["request_id_sha256"]
        != hashlib.sha256(request_id.encode("ascii")).hexdigest()
        or _INVITATION_REFERENCE.fullmatch(reference) is None
        or credential_reference != reference
        or not 32 <= len(raw_secret) <= 256
        or _EMAIL_HINT.fullmatch(document["email_hint"]) is None
        or _EXPIRY.fullmatch(document["expires_at"]) is None
        or _SHA256.fullmatch(supplied_tag or "") is None
        or not hmac.compare_digest(supplied_tag, expected_tag)
        or _canonical_envelope_payload(document) != payload
    ):
        raise _error("CREDENTIAL_RECOVERY_UNAVAILABLE", 6)
    if expected_row is not None:
        _validate_creation_row(expected_row)
        if (
            expected_row["invitation_id"] != reference
            or expected_row["request_fingerprint"] != request_fingerprint
            or expected_row["email_display_hint"] != document["email_hint"]
            or _to_zulu(expected_row["expires_at"]) != document["expires_at"]
            or not hmac.compare_digest(
                expected_row["invitation_secret_hmac"],
                invitation_secret_hmac(raw_secret, key),
            )
        ):
            raise _error("CREDENTIAL_RECOVERY_UNAVAILABLE", 6)
    return document


def _unique_envelope_object(pairs):
    result = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise _error("CREDENTIAL_RECOVERY_UNAVAILABLE", 6)
        result[key] = value
    return result


def _reject_envelope_constant(_value):
    raise _error("CREDENTIAL_RECOVERY_UNAVAILABLE", 6)


def _parse_persisted_timestamp(value: str) -> datetime:
    if (
        type(value) is not str
        or len(value) != 25
        or not value.endswith("+00:00")
    ):
        raise _error("DATABASE_ATTESTATION_FAILED", 3)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _error("DATABASE_ATTESTATION_FAILED", 3) from None
    if parsed.tzinfo is None or parsed.isoformat() != value or parsed.microsecond:
        raise _error("DATABASE_ATTESTATION_FAILED", 3)
    return parsed.astimezone(timezone.utc)


def _to_zulu(value: str) -> str:
    return _parse_persisted_timestamp(value).strftime("%Y-%m-%dT%H:%M:%SZ")


_CREATION_ROW_COLUMNS = (
    "invitation_id",
    "email_display_hint",
    "created_at",
    "expires_at",
    "invitation_status",
    "invited_email_hmac",
    "invitation_secret_hmac",
    "hash_version",
    "created_by",
    "source_metadata_json",
    "idempotency_key",
    "request_fingerprint",
    "revoked_at",
    "consumed_at",
)


def _creation_row_by_request(connection, idempotency_key: str):
    cursor = connection.cursor()
    try:
        row = cursor.execute(
            "SELECT " + ", ".join(_CREATION_ROW_COLUMNS)
            + " FROM account_invitations WHERE idempotency_key = ? LIMIT 2",
            (idempotency_key,),
        ).fetchone()
    finally:
        cursor.close()
    if row is None:
        return None
    if type(row) is not tuple or len(row) != len(_CREATION_ROW_COLUMNS):
        raise _error("DATABASE_ATTESTATION_FAILED", 3)
    return dict(zip(_CREATION_ROW_COLUMNS, row, strict=True))


def _creation_row_by_reference(connection, invitation_reference: str):
    cursor = connection.cursor()
    try:
        row = cursor.execute(
            "SELECT " + ", ".join(_CREATION_ROW_COLUMNS)
            + " FROM account_invitations WHERE invitation_id = ? LIMIT 2",
            (invitation_reference,),
        ).fetchone()
    finally:
        cursor.close()
    if row is None:
        return None
    if type(row) is not tuple or len(row) != len(_CREATION_ROW_COLUMNS):
        raise _error("DATABASE_ATTESTATION_FAILED", 3)
    return dict(zip(_CREATION_ROW_COLUMNS, row, strict=True))


def _validate_creation_row(row: dict):
    if (
        type(row) is not dict
        or set(row) != set(_CREATION_ROW_COLUMNS)
        or type(row["invitation_id"]) is not str
        or _INVITATION_REFERENCE.fullmatch(row["invitation_id"]) is None
        or type(row["email_display_hint"]) is not str
        or _EMAIL_HINT.fullmatch(row["email_display_hint"]) is None
        or row["invitation_status"] not in {"pending", "consumed", "revoked"}
        or type(row["invited_email_hmac"]) is not str
        or _SHA256.fullmatch(row["invited_email_hmac"]) is None
        or type(row["invitation_secret_hmac"]) is not str
        or _SHA256.fullmatch(row["invitation_secret_hmac"]) is None
        or row["hash_version"] != "hmac_sha256_v1"
        or row["created_by"] != OPERATOR_ACTOR
        or type(row["idempotency_key"]) is not str
        or type(row["request_fingerprint"]) is not str
        or _SHA256.fullmatch(row["request_fingerprint"]) is None
        or type(row["source_metadata_json"]) is not str
    ):
        raise _error("DATABASE_ATTESTATION_FAILED", 3)
    _parse_persisted_timestamp(row["created_at"])
    _parse_persisted_timestamp(row["expires_at"])


def _status_row_by_reference(connection, invitation_reference: str):
    cursor = connection.cursor()
    try:
        rows = cursor.execute(
            "SELECT invitation_id, email_display_hint, created_at, expires_at, "
            "invitation_status FROM account_invitations "
            "WHERE invitation_id = ? LIMIT 2",
            (invitation_reference,),
        ).fetchall()
    finally:
        cursor.close()
    if len(rows) == 0:
        return None
    if len(rows) != 1 or type(rows[0]) is not tuple or len(rows[0]) != 5:
        raise _error("DATABASE_ATTESTATION_FAILED", 3)
    row = dict(
        zip(
            (
                "invitation_id",
                "email_display_hint",
                "created_at",
                "expires_at",
                "invitation_status",
            ),
            rows[0],
            strict=True,
        )
    )
    if (
        row["invitation_id"] != invitation_reference
        or type(row["email_display_hint"]) is not str
        or _EMAIL_HINT.fullmatch(row["email_display_hint"]) is None
        or row["invitation_status"] not in {"pending", "consumed", "revoked"}
    ):
        raise _error("DATABASE_ATTESTATION_FAILED", 3)
    _parse_persisted_timestamp(row["created_at"])
    _parse_persisted_timestamp(row["expires_at"])
    return row


def _effective_status(row, now: datetime) -> str:
    persisted = row["invitation_status"]
    if persisted == "consumed":
        return "consumed"
    if persisted == "revoked":
        return "revoked"
    if persisted != "pending":
        raise _error("DATABASE_ATTESTATION_FAILED", 3)
    return "expired" if _parse_persisted_timestamp(row["expires_at"]) <= now else "pending"


def _result_from_row(
    operation: str,
    outcome: str,
    row,
    now: datetime,
    *,
    state: _OperationState,
):
    return PrivateBetaInvitationResult(
        operation=operation,
        outcome=outcome,
        invitation_reference=row["invitation_id"],
        email_hint=row["email_display_hint"],
        created_at=_to_zulu(row["created_at"]) if operation == "status" else None,
        expires_at=_to_zulu(row["expires_at"]),
        status=_effective_status(row, now),
        _state=state,
    )


def create_private_beta_invitation(
    *,
    configuration_path,
    database_path,
    invitation_key_path,
    request_id,
    expires_at,
    credential_output,
    hidden_email_reader,
    _clock=None,
    _checkpoint=None,
) -> PrivateBetaInvitationResult:
    """Create or recover one exact offline invitation request."""
    request_id = _parse_request_id(request_id)
    expiry = _parse_expiry(expires_at)
    _validated_absolute_path(configuration_path)
    _validated_absolute_path(database_path)
    _validated_absolute_path(invitation_key_path)
    _validated_absolute_path(credential_output, code="OUTPUT_FORM_INVALID")
    if not callable(hidden_email_reader) or (
        _clock is not None and not callable(_clock)
    ) or (
        _checkpoint is not None and not callable(_checkpoint)
    ):
        raise _error("INVALID_INPUT", 2)
    _drain_retained_cleanup_or_raise()
    targets = None
    output = None
    result = None
    primary = None
    try:
        targets = _resolve_targets(
            configuration_path,
            database_path,
            invitation_key_path,
            checkpoint=_checkpoint,
        )
        output = _prepare_output_target(
            credential_output,
            targets,
            checkpoint=_checkpoint,
        )
        _revalidate_targets(targets)
        _revalidate_output_parent(output)
        normalized_email = _read_and_normalize_email(hidden_email_reader)
        result = _execute_owned_operation(
            "create",
            targets,
            output=output,
            writable=True,
            checkpoint=_checkpoint,
            operation=lambda connection, session, ownership, state: _perform_create(
                connection,
                session,
                ownership,
                targets=targets,
                output=output,
                normalized_email=normalized_email,
                request_id=request_id,
                expiry=expiry,
                clock=_clock or _utc_now,
                checkpoint=_checkpoint,
                state=state,
            ),
        )
    except PrivateBetaInvitationOperationError as exc:
        primary = exc
    except BaseException:
        primary = _error("INTERNAL_FAILURE", 7)
    cleanup_incomplete = _finalize_outer_authorities(
        targets=targets,
        output=output,
        checkpoint=_checkpoint,
        durable=_outcome_is_durable(primary, result),
    )
    _raise_outer_outcome(
        primary,
        result=result,
        cleanup_incomplete=cleanup_incomplete,
    )
    return result


def status_private_beta_invitation(
    *,
    configuration_path,
    database_path,
    invitation_key_path,
    invitation_reference,
    _clock=None,
    _checkpoint=None,
) -> PrivateBetaInvitationResult:
    """Return the redacted effective state of one exact invitation."""
    invitation_reference = _parse_invitation_reference(invitation_reference)
    _validated_absolute_path(configuration_path)
    _validated_absolute_path(database_path)
    _validated_absolute_path(invitation_key_path)
    if (_clock is not None and not callable(_clock)) or (
        _checkpoint is not None and not callable(_checkpoint)
    ):
        raise _error("INVALID_INPUT", 2)
    _drain_retained_cleanup_or_raise()
    targets = None
    result = None
    primary = None
    try:
        targets = _resolve_targets(
            configuration_path,
            database_path,
            invitation_key_path,
            checkpoint=_checkpoint,
        )
        result = _execute_owned_operation(
            "status",
            targets,
            output=None,
            writable=False,
            checkpoint=_checkpoint,
            operation=lambda connection, session, ownership, state: _perform_status(
                connection,
                ownership,
                targets=targets,
                invitation_reference=invitation_reference,
                clock=_clock or _utc_now,
                checkpoint=_checkpoint,
                state=state,
            ),
        )
    except PrivateBetaInvitationOperationError as exc:
        primary = exc
    except BaseException:
        primary = _error("INTERNAL_FAILURE", 7)
    cleanup_incomplete = _finalize_outer_authorities(
        targets=targets,
        output=None,
        checkpoint=_checkpoint,
        durable=_outcome_is_durable(primary, result),
    )
    _raise_outer_outcome(
        primary,
        result=result,
        cleanup_incomplete=cleanup_incomplete,
    )
    return result


def revoke_private_beta_invitation(
    *,
    configuration_path,
    database_path,
    invitation_key_path,
    invitation_reference,
    _clock=None,
    _checkpoint=None,
) -> PrivateBetaInvitationResult:
    """Revoke one still-pending, unexpired invitation."""
    invitation_reference = _parse_invitation_reference(invitation_reference)
    _validated_absolute_path(configuration_path)
    _validated_absolute_path(database_path)
    _validated_absolute_path(invitation_key_path)
    if (_clock is not None and not callable(_clock)) or (
        _checkpoint is not None and not callable(_checkpoint)
    ):
        raise _error("INVALID_INPUT", 2)
    _drain_retained_cleanup_or_raise()
    targets = None
    result = None
    primary = None
    try:
        targets = _resolve_targets(
            configuration_path,
            database_path,
            invitation_key_path,
            checkpoint=_checkpoint,
        )
        result = _execute_owned_operation(
            "revoke",
            targets,
            output=None,
            writable=True,
            checkpoint=_checkpoint,
            operation=lambda connection, session, ownership, state: _perform_revoke(
                connection,
                session,
                ownership,
                targets=targets,
                invitation_reference=invitation_reference,
                clock=_clock or _utc_now,
                checkpoint=_checkpoint,
                state=state,
            ),
        )
    except PrivateBetaInvitationOperationError as exc:
        primary = exc
    except BaseException:
        primary = _error("INTERNAL_FAILURE", 7)
    cleanup_incomplete = _finalize_outer_authorities(
        targets=targets,
        output=None,
        checkpoint=_checkpoint,
        durable=_outcome_is_durable(primary, result),
    )
    _raise_outer_outcome(
        primary,
        result=result,
        cleanup_incomplete=cleanup_incomplete,
    )
    return result


def _outcome_is_durable(primary, result) -> bool:
    return bool(
        (
            isinstance(primary, PrivateBetaInvitationOperationError)
            and primary.code == "COMMITTED_RETRY_REQUIRED"
        )
        or (
            type(result) is PrivateBetaInvitationResult
            and result._durable_delivery
        )
    )


def _finalize_outer_authorities(
    *,
    targets,
    output,
    checkpoint,
    durable,
) -> bool:
    incomplete = False
    retained_output = None
    retained_configuration = None
    if output is not None:
        report = _close_with_one_retry(output, checkpoint=checkpoint)
        if not report.terminal:
            retained_output = output
        incomplete = (
            incomplete
            or not report.terminal
            or report.exception_observed
        )
    if targets is not None:
        report = _close_with_one_retry(
            targets.configuration,
            checkpoint=checkpoint,
        )
        if not report.terminal:
            retained_configuration = targets.configuration
        incomplete = (
            incomplete
            or not report.terminal
            or report.exception_observed
        )
    _retain_cleanup_authorities(
        output=retained_output,
        configuration=retained_configuration,
        durable=durable,
    )
    return incomplete


def _raise_outer_outcome(primary, *, result, cleanup_incomplete):
    if cleanup_incomplete:
        if (
            isinstance(primary, PrivateBetaInvitationOperationError)
            and primary.code == "COMMITTED_RETRY_REQUIRED"
        ) or (
            type(result) is PrivateBetaInvitationResult
            and result._durable_delivery
        ):
            raise _committed_retry_required(cleanup_incomplete=True) from None
        raise _error("CLEANUP_INCOMPLETE", 7) from None
    if primary is not None:
        raise primary from None


def _read_and_normalize_email(hidden_email_reader) -> str:
    values = None
    first = None
    second = None
    try:
        try:
            values = hidden_email_reader()
        except BaseException:
            raise _error("CONSOLE_UNAVAILABLE", 2) from None
        if type(values) is not tuple or len(values) != 2 or any(type(value) is not str for value in values):
            raise _error("CONSOLE_UNAVAILABLE", 2)
        try:
            first = normalize_email(values[0])
            second = normalize_email(values[1])
        except InvalidAccountInput:
            raise _error("EMAIL_INVALID", 2) from None
        if not hmac.compare_digest(first, second):
            raise _error("EMAIL_MISMATCH", 2)
        return first
    finally:
        values = None
        second = None


def _execute_owned_operation(
    operation_name: str,
    targets: _TargetSet,
    *,
    output: _OutputTarget | None,
    writable: bool,
    checkpoint,
    operation,
):
    state = _OperationState(operation_name)
    ownership = None
    pending_session = None
    session = None
    result = None
    primary = None
    cleanup_complete = True
    acquired = False
    database_opened = False
    try:
        try:
            ownership = acquire_database_lifetime_ownership(
                targets.database_path,
                role=ROLE_OFFLINE_OPERATOR,
            )
            acquired = True
        except DatabaseLifetimeOwnershipError as exc:
            if exc.category == "contention":
                raise _error("OWNERSHIP_BUSY", 4) from None
            if exc.category in {"cleanup_incomplete", "ownership_lost", "invalid_capability"}:
                raise _error("CLEANUP_INCOMPLETE", 7) from None
            raise _error("TARGET_VALIDATION_FAILED", 3) from None
        _require_authentic_ownership(ownership, targets)
        _revalidate_targets(targets)
        _require_coordination_identity_distinct(targets)
        if output is not None:
            _revalidate_output_parent(output)
        pending_session = _RawResourceCleanup(kind="database")
        returned_authority = _open_database_session(
            targets,
            ownership,
            writable=writable,
            state=state,
            checkpoint=checkpoint,
            allow_hot_journal_recovery=(operation_name == "create"),
            authority=pending_session,
        )
        if returned_authority is not pending_session:
            raise _error("INTERNAL_FAILURE", 7)
        session = pending_session.session
        if session is None:
            raise _error("INTERNAL_FAILURE", 7)
        _emit_checkpoint(
            checkpoint,
            "after_database_session_delivery_before_acknowledgement",
        )
        _emit_checkpoint(checkpoint, "before_database_session_adoption")
        pending_session.acknowledge_database_session(
            session,
            checkpoint=checkpoint,
        )
        _emit_checkpoint(checkpoint, "after_database_session_adoption")
        pending_session = None
        database_opened = True
        _attest_database(session.connection, targets)
        _require_authentic_ownership(ownership, targets)
        _revalidate_targets(targets)
        result = operation(session.connection, session, ownership, state)
        if type(result) is not PrivateBetaInvitationResult:
            raise _error("INTERNAL_FAILURE", 7)
    except PrivateBetaInvitationOperationError as exc:
        primary = exc
    except sqlite3.OperationalError as exc:
        primary = _error("DATABASE_BUSY", 4) if _sqlite_is_busy(exc) else _error("INTERNAL_FAILURE", 7)
    except BaseException:
        primary = _error("INTERNAL_FAILURE", 7)
    finally:
        if pending_session is not None:
            if (
                session is not None
                and pending_session.handoff_acknowledged_for(session)
            ):
                # The destination was installed before the atomic
                # acknowledgement.  Retire only the stale source alias and let
                # the session cleanup path below remain solely responsible.
                pending_session = None
            else:
                # Before acknowledgement the caller-created raw authority is
                # the sole owner, including across the callee return boundary.
                session = None
                report = _close_or_retain_raw(
                    pending_session,
                    checkpoint=checkpoint,
                    ownership=ownership,
                    database_path=targets.database_path,
                    state=state,
                )
                pending_session = None
                if not report.terminal or report.exception_observed:
                    cleanup_complete = False
        if session is not None:
            report = _close_with_one_retry(
                session,
                checkpoint=checkpoint,
            )
            if not report.terminal or report.exception_observed:
                cleanup_complete = False
            if report.terminal:
                session = None
        try:
            _revalidate_targets(targets, database_stable=not writable)
            if database_opened:
                _require_no_sqlite_sidecars(targets.database_path)
        except BaseException:
            if primary is None or database_opened:
                cleanup_complete = False
        if output is not None:
            try:
                _revalidate_output_parent(output)
            except BaseException:
                cleanup_complete = False
            report = _close_with_one_retry(output, checkpoint=checkpoint)
            if not report.terminal or report.exception_observed:
                cleanup_complete = False
        report = _close_with_one_retry(
            targets.configuration,
            checkpoint=checkpoint,
        )
        if not report.terminal or report.exception_observed:
            cleanup_complete = False
        if state.raw_ownership_retained:
            # The retained raw authority also owns PB-OWN until its exact
            # database resources are terminal.  Drop only this duplicate local
            # reference; the coordinator will release ownership in order.
            ownership = None
            acquired = False
            cleanup_complete = False
        elif acquired and session is None:
            report = _release_ownership_with_one_retry(
                ownership,
                targets.database_path,
                state=state,
                checkpoint=checkpoint,
            )
            if not report.terminal or report.exception_observed:
                cleanup_complete = False
            if report.terminal:
                ownership = None
        elif acquired:
            cleanup_complete = False
        _retain_cleanup_authorities(
            session=session,
            ownership=ownership if acquired else None,
            database_path=targets.database_path if acquired else None,
            state=state if acquired else None,
            durable=state.durable_mutation_may_have_occurred,
        )
        state.cleanup_incomplete = not cleanup_complete
    if not cleanup_complete:
        if state.durable_mutation_may_have_occurred:
            raise _committed_retry_required(cleanup_incomplete=True)
        raise _error("CLEANUP_INCOMPLETE", 7)
    if primary is not None:
        if state.indeterminate_durable_boundary or (
            state.durable_preexisting
            and primary.code in {"CLEANUP_INCOMPLETE", "INTERNAL_FAILURE"}
        ):
            raise _committed_retry_required(
                cleanup_incomplete=primary.cleanup_incomplete,
            ) from None
        raise primary from None
    try:
        _emit_checkpoint(
            checkpoint,
            "after_ownership_release_before_result",
        )
    except BaseException:
        if state.durable_mutation_may_have_occurred:
            raise _committed_retry_required() from None
        raise _error("INTERNAL_FAILURE", 7) from None
    return result


def _release_ownership_with_one_retry(
    ownership,
    database_path: Path,
    *,
    state: _OperationState,
    checkpoint=None,
) -> _CloseReport:
    state.begin_ownership_release()
    exception_observed = _checkpoint_exception(
        checkpoint,
        "before_ownership_release",
    )
    terminal = False
    for _attempt in range(2):
        try:
            released = release_database_lifetime_ownership(
                ownership,
                role=ROLE_OFFLINE_OPERATOR,
                database_path=database_path,
            )
        except BaseException:
            released = False
            exception_observed = True
        try:
            terminal = database_lifetime_ownership_is_released(ownership)
        except BaseException:
            terminal = False
            exception_observed = True
        if terminal:
            if not released:
                exception_observed = True
            state.confirm_ownership_release()
            return _CloseReport(True, exception_observed)
        exception_observed = True
    return _CloseReport(terminal, exception_observed)


def _drain_retained_cleanup_or_raise():
    failure_observed = False
    durable_failure = False
    remaining = []
    with _RETAINED_CLEANUP_LOCK:
        retained = list(_RETAINED_CLEANUPS)
        _RETAINED_CLEANUPS.clear()
        for entry in retained:
            entry_exception = False
            for attribute in ("raw", "session", "output", "configuration"):
                authority = getattr(entry, attribute)
                if authority is None:
                    continue
                report = _close_with_one_retry(authority, checkpoint=None)
                entry_exception = entry_exception or report.exception_observed
                if report.terminal:
                    setattr(entry, attribute, None)
            if (
                entry.ownership is not None
                and entry.raw is None
                and entry.session is None
            ):
                if entry.database_path is None or entry.state is None:
                    entry_exception = True
                else:
                    report = _release_ownership_with_one_retry(
                        entry.ownership,
                        entry.database_path,
                        state=entry.state,
                        checkpoint=None,
                    )
                    entry_exception = (
                        entry_exception or report.exception_observed
                    )
                    if report.terminal:
                        entry.ownership = None
            pending = any(
                authority is not None
                for authority in (
                    entry.raw,
                    entry.session,
                    entry.output,
                    entry.configuration,
                    entry.ownership,
                )
            )
            entry_failed = pending or entry_exception
            failure_observed = failure_observed or entry_failed
            durable_failure = durable_failure or (
                entry_failed and entry.durable
            )
            if pending:
                remaining.append(entry)
        _RETAINED_CLEANUPS.extend(remaining)
    if failure_observed:
        if durable_failure:
            raise _committed_retry_required(cleanup_incomplete=True)
        raise _error("CLEANUP_INCOMPLETE", 7)


def _validate_database_descriptor(
    session: _DatabaseSession,
    targets: _TargetSet,
    *,
    stable: bool,
):
    descriptor = session.descriptor
    if descriptor is None:
        raise _error("OWNERSHIP_LOST", 7)
    try:
        opened = _identity(os.fstat(descriptor))
        current = _identity(os.lstat(targets.database_path))
    except OSError:
        raise _error("OWNERSHIP_LOST", 7) from None
    comparison = _same_stable_identity if stable else _same_object_identity
    if (
        os.get_inheritable(descriptor)
        or not comparison(session.descriptor_identity, opened)
        or not comparison(targets.database_identity, current)
    ):
        raise _error("OWNERSHIP_LOST", 7)


def _begin_immediate(connection: sqlite3.Connection):
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        if _sqlite_is_busy(exc):
            raise _error("DATABASE_BUSY", 4) from None
        raise _error("DATABASE_ATTESTATION_FAILED", 3) from None
    if not connection.in_transaction:
        raise _error("DATABASE_ATTESTATION_FAILED", 3)


def _clock_now(clock) -> datetime:
    try:
        now = clock()
    except BaseException:
        raise _error("INTERNAL_FAILURE", 7) from None
    if type(now) is not datetime or now.tzinfo is None:
        raise _error("INTERNAL_FAILURE", 7)
    return now.astimezone(timezone.utc).replace(microsecond=0)


def _perform_status(
    connection,
    ownership,
    *,
    targets,
    invitation_reference,
    clock,
    checkpoint,
    state,
):
    if connection.in_transaction or connection.execute("PRAGMA query_only").fetchone() != (1,):
        raise _error("DATABASE_ATTESTATION_FAILED", 3)
    _require_authentic_ownership(ownership, targets)
    _revalidate_targets(targets)
    row = _status_row_by_reference(connection, invitation_reference)
    if row is None:
        raise _error("INVITATION_UNKNOWN", 5)
    _emit_checkpoint(checkpoint, "status_after_row_read")
    now = _clock_now(clock)
    _require_authentic_ownership(ownership, targets)
    _revalidate_targets(targets)
    if connection.in_transaction:
        raise _error("DATABASE_ATTESTATION_FAILED", 3)
    return _result_from_row(
        "status",
        "found",
        row,
        now,
        state=state,
    )


def _perform_revoke(
    connection,
    session,
    ownership,
    *,
    targets,
    invitation_reference,
    clock,
    checkpoint,
    state,
):
    _begin_immediate(connection)
    operation_now = _clock_now(clock)
    try:
        _require_authentic_ownership(ownership, targets)
        _revalidate_targets(targets)
        _validate_database_descriptor(session, targets, stable=True)
        existing = _status_row_by_reference(connection, invitation_reference)
        if existing is None:
            connection.rollback()
            raise _error("INVITATION_UNKNOWN", 5)
        effective = _effective_status(existing, operation_now)
        if effective == "revoked":
            state.note_preexisting_durable_mutation()
            connection.commit()
            if connection.in_transaction:
                raise _error("CLEANUP_INCOMPLETE", 7)
            return _result_from_row(
                "revoke",
                "replayed",
                existing,
                operation_now,
                state=state,
            )
        if effective != "pending":
            connection.rollback()
            raise _error(
                "INVITATION_NOT_PENDING",
                5,
                status=effective,
            )
        try:
            connection.row_factory = sqlite3.Row
            revoke_invitation(
                connection,
                invitation_id=invitation_reference,
                now=operation_now,
            )
        except AuthenticationUnavailable:
            connection.row_factory = None
            row = _status_row_by_reference(connection, invitation_reference)
            connection.rollback()
            if row is None:
                raise _error("INVITATION_UNKNOWN", 5)
            raise _error(
                "INVITATION_NOT_PENDING",
                5,
                status=_effective_status(row, operation_now),
            )
        finally:
            connection.row_factory = None
        row = _status_row_by_reference(connection, invitation_reference)
        if row is None or row["invitation_status"] != "revoked":
            raise _error("DATABASE_ATTESTATION_FAILED", 3)
        _require_authentic_ownership(ownership, targets)
        _revalidate_targets(targets, database_stable=False)
        _validate_database_descriptor(session, targets, stable=False)
        state.begin_database_commit()
        _emit_checkpoint(checkpoint, "before_database_commit")
        connection.commit()
        state.confirm_database_commit()
        if connection.in_transaction:
            raise _error("CLEANUP_INCOMPLETE", 7)
        _emit_checkpoint(checkpoint, "after_database_commit")
        _require_no_sqlite_sidecars(targets.database_path)
        return _result_from_row(
            "revoke",
            "revoked",
            row,
            operation_now,
            state=state,
        )
    except BaseException:
        if connection.in_transaction:
            try:
                connection.rollback()
            except BaseException:
                raise _error("CLEANUP_INCOMPLETE", 7) from None
        raise


def _perform_create(
    connection,
    session,
    ownership,
    *,
    targets,
    output,
    normalized_email,
    request_id,
    expiry,
    clock,
    checkpoint,
    state,
):
    _begin_immediate(connection)
    operation_now = _clock_now(clock)
    key_buffer = None
    key = None
    stage_created = False
    try:
        if expiry <= operation_now:
            raise _error("EXPIRY_INVALID", 2)
        _require_authentic_ownership(ownership, targets)
        _revalidate_targets(targets)
        _revalidate_output_parent(output)
        _validate_database_descriptor(session, targets, stable=True)
        # BEGIN IMMEDIATE above proves writer availability before key bytes are
        # opened or copied into the Python process.
        key_buffer = _read_invitation_key(targets)
        key = bytes(key_buffer)
        source_metadata = {
            "configuration_binding_sha256": targets.configuration_binding,
            "operator_protocol": CREATE_PROTOCOL,
            "output_binding_sha256": output.output_binding,
        }
        request_fingerprint = invitation_creation_request_fingerprint(
            email=normalized_email,
            lookup_key=key,
            expires_at=expiry,
            created_by=OPERATOR_ACTOR,
            source_metadata=source_metadata,
        )
        idempotency_key = IDEMPOTENCY_PREFIX + request_id
        existing = _creation_row_by_request(connection, idempotency_key)
        if existing is not None:
            _validate_creation_row(existing)
            if (
                existing["request_fingerprint"] != request_fingerprint
                or existing["idempotency_key"] != idempotency_key
                or existing["source_metadata_json"]
                != json.dumps(source_metadata, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            ):
                raise _error("REQUEST_ID_CONFLICT", 5)
            return _recover_committed_create(
                connection,
                session,
                ownership,
                targets=targets,
                output=output,
                key=key,
                request_id=request_id,
                request_fingerprint=request_fingerprint,
                row=existing,
                operation_now=operation_now,
                checkpoint=checkpoint,
                state=state,
            )

        final_exists = _optional_lstat(output.final_path) is not None
        stage_exists = _optional_lstat(output.stage_path) is not None
        if final_exists:
            raise _error("CREDENTIAL_RECOVERY_UNAVAILABLE", 6)
        if stage_exists:
            abandoned = _read_output_file(output.stage_path, output=output)
            _authenticate_envelope(
                abandoned,
                targets=targets,
                output=output,
                key=key,
                request_id=request_id,
                request_fingerprint=request_fingerprint,
                expected_row=None,
            )
            _remove_stage(output)

        _emit_checkpoint(checkpoint, "before_token_generation")
        try:
            connection.row_factory = sqlite3.Row
            creation = create_invitation(
                connection,
                email=normalized_email,
                lookup_key=key,
                expires_at=expiry,
                created_by=OPERATOR_ACTOR,
                idempotency_key=idempotency_key,
                source_metadata=source_metadata,
                now=operation_now,
                failure_injector=lambda point: (
                    _emit_checkpoint(checkpoint, "after_invitation_savepoint")
                    if point == "after_invitation_insert"
                    else None
                ),
            )
        except AuthenticationUnavailable:
            raise _error("REQUEST_ID_CONFLICT", 5) from None
        except InvalidAccountInput:
            raise _error("INVALID_INPUT", 2) from None
        finally:
            connection.row_factory = None
        row = _creation_row_by_request(connection, idempotency_key)
        if row is None:
            raise _error("DATABASE_ATTESTATION_FAILED", 3)
        _validate_creation_row(row)
        if (
            row["request_fingerprint"] != request_fingerprint
            or creation.invitation.invitation_id != row["invitation_id"]
            or not creation.invitation_token.startswith(row["invitation_id"] + ".")
        ):
            raise _error("DATABASE_ATTESTATION_FAILED", 3)
        envelope = _build_envelope(
            targets=targets,
            output=output,
            key=key,
            request_id=request_id,
            request_fingerprint=request_fingerprint,
            invitation_reference=row["invitation_id"],
            invitation_credential=creation.invitation_token,
            email_hint=row["email_display_hint"],
            expires_at_z=_to_zulu(row["expires_at"]),
        )
        _create_stage_file(output, envelope, checkpoint=checkpoint)
        stage_created = True
        if _optional_lstat(output.final_path) is not None:
            raise _error("CREDENTIAL_DESTINATION_UNAVAILABLE", 6)
        staged = _read_output_file(output.stage_path, output=output)
        _authenticate_envelope(
            staged,
            targets=targets,
            output=output,
            key=key,
            request_id=request_id,
            request_fingerprint=request_fingerprint,
            expected_row=row,
        )
        _require_authentic_ownership(ownership, targets)
        _revalidate_targets(targets, database_stable=False)
        _revalidate_output_parent(output)
        _validate_database_descriptor(session, targets, stable=False)
        state.begin_database_commit()
        _emit_checkpoint(checkpoint, "before_database_commit")
        connection.commit()
        if connection.in_transaction:
            raise _error("CLEANUP_INCOMPLETE", 7)
        state.confirm_database_commit()
        _emit_checkpoint(checkpoint, "after_database_commit")
        _require_authentic_ownership(ownership, targets)
        _revalidate_targets(targets, database_stable=False)
        _require_no_sqlite_sidecars(targets.database_path)
        _publish_stage(
            output,
            state=state,
            checkpoint=checkpoint,
        )
        stage_created = False
        final_payload = _read_output_file(output.final_path, output=output)
        _authenticate_envelope(
            final_payload,
            targets=targets,
            output=output,
            key=key,
            request_id=request_id,
            request_fingerprint=request_fingerprint,
            expected_row=row,
        )
        _emit_checkpoint(checkpoint, "after_final_revalidation")
        return _result_from_row(
            "create",
            "created",
            row,
            operation_now,
            state=state,
        )
    except BaseException:
        if connection.in_transaction:
            try:
                connection.rollback()
            except BaseException:
                raise _error("CLEANUP_INCOMPLETE", 7) from None
        if (
            stage_created
            and not state.database_commit_attempted
            and _optional_lstat(output.stage_path) is not None
        ):
            try:
                _remove_stage(output)
            except BaseException:
                raise _error("CLEANUP_INCOMPLETE", 7) from None
        raise
    finally:
        _clear_buffer(key_buffer)
        key_buffer = None
        key = None
        normalized_email = None


def _recover_committed_create(
    connection,
    session,
    ownership,
    *,
    targets,
    output,
    key,
    request_id,
    request_fingerprint,
    row,
    operation_now,
    checkpoint,
    state,
):
    state.note_preexisting_durable_mutation()
    final_exists = _optional_lstat(output.final_path) is not None
    stage_exists = _optional_lstat(output.stage_path) is not None
    if not final_exists and not stage_exists:
        raise _error("CREDENTIAL_RECOVERY_UNAVAILABLE", 6)
    double_link = final_exists and stage_exists
    if double_link and row["invitation_status"] != "pending":
        raise _error("CREDENTIAL_RECOVERY_UNAVAILABLE", 6)
    if double_link:
        payload, _identity_before_recovery = _read_posix_double_link_payload(
            output
        )
    else:
        path = output.final_path if final_exists else output.stage_path
        payload = _read_output_file(path, output=output)
    _authenticate_envelope(
        payload,
        targets=targets,
        output=output,
        key=key,
        request_id=request_id,
        request_fingerprint=request_fingerprint,
        expected_row=row,
    )
    _require_authentic_ownership(ownership, targets)
    _revalidate_targets(targets)
    _validate_database_descriptor(session, targets, stable=True)
    _revalidate_output_parent(output)
    state.begin_database_commit()
    _emit_checkpoint(checkpoint, "before_database_commit")
    connection.commit()
    if connection.in_transaction:
        raise _error("CLEANUP_INCOMPLETE", 7)
    state.confirm_database_commit()
    _emit_checkpoint(checkpoint, "after_database_commit")
    if double_link:
        _recover_posix_double_link(
            output,
            targets=targets,
            ownership=ownership,
            session=session,
            key=key,
            request_id=request_id,
            request_fingerprint=request_fingerprint,
            row=row,
            state=state,
            checkpoint=checkpoint,
        )
        outcome = "recovered"
    elif stage_exists:
        _publish_stage(
            output,
            state=state,
            checkpoint=checkpoint,
        )
        final_payload = _read_output_file(output.final_path, output=output)
        _authenticate_envelope(
            final_payload,
            targets=targets,
            output=output,
            key=key,
            request_id=request_id,
            request_fingerprint=request_fingerprint,
            expected_row=row,
        )
        outcome = "recovered"
    else:
        state.begin_credential_publication()
        _flush_output_directory(output)
        final_payload = _read_output_file(output.final_path, output=output)
        _authenticate_envelope(
            final_payload,
            targets=targets,
            output=output,
            key=key,
            request_id=request_id,
            request_fingerprint=request_fingerprint,
            expected_row=row,
        )
        state.confirm_credential_publication()
        outcome = "replayed"
    _emit_checkpoint(checkpoint, "after_final_revalidation")
    return _result_from_row(
        "create",
        outcome,
        row,
        operation_now,
        state=state,
    )

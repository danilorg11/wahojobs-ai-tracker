"""Sealed domain values for dormant durable Google OIDC transactions.

The module owns the versioned canonical encodings shared by persistence and
protection.  It performs no database, key, random, environment, file, network,
or thread activity at import time.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
import threading


__all__ = (
    "PreparedDurableGoogleOidcAuthorization",
    "ClaimedGoogleOidcAuthorizationMaterial",
    "GoogleOidcAuthorizationTransactionCleanupResult",
    "GoogleOidcAuthorizationTransactionReconciliationResult",
    "TRANSACTION_RECORD_VERSION",
    "STATE_DIGEST_VERSION",
    "PROTECTION_ENVELOPE_VERSION",
    "PROTECTED_MATERIAL_VERSION",
    "ASSOCIATED_DATA_VERSION",
    "TRANSACTION_TTL_SECONDS",
)


TRANSACTION_RECORD_VERSION = 1
STATE_DIGEST_VERSION = 1
PROTECTION_ENVELOPE_VERSION = 1
PROTECTED_MATERIAL_VERSION = 2
ASSOCIATED_DATA_VERSION = 1
TRANSACTION_TTL_SECONDS = 600
MAX_KEY_VERSION = 2_147_483_647
MAX_PROTECTED_PLAINTEXT_BYTES = 512
MAX_PROTECTED_CIPHERTEXT_BYTES = 528
MAX_INVITATION_CREDENTIAL_BYTES = 128
MAX_AUTHORIZATION_URL_BYTES = 8192
MAX_DURABLE_CONFIGURATION_CONTEXT_BYTES = 8192


@dataclass(frozen=True, slots=True)
class _GoogleOidcCleanupContract:
    """Explicit bounded cleanup mutation, inspection, and retention policy."""

    max_mutations: int = 1_000
    max_candidate_inspections: int = 4_000
    min_terminal_retention_seconds: int = 1
    max_terminal_retention_seconds: int = 31_536_000


GOOGLE_OIDC_CLEANUP_CONTRACT = _GoogleOidcCleanupContract()
MAX_CLEANUP_LIMIT = GOOGLE_OIDC_CLEANUP_CONTRACT.max_mutations


@dataclass(frozen=True, slots=True)
class _GoogleOidcReconciliationBudgetContract:
    """One repository-wide bound for reconciliation work and presentation."""

    max_scan_rows: int = 1_000
    max_retained_findings: int = 1_000
    max_output_bytes: int = 524_288
    max_result_rows: int = 64_000
    max_snapshot_pages: int = 8_192
    max_backup_callbacks: int = 16_400
    max_sqlite_progress_calls: int = 100_000
    max_authorizer_calls: int = 100_000


GOOGLE_OIDC_RECONCILIATION_BUDGET = (
    _GoogleOidcReconciliationBudgetContract()
)
MAX_RECONCILIATION_ISSUES = (
    GOOGLE_OIDC_RECONCILIATION_BUDGET.max_retained_findings
)

PREPARED_LIFECYCLE = "prepared"
TERMINAL_LIFECYCLES = frozenset({"consumed", "expired", "invalidated"})
TRANSACTION_LIFECYCLES = frozenset(
    {PREPARED_LIFECYCLE, *TERMINAL_LIFECYCLES}
)

_TRANSACTION_ID = re.compile(r"^oidctx_[0-9a-f]{32}$")
_ENVIRONMENT_NAMESPACE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_ISSUE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_LEGACY_PROTECTED_MATERIAL_VERSION = 1
_PROTECTED_MATERIAL_DOMAIN_V1 = (
    b"wahojobs-google-oidc-protected-material-v1"
)
_PROTECTED_MATERIAL_DOMAIN_V2 = (
    b"wahojobs-google-oidc-protected-material-v2"
)
_ASSOCIATED_DATA_DOMAIN = (
    b"wahojobs-google-oidc-authorization-transaction-aad-v1"
)
_PREPARED_ISSUANCE_CAPABILITY = object()
_CLAIMED_ISSUANCE_CAPABILITY = object()
_CLAIMED_SERVICE_CAPABILITY = object()
_CLEANUP_ISSUANCE_CAPABILITY = object()
_RECONCILIATION_ISSUANCE_CAPABILITY = object()


class PreparedDurableGoogleOidcAuthorization:
    """Sealed committed preparation with a deliberately redacted display."""

    __slots__ = ("__record", "__weakref__")

    def __new__(cls, *_args, **_kwargs):
        raise TypeError("durable_google_oidc_preparation_required")

    @classmethod
    def _issue(
        cls,
        capability,
        *,
        transaction_id,
        authorization_url,
        created_at,
        expires_at,
    ):
        if (
            cls is not PreparedDurableGoogleOidcAuthorization
            or capability is not _PREPARED_ISSUANCE_CAPABILITY
        ):
            raise TypeError("durable_google_oidc_preparation_required")
        transaction_id = _validated_transaction_id(transaction_id)
        created_at = _canonical_utc_time(created_at)
        expires_at = _canonical_utc_time(expires_at)
        _require_exact_ten_minute_chronology(created_at, expires_at)
        url_buffer = _validated_authorization_url_buffer(authorization_url)
        record = _PreparedAuthorizationRecord(
            transaction_id=transaction_id,
            authorization_url_buffer=url_buffer,
            created_at=created_at,
            expires_at=expires_at,
        )
        instance = object.__new__(cls)
        object.__setattr__(
            instance,
            "_PreparedDurableGoogleOidcAuthorization__record",
            record,
        )
        return instance

    @property
    def transaction_id(self):
        return _prepared_record(self).transaction_id

    @property
    def authorization_url(self):
        record = _prepared_record(self)
        with record.lock:
            if record.closed or not record.authorization_url_buffer:
                raise TypeError("durable_google_oidc_preparation_unavailable")
            try:
                return bytes(record.authorization_url_buffer).decode("ascii")
            except UnicodeError:
                raise TypeError(
                    "durable_google_oidc_preparation_unavailable"
                ) from None

    @property
    def created_at(self):
        return _prepared_record(self).created_at

    @property
    def expires_at(self):
        return _prepared_record(self).expires_at

    @property
    def closed(self):
        record = _prepared_record(self)
        with record.lock:
            return record.closed

    def close(self):
        try:
            record = object.__getattribute__(
                self,
                "_PreparedDurableGoogleOidcAuthorization__record",
            )
        except AttributeError:
            return
        if type(record) is not _PreparedAuthorizationRecord:
            return
        with record.lock:
            if record.closed:
                return
            _clear_buffer(record.authorization_url_buffer)
            record.closed = True

    def __setattr__(self, _name, _value):
        raise AttributeError("durable_google_oidc_preparation_is_immutable")

    def __repr__(self):
        return "PreparedDurableGoogleOidcAuthorization(<redacted>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("durable_google_oidc_preparation_not_serializable")

    def __copy__(self):
        raise TypeError("durable_google_oidc_preparation_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("durable_google_oidc_preparation_not_copyable")

    def __init_subclass__(cls, **_kwargs):
        raise TypeError("durable_google_oidc_preparation_not_subclassable")

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class ClaimedGoogleOidcAuthorizationMaterial:
    """Sealed, one-use ownership capsule for claimed transaction material."""

    __slots__ = ("__record", "__weakref__")

    def __new__(cls, *_args, **_kwargs):
        raise TypeError("claimed_google_oidc_material_required")

    @classmethod
    def _issue(cls, capability, **values):
        values.setdefault("invitation_credential", None)
        secret_names = (
            "state",
            "nonce",
            "pkce_verifier",
            "b2d1_request_key",
            "invitation_credential",
        )
        secret_buffers = tuple(values.get(name) for name in secret_names)
        retained = False
        try:
            if (
                cls is not ClaimedGoogleOidcAuthorizationMaterial
                or capability is not _CLAIMED_ISSUANCE_CAPABILITY
            ):
                raise TypeError("claimed_google_oidc_material_required")
            normalized = _validated_claimed_values(values)
            record = _ClaimedMaterialRecord(**normalized)
            instance = object.__new__(cls)
            object.__setattr__(
                instance,
                "_ClaimedGoogleOidcAuthorizationMaterial__record",
                record,
            )
            retained = True
            return instance
        finally:
            if not retained:
                for buffer in secret_buffers:
                    _clear_buffer(buffer)

    def _take(self, capability):
        if (
            type(self) is not ClaimedGoogleOidcAuthorizationMaterial
            or capability is not _CLAIMED_SERVICE_CAPABILITY
        ):
            raise TypeError("claimed_google_oidc_material_required")
        record = _claimed_record(self)
        with record.lock:
            if record.used:
                raise TypeError("claimed_google_oidc_material_unavailable")
            values = {
                "transaction_id": record.transaction_id,
                "record_version": record.record_version,
                "provider": record.provider,
                "environment_namespace": record.environment_namespace,
                "configuration_fingerprint": record.configuration_fingerprint,
                "state_digest_version": record.state_digest_version,
                "lookup_key_version": record.lookup_key_version,
                "created_at": record.created_at,
                "expires_at": record.expires_at,
                "claimed_at": record.claimed_at,
                "protection_envelope_version": (
                    record.protection_envelope_version
                ),
                "protection_key_version": record.protection_key_version,
                "state": record.state,
                "nonce": record.nonce,
                "pkce_verifier": record.pkce_verifier,
                "b2d1_request_key": record.b2d1_request_key,
                "invitation_credential": record.invitation_credential,
            }
            record.state = None
            record.nonce = None
            record.pkce_verifier = None
            record.b2d1_request_key = None
            record.invitation_credential = None
            record.used = True
            return values

    @property
    def available(self):
        record = _claimed_record(self)
        with record.lock:
            return not record.used

    def close(self):
        try:
            record = object.__getattribute__(
                self,
                "_ClaimedGoogleOidcAuthorizationMaterial__record",
            )
        except AttributeError:
            return
        if type(record) is not _ClaimedMaterialRecord:
            return
        with record.lock:
            _clear_buffer(record.state)
            _clear_buffer(record.nonce)
            _clear_buffer(record.pkce_verifier)
            _clear_buffer(record.b2d1_request_key)
            _clear_buffer(record.invitation_credential)
            record.state = None
            record.nonce = None
            record.pkce_verifier = None
            record.b2d1_request_key = None
            record.invitation_credential = None
            record.used = True

    def __setattr__(self, _name, _value):
        raise AttributeError("claimed_google_oidc_material_is_immutable")

    def __repr__(self):
        return "ClaimedGoogleOidcAuthorizationMaterial(<redacted>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("claimed_google_oidc_material_not_serializable")

    def __copy__(self):
        raise TypeError("claimed_google_oidc_material_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("claimed_google_oidc_material_not_copyable")

    def __init_subclass__(cls, **_kwargs):
        raise TypeError("claimed_google_oidc_material_not_subclassable")

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class GoogleOidcAuthorizationTransactionCleanupResult:
    """Immutable bounded cleanup counts without row-level metadata."""

    __slots__ = (
        "_expired_count",
        "_deleted_count",
        "_limit",
        "_terminal_retention_seconds",
        "_candidate_inspection_limit",
        "_terminal_candidates_inspected",
        "_skipped_too_recent",
        "_skipped_structurally_invalid",
        "_skipped_unsupported_version",
        "_skipped_chronology_invalid",
        "_known_remaining",
        "_remaining_exact",
        "_candidate_inspection_truncated",
        "_complete",
        "_commit_outcome",
    )

    def __new__(cls, *_args, **_kwargs):
        raise TypeError("google_oidc_transaction_cleanup_result_required")

    @classmethod
    def _issue(
        cls,
        capability,
        *,
        expired_count,
        deleted_count,
        limit,
        terminal_retention_seconds,
        candidate_inspection_limit,
        terminal_candidates_inspected,
        skipped_too_recent,
        skipped_structurally_invalid,
        skipped_unsupported_version,
        skipped_chronology_invalid,
        known_remaining,
        remaining_exact,
        candidate_inspection_truncated,
        complete,
        commit_outcome,
    ):
        bounded_counts = (
            terminal_candidates_inspected,
            skipped_too_recent,
            skipped_structurally_invalid,
            skipped_unsupported_version,
            skipped_chronology_invalid,
        )
        if (
            cls is not GoogleOidcAuthorizationTransactionCleanupResult
            or capability is not _CLEANUP_ISSUANCE_CAPABILITY
            or type(limit) is not int
            or not (1 <= limit <= MAX_CLEANUP_LIMIT)
            or type(expired_count) is not int
            or type(deleted_count) is not int
            or expired_count < 0
            or deleted_count < 0
            or expired_count > limit
            or deleted_count > limit
            or expired_count + deleted_count > limit
            or type(terminal_retention_seconds) is not int
            or not (
                GOOGLE_OIDC_CLEANUP_CONTRACT.min_terminal_retention_seconds
                <= terminal_retention_seconds
                <= GOOGLE_OIDC_CLEANUP_CONTRACT.max_terminal_retention_seconds
            )
            or type(candidate_inspection_limit) is not int
            or not (
                1
                <= candidate_inspection_limit
                <= GOOGLE_OIDC_CLEANUP_CONTRACT.max_candidate_inspections
            )
            or any(
                type(value) is not int or value < 0
                for value in bounded_counts
            )
            or terminal_candidates_inspected > candidate_inspection_limit
            or (
                deleted_count
                + skipped_too_recent
                + skipped_structurally_invalid
                + skipped_unsupported_version
                + skipped_chronology_invalid
                > terminal_candidates_inspected
            )
            or type(known_remaining) is not int
            or known_remaining < 0
            or type(remaining_exact) is not bool
            or type(candidate_inspection_truncated) is not bool
            or type(complete) is not bool
            or remaining_exact is candidate_inspection_truncated
            or (not complete and known_remaining < 1)
            or (
                candidate_inspection_truncated
                and (
                    complete
                    or remaining_exact
                    or known_remaining < 1
                )
            )
            or (
                complete
                and (
                    known_remaining != 0
                    or not remaining_exact
                    or candidate_inspection_truncated
                )
            )
            or type(commit_outcome) is not str
            or commit_outcome != "committed"
        ):
            raise TypeError(
                "google_oidc_transaction_cleanup_result_invalid"
            )
        instance = object.__new__(cls)
        object.__setattr__(instance, "_expired_count", expired_count)
        object.__setattr__(instance, "_deleted_count", deleted_count)
        object.__setattr__(instance, "_limit", limit)
        object.__setattr__(
            instance,
            "_terminal_retention_seconds",
            terminal_retention_seconds,
        )
        object.__setattr__(
            instance,
            "_candidate_inspection_limit",
            candidate_inspection_limit,
        )
        object.__setattr__(
            instance,
            "_terminal_candidates_inspected",
            terminal_candidates_inspected,
        )
        object.__setattr__(
            instance,
            "_skipped_too_recent",
            skipped_too_recent,
        )
        object.__setattr__(
            instance,
            "_skipped_structurally_invalid",
            skipped_structurally_invalid,
        )
        object.__setattr__(
            instance,
            "_skipped_unsupported_version",
            skipped_unsupported_version,
        )
        object.__setattr__(
            instance,
            "_skipped_chronology_invalid",
            skipped_chronology_invalid,
        )
        object.__setattr__(instance, "_known_remaining", known_remaining)
        object.__setattr__(instance, "_remaining_exact", remaining_exact)
        object.__setattr__(
            instance,
            "_candidate_inspection_truncated",
            candidate_inspection_truncated,
        )
        object.__setattr__(instance, "_complete", complete)
        object.__setattr__(instance, "_commit_outcome", commit_outcome)
        return instance

    @property
    def expired_count(self):
        return self._expired_count

    @property
    def deleted_count(self):
        return self._deleted_count

    @property
    def limit(self):
        return self._limit

    @property
    def terminal_retention_seconds(self):
        return self._terminal_retention_seconds

    @property
    def candidate_inspection_limit(self):
        return self._candidate_inspection_limit

    @property
    def terminal_candidates_inspected(self):
        return self._terminal_candidates_inspected

    @property
    def skipped_too_recent(self):
        return self._skipped_too_recent

    @property
    def skipped_structurally_invalid(self):
        return self._skipped_structurally_invalid

    @property
    def skipped_unsupported_version(self):
        return self._skipped_unsupported_version

    @property
    def skipped_chronology_invalid(self):
        return self._skipped_chronology_invalid

    @property
    def known_remaining(self):
        return self._known_remaining

    @property
    def remaining_exact(self):
        return self._remaining_exact

    @property
    def candidate_inspection_truncated(self):
        return self._candidate_inspection_truncated

    @property
    def complete(self):
        return self._complete

    @property
    def commit_outcome(self):
        return self._commit_outcome

    def as_dict(self):
        return {
            "expired_count": self._expired_count,
            "deleted_count": self._deleted_count,
            "limit": self._limit,
            "terminal_retention_seconds": (
                self._terminal_retention_seconds
            ),
            "candidate_inspection_limit": (
                self._candidate_inspection_limit
            ),
            "terminal_candidates_inspected": (
                self._terminal_candidates_inspected
            ),
            "skipped_too_recent": self._skipped_too_recent,
            "skipped_structurally_invalid": (
                self._skipped_structurally_invalid
            ),
            "skipped_unsupported_version": (
                self._skipped_unsupported_version
            ),
            "skipped_chronology_invalid": (
                self._skipped_chronology_invalid
            ),
            "known_remaining": self._known_remaining,
            "remaining_exact": self._remaining_exact,
            "candidate_inspection_truncated": (
                self._candidate_inspection_truncated
            ),
            "complete": self._complete,
            "commit_outcome": self._commit_outcome,
        }

    def __setattr__(self, _name, _value):
        raise AttributeError(
            "google_oidc_transaction_cleanup_result_is_immutable"
        )

    def __repr__(self):
        return (
            "GoogleOidcAuthorizationTransactionCleanupResult("
            f"expired_count={self._expired_count!r}, "
            f"deleted_count={self._deleted_count!r}, "
            f"limit={self._limit!r}, "
            "terminal_retention_seconds="
            f"{self._terminal_retention_seconds!r}, "
            "candidate_inspection_limit="
            f"{self._candidate_inspection_limit!r}, "
            "terminal_candidates_inspected="
            f"{self._terminal_candidates_inspected!r}, "
            f"skipped_too_recent={self._skipped_too_recent!r}, "
            "skipped_structurally_invalid="
            f"{self._skipped_structurally_invalid!r}, "
            "skipped_unsupported_version="
            f"{self._skipped_unsupported_version!r}, "
            "skipped_chronology_invalid="
            f"{self._skipped_chronology_invalid!r}, "
            f"known_remaining={self._known_remaining!r}, "
            f"remaining_exact={self._remaining_exact!r}, "
            "candidate_inspection_truncated="
            f"{self._candidate_inspection_truncated!r}, "
            f"complete={self._complete!r}, "
            f"commit_outcome={self._commit_outcome!r})"
        )

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError(
            "google_oidc_transaction_cleanup_result_not_serializable"
        )

    def __copy__(self):
        raise TypeError(
            "google_oidc_transaction_cleanup_result_not_copyable"
        )

    def __deepcopy__(self, _memo):
        raise TypeError(
            "google_oidc_transaction_cleanup_result_not_copyable"
        )

    def __init_subclass__(cls, **_kwargs):
        raise TypeError(
            "google_oidc_transaction_cleanup_result_not_subclassable"
        )


class GoogleOidcAuthorizationTransactionReconciliationResult:
    """Immutable, bounded, row-sanitized reconciliation projection."""

    __slots__ = ("_status", "_inspected_rows", "_issues")

    def __new__(cls, *_args, **_kwargs):
        raise TypeError("google_oidc_transaction_reconciliation_required")

    @classmethod
    def _issue(
        cls,
        capability,
        *,
        inspected_rows,
        issues,
    ):
        if (
            cls is not GoogleOidcAuthorizationTransactionReconciliationResult
            or capability is not _RECONCILIATION_ISSUANCE_CAPABILITY
            or type(inspected_rows) is not int
            or not (
                0
                <= inspected_rows
                <= GOOGLE_OIDC_RECONCILIATION_BUDGET.max_scan_rows
            )
            or type(issues) is not tuple
            or len(issues) > MAX_RECONCILIATION_ISSUES
        ):
            raise TypeError("google_oidc_transaction_reconciliation_invalid")
        normalized = tuple(_validated_reconciliation_issue(issue) for issue in issues)
        instance = object.__new__(cls)
        object.__setattr__(
            instance,
            "_status",
            "ok" if not normalized else "issues_detected",
        )
        object.__setattr__(instance, "_inspected_rows", inspected_rows)
        object.__setattr__(instance, "_issues", normalized)
        return instance

    @property
    def status(self):
        return self._status

    @property
    def inspected_rows(self):
        return self._inspected_rows

    @property
    def issues(self):
        return tuple(dict(issue) for issue in self._issues)

    def as_dict(self):
        return {
            "status": self._status,
            "inspected_rows": self._inspected_rows,
            "issues": [dict(issue) for issue in self._issues],
        }

    def __setattr__(self, _name, _value):
        raise AttributeError(
            "google_oidc_transaction_reconciliation_is_immutable"
        )

    def __repr__(self):
        return (
            "GoogleOidcAuthorizationTransactionReconciliationResult("
            f"status={self._status!r}, inspected_rows={self._inspected_rows!r}, "
            f"issue_count={len(self._issues)!r})"
        )

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError(
            "google_oidc_transaction_reconciliation_not_serializable"
        )

    def __copy__(self):
        raise TypeError(
            "google_oidc_transaction_reconciliation_not_copyable"
        )

    def __deepcopy__(self, _memo):
        raise TypeError(
            "google_oidc_transaction_reconciliation_not_copyable"
        )

    def __init_subclass__(cls, **_kwargs):
        raise TypeError(
            "google_oidc_transaction_reconciliation_not_subclassable"
        )


class _PreparedAuthorizationRecord:
    __slots__ = (
        "transaction_id",
        "authorization_url_buffer",
        "created_at",
        "expires_at",
        "lock",
        "closed",
    )

    def __init__(
        self,
        *,
        transaction_id,
        authorization_url_buffer,
        created_at,
        expires_at,
    ):
        self.transaction_id = transaction_id
        self.authorization_url_buffer = authorization_url_buffer
        self.created_at = created_at
        self.expires_at = expires_at
        self.lock = threading.Lock()
        self.closed = False


class _ClaimedMaterialRecord:
    __slots__ = (
        "transaction_id",
        "record_version",
        "provider",
        "environment_namespace",
        "configuration_fingerprint",
        "state_digest_version",
        "lookup_key_version",
        "created_at",
        "expires_at",
        "claimed_at",
        "protection_envelope_version",
        "protection_key_version",
        "state",
        "nonce",
        "pkce_verifier",
        "b2d1_request_key",
        "invitation_credential",
        "lock",
        "used",
    )

    def __init__(self, **values):
        for name, value in values.items():
            setattr(self, name, value)
        self.lock = threading.Lock()
        self.used = False


def _issue_prepared_authorization(
    *,
    transaction_id,
    authorization_url,
    created_at,
    expires_at,
):
    return PreparedDurableGoogleOidcAuthorization._issue(
        _PREPARED_ISSUANCE_CAPABILITY,
        transaction_id=transaction_id,
        authorization_url=authorization_url,
        created_at=created_at,
        expires_at=expires_at,
    )


def _issue_claimed_material(**values):
    return ClaimedGoogleOidcAuthorizationMaterial._issue(
        _CLAIMED_ISSUANCE_CAPABILITY,
        **values,
    )


def _take_claimed_material(capsule):
    if type(capsule) is not ClaimedGoogleOidcAuthorizationMaterial:
        raise TypeError("claimed_google_oidc_material_required")
    return capsule._take(_CLAIMED_SERVICE_CAPABILITY)


def _clear_claimed_material_values(values):
    if type(values) is not dict:
        return
    for name in (
        "state",
        "nonce",
        "pkce_verifier",
        "b2d1_request_key",
        "invitation_credential",
    ):
        _clear_buffer(values.get(name))
        values[name] = None
    values.clear()


def _issue_cleanup_result(
    *,
    expired_count,
    deleted_count,
    limit,
    terminal_retention_seconds,
    candidate_inspection_limit,
    terminal_candidates_inspected,
    skipped_too_recent,
    skipped_structurally_invalid,
    skipped_unsupported_version,
    skipped_chronology_invalid,
    known_remaining,
    remaining_exact,
    candidate_inspection_truncated,
    complete,
    commit_outcome,
):
    return GoogleOidcAuthorizationTransactionCleanupResult._issue(
        _CLEANUP_ISSUANCE_CAPABILITY,
        expired_count=expired_count,
        deleted_count=deleted_count,
        limit=limit,
        terminal_retention_seconds=terminal_retention_seconds,
        candidate_inspection_limit=candidate_inspection_limit,
        terminal_candidates_inspected=terminal_candidates_inspected,
        skipped_too_recent=skipped_too_recent,
        skipped_structurally_invalid=skipped_structurally_invalid,
        skipped_unsupported_version=skipped_unsupported_version,
        skipped_chronology_invalid=skipped_chronology_invalid,
        known_remaining=known_remaining,
        remaining_exact=remaining_exact,
        candidate_inspection_truncated=candidate_inspection_truncated,
        complete=complete,
        commit_outcome=commit_outcome,
    )


def _issue_reconciliation_result(*, inspected_rows, issues):
    return GoogleOidcAuthorizationTransactionReconciliationResult._issue(
        _RECONCILIATION_ISSUANCE_CAPABILITY,
        inspected_rows=inspected_rows,
        issues=issues,
    )


def _canonical_utc_time(value):
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
        or value.microsecond != 0
    ):
        raise TypeError("google_oidc_transaction_time_invalid")
    return value.astimezone(timezone.utc)


def _canonical_time_text(value):
    return _canonical_utc_time(value).isoformat(timespec="seconds")


def _parse_canonical_time_text(value):
    if type(value) is not str or len(value) != 25:
        raise TypeError("google_oidc_transaction_time_invalid")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S+00:00").replace(
            tzinfo=timezone.utc
        )
    except (TypeError, ValueError):
        raise TypeError("google_oidc_transaction_time_invalid") from None
    if _canonical_time_text(parsed) != value:
        raise TypeError("google_oidc_transaction_time_invalid")
    return parsed


def _canonical_associated_data(
    *,
    transaction_id,
    provider,
    environment_namespace,
    configuration_fingerprint,
    state_digest_version,
    lookup_key_version,
    state_lookup_digest,
    created_at,
    expires_at,
    protection_envelope_version,
    protection_key_version,
    record_version=TRANSACTION_RECORD_VERSION,
):
    transaction_id = _validated_transaction_id(transaction_id)
    record_version = _validated_exact_version(
        record_version,
        TRANSACTION_RECORD_VERSION,
    )
    provider = _validated_provider(provider)
    environment_namespace = _validated_environment(environment_namespace)
    configuration_fingerprint = _validated_fixed_bytes(
        configuration_fingerprint,
        32,
    )
    state_digest_version = _validated_exact_version(
        state_digest_version,
        STATE_DIGEST_VERSION,
    )
    lookup_key_version = _validated_key_version(lookup_key_version)
    state_lookup_digest = _validated_fixed_bytes(state_lookup_digest, 32)
    created_at = _canonical_utc_time(created_at)
    expires_at = _canonical_utc_time(expires_at)
    _require_exact_ten_minute_chronology(created_at, expires_at)
    protection_envelope_version = _validated_exact_version(
        protection_envelope_version,
        PROTECTION_ENVELOPE_VERSION,
    )
    protection_key_version = _validated_key_version(protection_key_version)
    return _length_prefixed_encoding(
        _ASSOCIATED_DATA_DOMAIN,
        (
            ASSOCIATED_DATA_VERSION.to_bytes(4, "big"),
            transaction_id.encode("ascii"),
            record_version.to_bytes(4, "big"),
            provider.encode("ascii"),
            environment_namespace.encode("ascii"),
            configuration_fingerprint,
            state_digest_version.to_bytes(4, "big"),
            lookup_key_version.to_bytes(4, "big"),
            state_lookup_digest,
            _canonical_time_text(created_at).encode("ascii"),
            _canonical_time_text(expires_at).encode("ascii"),
            protection_envelope_version.to_bytes(4, "big"),
            protection_key_version.to_bytes(4, "big"),
        ),
    )


def _canonical_state_lookup_input(state):
    state_bytes = _validated_base64url_buffer(
        state,
        encoded_length=43,
        decoded_length=32,
        field="state",
        accept_text=True,
    )
    try:
        return _length_prefixed_encoding(
            b"wahojobs-google-oidc-state-lookup-v1",
            (STATE_DIGEST_VERSION.to_bytes(4, "big"), bytes(state_bytes)),
        )
    finally:
        _clear_buffer(state_bytes)


def _canonical_configuration_binding_input(configuration_binding):
    if (
        type(configuration_binding) is not bytes
        or not (
            1
            <= len(configuration_binding)
            <= MAX_DURABLE_CONFIGURATION_CONTEXT_BYTES
        )
    ):
        raise TypeError("google_oidc_configuration_binding_invalid")
    return _length_prefixed_encoding(
        b"wahojobs-google-oidc-configuration-binding-v1",
        (bytes(configuration_binding),),
    )


def _serialize_protected_material_v1(
    *,
    state,
    nonce,
    pkce_verifier,
    b2d1_request_key,
):
    state_buffer = None
    nonce_buffer = None
    verifier_buffer = None
    request_buffer = None
    try:
        state_buffer = _validated_base64url_buffer(
            state,
            encoded_length=43,
            decoded_length=32,
            field="state",
        )
        nonce_buffer = _validated_base64url_buffer(
            nonce,
            encoded_length=43,
            decoded_length=32,
            field="nonce",
        )
        verifier_buffer = _validated_base64url_buffer(
            pkce_verifier,
            encoded_length=86,
            decoded_length=64,
            field="pkce_verifier",
        )
        request_buffer = _validated_b2d1_request_key_buffer(
            b2d1_request_key
        )
        encoded = _length_prefixed_encoding(
            _PROTECTED_MATERIAL_DOMAIN_V1,
            (
                _LEGACY_PROTECTED_MATERIAL_VERSION.to_bytes(4, "big"),
                bytes(state_buffer),
                bytes(nonce_buffer),
                bytes(verifier_buffer),
                bytes(request_buffer),
            ),
        )
        if len(encoded) > MAX_PROTECTED_PLAINTEXT_BYTES:
            raise TypeError("google_oidc_protected_material_invalid")
        return bytearray(encoded)
    finally:
        _clear_buffer(state_buffer)
        _clear_buffer(nonce_buffer)
        _clear_buffer(verifier_buffer)
        _clear_buffer(request_buffer)


def _parse_protected_material_v1(serialized):
    if (
        type(serialized) is not bytearray
        or not serialized
        or len(serialized) > MAX_PROTECTED_PLAINTEXT_BYTES
    ):
        raise TypeError("google_oidc_protected_material_invalid")
    components = _parse_length_prefixed_encoding(
        serialized,
        expected_domain=_PROTECTED_MATERIAL_DOMAIN_V1,
        expected_count=5,
    )
    retained = False
    try:
        if components[0] != bytearray(
            _LEGACY_PROTECTED_MATERIAL_VERSION.to_bytes(4, "big")
        ):
            raise TypeError("google_oidc_protected_material_invalid")
        state = _validated_base64url_buffer(
            components[1],
            encoded_length=43,
            decoded_length=32,
            field="state",
        )
        nonce = _validated_base64url_buffer(
            components[2],
            encoded_length=43,
            decoded_length=32,
            field="nonce",
        )
        verifier = _validated_base64url_buffer(
            components[3],
            encoded_length=86,
            decoded_length=64,
            field="pkce_verifier",
        )
        request_key = _validated_b2d1_request_key_buffer(components[4])
        values = {
            "state": state,
            "nonce": nonce,
            "pkce_verifier": verifier,
            "b2d1_request_key": request_key,
        }
        retained = True
        return values
    finally:
        for component in components:
            _clear_buffer(component)
        if not retained:
            for name in (
                "state",
                "nonce",
                "verifier",
                "request_key",
            ):
                _clear_buffer(locals().get(name))


def _serialize_protected_material(
    *,
    state,
    nonce,
    pkce_verifier,
    b2d1_request_key,
    invitation_credential=None,
):
    state_buffer = None
    nonce_buffer = None
    verifier_buffer = None
    request_buffer = None
    invitation_buffer = None
    try:
        state_buffer = _validated_base64url_buffer(
            state,
            encoded_length=43,
            decoded_length=32,
            field="state",
        )
        nonce_buffer = _validated_base64url_buffer(
            nonce,
            encoded_length=43,
            decoded_length=32,
            field="nonce",
        )
        verifier_buffer = _validated_base64url_buffer(
            pkce_verifier,
            encoded_length=86,
            decoded_length=64,
            field="pkce_verifier",
        )
        request_buffer = _validated_b2d1_request_key_buffer(
            b2d1_request_key
        )
        invitation_buffer = _validated_invitation_credential_buffer(
            invitation_credential,
            allow_none=True,
        )
        encoded = _length_prefixed_encoding(
            _PROTECTED_MATERIAL_DOMAIN_V2,
            (
                PROTECTED_MATERIAL_VERSION.to_bytes(4, "big"),
                bytes(state_buffer),
                bytes(nonce_buffer),
                bytes(verifier_buffer),
                bytes(request_buffer),
                b"" if invitation_buffer is None else bytes(invitation_buffer),
            ),
        )
        if len(encoded) > MAX_PROTECTED_PLAINTEXT_BYTES:
            raise TypeError("google_oidc_protected_material_invalid")
        return bytearray(encoded)
    finally:
        _clear_buffer(state_buffer)
        _clear_buffer(nonce_buffer)
        _clear_buffer(verifier_buffer)
        _clear_buffer(request_buffer)
        _clear_buffer(invitation_buffer)


def _parse_protected_material(serialized):
    domain = _protected_material_domain(serialized)
    if domain == _PROTECTED_MATERIAL_DOMAIN_V1:
        values = _parse_protected_material_v1(serialized)
        values["invitation_credential"] = None
        return values
    if domain == _PROTECTED_MATERIAL_DOMAIN_V2:
        return _parse_protected_material_v2(serialized)
    raise TypeError("google_oidc_protected_material_invalid")


def _parse_protected_material_v2(serialized):
    if (
        type(serialized) is not bytearray
        or not serialized
        or len(serialized) > MAX_PROTECTED_PLAINTEXT_BYTES
    ):
        raise TypeError("google_oidc_protected_material_invalid")
    components = _parse_length_prefixed_encoding(
        serialized,
        expected_domain=_PROTECTED_MATERIAL_DOMAIN_V2,
        expected_count=6,
    )
    retained = False
    try:
        if components[0] != bytearray(
            PROTECTED_MATERIAL_VERSION.to_bytes(4, "big")
        ):
            raise TypeError("google_oidc_protected_material_invalid")
        state = _validated_base64url_buffer(
            components[1],
            encoded_length=43,
            decoded_length=32,
            field="state",
        )
        nonce = _validated_base64url_buffer(
            components[2],
            encoded_length=43,
            decoded_length=32,
            field="nonce",
        )
        verifier = _validated_base64url_buffer(
            components[3],
            encoded_length=86,
            decoded_length=64,
            field="pkce_verifier",
        )
        request_key = _validated_b2d1_request_key_buffer(components[4])
        invitation = (
            None
            if not components[5]
            else _validated_invitation_credential_buffer(components[5])
        )
        values = {
            "state": state,
            "nonce": nonce,
            "pkce_verifier": verifier,
            "b2d1_request_key": request_key,
            "invitation_credential": invitation,
        }
        retained = True
        return values
    finally:
        for component in components:
            _clear_buffer(component)
        if not retained:
            for name in (
                "state",
                "nonce",
                "verifier",
                "request_key",
                "invitation",
            ):
                _clear_buffer(locals().get(name))


def _protected_material_domain(serialized):
    if (
        type(serialized) is not bytearray
        or len(serialized) < 3
        or len(serialized) > MAX_PROTECTED_PLAINTEXT_BYTES
    ):
        raise TypeError("google_oidc_protected_material_invalid")
    domain_length = int.from_bytes(serialized[:2], "big")
    domain_end = 2 + domain_length
    if domain_length < 1 or domain_end >= len(serialized):
        raise TypeError("google_oidc_protected_material_invalid")
    return bytes(serialized[2:domain_end])


def _validated_invitation_credential_buffer(value, *, allow_none=False):
    if allow_none and value is None:
        return None
    if (
        type(value) is not bytearray
        or not (1 <= len(value) <= MAX_INVITATION_CREDENTIAL_BYTES)
    ):
        raise TypeError("google_oidc_invitation_credential_invalid")
    return bytearray(value)


def _length_prefixed_encoding(domain, components):
    if (
        type(domain) is not bytes
        or not domain
        or len(domain) > 255
        or type(components) is not tuple
        or not (1 <= len(components) <= 255)
        or any(type(component) is not bytes for component in components)
    ):
        raise TypeError("google_oidc_canonical_encoding_invalid")
    total = 2 + len(domain) + 1
    for component in components:
        if len(component) > 65_535:
            raise TypeError("google_oidc_canonical_encoding_invalid")
        total += 4 + len(component)
    if total > 65_535:
        raise TypeError("google_oidc_canonical_encoding_invalid")
    encoded = bytearray()
    encoded.extend(len(domain).to_bytes(2, "big"))
    encoded.extend(domain)
    encoded.append(len(components))
    for component in components:
        encoded.extend(len(component).to_bytes(4, "big"))
        encoded.extend(component)
    return bytes(encoded)


def _parse_length_prefixed_encoding(
    serialized,
    *,
    expected_domain,
    expected_count,
):
    if (
        type(serialized) is not bytearray
        or type(expected_domain) is not bytes
        or not expected_domain
        or type(expected_count) is not int
        or not (1 <= expected_count <= 255)
    ):
        raise TypeError("google_oidc_canonical_encoding_invalid")
    components = []
    try:
        if len(serialized) < 3:
            raise TypeError("google_oidc_canonical_encoding_invalid")
        domain_length = int.from_bytes(serialized[0:2], "big")
        position = 2
        if domain_length != len(expected_domain):
            raise TypeError("google_oidc_canonical_encoding_invalid")
        domain = serialized[position : position + domain_length]
        position += domain_length
        try:
            if bytes(domain) != expected_domain:
                raise TypeError("google_oidc_canonical_encoding_invalid")
        finally:
            _clear_buffer(domain)
        if position >= len(serialized) or serialized[position] != expected_count:
            raise TypeError("google_oidc_canonical_encoding_invalid")
        position += 1
        for _index in range(expected_count):
            if position + 4 > len(serialized):
                raise TypeError("google_oidc_canonical_encoding_invalid")
            length = int.from_bytes(serialized[position : position + 4], "big")
            position += 4
            if position + length > len(serialized):
                raise TypeError("google_oidc_canonical_encoding_invalid")
            components.append(serialized[position : position + length])
            position += length
        if position != len(serialized):
            raise TypeError("google_oidc_canonical_encoding_invalid")
        return components
    except Exception:
        for component in components:
            _clear_buffer(component)
        raise


def _validated_claimed_values(values):
    if type(values) is not dict:
        raise TypeError("claimed_google_oidc_material_invalid")
    expected_names = {
        "transaction_id",
        "record_version",
        "provider",
        "environment_namespace",
        "configuration_fingerprint",
        "state_digest_version",
        "lookup_key_version",
        "created_at",
        "expires_at",
        "claimed_at",
        "protection_envelope_version",
        "protection_key_version",
        "state",
        "nonce",
        "pkce_verifier",
        "b2d1_request_key",
        "invitation_credential",
    }
    if set(values) != expected_names:
        raise TypeError("claimed_google_oidc_material_invalid")
    created_at = _canonical_utc_time(values["created_at"])
    expires_at = _canonical_utc_time(values["expires_at"])
    claimed_at = _canonical_utc_time(values["claimed_at"])
    _require_exact_ten_minute_chronology(created_at, expires_at)
    if not (created_at <= claimed_at < expires_at):
        raise TypeError("claimed_google_oidc_material_invalid")
    return {
        "transaction_id": _validated_transaction_id(values["transaction_id"]),
        "record_version": _validated_exact_version(
            values["record_version"],
            TRANSACTION_RECORD_VERSION,
        ),
        "provider": _validated_provider(values["provider"]),
        "environment_namespace": _validated_environment(
            values["environment_namespace"]
        ),
        "configuration_fingerprint": _validated_fixed_bytes(
            values["configuration_fingerprint"],
            32,
        ),
        "state_digest_version": _validated_exact_version(
            values["state_digest_version"],
            STATE_DIGEST_VERSION,
        ),
        "lookup_key_version": _validated_key_version(
            values["lookup_key_version"]
        ),
        "created_at": created_at,
        "expires_at": expires_at,
        "claimed_at": claimed_at,
        "protection_envelope_version": _validated_exact_version(
            values["protection_envelope_version"],
            PROTECTION_ENVELOPE_VERSION,
        ),
        "protection_key_version": _validated_key_version(
            values["protection_key_version"]
        ),
        "state": _validated_owned_secret_buffer(
            values["state"],
            validator=lambda value: _validated_base64url_buffer(
                value,
                encoded_length=43,
                decoded_length=32,
                field="state",
            ),
        ),
        "nonce": _validated_owned_secret_buffer(
            values["nonce"],
            validator=lambda value: _validated_base64url_buffer(
                value,
                encoded_length=43,
                decoded_length=32,
                field="nonce",
            ),
        ),
        "pkce_verifier": _validated_owned_secret_buffer(
            values["pkce_verifier"],
            validator=lambda value: _validated_base64url_buffer(
                value,
                encoded_length=86,
                decoded_length=64,
                field="pkce_verifier",
            ),
        ),
        "b2d1_request_key": _validated_owned_secret_buffer(
            values["b2d1_request_key"],
            validator=_validated_b2d1_request_key_buffer,
        ),
        "invitation_credential": _validated_owned_optional_secret_buffer(
            values["invitation_credential"],
        ),
    }


def _validated_owned_secret_buffer(value, *, validator):
    if type(value) is not bytearray:
        raise TypeError("claimed_google_oidc_material_invalid")
    validated = validator(value)
    try:
        if validated != value:
            raise TypeError("claimed_google_oidc_material_invalid")
    finally:
        _clear_buffer(validated)
    return value


def _validated_owned_optional_secret_buffer(value):
    if value is None:
        return None
    return _validated_owned_secret_buffer(
        value,
        validator=_validated_invitation_credential_buffer,
    )


def _validated_reconciliation_issue(issue):
    if (
        type(issue) is not dict
        or set(issue) not in ({"code"}, {"code", "ordinal"})
        or type(issue.get("code")) is not str
        or _ISSUE_CODE.fullmatch(issue["code"]) is None
    ):
        raise TypeError("google_oidc_transaction_reconciliation_invalid")
    normalized = {"code": issue["code"]}
    if "ordinal" in issue:
        ordinal = issue["ordinal"]
        if (
            type(ordinal) is not int
            or not (
                1
                <= ordinal
                <= GOOGLE_OIDC_RECONCILIATION_BUDGET.max_scan_rows
            )
        ):
            raise TypeError("google_oidc_transaction_reconciliation_invalid")
        normalized["ordinal"] = ordinal
    return normalized


def _prepared_record(value):
    if type(value) is not PreparedDurableGoogleOidcAuthorization:
        raise TypeError("durable_google_oidc_preparation_required")
    try:
        record = object.__getattribute__(
            value,
            "_PreparedDurableGoogleOidcAuthorization__record",
        )
    except AttributeError:
        raise TypeError("durable_google_oidc_preparation_required") from None
    if type(record) is not _PreparedAuthorizationRecord:
        raise TypeError("durable_google_oidc_preparation_required")
    return record


def _claimed_record(value):
    if type(value) is not ClaimedGoogleOidcAuthorizationMaterial:
        raise TypeError("claimed_google_oidc_material_required")
    try:
        record = object.__getattribute__(
            value,
            "_ClaimedGoogleOidcAuthorizationMaterial__record",
        )
    except AttributeError:
        raise TypeError("claimed_google_oidc_material_required") from None
    if type(record) is not _ClaimedMaterialRecord:
        raise TypeError("claimed_google_oidc_material_required")
    return record


def _validated_transaction_id(value):
    if type(value) is not str or _TRANSACTION_ID.fullmatch(value) is None:
        raise TypeError("google_oidc_transaction_id_invalid")
    return value


def _validated_provider(value):
    if type(value) is not str or value != "google":
        raise TypeError("google_oidc_transaction_provider_invalid")
    return value


def _validated_environment(value):
    if (
        type(value) is not str
        or _ENVIRONMENT_NAMESPACE.fullmatch(value) is None
    ):
        raise TypeError("google_oidc_transaction_environment_invalid")
    return value


def _validated_key_version(value):
    if type(value) is not int or not (1 <= value <= MAX_KEY_VERSION):
        raise TypeError("google_oidc_transaction_key_version_invalid")
    return value


def _validated_exact_version(value, expected):
    if type(value) is not int or value != expected:
        raise TypeError("google_oidc_transaction_version_invalid")
    return value


def _validated_fixed_bytes(value, length):
    if type(value) is not bytes or len(value) != length:
        raise TypeError("google_oidc_transaction_binary_field_invalid")
    return bytes(value)


def _validated_authorization_url_buffer(value):
    if type(value) is not str:
        raise TypeError("durable_google_oidc_authorization_url_invalid")
    try:
        encoded = value.encode("ascii")
    except UnicodeError:
        raise TypeError(
            "durable_google_oidc_authorization_url_invalid"
        ) from None
    if (
        not encoded
        or len(encoded) > MAX_AUTHORIZATION_URL_BYTES
        or any(byte < 0x21 or byte == 0x7F for byte in encoded)
    ):
        raise TypeError("durable_google_oidc_authorization_url_invalid")
    return bytearray(encoded)


def _validated_base64url_buffer(
    value,
    *,
    encoded_length,
    decoded_length,
    field,
    accept_text=False,
):
    if accept_text and type(value) is str:
        try:
            source = bytearray(value.encode("ascii"))
        except UnicodeError:
            raise TypeError(f"google_oidc_{field}_invalid") from None
    elif type(value) is bytearray:
        source = bytearray(value)
    else:
        raise TypeError(f"google_oidc_{field}_invalid")
    try:
        if (
            len(source) != encoded_length
            or _BASE64URL.fullmatch(
                bytes(source).decode("ascii", errors="strict")
            )
            is None
        ):
            raise TypeError(f"google_oidc_{field}_invalid")
        decoded = base64.urlsafe_b64decode(bytes(source) + b"==")
        canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=")
        if len(decoded) != decoded_length or canonical != bytes(source):
            raise TypeError(f"google_oidc_{field}_invalid")
        return bytearray(source)
    except (UnicodeError, ValueError):
        raise TypeError(f"google_oidc_{field}_invalid") from None
    finally:
        _clear_buffer(source)


def _validated_b2d1_request_key_buffer(value):
    if type(value) is not bytearray:
        raise TypeError("google_oidc_b2d1_request_key_invalid")
    copied = bytearray(value)
    suffix = bytearray()
    try:
        prefix = b"google-oidc-"
        if len(copied) != len(prefix) + 43 or copied[: len(prefix)] != prefix:
            raise TypeError("google_oidc_b2d1_request_key_invalid")
        suffix = copied[len(prefix) :]
        validated = _validated_base64url_buffer(
            suffix,
            encoded_length=43,
            decoded_length=32,
            field="b2d1_request_key",
        )
        _clear_buffer(validated)
        return bytearray(copied)
    finally:
        _clear_buffer(copied)
        _clear_buffer(suffix)


def _require_exact_ten_minute_chronology(created_at, expires_at):
    if expires_at != created_at + timedelta(seconds=TRANSACTION_TTL_SECONDS):
        raise TypeError("google_oidc_transaction_chronology_invalid")


def _clear_buffer(value):
    if type(value) is bytearray:
        value[:] = b"\x00" * len(value)
        value.clear()

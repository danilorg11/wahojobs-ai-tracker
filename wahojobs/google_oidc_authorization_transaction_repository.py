"""Caller-connection repository for durable Google OIDC transactions.

Every write uses one short repository-owned ``BEGIN IMMEDIATE`` transaction.
The module never opens a database and rejects caller-owned outer transactions.
It enables and verifies ``recursive_triggers`` both before beginning work and
again under the owned write lock immediately before the first mutation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import hmac
import secrets
import sqlite3

from wahojobs.google_oidc_authorization_transaction_schema import (
    attest_google_oidc_authorization_transaction_schema,
)
from wahojobs.google_oidc_authorization_transactions import (
    GOOGLE_OIDC_CLEANUP_CONTRACT,
    PROTECTION_ENVELOPE_VERSION,
    STATE_DIGEST_VERSION,
    TRANSACTION_RECORD_VERSION,
    TRANSACTION_TTL_SECONDS,
    _canonical_associated_data,
    _clear_claimed_material_values,
    _issue_claimed_material,
    _issue_cleanup_result,
    _issue_prepared_authorization,
    _parse_canonical_time_text,
)
from wahojobs.google_oidc_authorization_transaction_reconciliation import (
    _COLUMNS as _RECONCILIATION_COLUMNS,
    _VALUE_CAPS as _RECONCILIATION_VALUE_CAPS,
    _parse_timestamp as _reconciliation_parse_timestamp,
    _prepare_projected_row as _prepare_reconciliation_projected_row,
    _reuse_metadata as _reconciliation_reuse_metadata,
    _scan_row as _scan_reconciliation_row,
)
from wahojobs.google_oidc_gateway import (
    GoogleOidcGateway,
    _durable_google_oidc_authorization_url,
    _durable_google_oidc_context,
    _durable_google_oidc_now,
)
from wahojobs.google_oidc_transaction_protection import (
    GoogleOidcTransactionKeyAuthority,
    GoogleOidcTransactionProtectionError,
    _configuration_fingerprint,
    _protect_material,
    _state_lookup_digests,
    _unprotect_material,
    _verify_state_lookup_digest,
)


_ERROR_REASONS = frozenset(
    {
        "invalid_or_expired_transaction",
        "temporary_contention",
        "unavailable",
    }
)
_BUSY_CODES = {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
_SELECT_COLUMNS = (
    "transaction_id",
    "record_version",
    "provider",
    "environment_namespace",
    "configuration_fingerprint",
    "state_digest_version",
    "lookup_key_version",
    "state_lookup_digest",
    "created_at",
    "expires_at",
    "lifecycle",
    "claimed_at",
    "terminal_at",
    "row_version",
    "protection_envelope_version",
    "protection_key_version",
    "protection_nonce",
    "protected_material",
)
_SELECT_PROJECTION = ", ".join(_SELECT_COLUMNS)
_CLEANUP_CANDIDATE_INSPECTION_LIMIT = (
    GOOGLE_OIDC_CLEANUP_CONTRACT.max_candidate_inspections
)
_CLEANUP_UNSUPPORTED_VERSION_CODES = frozenset(
    {
        "unknown_lookup_key_version",
        "unknown_protection_key_version",
    }
)
_CLEANUP_CHRONOLOGY_CODES = frozenset(
    {
        "invalid_created_at",
        "invalid_expires_at",
        "invalid_ten_minute_chronology",
        "invalid_claimed_chronology",
        "invalid_terminal_chronology",
        "terminal_missing_chronology",
        "contradictory_transaction_row",
    }
)


@dataclass(frozen=True, slots=True)
class _CleanupOperationState:
    limit: int
    terminal_retention_seconds: int
    candidate_inspection_limit: int


class _CleanupValidationCollector:
    __slots__ = ("codes",)

    def __init__(self):
        self.codes = []

    def add(self, code, _ordinal, *, private_key):
        if type(code) is not str or type(private_key) is not bytes:
            raise _RepositoryFailure("unavailable")
        self.codes.append(code)


class GoogleOidcAuthorizationTransactionRepositoryError(Exception):
    """Final, redacted repository failure with one bounded reason code."""

    __slots__ = ("reason_code",)

    def __init__(self, reason_code):
        if reason_code not in _ERROR_REASONS:
            reason_code = "unavailable"
        self.reason_code = reason_code
        super().__init__(
            "Durable Google OIDC authorization transaction unavailable."
        )

    def __repr__(self):
        return (
            "GoogleOidcAuthorizationTransactionRepositoryError("
            f"reason_code={self.reason_code!r})"
        )

    def __reduce_ex__(self, _protocol):
        raise TypeError("google_oidc_transaction_repository_error_not_serializable")

    def __copy__(self):
        raise TypeError("google_oidc_transaction_repository_error_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("google_oidc_transaction_repository_error_not_copyable")

    def __init_subclass__(cls, **_kwargs):
        raise TypeError(
            "google_oidc_transaction_repository_error_not_subclassable"
        )


def prepare_google_oidc_authorization_transaction(
    connection,
    gateway,
    key_authority,
    *,
    invitation_credential=None,
):
    """Create and commit one fresh transaction through a sanitized boundary."""

    try:
        outcome = _prepare_google_oidc_authorization_transaction_sensitive(
            connection,
            gateway,
            key_authority,
            invitation_credential=invitation_credential,
        )
    finally:
        _clear_buffer(invitation_credential)
        connection = None
        gateway = None
        key_authority = None
        invitation_credential = None
    return _resolve_repository_outcome(outcome)


def _prepare_google_oidc_authorization_transaction_sensitive(
    connection,
    gateway,
    key_authority,
    *,
    invitation_credential=None,
):
    """Create, seal, and commit one fresh transaction before returning its URL."""

    url_buffer = None
    digests = ()
    envelope = None
    state = None
    nonce = None
    pkce_verifier = None
    b2d1_request_key = None
    try:
        connection = _require_idle_writable_connection(connection)
        _require_gateway_and_authority(gateway, key_authority)
        _attest(connection)
        provider, environment, binding = _durable_google_oidc_context(gateway)
        created_at = _durable_google_oidc_now(gateway)
        expires_at = created_at + timedelta(
            seconds=TRANSACTION_TTL_SECONDS
        )
        transaction_id = "oidctx_" + secrets.token_hex(16)
        state = bytearray(secrets.token_urlsafe(32).encode("ascii"))
        nonce = bytearray(secrets.token_urlsafe(32).encode("ascii"))
        pkce_verifier = bytearray(
            secrets.token_urlsafe(64).encode("ascii")
        )
        b2d1_request_key = bytearray(
            ("google-oidc-" + secrets.token_urlsafe(32)).encode("ascii")
        )
        try:
            url_buffer = _durable_google_oidc_authorization_url(
                gateway,
                state=state,
                nonce=nonce,
                pkce_verifier=pkce_verifier,
            )
            digests = _state_lookup_digests(key_authority, state)
            lookup_version = key_authority.active_lookup_version
            state_digest = _digest_for_version(digests, lookup_version)
            configuration_fingerprint = _configuration_fingerprint(
                key_authority,
                lookup_version,
                binding,
            )
            protection_key_version = (
                key_authority.active_protection_version
            )
            associated_data = _canonical_associated_data(
                transaction_id=transaction_id,
                provider=provider,
                environment_namespace=environment,
                configuration_fingerprint=configuration_fingerprint,
                state_digest_version=STATE_DIGEST_VERSION,
                lookup_key_version=lookup_version,
                state_lookup_digest=state_digest,
                created_at=created_at,
                expires_at=expires_at,
                protection_envelope_version=PROTECTION_ENVELOPE_VERSION,
                protection_key_version=protection_key_version,
            )
            envelope = _protect_material(
                key_authority,
                state=state,
                nonce=nonce,
                pkce_verifier=pkce_verifier,
                b2d1_request_key=b2d1_request_key,
                associated_data=associated_data,
                invitation_credential=invitation_credential,
            )
        finally:
            _clear_buffer(state)
            _clear_buffer(nonce)
            _clear_buffer(pkce_verifier)
            _clear_buffer(b2d1_request_key)
            _clear_buffer(invitation_credential)

        expected = (
            transaction_id,
            TRANSACTION_RECORD_VERSION,
            provider,
            environment,
            configuration_fingerprint,
            STATE_DIGEST_VERSION,
            lookup_version,
            state_digest,
            created_at.isoformat(),
            expires_at.isoformat(),
            "prepared",
            None,
            None,
            1,
            envelope.version,
            envelope.key_version,
            envelope.nonce,
            envelope.ciphertext,
        )
        connection.execute("BEGIN IMMEDIATE")
        try:
            _failure_boundary("prepare.after_begin")
            locked_now = _durable_google_oidc_now(gateway)
            if locked_now < created_at or locked_now >= expires_at:
                raise _RepositoryFailure("unavailable")
            _attest(connection)
            if _lookup_rows(connection, digests, limit=1):
                raise _RepositoryFailure("unavailable")
            _enable_and_verify_recursive_triggers(connection)
            connection.execute(
                "INSERT INTO google_oidc_authorization_transactions "
                f"({_SELECT_PROJECTION}) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                expected,
            )
            _failure_boundary("prepare.after_insert")
            durable = connection.execute(
                f"SELECT {_SELECT_PROJECTION} "
                "FROM google_oidc_authorization_transactions "
                "WHERE transaction_id=?",
                (transaction_id,),
            ).fetchone()
            if durable is None or tuple(durable) != expected:
                raise _RepositoryFailure("unavailable")
            _failure_boundary("prepare.after_reread")
            connection.commit()
        except BaseException:
            _rollback_if_active(connection)
            raise
        if connection.in_transaction:
            raise _RepositoryFailure("unavailable")
        _failure_boundary("prepare.after_commit")
        try:
            authorization_url = bytes(url_buffer).decode("ascii", "strict")
        except UnicodeError:
            raise _RepositoryFailure("unavailable") from None
        result = _issue_prepared_authorization(
            transaction_id=transaction_id,
            authorization_url=authorization_url,
            created_at=created_at,
            expires_at=expires_at,
        )
        return result
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
        _detach_exception_graph(exc)
        raise
    except GoogleOidcAuthorizationTransactionRepositoryError as exc:
        reason = exc.reason_code
        _detach_exception_graph(exc)
        return _RepositoryFailureSignal(reason)
    except _RepositoryFailure as exc:
        reason = exc.reason
        _detach_exception_graph(exc)
        return _RepositoryFailureSignal(reason)
    except sqlite3.Error as exc:
        reason = _sqlite_reason(
            getattr(exc, "sqlite_errorcode", None)
        )
        _detach_exception_graph(exc)
        return _RepositoryFailureSignal(reason)
    except GoogleOidcTransactionProtectionError as exc:
        _detach_exception_graph(exc)
        return _RepositoryFailureSignal("unavailable")
    except Exception as exc:
        _detach_exception_graph(exc)
        return _RepositoryFailureSignal("unavailable")
    finally:
        _clear_buffer(url_buffer)
        url_buffer = None
        state = None
        nonce = None
        pkce_verifier = None
        b2d1_request_key = None
        invitation_credential = None
        digests = ()
        envelope = None
        binding = None
        provider = None
        environment = None
        created_at = None
        expires_at = None
        transaction_id = None
        lookup_version = None
        protection_key_version = None
        configuration_fingerprint = None
        state_digest = None
        associated_data = None
        expected = None
        durable = None
        locked_now = None
        authorization_url = None
        result = None
        connection = None
        gateway = None
        key_authority = None


def claim_google_oidc_authorization_transaction(
    connection,
    gateway,
    key_authority,
    callback_state,
):
    """Claim one transaction through a sanitized public failure boundary."""

    try:
        outcome = _claim_google_oidc_authorization_transaction_sensitive(
            connection,
            gateway,
            key_authority,
            callback_state,
        )
    finally:
        connection = None
        gateway = None
        key_authority = None
        callback_state = None
    return _resolve_repository_outcome(outcome)


def _claim_google_oidc_authorization_transaction_sensitive(
    connection,
    gateway,
    key_authority,
    callback_state,
):
    """Atomically terminalize one state, commit, and only then decrypt it."""

    material_values = None
    capsule = None
    digests = ()
    try:
        connection = _require_idle_writable_connection(connection)
        _require_gateway_and_authority(gateway, key_authority)
        provider, environment, binding = _durable_google_oidc_context(gateway)
        try:
            digests = _state_lookup_digests(key_authority, callback_state)
        except Exception:
            raise _RepositoryFailure(
                "invalid_or_expired_transaction"
            ) from None

        connection.execute("BEGIN IMMEDIATE")
        post_commit_reason = None
        row = None
        associated_data = None
        try:
            _failure_boundary("claim.after_begin")
            claimed_at = _durable_google_oidc_now(gateway)
            _attest(connection)
            candidates = _lookup_rows(connection, digests, limit=4)
            if not candidates:
                raise _RepositoryFailure(
                    "invalid_or_expired_transaction"
                )
            if len(candidates) != 1:
                _enable_and_verify_recursive_triggers(connection)
                _invalidate_candidates(
                    connection,
                    candidates,
                    claimed_at,
                )
                _failure_boundary("claim.after_multiple_invalidation")
                connection.commit()
                post_commit_reason = "invalid_or_expired_transaction"
            else:
                row = _row_dict(candidates[0])
                lifecycle = row["lifecycle"]
                if lifecycle != "prepared" or row["row_version"] != 1:
                    raise _RepositoryFailure(
                        "invalid_or_expired_transaction"
                    )
                created_at = _parse_canonical_time_text(row["created_at"])
                expires_at = _parse_canonical_time_text(row["expires_at"])
                verified_lookup = _verify_state_lookup_digest(
                    key_authority,
                    callback_state,
                    row["lookup_key_version"],
                    row["state_lookup_digest"],
                )
                expected_configuration = _configuration_fingerprint(
                    key_authority,
                    row["lookup_key_version"],
                    binding,
                )
                configuration_matches = (
                    row["provider"] == provider
                    and row["environment_namespace"] == environment
                    and hmac.compare_digest(
                        expected_configuration,
                        row["configuration_fingerprint"],
                    )
                )
                protection_version_known = (
                    row["protection_key_version"]
                    in key_authority.accepted_protection_versions
                )
                if claimed_at < created_at:
                    next_lifecycle = "invalidated"
                    transition_time = created_at
                    post_commit_reason = "invalid_or_expired_transaction"
                elif claimed_at >= expires_at:
                    next_lifecycle = "expired"
                    transition_time = claimed_at
                    post_commit_reason = "invalid_or_expired_transaction"
                elif not verified_lookup or not configuration_matches:
                    next_lifecycle = "invalidated"
                    transition_time = claimed_at
                    post_commit_reason = "invalid_or_expired_transaction"
                elif not protection_version_known:
                    next_lifecycle = "invalidated"
                    transition_time = claimed_at
                    post_commit_reason = "unavailable"
                else:
                    next_lifecycle = "consumed"
                    transition_time = claimed_at
                    associated_data = _associated_data_for_row(
                        row,
                        created_at,
                        expires_at,
                    )

                claimed_text = (
                    transition_time.isoformat()
                    if next_lifecycle == "consumed"
                    else None
                )
                terminal_text = transition_time.isoformat()
                _enable_and_verify_recursive_triggers(connection)
                cursor = connection.execute(
                    "UPDATE google_oidc_authorization_transactions "
                    "SET lifecycle=?, claimed_at=?, terminal_at=?, row_version=2 "
                    "WHERE transaction_id=? AND lifecycle='prepared' "
                    "AND row_version=1 AND claimed_at IS NULL "
                    "AND terminal_at IS NULL",
                    (
                        next_lifecycle,
                        claimed_text,
                        terminal_text,
                        row["transaction_id"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise _RepositoryFailure(
                        "invalid_or_expired_transaction"
                    )
                _failure_boundary("claim.after_transition")
                durable = connection.execute(
                    f"SELECT {_SELECT_PROJECTION} "
                    "FROM google_oidc_authorization_transactions "
                    "WHERE transaction_id=?",
                    (row["transaction_id"],),
                ).fetchone()
                expected = tuple(row[name] for name in _SELECT_COLUMNS)
                expected = (
                    *expected[:10],
                    next_lifecycle,
                    claimed_text,
                    terminal_text,
                    2,
                    *expected[14:],
                )
                if durable is None or tuple(durable) != expected:
                    raise _RepositoryFailure("unavailable")
                _failure_boundary("claim.after_reread")
                connection.commit()
            if connection.in_transaction:
                raise _RepositoryFailure("unavailable")
        except BaseException:
            _rollback_if_active(connection)
            raise

        _failure_boundary("claim.after_commit")
        if post_commit_reason is not None:
            raise _RepositoryFailure(post_commit_reason)
        if row is None or associated_data is None:
            raise _RepositoryFailure("unavailable")

        material_values = _unprotect_material(
            key_authority,
            protection_key_version=row["protection_key_version"],
            protection_nonce=row["protection_nonce"],
            protected_material=row["protected_material"],
            associated_data=associated_data,
        )
        _failure_boundary("claim.after_decrypt")
        protected_state = material_values.get("state")
        if (
            type(protected_state) is not bytearray
            or not _verify_state_lookup_digest(
                key_authority,
                protected_state,
                row["lookup_key_version"],
                row["state_lookup_digest"],
            )
            or not hmac.compare_digest(
                bytes(protected_state),
                callback_state.encode("ascii", "strict"),
            )
        ):
            raise _RepositoryFailure("unavailable")
        capsule = _issue_claimed_material(
            transaction_id=row["transaction_id"],
            record_version=row["record_version"],
            provider=row["provider"],
            environment_namespace=row["environment_namespace"],
            configuration_fingerprint=row["configuration_fingerprint"],
            state_digest_version=row["state_digest_version"],
            lookup_key_version=row["lookup_key_version"],
            created_at=_parse_canonical_time_text(row["created_at"]),
            expires_at=_parse_canonical_time_text(row["expires_at"]),
            claimed_at=claimed_at,
            protection_envelope_version=row[
                "protection_envelope_version"
            ],
            protection_key_version=row["protection_key_version"],
            state=material_values.pop("state"),
            nonce=material_values.pop("nonce"),
            pkce_verifier=material_values.pop("pkce_verifier"),
            b2d1_request_key=material_values.pop("b2d1_request_key"),
            invitation_credential=material_values.pop(
                "invitation_credential"
            ),
        )
        material_values.clear()
        return capsule
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
        _detach_exception_graph(exc)
        raise
    except GoogleOidcAuthorizationTransactionRepositoryError as exc:
        reason = exc.reason_code
        _detach_exception_graph(exc)
        return _RepositoryFailureSignal(reason)
    except _RepositoryFailure as exc:
        reason = exc.reason
        _detach_exception_graph(exc)
        return _RepositoryFailureSignal(reason)
    except sqlite3.Error as exc:
        reason = _sqlite_reason(
            getattr(exc, "sqlite_errorcode", None)
        )
        _detach_exception_graph(exc)
        return _RepositoryFailureSignal(reason)
    except GoogleOidcTransactionProtectionError as exc:
        _detach_exception_graph(exc)
        return _RepositoryFailureSignal("unavailable")
    except Exception as exc:
        _detach_exception_graph(exc)
        return _RepositoryFailureSignal("unavailable")
    finally:
        if capsule is None:
            _clear_claimed_material_values(material_values)
        material_values = None
        capsule = None
        digests = ()
        provider = None
        environment = None
        binding = None
        candidates = None
        row = None
        associated_data = None
        expected_configuration = None
        configuration_matches = None
        lifecycle = None
        created_at = None
        expires_at = None
        claimed_at = None
        transition_time = None
        claimed_text = None
        terminal_text = None
        next_lifecycle = None
        cursor = None
        durable = None
        expected = None
        protected_state = None
        connection = None
        gateway = None
        key_authority = None
        callback_state = None


def cleanup_google_oidc_authorization_transactions(
    connection,
    gateway,
    key_authority,
    *,
    limit,
    terminal_retention_seconds,
):
    """Expire prepared rows and retain terminals for an explicit duration."""

    try:
        outcome = _cleanup_google_oidc_authorization_transactions_sensitive(
            connection,
            gateway,
            key_authority,
            limit=limit,
            terminal_retention_seconds=terminal_retention_seconds,
        )
    finally:
        connection = None
        gateway = None
        key_authority = None
        terminal_retention_seconds = None
    return _resolve_repository_outcome(outcome)


def _cleanup_google_oidc_authorization_transactions_sensitive(
    connection,
    gateway,
    key_authority,
    *,
    limit,
    terminal_retention_seconds,
):
    """Expire eligible prepared rows and delete only pre-existing terminals."""

    operation = None
    lookup_versions = None
    protection_versions = None
    projected_rows = None
    assessments = None
    reused_row_keys = None
    expiry_candidates = None
    terminal_candidates = None
    result = None
    try:
        operation = _cleanup_operation_state(
            limit=limit,
            terminal_retention_seconds=terminal_retention_seconds,
        )
        connection = _require_idle_writable_connection(connection)
        _require_gateway_and_authority(gateway, key_authority)
        lookup_versions, protection_versions = (
            _cleanup_authority_version_snapshot(key_authority)
        )
        connection.execute("BEGIN IMMEDIATE")
        try:
            now = _durable_google_oidc_now(gateway)
            try:
                retention_threshold = now - timedelta(
                    seconds=operation.terminal_retention_seconds
                )
            except OverflowError:
                raise _RepositoryFailure("unavailable") from None
            _attest(connection)
            projected_rows, candidate_inspection_truncated = (
                _cleanup_snapshot_rows(
                    connection,
                    operation.candidate_inspection_limit,
                )
            )
            assessments = tuple(
                _cleanup_row_assessment(
                    raw,
                    ordinal,
                    lookup_versions,
                    protection_versions,
                    now,
                )
                for ordinal, raw in enumerate(projected_rows)
            )
            reused_row_keys = _cleanup_reused_row_keys(assessments)
            expiry_candidates = []
            terminal_candidates = []
            terminal_candidates_inspected = 0
            skipped_too_recent = 0
            skipped_structurally_invalid = 0
            skipped_unsupported_version = 0
            skipped_chronology_invalid = 0

            for assessment in assessments:
                values = assessment["values"]
                codes = assessment["codes"]
                lifecycle = values["lifecycle"]
                terminal_like = (
                    lifecycle != "prepared"
                    or values["row_version"] != 1
                    or values["claimed_at"] is not None
                    or values["terminal_at"] is not None
                )
                version_unavailable = bool(
                    codes & _CLEANUP_UNSUPPORTED_VERSION_CODES
                )
                chronology_invalid = bool(
                    codes & _CLEANUP_CHRONOLOGY_CODES
                )
                copied_material = (
                    assessment["row_key"] in reused_row_keys
                )

                if not terminal_like:
                    expires_at = assessment["expires_at"]
                    if (
                        lifecycle == "prepared"
                        and assessment["structurally_valid"]
                        and not version_unavailable
                        and not copied_material
                        and expires_at is not None
                        and expires_at <= now
                    ):
                        expiry_candidates.append(
                            (
                                expires_at,
                                values["transaction_id"],
                                values["expires_at"],
                            )
                        )
                    continue

                terminal_candidates_inspected += 1
                terminal_at = assessment["terminal_at"]
                if terminal_at is not None and terminal_at > now:
                    skipped_chronology_invalid += 1
                elif chronology_invalid:
                    skipped_chronology_invalid += 1
                elif version_unavailable:
                    skipped_unsupported_version += 1
                elif (
                    not assessment["structurally_valid"]
                    or copied_material
                    or lifecycle
                    not in {"consumed", "expired", "invalidated"}
                ):
                    skipped_structurally_invalid += 1
                elif terminal_at is None:
                    skipped_chronology_invalid += 1
                elif terminal_at > retention_threshold:
                    skipped_too_recent += 1
                else:
                    terminal_candidates.append(
                        (
                            terminal_at,
                            lifecycle,
                            values["transaction_id"],
                            values["claimed_at"],
                            values["terminal_at"],
                            values["lookup_key_version"],
                            values["protection_key_version"],
                        )
                    )

            expiry_candidates.sort(key=lambda item: (item[0], item[1]))
            terminal_candidates.sort(
                key=lambda item: (item[0], item[1], item[2])
            )

            expired_count = 0
            deleted_count = 0
            if candidate_inspection_truncated:
                known_remaining = (
                    len(expiry_candidates)
                    + len(terminal_candidates)
                    + 1
                )
                remaining_exact = False
                complete = False
                _failure_boundary("cleanup.after_expiry")
            else:
                _enable_and_verify_recursive_triggers(connection)
                now_text = now.isoformat()
                for _expires, transaction_id, expires_text in (
                    expiry_candidates[: operation.limit]
                ):
                    cursor = connection.execute(
                        "UPDATE google_oidc_authorization_transactions "
                        "SET lifecycle='expired', claimed_at=NULL, "
                        "terminal_at=?, row_version=2 "
                        "WHERE transaction_id=? AND lifecycle='prepared' "
                        "AND row_version=1 AND claimed_at IS NULL "
                        "AND terminal_at IS NULL AND expires_at IS ? "
                        "AND expires_at<=?",
                        (
                            now_text,
                            transaction_id,
                            expires_text,
                            now_text,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise _RepositoryFailure("unavailable")
                    expired_count += 1
                _failure_boundary("cleanup.after_expiry")
                remaining = operation.limit - expired_count
                for (
                    _terminal_at,
                    lifecycle,
                    transaction_id,
                    claimed_text,
                    terminal_text,
                    lookup_version,
                    protection_version,
                ) in terminal_candidates[:remaining]:
                    cursor = connection.execute(
                        "DELETE FROM google_oidc_authorization_transactions "
                        "WHERE transaction_id=? AND lifecycle=? "
                        "AND row_version=2 AND claimed_at IS ? "
                        "AND terminal_at IS ? AND lookup_key_version=? "
                        "AND protection_key_version=?",
                        (
                            transaction_id,
                            lifecycle,
                            claimed_text,
                            terminal_text,
                            lookup_version,
                            protection_version,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise _RepositoryFailure("unavailable")
                    deleted_count += 1
                known_remaining = (
                    len(expiry_candidates)
                    - expired_count
                    + len(terminal_candidates)
                    - deleted_count
                )
                remaining_exact = True
                complete = known_remaining == 0

            _failure_boundary("cleanup.after_delete")
            if (
                expired_count < 0
                or deleted_count < 0
                or expired_count + deleted_count > operation.limit
            ):
                raise _RepositoryFailure("unavailable")
            result = _issue_cleanup_result(
                expired_count=expired_count,
                deleted_count=deleted_count,
                limit=operation.limit,
                terminal_retention_seconds=(
                    operation.terminal_retention_seconds
                ),
                candidate_inspection_limit=(
                    operation.candidate_inspection_limit
                ),
                terminal_candidates_inspected=(
                    terminal_candidates_inspected
                ),
                skipped_too_recent=skipped_too_recent,
                skipped_structurally_invalid=(
                    skipped_structurally_invalid
                ),
                skipped_unsupported_version=(
                    skipped_unsupported_version
                ),
                skipped_chronology_invalid=(
                    skipped_chronology_invalid
                ),
                known_remaining=known_remaining,
                remaining_exact=remaining_exact,
                candidate_inspection_truncated=(
                    candidate_inspection_truncated
                ),
                complete=complete,
                commit_outcome="committed",
            )
            connection.commit()
        except BaseException:
            _rollback_if_active(connection)
            raise
        if connection.in_transaction:
            raise _RepositoryFailure("unavailable")
        _failure_boundary("cleanup.after_commit")
        return result
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
        _detach_exception_graph(exc)
        raise
    except GoogleOidcAuthorizationTransactionRepositoryError as exc:
        reason = exc.reason_code
        _detach_exception_graph(exc)
        return _RepositoryFailureSignal(reason)
    except _RepositoryFailure as exc:
        reason = exc.reason
        _detach_exception_graph(exc)
        return _RepositoryFailureSignal(reason)
    except sqlite3.Error as exc:
        reason = _sqlite_reason(
            getattr(exc, "sqlite_errorcode", None)
        )
        _detach_exception_graph(exc)
        return _RepositoryFailureSignal(reason)
    except Exception as exc:
        _detach_exception_graph(exc)
        return _RepositoryFailureSignal("unavailable")
    finally:
        operation = None
        lookup_versions = None
        protection_versions = None
        projected_rows = None
        assessments = None
        reused_row_keys = None
        expiry_candidates = None
        terminal_candidates = None
        assessment = None
        values = None
        codes = None
        lifecycle = None
        terminal_like = None
        version_unavailable = None
        chronology_invalid = None
        copied_material = None
        expires_at = None
        terminal_at = None
        _expires = None
        _terminal_at = None
        retention_threshold = None
        now = None
        now_text = None
        claimed_text = None
        terminal_text = None
        lookup_version = None
        protection_version = None
        remaining = None
        transaction_id = None
        cursor = None
        result = None
        connection = None
        gateway = None
        key_authority = None
        terminal_retention_seconds = None


def _cleanup_operation_state(*, limit, terminal_retention_seconds):
    candidate_limit = _CLEANUP_CANDIDATE_INSPECTION_LIMIT
    if (
        type(limit) is not int
        or not 1 <= limit <= GOOGLE_OIDC_CLEANUP_CONTRACT.max_mutations
        or type(terminal_retention_seconds) is not int
        or not (
            GOOGLE_OIDC_CLEANUP_CONTRACT.min_terminal_retention_seconds
            <= terminal_retention_seconds
            <= GOOGLE_OIDC_CLEANUP_CONTRACT.max_terminal_retention_seconds
        )
        or type(candidate_limit) is not int
        or not (
            1
            <= candidate_limit
            <= GOOGLE_OIDC_CLEANUP_CONTRACT.max_candidate_inspections
        )
    ):
        raise _RepositoryFailure("unavailable")
    return _CleanupOperationState(
        limit=limit,
        terminal_retention_seconds=terminal_retention_seconds,
        candidate_inspection_limit=candidate_limit,
    )


def _cleanup_authority_version_snapshot(key_authority):
    lookup_versions = key_authority.accepted_lookup_versions
    protection_versions = key_authority.accepted_protection_versions
    for versions in (lookup_versions, protection_versions):
        if (
            type(versions) is not tuple
            or not versions
            or any(
                type(version) is not int
                or not 1 <= version <= 2_147_483_647
                for version in versions
            )
            or tuple(sorted(versions)) != versions
            or len(set(versions)) != len(versions)
        ):
            raise _RepositoryFailure("unavailable")
    return lookup_versions, protection_versions


def _cleanup_snapshot_rows(connection, maximum):
    values = []
    storage = []
    lengths = []
    for name in _RECONCILIATION_COLUMNS:
        quoted = f'"{name}"'
        cap = _RECONCILIATION_VALUE_CAPS[name]
        values.append(
            "CASE typeof({0}) "
            "WHEN 'text' THEN substr(CAST({0} AS BLOB),1,{1}) "
            "WHEN 'blob' THEN substr({0},1,{1}) "
            "ELSE {0} END".format(quoted, cap)
        )
        storage.append(f"typeof({quoted})")
        lengths.append(f"length(CAST({quoted} AS BLOB))")
    projection = ", ".join((*values, *storage, *lengths))
    cursor = connection.execute(
        f"SELECT {projection} "
        "FROM google_oidc_authorization_transactions "
        "ORDER BY transaction_id LIMIT ?",
        (maximum + 1,),
    )
    rows = []
    for _index in range(maximum + 1):
        row = cursor.fetchone()
        if row is None:
            return tuple(rows), False
        rows.append(tuple(row))
    return tuple(rows[:maximum]), True


def _cleanup_row_assessment(
    raw,
    ordinal,
    lookup_versions,
    protection_versions,
    now,
):
    values, storage, lengths, row_key = (
        _prepare_reconciliation_projected_row(raw)
    )
    collector = _CleanupValidationCollector()
    structurally_valid = _scan_reconciliation_row(
        values,
        storage,
        lengths,
        ordinal,
        row_key,
        lookup_versions,
        protection_versions,
        collector,
        now,
    )
    return {
        "values": values,
        "storage": storage,
        "lengths": lengths,
        "row_key": row_key,
        "codes": frozenset(collector.codes),
        "structurally_valid": structurally_valid,
        "expires_at": _reconciliation_parse_timestamp(
            values["expires_at"]
        ),
        "terminal_at": _reconciliation_parse_timestamp(
            values["terminal_at"]
        ),
    }


def _cleanup_reused_row_keys(assessments):
    groups = {}
    for assessment in assessments:
        values = assessment["values"]
        storage = assessment["storage"]
        lengths = assessment["lengths"]
        material = values["protected_material"]
        if (
            storage["protected_material"] != "blob"
            or type(material) is not bytes
            or type(lengths["protected_material"]) is not int
            or lengths["protected_material"] != len(material)
            or not 17 <= len(material) <= 528
        ):
            continue
        metadata = _reconciliation_reuse_metadata(
            values,
            storage,
            lengths,
            excluded=frozenset({"protected_material"}),
        )
        groups.setdefault(material, []).append(
            (assessment["row_key"], metadata)
        )
    reused = set()
    for entries in groups.values():
        if len(entries) > 1:
            reused.update(row_key for row_key, _metadata in entries)
    return frozenset(reused)


class _RepositoryFailure(Exception):
    __slots__ = ("reason",)

    def __init__(self, reason):
        self.reason = reason if reason in _ERROR_REASONS else "unavailable"


class _RepositoryFailureSignal:
    __slots__ = ("reason",)

    def __init__(self, reason):
        self.reason = reason if reason in _ERROR_REASONS else "unavailable"


def _resolve_repository_outcome(outcome):
    if type(outcome) is _RepositoryFailureSignal:
        reason = outcome.reason
        outcome = None
        raise _error(reason)
    return outcome


def _detach_exception_graph(exception):
    pending = [exception]
    visited = set()
    remaining = 64
    while pending and remaining:
        current = pending.pop()
        identity = id(current)
        if identity in visited or not isinstance(current, BaseException):
            continue
        visited.add(identity)
        remaining -= 1
        cause = current.__cause__
        context = current.__context__
        current.__traceback__ = None
        current.__cause__ = None
        current.__context__ = None
        if cause is not None:
            pending.append(cause)
        if context is not None:
            pending.append(context)


def _require_idle_writable_connection(connection):
    if (
        type(connection) is not sqlite3.Connection
        or connection.in_transaction
    ):
        raise _RepositoryFailure("unavailable")
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()
    query_only = connection.execute("PRAGMA query_only").fetchone()
    if (
        foreign_keys is None
        or foreign_keys[0] != 1
        or query_only is None
        or query_only[0] != 0
    ):
        raise _RepositoryFailure("unavailable")
    _enable_and_verify_recursive_triggers(connection)
    return connection


def _enable_and_verify_recursive_triggers(connection):
    connection.execute("PRAGMA recursive_triggers = ON")
    row = connection.execute("PRAGMA recursive_triggers").fetchone()
    if row is None or type(row[0]) is not int or row[0] != 1:
        raise _RepositoryFailure("unavailable")


def _require_gateway_and_authority(gateway, key_authority):
    if (
        type(gateway) is not GoogleOidcGateway
        or type(key_authority) is not GoogleOidcTransactionKeyAuthority
        or key_authority.closed
    ):
        raise _RepositoryFailure("unavailable")


def _attest(connection):
    attestation = attest_google_oidc_authorization_transaction_schema(
        connection
    )
    if (
        type(attestation) is not dict
        or attestation.get("state") != "correctly_installed"
        or attestation.get("blocking") is not False
        or attestation.get("migration_marker_present") is not True
    ):
        raise _RepositoryFailure("unavailable")


def _digest_for_version(digests, version):
    matches = [
        digest
        for candidate_version, digest in digests
        if candidate_version == version
    ]
    if len(matches) != 1:
        raise _RepositoryFailure("unavailable")
    return matches[0]


def _lookup_rows(connection, digests, *, limit):
    if not digests or not 1 <= len(digests) <= 3:
        raise _RepositoryFailure("unavailable")
    values_clause = ", ".join("(?, ?)" for _item in digests)
    parameters = [
        component
        for version_digest in digests
        for component in version_digest
    ]
    parameters.append(limit)
    cursor = connection.execute(
        "WITH requested(lookup_key_version, state_lookup_digest) AS "
        f"(VALUES {values_clause}) "
        f"SELECT {_SELECT_PROJECTION} "
        "FROM google_oidc_authorization_transactions AS transaction_record "
        "JOIN requested USING (lookup_key_version, state_lookup_digest) "
        "ORDER BY transaction_record.transaction_id LIMIT ?",
        tuple(parameters),
    )
    return cursor.fetchall()


def _row_dict(row):
    values = tuple(row)
    if len(values) != len(_SELECT_COLUMNS):
        raise _RepositoryFailure("unavailable")
    return dict(zip(_SELECT_COLUMNS, values))


def _associated_data_for_row(row, created_at, expires_at):
    return _canonical_associated_data(
        transaction_id=row["transaction_id"],
        record_version=row["record_version"],
        provider=row["provider"],
        environment_namespace=row["environment_namespace"],
        configuration_fingerprint=row["configuration_fingerprint"],
        state_digest_version=row["state_digest_version"],
        lookup_key_version=row["lookup_key_version"],
        state_lookup_digest=row["state_lookup_digest"],
        created_at=created_at,
        expires_at=expires_at,
        protection_envelope_version=row["protection_envelope_version"],
        protection_key_version=row["protection_key_version"],
    )


def _invalidate_candidates(connection, candidates, now):
    for candidate in candidates:
        row = _row_dict(candidate)
        if row["lifecycle"] != "prepared" or row["row_version"] != 1:
            continue
        try:
            created_at = _parse_canonical_time_text(row["created_at"])
        except TypeError:
            raise _RepositoryFailure("unavailable") from None
        terminal_at = max(now, created_at).isoformat()
        connection.execute(
            "UPDATE google_oidc_authorization_transactions "
            "SET lifecycle='invalidated', claimed_at=NULL, terminal_at=?, "
            "row_version=2 WHERE transaction_id=? AND lifecycle='prepared' "
            "AND row_version=1",
            (terminal_at, row["transaction_id"]),
        )


def _rollback_if_active(connection):
    try:
        if connection.in_transaction:
            connection.rollback()
    except BaseException:
        raise _RepositoryFailure("unavailable") from None


def _clear_buffer(value):
    if type(value) is bytearray:
        value[:] = b"\x00" * len(value)
        value.clear()


def _sqlite_reason(error_code):
    if type(error_code) is int and (error_code & 0xFF) in _BUSY_CODES:
        return "temporary_contention"
    return "unavailable"


def _error(reason):
    return GoogleOidcAuthorizationTransactionRepositoryError(reason)


def _failure_boundary(_name):
    """Private no-op seam used only by crash-boundary tests."""


__all__ = (
    "GoogleOidcAuthorizationTransactionRepositoryError",
    "claim_google_oidc_authorization_transaction",
    "cleanup_google_oidc_authorization_transactions",
    "prepare_google_oidc_authorization_transaction",
)

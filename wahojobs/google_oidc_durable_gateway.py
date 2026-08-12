"""Dormant composition of durable state with the fixed Google OIDC gateway.

The functions in this module add no route, database opener, key loader, or
runtime activation.  Callers supply the exact migrated SQLite connection,
configured gateway, key authority, and accepted B2D1/B2C4 boundaries.
"""

from __future__ import annotations

from hmac import compare_digest as _constant_time_equal
import sqlite3

from wahojobs.google_oidc_authorization_transaction_repository import (
    GoogleOidcAuthorizationTransactionRepositoryError,
    claim_google_oidc_authorization_transaction,
    prepare_google_oidc_authorization_transaction,
)
from wahojobs.google_oidc_authorization_transactions import (
    _clear_buffer,
    _clear_claimed_material_values,
    _take_claimed_material,
)
from wahojobs.google_oidc_gateway import (
    GoogleOidcGateway,
    _complete_durable_google_oidc_claimed,
    _failure,
    _poison_gateway_for_control,
    _validated_durable_google_oidc_callback,
)
from wahojobs.google_oidc_transaction_protection import (
    GoogleOidcTransactionKeyAuthority,
)

_TRANSACTION_ID_PREFIX = b"oidctx_"
_TRANSACTION_ID_BYTES = 39
_LOWER_HEXADECIMAL = frozenset(b"0123456789abcdef")


def prepare_durable_google_oidc_authorization(
    connection,
    gateway,
    key_authority,
    *,
    invitation_credential=None,
):
    """Commit one protected transaction before exposing its authorization URL."""

    try:
        _require_concrete_boundary(connection, gateway, key_authority)
        return prepare_google_oidc_authorization_transaction(
            connection,
            gateway,
            key_authority,
            invitation_credential=invitation_credential,
        )
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as control:
        _poison_gateway_for_control(gateway, control)
        _detach_exception(control)
        raise control from None
    except GoogleOidcAuthorizationTransactionRepositoryError as exc:
        _detach_exception(exc)
        return _failure("unavailable")
    except Exception as exc:
        _detach_exception(exc)
        return _failure("unavailable")
    finally:
        _clear_buffer(invitation_credential)
        connection = None
        gateway = None
        key_authority = None
        invitation_credential = None


def complete_durable_google_oidc_authorization(
    connection,
    gateway,
    key_authority,
    callback_url,
    completion_policy,
    request_secret_vault,
):
    """Terminally claim before invoking the existing real provider/B2D1 path."""

    callback_state = None
    authoritative_callback_url = None
    capsule = None
    values = None
    try:
        _require_concrete_boundary(connection, gateway, key_authority)
        try:
            callback_state, authoritative_callback_url = (
                _validated_durable_google_oidc_callback(
                    gateway,
                    callback_url,
                )
            )
            callback_url = None
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except Exception as exc:
            _detach_exception(exc)
            return _failure("invalid_or_expired_transaction")
        try:
            capsule = claim_google_oidc_authorization_transaction(
                connection,
                gateway,
                key_authority,
                callback_state,
            )
        except GoogleOidcAuthorizationTransactionRepositoryError as exc:
            reason = exc.reason_code
            _detach_exception(exc)
            if reason == "invalid_or_expired_transaction":
                return _failure("invalid_or_expired_transaction")
            return _failure("unavailable")
        values = _take_claimed_material(capsule)
        capsule.close()
        capsule = None
        return _complete_claimed_authorization(
            gateway,
            connection,
            authoritative_callback_url,
            completion_policy,
            request_secret_vault,
            state=values["state"],
            nonce=values["nonce"],
            pkce_verifier=values["pkce_verifier"],
            b2d1_request_key=values["b2d1_request_key"],
            invitation_credential=values["invitation_credential"],
            created_at=values["created_at"],
            expires_at=values["expires_at"],
            claimed_at=values["claimed_at"],
        )
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as control:
        _poison_gateway_for_control(gateway, control)
        _detach_exception(control)
        raise control from None
    except Exception as exc:
        _detach_exception(exc)
        return _failure("unavailable")
    finally:
        if capsule is not None:
            try:
                capsule.close()
            except Exception as exc:
                _detach_exception(exc)
        _clear_claimed_material_values(values)
        connection = None
        gateway = None
        key_authority = None
        callback_url = None
        authoritative_callback_url = None
        completion_policy = None
        request_secret_vault = None
        callback_state = None
        capsule = None
        values = None


def complete_browser_bound_durable_google_oidc_authorization(
    connection,
    gateway,
    key_authority,
    callback_url,
    browser_transaction_id,
    completion_policy,
    request_secret_vault,
):
    """Terminally claim before accepting one browser transaction binding."""

    callback_state = None
    authoritative_callback_url = None
    capsule = None
    values = None
    try:
        _require_concrete_boundary(connection, gateway, key_authority)
        try:
            callback_state, authoritative_callback_url = (
                _validated_durable_google_oidc_callback(
                    gateway,
                    callback_url,
                )
            )
            callback_url = None
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except Exception as exc:
            _detach_exception(exc)
            return _failure("invalid_or_expired_transaction")
        try:
            capsule = claim_google_oidc_authorization_transaction(
                connection,
                gateway,
                key_authority,
                callback_state,
            )
        except GoogleOidcAuthorizationTransactionRepositoryError as exc:
            reason = exc.reason_code
            _detach_exception(exc)
            if reason == "invalid_or_expired_transaction":
                return _failure("invalid_or_expired_transaction")
            return _failure("unavailable")
        values = _take_claimed_material(capsule)
        capsule.close()
        capsule = None
        if not _browser_transaction_binding_matches(
            values["transaction_id"],
            browser_transaction_id,
        ):
            return _failure("invalid_or_expired_transaction")
        return _complete_claimed_authorization(
            gateway,
            connection,
            authoritative_callback_url,
            completion_policy,
            request_secret_vault,
            state=values["state"],
            nonce=values["nonce"],
            pkce_verifier=values["pkce_verifier"],
            b2d1_request_key=values["b2d1_request_key"],
            invitation_credential=values["invitation_credential"],
            created_at=values["created_at"],
            expires_at=values["expires_at"],
            claimed_at=values["claimed_at"],
        )
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as control:
        _poison_gateway_for_control(gateway, control)
        _detach_exception(control)
        raise control from None
    except Exception as exc:
        _detach_exception(exc)
        return _failure("unavailable")
    finally:
        if capsule is not None:
            try:
                capsule.close()
            except Exception as exc:
                _detach_exception(exc)
        _clear_claimed_material_values(values)
        connection = None
        gateway = None
        key_authority = None
        callback_url = None
        authoritative_callback_url = None
        browser_transaction_id = None
        completion_policy = None
        request_secret_vault = None
        callback_state = None
        capsule = None
        values = None


def _complete_claimed_authorization(
    gateway,
    connection,
    callback_url,
    completion_policy,
    request_secret_vault,
    *,
    state,
    nonce,
    pkce_verifier,
    b2d1_request_key,
    invitation_credential,
    created_at,
    expires_at,
    claimed_at,
):
    """Complete through the fixed server-private claimed-material boundary."""

    try:
        return _complete_durable_google_oidc_claimed(
            gateway,
            connection,
            callback_url,
            completion_policy,
            request_secret_vault,
            state=state,
            nonce=nonce,
            pkce_verifier=pkce_verifier,
            b2d1_request_key=b2d1_request_key,
            created_at=created_at,
            expires_at=expires_at,
            claimed_at=claimed_at,
            invitation_credential=invitation_credential,
        )
    finally:
        _clear_buffer(invitation_credential)
        invitation_credential = None


def _browser_transaction_binding_matches(
    claimed_transaction_id,
    browser_transaction_id,
):
    claimed, claimed_valid = _fixed_transaction_id_candidate(
        claimed_transaction_id
    )
    supplied, supplied_valid = _fixed_transaction_id_candidate(
        browser_transaction_id
    )
    matched = _constant_time_equal(claimed, supplied)
    return bool(matched and claimed_valid and supplied_valid)


def _fixed_transaction_id_candidate(value):
    candidate = b"\x00" * _TRANSACTION_ID_BYTES
    valid = False
    if type(value) is str:
        try:
            encoded = value.encode("ascii", "strict")
        except UnicodeError:
            encoded = b""
        if len(encoded) == _TRANSACTION_ID_BYTES:
            candidate = encoded
            valid = (
                encoded.startswith(_TRANSACTION_ID_PREFIX)
                and all(
                    byte in _LOWER_HEXADECIMAL
                    for byte in encoded[len(_TRANSACTION_ID_PREFIX) :]
                )
            )
    return candidate, valid


def _require_concrete_boundary(connection, gateway, key_authority):
    if (
        type(connection) is not sqlite3.Connection
        or type(gateway) is not GoogleOidcGateway
        or type(key_authority) is not GoogleOidcTransactionKeyAuthority
    ):
        raise TypeError("durable_google_oidc_boundary_invalid")


def _detach_exception(exc):
    try:
        exc.__traceback__ = None
        exc.__cause__ = None
        exc.__context__ = None
    except Exception:
        pass


__all__ = (
    "complete_browser_bound_durable_google_oidc_authorization",
    "complete_durable_google_oidc_authorization",
    "prepare_durable_google_oidc_authorization",
)

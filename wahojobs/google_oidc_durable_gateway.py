"""Dormant composition of durable state with the fixed Google OIDC gateway.

The functions in this module add no route, database opener, key loader, or
runtime activation.  Callers supply the exact migrated SQLite connection,
configured gateway, key authority, and accepted B2D1/B2C4 boundaries.
"""

from __future__ import annotations

import sqlite3

from wahojobs.google_oidc_authorization_transaction_repository import (
    GoogleOidcAuthorizationTransactionRepositoryError,
    claim_google_oidc_authorization_transaction,
    prepare_google_oidc_authorization_transaction,
)
from wahojobs.google_oidc_authorization_transactions import (
    _clear_claimed_material_values,
    _take_claimed_material,
)
from wahojobs.google_oidc_gateway import (
    GoogleOidcGateway,
    _complete_durable_google_oidc_claimed,
    _durable_google_oidc_callback_state,
    _failure,
    _poison_gateway_for_control,
)
from wahojobs.google_oidc_transaction_protection import (
    GoogleOidcTransactionKeyAuthority,
)


def prepare_durable_google_oidc_authorization(
    connection,
    gateway,
    key_authority,
):
    """Commit one protected transaction before exposing its authorization URL."""

    try:
        _require_concrete_boundary(connection, gateway, key_authority)
        return prepare_google_oidc_authorization_transaction(
            connection,
            gateway,
            key_authority,
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
        connection = None
        gateway = None
        key_authority = None


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
    capsule = None
    values = None
    try:
        _require_concrete_boundary(connection, gateway, key_authority)
        try:
            callback_state = _durable_google_oidc_callback_state(
                gateway,
                callback_url,
            )
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
        return _complete_durable_google_oidc_claimed(
            gateway,
            connection,
            callback_url,
            completion_policy,
            request_secret_vault,
            state=values["state"],
            nonce=values["nonce"],
            pkce_verifier=values["pkce_verifier"],
            b2d1_request_key=values["b2d1_request_key"],
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
        completion_policy = None
        request_secret_vault = None
        callback_state = None
        capsule = None
        values = None


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
    "complete_durable_google_oidc_authorization",
    "prepare_durable_google_oidc_authorization",
)

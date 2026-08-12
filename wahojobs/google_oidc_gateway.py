"""Dormant, sealed Google authorization-code and OIDC login gateway.

Construction fixes the reviewed Google policy and always installs the real
Authlib/joserfc adapter.  The module has no runtime activation, route, or
test-only verification authority.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import math
import re
import secrets
import sqlite3
import threading
import time
import weakref
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, unquote_to_bytes, urlencode, urlsplit

from wahojobs.account_reconciliation import (
    attest_account_schema,
    authoritative_account_row_valid,
    authoritative_auth_identity_row_valid,
)
from wahojobs.accounts import (
    AccountService,
    AuthenticationUnavailable,
    InvalidAccountInput,
    TrustedIdentityVerifier,
)
from wahojobs.google_oidc_authorization_transactions import (
    MAX_DURABLE_CONFIGURATION_CONTEXT_BYTES,
)
from wahojobs.ownership import (
    AccountNativePrincipalBootstrapResult,
    ensure_account_native_principal,
)
import wahojobs.trusted_login_completion as _trusted_login
from wahojobs.trusted_login_completion import (
    TrustedExternalIdentityAuthentication,
    complete_trusted_login,
)


_PROVIDER = "google"
_CANONICAL_ISSUER = "https://accounts.google.com"
_ACCEPTED_ISSUERS = (_CANONICAL_ISSUER, "accounts.google.com")
_AUTHORIZATION_ENDPOINT = _CANONICAL_ISSUER + "/o/oauth2/v2/auth"
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_JWKS_ENDPOINT = "https://www.googleapis.com/oauth2/v3/certs"
_SCOPES = ("openid", "email")
_ALGORITHMS = ("RS256",)
_ASSURANCE_POLICY_VERSION = "google_oidc_v1"
_CLOCK_SKEW_SECONDS = 60
_MAX_AUTHENTICATION_AGE_SECONDS = 86_400
_CONNECT_TIMEOUT_SECONDS = 3
_READ_TIMEOUT_SECONDS = 5
_TOKEN_RESPONSE_LIMIT = 64 * 1024
_JWKS_RESPONSE_LIMIT = 256 * 1024
_JWKS_MAX_TTL_SECONDS = 6 * 60 * 60
_JWKS_FALLBACK_TTL_SECONDS = 5 * 60
_JWKS_UNKNOWN_KID_REFRESH_SECONDS = 60
_JWKS_MAX_KEYS = 32
_TRANSACTION_TTL = timedelta(minutes=10)
_ENVIRONMENTS = frozenset({"development", "test", "private_beta"})
_CALLBACK_URL_LIMIT = 8192
_CALLBACK_PARAMETER_LIMIT = 9
_CALLBACK_PARAMETER_NAME_LIMIT = 64
_CALLBACK_PARAMETER_VALUE_LIMIT = 4096
_SUPPORTED_RESPONSE_CONTENT_ENCODINGS = frozenset(
    {"identity", "gzip", "deflate"}
)
_SUBJECT_LIMIT = 1024
_METADATA_VERSION = "google_oidc_v1"
_INVITATION_LOOKUP_KEY_MIN_BYTES = 32
_INVITATION_LOOKUP_KEY_MAX_BYTES = 512

_FAILURE_STATUSES = frozenset(
    {
        "authentication_denied",
        "provider_unavailable",
        "invalid_or_expired_transaction",
        "unavailable",
    }
)
_FAILURE_HASHES = {
    "authentication_denied": 0x32E7A11,
    "provider_unavailable": 0x4A1F9C2,
    "invalid_or_expired_transaction": 0x612B8D3,
    "unavailable": 0x7D903E4,
}
_SUCCESS_CALLBACK_NAMES = frozenset({"code", "iss", "state"})
_ERROR_CALLBACK_REQUIRED_NAMES = frozenset({"error", "iss", "state"})
_ERROR_CALLBACK_OPTIONAL_NAMES = frozenset(
    {"error_description", "error_uri"}
)
_ERROR_CALLBACK_NAMES = (
    _ERROR_CALLBACK_REQUIRED_NAMES | _ERROR_CALLBACK_OPTIONAL_NAMES
)
_AUTHORITATIVE_CALLBACK_NAMES = (
    _SUCCESS_CALLBACK_NAMES | _ERROR_CALLBACK_NAMES
)
_OAUTH_INFRASTRUCTURE_ERRORS = frozenset(
    {"server_error", "temporarily_unavailable"}
)
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9a-fA-F]{2})")
_HTTP_TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_DURABLE_STATE = re.compile(r"^[A-Za-z0-9_-]{43}$")

_PREPARATION_ISSUANCE_CAPABILITY = object()
_FAILURE_ISSUANCE_CAPABILITY = object()

class _AuthenticationDenied(Exception):
    pass


class _CallbackQueryInvalid(_AuthenticationDenied):
    pass


class _ResponseIssuerMissing(_AuthenticationDenied):
    pass


class _ResponseIssuerMismatch(_AuthenticationDenied):
    pass


class _DurableIdentityMissing(_AuthenticationDenied):
    pass


class _ProviderUnavailable(Exception):
    pass


class _InvalidTransaction(Exception):
    pass


class _Unavailable(Exception):
    pass


class _ProviderResponseTooLarge(Exception):
    pass


class _ProviderResponseInvalid(Exception):
    pass


class _GoogleClientCredential:
    __slots__ = ("_record", "__weakref__")

    def __new__(cls, *_args, **_kwargs):
        raise TypeError("google_oidc_credential_required")

    def __setattr__(self, _name, _value):
        raise AttributeError("google_oidc_credential_is_immutable")

    def __repr__(self):
        return "_GoogleClientCredential(<redacted>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("google_oidc_credential_not_serializable")

    def __copy__(self):
        raise TypeError("google_oidc_credential_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("google_oidc_credential_not_copyable")

    def __init_subclass__(cls, **_kwargs):
        raise TypeError("google_oidc_credential_not_subclassable")


class TrustedGoogleOidcConfiguration:
    """Opaque, gateway-owned view of the fixed Google configuration."""

    __slots__ = ("__record", "__weakref__")

    def __new__(cls, *_args, **_kwargs):
        raise TypeError("trusted_google_oidc_configuration_required")

    @property
    def _record(self):
        return object.__getattribute__(
            self,
            "_TrustedGoogleOidcConfiguration__record",
        )

    def __setattr__(self, _name, _value):
        raise AttributeError("trusted_google_oidc_configuration_is_immutable")

    def __repr__(self):
        return "TrustedGoogleOidcConfiguration(<redacted>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("trusted_google_oidc_configuration_not_serializable")

    def __copy__(self):
        raise TypeError("trusted_google_oidc_configuration_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("trusted_google_oidc_configuration_not_copyable")

    def __init_subclass__(cls, **_kwargs):
        raise TypeError("trusted_google_oidc_configuration_not_subclassable")


class GoogleOidcAuthorizationTransaction:
    """Opaque, gateway-owned one-use authorization transaction."""

    __slots__ = ("_gateway_reference", "__weakref__")

    def __new__(cls, *_args, **_kwargs):
        raise TypeError("google_oidc_authorization_transaction_required")

    @property
    def status(self):
        gateway = self._gateway_reference()
        if type(gateway) is not GoogleOidcGateway:
            return "consumed"
        try:
            gateway_record = object.__getattribute__(gateway, "_record")
        except AttributeError:
            return "consumed"
        if type(gateway_record) is not _GatewayRecord:
            return "consumed"
        with gateway_record.lock:
            transaction = gateway_record.transactions.get(self)
            if transaction is None:
                return "consumed"
            with transaction.lock:
                return transaction.lifecycle

    @property
    def created_at(self):
        return _transaction_public_time(self, "created_at")

    @property
    def expires_at(self):
        return _transaction_public_time(self, "expires_at")

    def __setattr__(self, _name, _value):
        raise AttributeError("google_oidc_authorization_transaction_is_immutable")

    def __repr__(self):
        return "GoogleOidcAuthorizationTransaction(<redacted>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("google_oidc_authorization_transaction_not_serializable")

    def __copy__(self):
        raise TypeError("google_oidc_authorization_transaction_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("google_oidc_authorization_transaction_not_copyable")

    def __init_subclass__(cls, **_kwargs):
        raise TypeError("google_oidc_authorization_transaction_not_subclassable")


class PreparedGoogleOidcAuthorization:
    """Redacted preparation holding one authorization URL and transaction."""

    __slots__ = ("_record", "__weakref__")

    def __new__(cls, *_args, **_kwargs):
        raise TypeError("prepared_google_oidc_authorization_required")

    @classmethod
    def _issue(cls, capability, transaction, url_buffer):
        if (
            cls is not PreparedGoogleOidcAuthorization
            or capability is not _PREPARATION_ISSUANCE_CAPABILITY
            or type(transaction) is not GoogleOidcAuthorizationTransaction
            or type(url_buffer) is not bytearray
            or not url_buffer
        ):
            raise TypeError("prepared_google_oidc_authorization_invalid")
        instance = object.__new__(cls)
        object.__setattr__(
            instance,
            "_record",
            _PreparationRecord(
                transaction=transaction,
                url_buffer=url_buffer,
            ),
        )
        return instance

    @property
    def transaction(self):
        try:
            record = object.__getattribute__(self, "_record")
        except AttributeError:
            raise TypeError(
                "prepared_google_oidc_authorization_invalid"
            ) from None
        if type(record) is not _PreparationRecord:
            raise TypeError("prepared_google_oidc_authorization_invalid")
        return record.transaction

    @property
    def authorization_url(self):
        try:
            record = object.__getattribute__(self, "_record")
        except AttributeError:
            raise TypeError(
                "prepared_google_oidc_authorization_invalid"
            ) from None
        if (
            type(record) is not _PreparationRecord
            or not record.url_buffer
        ):
            raise TypeError("prepared_google_oidc_authorization_expired")
        return bytes(record.url_buffer).decode("ascii")

    def __setattr__(self, _name, _value):
        raise AttributeError("prepared_google_oidc_authorization_is_immutable")

    def __repr__(self):
        return "PreparedGoogleOidcAuthorization(<redacted>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("prepared_google_oidc_authorization_not_serializable")

    def __copy__(self):
        raise TypeError("prepared_google_oidc_authorization_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("prepared_google_oidc_authorization_not_copyable")

    def __init_subclass__(cls, **_kwargs):
        raise TypeError("prepared_google_oidc_authorization_not_subclassable")


class GoogleOidcGatewayFailure:
    """Bounded pre-B2D1 failure with no provider or durable identity details."""

    __slots__ = ("_status", "_seal", "__weakref__")

    def __new__(cls, *_args, **_kwargs):
        raise TypeError("google_oidc_gateway_failure_not_constructible")

    @classmethod
    def _issue(cls, capability, status):
        if (
            cls is not GoogleOidcGatewayFailure
            or capability is not _FAILURE_ISSUANCE_CAPABILITY
            or status not in _FAILURE_STATUSES
        ):
            raise TypeError("google_oidc_gateway_failure_not_constructible")
        instance = object.__new__(cls)
        object.__setattr__(instance, "_status", status)
        object.__setattr__(
            instance,
            "_seal",
            _FAILURE_ISSUANCE_CAPABILITY,
        )
        return instance

    @property
    def status(self):
        status = self._status
        if (
            self._seal is not _FAILURE_ISSUANCE_CAPABILITY
            or type(status) is not str
            or status not in _FAILURE_STATUSES
        ):
            raise TypeError("google_oidc_gateway_failure_invalid")
        return status

    def as_dict(self):
        return {"status": self.status}

    def __eq__(self, other):
        if type(other) is not GoogleOidcGatewayFailure:
            return False
        try:
            return self.status == other.status
        except (AttributeError, TypeError):
            return False

    def __hash__(self):
        return _FAILURE_HASHES[self.status]

    def __setattr__(self, _name, _value):
        raise AttributeError("google_oidc_gateway_failure_is_immutable")

    def __repr__(self):
        return f"GoogleOidcGatewayFailure(status={self.status!r})"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("google_oidc_gateway_failure_not_serializable")

    def __copy__(self):
        raise TypeError("google_oidc_gateway_failure_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("google_oidc_gateway_failure_not_copyable")

    def __init_subclass__(cls, **_kwargs):
        raise TypeError("google_oidc_gateway_failure_not_subclassable")


class GoogleOidcGateway:
    """Dormant, real-adapter-only Google OIDC gateway."""

    __slots__ = ("_record", "__weakref__")

    def __init__(
        self,
        *,
        client_id,
        client_secret,
        redirect_uri,
        environment_namespace,
    ):
        if type(self) is not GoogleOidcGateway:
            raise TypeError("google_oidc_gateway_not_subclassable")
        client_id = _validated_client_id(client_id)
        redirect_uri = _validated_redirect_uri(redirect_uri)
        environment = _validated_environment(environment_namespace)
        if type(client_secret) is not bytearray:
            raise TypeError("google_oidc_secret_buffer_required")
        secret_copy = bytearray(client_secret)
        _clear_buffer(client_secret)
        if not (16 <= len(secret_copy) <= 512):
            _clear_buffer(secret_copy)
            raise TypeError("google_oidc_secret_invalid")
        try:
            authority = object()
            credential = object.__new__(_GoogleClientCredential)
            digest = hashlib.sha256(
                b"wahojobs-google-oidc-credential-v1\x00"
                + bytes(secret_copy)
            ).digest()
            object.__setattr__(
                credential,
                "_record",
                _CredentialRecord(
                    secret_buffer=secret_copy,
                    digest=digest,
                    configuration_authority=authority,
                ),
            )
            config_record = _ConfigurationRecord(
                client_id=client_id,
                redirect_uri=redirect_uri,
                credential=credential,
                environment=environment,
                authority=authority,
            )
            configuration = object.__new__(TrustedGoogleOidcConfiguration)
            object.__setattr__(
                configuration,
                "_TrustedGoogleOidcConfiguration__record",
                config_record,
            )
            cache = _GoogleOidcJwksCache(config_record)
            adapter = _RealGoogleOidcAdapter(config_record, cache)
            identity_verifier = TrustedIdentityVerifier()
            object.__setattr__(
                self,
                "_record",
                _GatewayRecord(
                    configuration=configuration,
                    configuration_record=config_record,
                    identity_verifier=identity_verifier,
                    account_service=AccountService(identity_verifier),
                    invitation_lookup_key=None,
                    provider_adapter=adapter,
                    cache=cache,
                ),
            )
        except BaseException:
            _clear_buffer(secret_copy)
            raise

    def prepare_authorization(self):
        gateway_object = self
        result = _prepare_authorization_guarded(gateway_object)
        if type(result) is _ControlFlowSignal:
            control = result.control
            _poison_gateway_for_control(gateway_object, control)
            self = None
            gateway_object = None
            result = None
            _detach_exception(control)
            raise control from None
        gateway_object = None
        return result

    def complete_authorization(
        self,
        connection,
        transaction,
        callback_url,
        completion_policy,
        request_secret_vault,
    ):
        gateway_object = self
        result = _complete_authorization_guarded(
            gateway_object,
            connection,
            transaction,
            callback_url,
            completion_policy,
            request_secret_vault,
        )
        if type(result) is _ControlFlowSignal:
            control = result.control
            _poison_gateway_for_control(gateway_object, control)
            self = None
            gateway_object = None
            connection = None
            transaction = None
            callback_url = None
            completion_policy = None
            request_secret_vault = None
            result = None
            _detach_exception(control)
            raise control from None
        gateway_object = None
        connection = None
        transaction = None
        callback_url = None
        completion_policy = None
        request_secret_vault = None
        return result

    def close(self):
        try:
            record = object.__getattribute__(self, "_record")
        except AttributeError:
            return
        if type(record) is not _GatewayRecord:
            return
        _close_gateway_record(record)

    def __setattr__(self, _name, _value):
        raise AttributeError("google_oidc_gateway_is_immutable")

    def __repr__(self):
        return "GoogleOidcGateway(<configured>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("google_oidc_gateway_not_serializable")

    def __copy__(self):
        raise TypeError("google_oidc_gateway_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("google_oidc_gateway_not_copyable")

    def __init_subclass__(cls, **_kwargs):
        raise TypeError("google_oidc_gateway_not_subclassable")


def _configure_invitation_provisioning(gateway, invitation_lookup_key):
    """Install one server-private M002 invitation authority before use."""

    key_copy = None
    try:
        if type(invitation_lookup_key) is not bytearray:
            raise TypeError("google_oidc_invitation_key_buffer_required")
        key_copy = bytearray(invitation_lookup_key)
        _clear_buffer(invitation_lookup_key)
        if not (
            _INVITATION_LOOKUP_KEY_MIN_BYTES
            <= len(key_copy)
            <= _INVITATION_LOOKUP_KEY_MAX_BYTES
        ):
            raise TypeError("google_oidc_invitation_key_invalid")
        record = _gateway_record(gateway)
        with record.lock:
            if (
                record.closed
                or record.invitation_lookup_key is not None
                or record.invitation_lookup_key_digest is not None
            ):
                raise TypeError("google_oidc_gateway_unavailable")
            record.invitation_lookup_key = key_copy
            record.invitation_lookup_key_digest = hashlib.sha256(
                bytes(key_copy)
            ).digest()
            record.attestation = _gateway_attestation(record)
            key_copy = None
        return gateway
    finally:
        _clear_buffer(invitation_lookup_key)
        _clear_buffer(key_copy)
        invitation_lookup_key = None
        key_copy = None


def _configure_account_native_bootstrap(gateway, bootstrap_authority):
    """Install the accepted server-private ownership bootstrap authority."""

    if bootstrap_authority is not ensure_account_native_principal:
        raise TypeError("google_oidc_ownership_authority_invalid")
    record = _gateway_record(gateway)
    with record.lock:
        if record.closed:
            raise TypeError("google_oidc_gateway_unavailable")
        if record.account_native_bootstrap is None:
            record.account_native_bootstrap = bootstrap_authority
            record.attestation = _gateway_attestation(record)
        elif record.account_native_bootstrap is not bootstrap_authority:
            raise TypeError("google_oidc_gateway_unavailable")
    return gateway


def _prepare_authorization_guarded(gateway_object):
    try:
        return _prepare_authorization_impl(gateway_object)
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as control:
        _detach_exception(control)
        return _ControlFlowSignal(control)
    finally:
        gateway_object = None


def _prepare_authorization_impl(gateway_object):
    gateway_record = None
    transaction = None
    transaction_record = None
    authorization_url = None
    url_buffer = None
    retained = False
    try:
        gateway_record = _gateway_record(gateway_object)
        now = _clock_now(gateway_record.configuration_record)
        transaction = _new_transaction(gateway_object, gateway_record, now)
        transaction_record = gateway_record.transactions[transaction]
        authorization_url = _prepare_authorization_url(
            gateway_record.configuration_record,
            transaction_record,
        )
        url_buffer = bytearray(authorization_url.encode("ascii"))
        with transaction_record.lock:
            if transaction_record.lifecycle != "fresh":
                raise _Unavailable()
            transaction_record.authorization_url_buffer = url_buffer
        prepared = PreparedGoogleOidcAuthorization._issue(
            _PREPARATION_ISSUANCE_CAPABILITY,
            transaction,
            url_buffer,
        )
        retained = True
        return prepared
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except Exception as exc:
        _detach_exception(exc)
        return _failure("unavailable")
    finally:
        if not retained and transaction_record is not None:
            _terminalize_transaction(transaction_record)
            if gateway_record is not None and transaction is not None:
                with gateway_record.lock:
                    gateway_record.transactions.pop(transaction, None)
        if not retained:
            _clear_buffer(url_buffer)
        gateway_object = None
        gateway_record = None
        transaction = None
        transaction_record = None
        authorization_url = None
        url_buffer = None


def _complete_authorization_guarded(
    gateway_object,
    connection,
    transaction,
    callback_url,
    completion_policy,
    request_secret_vault,
):
    try:
        return _complete_authorization_impl(
            gateway_object,
            connection,
            transaction,
            callback_url,
            completion_policy,
            request_secret_vault,
        )
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as control:
        _detach_exception(control)
        return _ControlFlowSignal(control)
    finally:
        gateway_object = None
        connection = None
        transaction = None
        callback_url = None
        completion_policy = None
        request_secret_vault = None


def _complete_authorization_impl(
    gateway_object,
    connection,
    transaction,
    callback_url,
    completion_policy,
    request_secret_vault,
):
    gateway_record = None
    transaction_record = None
    claim_attempt = object()
    projection = None
    verified_identity = None
    resolved = None
    capsule = None
    capsule_values = None
    proof = None
    try:
        try:
            gateway_record = _gateway_record(gateway_object)
            transaction_record = _claim_transaction(
                gateway_object,
                gateway_record,
                transaction,
                _clock_now(gateway_record.configuration_record),
                claim_attempt,
            )
        except _InvalidTransaction as exc:
            _detach_exception(exc)
            return _failure("invalid_or_expired_transaction")
        except Exception as exc:
            _consume_claim_attempt_if_owned(
                gateway_object,
                gateway_record,
                transaction,
                claim_attempt,
            )
            _consume_fresh_transaction_if_owned(
                gateway_object,
                gateway_record,
                transaction,
            )
            _detach_exception(exc)
            return _failure("unavailable")

        try:
            projection = _verify_provider(
                gateway_record,
                callback_url,
                transaction_record,
            )
        except _InvalidTransaction as exc:
            _detach_exception(exc)
            return _failure("invalid_or_expired_transaction")
        except _AuthenticationDenied as exc:
            _detach_exception(exc)
            return _failure("authentication_denied")
        except _ProviderUnavailable as exc:
            _detach_exception(exc)
            if gateway_record.closed:
                return _failure("invalid_or_expired_transaction")
            return _failure("provider_unavailable")
        except Exception as exc:
            _detach_exception(exc)
            if gateway_record.closed:
                return _failure("invalid_or_expired_transaction")
            return _failure("unavailable")

        try:
            now = _clock_now(gateway_record.configuration_record)
            _require_claimed_transaction_fresh(
                transaction_record,
                gateway_record,
                now,
                claim_attempt,
            )
            verified_identity = (
                gateway_record.identity_verifier.from_validated_google_claims(
                    provider_subject=projection[0],
                    verified_email=None,
                    email_verified=False,
                    authenticated_at=projection[1],
                    metadata_version=_METADATA_VERSION,
                )
            )
            if not gateway_record.identity_verifier._accepts(verified_identity):
                raise _Unavailable()
            resolved = _resolve_durable_identity(
                connection,
                verified_identity,
                now,
            )
            capsule = _commit_claimed_delegation(
                gateway_object,
                gateway_record,
                transaction,
                transaction_record,
                claim_attempt,
                resolved,
                projection,
            )
            capsule_values = capsule.take(claim_attempt)
            capsule = None
            (
                account_id,
                identity_id,
                authenticated_at,
                proof_expiry,
                environment,
                request_key,
                trusted_now,
            ) = capsule_values
            capsule_values = None
            gateway_object = None
            gateway_record = None
            transaction = None
            transaction_record = None
            callback_url = None
            claim_attempt = None
            projection = None
            verified_identity = None
            resolved = None
            proof = TrustedExternalIdentityAuthentication._issue(
                _trusted_login._ASSERTION_ISSUANCE_CAPABILITY,
                account_id=account_id,
                identity_id=identity_id,
                provider=_PROVIDER,
                authenticated_at=authenticated_at,
                expires_at=proof_expiry,
                assurance_policy_version=_ASSURANCE_POLICY_VERSION,
                environment_namespace=environment,
            )
        except _InvalidTransaction as exc:
            _detach_exception(exc)
            return _failure("invalid_or_expired_transaction")
        except _AuthenticationDenied as exc:
            _detach_exception(exc)
            return _failure("authentication_denied")
        except _Unavailable as exc:
            _detach_exception(exc)
            return _failure("unavailable")
        except (sqlite3.Error, TypeError, ValueError) as exc:
            _detach_exception(exc)
            return _failure("unavailable")
        except Exception as exc:
            _detach_exception(exc)
            return _failure("unavailable")

        try:
            return complete_trusted_login(
                connection,
                proof,
                completion_policy,
                request_secret_vault,
                trusted_now=trusted_now,
                idempotency_key=request_key,
            )
        finally:
            account_id = None
            identity_id = None
            authenticated_at = None
            proof_expiry = None
            environment = None
            request_key = None
            trusted_now = None
            proof = None
    finally:
        if transaction_record is not None:
            _terminalize_transaction(transaction_record)
        gateway_object = None
        gateway_record = None
        connection = None
        transaction = None
        transaction_record = None
        callback_url = None
        completion_policy = None
        request_secret_vault = None
        claim_attempt = None
        projection = None
        verified_identity = None
        resolved = None
        capsule = None
        capsule_values = None
        proof = None


def _commit_claimed_delegation(
    gateway_object,
    gateway_record,
    transaction,
    transaction_record,
    claim_attempt,
    resolved,
    projection,
):
    if (
        type(gateway_object) is not GoogleOidcGateway
        or type(gateway_record) is not _GatewayRecord
        or type(transaction) is not GoogleOidcAuthorizationTransaction
        or type(transaction_record) is not _TransactionRecord
        or type(claim_attempt) is not object
        or type(resolved) is not _ResolvedDurableIdentity
        or type(projection) is not tuple
        or len(projection) != 6
    ):
        raise _InvalidTransaction()
    with gateway_record.lock:
        if (
            gateway_record.closed
            or gateway_record.transactions.get(transaction)
            is not transaction_record
        ):
            raise _InvalidTransaction()
        try:
            owned_record = object.__getattribute__(gateway_object, "_record")
        except AttributeError:
            raise _InvalidTransaction() from None
        if owned_record is not gateway_record:
            raise _InvalidTransaction()
        configuration = gateway_record.configuration_record
        with transaction_record.lock:
            if (
                type(configuration) is not _ConfigurationRecord
                or transaction._gateway_reference() is not gateway_object
                or transaction_record.gateway is not gateway_object
                or transaction_record.lifecycle != "in_progress"
                or transaction_record.claim_owner is not claim_attempt
                or transaction_record.gateway_authority
                is not gateway_record.transaction_authority
                or transaction_record.configuration
                is not gateway_record.configuration
                or transaction_record.configuration_authority
                is not configuration.authority
                or transaction_record.client_configuration_identity
                is not configuration.client_configuration_identity
                or transaction_record.provider != configuration.provider
                or transaction_record.environment != configuration.environment
                or transaction_record.redirect_uri != configuration.redirect_uri
                or gateway_record.attestation
                != _gateway_attestation(gateway_record)
                or transaction_record.attestation
                != _transaction_attestation(transaction_record)
            ):
                raise _InvalidTransaction()
            committed_now = _clock_now(configuration)
            if (
                committed_now < transaction_record.created_at
                or committed_now >= transaction_record.expires_at
            ):
                raise _InvalidTransaction()
            authenticated_at = projection[1]
            proof_expiry = min(
                projection[2],
                authenticated_at
                + timedelta(seconds=_MAX_AUTHENTICATION_AGE_SECONDS),
                transaction_record.expires_at,
            )
            if proof_expiry <= committed_now:
                raise _AuthenticationDenied()
            request_key = _buffer_text(
                transaction_record.b2d1_request_key
            )
            capsule = _CommittedDelegationCapsule(
                owner=claim_attempt,
                account_id=resolved.account_id,
                identity_id=resolved.identity_id,
                authenticated_at=authenticated_at,
                expires_at=proof_expiry,
                environment=configuration.environment,
                request_key=request_key,
                trusted_now=committed_now,
            )
            transaction_record.lifecycle = "consumed"
            _clear_transaction_buffers(transaction_record)
            transaction_record.gateway = None
            transaction_record.gateway_authority = None
            transaction_record.configuration = None
            transaction_record.configuration_authority = None
            transaction_record.client_configuration_identity = None
            transaction_record.provider = None
            transaction_record.environment = None
            transaction_record.redirect_uri = None
            transaction_record.created_at = None
            transaction_record.expires_at = None
            gateway_record.transactions.pop(transaction, None)
            return capsule


def _close_gateway_record(record):
    adapter = None
    cache = None
    configuration = None
    configuration_record = None
    credential = None
    invitation_lookup_key = None
    transactions = ()
    with record.lock:
        if (
            record.closed
            and record.provider_adapter is None
            and record.cache is None
            and record.configuration_record is None
        ):
            return
        record.closed = True
        transactions = tuple(record.transactions.values())
        record.transactions.clear()
        for transaction_record in transactions:
            _sever_transaction_record(transaction_record)
        adapter = record.provider_adapter
        cache = record.cache
        configuration = record.configuration
        configuration_record = record.configuration_record
        if type(configuration_record) is _ConfigurationRecord:
            credential = configuration_record.credential
        invitation_lookup_key = record.invitation_lookup_key
        record.provider_adapter = None
        record.cache = None
        record.identity_verifier = None
        record.account_service = None
        record.account_native_bootstrap = None
        record.invitation_lookup_key = None
        record.invitation_lookup_key_digest = None
        record.transaction_authority = None
        record.configuration = None
        record.configuration_record = None
        record.attestation = None
    if type(adapter) is _RealGoogleOidcAdapter:
        adapter._configuration = None
        adapter._cache = None
    if type(configuration_record) is _ConfigurationRecord:
        with configuration_record.lock:
            configuration_record.closed = True
            configuration_record.credential = None
            configuration_record.authority = None
            configuration_record.client_configuration_identity = None
            configuration_record.attestation = None
    _close_credential(credential)
    _clear_buffer(invitation_lookup_key)
    if type(cache) is _GoogleOidcJwksCache:
        try:
            cache.close()
        except BaseException as exc:
            _detach_exception(exc)
    adapter = None
    cache = None
    configuration = None
    configuration_record = None
    credential = None
    invitation_lookup_key = None
    transactions = ()


def _sever_transaction_record(record):
    if type(record) is not _TransactionRecord:
        return
    with record.lock:
        record.lifecycle = "consumed"
        _clear_transaction_buffers(record)
        record.gateway = None
        record.gateway_authority = None
        record.configuration = None
        record.configuration_authority = None
        record.client_configuration_identity = None
        record.provider = None
        record.environment = None
        record.redirect_uri = None
        record.created_at = None
        record.expires_at = None


def _poison_gateway_for_control(gateway_object, preserved_control):
    try:
        record = object.__getattribute__(gateway_object, "_record")
    except BaseException as exc:
        _detach_exception(exc)
        return
    if type(record) is not _GatewayRecord:
        return
    try:
        _close_gateway_record(record)
    except BaseException as exc:
        _detach_exception(exc)
    finally:
        gateway_object = None
        record = None
        preserved_control = None


class _CredentialRecord:
    __slots__ = ("secret_buffer", "digest", "configuration_authority", "closed")

    def __init__(self, *, secret_buffer, digest, configuration_authority):
        self.secret_buffer = secret_buffer
        self.digest = digest
        self.configuration_authority = configuration_authority
        self.closed = False


class _ConfigurationRecord:
    __slots__ = (
        "provider",
        "issuers",
        "authorization_endpoint",
        "token_endpoint",
        "jwks_endpoint",
        "algorithms",
        "scopes",
        "pkce_method",
        "client_configuration_identity",
        "client_id",
        "redirect_uri",
        "credential",
        "environment",
        "assurance_policy_version",
        "clock_skew_seconds",
        "maximum_authentication_age_seconds",
        "transaction_ttl",
        "connect_timeout_seconds",
        "read_timeout_seconds",
        "token_response_limit",
        "jwks_response_limit",
        "jwks_max_ttl_seconds",
        "jwks_fallback_ttl_seconds",
        "jwks_unknown_kid_refresh_seconds",
        "jwks_max_keys",
        "authority",
        "lock",
        "closed",
        "attestation",
    )

    def __init__(
        self,
        *,
        client_id,
        redirect_uri,
        credential,
        environment,
        authority,
    ):
        self.provider = _PROVIDER
        self.issuers = _ACCEPTED_ISSUERS
        self.authorization_endpoint = _AUTHORIZATION_ENDPOINT
        self.token_endpoint = _TOKEN_ENDPOINT
        self.jwks_endpoint = _JWKS_ENDPOINT
        self.algorithms = _ALGORITHMS
        self.scopes = _SCOPES
        self.pkce_method = "S256"
        self.client_configuration_identity = object()
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self.credential = credential
        self.environment = environment
        self.assurance_policy_version = _ASSURANCE_POLICY_VERSION
        self.clock_skew_seconds = _CLOCK_SKEW_SECONDS
        self.maximum_authentication_age_seconds = _MAX_AUTHENTICATION_AGE_SECONDS
        self.transaction_ttl = _TRANSACTION_TTL
        self.connect_timeout_seconds = _CONNECT_TIMEOUT_SECONDS
        self.read_timeout_seconds = _READ_TIMEOUT_SECONDS
        self.token_response_limit = _TOKEN_RESPONSE_LIMIT
        self.jwks_response_limit = _JWKS_RESPONSE_LIMIT
        self.jwks_max_ttl_seconds = _JWKS_MAX_TTL_SECONDS
        self.jwks_fallback_ttl_seconds = _JWKS_FALLBACK_TTL_SECONDS
        self.jwks_unknown_kid_refresh_seconds = (
            _JWKS_UNKNOWN_KID_REFRESH_SECONDS
        )
        self.jwks_max_keys = _JWKS_MAX_KEYS
        self.authority = authority
        self.lock = threading.Lock()
        self.closed = False
        self.attestation = _configuration_attestation(self)


class _GatewayRecord:
    __slots__ = (
        "configuration",
        "configuration_record",
        "identity_verifier",
        "account_service",
        "account_native_bootstrap",
        "invitation_lookup_key",
        "invitation_lookup_key_digest",
        "provider_adapter",
        "cache",
        "transaction_authority",
        "transactions",
        "lock",
        "closed",
        "attestation",
    )

    def __init__(
        self,
        *,
        configuration,
        configuration_record,
        identity_verifier,
        account_service,
        invitation_lookup_key,
        provider_adapter,
        cache,
    ):
        self.configuration = configuration
        self.configuration_record = configuration_record
        self.identity_verifier = identity_verifier
        self.account_service = account_service
        self.account_native_bootstrap = None
        self.invitation_lookup_key = invitation_lookup_key
        self.invitation_lookup_key_digest = (
            None
            if invitation_lookup_key is None
            else hashlib.sha256(bytes(invitation_lookup_key)).digest()
        )
        self.provider_adapter = provider_adapter
        self.cache = cache
        self.transaction_authority = object()
        self.transactions = weakref.WeakKeyDictionary()
        self.lock = threading.Lock()
        self.closed = False
        self.attestation = _gateway_attestation(self)


class _TransactionRecord:
    __slots__ = (
        "lock",
        "lifecycle",
        "claim_owner",
        "gateway",
        "gateway_authority",
        "configuration",
        "configuration_authority",
        "client_configuration_identity",
        "provider",
        "environment",
        "redirect_uri",
        "created_at",
        "expires_at",
        "state",
        "nonce",
        "pkce_verifier",
        "b2d1_request_key",
        "authorization_url_buffer",
        "attestation",
    )

    def __init__(
        self,
        *,
        gateway,
        gateway_authority,
        configuration,
        configuration_authority,
        client_configuration_identity,
        provider,
        environment,
        redirect_uri,
        created_at,
        expires_at,
        state,
        nonce,
        pkce_verifier,
        b2d1_request_key,
    ):
        self.lock = threading.Lock()
        self.lifecycle = "fresh"
        self.claim_owner = None
        self.gateway = gateway
        self.gateway_authority = gateway_authority
        self.configuration = configuration
        self.configuration_authority = configuration_authority
        self.client_configuration_identity = client_configuration_identity
        self.provider = provider
        self.environment = environment
        self.redirect_uri = redirect_uri
        self.created_at = created_at
        self.expires_at = expires_at
        self.state = state
        self.nonce = nonce
        self.pkce_verifier = pkce_verifier
        self.b2d1_request_key = b2d1_request_key
        self.authorization_url_buffer = bytearray()
        self.attestation = _transaction_attestation(self)


class _PreparationRecord:
    __slots__ = ("transaction", "url_buffer")

    def __init__(self, *, transaction, url_buffer):
        self.transaction = transaction
        self.url_buffer = url_buffer


class _ResolvedDurableIdentity:
    __slots__ = ("account_id", "identity_id")

    def __init__(self, *, account_id, identity_id):
        self.account_id = account_id
        self.identity_id = identity_id


class _DurableProviderTransaction:
    __slots__ = ("state", "nonce", "pkce_verifier")

    def __init__(self, *, state, nonce, pkce_verifier):
        self.state = state
        self.nonce = nonce
        self.pkce_verifier = pkce_verifier


class _CommittedDelegationCapsule:
    __slots__ = (
        "_lock",
        "_owner",
        "_account_id",
        "_identity_id",
        "_authenticated_at",
        "_expires_at",
        "_environment",
        "_request_key",
        "_trusted_now",
        "_used",
    )

    def __init__(
        self,
        *,
        owner,
        account_id,
        identity_id,
        authenticated_at,
        expires_at,
        environment,
        request_key,
        trusted_now,
    ):
        self._lock = threading.Lock()
        self._owner = owner
        self._account_id = account_id
        self._identity_id = identity_id
        self._authenticated_at = authenticated_at
        self._expires_at = expires_at
        self._environment = environment
        self._request_key = request_key
        self._trusted_now = trusted_now
        self._used = False

    def take(self, owner):
        with self._lock:
            if self._used or owner is not self._owner:
                raise _InvalidTransaction()
            values = (
                self._account_id,
                self._identity_id,
                self._authenticated_at,
                self._expires_at,
                self._environment,
                self._request_key,
                self._trusted_now,
            )
            self._used = True
            self._owner = None
            self._account_id = None
            self._identity_id = None
            self._authenticated_at = None
            self._expires_at = None
            self._environment = None
            self._request_key = None
            self._trusted_now = None
            return values

    def __repr__(self):
        return "_CommittedDelegationCapsule(<redacted>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("google_oidc_delegation_capsule_not_serializable")

    def __copy__(self):
        raise TypeError("google_oidc_delegation_capsule_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("google_oidc_delegation_capsule_not_copyable")


class _ControlFlowSignal:
    __slots__ = ("control",)

    def __init__(self, control):
        self.control = control


class _JwksRefreshRateLimited(Exception):
    pass


class _JwksRefreshFlight:
    __slots__ = ("base_generation", "done", "succeeded")

    def __init__(self, base_generation):
        self.base_generation = base_generation
        self.done = False
        self.succeeded = False


class _GoogleOidcJwksCache:
    __slots__ = (
        "_configuration",
        "_condition",
        "_key_set",
        "_expires_at",
        "_generation",
        "_last_forced_refresh",
        "_flight",
        "_closed",
    )

    def __init__(self, configuration):
        self._configuration = configuration
        self._condition = threading.Condition()
        self._key_set = None
        self._expires_at = None
        self._generation = 0
        self._last_forced_refresh = None
        self._flight = None
        self._closed = False

    def get(self, fetcher, key_set_type):
        while True:
            with self._condition:
                self._require_open_locked()
                now = _monotonic_now(self._configuration)
                snapshot = self._valid_snapshot_locked(now)
                if snapshot is not None:
                    return snapshot
                self._clear_expired_locked()
                flight = self._flight
                if flight is None:
                    flight = _JwksRefreshFlight(self._generation)
                    self._flight = flight
                    break
                self._wait_for_flight_locked(flight)
        return self._perform_refresh(flight, fetcher, key_set_type)

    def recover_unknown_kid(self, used_generation, fetcher, key_set_type):
        if type(used_generation) is not int or used_generation < 0:
            raise _ProviderUnavailable()
        while True:
            with self._condition:
                self._require_open_locked()
                now = _monotonic_now(self._configuration)
                snapshot = self._valid_snapshot_locked(now)
                if snapshot is not None and snapshot[1] != used_generation:
                    return snapshot
                if snapshot is None:
                    self._clear_expired_locked()
                flight = self._flight
                if flight is not None:
                    self._wait_for_flight_locked(flight)
                    continue
                if snapshot is not None:
                    if (
                        self._last_forced_refresh is not None
                        and now - self._last_forced_refresh
                        < self._configuration.jwks_unknown_kid_refresh_seconds
                    ):
                        raise _JwksRefreshRateLimited()
                    self._last_forced_refresh = now
                flight = _JwksRefreshFlight(self._generation)
                self._flight = flight
                break
        return self._perform_refresh(flight, fetcher, key_set_type)

    def _perform_refresh(self, flight, fetcher, key_set_type):
        document = None
        key_set = None
        try:
            document, ttl = fetcher()
            if type(document) is not dict:
                raise _ProviderUnavailable()
            if type(ttl) is not int or ttl <= 0:
                raise _ProviderUnavailable()
            keys = document.get("keys")
            if (
                type(keys) is not list
                or not keys
                or self._configuration is None
                or len(keys) > self._configuration.jwks_max_keys
                or any(type(item) is not dict for item in keys)
            ):
                raise _ProviderUnavailable()
            key_set = key_set_type.import_key_set(document)
            now = _monotonic_now(self._configuration)
            with self._condition:
                if self._closed or self._flight is not flight:
                    raise _ProviderUnavailable()
                self._key_set = key_set
                self._expires_at = now + ttl
                self._generation += 1
                flight.done = True
                flight.succeeded = True
                self._flight = None
                self._condition.notify_all()
                return self._key_set, self._generation
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            self._fail_flight(flight)
            raise
        except _ProviderUnavailable:
            self._fail_flight(flight)
            raise
        except Exception as exc:
            self._fail_flight(flight)
            _detach_exception(exc)
            raise _ProviderUnavailable() from None
        finally:
            document = None
            key_set = None

    def _require_open_locked(self):
        if self._closed or self._configuration is None:
            raise _ProviderUnavailable()

    def _valid_snapshot_locked(self, now):
        if (
            self._key_set is not None
            and self._expires_at is not None
            and now < self._expires_at
        ):
            return self._key_set, self._generation
        return None

    def _clear_expired_locked(self):
        self._key_set = None
        self._expires_at = None

    def _wait_for_flight_locked(self, flight):
        while not flight.done and not self._closed:
            self._condition.wait()
        if self._closed or not flight.succeeded:
            raise _ProviderUnavailable()

    def _fail_flight(self, flight):
        with self._condition:
            flight.done = True
            flight.succeeded = False
            if self._flight is flight:
                self._flight = None
            self._condition.notify_all()

    def close(self):
        with self._condition:
            self._closed = True
            self._key_set = None
            self._expires_at = None
            self._generation = 0
            self._last_forced_refresh = None
            flight = self._flight
            if flight is not None:
                flight.done = True
                flight.succeeded = False
            self._flight = None
            self._configuration = None
            self._condition.notify_all()


class _RealGoogleOidcAdapter:
    __slots__ = ("_configuration", "_cache")

    def __init__(self, configuration, cache):
        self._configuration = configuration
        self._cache = cache

    def verify(self, callback_url, transaction):
        dependencies = _load_dependencies()
        callback_url = _validated_callback_url(
            callback_url,
            self._configuration.redirect_uri,
        )
        token = None
        id_token = None
        decoded = None
        claims = None
        key_set = None
        generation = None
        try:
            token = _exchange_code(
                dependencies,
                self._configuration,
                transaction,
                callback_url,
            )
            id_token = token.get("id_token")
            if (
                type(id_token) is not str
                or not id_token
                or len(id_token.encode("ascii", "strict"))
                > self._configuration.token_response_limit
            ):
                raise _ProviderUnavailable()
            key_set, generation = self._cache.get(
                lambda: _fetch_jwks(dependencies, self._configuration),
                dependencies.KeySet,
            )
            try:
                decoded = dependencies.jwt.decode(
                    id_token,
                    key_set,
                    algorithms=["RS256"],
                )
            except dependencies.InvalidKeyIdError:
                try:
                    key_set, generation = self._cache.recover_unknown_kid(
                        generation,
                        lambda: _fetch_jwks(dependencies, self._configuration),
                        dependencies.KeySet,
                    )
                except _JwksRefreshRateLimited:
                    raise _AuthenticationDenied() from None
                try:
                    decoded = dependencies.jwt.decode(
                        id_token,
                        key_set,
                        algorithms=["RS256"],
                    )
                except dependencies.InvalidKeyIdError:
                    raise _AuthenticationDenied() from None
            now = _clock_now(self._configuration)
            claims = _validated_code_id_token(
                dependencies,
                decoded,
                transaction,
                self._configuration,
                now,
            )
            provider_subject = _validated_subject(claims["sub"])
            authenticated_at = _canonical_time(
                datetime.fromtimestamp(
                    claims["auth_time"],
                    timezone.utc,
                )
            )
            token_expires_at = _canonical_time(
                datetime.fromtimestamp(
                    claims["exp"],
                    timezone.utc,
                )
            )
            if token_expires_at <= authenticated_at:
                raise _AuthenticationDenied()
            verified_email, email_verified = _verified_email_projection(
                claims
            )
            return (
                provider_subject,
                authenticated_at,
                token_expires_at,
                _ASSURANCE_POLICY_VERSION,
                verified_email,
                email_verified,
            )
        except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
            _detach_exception(exc)
            raise exc from None
        except _AuthenticationDenied:
            raise
        except _ProviderUnavailable:
            raise
        except _InvalidTransaction:
            raise
        except (_ProviderResponseInvalid, _ProviderResponseTooLarge) as exc:
            _detach_exception(exc)
            raise _ProviderUnavailable() from None
        except (
            dependencies.MismatchingStateError,
            dependencies.MismatchingStateException,
        ) as exc:
            _detach_exception(exc)
            raise _InvalidTransaction() from None
        except dependencies.OAuthError as exc:
            error = exc.error
            _detach_exception(exc)
            if error in _OAUTH_INFRASTRUCTURE_ERRORS:
                raise _ProviderUnavailable() from None
            raise _AuthenticationDenied() from None
        except dependencies.RequestException as exc:
            _detach_exception(exc)
            raise _ProviderUnavailable() from None
        except dependencies.JoseError as exc:
            _detach_exception(exc)
            raise _AuthenticationDenied() from None
        except dependencies.AuthlibBaseError as exc:
            _detach_exception(exc)
            raise _ProviderUnavailable() from None
        except (UnicodeError, ValueError, TypeError, KeyError) as exc:
            _detach_exception(exc)
            raise _ProviderUnavailable() from None
        finally:
            if isinstance(token, dict):
                token.clear()
            token = None
            id_token = None
            decoded = None
            claims = None
            key_set = None
            generation = None
            callback_url = None
            transaction = None
            dependencies = None


class _Dependencies:
    __slots__ = (
        "OAuth2Session",
        "CodeIDToken",
        "jwt",
        "KeySet",
        "AuthlibBaseError",
        "InvalidKeyIdError",
        "JoseError",
        "MismatchingStateError",
        "MismatchingStateException",
        "OAuthError",
        "RequestException",
        "RequestsSession",
        "Response",
        "HTTPAdapter",
    )


def _load_dependencies():
    from authlib.common.errors import AuthlibBaseError
    from authlib.integrations.base_client.errors import (
        MismatchingStateError,
        OAuthError,
    )
    from authlib.integrations.requests_client import OAuth2Session
    from authlib.oidc.core import CodeIDToken
    from authlib.oauth2.rfc6749.errors import MismatchingStateException
    from joserfc import jwt
    from joserfc.errors import InvalidKeyIdError, JoseError
    from joserfc.jwk import KeySet
    from requests import Response, Session as RequestsSession
    from requests.adapters import HTTPAdapter
    from requests.exceptions import RequestException

    dependencies = _Dependencies()
    dependencies.OAuth2Session = OAuth2Session
    dependencies.CodeIDToken = CodeIDToken
    dependencies.jwt = jwt
    dependencies.KeySet = KeySet
    dependencies.AuthlibBaseError = AuthlibBaseError
    dependencies.InvalidKeyIdError = InvalidKeyIdError
    dependencies.JoseError = JoseError
    dependencies.MismatchingStateError = MismatchingStateError
    dependencies.MismatchingStateException = MismatchingStateException
    dependencies.OAuthError = OAuthError
    dependencies.RequestException = RequestException
    dependencies.RequestsSession = RequestsSession
    dependencies.Response = Response
    dependencies.HTTPAdapter = HTTPAdapter
    return dependencies


def _configuration_attestation(record):
    return (
        record.provider,
        record.issuers,
        record.authorization_endpoint,
        record.token_endpoint,
        record.jwks_endpoint,
        record.algorithms,
        record.scopes,
        record.pkce_method,
        id(record.client_configuration_identity),
        record.client_id,
        record.redirect_uri,
        id(record.credential),
        record.environment,
        record.assurance_policy_version,
        record.clock_skew_seconds,
        record.maximum_authentication_age_seconds,
        record.transaction_ttl,
        record.connect_timeout_seconds,
        record.read_timeout_seconds,
        record.token_response_limit,
        record.jwks_response_limit,
        record.jwks_max_ttl_seconds,
        record.jwks_fallback_ttl_seconds,
        record.jwks_unknown_kid_refresh_seconds,
        record.jwks_max_keys,
        id(record.authority),
    )


def _gateway_attestation(record):
    return (
        id(record.configuration),
        id(record.configuration_record),
        id(record.identity_verifier),
        id(record.account_service),
        id(record.account_native_bootstrap),
        id(record.invitation_lookup_key),
        record.invitation_lookup_key_digest,
        id(record.provider_adapter),
        id(record.cache),
        id(record.transaction_authority),
        id(record.transactions),
        id(record.lock),
    )


def _verified_email_projection(claims):
    if not isinstance(claims, dict):
        try:
            email = claims.get("email")
            email_verified = claims.get("email_verified")
        except (AttributeError, TypeError):
            return None, False
    else:
        email = claims.get("email")
        email_verified = claims.get("email_verified")
    if type(email) is not str or type(email_verified) is not bool:
        return None, False
    return email, email_verified is True


def _transaction_attestation(record):
    buffers = (
        record.state,
        record.nonce,
        record.pkce_verifier,
        record.b2d1_request_key,
    )
    if any(type(value) is not bytearray or not value for value in buffers):
        return None
    return (
        id(record.gateway),
        id(record.gateway_authority),
        id(record.configuration),
        id(record.configuration_authority),
        id(record.client_configuration_identity),
        record.provider,
        record.environment,
        record.redirect_uri,
        record.created_at,
        record.expires_at,
        tuple(hashlib.sha256(bytes(value)).digest() for value in buffers),
    )


def _configuration_record(configuration):
    if type(configuration) is not TrustedGoogleOidcConfiguration:
        raise TypeError("trusted_google_oidc_configuration_required")
    try:
        record = object.__getattribute__(configuration, "_record")
    except AttributeError:
        raise TypeError(
            "trusted_google_oidc_configuration_invalid"
        ) from None
    if (
        type(record) is not _ConfigurationRecord
        or record.closed
        or record.provider != _PROVIDER
        or record.issuers != _ACCEPTED_ISSUERS
        or record.authorization_endpoint != _AUTHORIZATION_ENDPOINT
        or record.token_endpoint != _TOKEN_ENDPOINT
        or record.jwks_endpoint != _JWKS_ENDPOINT
        or record.algorithms != _ALGORITHMS
        or record.scopes != _SCOPES
        or record.pkce_method != "S256"
        or record.connect_timeout_seconds != _CONNECT_TIMEOUT_SECONDS
        or record.read_timeout_seconds != _READ_TIMEOUT_SECONDS
        or record.token_response_limit != _TOKEN_RESPONSE_LIMIT
        or record.jwks_response_limit != _JWKS_RESPONSE_LIMIT
        or record.jwks_max_ttl_seconds != _JWKS_MAX_TTL_SECONDS
        or record.jwks_fallback_ttl_seconds != _JWKS_FALLBACK_TTL_SECONDS
        or record.jwks_unknown_kid_refresh_seconds
        != _JWKS_UNKNOWN_KID_REFRESH_SECONDS
        or record.jwks_max_keys != _JWKS_MAX_KEYS
        or record.assurance_policy_version != _ASSURANCE_POLICY_VERSION
        or record.environment not in _ENVIRONMENTS
        or record.attestation != _configuration_attestation(record)
    ):
        raise TypeError("trusted_google_oidc_configuration_invalid")
    try:
        credential = object.__getattribute__(
            record.credential,
            "_record",
        )
    except (AttributeError, TypeError):
        raise TypeError(
            "trusted_google_oidc_configuration_invalid"
        ) from None
    if (
        type(credential) is not _CredentialRecord
        or credential.closed
        or credential.configuration_authority is not record.authority
        or type(credential.secret_buffer) is not bytearray
        or not credential.secret_buffer
        or type(credential.digest) is not bytes
        or not hmac.compare_digest(
            hashlib.sha256(
                b"wahojobs-google-oidc-credential-v1\x00"
                + bytes(credential.secret_buffer)
            ).digest(),
            credential.digest,
        )
    ):
        raise TypeError("trusted_google_oidc_configuration_invalid")
    return record


def _gateway_record(gateway):
    if type(gateway) is not GoogleOidcGateway:
        raise TypeError("google_oidc_gateway_required")
    try:
        record = object.__getattribute__(gateway, "_record")
    except AttributeError:
        raise TypeError("google_oidc_gateway_unavailable") from None
    if type(record) is not _GatewayRecord or record.closed:
        raise TypeError("google_oidc_gateway_unavailable")
    if record.attestation != _gateway_attestation(record):
        raise TypeError("google_oidc_gateway_unavailable")
    if _configuration_record(record.configuration) is not record.configuration_record:
        raise TypeError("google_oidc_gateway_unavailable")
    if type(record.identity_verifier) is not TrustedIdentityVerifier:
        raise TypeError("google_oidc_gateway_unavailable")
    try:
        service_verifier = object.__getattribute__(
            record.account_service,
            "_identity_verifier",
        )
    except (AttributeError, TypeError):
        raise TypeError("google_oidc_gateway_unavailable") from None
    invitation_key = record.invitation_lookup_key
    invitation_digest = record.invitation_lookup_key_digest
    bootstrap_authority = record.account_native_bootstrap
    if (
        type(record.account_service) is not AccountService
        or service_verifier is not record.identity_verifier
        or (
            bootstrap_authority is not None
            and bootstrap_authority is not ensure_account_native_principal
        )
        or not (
            (invitation_key is None and invitation_digest is None)
            or (
                type(invitation_key) is bytearray
                and _INVITATION_LOOKUP_KEY_MIN_BYTES
                <= len(invitation_key)
                <= _INVITATION_LOOKUP_KEY_MAX_BYTES
                and type(invitation_digest) is bytes
                and hmac.compare_digest(
                    hashlib.sha256(bytes(invitation_key)).digest(),
                    invitation_digest,
                )
            )
        )
    ):
        raise TypeError("google_oidc_gateway_unavailable")
    if (
        type(record.cache) is not _GoogleOidcJwksCache
        or type(record.provider_adapter) is not _RealGoogleOidcAdapter
        or record.provider_adapter._configuration
        is not record.configuration_record
        or record.provider_adapter._cache is not record.cache
    ):
        raise TypeError("google_oidc_gateway_unavailable")
    return record


def _durable_google_oidc_context(gateway):
    """Return the fixed, process-reconstructible durable configuration context."""

    gateway_record = _gateway_record(gateway)
    configuration = gateway_record.configuration_record
    credential = object.__getattribute__(
        configuration.credential,
        "_record",
    )
    binding_document = {
        "algorithms": configuration.algorithms,
        "assurance_policy_version": configuration.assurance_policy_version,
        "authorization_endpoint": configuration.authorization_endpoint,
        "client_credential_digest": credential.digest.hex(),
        "client_id": configuration.client_id,
        "clock_skew_seconds": configuration.clock_skew_seconds,
        "connect_timeout_seconds": configuration.connect_timeout_seconds,
        "environment_namespace": configuration.environment,
        "issuers": configuration.issuers,
        "jwks_endpoint": configuration.jwks_endpoint,
        "jwks_fallback_ttl_seconds": (
            configuration.jwks_fallback_ttl_seconds
        ),
        "jwks_max_keys": configuration.jwks_max_keys,
        "jwks_max_ttl_seconds": configuration.jwks_max_ttl_seconds,
        "jwks_response_limit": configuration.jwks_response_limit,
        "jwks_unknown_kid_refresh_seconds": (
            configuration.jwks_unknown_kid_refresh_seconds
        ),
        "maximum_authentication_age_seconds": (
            configuration.maximum_authentication_age_seconds
        ),
        "pkce_method": configuration.pkce_method,
        "provider": configuration.provider,
        "read_timeout_seconds": configuration.read_timeout_seconds,
        "redirect_uri": configuration.redirect_uri,
        "scopes": configuration.scopes,
        "token_endpoint": configuration.token_endpoint,
        "token_response_limit": configuration.token_response_limit,
        "transaction_ttl_seconds": int(
            configuration.transaction_ttl.total_seconds()
        ),
        "version": 1,
    }
    encoded = json.dumps(
        binding_document,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    if not (
        1
        <= len(encoded)
        <= MAX_DURABLE_CONFIGURATION_CONTEXT_BYTES
    ):
        raise TypeError("google_oidc_gateway_unavailable")
    return configuration.provider, configuration.environment, encoded


def _durable_google_oidc_now(gateway):
    gateway_record = _gateway_record(gateway)
    return _canonical_time(
        _clock_now(gateway_record.configuration_record)
    )


def _durable_google_oidc_authorization_url(
    gateway,
    *,
    state,
    nonce,
    pkce_verifier,
):
    gateway_record = _gateway_record(gateway)
    if (
        type(state) is not bytearray
        or type(nonce) is not bytearray
        or type(pkce_verifier) is not bytearray
    ):
        raise TypeError("google_oidc_durable_material_invalid")
    transaction = _DurableProviderTransaction(
        state=state,
        nonce=nonce,
        pkce_verifier=pkce_verifier,
    )
    try:
        url = _prepare_authorization_url(
            gateway_record.configuration_record,
            transaction,
        )
        return bytearray(url.encode("ascii", "strict"))
    finally:
        transaction.state = None
        transaction.nonce = None
        transaction.pkce_verifier = None
        transaction = None


def _new_transaction(gateway, gateway_record, now):
    config = gateway_record.configuration_record
    state = None
    nonce = None
    verifier = None
    request_key = None
    record = None
    retained = False
    try:
        state = bytearray(secrets.token_urlsafe(32).encode("ascii"))
        nonce = bytearray(secrets.token_urlsafe(32).encode("ascii"))
        verifier = bytearray(secrets.token_urlsafe(64).encode("ascii"))
        request_key = bytearray(
            ("google-oidc-" + secrets.token_urlsafe(32)).encode("ascii")
        )
        transaction = object.__new__(GoogleOidcAuthorizationTransaction)
        object.__setattr__(
            transaction,
            "_gateway_reference",
            weakref.ref(gateway),
        )
        record = _TransactionRecord(
            gateway=gateway,
            gateway_authority=gateway_record.transaction_authority,
            configuration=gateway_record.configuration,
            configuration_authority=config.authority,
            client_configuration_identity=config.client_configuration_identity,
            provider=config.provider,
            environment=config.environment,
            redirect_uri=config.redirect_uri,
            created_at=now,
            expires_at=now + config.transaction_ttl,
            state=state,
            nonce=nonce,
            pkce_verifier=verifier,
            b2d1_request_key=request_key,
        )
        with gateway_record.lock:
            if gateway_record.closed:
                _terminalize_transaction(record)
                raise _Unavailable()
            gateway_record.transactions[transaction] = record
            retained = True
        return transaction
    finally:
        if not retained:
            _clear_buffer(state)
            _clear_buffer(nonce)
            _clear_buffer(verifier)
            _clear_buffer(request_key)


def _prepare_authorization_url(configuration, transaction):
    dependencies = _load_dependencies()
    session = None
    preserved_control = None
    state = _buffer_text(transaction.state)
    nonce = _buffer_text(transaction.nonce)
    verifier = _buffer_text(transaction.pkce_verifier)
    try:
        session = dependencies.OAuth2Session(
            client_id=configuration.client_id,
            scope=configuration.scopes,
            redirect_uri=configuration.redirect_uri,
            code_challenge_method=configuration.pkce_method,
            response_type="code",
        )
        session.trust_env = False
        url, returned_state = session.create_authorization_url(
            configuration.authorization_endpoint,
            state=state,
            code_verifier=verifier,
            nonce=nonce,
            claims=json.dumps(
                {"id_token": {"auth_time": {"essential": True}}},
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        if returned_state != state:
            raise _Unavailable()
        _validate_prepared_authorization_url(
            url,
            configuration,
            state,
            nonce,
        )
        return url
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
        preserved_control = exc
        raise
    finally:
        if session is not None:
            _cleanup_preserving_exception(
                preserved_control,
                lambda: _clear_oauth_session_token(session),
                session.close,
            )
        state = None
        nonce = None
        verifier = None
        dependencies = None


def _validate_prepared_authorization_url(url, configuration, state, nonce):
    if type(url) is not str or len(url.encode("ascii", "strict")) > _CALLBACK_URL_LIMIT:
        raise _Unavailable()
    parts = urlsplit(url)
    endpoint = urlsplit(configuration.authorization_endpoint)
    if (
        parts.scheme != endpoint.scheme
        or parts.netloc != endpoint.netloc
        or parts.path != endpoint.path
        or parts.fragment
    ):
        raise _Unavailable()
    pairs = parse_qsl(
        parts.query,
        keep_blank_values=True,
        strict_parsing=True,
        max_num_fields=16,
    )
    values = {}
    for name, value in pairs:
        if name in values:
            raise _Unavailable()
        values[name] = value
    required = {
        "client_id": configuration.client_id,
        "redirect_uri": configuration.redirect_uri,
        "response_type": "code",
        "state": state,
        "nonce": nonce,
        "code_challenge_method": configuration.pkce_method,
        "scope": "openid email",
        "claims": json.dumps(
            {"id_token": {"auth_time": {"essential": True}}},
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    if (
        set(values) != set(required) | {"code_challenge"}
        or any(values.get(name) != value for name, value in required.items())
    ):
        raise _Unavailable()
    if not values.get("code_challenge"):
        raise _Unavailable()


def _claim_transaction(
    gateway,
    gateway_record,
    transaction,
    now,
    claim_attempt,
):
    if type(transaction) is not GoogleOidcAuthorizationTransaction:
        raise _InvalidTransaction()
    if type(claim_attempt) is not object:
        raise _InvalidTransaction()
    record = None
    transitioned = False
    try:
        with gateway_record.lock:
            record = gateway_record.transactions.get(transaction)
            if type(record) is not _TransactionRecord:
                raise _InvalidTransaction()
            config = gateway_record.configuration_record
            with record.lock:
                if record.lifecycle != "fresh":
                    raise _InvalidTransaction()
                if (
                    transaction._gateway_reference() is not gateway
                    or record.gateway is not gateway
                    or record.gateway_authority
                    is not gateway_record.transaction_authority
                    or record.configuration is not gateway_record.configuration
                    or record.configuration_authority is not config.authority
                    or record.client_configuration_identity
                    is not config.client_configuration_identity
                    or record.provider != config.provider
                    or record.environment != config.environment
                    or record.redirect_uri != config.redirect_uri
                    or record.attestation != _transaction_attestation(record)
                ):
                    record.lifecycle = "consumed"
                    _clear_transaction_buffers(record)
                    raise _InvalidTransaction()
                if now < record.created_at or now >= record.expires_at:
                    record.lifecycle = "consumed"
                    _clear_transaction_buffers(record)
                    raise _InvalidTransaction()
                record.claim_owner = claim_attempt
                record.lifecycle = "in_progress"
                transitioned = True
        return record
    except BaseException as exc:
        if transitioned:
            _cleanup_preserving_exception(
                exc,
                lambda: _consume_claim_record_if_owned(
                    record,
                    claim_attempt,
                ),
            )
        raise


def _require_claimed_transaction_fresh(
    record,
    gateway_record,
    now,
    claim_attempt,
):
    if (
        type(record) is not _TransactionRecord
        or type(gateway_record) is not _GatewayRecord
    ):
        raise _InvalidTransaction()
    with record.lock:
        if (
            record.lifecycle != "in_progress"
            or record.claim_owner is not claim_attempt
            or record.gateway_authority
            is not gateway_record.transaction_authority
            or now < record.created_at
            or now >= record.expires_at
            or record.attestation != _transaction_attestation(record)
        ):
            raise _InvalidTransaction()


def _consume_claim_record_if_owned(record, claim_attempt):
    if (
        type(record) is not _TransactionRecord
        or type(claim_attempt) is not object
    ):
        return
    with record.lock:
        if (
            record.lifecycle == "in_progress"
            and record.claim_owner is claim_attempt
        ):
            record.lifecycle = "consumed"
            _clear_transaction_buffers(record)


def _consume_claim_attempt_if_owned(
    gateway,
    gateway_record,
    transaction,
    claim_attempt,
):
    if (
        type(gateway) is not GoogleOidcGateway
        or type(gateway_record) is not _GatewayRecord
        or type(transaction) is not GoogleOidcAuthorizationTransaction
        or type(claim_attempt) is not object
    ):
        return
    try:
        owned_record = object.__getattribute__(gateway, "_record")
    except AttributeError:
        return
    if owned_record is not gateway_record:
        return
    with gateway_record.lock:
        record = gateway_record.transactions.get(transaction)
        if type(record) is not _TransactionRecord:
            return
        _consume_claim_record_if_owned(record, claim_attempt)


def _consume_fresh_transaction_if_owned(
    gateway,
    gateway_record,
    transaction,
):
    if (
        type(gateway) is not GoogleOidcGateway
        or type(gateway_record) is not _GatewayRecord
        or type(transaction) is not GoogleOidcAuthorizationTransaction
    ):
        return
    try:
        owned_record = object.__getattribute__(gateway, "_record")
    except AttributeError:
        return
    if owned_record is not gateway_record:
        return
    with gateway_record.lock:
        record = gateway_record.transactions.get(transaction)
        if type(record) is not _TransactionRecord:
            return
        with record.lock:
            if (
                record.lifecycle == "fresh"
                and record.gateway is gateway
            ):
                record.lifecycle = "consumed"
                _clear_transaction_buffers(record)


def _terminalize_transaction(record):
    if type(record) is not _TransactionRecord:
        return
    with record.lock:
        record.lifecycle = "consumed"
        _clear_transaction_buffers(record)


def _clear_transaction_buffers(record):
    _clear_buffer(record.state)
    _clear_buffer(record.nonce)
    _clear_buffer(record.pkce_verifier)
    _clear_buffer(record.b2d1_request_key)
    _clear_buffer(record.authorization_url_buffer)
    record.claim_owner = None
    record.attestation = None


def _transaction_public_time(transaction, field):
    gateway = transaction._gateway_reference()
    try:
        record = (
            object.__getattribute__(gateway, "_record")
            if gateway is not None
            else None
        )
    except AttributeError:
        record = None
    if type(record) is not _GatewayRecord:
        raise TypeError("google_oidc_authorization_transaction_invalid")
    with record.lock:
        transaction_record = record.transactions.get(transaction)
        if type(transaction_record) is not _TransactionRecord:
            raise TypeError("google_oidc_authorization_transaction_invalid")
        with transaction_record.lock:
            return getattr(transaction_record, field)


def _verify_provider(gateway, callback_url, transaction):
    adapter = gateway.provider_adapter
    if (
        type(adapter) is not _RealGoogleOidcAdapter
        or adapter._configuration is not gateway.configuration_record
        or adapter._cache is not gateway.cache
    ):
        raise _Unavailable()
    projection = adapter.verify(callback_url, transaction)
    if (
        type(projection) is not tuple
        or len(projection) != 6
        or type(projection[0]) is not str
        or type(projection[1]) is not datetime
        or type(projection[2]) is not datetime
        or projection[2] <= projection[1]
        or projection[3] != _ASSURANCE_POLICY_VERSION
        or (
            projection[4] is not None
            and type(projection[4]) is not str
        )
        or type(projection[5]) is not bool
        or (projection[5] and projection[4] is None)
    ):
        raise _Unavailable()
    return projection


def _complete_durable_google_oidc_claimed(
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
    created_at,
    expires_at,
    claimed_at,
    invitation_credential=None,
):
    """Complete one already-terminal durable claim through the real gateway."""

    gateway_record = None
    transaction = None
    projection = None
    verified_identity = None
    resolved = None
    ownership = None
    proof = None
    try:
        gateway_record = _gateway_record(gateway)
        configuration = gateway_record.configuration_record
        if (
            type(connection) is not sqlite3.Connection
            or connection.in_transaction
            or type(state) is not bytearray
            or type(nonce) is not bytearray
            or type(pkce_verifier) is not bytearray
            or type(b2d1_request_key) is not bytearray
        ):
            raise _Unavailable()
        state_text = _buffer_text(state)
        nonce_text = _buffer_text(nonce)
        verifier_text = _buffer_text(pkce_verifier)
        request_key = _buffer_text(b2d1_request_key)
        if (
            _DURABLE_STATE.fullmatch(state_text) is None
            or _DURABLE_STATE.fullmatch(nonce_text) is None
            or len(verifier_text) != 86
            or re.fullmatch(r"[A-Za-z0-9_-]{86}", verifier_text) is None
            or len(request_key) != 55
            or not request_key.startswith("google-oidc-")
            or _DURABLE_STATE.fullmatch(request_key[12:]) is None
        ):
            raise _InvalidTransaction()
        created_at = _canonical_time(created_at)
        expires_at = _canonical_time(expires_at)
        claimed_at = _canonical_time(claimed_at)
        if (
            expires_at - created_at != _TRANSACTION_TTL
            or claimed_at < created_at
            or claimed_at >= expires_at
        ):
            raise _InvalidTransaction()
        now = _canonical_time(_clock_now(configuration))
        if now < claimed_at or now >= expires_at:
            raise _InvalidTransaction()
        transaction = _DurableProviderTransaction(
            state=state,
            nonce=nonce,
            pkce_verifier=pkce_verifier,
        )
        projection = _verify_provider(
            gateway_record,
            callback_url,
            transaction,
        )
        now = _canonical_time(_clock_now(configuration))
        if (
            connection.in_transaction
            or now < claimed_at
            or now >= expires_at
        ):
            raise _InvalidTransaction()
        verified_identity = (
            gateway_record.identity_verifier.from_validated_google_claims(
                provider_subject=projection[0],
                verified_email=None,
                email_verified=False,
                authenticated_at=projection[1],
                metadata_version=_METADATA_VERSION,
            )
        )
        if not gateway_record.identity_verifier._accepts(verified_identity):
            raise _Unavailable()
        resolved = _resolve_or_provision_durable_identity(
            connection,
            gateway_record,
            verified_identity,
            projection,
            now,
            invitation_credential=invitation_credential,
            idempotency_key=request_key,
        )
        if connection.in_transaction:
            raise _Unavailable()
        ownership = _ensure_account_native_principal_for_login(
            connection,
            gateway_record,
            resolved,
            now,
        )
        if connection.in_transaction:
            raise _Unavailable()
        ownership = None
        authenticated_at = projection[1]
        proof_expiry = min(
            projection[2],
            authenticated_at
            + timedelta(seconds=_MAX_AUTHENTICATION_AGE_SECONDS),
            expires_at,
        )
        if proof_expiry <= now:
            raise _AuthenticationDenied()
        proof = TrustedExternalIdentityAuthentication._issue(
            _trusted_login._ASSERTION_ISSUANCE_CAPABILITY,
            account_id=resolved.account_id,
            identity_id=resolved.identity_id,
            provider=_PROVIDER,
            authenticated_at=authenticated_at,
            expires_at=proof_expiry,
            assurance_policy_version=_ASSURANCE_POLICY_VERSION,
            environment_namespace=configuration.environment,
        )
        return complete_trusted_login(
            connection,
            proof,
            completion_policy,
            request_secret_vault,
            trusted_now=now,
            idempotency_key=request_key,
        )
    except _InvalidTransaction as exc:
        _detach_exception(exc)
        return _failure("invalid_or_expired_transaction")
    except _AuthenticationDenied as exc:
        _detach_exception(exc)
        return _failure("authentication_denied")
    except _ProviderUnavailable as exc:
        _detach_exception(exc)
        if gateway_record is not None and gateway_record.closed:
            return _failure("invalid_or_expired_transaction")
        return _failure("provider_unavailable")
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as control:
        _poison_gateway_for_control(gateway, control)
        _detach_exception(control)
        raise control from None
    except Exception as exc:
        _detach_exception(exc)
        if gateway_record is not None and gateway_record.closed:
            return _failure("invalid_or_expired_transaction")
        return _failure("unavailable")
    finally:
        if transaction is not None:
            transaction.state = None
            transaction.nonce = None
            transaction.pkce_verifier = None
        _clear_buffer(state)
        _clear_buffer(nonce)
        _clear_buffer(pkce_verifier)
        _clear_buffer(b2d1_request_key)
        gateway = None
        connection = None
        callback_url = None
        completion_policy = None
        request_secret_vault = None
        gateway_record = None
        transaction = None
        projection = None
        verified_identity = None
        resolved = None
        ownership = None
        proof = None
        invitation_credential = None


def _ensure_account_native_principal_for_login(
    connection,
    gateway_record,
    resolved,
    now,
):
    if (
        type(connection) is not sqlite3.Connection
        or connection.in_transaction
        or type(gateway_record) is not _GatewayRecord
        or gateway_record.closed
        or gateway_record.account_native_bootstrap
        is not ensure_account_native_principal
        or type(resolved) is not _ResolvedDurableIdentity
        or type(now) is not datetime
    ):
        raise _Unavailable()
    result = gateway_record.account_native_bootstrap(
        connection,
        user_id=resolved.account_id,
        environment_namespace=(
            gateway_record.configuration_record.environment
        ),
        occurred_at=now.isoformat(timespec="seconds"),
    )
    if (
        type(result) is not AccountNativePrincipalBootstrapResult
        or result.environment_namespace
        != gateway_record.configuration_record.environment
        or type(result.principal_id) is not str
        or not result.principal_id
        or type(result.binding_id) is not str
        or not result.binding_id
        or type(result.initial_event_id) is not str
        or not result.initial_event_id
        or type(result.created) is not bool
        or connection.in_transaction
    ):
        raise _Unavailable()
    return result


def _validated_callback_url(callback_url, redirect_uri):
    canonical_callback, _values, has_error = _validated_callback_response(
        callback_url,
        redirect_uri,
    )
    if has_error:
        raise _AuthenticationDenied()
    return canonical_callback


def _validated_callback_response(callback_url, redirect_uri):
    """Validate one callback and discard bounded non-authoritative fields."""

    if (
        type(callback_url) is not str
        or not callback_url
        or _CONTROL_CHARACTERS.search(callback_url) is not None
    ):
        raise _AuthenticationDenied()
    try:
        encoded = callback_url.encode("ascii", "strict")
    except UnicodeError:
        raise _AuthenticationDenied() from None
    if len(encoded) > _CALLBACK_URL_LIMIT:
        raise _AuthenticationDenied()
    try:
        parts = urlsplit(callback_url)
    except ValueError:
        raise _AuthenticationDenied() from None
    if (
        parts.fragment
        or parts.username is not None
        or parts.password is not None
        or "?" not in callback_url
        or callback_url.split("?", 1)[0] != redirect_uri
    ):
        raise _AuthenticationDenied()
    pairs, values, has_error = _validated_callback_parameters(parts.query)
    try:
        canonical_query = urlencode(
            pairs,
            doseq=False,
            safe="",
            encoding="utf-8",
            errors="strict",
        )
        canonical_callback = redirect_uri + "?" + canonical_query
        if (
            len(canonical_callback.encode("ascii", "strict"))
            > _CALLBACK_URL_LIMIT
        ):
            raise _AuthenticationDenied()
    except (TypeError, UnicodeError, ValueError):
        raise _AuthenticationDenied() from None
    return canonical_callback, values, has_error


def _validated_durable_google_oidc_callback(gateway, callback_url):
    """Recover state and an authoritative-only callback before durable I/O."""

    gateway_record = _gateway_record(gateway)
    redirect_uri = gateway_record.configuration_record.redirect_uri
    try:
        canonical_callback, values, _has_error = (
            _validated_callback_response(callback_url, redirect_uri)
        )
    except (TypeError, ValueError, _AuthenticationDenied):
        raise _InvalidTransaction() from None
    state = values.get("state")
    if (
        type(state) is not str
        or _DURABLE_STATE.fullmatch(state) is None
    ):
        raise _InvalidTransaction()
    return state, canonical_callback


def _durable_google_oidc_callback_state(gateway, callback_url):
    """Strictly recover correlation state before any durable claim or I/O."""

    try:
        state, canonical_callback = _validated_durable_google_oidc_callback(
            gateway,
            callback_url,
        )
        return state
    finally:
        callback_url = None
        canonical_callback = None


def _validated_callback_parameters(raw_query):
    """Decode one bounded response and retain only authoritative fields."""

    try:
        decoded_pairs = _strict_callback_query(raw_query)
    except (TypeError, ValueError, _AuthenticationDenied):
        raise _CallbackQueryInvalid() from None
    if not decoded_pairs or len(decoded_pairs) > _CALLBACK_PARAMETER_LIMIT:
        raise _CallbackQueryInvalid()
    seen_names = set()
    authoritative_pairs = []
    values = {}
    for name, value in decoded_pairs:
        if (
            not name
            or name in seen_names
            or len(name.encode("utf-8")) > _CALLBACK_PARAMETER_NAME_LIMIT
            or len(value.encode("utf-8")) > _CALLBACK_PARAMETER_VALUE_LIMIT
            or _CONTROL_CHARACTERS.search(name) is not None
            or _CONTROL_CHARACTERS.search(value) is not None
        ):
            raise _CallbackQueryInvalid()
        seen_names.add(name)
        if name in _AUTHORITATIVE_CALLBACK_NAMES:
            authoritative_pairs.append((name, value))
            values[name] = value

    names = frozenset(values)
    success_shape = bool(
        frozenset({"code", "state"}) <= names <= _SUCCESS_CALLBACK_NAMES
        and values["state"]
        and values["code"]
    )
    error_shape = bool(
        frozenset({"error", "state"}) <= names <= _ERROR_CALLBACK_NAMES
        and values["state"]
        and values["error"]
    )
    if success_shape == error_shape:
        raise _CallbackQueryInvalid()
    if "iss" not in values:
        raise _ResponseIssuerMissing()
    if not _valid_authorization_response_issuer(values["iss"]):
        raise _ResponseIssuerMismatch()
    return tuple(authoritative_pairs), values, error_shape


def _valid_authorization_response_issuer(value):
    return type(value) is str and hmac.compare_digest(
        value,
        _CANONICAL_ISSUER,
    )


def _strict_callback_query(raw_query):
    if type(raw_query) is not str or not raw_query:
        raise _AuthenticationDenied()
    raw_fields = raw_query.split("&")
    if (
        not raw_fields
        or len(raw_fields) > _CALLBACK_PARAMETER_LIMIT
        or any(not field or "=" not in field for field in raw_fields)
    ):
        raise _AuthenticationDenied()
    pairs = []
    try:
        for field in raw_fields:
            raw_name, raw_value = field.split("=", 1)
            pairs.append(
                (
                    _strict_callback_component(raw_name),
                    _strict_callback_component(raw_value),
                )
            )
    except (TypeError, UnicodeError, ValueError):
        pairs.clear()
        raise _AuthenticationDenied() from None
    return tuple(pairs)


def _strict_callback_component(raw_component):
    if (
        type(raw_component) is not str
        or _INVALID_PERCENT_ESCAPE.search(raw_component) is not None
    ):
        raise ValueError("invalid_callback_component")
    raw_bytes = unquote_to_bytes(raw_component.replace("+", " "))
    decoded = raw_bytes.decode("utf-8", "strict")
    if (
        "\ufffd" in decoded
        or _CONTROL_CHARACTERS.search(decoded) is not None
    ):
        raise ValueError("invalid_callback_component")
    return decoded


def _exchange_code(dependencies, configuration, transaction, callback_url):
    session = None
    token = None
    preserved_control = None
    state = _buffer_text(transaction.state)
    verifier = _buffer_text(transaction.pkce_verifier)
    secret = _credential_text(
        configuration.credential,
        configuration.authority,
    )
    try:
        session = _new_bounded_oauth_session(
            dependencies,
            configuration,
            secret,
        )
        token = session.fetch_token(
            configuration.token_endpoint,
            authorization_response=callback_url,
            state=state,
            code_verifier=verifier,
            redirect_uri=configuration.redirect_uri,
            grant_type="authorization_code",
            allow_redirects=False,
            timeout=(
                configuration.connect_timeout_seconds,
                configuration.read_timeout_seconds,
            ),
            verify=True,
        )
        if not isinstance(token, dict):
            raise _ProviderUnavailable()
        return {"id_token": token.get("id_token")}
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
        preserved_control = exc
        _detach_exception(exc)
        raise exc from None
    finally:
        state = None
        verifier = None
        secret = None
        if session is not None:
            _cleanup_preserving_exception(
                preserved_control,
                lambda: _clear_oauth_session_token(session),
                session.close,
            )
        session = None
        token = None
        callback_url = None
        transaction = None
        configuration = None
        dependencies = None


def _new_bounded_oauth_session(dependencies, configuration, client_secret):
    expected_url = configuration.token_endpoint

    class BoundedOAuth2Session(dependencies.OAuth2Session):
        def send(self, request, **kwargs):
            return _bounded_send(
                super().send,
                request,
                kwargs,
                expected_url=expected_url,
                expected_method="POST",
                maximum_bytes=configuration.token_response_limit,
                response_type=dependencies.Response,
            )

    session = BoundedOAuth2Session(
        client_id=configuration.client_id,
        client_secret=client_secret,
        scope=configuration.scopes,
        redirect_uri=configuration.redirect_uri,
        code_challenge_method=configuration.pkce_method,
        token_endpoint_auth_method="client_secret_post",
        default_timeout=(
            configuration.connect_timeout_seconds,
            configuration.read_timeout_seconds,
        ),
    )
    try:
        session.trust_env = False
        session.headers["Accept-Encoding"] = "gzip, deflate"
        session.mount("https://", dependencies.HTTPAdapter(max_retries=0))
        session.mount("http://", dependencies.HTTPAdapter(max_retries=0))
        session.register_compliance_hook(
            "access_token_response",
            lambda response: _validate_token_response(
                response,
                expected_url,
            ),
        )
        return session
    except BaseException as exc:
        _cleanup_preserving_exception(
            exc,
            lambda: _clear_oauth_session_token(session),
            session.close,
        )
        raise


def _fetch_jwks(dependencies, configuration):
    expected_url = configuration.jwks_endpoint

    class BoundedJwksSession(dependencies.RequestsSession):
        def send(self, request, **kwargs):
            return _bounded_send(
                super().send,
                request,
                kwargs,
                expected_url=expected_url,
                expected_method="GET",
                maximum_bytes=configuration.jwks_response_limit,
                response_type=dependencies.Response,
            )

    session = BoundedJwksSession()
    response = None
    document = None
    preserved_control = None
    try:
        session.trust_env = False
        session.headers["Accept-Encoding"] = "gzip, deflate"
        session.mount("https://", dependencies.HTTPAdapter(max_retries=0))
        session.mount("http://", dependencies.HTTPAdapter(max_retries=0))
        response = session.get(
            expected_url,
            headers={"Accept": "application/json"},
            allow_redirects=False,
            timeout=(
                configuration.connect_timeout_seconds,
                configuration.read_timeout_seconds,
            ),
            verify=True,
        )
        if (
            response.status_code != 200
            or response.url != expected_url
            or response.is_redirect
            or not _json_content_type(response.headers.get("Content-Type"))
        ):
            raise _ProviderUnavailable()
        try:
            document = response.json()
        except Exception as exc:
            _detach_exception(exc)
            raise _ProviderUnavailable() from None
        ttl = _jwks_ttl(
            response.headers.get("Cache-Control"),
            response.headers.get("Age"),
            configuration,
        )
        return document, ttl
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
        preserved_control = exc
        _detach_exception(exc)
        raise exc from None
    except _ProviderUnavailable:
        raise
    except dependencies.RequestException as exc:
        _detach_exception(exc)
        raise _ProviderUnavailable() from None
    finally:
        actions = []
        if response is not None:
            actions.append(response.close)
        actions.append(session.close)
        _cleanup_preserving_exception(
            preserved_control,
            *actions,
        )
        response = None
        session = None


def _bounded_send(
    parent_send,
    request,
    kwargs,
    *,
    expected_url,
    expected_method,
    maximum_bytes,
    response_type,
):
    if (
        request.url != expected_url
        or request.method != expected_method
        or kwargs.get("allow_redirects") is not False
        or kwargs.get("verify") is not True
    ):
        raise _ProviderResponseInvalid()
    kwargs["stream"] = True
    response = parent_send(request, **kwargs)
    if response.url != expected_url or response.history or response.is_redirect:
        _cleanup_preserving_exception(
            _ProviderResponseInvalid(),
            response.close,
        )
        raise _ProviderResponseInvalid()
    try:
        _validated_response_content_encoding(response)
    except BaseException as exc:
        _cleanup_preserving_exception(exc, response.close)
        raise
    declared = response.headers.get("Content-Length")
    if declared is not None:
        try:
            if int(declared) > maximum_bytes or int(declared) < 0:
                _cleanup_preserving_exception(
                    _ProviderResponseTooLarge(),
                    response.close,
                )
                raise _ProviderResponseTooLarge()
        except ValueError:
            _cleanup_preserving_exception(
                _ProviderResponseInvalid(),
                response.close,
            )
            raise _ProviderResponseInvalid() from None
    content = bytearray()
    try:
        for chunk in response.iter_content(chunk_size=8192):
            if not chunk:
                continue
            content.extend(chunk)
            if len(content) > maximum_bytes:
                raise _ProviderResponseTooLarge()
        bounded_response = response_type()
        bounded_response.status_code = response.status_code
        bounded_response.headers = response.headers.copy()
        bounded_response.headers.pop("Content-Encoding", None)
        bounded_response.headers["Content-Length"] = str(len(content))
        bounded_response.url = response.url
        bounded_response.history = list(response.history)
        bounded_response.reason = response.reason
        bounded_response.encoding = response.encoding
        bounded_response.request = response.request
        bounded_response.elapsed = response.elapsed
        bounded_response.raw = io.BytesIO(bytes(content))
        try:
            response.close()
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except Exception as exc:
            _detach_exception(exc)
            raise _ProviderResponseInvalid() from None
        return bounded_response
    except BaseException as exc:
        _cleanup_preserving_exception(exc, response.close)
        raise
    finally:
        _clear_buffer(content)


def _validated_response_content_encoding(response):
    try:
        declared = response.headers.get("Content-Encoding")
    except (AttributeError, TypeError):
        raise _ProviderResponseInvalid() from None
    normalized = _parsed_content_encoding(declared)
    raw_headers = getattr(getattr(response, "raw", None), "headers", None)
    if raw_headers is not None and hasattr(raw_headers, "getlist"):
        try:
            raw_values = tuple(raw_headers.getlist("Content-Encoding"))
        except (AttributeError, TypeError, ValueError):
            raise _ProviderResponseInvalid() from None
        if len(raw_values) > 1:
            raise _ProviderResponseInvalid()
        if raw_values:
            if declared is None:
                raise _ProviderResponseInvalid()
            raw_normalized = _parsed_content_encoding(raw_values[0])
            if raw_normalized != normalized:
                raise _ProviderResponseInvalid()
    if declared is None:
        return "identity"
    try:
        response.headers["Content-Encoding"] = normalized
        if raw_headers is not None:
            raw_headers["Content-Encoding"] = normalized
    except (AttributeError, TypeError, ValueError):
        raise _ProviderResponseInvalid() from None
    return normalized


def _parsed_content_encoding(value):
    if value is None:
        return "identity"
    if type(value) is not str:
        raise _ProviderResponseInvalid()
    stripped = value.strip(" \t")
    if (
        not stripped
        or "," in stripped
        or _HTTP_TOKEN.fullmatch(stripped) is None
    ):
        raise _ProviderResponseInvalid()
    normalized = stripped.casefold()
    if normalized not in _SUPPORTED_RESPONSE_CONTENT_ENCODINGS:
        raise _ProviderResponseInvalid()
    return normalized


def _validate_token_response(response, expected_url):
    if (
        response.url != expected_url
        or response.is_redirect
        or response.status_code not in {200, 400}
        or not _json_content_type(response.headers.get("Content-Type"))
    ):
        raise _ProviderResponseInvalid()
    return response


def _validated_code_id_token(
    dependencies,
    decoded,
    transaction,
    configuration,
    now,
):
    now_timestamp = int(now.timestamp())
    nonce = _buffer_text(transaction.nonce)

    def exact_numeric(_claims, value):
        return type(value) is int and value >= 0

    def validate_iat(_claims, value):
        return exact_numeric(_claims, value) and value <= (
            now_timestamp + configuration.clock_skew_seconds
        )

    def validate_nbf(_claims, value):
        return exact_numeric(_claims, value) and value <= (
            now_timestamp + configuration.clock_skew_seconds
        )

    def validate_auth_time(claims, value):
        iat = claims.get("iat")
        return (
            exact_numeric(claims, value)
            and type(iat) is int
            and value <= now_timestamp + configuration.clock_skew_seconds
            and now_timestamp - value
            <= configuration.maximum_authentication_age_seconds
            + configuration.clock_skew_seconds
            and value <= iat + configuration.clock_skew_seconds
        )

    def validate_subject(_claims, value):
        try:
            _validated_subject(value)
            return True
        except (TypeError, ValueError):
            return False

    def validate_audience(_claims, value):
        return type(value) is str and value == configuration.client_id

    def validate_azp(_claims, value):
        return type(value) is str and value == configuration.client_id

    options = {
        "iss": {"essential": True, "values": list(configuration.issuers)},
        "sub": {"essential": True, "validate": validate_subject},
        "aud": {
            "essential": True,
            "value": configuration.client_id,
            "validate": validate_audience,
        },
        "exp": {"essential": True, "validate": exact_numeric},
        "iat": {"essential": True, "validate": validate_iat},
        "nbf": {"validate": validate_nbf},
        "nonce": {"essential": True, "value": nonce},
        "azp": {"validate": validate_azp},
        "auth_time": {"essential": True, "validate": validate_auth_time},
    }
    params = {
        "client_id": configuration.client_id,
        "nonce": nonce,
        "max_age": configuration.maximum_authentication_age_seconds,
    }
    try:
        claims = dependencies.CodeIDToken(
            decoded.claims,
            decoded.header,
            options=options,
            params=params,
        )
        claims.validate(
            now=now_timestamp,
            leeway=configuration.clock_skew_seconds,
        )
        if claims["exp"] <= claims["auth_time"]:
            raise _AuthenticationDenied()
        return claims
    finally:
        nonce = None
        options = None
        params = None


def _resolve_durable_identity(connection, identity, now):
    if (
        type(connection) is not sqlite3.Connection
        or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
        or connection.execute("PRAGMA query_only").fetchone()[0] != 0
        or not attest_account_schema(connection)
    ):
        raise _Unavailable()
    subject = identity.provider_subject
    identity_rows = _rows(
        connection,
        "SELECT auth_identity_id, user_id, provider, provider_subject, "
        "verified_email, email_verified, created_at, last_authenticated_at, "
        "disabled_at, link_idempotency_key, request_fingerprint "
        "FROM auth_identities WHERE provider = ? AND provider_subject = ? "
        "ORDER BY auth_identity_id LIMIT 2",
        (_PROVIDER, subject),
    )
    if not identity_rows:
        raise _DurableIdentityMissing()
    if len(identity_rows) != 1:
        raise _Unavailable()
    identity_row = identity_rows[0]
    if not authoritative_auth_identity_row_valid(identity_row):
        raise _Unavailable()
    if (
        identity_row["provider"] != _PROVIDER
        or identity_row["provider_subject"] != subject
    ):
        raise _Unavailable()
    if identity_row["disabled_at"] is not None:
        raise _AuthenticationDenied()
    account_rows = _rows(
        connection,
        "SELECT user_id, lifecycle_status, row_version, created_at, updated_at, "
        "deletion_requested_at, deactivated_at FROM users WHERE user_id = ? "
        "ORDER BY user_id LIMIT 2",
        (identity_row["user_id"],),
    )
    if not account_rows:
        raise _AuthenticationDenied()
    if len(account_rows) != 1:
        raise _Unavailable()
    account = account_rows[0]
    if not authoritative_account_row_valid(
        account,
        expected_user_id=identity_row["user_id"],
    ):
        raise _Unavailable()
    if account["lifecycle_status"] != "active":
        raise _AuthenticationDenied()
    if not authoritative_auth_identity_row_valid(
        identity_row,
        expected_user_id=account["user_id"],
        account_created_at=account["created_at"],
    ):
        raise _Unavailable()
    account_created = _database_time(account["created_at"])
    account_updated = _database_time(account["updated_at"])
    identity_created = _database_time(identity_row["created_at"])
    last_authenticated = _database_time(identity_row["last_authenticated_at"])
    if (
        account_created > now
        or account_updated > now
        or identity_created < account_created
        or last_authenticated < identity_created
        or last_authenticated > now
    ):
        raise _Unavailable()
    if identity_created > identity.authenticated_at:
        raise _Unavailable()
    return _ResolvedDurableIdentity(
        account_id=account["user_id"],
        identity_id=identity_row["auth_identity_id"],
    )


def _resolve_or_provision_durable_identity(
    connection,
    gateway_record,
    lookup_identity,
    projection,
    now,
    *,
    invitation_credential,
    idempotency_key,
):
    try:
        return _resolve_durable_identity(connection, lookup_identity, now)
    except _DurableIdentityMissing:
        pass

    if (
        type(gateway_record) is not _GatewayRecord
        or type(projection) is not tuple
        or len(projection) != 6
        or type(projection[4]) is not str
        or projection[5] is not True
        or type(invitation_credential) is not bytearray
        or not invitation_credential
        or type(idempotency_key) is not str
    ):
        raise _AuthenticationDenied()
    invitation_key = gateway_record.invitation_lookup_key
    if type(invitation_key) is not bytearray or not invitation_key:
        raise _Unavailable()
    try:
        invitation_token = bytes(invitation_credential).decode(
            "ascii",
            "strict",
        )
    except UnicodeError:
        raise _AuthenticationDenied() from None

    provisioning_identity = None
    invitation_lookup_key = None
    try:
        try:
            provisioning_identity = (
                gateway_record.identity_verifier.from_validated_google_claims(
                    provider_subject=projection[0],
                    verified_email=projection[4],
                    email_verified=True,
                    authenticated_at=now,
                    metadata_version=_METADATA_VERSION,
                )
            )
        except (AuthenticationUnavailable, InvalidAccountInput):
            raise _AuthenticationDenied() from None
        if not gateway_record.identity_verifier._accepts(
            provisioning_identity
        ):
            raise _Unavailable()
        invitation_lookup_key = bytes(invitation_key)
        try:
            gateway_record.account_service.create_invited_user(
                connection,
                identity=provisioning_identity,
                invitation_token=invitation_token,
                invitation_lookup_key=invitation_lookup_key,
                idempotency_key=idempotency_key,
                now=now,
            )
        except AuthenticationUnavailable:
            raise _AuthenticationDenied() from None
        if connection.in_transaction:
            raise _Unavailable()
        return _resolve_durable_identity(
            connection,
            provisioning_identity,
            now,
        )
    finally:
        invitation_token = None
        invitation_lookup_key = None
        provisioning_identity = None


def _rows(connection, sql, parameters):
    cursor = connection.execute(sql, parameters)
    names = tuple(item[0] for item in cursor.description)
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _credential_text(credential, configuration_authority):
    try:
        record = object.__getattribute__(credential, "_record")
    except (AttributeError, TypeError):
        raise _Unavailable() from None
    if (
        type(credential) is not _GoogleClientCredential
        or type(record) is not _CredentialRecord
        or record.closed
        or record.configuration_authority is not configuration_authority
        or not record.secret_buffer
    ):
        raise _Unavailable()
    actual = hashlib.sha256(
        b"wahojobs-google-oidc-credential-v1\x00"
        + bytes(record.secret_buffer)
    ).digest()
    if not hmac.compare_digest(actual, record.digest):
        raise _Unavailable()
    try:
        return bytes(record.secret_buffer).decode("utf-8", "strict")
    except UnicodeError:
        raise _Unavailable() from None


def _close_credential(credential):
    try:
        record = object.__getattribute__(credential, "_record")
    except (AttributeError, TypeError):
        return
    if type(record) is not _CredentialRecord or record.closed:
        return
    _clear_buffer(record.secret_buffer)
    record.digest = b""
    record.configuration_authority = None
    record.closed = True


def _validated_client_id(value):
    if (
        type(value) is not str
        or value != value.strip()
        or not (8 <= len(value) <= 256)
        or _CONTROL_CHARACTERS.search(value) is not None
    ):
        raise TypeError("google_oidc_client_id_invalid")
    return value


def _validated_redirect_uri(value):
    if (
        type(value) is not str
        or value != value.strip()
        or len(value) > 2048
        or _CONTROL_CHARACTERS.search(value) is not None
    ):
        raise TypeError("google_oidc_redirect_uri_invalid")
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.netloc
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise TypeError("google_oidc_redirect_uri_invalid")
    return value


def _validated_environment(value):
    if type(value) is not str or value not in _ENVIRONMENTS:
        raise TypeError("google_oidc_environment_invalid")
    return value


def _validated_subject(value):
    if (
        type(value) is not str
        or value != value.strip()
        or not (1 <= len(value) <= _SUBJECT_LIMIT)
        or _CONTROL_CHARACTERS.search(value) is not None
    ):
        raise ValueError("google_oidc_subject_invalid")
    return value


def _canonical_time(value):
    if type(value) is not datetime or value.tzinfo is None:
        raise TypeError("google_oidc_time_invalid")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _database_time(value):
    if type(value) is not str:
        raise _Unavailable()
    try:
        parsed = datetime.fromisoformat(value)
        canonical = _canonical_time(parsed)
    except (TypeError, ValueError):
        raise _Unavailable() from None
    if canonical.isoformat() != value:
        raise _Unavailable()
    return canonical


def _clock_now(_configuration):
    return datetime.now(timezone.utc).replace(microsecond=0)


def _monotonic_now(_configuration):
    value = time.monotonic()
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        raise _ProviderUnavailable()
    return float(value)


def _jwks_ttl(cache_control, age_header, configuration):
    ttl_values = []
    malformed_max_age = False
    if type(cache_control) is str:
        for directive in cache_control.split(","):
            name, separator, value = directive.strip().partition("=")
            name = name.casefold()
            if name in {"no-cache", "no-store"}:
                return 0
            if name != "max-age":
                continue
            value = value.strip()
            if (
                len(value) >= 2
                and value[0] == value[-1] == '"'
            ):
                value = value[1:-1]
            if separator and value.isdigit():
                ttl_values.append(int(value))
            else:
                malformed_max_age = True
    ttl = configuration.jwks_fallback_ttl_seconds
    if ttl_values:
        ttl = min(ttl_values)
        if malformed_max_age:
            ttl = min(ttl, configuration.jwks_fallback_ttl_seconds)
    ttl = min(ttl, configuration.jwks_max_ttl_seconds)
    if age_header is not None:
        if (
            type(age_header) is not str
            or not age_header.strip().isdigit()
        ):
            return 0
        ttl -= int(age_header.strip())
    return max(0, ttl)


def _json_content_type(value):
    return (
        type(value) is str
        and value.split(";", 1)[0].strip().casefold() == "application/json"
    )


def _buffer_text(value):
    if type(value) is not bytearray or not value:
        raise _InvalidTransaction()
    try:
        return bytes(value).decode("ascii", "strict")
    except UnicodeError:
        raise _InvalidTransaction() from None


def _clear_buffer(value):
    if type(value) is bytearray:
        value[:] = b"\x00" * len(value)
        value.clear()


def _clear_oauth_session_token(session):
    token = session.token
    if isinstance(token, dict):
        token.clear()
    session.token = {}


def _cleanup_preserving_exception(preserved_exception, *actions):
    failures = []
    preserved_is_control = isinstance(
        preserved_exception,
        (KeyboardInterrupt, SystemExit, GeneratorExit),
    )
    for action in actions:
        try:
            action()
        except BaseException as exc:
            cleanup_is_control = isinstance(
                exc,
                (KeyboardInterrupt, SystemExit, GeneratorExit),
            )
            if preserved_is_control:
                _detach_exception(exc)
            elif cleanup_is_control or preserved_exception is None:
                failures.append(exc)
            else:
                _detach_exception(exc)
    if failures:
        selected = next(
            (
                exc
                for exc in failures
                if isinstance(
                    exc,
                    (KeyboardInterrupt, SystemExit, GeneratorExit),
                )
            ),
            failures[0],
        )
        for exc in failures:
            if exc is not selected:
                _detach_exception(exc)
        if preserved_exception is not None:
            _detach_exception(preserved_exception)
        _detach_exception(selected)
        raise selected from None


def _failure(status):
    return GoogleOidcGatewayFailure._issue(
        _FAILURE_ISSUANCE_CAPABILITY,
        status,
    )


def _detach_exception(exc):
    try:
        exc.__traceback__ = None
        exc.__cause__ = None
        exc.__context__ = None
    except Exception:
        pass


__all__ = (
    "GoogleOidcAuthorizationTransaction",
    "GoogleOidcGateway",
    "GoogleOidcGatewayFailure",
    "PreparedGoogleOidcAuthorization",
    "TrustedGoogleOidcConfiguration",
)

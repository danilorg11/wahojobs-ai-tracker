"""Dormant trusted login completion over Migration-002 browser sessions."""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import hmac
import json
import re
import sqlite3
import threading
import weakref

from wahojobs.account_reconciliation import (
    authoritative_account_row_valid,
    authoritative_auth_identity_row_valid,
)
from wahojobs.accounts import PROVIDERS
from wahojobs.browser_session_lifecycle import (
    MAX_ABSOLUTE_TTL,
    MAX_IDLE_TTL,
    MIN_ABSOLUTE_TTL,
    MIN_IDLE_TTL,
    BrowserSessionLifecycleError,
    CreateBrowserSessionCommand,
    IssuedBrowserSession,
    RequestScopedSessionSecretVault,
    SessionDeliveryLease,
    _ACCOUNT_ID,
    _COMMAND_ISSUANCE_CAPABILITY,
    _IDENTITY_ID,
    _RESPONSE_COMPOSITION_CAPABILITY,
    abort_request_scoped_secret_vault,
    _attest_mutation_connection,
    _mutation_scope,
    _require_secret_vault,
    _trusted_time,
    _validated_id,
    _validated_idempotency_key,
    _validated_ttl,
    compensate_undelivered_issued_session,
    create_browser_session,
    emergency_terminalize_request_scoped_secret_vault,
    finalize_pending_issued_session,
    force_compensate_undelivered_issued_session,
    prepare_issued_session_delivery,
    terminalize_undelivered_issued_result,
    verify_compensated_undelivered_issued_session,
    verify_request_scoped_secret_vault_terminal,
)
from wahojobs.ownership import validate_environment_namespace


_ASSURANCE_POLICY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TRUSTED_LOGIN_ENVIRONMENTS = frozenset(
    {"development", "test", "private_beta"}
)
_ASSERTION_ISSUANCE_CAPABILITY = object()
_ASSERTION_SERVICE_CAPABILITY = object()
_COMPLETION_POLICY_ISSUANCE_CAPABILITY = object()
_COMPLETION_POLICY_SERVICE_CAPABILITY = object()
_VALIDATED_LOGIN_CAPABILITY = object()
_RESULT_ISSUANCE_CAPABILITY = object()
_RESULT_SERVICE_CAPABILITY = object()
# These registries authenticate only the accepted pre-completion proof chain.
_ISSUED_ASSERTIONS = weakref.WeakKeyDictionary()
_ISSUED_COMPLETION_POLICIES = weakref.WeakKeyDictionary()
_VALIDATED_LOGINS = weakref.WeakSet()
_RUNTIME_EXPECTED_PROVIDER = "google"
_RUNTIME_ASSURANCE_POLICY_VERSION = "google_oidc_v1"
_RUNTIME_COMPLETION_POLICY_VERSION = "trusted_login_completion_v1"

_OUTCOMES = frozenset(
    {
        "issued",
        "pending_commit",
        "already_completed",
        "authentication_denied",
        "unavailable",
        "idempotency_conflict",
    }
)


def _configuration_error() -> TypeError:
    return TypeError("trusted_login_completion_configuration_invalid")


class TrustedExternalIdentityAuthentication:
    """Sealed proof that a trusted gateway authenticated one durable identity."""

    __slots__ = (
        "_account_id",
        "_identity_id",
        "_provider",
        "_authenticated_at",
        "_expires_at",
        "_assurance_policy_version",
        "_environment_namespace",
        "__weakref__",
    )

    def __new__(cls, *_args, **_kwargs):
        raise TypeError("trusted_external_authentication_required")

    @classmethod
    def _issue(
        cls,
        capability,
        *,
        account_id,
        identity_id,
        provider,
        authenticated_at,
        expires_at,
        assurance_policy_version,
        environment_namespace,
    ):
        if cls is not TrustedExternalIdentityAuthentication or capability is not _ASSERTION_ISSUANCE_CAPABILITY:
            raise TypeError("trusted_external_authentication_required")
        try:
            account_id = _validated_id(account_id, _ACCOUNT_ID)
            identity_id = _validated_id(identity_id, _IDENTITY_ID)
            authenticated_at = _trusted_time(authenticated_at)
            expires_at = _trusted_time(expires_at)
            environment_namespace = _validated_login_environment(
                environment_namespace
            )
        except (TypeError, ValueError):
            raise TypeError("trusted_external_authentication_invalid") from None
        if (
            type(provider) is not str
            or provider not in PROVIDERS
            or type(assurance_policy_version) is not str
            or _ASSURANCE_POLICY.fullmatch(assurance_policy_version) is None
            or expires_at <= authenticated_at
        ):
            raise TypeError("trusted_external_authentication_invalid")
        instance = object.__new__(cls)
        object.__setattr__(instance, "_account_id", account_id)
        object.__setattr__(instance, "_identity_id", identity_id)
        object.__setattr__(instance, "_provider", provider)
        object.__setattr__(instance, "_authenticated_at", authenticated_at)
        object.__setattr__(instance, "_expires_at", expires_at)
        object.__setattr__(
            instance,
            "_assurance_policy_version",
            assurance_policy_version,
        )
        object.__setattr__(instance, "_environment_namespace", environment_namespace)
        _ISSUED_ASSERTIONS[instance] = _assertion_attestation(
            account_id=account_id,
            identity_id=identity_id,
            provider=provider,
            authenticated_at=authenticated_at,
            expires_at=expires_at,
            assurance_policy_version=assurance_policy_version,
            environment_namespace=environment_namespace,
        )
        return instance

    def _values_for_service(self, capability):
        if (
            capability is not _ASSERTION_SERVICE_CAPABILITY
            or type(self) is not TrustedExternalIdentityAuthentication
        ):
            raise TypeError("trusted_external_authentication_required")
        values = {
            "account_id": self._account_id,
            "identity_id": self._identity_id,
            "provider": self._provider,
            "authenticated_at": self._authenticated_at,
            "expires_at": self._expires_at,
            "assurance_policy_version": self._assurance_policy_version,
            "environment_namespace": self._environment_namespace,
        }
        try:
            expected = _ISSUED_ASSERTIONS[self]
            actual = _assertion_attestation(**values)
        except Exception:
            raise TypeError("trusted_external_authentication_required") from None
        if not hmac.compare_digest(actual, expected):
            raise TypeError("trusted_external_authentication_required")
        return values

    def __setattr__(self, _name, _value):
        raise AttributeError("trusted_external_authentication_is_immutable")

    def __repr__(self):
        return "TrustedExternalIdentityAuthentication(<redacted>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("trusted_external_authentication_not_serializable")

    def __copy__(self):
        raise TypeError("trusted_external_authentication_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("trusted_external_authentication_not_copyable")

    def __init_subclass__(cls, **_kwargs):
        raise TypeError("trusted_external_authentication_not_subclassable")


class TrustedLoginSessionPolicy:
    """Explicit trusted composition for environment and session lifetime."""

    __slots__ = (
        "_environment_namespace",
        "_idle_ttl",
        "_absolute_ttl",
    )

    def __init__(self, *, environment_namespace, idle_ttl, absolute_ttl):
        try:
            environment_namespace = _validated_login_environment(
                environment_namespace
            )
            idle_ttl = _validated_ttl(
                idle_ttl,
                minimum=MIN_IDLE_TTL,
                maximum=MAX_IDLE_TTL,
            )
            absolute_ttl = _validated_ttl(
                absolute_ttl,
                minimum=MIN_ABSOLUTE_TTL,
                maximum=MAX_ABSOLUTE_TTL,
            )
        except (TypeError, ValueError):
            raise _configuration_error() from None
        if idle_ttl > absolute_ttl:
            raise _configuration_error()
        object.__setattr__(self, "_environment_namespace", environment_namespace)
        object.__setattr__(self, "_idle_ttl", idle_ttl)
        object.__setattr__(self, "_absolute_ttl", absolute_ttl)

    def __setattr__(self, _name, _value):
        raise AttributeError("trusted_login_session_policy_is_immutable")

    def __repr__(self):
        return "TrustedLoginSessionPolicy(<configured>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("trusted_login_session_policy_not_serializable")

    def __copy__(self):
        raise TypeError("trusted_login_session_policy_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("trusted_login_session_policy_not_copyable")

    def __init_subclass__(cls, **_kwargs):
        raise TypeError("trusted_login_session_policy_not_subclassable")


class TrustedLoginCompletionPolicy:
    """Sealed trusted composition for assurance, environment, and session policy."""

    __slots__ = (
        "_expected_provider",
        "_expected_assurance_policy_version",
        "_environment_namespace",
        "_completion_policy_version",
        "_idle_ttl",
        "_absolute_ttl",
        "__weakref__",
    )

    def __new__(cls, *_args, **_kwargs):
        raise TypeError("trusted_login_completion_policy_required")

    @classmethod
    def _issue(
        cls,
        capability,
        *,
        expected_provider,
        expected_assurance_policy_version,
        environment_namespace,
        completion_policy_version,
        session_policy,
    ):
        if (
            cls is not TrustedLoginCompletionPolicy
            or capability is not _COMPLETION_POLICY_ISSUANCE_CAPABILITY
        ):
            raise TypeError("trusted_login_completion_policy_required")
        try:
            session_values = _validated_session_policy_values(session_policy)
            environment_namespace = _validated_login_environment(
                environment_namespace
            )
        except Exception:
            raise TypeError("trusted_login_completion_policy_invalid") from None
        if (
            type(expected_provider) is not str
            or expected_provider not in PROVIDERS
            or type(expected_assurance_policy_version) is not str
            or _ASSURANCE_POLICY.fullmatch(expected_assurance_policy_version) is None
            or type(completion_policy_version) is not str
            or _ASSURANCE_POLICY.fullmatch(completion_policy_version) is None
            or session_values["environment_namespace"] != environment_namespace
        ):
            raise TypeError("trusted_login_completion_policy_invalid")
        instance = object.__new__(cls)
        values = {
            "expected_provider": expected_provider,
            "expected_assurance_policy_version": expected_assurance_policy_version,
            "environment_namespace": environment_namespace,
            "completion_policy_version": completion_policy_version,
            "idle_ttl": session_values["idle_ttl"],
            "absolute_ttl": session_values["absolute_ttl"],
        }
        for name, value in values.items():
            object.__setattr__(instance, f"_{name}", value)
        _ISSUED_COMPLETION_POLICIES[instance] = _completion_policy_attestation(
            **values
        )
        return instance

    def _values_for_service(self, capability):
        if (
            capability is not _COMPLETION_POLICY_SERVICE_CAPABILITY
            or type(self) is not TrustedLoginCompletionPolicy
        ):
            raise TypeError("trusted_login_completion_policy_required")
        values = {
            "expected_provider": self._expected_provider,
            "expected_assurance_policy_version": (
                self._expected_assurance_policy_version
            ),
            "environment_namespace": self._environment_namespace,
            "completion_policy_version": self._completion_policy_version,
            "idle_ttl": self._idle_ttl,
            "absolute_ttl": self._absolute_ttl,
        }
        try:
            expected = _ISSUED_COMPLETION_POLICIES[self]
            actual = _completion_policy_attestation(**values)
        except Exception:
            raise TypeError("trusted_login_completion_policy_required") from None
        if not hmac.compare_digest(actual, expected):
            raise TypeError("trusted_login_completion_policy_required")
        return values

    def __setattr__(self, _name, _value):
        raise AttributeError("trusted_login_completion_policy_is_immutable")

    def __repr__(self):
        return "TrustedLoginCompletionPolicy(<configured>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("trusted_login_completion_policy_not_serializable")

    def __copy__(self):
        raise TypeError("trusted_login_completion_policy_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("trusted_login_completion_policy_not_copyable")

    def __init_subclass__(cls, **_kwargs):
        raise TypeError("trusted_login_completion_policy_not_subclassable")


class _TrustedLoginCompletionAuthoritySeal:
    """Owner-bound seal for one completion authority."""

    __slots__ = ("_owner", "_capability")

    def __new__(cls, *_args, **_kwargs):
        raise TypeError("trusted_login_completion_authority_not_constructible")

    @classmethod
    def _issue(cls, capability, owner):
        if (
            cls is not _TrustedLoginCompletionAuthoritySeal
            or capability is not _RESULT_ISSUANCE_CAPABILITY
        ):
            raise TypeError("trusted_login_completion_authority_not_constructible")
        instance = object.__new__(cls)
        object.__setattr__(instance, "_owner", weakref.ref(owner))
        object.__setattr__(instance, "_capability", capability)
        return instance

    def _owns(self, owner):
        return (
            type(self) is _TrustedLoginCompletionAuthoritySeal
            and self._capability is _RESULT_ISSUANCE_CAPABILITY
            and self._owner() is owner
        )

    def __setattr__(self, _name, _value):
        raise AttributeError("trusted_login_completion_authority_is_immutable")

    def __repr__(self):
        return "_TrustedLoginCompletionAuthoritySeal(<redacted>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("trusted_login_completion_authority_not_serializable")

    def __copy__(self):
        raise TypeError("trusted_login_completion_authority_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("trusted_login_completion_authority_not_copyable")

    def __init_subclass__(cls, **_kwargs):
        raise TypeError("trusted_login_completion_authority_not_subclassable")


class _TrustedLoginCompletionAuthority:
    """Private one-shot authority bound to one result, session, and request vault."""

    __slots__ = (
        "_owner",
        "_issued_session",
        "_request_secret_vault",
        "_state",
        "_lock",
        "_seal",
        "__weakref__",
    )

    def __new__(cls, *_args, **_kwargs):
        raise TypeError("trusted_login_completion_authority_not_constructible")

    @classmethod
    def _issue(
        cls,
        capability,
        *,
        owner,
        issued_session,
        request_secret_vault,
    ):
        has_delivery_owner = (
            type(issued_session) is IssuedBrowserSession
            and issued_session._is_sealed()
            and type(request_secret_vault) is RequestScopedSessionSecretVault
        )
        if (
            cls is not _TrustedLoginCompletionAuthority
            or capability is not _RESULT_ISSUANCE_CAPABILITY
            or type(owner) is not TrustedLoginCompletionResult
            or (
                (issued_session is None and request_secret_vault is None)
                == has_delivery_owner
            )
        ):
            raise TypeError("trusted_login_completion_authority_not_constructible")
        instance = object.__new__(cls)
        object.__setattr__(instance, "_owner", weakref.ref(owner))
        object.__setattr__(instance, "_issued_session", issued_session)
        object.__setattr__(
            instance,
            "_request_secret_vault",
            request_secret_vault,
        )
        object.__setattr__(instance, "_state", "available")
        object.__setattr__(instance, "_lock", threading.Lock())
        object.__setattr__(
            instance,
            "_seal",
            _TrustedLoginCompletionAuthoritySeal._issue(
                _RESULT_ISSUANCE_CAPABILITY,
                instance,
            ),
        )
        return instance

    def _owns(self, owner, issued_session):
        seal = getattr(self, "_seal", None)
        return (
            type(self) is _TrustedLoginCompletionAuthority
            and type(seal) is _TrustedLoginCompletionAuthoritySeal
            and seal._owns(self)
            and self._owner() is owner
            and self._issued_session is issued_session
        )

    def _claim(self, capability, owner, request_secret_vault, operation):
        expected_status = (
            "issued"
            if operation == "delivery"
            else "pending_commit"
            if operation == "finalize"
            else None
        )
        issued_session = getattr(owner, "_issued_session", None)
        if (
            capability is not _RESULT_SERVICE_CAPABILITY
            or expected_status is None
            or not self._owns(owner, issued_session)
            or type(issued_session) is not IssuedBrowserSession
            or not issued_session._is_sealed()
            or type(request_secret_vault) is not RequestScopedSessionSecretVault
            or self._request_secret_vault is not request_secret_vault
        ):
            raise BrowserSessionLifecycleError("session_state_conflict")
        with self._lock:
            if not self._owns(owner, issued_session):
                raise BrowserSessionLifecycleError("session_state_conflict")
            if self._state != "available":
                raise BrowserSessionLifecycleError("already_completed")
            if (
                self._request_secret_vault is not request_secret_vault
                or getattr(owner, "_status", None) != expected_status
                or issued_session.status != expected_status
            ):
                raise BrowserSessionLifecycleError("session_state_conflict")
            object.__setattr__(self, "_state", operation)
            return issued_session

    def __setattr__(self, _name, _value):
        raise AttributeError("trusted_login_completion_authority_is_immutable")

    def __repr__(self):
        return "_TrustedLoginCompletionAuthority(<redacted>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("trusted_login_completion_authority_not_serializable")

    def __copy__(self):
        raise TypeError("trusted_login_completion_authority_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("trusted_login_completion_authority_not_copyable")

    def __init_subclass__(cls, **_kwargs):
        raise TypeError("trusted_login_completion_authority_not_subclassable")


class TrustedLoginCompletionResult:
    """Immutable sanitized outcome with an optional accepted nonsecret result."""

    __slots__ = ("_status", "_issued_session", "_authority", "__weakref__")

    def __new__(cls, *_args, **_kwargs):
        raise TypeError("trusted_login_completion_result_not_constructible")

    @classmethod
    def _issue(
        cls,
        capability,
        status,
        issued_session=None,
        request_secret_vault=None,
    ):
        success = status in {"issued", "pending_commit", "already_completed"}
        if (
            cls is not TrustedLoginCompletionResult
            or capability is not _RESULT_ISSUANCE_CAPABILITY
            or status not in _OUTCOMES
            or success != (type(issued_session) is IssuedBrowserSession)
            or success
            != (type(request_secret_vault) is RequestScopedSessionSecretVault)
            or (success and issued_session.status != status)
        ):
            raise TypeError("trusted_login_completion_result_not_constructible")
        instance = object.__new__(cls)
        object.__setattr__(instance, "_status", status)
        object.__setattr__(instance, "_issued_session", issued_session)
        object.__setattr__(
            instance,
            "_authority",
            _TrustedLoginCompletionAuthority._issue(
                _RESULT_ISSUANCE_CAPABILITY,
                owner=instance,
                issued_session=issued_session,
                request_secret_vault=request_secret_vault,
            ),
        )
        return instance

    @property
    def status(self):
        self._require_sealed()
        return self._status

    @property
    def issued_session(self):
        self._require_sealed()
        return self._issued_session

    def _claim_issued_session(self, capability, request_secret_vault, operation):
        self._require_sealed()
        return self._authority._claim(
            capability,
            self,
            request_secret_vault,
            operation,
        )

    def _prepare_delivery(
        self,
        capability,
        connection,
        request_secret_vault,
        now,
    ):
        issued_session = self._claim_issued_session(
            capability,
            request_secret_vault,
            "delivery",
        )
        try:
            return prepare_issued_session_delivery(
                connection,
                issued_session,
                request_secret_vault,
                _RESPONSE_COMPOSITION_CAPABILITY,
                now=now,
            )
        finally:
            connection = None
            issued_session = None
            request_secret_vault = None

    def _is_sealed(self):
        authority = getattr(self, "_authority", None)
        issued_session = getattr(self, "_issued_session", None)
        return (
            type(self) is TrustedLoginCompletionResult
            and type(authority) is _TrustedLoginCompletionAuthority
            and authority._owns(self, issued_session)
        )

    def _require_sealed(self):
        if not self._is_sealed():
            raise BrowserSessionLifecycleError("session_state_conflict")

    def __setattr__(self, _name, _value):
        raise AttributeError("trusted_login_completion_result_is_immutable")

    def __repr__(self):
        try:
            status = self.status
        except Exception:
            status = "invalid"
        return f"TrustedLoginCompletionResult(status={status!r})"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("trusted_login_completion_result_not_serializable")

    def __copy__(self):
        raise TypeError("trusted_login_completion_result_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("trusted_login_completion_result_not_copyable")

    def __init_subclass__(cls, **_kwargs):
        raise TypeError("trusted_login_completion_result_not_subclassable")


class _ValidatedTrustedLogin:
    """One-use proof that every login prerequisite passed in the transaction."""

    __slots__ = (
        "account_id",
        "identity_id",
        "accepted_at",
        "idle_ttl",
        "absolute_ttl",
        "idempotency_key",
        "__weakref__",
    )

    def __new__(cls, *_args, **_kwargs):
        raise _configuration_error()

    @classmethod
    def _issue(cls, capability, **values):
        if cls is not _ValidatedTrustedLogin or capability is not _VALIDATED_LOGIN_CAPABILITY:
            raise _configuration_error()
        instance = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(instance, name, value)
        _VALIDATED_LOGINS.add(instance)
        return instance

    def __setattr__(self, _name, _value):
        raise AttributeError("validated_trusted_login_is_immutable")


class _AuthenticationDenied(Exception):
    pass


class _Unavailable(Exception):
    pass


class _IdempotencyConflict(Exception):
    pass


def create_trusted_login_completion_policy(
    *,
    environment_namespace: str,
    idle_ttl: timedelta,
    absolute_ttl: timedelta,
) -> TrustedLoginCompletionPolicy:
    """Issue the fixed Google trusted-login policy for runtime composition."""

    session_policy = TrustedLoginSessionPolicy(
        environment_namespace=environment_namespace,
        idle_ttl=idle_ttl,
        absolute_ttl=absolute_ttl,
    )
    return TrustedLoginCompletionPolicy._issue(
        _COMPLETION_POLICY_ISSUANCE_CAPABILITY,
        expected_provider=_RUNTIME_EXPECTED_PROVIDER,
        expected_assurance_policy_version=(
            _RUNTIME_ASSURANCE_POLICY_VERSION
        ),
        environment_namespace=environment_namespace,
        completion_policy_version=_RUNTIME_COMPLETION_POLICY_VERSION,
        session_policy=session_policy,
    )


def prepare_session_delivery(
    connection: sqlite3.Connection,
    completion_result: TrustedLoginCompletionResult,
    request_secret_vault: RequestScopedSessionSecretVault,
    *,
    now: datetime,
) -> SessionDeliveryLease:
    """Prepare one compensatable browser response from a completed login."""

    lease = None
    failure_code = None
    control_flow = None
    try:
        if type(completion_result) is not TrustedLoginCompletionResult:
            raise BrowserSessionLifecycleError("session_state_conflict")
        lease = completion_result._prepare_delivery(
            _RESULT_SERVICE_CAPABILITY,
            connection,
            request_secret_vault,
            now,
        )
    except BrowserSessionLifecycleError as exc:
        failure_code = exc.code
        _detach_exception(exc)
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
        control_flow = exc
        _detach_exception(exc)
    except Exception as exc:
        failure_code = "internal_consistency_failure"
        _detach_exception(exc)
    finally:
        connection = None
        completion_result = None
        request_secret_vault = None
        now = None
    if control_flow is not None:
        propagated = control_flow
        control_flow = None
        raise propagated from None
    if failure_code is not None:
        raise BrowserSessionLifecycleError(failure_code) from None
    return lease


def complete_trusted_login(
    connection: sqlite3.Connection,
    trusted_assertion: TrustedExternalIdentityAuthentication,
    completion_policy: TrustedLoginCompletionPolicy,
    request_secret_vault: RequestScopedSessionSecretVault,
    *,
    trusted_now: datetime,
    idempotency_key: str,
    _failure_injector=None,
) -> TrustedLoginCompletionResult:
    """Validate one trusted proof and create its M002 session atomically."""

    issued_session = None
    returned = False
    committed_creation = False
    try:
        values = _validated_assertion_values(trusted_assertion, trusted_now)
        policy = _validated_completion_policy_values(completion_policy)
        try:
            _require_secret_vault(request_secret_vault)
            idempotency_key = _validated_idempotency_key(idempotency_key)
        except Exception:
            raise _Unavailable() from None
        scope_prefix, bound_key = _bound_idempotency_key(
            values,
            policy,
            idempotency_key,
        )
        service_owned_transaction = not connection.in_transaction
        with _mutation_scope(connection):
            _attest_mutation_connection(connection)
            _validate_durable_login_state(connection, values, trusted_now)
            _require_idempotency_binding(connection, scope_prefix, bound_key)
            _require_policy_coherence(values, policy)
            _inject(_failure_injector, "before_command_issuance")
            validated = _ValidatedTrustedLogin._issue(
                _VALIDATED_LOGIN_CAPABILITY,
                account_id=values["account_id"],
                identity_id=values["identity_id"],
                accepted_at=values["authenticated_at"],
                idle_ttl=policy["idle_ttl"],
                absolute_ttl=policy["absolute_ttl"],
                idempotency_key=bound_key,
            )
            issued_session = _issue_create_session_command_and_execute(
                connection,
                validated,
                request_secret_vault,
                trusted_now,
                _failure_injector,
            )
            _inject(_failure_injector, "after_session_creation")
        committed_creation = service_owned_transaction
        if service_owned_transaction and issued_session.status == "pending_commit":
            issued_session = _finalize_pending_with_retry(
                connection,
                issued_session,
                request_secret_vault,
            )
        expected_status = "issued" if service_owned_transaction else "pending_commit"
        if issued_session.status == "already_completed":
            expected_status = "already_completed"
        if issued_session.status != expected_status:
            raise _Unavailable()
        result = TrustedLoginCompletionResult._issue(
            _RESULT_ISSUANCE_CAPABILITY,
            expected_status,
            issued_session,
            request_secret_vault,
        )
        returned = True
        return result
    except _AuthenticationDenied:
        return _failure_result("authentication_denied")
    except _IdempotencyConflict:
        return _failure_result("idempotency_conflict")
    except BrowserSessionLifecycleError as exc:
        code = exc.code
        _detach_exception(exc)
        if code == "ineligible_account_or_identity":
            return _failure_result("authentication_denied")
        if code == "idempotency_conflict":
            return _failure_result("idempotency_conflict")
        return _failure_result("unavailable")
    except sqlite3.OperationalError as exc:
        _detach_exception(exc)
        return _failure_result("unavailable")
    except Exception as exc:
        _detach_exception(exc)
        return _failure_result("unavailable")
    finally:
        if issued_session is not None and not returned:
            _establish_failure_postconditions(
                connection,
                issued_session,
                request_secret_vault,
                trusted_now,
                compensate=(
                    committed_creation
                    and issued_session.status in {"pending_commit", "issued"}
                ),
            )
        trusted_assertion = None
        completion_policy = None
        connection = None
        request_secret_vault = None
        _failure_injector = None


def finalize_pending_trusted_login(
    connection: sqlite3.Connection,
    completion_result: TrustedLoginCompletionResult,
    request_secret_vault: RequestScopedSessionSecretVault,
    *,
    trusted_now: datetime,
) -> TrustedLoginCompletionResult:
    """Finalize a caller-owned transaction only after its outer commit."""

    issued_session = None
    returned = False
    may_compensate = False
    try:
        if type(completion_result) is not TrustedLoginCompletionResult:
            raise _Unavailable()
        issued_session = completion_result._claim_issued_session(
            _RESULT_SERVICE_CAPABILITY,
            request_secret_vault,
            "finalize",
        )
        trusted_now = _trusted_time(trusted_now)
        may_compensate = (
            type(connection) is sqlite3.Connection
            and not connection.in_transaction
        )
        issued_session = _finalize_pending_with_retry(
            connection,
            issued_session,
            request_secret_vault,
        )
        result = TrustedLoginCompletionResult._issue(
            _RESULT_ISSUANCE_CAPABILITY,
            "issued",
            issued_session,
            request_secret_vault,
        )
        returned = True
        return result
    except Exception as exc:
        _detach_exception(exc)
        return _failure_result("unavailable")
    finally:
        if type(issued_session) is IssuedBrowserSession and not returned:
            _establish_failure_postconditions(
                connection,
                issued_session,
                request_secret_vault,
                trusted_now,
                compensate=(
                    may_compensate
                    and issued_session.status in {"pending_commit", "issued"}
                ),
            )
        connection = None
        completion_result = None
        request_secret_vault = None
        trusted_now = None


def _validated_assertion_values(assertion, trusted_now):
    try:
        trusted_now = _trusted_time(trusted_now)
    except (TypeError, ValueError):
        raise _Unavailable() from None
    if type(assertion) is not TrustedExternalIdentityAuthentication:
        raise _AuthenticationDenied()
    try:
        values = assertion._values_for_service(_ASSERTION_SERVICE_CAPABILITY)
        values["account_id"] = _validated_id(values["account_id"], _ACCOUNT_ID)
        values["identity_id"] = _validated_id(values["identity_id"], _IDENTITY_ID)
        values["authenticated_at"] = _trusted_time(values["authenticated_at"])
        values["expires_at"] = _trusted_time(values["expires_at"])
        values["environment_namespace"] = _validated_login_environment(
            values["environment_namespace"]
        )
    except Exception:
        raise _AuthenticationDenied() from None
    if (
        type(values["provider"]) is not str
        or values["provider"] not in PROVIDERS
        or type(values["assurance_policy_version"]) is not str
        or _ASSURANCE_POLICY.fullmatch(values["assurance_policy_version"]) is None
        or values["expires_at"] <= values["authenticated_at"]
        or values["authenticated_at"] > trusted_now
        or values["expires_at"] <= trusted_now
    ):
        raise _AuthenticationDenied()
    return values


def _assertion_attestation(
    *,
    account_id,
    identity_id,
    provider,
    authenticated_at,
    expires_at,
    assurance_policy_version,
    environment_namespace,
):
    if type(authenticated_at) is not datetime or type(expires_at) is not datetime:
        raise TypeError("trusted_external_authentication_required")
    payload = (
        "trusted_external_identity_authentication_v1",
        account_id,
        identity_id,
        provider,
        authenticated_at.isoformat(),
        expires_at.isoformat(),
        assurance_policy_version,
        environment_namespace,
    )
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    ).digest()


def _completion_policy_attestation(
    *,
    expected_provider,
    expected_assurance_policy_version,
    environment_namespace,
    completion_policy_version,
    idle_ttl,
    absolute_ttl,
):
    if type(idle_ttl) is not timedelta or type(absolute_ttl) is not timedelta:
        raise TypeError("trusted_login_completion_policy_required")
    payload = (
        "trusted_login_completion_policy_v1",
        expected_provider,
        expected_assurance_policy_version,
        environment_namespace,
        completion_policy_version,
        int(idle_ttl.total_seconds()),
        int(absolute_ttl.total_seconds()),
    )
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    ).digest()


def _validated_session_policy_values(policy):
    if type(policy) is not TrustedLoginSessionPolicy:
        raise _Unavailable()
    try:
        environment = _validated_login_environment(
            policy._environment_namespace
        )
        idle_ttl = _validated_ttl(
            policy._idle_ttl,
            minimum=MIN_IDLE_TTL,
            maximum=MAX_IDLE_TTL,
        )
        absolute_ttl = _validated_ttl(
            policy._absolute_ttl,
            minimum=MIN_ABSOLUTE_TTL,
            maximum=MAX_ABSOLUTE_TTL,
        )
    except Exception:
        raise _Unavailable() from None
    if idle_ttl > absolute_ttl:
        raise _Unavailable()
    return {
        "environment_namespace": environment,
        "idle_ttl": idle_ttl,
        "absolute_ttl": absolute_ttl,
    }


def _validated_completion_policy_values(policy):
    if type(policy) is not TrustedLoginCompletionPolicy:
        raise _Unavailable()
    try:
        values = policy._values_for_service(_COMPLETION_POLICY_SERVICE_CAPABILITY)
        values["environment_namespace"] = _validated_login_environment(
            values["environment_namespace"]
        )
        values["idle_ttl"] = _validated_ttl(
            values["idle_ttl"],
            minimum=MIN_IDLE_TTL,
            maximum=MAX_IDLE_TTL,
        )
        values["absolute_ttl"] = _validated_ttl(
            values["absolute_ttl"],
            minimum=MIN_ABSOLUTE_TTL,
            maximum=MAX_ABSOLUTE_TTL,
        )
    except Exception:
        raise _Unavailable() from None
    if (
        type(values["expected_provider"]) is not str
        or values["expected_provider"] not in PROVIDERS
        or type(values["expected_assurance_policy_version"]) is not str
        or _ASSURANCE_POLICY.fullmatch(
            values["expected_assurance_policy_version"]
        )
        is None
        or type(values["completion_policy_version"]) is not str
        or _ASSURANCE_POLICY.fullmatch(values["completion_policy_version"])
        is None
        or values["idle_ttl"] > values["absolute_ttl"]
    ):
        raise _Unavailable()
    return values


def _require_policy_coherence(assertion, policy):
    if (
        assertion["provider"] != policy["expected_provider"]
        or assertion["assurance_policy_version"]
        != policy["expected_assurance_policy_version"]
        or assertion["environment_namespace"] != policy["environment_namespace"]
    ):
        raise _AuthenticationDenied()


def _validate_durable_login_state(connection, values, trusted_now):
    account_rows = _rows(
        connection,
        "SELECT user_id, lifecycle_status, row_version, created_at, updated_at, "
        "deletion_requested_at, deactivated_at FROM users WHERE user_id = ? LIMIT 2",
        (values["account_id"],),
    )
    if len(account_rows) != 1:
        raise _AuthenticationDenied()
    account = account_rows[0]
    if not authoritative_account_row_valid(
        account,
        expected_user_id=values["account_id"],
    ):
        raise _Unavailable()
    if account["lifecycle_status"] != "active":
        raise _AuthenticationDenied()
    account_created = _parse_time(account["created_at"])
    account_updated = _parse_time(account["updated_at"])
    if account_created > trusted_now or account_updated > trusted_now:
        raise _Unavailable()

    identity_rows = _rows(
        connection,
        "SELECT auth_identity_id, user_id, provider, provider_subject, verified_email, "
        "email_verified, created_at, last_authenticated_at, disabled_at, "
        "link_idempotency_key, request_fingerprint FROM auth_identities "
        "WHERE auth_identity_id = ? LIMIT 2",
        (values["identity_id"],),
    )
    if len(identity_rows) != 1:
        raise _AuthenticationDenied()
    identity = identity_rows[0]
    if not authoritative_auth_identity_row_valid(identity):
        raise _Unavailable()
    if (
        identity["user_id"] != values["account_id"]
        or identity["provider"] != values["provider"]
        or identity["disabled_at"] is not None
    ):
        raise _AuthenticationDenied()
    if not authoritative_auth_identity_row_valid(
        identity,
        expected_user_id=values["account_id"],
        account_created_at=account["created_at"],
    ):
        raise _Unavailable()
    identity_created = _parse_time(identity["created_at"])
    last_authenticated = _parse_time(identity["last_authenticated_at"])
    if last_authenticated < identity_created or last_authenticated > trusted_now:
        raise _Unavailable()
    if identity_created > values["authenticated_at"]:
        raise _AuthenticationDenied()
    matching = _rows(
        connection,
        "SELECT auth_identity_id FROM auth_identities "
        "WHERE (provider = ? AND provider_subject = ?) "
        "OR (user_id = ? AND provider = ?) LIMIT 3",
        (
            identity["provider"],
            identity["provider_subject"],
            identity["user_id"],
            identity["provider"],
        ),
    )
    if len(matching) != 1 or matching[0]["auth_identity_id"] != values["identity_id"]:
        raise _Unavailable()


def _bound_idempotency_key(values, policy, idempotency_key):
    scope_digest = hashlib.sha256(
        b"b2d1-idempotency-scope-v1\x00" + idempotency_key.encode("utf-8")
    ).hexdigest()
    binding_payload = {
        "absolute_seconds": int(policy["absolute_ttl"].total_seconds()),
        "account_id": values["account_id"],
        "assurance_policy_version": values["assurance_policy_version"],
        "authenticated_at": values["authenticated_at"].isoformat(),
        "environment_namespace": values["environment_namespace"],
        "expected_assurance_policy_version": policy[
            "expected_assurance_policy_version"
        ],
        "expected_provider": policy["expected_provider"],
        "expires_at": values["expires_at"].isoformat(),
        "identity_id": values["identity_id"],
        "idle_seconds": int(policy["idle_ttl"].total_seconds()),
        "provider": values["provider"],
        "trusted_completion_policy_version": policy[
            "completion_policy_version"
        ],
        "version": "b2d1_v1",
    }
    serialized = json.dumps(
        binding_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    binding_digest = hashlib.sha256(serialized).hexdigest()
    prefix = f"b2d1.{scope_digest}."
    return prefix, f"{prefix}{binding_digest}"


def _require_idempotency_binding(connection, scope_prefix, bound_key):
    rows = _rows(
        connection,
        "SELECT creation_idempotency_key FROM account_sessions "
        "WHERE creation_idempotency_key LIKE ? LIMIT 2",
        (scope_prefix + "%",),
    )
    if len(rows) > 1:
        raise _Unavailable()
    if rows and not hmac.compare_digest(rows[0]["creation_idempotency_key"], bound_key):
        raise _IdempotencyConflict()


def _issue_create_session_command_and_execute(
    connection,
    validated,
    secret_vault,
    trusted_now,
    failure_injector,
):
    if type(validated) is not _ValidatedTrustedLogin or validated not in _VALIDATED_LOGINS:
        raise _Unavailable()
    _VALIDATED_LOGINS.discard(validated)
    command = CreateBrowserSessionCommand._issue(
        _COMMAND_ISSUANCE_CAPABILITY,
        account_id=_validated_id(validated.account_id, _ACCOUNT_ID),
        supporting_identity_id=_validated_id(validated.identity_id, _IDENTITY_ID),
        idempotency_key=_validated_idempotency_key(validated.idempotency_key),
        accepted_at=_trusted_time(validated.accepted_at),
        idle_ttl=_validated_ttl(
            validated.idle_ttl,
            minimum=MIN_IDLE_TTL,
            maximum=MAX_IDLE_TTL,
        ),
        absolute_ttl=_validated_ttl(
            validated.absolute_ttl,
            minimum=MIN_ABSOLUTE_TTL,
            maximum=MAX_ABSOLUTE_TTL,
        ),
    )
    _inject(failure_injector, "after_command_issuance")
    return create_browser_session(
        connection,
        command,
        secret_vault,
        _failure_injector=failure_injector,
        _clock=lambda: trusted_now,
    )


def _finalize_pending_with_retry(connection, issued_session, secret_vault):
    for _attempt in range(2):
        try:
            finalized = finalize_pending_issued_session(
                connection,
                issued_session,
                secret_vault,
                _RESPONSE_COMPOSITION_CAPABILITY,
            )
            if (
                finalized is issued_session
                and finalized.status == "issued"
            ):
                return finalized
        except (SystemExit, GeneratorExit):
            raise
        except KeyboardInterrupt as exc:
            _detach_exception(exc)
        except Exception as exc:
            _detach_exception(exc)
        if issued_session.status == "issued":
            return issued_session
        if not _post_commit_connection_usable(connection):
            break
    raise _Unavailable()


def _compensate_undelivered_with_retry(
    connection,
    issued_session,
    secret_vault,
    trusted_now,
):
    for _attempt in range(2):
        try:
            outcome = compensate_undelivered_issued_session(
                connection,
                issued_session,
                secret_vault,
                _RESPONSE_COMPOSITION_CAPABILITY,
                trusted_now=trusted_now,
            )
            if outcome.status in {"revoked", "already_completed"}:
                verified = verify_compensated_undelivered_issued_session(
                    connection,
                    issued_session,
                    secret_vault,
                    _RESPONSE_COMPOSITION_CAPABILITY,
                    trusted_now=trusted_now,
                )
                if verified.status == "already_completed":
                    return
        except Exception as exc:
            _detach_exception(exc)
        if not _post_commit_connection_usable(connection):
            break
    raise _Unavailable()


def _force_compensate_and_verify(
    connection,
    issued_session,
    secret_vault,
    trusted_now,
):
    for _attempt in range(2):
        try:
            outcome = force_compensate_undelivered_issued_session(
                connection,
                issued_session,
                secret_vault,
                _RESPONSE_COMPOSITION_CAPABILITY,
                trusted_now=trusted_now,
            )
            if outcome.status not in {"revoked", "already_completed"}:
                raise _Unavailable()
            verified = verify_compensated_undelivered_issued_session(
                connection,
                issued_session,
                secret_vault,
                _RESPONSE_COMPOSITION_CAPABILITY,
                trusted_now=trusted_now,
            )
            if verified.status == "already_completed":
                return
        except Exception as exc:
            _detach_exception(exc)
        if not _post_commit_connection_usable(connection):
            break
    raise _Unavailable()


def _cleanup_vault_with_retry(secret_vault):
    for _attempt in range(2):
        try:
            abort_request_scoped_secret_vault(
                secret_vault,
                _RESPONSE_COMPOSITION_CAPABILITY,
            )
            verify_request_scoped_secret_vault_terminal(
                secret_vault,
                _RESPONSE_COMPOSITION_CAPABILITY,
            )
            return
        except Exception as exc:
            _detach_exception(exc)
    raise _Unavailable()


def _emergency_terminalize_and_verify(secret_vault):
    for _attempt in range(2):
        try:
            emergency_terminalize_request_scoped_secret_vault(
                secret_vault,
                _RESPONSE_COMPOSITION_CAPABILITY,
            )
            verify_request_scoped_secret_vault_terminal(
                secret_vault,
                _RESPONSE_COMPOSITION_CAPABILITY,
            )
            return
        except Exception as exc:
            _detach_exception(exc)
    raise _Unavailable()


def _establish_failure_postconditions(
    connection,
    issued_session,
    secret_vault,
    trusted_now,
    *,
    compensate,
):
    control_flow = None
    try:
        if compensate:
            try:
                _compensate_undelivered_with_retry(
                    connection,
                    issued_session,
                    secret_vault,
                    trusted_now,
                )
            except (SystemExit, GeneratorExit, KeyboardInterrupt) as exc:
                control_flow = exc
                _force_compensate_and_verify(
                    connection,
                    issued_session,
                    secret_vault,
                    trusted_now,
                )
            except Exception:
                _force_compensate_and_verify(
                    connection,
                    issued_session,
                    secret_vault,
                    trusted_now,
                )
        if issued_session.status in {"pending_commit", "issued"}:
            terminalize_undelivered_issued_result(
                issued_session,
                _RESPONSE_COMPOSITION_CAPABILITY,
            )
    finally:
        if control_flow is None:
            try:
                _cleanup_vault_with_retry(secret_vault)
            except (SystemExit, GeneratorExit, KeyboardInterrupt) as exc:
                control_flow = exc
                _emergency_terminalize_and_verify(secret_vault)
            except Exception:
                _emergency_terminalize_and_verify(secret_vault)
        else:
            _emergency_terminalize_and_verify(secret_vault)
    if control_flow is not None:
        propagated = control_flow
        control_flow = None
        _detach_exception(propagated)
        raise propagated from None


def _post_commit_connection_usable(connection):
    try:
        return (
            type(connection) is sqlite3.Connection
            and not connection.in_transaction
            and connection.execute("SELECT 1").fetchone()[0] == 1
        )
    except Exception:
        return False


def _validated_login_environment(value):
    environment = validate_environment_namespace(value)
    if environment not in _TRUSTED_LOGIN_ENVIRONMENTS:
        raise ValueError("trusted_login_environment_invalid")
    return environment


def _detach_exception(exc):
    """Drop retained recovery frames without exposing or retaining the failure."""

    try:
        exc.__traceback__ = None
        exc.__cause__ = None
        exc.__context__ = None
    except Exception:
        pass


def _failure_result(status):
    return TrustedLoginCompletionResult._issue(
        _RESULT_ISSUANCE_CAPABILITY,
        status,
    )


def _rows(connection, sql, parameters):
    cursor = connection.execute(sql, parameters)
    names = tuple(item[0] for item in cursor.description)
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _parse_time(value):
    try:
        parsed = datetime.fromisoformat(value)
        return _trusted_time(parsed)
    except Exception:
        raise _Unavailable() from None


def _inject(callback, point):
    if callback is None:
        return
    if not callable(callback):
        raise _Unavailable()
    callback(point)

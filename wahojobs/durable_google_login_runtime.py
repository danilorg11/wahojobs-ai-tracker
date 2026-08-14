"""Explicit, fail-closed runtime composition for durable browser login.

Importing this module performs no configuration, environment, filesystem,
database, network, route, clock, or randomness work.  Activation is available
only through :func:`build_durable_google_login_runtime`.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import functools
import hashlib
import hmac
import itertools
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
import time
from types import GetSetDescriptorType, MemberDescriptorType
from urllib.parse import quote, urlsplit

from wahojobs.closed_schema_authority import (
    ClosedSchemaAttestationError,
    current_closed_schema_is_exact,
)
from wahojobs.database_lifetime_ownership import (
    DatabaseLifetimeOwnership,
    DatabaseLifetimeOwnershipError,
    ROLE_DURABLE_RUNTIME,
    acquire_database_lifetime_ownership,
    database_lifetime_ownership_is_released,
    release_database_lifetime_ownership,
    require_database_lifetime_ownership,
)


CONFIGURATION_VERSION = 1
CONFIGURATION_MAX_BYTES = 65_536
MAX_PATH_BYTES = 4_096
MAX_KEY_VERSIONS = 3
CLIENT_SECRET_MIN_BYTES = 16
CLIENT_SECRET_MAX_BYTES = 512
TRANSACTION_KEY_BYTES = 32
INVITATION_LOOKUP_KEY_MIN_BYTES = 32
INVITATION_LOOKUP_KEY_MAX_BYTES = 512
POST_LOGIN_PATH = "/account/profile"
CALLBACK_PATH = "/auth/google/callback"
_SQLITE_HEADER_BYTES = 100
_PATH_TYPE = type(Path())
_WINDOWS_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
SUPPORTED_ENVIRONMENTS = frozenset(
    {"development", "test", "private_beta"}
)
SUPPORTED_BIND_HOST = "127.0.0.1"

_CONFIGURATION_FIELDS = frozenset(
    {
        "version",
        "environment",
        "database_path",
        "bind_host",
        "bind_port",
        "public_origin",
        "google_redirect_uri",
        "google_client_id",
        "google_client_secret_file",
        "oidc_lookup_keys",
        "oidc_lookup_active_version",
        "oidc_protection_keys",
        "oidc_protection_active_version",
        "session_idle_ttl_seconds",
        "session_absolute_ttl_seconds",
        "allowed_post_login_paths",
    }
)
_OPTIONAL_CONFIGURATION_FIELDS = frozenset(
    {"account_invitation_lookup_key_file"}
)
_KEY_REFERENCE_FIELDS = frozenset({"version", "file"})
_CLIENT_ID = re.compile(r"^[\x21-\x7e]{8,256}$")
_HOST = re.compile(r"^(?:localhost|127\.0\.0\.1)$")


class DurableGoogleLoginConfigurationError(Exception):
    """Sanitized startup failure."""

    def __init__(self):
        super().__init__("Durable Google login configuration is unavailable.")


class _DatabaseCleanupFailure(Exception):
    __slots__ = ()


@dataclass(frozen=True, slots=True, repr=False)
class DurableGoogleLoginConfiguration:
    """Minimal nonsecret serving configuration retained after activation."""

    bind_host: str
    bind_port: int
    public_origin: str

    def __repr__(self):
        return (
            "DurableGoogleLoginConfiguration("
            f"bind_host={self.bind_host!r}, bind_port={self.bind_port!r}, "
            f"public_origin={self.public_origin!r})"
        )

    __str__ = __repr__


@dataclass(frozen=True, slots=True, repr=False)
class _FileIdentity:
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int
    mode: int
    links: int


class _ValidatedFileReference:
    __slots__ = ("__identity", "__path", "__secret")

    def __init__(self, capability, *, path, identity, secret):
        if (
            capability is not _FILE_REFERENCE_CAPABILITY
            or type(path) is not _PATH_TYPE
            or type(identity) is not _FileIdentity
            or type(secret) is not bool
        ):
            raise DurableGoogleLoginConfigurationError()
        self.__path = path
        self.__identity = identity
        self.__secret = secret

    def __repr__(self):
        return "_ValidatedFileReference(<sealed>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("validated_file_reference_not_serializable")

    def __copy__(self):
        raise TypeError("validated_file_reference_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("validated_file_reference_not_copyable")


class _DatabaseTargetAuthority:
    __slots__ = ("__identity", "__path")

    def __init__(self, capability, *, path, identity):
        if (
            capability is not _DATABASE_TARGET_CAPABILITY
            or type(path) is not _PATH_TYPE
            or type(identity) is not _FileIdentity
        ):
            raise DurableGoogleLoginConfigurationError()
        self.__path = path
        self.__identity = identity

    def __repr__(self):
        return "_DatabaseTargetAuthority(<sealed>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("database_target_authority_not_serializable")

    def __copy__(self):
        raise TypeError("database_target_authority_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("database_target_authority_not_copyable")


class _DatabaseLifetimeOwnershipResource:
    __slots__ = ("_ownership", "_target")

    def __init__(self, target):
        if type(target) is not _DatabaseTargetAuthority:
            raise DurableGoogleLoginConfigurationError()
        self._target = target
        self._ownership = None

    def publish(self, ownership):
        if (
            type(ownership) is not DatabaseLifetimeOwnership
            or self._ownership is not None
        ):
            raise DurableGoogleLoginConfigurationError()
        self._ownership = ownership
        return True

    @property
    def ownership(self):
        ownership = self._ownership
        if type(ownership) is not DatabaseLifetimeOwnership:
            raise DurableGoogleLoginConfigurationError()
        return ownership

    def close(self):
        ownership = self._ownership
        if ownership is None:
            return True
        try:
            return release_database_lifetime_ownership(
                ownership,
                role=ROLE_DURABLE_RUNTIME,
                database_path=_database_target_path(self._target),
            )
        except DatabaseLifetimeOwnershipError:
            raise DurableGoogleLoginConfigurationError() from None

    @property
    def closed(self):
        ownership = self._ownership
        return ownership is None or database_lifetime_ownership_is_released(
            ownership
        )

    def __repr__(self):
        return "_DatabaseLifetimeOwnershipResource(<sealed>)"

    __str__ = __repr__


@dataclass(frozen=True, slots=True, repr=False)
class _OidcKeyPathSpecification:
    version: int
    path_text: str


@dataclass(frozen=True, slots=True, repr=False)
class _OidcKeyFileReference:
    version: int
    file_reference: _ValidatedFileReference


@dataclass(frozen=True, slots=True, repr=False)
class _PureConfiguration:
    version: int
    environment: str
    database_path_text: str
    bind_host: str
    bind_port: int
    public_origin: str
    public_authority: str
    google_redirect_uri: str
    google_client_id: str
    google_client_secret_path_text: str
    account_invitation_lookup_key_path_text: str | None
    oidc_lookup_keys: tuple[_OidcKeyPathSpecification, ...]
    oidc_lookup_active_version: int
    oidc_protection_keys: tuple[_OidcKeyPathSpecification, ...]
    oidc_protection_active_version: int
    session_idle_ttl: timedelta
    session_absolute_ttl: timedelta
    allowed_post_login_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True, repr=False)
class _DurableGoogleLoginConstructionConfiguration:
    environment: str
    database_target: _DatabaseTargetAuthority
    public_configuration: DurableGoogleLoginConfiguration
    public_authority: str
    google_redirect_uri: str
    google_client_id: str
    google_client_secret_file: _ValidatedFileReference
    account_invitation_lookup_key_file: _ValidatedFileReference | None
    oidc_lookup_keys: tuple[_OidcKeyFileReference, ...]
    oidc_lookup_active_version: int
    oidc_protection_keys: tuple[_OidcKeyFileReference, ...]
    oidc_protection_active_version: int
    session_idle_ttl: timedelta
    session_absolute_ttl: timedelta
    allowed_post_login_paths: tuple[str, ...]

    def __repr__(self):
        return "_DurableGoogleLoginConstructionConfiguration(<redacted>)"

    __str__ = __repr__


@dataclass(frozen=True, slots=True, repr=False)
class _GoogleGatewayConstructionConfiguration:
    environment: str
    google_redirect_uri: str
    google_client_id: str
    invitation_lookup_key: bytearray | None

    def __repr__(self):
        return "_GoogleGatewayConstructionConfiguration(<redacted>)"

    __str__ = __repr__


class _ConfigurationWorkerOutcome:
    __slots__ = (
        "__handoff_reservation",
        "__status",
        "__value",
    )

    def __init__(
        self,
        capability,
        status,
        value=None,
        *,
        handoff_reservation=None,
    ):
        if (
            capability is not _WORKER_OUTCOME_CAPABILITY
            or (
                handoff_reservation is not None
                and type(handoff_reservation)
                is not _ActivationHandoffReservation
            )
            or status not in {
                "pending",
                "ok",
                "failure",
                "control_flow",
            }
            or (
                status == "control_flow"
                and not isinstance(
                    value,
                    (KeyboardInterrupt, SystemExit, GeneratorExit),
                )
            )
            or (status == "failure" and value is not None)
            or (status == "pending" and value is not None)
        ):
            raise RuntimeError("configuration_worker_outcome_invalid")
        self.__handoff_reservation = handoff_reservation
        self.__status = status
        self.__value = value

    def _publish(self, status, value=None):
        if (
            self.__status != "pending"
            or status not in {"ok", "failure", "control_flow"}
            or (
                status == "control_flow"
                and not isinstance(
                    value,
                    (KeyboardInterrupt, SystemExit, GeneratorExit),
                )
            )
            or (status == "failure" and value is not None)
        ):
            raise RuntimeError("configuration_worker_outcome_invalid")
        self.__value = value
        self.__status = status

    def _replace(self, status, value=None):
        if (
            status not in {"failure", "control_flow"}
            or (
                status == "control_flow"
                and not isinstance(
                    value,
                    (KeyboardInterrupt, SystemExit, GeneratorExit),
                )
            )
            or (status == "failure" and value is not None)
        ):
            raise RuntimeError("configuration_worker_outcome_invalid")
        self.__value = value
        self.__status = status

    def _clear_value(self):
        self.__value = None

    def _handoff_reservation(self):
        return self.__handoff_reservation

    def __repr__(self):
        return "_ConfigurationWorkerOutcome(<closed>)"

    __str__ = __repr__


class _ActivationHandoffReservation:
    __slots__ = ("__binding", "__binding_lock")

    def __init__(self, capability):
        if capability is not _HANDOFF_RESERVATION_CAPABILITY:
            raise DurableGoogleLoginConfigurationError()
        # Nested callers acquire a lease condition before this lock.
        # Reservation methods never acquire a lease condition or perform I/O.
        self.__binding_lock = threading.Lock()
        self.__binding = None

    def _bind(self, lease, generation):
        if (
            type(lease) is not _ActivationHandoffCleanupLease
            or type(generation) is not int
            or generation < 1
        ):
            raise DurableGoogleLoginConfigurationError()
        with self.__binding_lock:
            binding = self.__binding
            if binding is None:
                self.__binding = (lease, generation)
                return True
            return binding[0] is lease and binding[1] == generation

    def _binding(self):
        with self.__binding_lock:
            return self.__binding

    def _clear(self, lease, generation, expected_binding):
        with self.__binding_lock:
            binding = self.__binding
            if binding is None:
                return True
            if (
                binding is not expected_binding
                or binding[0] is not lease
                or binding[1] != generation
            ):
                return False
            self.__binding = None
            return True

    def __repr__(self):
        with self.__binding_lock:
            state = "vacant" if self.__binding is None else "claimed"
        return f"_ActivationHandoffReservation(<{state}>)"

    __str__ = __repr__


@dataclass(frozen=True, slots=True, repr=False)
class _CleanupReport:
    closed_resources: tuple[str, ...]
    unresolved_resources: tuple[str, ...]
    cleanup_complete: bool
    failure_categories: tuple[str, ...]

    def __repr__(self):
        return (
            "_CleanupReport("
            f"closed={len(self.closed_resources)}, "
            f"unresolved={len(self.unresolved_resources)}, "
            f"complete={self.cleanup_complete}, "
            f"failures={len(self.failure_categories)})"
        )

    __str__ = __repr__


class _ActivationHandoffCleanupLease:
    __slots__ = (
        "__condition",
        "__generation",
        "__owner",
        "__resource",
        "__state",
    )

    def __init__(self, resource, *, _capability=None):
        vacant = (
            resource is None
            and _capability is _HANDOFF_LEASE_POOL_CAPABILITY
        )
        if resource is None and not vacant:
            raise DurableGoogleLoginConfigurationError()
        self.__condition = threading.Condition(threading.Lock())
        self.__generation = 0
        self.__owner = None
        self.__resource = resource
        self.__state = (
            "vacant" if vacant else "cleanup_unresolved"
        )

    def reserve(self, reservation):
        if type(reservation) is not _ActivationHandoffReservation:
            raise DurableGoogleLoginConfigurationError()
        with self.__condition:
            binding = reservation._binding()
            if binding is not None:
                if binding[0] is not self:
                    return False
                generation = binding[1]
                if (
                    generation == self.__generation
                    and self.__state == "reserved"
                    and self.__resource is None
                    and self.__owner is None
                ):
                    return True
                if (
                    self.__state == "vacant"
                    and self.__resource is None
                    and self.__owner is None
                    and generation == self.__generation
                ):
                    self.__state = "reserved"
                    return True
                return False
            if (
                self.__state == "terminal"
                and self.__resource is None
                and self.__owner is None
            ):
                self.__state = "vacant"
            if (
                self.__state != "vacant"
                or self.__resource is not None
                or self.__owner is not None
            ):
                return False
            generation = self.__generation + 1
            self.__generation = generation
            if not reservation._bind(self, generation):
                return False
            self.__state = "reserved"
        return True

    def _binding_targets_self(self, binding):
        return (
            type(binding) is tuple
            and len(binding) == 2
            and binding[0] is self
            and type(binding[1]) is int
            and binding[1] >= 1
        )

    def reserved_by(self, reservation, expected_binding):
        if (
            type(reservation) is not _ActivationHandoffReservation
            or not self._binding_targets_self(expected_binding)
        ):
            return False
        with self.__condition:
            current = reservation._binding()
            return (
                current is expected_binding
                and expected_binding[1] == self.__generation
                and self.__state == "reserved"
                and self.__resource is None
                and self.__owner is None
            )

    def offer_reserved(
        self,
        reservation,
        resource,
        expected_binding,
    ):
        if (
            type(reservation) is not _ActivationHandoffReservation
            or resource is None
            or not self._binding_targets_self(expected_binding)
        ):
            raise DurableGoogleLoginConfigurationError()
        with self.__condition:
            current = reservation._binding()
            if current is not expected_binding:
                return False
            if expected_binding[1] != self.__generation:
                return False
            if (
                self.__resource is resource
                and self.__state == "reserved"
            ):
                self.__state = "cleanup_unresolved"
                return True
            if (
                self.__resource is resource
                and self.__state != "terminal"
            ):
                return True
            if (
                self.__state != "reserved"
                or self.__resource is not None
                or self.__owner is not None
            ):
                return False
            self.__resource = resource
            self.__state = "cleanup_unresolved"
        return True

    def owns_reserved(
        self,
        reservation,
        resource,
        expected_binding,
    ):
        if (
            type(reservation) is not _ActivationHandoffReservation
            or not self._binding_targets_self(expected_binding)
        ):
            return False
        with self.__condition:
            current = reservation._binding()
            return (
                current is expected_binding
                and expected_binding[1] == self.__generation
                and self.__resource is resource
                and self.__state != "terminal"
            )

    def cancel_reserved(self, reservation, expected_binding):
        if (
            type(reservation) is not _ActivationHandoffReservation
            or not self._binding_targets_self(expected_binding)
        ):
            return False
        generation = expected_binding[1]
        with self.__condition:
            current = reservation._binding()
            if current is None:
                return True
            if (
                current is not expected_binding
                or current[0] is not self
                or current[1] != generation
            ):
                return False
            if generation != self.__generation:
                released = True
            elif (
                self.__state
                in {"reserved", "reservation_release_pending"}
                and self.__resource is None
                and self.__owner is None
            ):
                self.__state = "vacant"
                self.__generation = generation + 1
                released = True
            elif (
                self.__state in {"vacant", "terminal"}
                and self.__resource is None
                and self.__owner is None
            ):
                self.__state = "vacant"
                self.__generation = generation + 1
                released = True
            else:
                released = False
        if not released:
            return False
        try:
            cleared = reservation._clear(
                self,
                generation,
                expected_binding,
            )
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ) as exc:
            _sanitize_exception_graph(exc)
            propagated = exc
            exc = None
            raise propagated from None
        except Exception as exc:
            _sanitize_exception_graph(exc)
            propagated = exc
            exc = None
            raise propagated from None
        if not cleared:
            current = reservation._binding()
            cleared = (
                current is None
                or current is not expected_binding
            )
        return cleared

    def _dispose_reserved(
        self,
        reservation,
        expected_resource,
        expected_binding,
    ):
        if (
            type(reservation) is not _ActivationHandoffReservation
            or expected_resource is None
            or not self._binding_targets_self(expected_binding)
        ):
            raise DurableGoogleLoginConfigurationError()
        generation = expected_binding[1]
        with self.__condition:
            current = reservation._binding()
            if current is None:
                return _HANDOFF_RESERVATION_RELEASED
            if (
                current is not expected_binding
                or current[0] is not self
                or current[1] != generation
            ):
                return _HANDOFF_RESERVATION_CONFLICT
            if (
                generation == self.__generation
                and self.__resource is expected_resource
                and self.__state != "terminal"
            ):
                return self
            if (
                generation == self.__generation
                and self.__state == "reserved"
                and self.__resource is None
                and self.__owner is None
            ):
                self.__state = "reservation_release_pending"
            elif not (
                generation != self.__generation
                or (
                    self.__state == "reservation_release_pending"
                    and self.__resource is None
                    and self.__owner is None
                )
                or (
                    self.__state in {"vacant", "terminal"}
                    and self.__resource is None
                    and self.__owner is None
                )
            ):
                return _HANDOFF_RESERVATION_CONFLICT
        if self.cancel_reserved(reservation, expected_binding):
            return _HANDOFF_RESERVATION_RELEASED
        return _HANDOFF_RESERVATION_CONFLICT

    def offer(self, resource):
        if resource is None:
            raise DurableGoogleLoginConfigurationError()
        with self.__condition:
            if (
                self.__state == "terminal"
                and self.__resource is None
                and self.__owner is None
            ):
                self.__state = "vacant"
            if (
                self.__resource is resource
                and self.__state == "vacant"
            ):
                self.__state = "cleanup_unresolved"
                return True
            if (
                self.__resource is resource
                and self.__state != "terminal"
            ):
                return True
            if (
                self.__resource is not None
                or self.__state != "vacant"
            ):
                return False
            self.__generation += 1
            self.__resource = resource
            self.__state = "cleanup_unresolved"
        return True

    def active(self):
        with self.__condition:
            return self.__resource is not None

    def owns(self, resource):
        with self.__condition:
            return (
                self.__resource is resource
                and self.__state != "terminal"
            )

    def _retain_existing(self, resource):
        if resource is None:
            raise DurableGoogleLoginConfigurationError()
        with self.__condition:
            if (
                self.__resource is not resource
                or self.__state == "terminal"
            ):
                return False
            if self.__state in {"vacant", "reserved"}:
                self.__state = "cleanup_unresolved"
            return self.__generation

    def _owns_generation(self, resource, generation):
        if type(generation) is not int:
            return False
        with self.__condition:
            return (
                self.__generation == generation
                and self.__resource is resource
                and self.__state != "terminal"
            )

    def close(self, *, _expected_resource=None):
        caller = threading.current_thread()
        with self.__condition:
            if (
                _expected_resource is not None
                and self.__resource is not _expected_resource
            ):
                return False
            if (
                self.__state == "vacant"
                and self.__resource is None
            ):
                return True
            if (
                self.__state == "vacant"
                and self.__resource is not None
            ):
                self.__state = "cleanup_unresolved"
            if (
                self.__state == "reserved"
                and self.__resource is not None
            ):
                self.__state = "cleanup_unresolved"
            if self.__state == "terminal":
                return True
            if self.__state == "closing":
                owner = self.__owner
                if (
                    owner is not None
                    and owner is not caller
                    and owner.is_alive()
                ):
                    return False
                self.__state = "cleanup_unresolved"
                self.__owner = None
            if self.__state != "cleanup_unresolved":
                return False
            self.__owner = caller
            self.__state = "closing"
            resource = self.__resource
        terminal = False
        try:
            terminal = _close_activation_resource_preserving_primary(
                resource
            )
            return terminal
        finally:
            committed = False
            while not committed:
                try:
                    with self.__condition:
                        if terminal:
                            self.__resource = None
                            self.__state = "terminal"
                        else:
                            self.__state = "cleanup_unresolved"
                        self.__owner = None
                        self.__condition.notify_all()
                    committed = True
                except (
                    KeyboardInterrupt,
                    SystemExit,
                    GeneratorExit,
                ) as exc:
                    _sanitize_exception_graph(exc)
                    exc = None
                except Exception as exc:
                    _sanitize_exception_graph(exc)
                    exc = None
            resource = None
            caller = None

    def terminal(self):
        with self.__condition:
            return self.__state == "terminal"

    def reset_terminal(self):
        with self.__condition:
            if (
                self.__state != "terminal"
                or self.__resource is not None
                or self.__owner is not None
            ):
                return False
            self.__state = "vacant"
        return True

    def __repr__(self):
        with self.__condition:
            state = self.__state
        return f"_ActivationHandoffCleanupLease(<{state}>)"

    __str__ = __repr__


class _ActivationPublicationGate:
    __slots__ = (
        "__condition",
        "__next_token",
        "__owner",
        "__outcome",
        "__token",
        "__wait_seconds",
    )

    def __init__(self, wait_seconds):
        if (
            type(wait_seconds) not in {int, float}
            or wait_seconds <= 0
        ):
            raise RuntimeError("activation_gate_configuration_invalid")
        self.__condition = threading.Condition(threading.Lock())
        self.__next_token = 1
        self.__owner = None
        self.__outcome = None
        self.__token = None
        self.__wait_seconds = float(wait_seconds)

    def __enter__(self):
        caller = threading.current_thread()
        token = None
        try:
            deadline = time.monotonic() + self.__wait_seconds
            with self.__condition:
                while self.__token is not None:
                    owner = self.__owner
                    if owner is caller:
                        raise DurableGoogleLoginConfigurationError()
                    if owner is not None and not owner.is_alive():
                        self.__token = None
                        self.__owner = None
                        self.__condition.notify_all()
                        break
                    if owner is None:
                        self.__token = None
                        self.__condition.notify_all()
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DurableGoogleLoginConfigurationError()
                    self.__condition.wait(remaining)
                token = self.__next_token
                self.__next_token += 1
                self.__token = token
                self.__owner = caller
                self.__outcome = None
            return self
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ):
            if token is not None:
                self._release(caller, token)
            raise
        except Exception:
            if token is not None:
                self._release(caller, token)
            raise

    def __exit__(self, exception_type, _exception, _traceback):
        caller = threading.current_thread()
        token = self.__token
        if exception_type is not None:
            disposed = False
            while not disposed:
                try:
                    disposed = self._dispose_outcome_preserving_primary(
                        caller,
                        token,
                    )
                except (
                    KeyboardInterrupt,
                    SystemExit,
                    GeneratorExit,
                ) as exc:
                    _sanitize_exception_graph(exc)
                    exc = None
                except Exception as exc:
                    _sanitize_exception_graph(exc)
                    exc = None
        released = False
        while not released:
            try:
                released = self._release(caller, token)
            except (
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ) as exc:
                _sanitize_exception_graph(exc)
                exc = None
            except Exception as exc:
                _sanitize_exception_graph(exc)
                exc = None
        caller = None
        token = None
        return False

    def protect_outcome(self, outcome):
        if type(outcome) is not _ConfigurationWorkerOutcome:
            raise DurableGoogleLoginConfigurationError()
        caller = threading.current_thread()
        with self.__condition:
            if (
                self.__token is None
                or self.__owner is not caller
                or self.__outcome is not None
            ):
                raise DurableGoogleLoginConfigurationError()
            self.__outcome = outcome
        caller = None
        return True

    def _dispose_outcome_preserving_primary(self, caller, token):
        with self.__condition:
            if (
                self.__token != token
                or self.__owner is not caller
            ):
                return True
            outcome = self.__outcome
        if outcome is not None:
            _close_worker_outcome_value_preserving_primary(outcome)
        outcome = None
        return True

    def _release(self, caller, token):
        released = False
        while not released:
            try:
                with self.__condition:
                    if self.__token != token:
                        released = True
                    elif (
                        self.__owner is not None
                        and self.__owner is not caller
                    ):
                        released = False
                    else:
                        self.__token = None
                        self.__owner = None
                        self.__outcome = None
                        self.__condition.notify_all()
                        released = True
            except (
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ) as exc:
                _sanitize_exception_graph(exc)
                exc = None
            except Exception as exc:
                _sanitize_exception_graph(exc)
                exc = None
        caller = None
        token = None
        return True

    def __repr__(self):
        with self.__condition:
            active = self.__token is not None
        return f"_ActivationPublicationGate(active={active})"

    __str__ = __repr__


class _CleanupEntry:
    __slots__ = (
        "action",
        "blocks_dependents",
        "category",
        "dependencies",
        "probe",
        "require_terminal_dependencies",
        "resource",
        "state",
        "token",
    )

    def __init__(
        self,
        *,
        token,
        category,
        resource,
        action,
        probe,
        dependencies,
        require_terminal_dependencies,
    ):
        self.token = token
        self.category = category
        self.dependencies = dependencies
        self.require_terminal_dependencies = require_terminal_dependencies
        self.resource = resource
        self.action = action
        self.probe = probe
        self.state = "owned"
        self.blocks_dependents = True


_CLEANUP_RESOURCE_ORDER = (
    "secret_buffers",
    "google_gateway",
    "key_authority",
    "protection_authority",
    "lookup_authority",
    "database_descriptor",
    "database_attestation_connection",
    "database_connections",
    "database_lifetime_ownership",
    "profile_integration",
    "browser_integration",
    "inactive_server",
    "tls_workspace",
    "listener_socket",
    "route_integration",
    "request_threads",
    "accepted_sockets",
    "server_shutdown",
    "serve_thread",
    "signal_handlers",
)
_CLEANUP_RESOURCE_CATEGORIES = frozenset(_CLEANUP_RESOURCE_ORDER)
_DATABASE_LIFETIME_TERMINAL_DEPENDENCIES = (
    "database_descriptor",
    "database_attestation_connection",
    "database_connections",
    "profile_integration",
    "browser_integration",
    "listener_socket",
    "route_integration",
    "request_threads",
    "accepted_sockets",
    "serve_thread",
)
_CLEANUP_WAIT_SECONDS = 5.0
_ACTIVATION_PUBLICATION_WAIT_SECONDS = 1.0
_ACTIVATION_PUBLICATION_GATE = _ActivationPublicationGate(
    _ACTIVATION_PUBLICATION_WAIT_SECONDS
)
_UNRESOLVED_HANDOFF_LOCK = threading.Lock()
_UNRESOLVED_HANDOFFS = {}


class _CleanupCoordinator:
    """One bounded, retryable owner for construction and runtime resources."""

    __slots__ = (
        "_condition",
        "_entries",
        "_failure_categories",
        "_last_report",
        "_next_token",
        "_next_owner_token",
        "_owner_active",
        "_owner_thread",
        "_owner_token",
    )

    def __init__(self):
        self._condition = threading.Condition(threading.Lock())
        self._entries = []
        self._failure_categories = set()
        self._last_report = _CleanupReport((), (), True, ())
        self._next_token = 1
        self._next_owner_token = 1
        self._owner_active = False
        self._owner_thread = None
        self._owner_token = None

    def own(
        self,
        category,
        resource,
        action,
        *,
        probe=None,
        dependencies=(),
        require_terminal_dependencies=False,
    ):
        if (
            category not in _CLEANUP_RESOURCE_CATEGORIES
            or resource is None
            or not callable(action)
            or (probe is not None and not callable(probe))
            or type(dependencies) is not tuple
            or type(require_terminal_dependencies) is not bool
            or any(
                dependency not in _CLEANUP_RESOURCE_CATEGORIES
                or dependency == category
                for dependency in dependencies
            )
        ):
            raise DurableGoogleLoginConfigurationError()
        with self._condition:
            if self._owner_active or any(
                entry.category == category
                and entry.state != "terminal"
                for entry in self._entries
            ):
                raise DurableGoogleLoginConfigurationError()
            token = self._next_token
            self._next_token += 1
            self._entries.append(
                _CleanupEntry(
                    token=token,
                    category=category,
                    resource=resource,
                    action=action,
                    probe=probe,
                    dependencies=dependencies,
                    require_terminal_dependencies=(
                        require_terminal_dependencies
                    ),
                )
            )
            self._last_report = self._snapshot_locked()
            return token

    def replace(self, token, resource):
        if type(token) is not int or resource is None:
            raise DurableGoogleLoginConfigurationError()
        with self._condition:
            entry = self._entry_locked(token)
            if entry.state not in {"owned", "unresolved"}:
                raise DurableGoogleLoginConfigurationError()
            entry.resource = resource
            self._last_report = self._snapshot_locked()

    def resolve(self, token):
        if type(token) is not int:
            raise DurableGoogleLoginConfigurationError()
        with self._condition:
            entry = self._entry_locked(token)
            if entry.state == "closing":
                raise DurableGoogleLoginConfigurationError()
            self._terminalize_entry_locked(entry)
            self._last_report = self._snapshot_locked()

    def resolve_resource(self, category, resource):
        if category not in _CLEANUP_RESOURCE_CATEGORIES:
            raise DurableGoogleLoginConfigurationError()
        with self._condition:
            for entry in reversed(self._entries):
                if (
                    entry.category == category
                    and entry.resource is resource
                    and entry.state != "terminal"
                ):
                    if entry.state == "closing":
                        raise DurableGoogleLoginConfigurationError()
                    self._terminalize_entry_locked(entry)
                    self._last_report = self._snapshot_locked()
                    return
        raise DurableGoogleLoginConfigurationError()

    def resource(self, category):
        if category not in _CLEANUP_RESOURCE_CATEGORIES:
            raise DurableGoogleLoginConfigurationError()
        with self._condition:
            for entry in reversed(self._entries):
                if entry.category == category and entry.state != "terminal":
                    return entry.resource
        return None

    def is_terminal(self, category):
        if category not in _CLEANUP_RESOURCE_CATEGORIES:
            raise DurableGoogleLoginConfigurationError()
        with self._condition:
            matching = [
                entry
                for entry in self._entries
                if entry.category == category
            ]
            return bool(matching) and all(
                entry.state == "terminal" for entry in matching
            )

    def snapshot(self):
        with self._condition:
            return self._snapshot_locked()

    def cleanup(self, *, preserve_primary=False):
        if type(preserve_primary) is not bool:
            raise DurableGoogleLoginConfigurationError()
        deadline = time.monotonic() + _CLEANUP_WAIT_SECONDS
        first_control = None
        owner_claimed = False
        owner_token = None
        setup_failed = False
        entries = ()
        report = None
        try:
            try:
                with self._condition:
                    timed_out = False
                    while self._owner_active:
                        active_thread = self._owner_thread
                        if (
                            active_thread is None
                            or (
                                active_thread
                                is threading.current_thread()
                                or not active_thread.is_alive()
                            )
                        ):
                            self._normalize_interrupted_entries_locked()
                            self._owner_active = False
                            self._owner_thread = None
                            self._owner_token = None
                            break
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            timed_out = True
                            break
                        self._condition.wait(remaining)
                    if not timed_out:
                        owner_claimed = True
                        owner_token = self._next_owner_token
                        self._next_owner_token += 1
                        self._owner_token = owner_token
                        self._owner_thread = threading.current_thread()
                        self._owner_active = True
                        self._normalize_interrupted_entries_locked()
                        entries = tuple(
                            entry
                            for entry in reversed(self._entries)
                            if entry.state in {"owned", "unresolved"}
                        )
            except (
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ) as exc:
                first_control = exc
                _sanitize_exception_graph(exc)
                exc = None
                setup_failed = True
            except Exception as exc:
                _sanitize_exception_graph(exc)
                exc = None
                setup_failed = True

            if owner_claimed and setup_failed:
                for _attempt in range(3):
                    try:
                        with self._condition:
                            self._normalize_interrupted_entries_locked()
                            entries = tuple(
                                entry
                                for entry in reversed(self._entries)
                                if entry.state in {"owned", "unresolved"}
                            )
                        setup_failed = False
                        break
                    except (
                        KeyboardInterrupt,
                        SystemExit,
                        GeneratorExit,
                    ) as exc:
                        if first_control is None:
                            first_control = exc
                        _sanitize_exception_graph(exc)
                        exc = None
                    except Exception as exc:
                        _sanitize_exception_graph(exc)
                        exc = None

            if owner_claimed and not setup_failed:
                for entry in entries:
                    terminal = False
                    failure = False
                    blocks_dependents = False
                    resource = None
                    action = None
                    probe = None
                    try:
                        with self._condition:
                            if entry.state == "terminal":
                                continue
                            if self._dependencies_live_locked(entry):
                                entry.state = "unresolved"
                                entry.blocks_dependents = True
                                continue
                            entry.state = "closing"
                            resource = entry.resource
                            action = entry.action
                            probe = entry.probe
                        try:
                            terminal = action(resource) is not False
                            blocks_dependents = not terminal
                        except (
                            KeyboardInterrupt,
                            SystemExit,
                            GeneratorExit,
                        ) as exc:
                            if first_control is None:
                                first_control = exc
                            _sanitize_exception_graph(exc)
                            exc = None
                            failure = True
                        except Exception as exc:
                            _sanitize_exception_graph(exc)
                            exc = None
                            failure = True

                        if not terminal and probe is not None:
                            try:
                                terminal = probe(resource) is True
                            except (
                                KeyboardInterrupt,
                                SystemExit,
                                GeneratorExit,
                            ) as exc:
                                if first_control is None:
                                    first_control = exc
                                _sanitize_exception_graph(exc)
                                exc = None
                                failure = True
                            except Exception as exc:
                                _sanitize_exception_graph(exc)
                                exc = None
                                failure = True

                        with self._condition:
                            if failure:
                                self._failure_categories.add(entry.category)
                            if terminal:
                                self._terminalize_entry_locked(entry)
                            else:
                                entry.state = "unresolved"
                                entry.blocks_dependents = (
                                    blocks_dependents
                                    or (
                                        failure
                                        and entry.category
                                        == "request_threads"
                                    )
                                )
                    except (
                        KeyboardInterrupt,
                        SystemExit,
                        GeneratorExit,
                    ) as exc:
                        if first_control is None:
                            first_control = exc
                        _sanitize_exception_graph(exc)
                        exc = None
                        failure = True
                    except Exception as exc:
                        _sanitize_exception_graph(exc)
                        exc = None
                        failure = True
                    finally:
                        for _attempt in range(3):
                            try:
                                self._recover_cleanup_entry(
                                    entry,
                                    terminal=terminal,
                                    failure=failure,
                                )
                                break
                            except (
                                KeyboardInterrupt,
                                SystemExit,
                                GeneratorExit,
                            ) as exc:
                                if first_control is None:
                                    first_control = exc
                                _sanitize_exception_graph(exc)
                                exc = None
                                failure = True
                            except Exception as exc:
                                _sanitize_exception_graph(exc)
                                exc = None
                                failure = True
                        resource = None
                        action = None
                        probe = None

                for entry in entries:
                    terminal = False
                    failure = False
                    resource = None
                    probe = None
                    try:
                        with self._condition:
                            if (
                                entry.state != "unresolved"
                                or entry.probe is None
                                or self._dependencies_live_locked(entry)
                            ):
                                continue
                            entry.state = "closing"
                            resource = entry.resource
                            probe = entry.probe
                        try:
                            terminal = probe(resource) is True
                        except (
                            KeyboardInterrupt,
                            SystemExit,
                            GeneratorExit,
                        ) as exc:
                            if first_control is None:
                                first_control = exc
                            _sanitize_exception_graph(exc)
                            exc = None
                            failure = True
                        except Exception as exc:
                            _sanitize_exception_graph(exc)
                            exc = None
                            failure = True
                        with self._condition:
                            if failure:
                                self._failure_categories.add(entry.category)
                            if terminal:
                                self._terminalize_entry_locked(entry)
                            else:
                                entry.state = "unresolved"
                    except (
                        KeyboardInterrupt,
                        SystemExit,
                        GeneratorExit,
                    ) as exc:
                        if first_control is None:
                            first_control = exc
                        _sanitize_exception_graph(exc)
                        exc = None
                        failure = True
                    except Exception as exc:
                        _sanitize_exception_graph(exc)
                        exc = None
                        failure = True
                    finally:
                        for _attempt in range(3):
                            try:
                                self._recover_cleanup_entry(
                                    entry,
                                    terminal=terminal,
                                    failure=failure,
                                )
                                break
                            except (
                                KeyboardInterrupt,
                                SystemExit,
                                GeneratorExit,
                            ) as exc:
                                if first_control is None:
                                    first_control = exc
                                _sanitize_exception_graph(exc)
                                exc = None
                                failure = True
                            except Exception as exc:
                                _sanitize_exception_graph(exc)
                                exc = None
                                failure = True
                        resource = None
                        probe = None
        finally:
            release_control = None
            if owner_claimed:
                report, release_control = self._release_cleanup_owner(
                    owner_token
                )
            else:
                try:
                    with self._condition:
                        report = self._last_report
                except (
                    KeyboardInterrupt,
                    SystemExit,
                    GeneratorExit,
                ) as exc:
                    release_control = exc
                    _sanitize_exception_graph(exc)
                    exc = None
                    report = self._last_report
                except Exception as exc:
                    _sanitize_exception_graph(exc)
                    exc = None
                    report = self._last_report
            if first_control is None and release_control is not None:
                first_control = release_control
            release_control = None

        if first_control is not None:
            propagated = first_control
            first_control = None
            if preserve_primary:
                propagated = None
            else:
                raise propagated from None
        return report

    def close(self, *, _preserve_primary=False):
        return self.cleanup(preserve_primary=_preserve_primary)

    def _entry_locked(self, token):
        for entry in self._entries:
            if entry.token == token:
                return entry
        raise DurableGoogleLoginConfigurationError()

    def _dependencies_live_locked(self, entry):
        for dependency in entry.dependencies:
            matching = [
                candidate
                for candidate in self._entries
                if candidate.category == dependency
            ]
            if matching and any(
                candidate.state != "terminal"
                and (
                    entry.require_terminal_dependencies
                    or candidate.blocks_dependents
                )
                for candidate in matching
            ):
                return True
        return False

    def _normalize_interrupted_entries_locked(self):
        for entry in self._entries:
            if entry.state == "closing":
                entry.state = "unresolved"
                entry.blocks_dependents = True
            elif entry.state == "terminalizing":
                self._terminalize_entry_locked(entry)

    @staticmethod
    def _terminalize_entry_locked(entry):
        entry.state = "terminalizing"
        entry.blocks_dependents = False
        entry.resource = None
        entry.action = None
        entry.probe = None
        entry.state = "terminal"

    def _recover_cleanup_entry(self, entry, *, terminal, failure):
        with self._condition:
            if failure:
                self._failure_categories.add(entry.category)
            if entry.state in {"closing", "terminalizing"}:
                if terminal or entry.state == "terminalizing":
                    self._terminalize_entry_locked(entry)
                else:
                    entry.state = "unresolved"
                    entry.blocks_dependents = True
        return True

    def _release_cleanup_owner(self, owner_token):
        first_control = None
        report = self._last_report
        while True:
            try:
                with self._condition:
                    if self._owner_token != owner_token:
                        return self._last_report, first_control
                    self._normalize_interrupted_entries_locked()
                    report = self._snapshot_locked()
                    self._last_report = report
                    self._owner_thread = None
                    self._owner_active = False
                    self._condition.notify_all()
                    self._owner_token = None
                return report, first_control
            except (
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ) as exc:
                if first_control is None:
                    first_control = exc
                _sanitize_exception_graph(exc)
                exc = None
            except Exception as exc:
                _sanitize_exception_graph(exc)
                exc = None

    def _snapshot_locked(self):
        categories = {
            entry.category
            for entry in self._entries
        }
        unresolved_categories = {
            entry.category
            for entry in self._entries
            if entry.state != "terminal"
        }
        closed = tuple(
            category
            for category in _CLEANUP_RESOURCE_ORDER
            if (
                category in categories
                and category not in unresolved_categories
            )
        )
        unresolved = tuple(
            category
            for category in _CLEANUP_RESOURCE_ORDER
            if category in unresolved_categories
        )
        return _CleanupReport(
            closed_resources=closed,
            unresolved_resources=unresolved,
            cleanup_complete=not unresolved,
            failure_categories=tuple(
                category
                for category in _CLEANUP_RESOURCE_ORDER
                if category in self._failure_categories
            ),
        )

    def __repr__(self):
        report = self.snapshot()
        return (
            "_CleanupCoordinator("
            f"complete={report.cleanup_complete}, "
            f"unresolved={len(report.unresolved_resources)})"
        )

    __str__ = __repr__


_FILE_REFERENCE_CAPABILITY = object()
_DATABASE_TARGET_CAPABILITY = object()
_WORKER_OUTCOME_CAPABILITY = object()
_PENDING_ACTIVATION_CAPABILITY = object()
_HANDOFF_RESERVATION_CAPABILITY = object()
_HANDOFF_LEASE_POOL_CAPABILITY = object()
_HANDOFF_RESERVATION_RELEASED = object()
_HANDOFF_RESERVATION_CONFLICT = object()
_DATABASE_INTERNAL_BORROW_CAPABILITY = object()
_EMERGENCY_HANDOFF_LEASES = tuple(
    _ActivationHandoffCleanupLease(
        None,
        _capability=_HANDOFF_LEASE_POOL_CAPABILITY,
    )
    for _index in range(64)
)


def _new_database_process_proof():
    proof = os.urandom(32)
    if type(proof) is not bytes or len(proof) != 32:
        raise DurableGoogleLoginConfigurationError()
    return proof


def _new_database_connection_proof():
    proof = os.urandom(32)
    if type(proof) is not bytes or len(proof) != 32:
        raise DurableGoogleLoginConfigurationError()
    return proof


class _DatabaseProcessEpoch:
    __slots__ = ("__pid", "__proof", "__token")

    def __init__(self, pid, proof=None):
        if type(pid) is not int or pid < 1:
            raise DurableGoogleLoginConfigurationError()
        if proof is None:
            proof = _new_database_process_proof()
        if type(proof) is not bytes or len(proof) != 32:
            raise DurableGoogleLoginConfigurationError()
        object.__setattr__(self, "_DatabaseProcessEpoch__pid", pid)
        object.__setattr__(self, "_DatabaseProcessEpoch__proof", proof)
        object.__setattr__(self, "_DatabaseProcessEpoch__token", object())

    @property
    def pid(self):
        return self.__pid

    @property
    def proof(self):
        return self.__proof

    @property
    def token(self):
        return self.__token

    def __repr__(self):
        return "_DatabaseProcessEpoch(<sealed>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("database_process_epoch_not_serializable")

    def __copy__(self):
        raise TypeError("database_process_epoch_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("database_process_epoch_not_copyable")

    def __setattr__(self, _name, _value):
        raise AttributeError("database_process_epoch_is_immutable")

    def __delattr__(self, _name):
        raise AttributeError("database_process_epoch_is_immutable")


_DATABASE_PROCESS_EPOCH = None
_DATABASE_PROCESS_EPOCH_PUBLICATION_LOCK = threading.Lock()
_DATABASE_PROCESS_EPOCH_PUBLICATION_PID = os.getpid()


def _reset_database_process_epoch_after_fork():
    global _DATABASE_PROCESS_EPOCH
    global _DATABASE_PROCESS_EPOCH_PUBLICATION_LOCK
    global _DATABASE_PROCESS_EPOCH_PUBLICATION_PID
    _DATABASE_PROCESS_EPOCH = None
    _DATABASE_PROCESS_EPOCH_PUBLICATION_LOCK = threading.Lock()
    _DATABASE_PROCESS_EPOCH_PUBLICATION_PID = os.getpid()


if "_DATABASE_PROCESS_EPOCH_AT_FORK_CALLBACK" not in globals():
    def _database_process_epoch_after_fork_callback():
        _reset_database_process_epoch_after_fork()

    _DATABASE_PROCESS_EPOCH_AT_FORK_CALLBACK = (
        _database_process_epoch_after_fork_callback
    )
    _DATABASE_PROCESS_EPOCH_AT_FORK_REGISTERED = False


if (
    hasattr(os, "register_at_fork")
    and not _DATABASE_PROCESS_EPOCH_AT_FORK_REGISTERED
):
    os.register_at_fork(
        after_in_child=_DATABASE_PROCESS_EPOCH_AT_FORK_CALLBACK
    )
    _DATABASE_PROCESS_EPOCH_AT_FORK_REGISTERED = True


def _database_process_epoch_is_current_without_lock(epoch):
    return (
        type(epoch) is _DatabaseProcessEpoch
        and epoch is _DATABASE_PROCESS_EPOCH
        and epoch.pid == os.getpid()
    )


def _current_database_process_epoch():
    global _DATABASE_PROCESS_EPOCH
    global _DATABASE_PROCESS_EPOCH_PUBLICATION_LOCK
    global _DATABASE_PROCESS_EPOCH_PUBLICATION_PID
    while True:
        pid = os.getpid()
        current = _DATABASE_PROCESS_EPOCH
        if (
            type(current) is _DatabaseProcessEpoch
            and current.pid == pid
        ):
            return current

        proof = _new_database_process_proof()
        if os.getpid() != pid:
            proof = None
            continue
        candidate = _DatabaseProcessEpoch(pid, proof)
        proof = None

        if _DATABASE_PROCESS_EPOCH_PUBLICATION_PID != pid:
            _reset_database_process_epoch_after_fork()
            candidate = None
            continue
        publication_lock = _DATABASE_PROCESS_EPOCH_PUBLICATION_LOCK
        with publication_lock:
            if os.getpid() != pid:
                candidate = None
                continue
            current = _DATABASE_PROCESS_EPOCH
            if (
                type(current) is _DatabaseProcessEpoch
                and current.pid == pid
            ):
                candidate = None
                return current
            _DATABASE_PROCESS_EPOCH = candidate
            return candidate


def _require_current_database_process(epoch):
    if not _database_process_epoch_is_current_without_lock(epoch):
        raise DurableGoogleLoginConfigurationError()
    return True


def _publish_database_call_result(offer, callback, arguments):
    if (
        type(offer) is not list
        or offer
        or not callable(callback)
        or type(arguments) is not tuple
    ):
        raise DurableGoogleLoginConfigurationError()
    publication = next(
        map(
            offer.append,
            itertools.starmap(callback, (arguments,)),
        )
    )
    if publication is not None or len(offer) != 1:
        raise DurableGoogleLoginConfigurationError()
    return True


def _publish_database_descriptor_handle(offer, path):
    if type(offer) is not list or offer or type(path) is not _PATH_TYPE:
        raise DurableGoogleLoginConfigurationError()
    producer = functools.partial(
        open,
        path,
        mode="rb",
        buffering=0,
    )
    try:
        return _publish_database_call_result(offer, producer, ())
    finally:
        producer = None


class _DatabaseDescriptorOwnership:
    __slots__ = (
        "_generation",
        "_handle_offer",
        "_issuance",
        "_lock",
        "_manager_identity",
        "_process_epoch",
        "_state",
    )

    def __init__(
        self,
        *,
        process_epoch,
        manager_identity,
        generation,
    ):
        _require_current_database_process(process_epoch)
        if (
            manager_identity is None
            or type(generation) is not int
            or generation < 1
        ):
            raise DurableGoogleLoginConfigurationError()
        self._process_epoch = process_epoch
        self._manager_identity = manager_identity
        self._generation = generation
        self._issuance = object()
        self._handle_offer = []
        self._lock = threading.Lock()
        self._state = "pending"

    def open(self, target, *, require_stable_metadata=False):
        _require_current_database_process(self._process_epoch)
        if (
            type(target) is not _DatabaseTargetAuthority
            or type(require_stable_metadata) is not bool
        ):
            raise DurableGoogleLoginConfigurationError()
        with self._lock:
            if self._state != "pending" or self._handle_offer:
                raise DurableGoogleLoginConfigurationError()
            self._state = "creating"
        path = _database_target_path(target)
        _publish_database_descriptor_handle(self._handle_offer, path)
        with self._lock:
            if self._state != "creating" or len(self._handle_offer) != 1:
                raise DurableGoogleLoginConfigurationError()
            handle = self._handle_offer[0]
            if (
                not callable(getattr(handle, "close", None))
                or not callable(getattr(handle, "fileno", None))
                or getattr(handle, "closed", None) is not False
                or os.get_inheritable(handle.fileno())
            ):
                raise DurableGoogleLoginConfigurationError()
            self._state = "owned"
        try:
            _verify_pinned_database_target(
                self,
                target,
                require_stable_metadata=require_stable_metadata,
            )
            handle.seek(0)
            header = handle.read(_SQLITE_HEADER_BYTES)
            if (
                type(header) is not bytes
                or len(header) != _SQLITE_HEADER_BYTES
                or header[:16] != b"SQLite format 3\x00"
                or header[18:20] != b"\x01\x01"
            ):
                raise DurableGoogleLoginConfigurationError()
            _verify_pinned_database_target(
                self,
                target,
                require_stable_metadata=require_stable_metadata,
            )
        finally:
            handle = None
            header = None
            path = None
        return self

    def descriptor_for_validation(self):
        _require_current_database_process(self._process_epoch)
        with self._lock:
            if self._state != "owned" or len(self._handle_offer) != 1:
                raise DurableGoogleLoginConfigurationError()
            handle = self._handle_offer[0]
            if getattr(handle, "closed", None) is not False:
                raise DurableGoogleLoginConfigurationError()
            return handle.fileno()

    def close(self):
        _require_current_database_process(self._process_epoch)
        with self._lock:
            if self._state == "terminal":
                return True
            if not self._handle_offer:
                self._state = "terminal"
                return True
            if len(self._handle_offer) != 1:
                self._state = "unresolved"
                raise _DatabaseCleanupFailure()
            handle = self._handle_offer[0]
            self._state = "closing"
        first_control = None
        failed = False
        try:
            handle.close()
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ) as exc:
            first_control = exc
            _sanitize_exception_graph(exc)
            exc = None
            failed = True
        except Exception as exc:
            _sanitize_exception_graph(exc)
            exc = None
            failed = True
        try:
            terminal = handle.closed is True
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ) as exc:
            if first_control is None:
                first_control = exc
            _sanitize_exception_graph(exc)
            exc = None
            terminal = False
            failed = True
        except Exception as exc:
            _sanitize_exception_graph(exc)
            exc = None
            terminal = False
            failed = True
        with self._lock:
            if (
                len(self._handle_offer) == 1
                and self._handle_offer[0] is handle
            ):
                if terminal:
                    self._handle_offer.clear()
                    self._state = "terminal"
                else:
                    self._state = "unresolved"
        handle = None
        if first_control is not None:
            propagated = first_control
            first_control = None
            raise propagated from None
        if failed:
            raise _DatabaseCleanupFailure()
        return terminal

    @property
    def terminal(self):
        _require_current_database_process(self._process_epoch)
        with self._lock:
            return self._state == "terminal" and not self._handle_offer

    def __repr__(self):
        return "_DatabaseDescriptorOwnership(<sealed>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("database_descriptor_ownership_not_serializable")

    def __copy__(self):
        raise TypeError("database_descriptor_ownership_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("database_descriptor_ownership_not_copyable")


class _DatabaseStatementGuard:
    __slots__ = ("_first_statement", "_owner", "_target")

    def __init__(self, target, owner=None):
        if type(target) is not _DatabaseTargetAuthority:
            raise DurableGoogleLoginConfigurationError()
        if owner is not None and type(owner) is not _DatabaseConnectionOwnership:
            raise DurableGoogleLoginConfigurationError()
        self._target = target
        self._owner = owner
        self._first_statement = True

    def __call__(
        self,
        action_code,
        parameter_one,
        parameter_two,
        _database_name,
        _trigger_name,
    ):
        try:
            if (
                self._owner is not None
                and self._owner.statement_authorized() is not True
            ):
                return sqlite3.SQLITE_DENY
            _reverify_database_target(
                self._target,
                require_no_sidecars=self._first_statement,
            )
            self._first_statement = False
            if action_code in _FORBIDDEN_SQLITE_ACTIONS:
                return sqlite3.SQLITE_DENY
            if (
                action_code == sqlite3.SQLITE_PRAGMA
                and type(parameter_one) is str
                and parameter_one.casefold() in _FORBIDDEN_MUTATING_PRAGMAS
                and parameter_two is not None
            ):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        except BaseException as exc:
            _sanitize_exception_graph(exc)
            exc = None
            return sqlite3.SQLITE_DENY


_FORBIDDEN_SQLITE_ACTIONS = frozenset(
    getattr(sqlite3, name)
    for name in (
        "SQLITE_ATTACH",
        "SQLITE_DETACH",
        "SQLITE_ALTER_TABLE",
        "SQLITE_CREATE_INDEX",
        "SQLITE_CREATE_TABLE",
        "SQLITE_CREATE_TEMP_INDEX",
        "SQLITE_CREATE_TEMP_TABLE",
        "SQLITE_CREATE_TEMP_TRIGGER",
        "SQLITE_CREATE_TEMP_VIEW",
        "SQLITE_CREATE_TRIGGER",
        "SQLITE_CREATE_VIEW",
        "SQLITE_DROP_INDEX",
        "SQLITE_DROP_TABLE",
        "SQLITE_DROP_TEMP_INDEX",
        "SQLITE_DROP_TEMP_TABLE",
        "SQLITE_DROP_TEMP_TRIGGER",
        "SQLITE_DROP_TEMP_VIEW",
        "SQLITE_DROP_TRIGGER",
        "SQLITE_DROP_VIEW",
    )
    if hasattr(sqlite3, name)
)
_FORBIDDEN_MUTATING_PRAGMAS = frozenset(
    {
        "application_id",
        "auto_vacuum",
        "journal_mode",
        "legacy_file_format",
        "locking_mode",
        "page_size",
        "schema_version",
        "user_version",
        "writable_schema",
    }
)


class _DatabaseConnectionOwnership:
    __slots__ = (
        "_browser_cleanup_capability",
        "_browser_cleanup_borrower_token",
        "_browser_cleanup_claim",
        "_browser_cleanup_delegate",
        "_browser_cleanup_identity",
        "_browser_cleanup_mode",
        "_borrower_thread",
        "_borrower_token",
        "_cleanup_thread",
        "_cleanup_token",
        "_connection_identity",
        "_connection_offer",
        "_descriptor_owner",
        "_generation",
        "_issuance",
        "_manager",
        "_manager_identity",
        "_opening_abandoned",
        "_opening_thread",
        "_process_epoch",
        "_proof",
        "_release_thread",
        "_release_token",
        "_shutdown_requested",
        "_state",
    )

    def __init__(self, manager, *, generation, proof):
        if (
            type(manager) is not _RuntimeDatabaseConnections
            or type(generation) is not int
            or generation < 1
            or type(proof) is not bytes
            or len(proof) != 32
        ):
            raise DurableGoogleLoginConfigurationError()
        _require_current_database_process(manager._process_epoch)
        self._manager = manager
        self._process_epoch = manager._process_epoch
        self._manager_identity = manager._identity
        self._generation = generation
        self._issuance = object()
        self._proof = proof
        self._connection_identity = None
        self._connection_offer = []
        self._descriptor_owner = _DatabaseDescriptorOwnership(
            process_epoch=self._process_epoch,
            manager_identity=self._manager_identity,
            generation=generation,
        )
        self._opening_thread = threading.current_thread()
        self._opening_abandoned = False
        self._browser_cleanup_capability = None
        self._browser_cleanup_borrower_token = None
        self._browser_cleanup_claim = None
        self._browser_cleanup_delegate = None
        self._browser_cleanup_identity = None
        self._browser_cleanup_mode = None
        self._borrower_thread = None
        self._borrower_token = None
        self._release_thread = None
        self._release_token = None
        self._cleanup_thread = None
        self._cleanup_token = None
        self._shutdown_requested = False
        self._state = "opening"

    def __call__(self, database, **arguments):
        _require_current_database_process(self._process_epoch)
        if (
            type(database) is not str
            or set(arguments) != {"factory"}
            or arguments["factory"] is not self
        ):
            raise DurableGoogleLoginConfigurationError()
        producer = functools.partial(
            sqlite3.Connection,
            database,
            timeout=2.0,
            detect_types=0,
            isolation_level="",
            check_same_thread=False,
            cached_statements=0,
            uri=True,
        )
        _publish_database_call_result(
            self._connection_offer,
            producer,
            (),
        )
        producer = None
        connection = self._connection_offer[0]
        if type(connection) is not sqlite3.Connection:
            raise DurableGoogleLoginConfigurationError()
        self._connection_identity = connection
        return connection

    def open(
        self,
        target,
        *,
        mode,
        verify_schema,
        install_guard,
    ):
        _require_current_database_process(self._process_epoch)
        if (
            type(target) is not _DatabaseTargetAuthority
            or mode not in {"ro", "rw"}
            or type(verify_schema) is not bool
            or type(install_guard) is not bool
        ):
            raise DurableGoogleLoginConfigurationError()
        self._descriptor_owner.open(target)
        path = _database_target_path(target)
        connection = sqlite3.connect(
            _sqlite_file_uri(path, mode=mode),
            factory=self,
        )
        if (
            len(self._connection_offer) != 1
            or self._connection_offer[0] is not connection
            or type(connection) is not sqlite3.Connection
        ):
            raise DurableGoogleLoginConfigurationError()
        _verify_open_database_target(connection, target)
        _verify_pinned_database_target(self._descriptor_owner, target)
        connection.row_factory = sqlite3.Row
        connection.text_factory = str
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "PRAGMA query_only = " + ("OFF" if mode == "rw" else "ON")
        )
        if mode == "rw":
            connection.execute("PRAGMA recursive_triggers = ON")
        if (
            connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
            or connection.execute("PRAGMA query_only").fetchone()[0]
            != (0 if mode == "rw" else 1)
            or (
                mode == "rw"
                and connection.execute(
                    "PRAGMA recursive_triggers"
                ).fetchone()[0]
                != 1
            )
            or connection.in_transaction
        ):
            raise DurableGoogleLoginConfigurationError()
        _verify_open_database_target(connection, target)
        _verify_pinned_database_target(self._descriptor_owner, target)
        if verify_schema:
            _attest_closed_database_schema(connection)
            _verify_pinned_database_target(self._descriptor_owner, target)
        if install_guard:
            connection.set_authorizer(_DatabaseStatementGuard(target, self))
        if self._descriptor_owner.close() is not True:
            raise DurableGoogleLoginConfigurationError()
        connection = None
        path = None
        return True

    def raw_connection(self):
        _require_current_database_process(self._process_epoch)
        if len(self._connection_offer) != 1:
            raise DurableGoogleLoginConfigurationError()
        connection = self._connection_offer[0]
        if type(connection) is not sqlite3.Connection:
            raise DurableGoogleLoginConfigurationError()
        return connection

    def statement_authorized(self):
        try:
            _require_current_database_process(self._process_epoch)
        except DurableGoogleLoginConfigurationError:
            return False
        manager = self._manager
        current = threading.current_thread()
        with manager._condition:
            if self._manager_identity is not manager._identity:
                return False
            if self._state in {"leased", "close_pending"}:
                authorized = self._borrower_thread is current
                requires_lifetime = authorized
            elif self._state == "rollback_pending":
                authorized = self._release_thread is current
                requires_lifetime = False
            elif self._state == "closing":
                authorized = self._cleanup_thread is current
                requires_lifetime = False
            else:
                authorized = (
                    self._state == "opening"
                    and self._opening_thread is current
                )
                requires_lifetime = authorized
        if not authorized:
            return False
        if requires_lifetime:
            try:
                manager.require_database_lifetime_ownership()
            except DurableGoogleLoginConfigurationError:
                return False
        return True

    def cleanup_owned_resources(self):
        _require_current_database_process(self._process_epoch)
        first_control = None
        first_error = None
        cleanup_failed = False
        descriptor_terminal = False
        try:
            descriptor_terminal = self._descriptor_owner.close()
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ) as exc:
            first_control = exc
            _sanitize_exception_graph(exc)
            exc = None
            cleanup_failed = True
        except Exception as exc:
            first_error = exc
            _sanitize_exception_graph(exc)
            exc = None
            cleanup_failed = True
        connection_terminal = not self._connection_offer
        if self._connection_offer:
            connection = self._connection_offer[0]
            control = None
            try:
                (
                    connection_terminal,
                    failed,
                    control,
                ) = _cleanup_database_connection_independently(
                    connection,
                    rollback=True,
                )
                cleanup_failed = cleanup_failed or failed
                if first_control is None and control is not None:
                    first_control = control
            except (
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ) as exc:
                if first_control is None:
                    first_control = exc
                _sanitize_exception_graph(exc)
                exc = None
                cleanup_failed = True
            except Exception as exc:
                if first_error is None:
                    first_error = exc
                _sanitize_exception_graph(exc)
                exc = None
                cleanup_failed = True
            if not connection_terminal:
                try:
                    connection_terminal = (
                        _database_connection_is_closed(connection)
                    )
                except (
                    KeyboardInterrupt,
                    SystemExit,
                    GeneratorExit,
                ) as exc:
                    if first_control is None:
                        first_control = exc
                    _sanitize_exception_graph(exc)
                    exc = None
                    cleanup_failed = True
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
                    _sanitize_exception_graph(exc)
                    exc = None
                    cleanup_failed = True
            if connection_terminal:
                self._connection_offer.clear()
            connection = None
            control = None
        terminal = descriptor_terminal and connection_terminal
        if first_control is not None:
            propagated = first_control
            first_control = None
            raise propagated from None
        if first_error is not None:
            propagated = first_error
            first_error = None
            raise propagated from None
        if cleanup_failed:
            raise _DatabaseCleanupFailure()
        return terminal

    def __repr__(self):
        return "_DatabaseConnectionOwnership(<sealed>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("database_connection_ownership_not_serializable")

    def __copy__(self):
        raise TypeError("database_connection_ownership_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("database_connection_ownership_not_copyable")


class _DatabaseLeaseOperationAuthority:
    __slots__ = (
        "__borrower_thread",
        "__borrower_token",
        "__generation",
        "__manager_identity",
        "__pid",
        "__process_epoch",
        "__record_issuance",
        "__seal",
    )

    def __init__(
        self,
        *,
        process_epoch,
        manager_identity,
        record_issuance,
        generation,
        borrower_token,
        borrower_thread,
    ):
        if (
            type(process_epoch) is not _DatabaseProcessEpoch
            or manager_identity is None
            or record_issuance is None
            or type(generation) is not int
            or generation < 1
            or borrower_token is None
            or not isinstance(borrower_thread, threading.Thread)
        ):
            raise DurableGoogleLoginConfigurationError()
        object.__setattr__(
            self,
            "_DatabaseLeaseOperationAuthority__process_epoch",
            process_epoch,
        )
        object.__setattr__(
            self,
            "_DatabaseLeaseOperationAuthority__pid",
            process_epoch.pid,
        )
        object.__setattr__(
            self,
            "_DatabaseLeaseOperationAuthority__manager_identity",
            manager_identity,
        )
        object.__setattr__(
            self,
            "_DatabaseLeaseOperationAuthority__record_issuance",
            record_issuance,
        )
        object.__setattr__(
            self,
            "_DatabaseLeaseOperationAuthority__generation",
            generation,
        )
        object.__setattr__(
            self,
            "_DatabaseLeaseOperationAuthority__borrower_token",
            borrower_token,
        )
        object.__setattr__(
            self,
            "_DatabaseLeaseOperationAuthority__borrower_thread",
            borrower_thread,
        )
        object.__setattr__(
            self,
            "_DatabaseLeaseOperationAuthority__seal",
            _DATABASE_INTERNAL_BORROW_CAPABILITY,
        )

    def _require_before_lock(self):
        if (
            type(self) is not _DatabaseLeaseOperationAuthority
            or self.__seal is not _DATABASE_INTERNAL_BORROW_CAPABILITY
            or os.getpid() != self.__pid
            or threading.current_thread() is not self.__borrower_thread
            or not _database_process_epoch_is_current_without_lock(
                self.__process_epoch
            )
        ):
            raise DurableGoogleLoginConfigurationError()
        return True

    def _matches(self, manager, record, token, thread):
        self._require_before_lock()
        return (
            type(manager) is _RuntimeDatabaseConnections
            and type(record) is _DatabaseConnectionOwnership
            and self.__manager_identity is manager._identity
            and self.__record_issuance is record._issuance
            and self.__generation == record._generation
            and self.__borrower_token is token
            and self.__borrower_thread is thread
        )

    def __repr__(self):
        return "_DatabaseLeaseOperationAuthority(<sealed>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("database_lease_operation_authority_not_serializable")

    def __copy__(self):
        raise TypeError("database_lease_operation_authority_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("database_lease_operation_authority_not_copyable")

    def __setattr__(self, _name, _value):
        raise AttributeError("database_lease_operation_authority_is_immutable")

    def __delattr__(self, _name):
        raise AttributeError("database_lease_operation_authority_is_immutable")


class _DatabaseConnectionLease:
    __slots__ = (
        "_browser_cleanup_capability",
        "_browser_cleanup_identity",
        "_delivery_authority",
        "_borrower_thread",
        "_generation",
        "_manager",
        "_process_epoch",
        "_record",
        "_released",
        "_standalone",
        "_token",
    )

    def __init__(
        self,
        manager,
        record,
        token,
        borrower_thread,
        *,
        standalone=False,
    ):
        if (
            type(manager) is not _RuntimeDatabaseConnections
            or type(record) is not _DatabaseConnectionOwnership
            or token is None
            or borrower_thread is None
            or type(standalone) is not bool
        ):
            raise DurableGoogleLoginConfigurationError()
        self._manager = manager
        self._record = record
        self._token = token
        self._borrower_thread = borrower_thread
        self._process_epoch = manager._process_epoch
        self._generation = record._generation
        self._standalone = standalone
        self._released = False
        self._browser_cleanup_capability = None
        self._browser_cleanup_identity = None
        self._delivery_authority = _DatabaseLeaseOperationAuthority(
            process_epoch=self._process_epoch,
            manager_identity=manager._identity,
            record_issuance=record._issuance,
            generation=self._generation,
            borrower_token=token,
            borrower_thread=borrower_thread,
        )

    def _require_borrower(self):
        _require_current_database_process(self._process_epoch)
        if threading.current_thread() is not self._borrower_thread:
            raise DurableGoogleLoginConfigurationError()
        if self._released:
            raise DurableGoogleLoginConfigurationError()
        return True

    def _borrow_internal_connection(self, capability):
        if capability is not _DATABASE_INTERNAL_BORROW_CAPABILITY:
            raise DurableGoogleLoginConfigurationError()
        self._require_borrower()
        return self._manager._connection_for_lease(self)

    def _wahojobs_delivery_authority(self):
        authority = self._delivery_authority
        self._wahojobs_validate_delivery_authority(authority, None)
        return authority

    def _wahojobs_validate_delivery_authority(
        self,
        authority,
        connection,
    ):
        if authority is not self._delivery_authority:
            raise DurableGoogleLoginConfigurationError()
        authority._require_before_lock()
        return self._manager._validate_delivery_authority(
            self,
            authority,
            connection,
        )

    def _wahojobs_register_browser_cleanup(
        self,
        delegate,
        delegate_identity,
        capability,
    ):
        self._require_borrower()
        if (
            self._browser_cleanup_identity is None
            and self._browser_cleanup_capability is None
        ):
            self._browser_cleanup_identity = delegate_identity
            self._browser_cleanup_capability = capability
        elif (
            self._browser_cleanup_identity is not delegate_identity
            or self._browser_cleanup_capability is not capability
        ):
            raise DurableGoogleLoginConfigurationError()
        return self._manager._register_browser_cleanup_delegate(
            self,
            delegate,
            delegate_identity,
            capability,
        )

    def _wahojobs_claim_abandoned_browser_cleanup(
        self,
        delegate,
        delegate_identity,
        capability,
        connection,
    ):
        _require_current_database_process(self._process_epoch)
        if self._released:
            if (
                self._browser_cleanup_identity is delegate_identity
                and self._browser_cleanup_capability is capability
            ):
                return self._retire()
            raise DurableGoogleLoginConfigurationError()
        if (
            self._browser_cleanup_identity is not delegate_identity
            or self._browser_cleanup_capability is not capability
        ):
            raise DurableGoogleLoginConfigurationError()
        return self._manager._claim_abandoned_browser_cleanup(
            self,
            delegate,
            delegate_identity,
            capability,
            connection,
        )

    def _wahojobs_finish_abandoned_browser_cleanup(
        self,
        delegate,
        delegate_identity,
        capability,
        claim,
    ):
        _require_current_database_process(self._process_epoch)
        if self._released:
            if (
                self._browser_cleanup_identity is delegate_identity
                and self._browser_cleanup_capability is capability
            ):
                return self._retire()
            raise DurableGoogleLoginConfigurationError()
        return self._manager._finish_abandoned_browser_cleanup(
            self,
            delegate,
            delegate_identity,
            capability,
            claim,
        )

    def _wahojobs_abandon_abandoned_browser_cleanup(
        self,
        delegate,
        delegate_identity,
        capability,
    ):
        _require_current_database_process(self._process_epoch)
        if self._released:
            return self._retire()
        if (
            self._browser_cleanup_identity is not delegate_identity
            or self._browser_cleanup_capability is not capability
        ):
            raise DurableGoogleLoginConfigurationError()
        return self._manager._abandon_abandoned_browser_cleanup(
            self,
            delegate,
            delegate_identity,
            capability,
        )

    def _wahojobs_relinquish_unregistered_browser_cleanup(
        self,
        delegate,
        delegate_identity,
        capability,
    ):
        _require_current_database_process(self._process_epoch)
        if self._released:
            return self._retire()
        relinquished = (
            self._manager._relinquish_unregistered_browser_cleanup(
                self,
                delegate,
                delegate_identity,
                capability,
            )
        )
        if relinquished:
            self._retire()
        return relinquished

    def _wahojobs_browser_cleanup_is_closed(
        self,
        delegate_identity,
        capability,
    ):
        _require_current_database_process(self._process_epoch)
        if (
            self._browser_cleanup_identity is not None
            and (
                self._browser_cleanup_identity is not delegate_identity
                or self._browser_cleanup_capability is not capability
            )
        ):
            raise DurableGoogleLoginConfigurationError()
        if self._released:
            return self._retire()
        return False

    def _wahojobs_acknowledge_abandoned_browser_cleanup(
        self,
        delegate,
        delegate_identity,
        capability,
    ):
        _require_current_database_process(self._process_epoch)
        if self._released:
            if (
                self._browser_cleanup_identity is delegate_identity
                and self._browser_cleanup_capability is capability
            ):
                return self._retire()
            raise DurableGoogleLoginConfigurationError()
        acknowledged = (
            self._manager._acknowledge_abandoned_browser_cleanup(
                self,
                delegate,
                delegate_identity,
                capability,
            )
        )
        if acknowledged:
            self._retire()
        return acknowledged

    def execute(self, *arguments, **keywords):
        connection = self._borrow_internal_connection(
            _DATABASE_INTERNAL_BORROW_CAPABILITY
        )
        cursor = connection.execute(*arguments, **keywords)
        return _DatabaseCursorLease(self, cursor)

    @property
    def in_transaction(self):
        connection = self._borrow_internal_connection(
            _DATABASE_INTERNAL_BORROW_CAPABILITY
        )
        return connection.in_transaction

    def close(self):
        _require_current_database_process(self._process_epoch)
        if self._released:
            self._retire()
            if self._standalone:
                return self._manager.close()
            return True
        if threading.current_thread() is not self._borrower_thread:
            raise DurableGoogleLoginConfigurationError()
        manager = self._manager
        try:
            released = manager._release_connection_lease(
                self,
                rollback=True,
            )
        except BaseException:
            try:
                manager._recover_interrupted_release(self)
            except BaseException as cleanup:
                _sanitize_exception_graph(cleanup)
                cleanup = None
            disposed = False
            try:
                disposed = manager._lease_disposed(self)
            except BaseException as cleanup:
                _sanitize_exception_graph(cleanup)
                cleanup = None
            if disposed:
                self._retire()
                if self._standalone:
                    try:
                        manager.close()
                    except BaseException as cleanup:
                        _sanitize_exception_graph(cleanup)
                        cleanup = None
            raise
        if released:
            self._retire()
            if self._standalone:
                return manager.close()
        return released

    def _retire(self):
        try:
            self._released = True
        finally:
            if self._released:
                try:
                    self._record = None
                finally:
                    try:
                        self._token = None
                    finally:
                        self._borrower_thread = None
        return True

    @property
    def closed(self):
        _require_current_database_process(self._process_epoch)
        if not self._released:
            return False
        if not self._standalone:
            return True
        return self._manager.closed

    def __repr__(self):
        return "_DatabaseConnectionLease(<sealed>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("database_connection_lease_not_serializable")

    def __copy__(self):
        raise TypeError("database_connection_lease_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("database_connection_lease_not_copyable")


class _DatabaseCursorLease:
    __slots__ = ("_cursor", "_lease")

    def __init__(self, lease, cursor):
        if (
            type(lease) is not _DatabaseConnectionLease
            or type(cursor) is not sqlite3.Cursor
        ):
            raise DurableGoogleLoginConfigurationError()
        lease._require_borrower()
        self._lease = lease
        self._cursor = cursor

    def _require_cursor(self):
        lease = self._lease
        cursor = self._cursor
        if (
            type(lease) is not _DatabaseConnectionLease
            or type(cursor) is not sqlite3.Cursor
        ):
            raise DurableGoogleLoginConfigurationError()
        connection = lease._borrow_internal_connection(
            _DATABASE_INTERNAL_BORROW_CAPABILITY
        )
        if cursor.connection is not connection:
            raise DurableGoogleLoginConfigurationError()
        return cursor

    def fetchone(self):
        return self._require_cursor().fetchone()

    def fetchmany(self, size=None):
        cursor = self._require_cursor()
        if size is None:
            return cursor.fetchmany()
        return cursor.fetchmany(size)

    def fetchall(self):
        return self._require_cursor().fetchall()

    def close(self):
        cursor = self._require_cursor()
        cursor.close()
        self._cursor = None
        self._lease = None
        return True

    def __iter__(self):
        self._require_cursor()
        return self

    def __next__(self):
        return next(self._require_cursor())

    @property
    def description(self):
        return self._require_cursor().description

    @property
    def lastrowid(self):
        return self._require_cursor().lastrowid

    @property
    def rowcount(self):
        return self._require_cursor().rowcount

    def __repr__(self):
        return "_DatabaseCursorLease(<sealed>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("database_cursor_lease_not_serializable")

    def __copy__(self):
        raise TypeError("database_cursor_lease_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("database_cursor_lease_not_copyable")


class _RuntimeDatabaseConnections:
    __slots__ = (
        "_accepting",
        "_condition",
        "_identity",
        "_lifetime_ownership",
        "_next_generation",
        "_process_epoch",
        "_records",
        "_target",
    )

    def __init__(self, target, *, lifetime_ownership=None):
        if (
            type(target) is not _DatabaseTargetAuthority
            or (
                lifetime_ownership is not None
                and type(lifetime_ownership)
                is not DatabaseLifetimeOwnership
            )
        ):
            raise DurableGoogleLoginConfigurationError()
        self._process_epoch = _current_database_process_epoch()
        self._identity = object()
        self._target = target
        self._lifetime_ownership = lifetime_ownership
        self._accepting = True
        self._condition = threading.Condition(threading.Lock())
        self._records = {}
        self._next_generation = 1

    def require_current_process(self):
        return _require_current_database_process(self._process_epoch)

    def require_database_lifetime_ownership(self):
        self.require_current_process()
        with self._condition:
            target = self._target
            ownership = self._lifetime_ownership
        if target is None:
            raise DurableGoogleLoginConfigurationError()
        if ownership is None:
            return True
        try:
            return require_database_lifetime_ownership(
                ownership,
                role=ROLE_DURABLE_RUNTIME,
                database_path=_database_target_path(target),
            )
        except DatabaseLifetimeOwnershipError:
            raise DurableGoogleLoginConfigurationError() from None

    def _attestation_lifetime_ownership(self):
        self.require_database_lifetime_ownership()
        with self._condition:
            target = self._target
            ownership = self._lifetime_ownership
        if (
            target is None
            or type(ownership) is not DatabaseLifetimeOwnership
        ):
            raise DurableGoogleLoginConfigurationError()
        return ownership

    def open_writable_connection(self):
        self.require_database_lifetime_ownership()
        return self._finish_connection_open(
            mode="rw",
            verify_schema=True,
            install_guard=True,
        )

    def read_only_connection_provider(self):
        self.require_database_lifetime_ownership()
        return _managed_read_only_connection_scope(self)

    def writable_connection_provider(self):
        self.require_database_lifetime_ownership()
        return _managed_writable_connection_scope(self)

    def guarded_read_only_connection_provider(self):
        self.require_database_lifetime_ownership()
        return _managed_read_only_lease_scope(self)

    def close(self):
        self.require_current_process()
        first_control = None
        cleanup_failed = False
        claimed = []
        caller = threading.current_thread()
        terminal = False
        try:
            with self._condition:
                self._accepting = False
                self._prune_terminal_locked()
                for record in tuple(self._records.values()):
                    if record._state == "opening":
                        record._shutdown_requested = True
                        opening_thread = record._opening_thread
                        if (
                            not record._opening_abandoned
                            and opening_thread is not None
                            and opening_thread.is_alive()
                        ):
                            continue
                        record._opening_thread = None
                        record._opening_abandoned = True
                        token = object()
                        claimed.append((record, token))
                        record._cleanup_thread = caller
                        record._cleanup_token = token
                        record._state = "closing"
                        continue
                    if record._browser_cleanup_delegate is not None:
                        if record._state in {"leased", "close_pending"}:
                            record._state = "close_pending"
                            continue
                        if record._state == "rollback_pending":
                            owner = record._release_thread
                            if (
                                owner is not caller
                                and owner is not None
                                and owner.is_alive()
                            ):
                                continue
                            record._release_thread = None
                            record._release_token = None
                            record._state = "unresolved"
                            continue
                        if record._state == "closing":
                            owner = record._cleanup_thread
                            if (
                                owner is not caller
                                and owner is not None
                                and owner.is_alive()
                            ):
                                continue
                            if (
                                record._browser_cleanup_claim
                                is record._cleanup_token
                            ):
                                record._browser_cleanup_claim = None
                            record._cleanup_thread = None
                            record._cleanup_token = None
                            record._state = "unresolved"
                            continue
                        if record._state in {"unresolved", "ready"}:
                            continue
                    if record._state in {"leased", "close_pending"}:
                        record._state = "close_pending"
                        borrower = record._borrower_thread
                        if (
                            borrower is None
                            or not borrower.is_alive()
                        ):
                            record._borrower_thread = None
                            record._borrower_token = None
                            token = object()
                            claimed.append((record, token))
                            record._cleanup_thread = caller
                            record._cleanup_token = token
                            record._state = "closing"
                        continue
                    if record._state == "rollback_pending":
                        owner = record._release_thread
                        if (
                            owner is not caller
                            and owner is not None
                            and owner.is_alive()
                        ):
                            continue
                        record._release_thread = None
                        record._release_token = None
                        record._state = "unresolved"
                    if record._state == "closing":
                        owner = record._cleanup_thread
                        if (
                            owner is not caller
                            and owner is not None
                            and owner.is_alive()
                        ):
                            continue
                    if record._state in {"closing", "unresolved", "ready"}:
                        token = object()
                        claimed.append((record, token))
                        record._cleanup_thread = caller
                        record._cleanup_token = token
                        record._state = "closing"
            for record, token in claimed:
                try:
                    self._cleanup_claimed_record(record, token)
                except (
                    KeyboardInterrupt,
                    SystemExit,
                    GeneratorExit,
                ) as exc:
                    if first_control is None:
                        first_control = exc
                    _sanitize_exception_graph(exc)
                    exc = None
                    cleanup_failed = True
                except Exception as exc:
                    _sanitize_exception_graph(exc)
                    exc = None
                    cleanup_failed = True
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ) as exc:
            if first_control is None:
                first_control = exc
            _sanitize_exception_graph(exc)
            exc = None
            cleanup_failed = True
        except Exception as exc:
            _sanitize_exception_graph(exc)
            exc = None
            cleanup_failed = True
        finally:
            with self._condition:
                for record, token in claimed:
                    if (
                        self._records.get(record._issuance) is record
                        and record._state == "closing"
                        and record._cleanup_token is token
                    ):
                        record._cleanup_thread = None
                        record._cleanup_token = None
                        record._state = "unresolved"
                self._prune_terminal_locked()
                terminal = not self._records
                if terminal:
                    self._target = None
                    self._lifetime_ownership = None
                self._condition.notify_all()
        if first_control is not None:
            propagated = first_control
            first_control = None
            raise propagated from None
        if cleanup_failed:
            raise _DatabaseCleanupFailure()
        return terminal

    @property
    def closed(self):
        self.require_current_process()
        with self._condition:
            self._prune_terminal_locked()
            return (
                not self._accepting
                and not self._records
                and self._target is None
            )

    def _open_read_only_connection(self):
        self.require_database_lifetime_ownership()
        return self._finish_connection_open(
            mode="ro",
            verify_schema=True,
            install_guard=True,
        )

    def _finish_connection_open(
        self,
        *,
        mode,
        verify_schema,
        install_guard,
        standalone=False,
    ):
        self.require_database_lifetime_ownership()
        proof = _new_database_connection_proof()
        record = None
        lease = None
        cleanup_after_open = False
        try:
            with self._condition:
                if not self._accepting or self._target is None:
                    raise DurableGoogleLoginConfigurationError()
                self._prune_terminal_locked()
                target = self._target
                generation = self._next_generation
                self._next_generation += 1
            record = _DatabaseConnectionOwnership(
                self,
                generation=generation,
                proof=proof,
            )
            proof = None
            with self._condition:
                if (
                    not self._accepting
                    or self._target is not target
                    or not _database_process_epoch_is_current_without_lock(
                        self._process_epoch
                    )
                ):
                    raise DurableGoogleLoginConfigurationError()
                self._records[record._issuance] = record
            record.open(
                target,
                mode=mode,
                verify_schema=verify_schema,
                install_guard=install_guard,
            )
            with self._condition:
                if (
                    record._state != "opening"
                    or self._records.get(record._issuance) is not record
                ):
                    raise DurableGoogleLoginConfigurationError()
                if (
                    self._accepting
                    and self._target is not None
                    and not record._shutdown_requested
                ):
                    borrower = threading.current_thread()
                    token = object()
                    record._borrower_thread = borrower
                    record._borrower_token = token
                    record._opening_thread = None
                    record._state = "leased"
                    lease = _DatabaseConnectionLease(
                        self,
                        record,
                        token,
                        borrower,
                        standalone=standalone,
                    )
                else:
                    record._opening_thread = None
                    record._state = "closing"
                    record._cleanup_thread = threading.current_thread()
                    record._cleanup_token = object()
                    cleanup_after_open = True
            if lease is not None:
                self.require_database_lifetime_ownership()
                return lease
            if cleanup_after_open:
                self._cleanup_claimed_record(
                    record,
                    record._cleanup_token,
                )
            raise DurableGoogleLoginConfigurationError()
        except BaseException:
            if record is not None:
                self._abort_open_preserving_primary(record)
            raise

    def _connection_for_lease(self, lease):
        self.require_current_process()
        if type(lease) is not _DatabaseConnectionLease:
            raise DurableGoogleLoginConfigurationError()
        record = lease._record
        with self._condition:
            if (
                record is None
                or record._manager is not self
                or record._manager_identity is not self._identity
                or record._generation != lease._generation
                or record._borrower_token is not lease._token
                or record._borrower_thread is not threading.current_thread()
                or record._state not in {"leased", "close_pending"}
                or self._records.get(record._issuance) is not record
            ):
                raise DurableGoogleLoginConfigurationError()
            return record.raw_connection()

    def _validate_delivery_authority(
        self,
        lease,
        authority,
        connection,
    ):
        if (
            type(lease) is not _DatabaseConnectionLease
            or type(authority) is not _DatabaseLeaseOperationAuthority
            or (
                connection is not None
                and type(connection) is not sqlite3.Connection
            )
        ):
            raise DurableGoogleLoginConfigurationError()
        authority._require_before_lock()
        record = lease._record
        caller = threading.current_thread()
        with self._condition:
            if (
                record is None
                or not authority._matches(
                    self,
                    record,
                    lease._token,
                    caller,
                )
                or record._manager is not self
                or record._manager_identity is not self._identity
                or record._borrower_token is not lease._token
                or record._borrower_thread is not caller
                or record._state not in {"leased", "close_pending"}
                or self._records.get(record._issuance) is not record
                or (
                    connection is not None
                    and (
                        len(record._connection_offer) != 1
                        or record._connection_offer[0] is not connection
                    )
                )
            ):
                raise DurableGoogleLoginConfigurationError()
        return True

    def _register_browser_cleanup_delegate(
        self,
        lease,
        delegate,
        delegate_identity,
        capability,
    ):
        self.require_current_process()
        if (
            type(lease) is not _DatabaseConnectionLease
            or delegate is None
            or delegate_identity is None
            or capability is None
        ):
            raise DurableGoogleLoginConfigurationError()
        record = lease._record
        caller = threading.current_thread()
        with self._condition:
            if (
                record is None
                or record._manager is not self
                or record._manager_identity is not self._identity
                or record._generation != lease._generation
                or record._borrower_token is not lease._token
                or record._borrower_thread is not caller
                or record._state not in {"leased", "close_pending"}
                or self._records.get(record._issuance) is not record
            ):
                raise DurableGoogleLoginConfigurationError()
            if record._browser_cleanup_mode is None:
                record._browser_cleanup_delegate = delegate
                record._browser_cleanup_identity = delegate_identity
                record._browser_cleanup_capability = capability
                record._browser_cleanup_mode = "browser"
                self._condition.notify_all()
                return True
            if (
                record._browser_cleanup_mode == "browser"
                and record._browser_cleanup_delegate is delegate
                and record._browser_cleanup_identity
                is delegate_identity
                and record._browser_cleanup_capability is capability
            ):
                return True
        raise DurableGoogleLoginConfigurationError()

    def _claim_abandoned_browser_cleanup(
        self,
        lease,
        delegate,
        delegate_identity,
        capability,
        connection,
    ):
        self.require_current_process()
        if (
            type(lease) is not _DatabaseConnectionLease
            or delegate is None
            or delegate_identity is None
            or capability is None
            or (
                connection is not None
                and type(connection) is not sqlite3.Connection
            )
        ):
            raise DurableGoogleLoginConfigurationError()
        record = lease._record
        borrower = lease._borrower_thread
        if (
            record is None
            or borrower is None
            or borrower.is_alive()
        ):
            raise DurableGoogleLoginConfigurationError()
        caller = threading.current_thread()
        with self._condition:
            if (
                record._manager is not self
                or record._manager_identity is not self._identity
                or record._generation != lease._generation
            ):
                raise DurableGoogleLoginConfigurationError()
            current = self._records.get(record._issuance)
            if (
                current is None
                and record._state == "terminal"
                and not record._connection_offer
                and record._descriptor_owner.terminal
                and record._browser_cleanup_identity
                is delegate_identity
                and record._browser_cleanup_capability is capability
                and record._browser_cleanup_mode
                in {"terminal", "acknowledged"}
            ):
                return True
            if current is not record:
                raise DurableGoogleLoginConfigurationError()
            if (
                record._borrower_token is not lease._token
                or record._borrower_thread is not borrower
                or (
                    connection is not None
                    and record._connection_identity is not connection
                )
            ):
                raise DurableGoogleLoginConfigurationError()
            if not (
                record._browser_cleanup_mode == "browser"
                and record._browser_cleanup_delegate is delegate
                and record._browser_cleanup_identity
                is delegate_identity
                and record._browser_cleanup_capability is capability
            ):
                raise DurableGoogleLoginConfigurationError()
            if record._state == "rollback_pending":
                release_thread = record._release_thread
                if (
                    release_thread is not None
                    and release_thread is not caller
                    and release_thread.is_alive()
                ):
                    return False
                record._release_thread = None
                record._release_token = None
                record._state = "unresolved"
            if record._state == "closing":
                cleanup_thread = record._cleanup_thread
                if (
                    cleanup_thread is caller
                    and record._browser_cleanup_claim
                    is record._cleanup_token
                    and record._browser_cleanup_claim is not None
                ):
                    return record._browser_cleanup_claim
                if (
                    record._browser_cleanup_claim is not None
                    and record._browser_cleanup_claim
                    is record._cleanup_token
                ):
                    record._cleanup_thread = None
                    record._cleanup_token = None
                    record._browser_cleanup_claim = None
                    record._state = "unresolved"
                if (
                    record._state == "closing"
                    and cleanup_thread is not None
                    and cleanup_thread.is_alive()
                ):
                    return False
                if record._state == "closing":
                    record._cleanup_thread = None
                    record._cleanup_token = None
                    record._browser_cleanup_claim = None
                    record._state = "unresolved"
            if record._state not in {
                "leased",
                "close_pending",
                "unresolved",
                "ready",
            }:
                raise DurableGoogleLoginConfigurationError()
            claim = object()
            record._cleanup_thread = caller
            record._cleanup_token = claim
            record._browser_cleanup_claim = claim
            record._state = "closing"
            self._condition.notify_all()
            return claim

    def _abandon_abandoned_browser_cleanup(
        self,
        lease,
        delegate,
        delegate_identity,
        capability,
    ):
        self.require_current_process()
        if (
            type(lease) is not _DatabaseConnectionLease
            or delegate is None
            or delegate_identity is None
            or capability is None
        ):
            raise DurableGoogleLoginConfigurationError()
        record = lease._record
        if record is None:
            raise DurableGoogleLoginConfigurationError()
        caller = threading.current_thread()
        with self._condition:
            current = self._records.get(record._issuance)
            if (
                current is None
                and record._state == "terminal"
                and not record._connection_offer
                and record._descriptor_owner.terminal
                and record._browser_cleanup_identity
                is delegate_identity
                and record._browser_cleanup_capability is capability
                and record._browser_cleanup_mode
                in {"terminal", "acknowledged"}
            ):
                return True
            if (
                current is record
                and record._manager is self
                and record._manager_identity is self._identity
                and record._generation == lease._generation
                and record._browser_cleanup_mode == "manager"
                and record._browser_cleanup_delegate is None
                and record._browser_cleanup_identity
                is delegate_identity
                and record._browser_cleanup_capability is capability
                and record._browser_cleanup_borrower_token
                is lease._token
                and record._borrower_thread is None
                and record._borrower_token is None
                and record._state
                in {
                    "closing",
                    "close_pending",
                    "unresolved",
                    "ready",
                }
            ):
                return True
            if (
                current is not record
                or record._manager is not self
                or record._manager_identity is not self._identity
                or record._generation != lease._generation
                or record._borrower_token is not lease._token
                or record._browser_cleanup_mode != "browser"
                or record._browser_cleanup_delegate is not delegate
                or record._browser_cleanup_identity
                is not delegate_identity
                or record._browser_cleanup_capability is not capability
            ):
                raise DurableGoogleLoginConfigurationError()
            if (
                record._state == "closing"
                and record._cleanup_thread is caller
                and record._cleanup_token is not None
                and record._browser_cleanup_claim
                is record._cleanup_token
            ):
                cleanup_token = record._cleanup_token
                next_state = (
                    "close_pending"
                    if record._shutdown_requested or not self._accepting
                    else "unresolved"
                )
                try:
                    record._cleanup_thread = None
                finally:
                    if (
                        record._cleanup_token is cleanup_token
                        or record._browser_cleanup_claim is cleanup_token
                    ):
                        record._cleanup_thread = None
                        record._cleanup_token = None
                        record._browser_cleanup_claim = None
                        record._state = next_state
                self._condition.notify_all()
                return True
            if record._state in {
                "leased",
                "close_pending",
                "unresolved",
                "ready",
            }:
                return True
            if record._state == "closing":
                cleanup_thread = record._cleanup_thread
                if cleanup_thread is not None and cleanup_thread.is_alive():
                    return False
            raise DurableGoogleLoginConfigurationError()

    def _relinquish_unregistered_browser_cleanup(
        self,
        lease,
        delegate,
        delegate_identity,
        capability,
    ):
        self.require_current_process()
        if (
            type(lease) is not _DatabaseConnectionLease
            or delegate is None
            or delegate_identity is None
            or capability is None
        ):
            raise DurableGoogleLoginConfigurationError()
        record = lease._record
        borrower = lease._borrower_thread
        if (
            record is None
            or borrower is None
            or borrower.is_alive()
        ):
            raise DurableGoogleLoginConfigurationError()
        with self._condition:
            current = self._records.get(record._issuance)
            if (
                current is None
                and record._state == "terminal"
                and not record._connection_offer
                and record._descriptor_owner.terminal
                and record._manager is self
                and record._manager_identity is self._identity
                and record._generation == lease._generation
            ):
                return True
            if (
                current is not record
                or record._manager is not self
                or record._manager_identity is not self._identity
                or record._generation != lease._generation
            ):
                raise DurableGoogleLoginConfigurationError()
            if record._browser_cleanup_mode == "browser":
                if (
                    record._borrower_token is lease._token
                    and record._borrower_thread is borrower
                    and record._browser_cleanup_delegate is delegate
                    and record._browser_cleanup_identity
                    is delegate_identity
                    and record._browser_cleanup_capability is capability
                ):
                    return False
                raise DurableGoogleLoginConfigurationError()
            if record._browser_cleanup_mode is None:
                if (
                    record._borrower_token is not lease._token
                    or record._borrower_thread is not borrower
                ):
                    raise DurableGoogleLoginConfigurationError()
                record._browser_cleanup_delegate = None
                record._browser_cleanup_identity = delegate_identity
                record._browser_cleanup_capability = capability
                record._browser_cleanup_borrower_token = lease._token
                record._browser_cleanup_mode = "manager"
            elif record._browser_cleanup_mode == "manager":
                exact_partial_borrower = (
                    (
                        record._borrower_thread is borrower
                        and record._borrower_token is lease._token
                    )
                    or (
                        record._borrower_thread is None
                        and record._borrower_token is lease._token
                    )
                    or (
                        record._borrower_thread is None
                        and record._borrower_token is None
                    )
                )
                if (
                    record._browser_cleanup_delegate is not None
                    or record._browser_cleanup_identity
                    is not delegate_identity
                    or record._browser_cleanup_capability is not capability
                    or record._browser_cleanup_borrower_token
                    is not lease._token
                    or not exact_partial_borrower
                ):
                    raise DurableGoogleLoginConfigurationError()
            else:
                raise DurableGoogleLoginConfigurationError()

            if record._state == "rollback_pending":
                release_thread = record._release_thread
                if (
                    release_thread is not None
                    and release_thread.is_alive()
                ):
                    return False
                record._release_thread = None
                record._release_token = None
            elif record._state not in {
                "closing",
                "close_pending",
                "leased",
                "unresolved",
                "ready",
            }:
                raise DurableGoogleLoginConfigurationError()

            record._borrower_thread = None
            record._borrower_token = None
            if record._state in {
                "close_pending",
                "leased",
                "rollback_pending",
                "ready",
            }:
                record._state = (
                    "close_pending"
                    if record._shutdown_requested or not self._accepting
                    else "unresolved"
                )
            self._condition.notify_all()
            return True

    def _finish_abandoned_browser_cleanup(
        self,
        lease,
        delegate,
        delegate_identity,
        capability,
        claim,
    ):
        self.require_current_process()
        if (
            type(lease) is not _DatabaseConnectionLease
            or delegate is None
            or delegate_identity is None
            or capability is None
            or claim is None
            or claim is False
        ):
            raise DurableGoogleLoginConfigurationError()
        record = lease._record
        if record is None:
            raise DurableGoogleLoginConfigurationError()
        caller = threading.current_thread()
        with self._condition:
            current = self._records.get(record._issuance)
            if (
                current is None
                and record._state == "terminal"
                and not record._connection_offer
                and record._descriptor_owner.terminal
                and record._browser_cleanup_identity
                is delegate_identity
                and record._browser_cleanup_capability is capability
                and record._browser_cleanup_mode
                in {"terminal", "acknowledged"}
            ):
                return True
            if (
                current is not record
                or record._manager is not self
                or record._manager_identity is not self._identity
                or record._generation != lease._generation
                or record._borrower_token is not lease._token
                or record._browser_cleanup_mode != "browser"
                or record._browser_cleanup_delegate is not delegate
                or record._browser_cleanup_identity
                is not delegate_identity
                or record._browser_cleanup_capability is not capability
                or record._state != "closing"
                or record._cleanup_thread is not caller
                or record._cleanup_token is not claim
                or record._browser_cleanup_claim is not claim
            ):
                raise DurableGoogleLoginConfigurationError()
        return self._cleanup_claimed_record(record, claim)

    def _acknowledge_abandoned_browser_cleanup(
        self,
        lease,
        delegate,
        delegate_identity,
        capability,
    ):
        self.require_current_process()
        if (
            type(lease) is not _DatabaseConnectionLease
            or delegate is None
            or delegate_identity is None
            or capability is None
        ):
            raise DurableGoogleLoginConfigurationError()
        record = lease._record
        if record is None:
            raise DurableGoogleLoginConfigurationError()
        with self._condition:
            if (
                self._records.get(record._issuance) is not None
                or record._state != "terminal"
                or record._manager is not self
                or record._manager_identity is not self._identity
                or record._generation != lease._generation
                or record._browser_cleanup_identity
                is not delegate_identity
                or record._browser_cleanup_capability is not capability
                or record._browser_cleanup_mode
                not in {"terminal", "acknowledged"}
                or (
                    record._browser_cleanup_delegate is not None
                    and record._browser_cleanup_delegate is not delegate
                )
                or record._connection_offer
                or not record._descriptor_owner.terminal
            ):
                raise DurableGoogleLoginConfigurationError()
            record._browser_cleanup_delegate = None
            record._browser_cleanup_claim = None
            record._connection_identity = None
            record._browser_cleanup_mode = "acknowledged"
            self._condition.notify_all()
            return True

    def _release_connection_lease(self, lease, *, rollback):
        self.require_current_process()
        if (
            type(lease) is not _DatabaseConnectionLease
            or type(rollback) is not bool
        ):
            raise DurableGoogleLoginConfigurationError()
        record = lease._record
        caller = threading.current_thread()
        claim_token = object()
        failure = None
        terminal = False
        claimed = False
        try:
            with self._condition:
                if (
                    record is None
                    or record._manager is not self
                    or record._manager_identity is not self._identity
                    or record._generation != lease._generation
                    or record._borrower_token is not lease._token
                    or record._borrower_thread is not caller
                    or record._state
                    not in {
                        "leased",
                        "close_pending",
                        "rollback_pending",
                        "unresolved",
                    }
                    or (
                        record._state == "rollback_pending"
                        and record._release_thread is not caller
                        and record._release_thread is not None
                        and record._release_thread.is_alive()
                    )
                ):
                    if (
                        record is not None
                        and record._state == "terminal"
                        and self._records.get(record._issuance) is None
                    ):
                        return True
                    raise DurableGoogleLoginConfigurationError()
                if self._records.get(record._issuance) is not record:
                    raise DurableGoogleLoginConfigurationError()
                record._release_thread = caller
                record._release_token = claim_token
                record._state = "rollback_pending"
                claimed = True
            terminal = record.cleanup_owned_resources()
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ) as exc:
            failure = exc
            _sanitize_exception_graph(exc)
            exc = None
        except Exception as exc:
            failure = exc
            _sanitize_exception_graph(exc)
            exc = None
        finally:
            if record is not None:
                with self._condition:
                    if (
                        self._records.get(record._issuance) is record
                        and record._release_token is claim_token
                    ):
                        record._release_thread = None
                        record._release_token = None
                        if terminal:
                            record._borrower_thread = None
                            record._borrower_token = None
                            record._connection_identity = None
                            record._browser_cleanup_capability = None
                            record._browser_cleanup_borrower_token = None
                            record._browser_cleanup_claim = None
                            record._browser_cleanup_delegate = None
                            record._browser_cleanup_identity = None
                            record._browser_cleanup_mode = None
                            record._state = "terminal"
                            self._records.pop(record._issuance, None)
                        elif claimed:
                            record._state = "unresolved"
                    elif (
                        terminal
                        and record._state == "terminal"
                    ):
                        self._records.pop(record._issuance, None)
                    self._condition.notify_all()
        if failure is not None:
            propagated = failure
            failure = None
            raise propagated from None
        if not terminal:
            raise _DatabaseCleanupFailure()
        return True

    def _lease_disposed(self, lease):
        self.require_current_process()
        if type(lease) is not _DatabaseConnectionLease:
            raise DurableGoogleLoginConfigurationError()
        record = lease._record
        with self._condition:
            if (
                record is None
                or record._manager is not self
                or record._manager_identity is not self._identity
                or record._generation != lease._generation
            ):
                return False
            current = self._records.get(record._issuance)
            return (
                current is None
                and record._state == "terminal"
                and not record._connection_offer
                and record._descriptor_owner.terminal
            )

    def _recover_interrupted_release(self, lease):
        self.require_current_process()
        if type(lease) is not _DatabaseConnectionLease:
            raise DurableGoogleLoginConfigurationError()
        record = lease._record
        caller = threading.current_thread()
        with self._condition:
            if (
                record is not None
                and self._records.get(record._issuance) is record
                and record._manager_identity is self._identity
                and record._generation == lease._generation
                and record._borrower_token is lease._token
                and record._borrower_thread is caller
                and record._state == "rollback_pending"
                and record._release_thread is caller
            ):
                record._release_thread = None
                record._release_token = None
                record._state = "unresolved"
                self._condition.notify_all()
        return True

    def _cleanup_claimed_record(self, record, token):
        self.require_current_process()
        if (
            type(record) is not _DatabaseConnectionOwnership
            or token is None
        ):
            raise DurableGoogleLoginConfigurationError()
        caller = threading.current_thread()
        with self._condition:
            if (
                self._records.get(record._issuance) is not record
                or record._state != "closing"
                or record._cleanup_thread is not caller
                or record._cleanup_token is not token
            ):
                raise DurableGoogleLoginConfigurationError()
        terminal = False
        failure = None
        try:
            terminal = record.cleanup_owned_resources()
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ) as exc:
            failure = exc
            _sanitize_exception_graph(exc)
            exc = None
        except Exception as exc:
            failure = _DatabaseCleanupFailure()
            _sanitize_exception_graph(exc)
            exc = None
        finally:
            if not terminal:
                try:
                    terminal = (
                        not record._connection_offer
                        and record._descriptor_owner.terminal
                    )
                except (
                    KeyboardInterrupt,
                    SystemExit,
                    GeneratorExit,
                ) as exc:
                    if failure is None:
                        failure = exc
                    else:
                        _sanitize_exception_graph(exc)
                    exc = None
                except Exception as exc:
                    if failure is None:
                        failure = _DatabaseCleanupFailure()
                    _sanitize_exception_graph(exc)
                    exc = None
            with self._condition:
                if (
                    self._records.get(record._issuance) is record
                    and record._cleanup_token is token
                ):
                    record._cleanup_thread = None
                    record._cleanup_token = None
                    if terminal:
                        record._borrower_thread = None
                        record._borrower_token = None
                        record._release_thread = None
                        record._release_token = None
                        if (
                            record._browser_cleanup_claim is token
                            and record._browser_cleanup_delegate is not None
                        ):
                            record._browser_cleanup_mode = "terminal"
                        else:
                            record._connection_identity = None
                            record._browser_cleanup_capability = None
                            record._browser_cleanup_borrower_token = None
                            record._browser_cleanup_claim = None
                            record._browser_cleanup_delegate = None
                            record._browser_cleanup_identity = None
                            record._browser_cleanup_mode = None
                        record._state = "terminal"
                        self._records.pop(record._issuance, None)
                    else:
                        if record._browser_cleanup_claim is token:
                            record._browser_cleanup_claim = None
                        record._state = "unresolved"
                    self._condition.notify_all()
        if failure is not None:
            propagated = failure
            failure = None
            raise propagated from None
        return terminal

    def _abort_open_preserving_primary(self, record):
        try:
            with self._condition:
                if self._records.get(record._issuance) is not record:
                    return True
                record._opening_abandoned = True
                if record._state in {"leased", "close_pending"}:
                    record._borrower_thread = None
                    record._borrower_token = None
                record._opening_thread = None
                record._state = "closing"
                record._cleanup_thread = threading.current_thread()
                token = object()
                record._cleanup_token = token
            return self._cleanup_claimed_record(record, token)
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ) as exc:
            _sanitize_exception_graph(exc)
            exc = None
        except Exception as exc:
            _sanitize_exception_graph(exc)
            exc = None
        return False

    def _prune_terminal_locked(self):
        for issuance, record in tuple(self._records.items()):
            if record._state == "terminal":
                self._records.pop(issuance, None)

    def _release_connection(self, lease, *, rollback):
        return self._release_connection_lease(
            lease,
            rollback=rollback,
        )

    def __repr__(self):
        return "_RuntimeDatabaseConnections(<sealed>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("runtime_database_connections_not_serializable")

    def __copy__(self):
        raise TypeError("runtime_database_connections_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("runtime_database_connections_not_copyable")


class _PendingDurableGoogleLoginActivation:
    """Private constructed runtime awaiting the final pre-bind attestations."""

    __slots__ = (
        "_browser_integration",
        "_cleanup_coordinator",
        "_clock",
        "_condition",
        "_completion_policy",
        "_configuration",
        "_connections",
        "_activation_owner",
        "_gateway",
        "_key_authority",
        "_profile_integration",
        "_process_epoch",
        "_shutdown_requested",
        "_state",
    )

    def __init__(
        self,
        capability,
        *,
        configuration,
        connections,
        gateway,
        key_authority,
        completion_policy,
        profile_integration,
        browser_integration,
        clock,
        cleanup_coordinator,
    ):
        if (
            capability is not _PENDING_ACTIVATION_CAPABILITY
            or type(configuration)
            is not _DurableGoogleLoginConstructionConfiguration
            or type(connections) is not _RuntimeDatabaseConnections
            or type(cleanup_coordinator) is not _CleanupCoordinator
            or not callable(clock)
        ):
            raise DurableGoogleLoginConfigurationError()
        self._configuration = configuration
        self._connections = connections
        self._gateway = gateway
        self._key_authority = key_authority
        self._completion_policy = completion_policy
        self._profile_integration = profile_integration
        self._browser_integration = browser_integration
        self._clock = clock
        self._cleanup_coordinator = cleanup_coordinator
        self._condition = threading.Condition(threading.Lock())
        self._activation_owner = None
        self._process_epoch = connections._process_epoch
        self._shutdown_requested = False
        self._state = "pending"

    @property
    def configuration(self):
        _require_current_database_process(self._process_epoch)
        with self._condition:
            if self._state != "pending" or self._shutdown_requested:
                raise DurableGoogleLoginConfigurationError()
            return self._configuration.public_configuration

    @property
    def browser_integration(self):
        _require_current_database_process(self._process_epoch)
        with self._condition:
            if self._state != "pending" or self._shutdown_requested:
                raise DurableGoogleLoginConfigurationError()
            return self._browser_integration

    def require_database_lifetime_ownership(self):
        _require_current_database_process(self._process_epoch)
        with self._condition:
            if self._state != "pending" or self._shutdown_requested:
                raise DurableGoogleLoginConfigurationError()
            connections = self._connections
        return connections.require_database_lifetime_ownership()

    def _configure_callback_failure_telemetry(self, sink):
        _require_current_database_process(self._process_epoch)
        if not callable(sink):
            raise DurableGoogleLoginConfigurationError()
        with self._condition:
            if self._state != "pending" or self._shutdown_requested:
                raise DurableGoogleLoginConfigurationError()
            gateway = self._gateway
        try:
            from wahojobs.google_oidc_gateway import (
                _configure_callback_failure_telemetry,
            )

            _configure_callback_failure_telemetry(gateway, sink)
            return True
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except Exception:
            raise DurableGoogleLoginConfigurationError() from None
        finally:
            gateway = None
            sink = None

    def complete_activation(self):
        _require_current_database_process(self._process_epoch)
        activation_claimed = False
        configuration = None
        runtime = None
        target = None
        try:
            with self._condition:
                if self._state != "pending" or self._shutdown_requested:
                    raise DurableGoogleLoginConfigurationError()
                activation_claimed = True
                self._activation_owner = threading.current_thread()
                self._state = "offered"
                configuration = self._configuration
            target = configuration.database_target
            self._connections.require_database_lifetime_ownership()
            _reverify_secret_file_references(configuration)
            _attest_existing_database(
                target,
                cleanup_coordinator=self._cleanup_coordinator,
                lifetime_ownership=(
                    self._connections._attestation_lifetime_ownership()
                ),
            )
            self._connections.require_database_lifetime_ownership()
            _reverify_secret_file_references(configuration)
            _reverify_database_target(
                target,
                require_no_sidecars=True,
                require_stable_metadata=True,
            )
            with self._condition:
                if self._shutdown_requested:
                    raise DurableGoogleLoginConfigurationError()
            runtime = DurableGoogleLoginRuntime(
                configuration=configuration.public_configuration,
                connections=self._connections,
                gateway=self._gateway,
                key_authority=self._key_authority,
                completion_policy=self._completion_policy,
                profile_integration=self._profile_integration,
                browser_integration=self._browser_integration,
                clock=self._clock,
                cleanup_coordinator=self._cleanup_coordinator,
            )
            self._connections.require_database_lifetime_ownership()
            with self._condition:
                if (
                    self._shutdown_requested
                    or self._state != "offered"
                ):
                    raise DurableGoogleLoginConfigurationError()
                self._state = "accepted"
                self._condition.notify_all()
            self._connections.require_database_lifetime_ownership()
            with self._condition:
                if (
                    self._shutdown_requested
                    or self._state != "accepted"
                ):
                    raise DurableGoogleLoginConfigurationError()
                self._configuration = None
                self._connections = None
                self._gateway = None
                self._key_authority = None
                self._completion_policy = None
                self._profile_integration = None
                self._browser_integration = None
                self._clock = None
                self._state = "committed"
                self._activation_owner = None
                self._condition.notify_all()
            runtime.require_database_lifetime_ownership()
            return runtime
        except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
            propagated = exc
            if activation_claimed:
                self._fail_activation_preserving_primary()
            _sanitize_exception_graph(propagated)
            exc = None
            raise propagated from None
        except Exception as exc:
            _sanitize_exception_graph(exc)
            exc = None
            if activation_claimed:
                self._fail_activation_preserving_primary()
            raise DurableGoogleLoginConfigurationError() from None
        finally:
            activation_claimed = False
            configuration = None
            runtime = None
            target = None

    def close(self, *, _preserve_primary=False):
        _require_current_database_process(self._process_epoch)
        if type(_preserve_primary) is not bool:
            raise DurableGoogleLoginConfigurationError()
        deadline = time.monotonic() + _CLEANUP_WAIT_SECONDS
        caller = threading.current_thread()
        with self._condition:
            self._shutdown_requested = True
            while (
                self._state in {"offered", "accepted"}
                and self._activation_owner != caller
            ):
                activation_owner = self._activation_owner
                if (
                    activation_owner is None
                    or not activation_owner.is_alive()
                ):
                    self._state = "unresolved"
                    self._activation_owner = None
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._cleanup_coordinator.snapshot()
                self._condition.wait(remaining)
            if self._state in {"pending", "offered", "accepted"}:
                self._state = "cancelled"
                self._activation_owner = None
        report = None
        primary_control = None
        primary_error = None
        try:
            report = self._cleanup_coordinator.cleanup(
                preserve_primary=_preserve_primary,
            )
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ) as exc:
            primary_control = exc
            exc = None
        except Exception as exc:
            primary_error = exc
            exc = None
        finally:
            if report is None:
                try:
                    report = self._cleanup_coordinator.snapshot()
                except (
                    KeyboardInterrupt,
                    SystemExit,
                    GeneratorExit,
                ) as exc:
                    _sanitize_exception_graph(exc)
                    exc = None
                except Exception as exc:
                    _sanitize_exception_graph(exc)
                    exc = None
            for _attempt in range(3):
                try:
                    self._publish_activation_cleanup_state(
                        report,
                        preserve_committed=True,
                    )
                    break
                except (
                    KeyboardInterrupt,
                    SystemExit,
                    GeneratorExit,
                ) as exc:
                    _sanitize_exception_graph(exc)
                    exc = None
                except Exception as exc:
                    _sanitize_exception_graph(exc)
                    exc = None
        if primary_control is not None:
            propagated = primary_control
            primary_control = None
            raise propagated from None
        if primary_error is not None:
            propagated = primary_error
            primary_error = None
            raise propagated from None
        return report

    def _fail_activation(self):
        _require_current_database_process(self._process_epoch)
        report = None
        try:
            for _attempt in range(2):
                try:
                    report = self._cleanup_coordinator.cleanup(
                        preserve_primary=True,
                    )
                except (
                    KeyboardInterrupt,
                    SystemExit,
                    GeneratorExit,
                ) as exc:
                    _sanitize_exception_graph(exc)
                    exc = None
                    continue
                except Exception as exc:
                    _sanitize_exception_graph(exc)
                    exc = None
                    continue
                if report.cleanup_complete:
                    break
        finally:
            if report is None:
                try:
                    report = self._cleanup_coordinator.snapshot()
                except (
                    KeyboardInterrupt,
                    SystemExit,
                    GeneratorExit,
                ) as exc:
                    _sanitize_exception_graph(exc)
                    exc = None
                except Exception as exc:
                    _sanitize_exception_graph(exc)
                    exc = None
            for _attempt in range(3):
                try:
                    self._publish_activation_cleanup_state(
                        report,
                        preserve_committed=False,
                    )
                    break
                except (
                    KeyboardInterrupt,
                    SystemExit,
                    GeneratorExit,
                ) as exc:
                    _sanitize_exception_graph(exc)
                    exc = None
                except Exception as exc:
                    _sanitize_exception_graph(exc)
                    exc = None

    def _fail_activation_preserving_primary(self):
        _require_current_database_process(self._process_epoch)
        try:
            self._fail_activation()
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ) as exc:
            _sanitize_exception_graph(exc)
            exc = None
        except Exception as exc:
            _sanitize_exception_graph(exc)
            exc = None

    def _publish_activation_cleanup_state(
        self,
        report,
        *,
        preserve_committed,
    ):
        _require_current_database_process(self._process_epoch)
        with self._condition:
            if not preserve_committed or self._state != "committed":
                self._state = (
                    "cancelled"
                    if report is not None and report.cleanup_complete
                    else "unresolved"
                )
            self._activation_owner = None
            self._configuration = None
            self._connections = None
            self._gateway = None
            self._key_authority = None
            self._completion_policy = None
            self._profile_integration = None
            self._browser_integration = None
            self._clock = None
            self._condition.notify_all()
        return True

    def __repr__(self):
        _require_current_database_process(self._process_epoch)
        with self._condition:
            state = self._state
        return f"_PendingDurableGoogleLoginActivation(<{state}>)"

    __str__ = __repr__


class DurableGoogleLoginRuntime:
    """Own process-scoped authorities and request-scoped connection factories."""

    __slots__ = (
        "_configuration",
        "_cleanup_coordinator",
        "_connections",
        "_gateway",
        "_key_authority",
        "_completion_policy",
        "_profile_integration",
        "_browser_integration",
        "_clock",
        "_closed",
        "_lock",
        "_process_epoch",
        "_shutdown_requested",
    )

    def __init__(
        self,
        *,
        configuration,
        connections,
        gateway,
        key_authority,
        completion_policy,
        profile_integration,
        browser_integration,
        clock,
        cleanup_coordinator,
    ):
        if (
            type(configuration) is not DurableGoogleLoginConfiguration
            or type(connections) is not _RuntimeDatabaseConnections
            or type(cleanup_coordinator) is not _CleanupCoordinator
            or not callable(clock)
        ):
            raise DurableGoogleLoginConfigurationError()
        self._configuration = configuration
        self._cleanup_coordinator = cleanup_coordinator
        self._connections = connections
        self._gateway = gateway
        self._key_authority = key_authority
        self._completion_policy = completion_policy
        self._profile_integration = profile_integration
        self._browser_integration = browser_integration
        self._clock = clock
        self._closed = False
        self._lock = threading.Lock()
        self._process_epoch = connections._process_epoch
        self._shutdown_requested = False

    @property
    def configuration(self):
        _require_current_database_process(self._process_epoch)
        return self._configuration

    @property
    def browser_integration(self):
        _require_current_database_process(self._process_epoch)
        with self._lock:
            if (
                self._shutdown_requested
                or self._browser_integration is None
            ):
                raise DurableGoogleLoginConfigurationError()
            return self._browser_integration

    def open_writable_connection(self):
        _require_current_database_process(self._process_epoch)
        with self._lock:
            if self._shutdown_requested:
                raise DurableGoogleLoginConfigurationError()
            connections = self._connections
        return connections.open_writable_connection()

    def require_database_lifetime_ownership(self):
        _require_current_database_process(self._process_epoch)
        with self._lock:
            if self._shutdown_requested:
                raise DurableGoogleLoginConfigurationError()
            connections = self._connections
        return connections.require_database_lifetime_ownership()

    def read_only_connection_provider(self):
        _require_current_database_process(self._process_epoch)
        with self._lock:
            if self._shutdown_requested:
                raise DurableGoogleLoginConfigurationError()
            connections = self._connections
        return connections.guarded_read_only_connection_provider()

    def close(self, *, _preserve_primary=False):
        _require_current_database_process(self._process_epoch)
        if type(_preserve_primary) is not bool:
            raise DurableGoogleLoginConfigurationError()
        with self._lock:
            self._shutdown_requested = True
        report = None
        primary_control = None
        primary_error = None
        try:
            report = self._cleanup_coordinator.cleanup(
                preserve_primary=_preserve_primary,
            )
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ) as exc:
            primary_control = exc
            exc = None
        except Exception as exc:
            primary_error = exc
            exc = None
        finally:
            for _attempt in range(3):
                try:
                    self._synchronize_cleanup_state()
                    break
                except (
                    KeyboardInterrupt,
                    SystemExit,
                    GeneratorExit,
                ) as exc:
                    _sanitize_exception_graph(exc)
                    exc = None
                except Exception as exc:
                    _sanitize_exception_graph(exc)
                    exc = None
        if primary_control is not None:
            propagated = primary_control
            primary_control = None
            raise propagated from None
        if primary_error is not None:
            propagated = primary_error
            primary_error = None
            raise propagated from None
        return report

    def _synchronize_cleanup_state(self):
        _require_current_database_process(self._process_epoch)
        coordinator = self._cleanup_coordinator
        report = coordinator.snapshot()
        with self._lock:
            if coordinator.is_terminal("browser_integration"):
                self._browser_integration = None
                self._completion_policy = None
                self._clock = None
            if coordinator.is_terminal("profile_integration"):
                self._profile_integration = None
            if coordinator.is_terminal("database_connections"):
                self._connections = None
            if (
                coordinator.is_terminal("lookup_authority")
                and coordinator.is_terminal("protection_authority")
            ):
                self._key_authority = None
            if coordinator.is_terminal("google_gateway"):
                self._gateway = None
            self._closed = report.cleanup_complete

    def __repr__(self):
        _require_current_database_process(self._process_epoch)
        with self._lock:
            state = (
                "closed"
                if self._closed
                else (
                    "closing"
                    if self._shutdown_requested
                    else "configured"
                )
            )
        return f"DurableGoogleLoginRuntime(<{state}>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("durable_google_login_runtime_not_serializable")

    def __copy__(self):
        raise TypeError("durable_google_login_runtime_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("durable_google_login_runtime_not_copyable")


def load_durable_google_login_configuration(
    configuration_path,
) -> DurableGoogleLoginConfiguration:
    """Validate one strict document and publish only its serving projection."""

    outcome = None
    result = None
    try:
        with _ACTIVATION_PUBLICATION_GATE as gate:
            _require_no_unresolved_activation_handoffs()
            outcome = _ConfigurationWorkerOutcome(
                _WORKER_OUTCOME_CAPABILITY,
                "pending",
            )
            gate.protect_outcome(outcome)
            _run_configuration_worker(
                _load_public_configuration_worker,
                (configuration_path,),
                outcome,
            )
            result = _publish_configuration_worker_outcome(outcome)
            configuration_path = None
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        if outcome is not None:
            _close_worker_outcome_value_preserving_primary(outcome)
        result = None
        raise
    except Exception:
        if outcome is not None:
            _close_worker_outcome_value_preserving_primary(outcome)
        result = None
        raise
    while True:
        try:
            return result
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ) as exc:
            _sanitize_exception_graph(exc)
            exc = None
        except Exception as exc:
            _sanitize_exception_graph(exc)
            exc = None


def build_durable_google_login_runtime(
    configuration_path,
    *,
    _clock=None,
    _gateway_factory=None,
    _browser_integration_factory=None,
) -> DurableGoogleLoginRuntime:
    """Validate all configuration and compose the dedicated runtime atomically."""

    handoff_reservation = _new_activation_handoff_reservation()
    outcome = None
    result = None
    try:
        with _ACTIVATION_PUBLICATION_GATE as gate:
            _require_no_unresolved_activation_handoffs()
            _reserve_activation_handoff(handoff_reservation)
            outcome = _ConfigurationWorkerOutcome(
                _WORKER_OUTCOME_CAPABILITY,
                "pending",
                handoff_reservation=handoff_reservation,
            )
            gate.protect_outcome(outcome)
            _run_configuration_worker(
                _build_durable_google_login_runtime_worker,
                (
                    configuration_path,
                    _clock,
                    _gateway_factory,
                    _browser_integration_factory,
                ),
                outcome,
            )
            result = _publish_configuration_worker_outcome(outcome)
            configuration_path = None
            _clock = None
            _gateway_factory = None
            _browser_integration_factory = None
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        if outcome is not None:
            _close_worker_outcome_value_preserving_primary(outcome)
        _release_activation_handoff_reservation_preserving_primary(
            handoff_reservation
        )
        result = None
        raise
    except Exception:
        if outcome is not None:
            _close_worker_outcome_value_preserving_primary(outcome)
        _release_activation_handoff_reservation_preserving_primary(
            handoff_reservation
        )
        result = None
        raise
    while True:
        try:
            _release_activation_handoff_reservation_preserving_primary(
                handoff_reservation
            )
            handoff_reservation = None
            return result
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ) as exc:
            _sanitize_exception_graph(exc)
            exc = None
        except Exception as exc:
            _sanitize_exception_graph(exc)
            exc = None


def prepare_durable_google_login_activation(
    configuration_path,
    *,
    _clock=None,
    _gateway_factory=None,
    _browser_integration_factory=None,
    _pre_secret_preparer=None,
    _cleanup_coordinator=None,
    _checkpoint=None,
    _handoff_reservation=None,
    _callback_failure_telemetry_sink=None,
):
    """Construct private authorities without publishing the ready runtime."""

    if (
        _callback_failure_telemetry_sink is not None
        and not callable(_callback_failure_telemetry_sink)
    ):
        raise DurableGoogleLoginConfigurationError()
    owns_handoff_reservation = _handoff_reservation is None
    handoff_reservation = (
        _new_activation_handoff_reservation()
        if owns_handoff_reservation
        else _handoff_reservation
    )
    if (
        type(handoff_reservation)
        is not _ActivationHandoffReservation
    ):
        raise DurableGoogleLoginConfigurationError()
    outcome = None
    result = None
    try:
        with _ACTIVATION_PUBLICATION_GATE as gate:
            _require_no_unresolved_activation_handoffs()
            if owns_handoff_reservation:
                _reserve_activation_handoff(handoff_reservation)
            elif not _activation_handoff_reservation_is_reserved(
                handoff_reservation
            ):
                raise DurableGoogleLoginConfigurationError()
            outcome = _ConfigurationWorkerOutcome(
                _WORKER_OUTCOME_CAPABILITY,
                "pending",
                handoff_reservation=handoff_reservation,
            )
            gate.protect_outcome(outcome)
            _run_configuration_worker(
                _prepare_durable_google_login_activation_worker,
                (
                    configuration_path,
                    _clock,
                    _gateway_factory,
                    _browser_integration_factory,
                    _pre_secret_preparer,
                    _cleanup_coordinator,
                    _checkpoint,
                ),
                outcome,
            )
            result = _publish_configuration_worker_outcome(outcome)
            if _callback_failure_telemetry_sink is not None:
                result._configure_callback_failure_telemetry(
                    _callback_failure_telemetry_sink
                )
            configuration_path = None
            _clock = None
            _gateway_factory = None
            _browser_integration_factory = None
            _pre_secret_preparer = None
            _cleanup_coordinator = None
            _checkpoint = None
            _handoff_reservation = None
            _callback_failure_telemetry_sink = None
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        if outcome is not None:
            _close_worker_outcome_value_preserving_primary(outcome)
        if owns_handoff_reservation:
            _release_activation_handoff_reservation_preserving_primary(
                handoff_reservation
            )
        result = None
        raise
    except Exception:
        if outcome is not None:
            _close_worker_outcome_value_preserving_primary(outcome)
        if owns_handoff_reservation:
            _release_activation_handoff_reservation_preserving_primary(
                handoff_reservation
            )
        result = None
        raise
    while True:
        try:
            if owns_handoff_reservation:
                _release_activation_handoff_reservation_preserving_primary(
                    handoff_reservation
                )
            handoff_reservation = None
            _callback_failure_telemetry_sink = None
            return result
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ) as exc:
            _sanitize_exception_graph(exc)
            exc = None
        except Exception as exc:
            _sanitize_exception_graph(exc)
            exc = None


def _load_public_configuration_worker(configuration_path, worker_outcome):
    configuration = _load_construction_configuration(configuration_path)
    try:
        worker_outcome._publish(
            "ok",
            configuration.public_configuration,
        )
        return None
    finally:
        configuration = None
        configuration_path = None


def _build_durable_google_login_runtime_worker(
    configuration_path,
    clock_override,
    gateway_factory,
    browser_integration_factory,
    worker_outcome,
):
    handoff_reservation = worker_outcome._handoff_reservation()
    pending_outcome = _ConfigurationWorkerOutcome(
        _WORKER_OUTCOME_CAPABILITY,
        "pending",
        handoff_reservation=handoff_reservation,
    )
    pending = None
    runtime = None
    try:
        _prepare_durable_google_login_activation_worker(
            configuration_path,
            clock_override,
            gateway_factory,
            browser_integration_factory,
            None,
            None,
            None,
            pending_outcome,
        )
        pending = _worker_outcome_value(pending_outcome)
        runtime = pending.complete_activation()
        worker_outcome._publish("ok", runtime)
        pending_outcome._clear_value()
        runtime = None
        pending = None
        return None
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        _close_activation_handoff_preserving_primary(
            pending,
            runtime,
            handoff_reservation,
        )
        if runtime is None:
            _close_worker_outcome_value_preserving_primary(
                pending_outcome
            )
        else:
            pending_outcome._clear_value()
        raise
    except Exception:
        _close_activation_handoff_preserving_primary(
            pending,
            runtime,
            handoff_reservation,
        )
        if runtime is None:
            _close_worker_outcome_value_preserving_primary(
                pending_outcome
            )
        else:
            pending_outcome._clear_value()
        raise
    finally:
        pending = None
        runtime = None
        configuration_path = None
        clock_override = None
        gateway_factory = None
        browser_integration_factory = None
        handoff_reservation = None


def _close_activation_handoff_preserving_primary(
    pending,
    runtime,
    handoff_reservation=None,
):
    resource = runtime if runtime is not None else pending
    if resource is None:
        return True
    lease = None
    terminal = False
    try:
        lease = _retain_unresolved_activation_handoff(
            resource,
            handoff_reservation,
        )
        terminal = lease.close(_expected_resource=resource)
        if terminal:
            terminal = (
                _forget_unresolved_activation_handoff(lease)
                is True
            )
    except (
        KeyboardInterrupt,
        SystemExit,
        GeneratorExit,
    ) as exc:
        _sanitize_exception_graph(exc)
        exc = None
        terminal = False
    except Exception as exc:
        _sanitize_exception_graph(exc)
        exc = None
        terminal = False
    lease = None
    resource = None
    return terminal


def _new_activation_handoff_reservation():
    return _ActivationHandoffReservation(
        _HANDOFF_RESERVATION_CAPABILITY
    )


def _reserve_activation_handoff(reservation):
    if type(reservation) is not _ActivationHandoffReservation:
        raise DurableGoogleLoginConfigurationError()
    binding = reservation._binding()
    if binding is not None:
        lease = binding[0]
        if lease.reserve(reservation):
            return True
        raise DurableGoogleLoginConfigurationError()
    for lease in _EMERGENCY_HANDOFF_LEASES:
        if lease.reserve(reservation):
            return True
    raise DurableGoogleLoginConfigurationError()


def _activation_handoff_reservation_is_reserved(reservation):
    if type(reservation) is not _ActivationHandoffReservation:
        return False
    binding = reservation._binding()
    return (
        binding is not None
        and binding[0].reserved_by(reservation, binding)
    )


def _release_activation_handoff_reservation(reservation):
    if reservation is None:
        return True
    if type(reservation) is not _ActivationHandoffReservation:
        return False
    binding = reservation._binding()
    if binding is None:
        return True
    return binding[0].cancel_reserved(reservation, binding)


def _release_activation_handoff_reservation_preserving_primary(
    reservation,
):
    if reservation is None:
        return True
    if type(reservation) is not _ActivationHandoffReservation:
        return False
    capture_pending = object()
    binding = capture_pending
    while binding is capture_pending:
        try:
            binding = reservation._binding()
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ) as exc:
            _sanitize_exception_graph(exc)
            exc = None
        except Exception as exc:
            _sanitize_exception_graph(exc)
            exc = None
    if binding is None:
        return True
    while True:
        try:
            return binding[0].cancel_reserved(
                reservation,
                binding,
            )
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ) as exc:
            _sanitize_exception_graph(exc)
            exc = None
        except Exception as exc:
            _sanitize_exception_graph(exc)
            exc = None


def _dispose_unused_activation_handoff_reservation_exact_preserving_primary(
    reservation,
    expected_resource,
    expected_binding,
):
    if (
        type(reservation) is not _ActivationHandoffReservation
        or expected_resource is None
        or type(expected_binding) is not tuple
        or len(expected_binding) != 2
        or type(expected_binding[0])
        is not _ActivationHandoffCleanupLease
    ):
        return _HANDOFF_RESERVATION_CONFLICT
    lease = expected_binding[0]
    while True:
        try:
            disposition = lease._dispose_reserved(
                reservation,
                expected_resource,
                expected_binding,
            )
            if disposition in {
                _HANDOFF_RESERVATION_RELEASED,
                _HANDOFF_RESERVATION_CONFLICT,
            } or type(disposition) is _ActivationHandoffCleanupLease:
                return disposition
            return _HANDOFF_RESERVATION_CONFLICT
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ) as exc:
            _sanitize_exception_graph(exc)
            exc = None
        except Exception as exc:
            _sanitize_exception_graph(exc)
            exc = None


def _dispose_unused_activation_handoff_reservation_preserving_primary(
    reservation,
    expected_resource,
):
    if (
        type(reservation) is not _ActivationHandoffReservation
        or expected_resource is None
    ):
        raise DurableGoogleLoginConfigurationError()
    capture_pending = object()
    binding = capture_pending
    while binding is capture_pending:
        try:
            binding = reservation._binding()
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ) as exc:
            _sanitize_exception_graph(exc)
            exc = None
        except Exception as exc:
            _sanitize_exception_graph(exc)
            exc = None
    if binding is None:
        return _HANDOFF_RESERVATION_RELEASED
    while True:
        try:
            return (
                _dispose_unused_activation_handoff_reservation_exact_preserving_primary(
                    reservation,
                    expected_resource,
                    binding,
                )
            )
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ) as exc:
            _sanitize_exception_graph(exc)
            exc = None
        except Exception as exc:
            _sanitize_exception_graph(exc)
            exc = None


def _retain_existing_emergency_handoff_owner(
    resource,
    handoff_reservation,
    handoff_binding,
):
    if (
        (handoff_reservation is None) != (handoff_binding is None)
        or (
            handoff_reservation is not None
            and type(handoff_reservation)
            is not _ActivationHandoffReservation
        )
    ):
        raise DurableGoogleLoginConfigurationError()
    for emergency in _EMERGENCY_HANDOFF_LEASES:
        generation = emergency._retain_existing(resource)
        if generation is False:
            continue
        if handoff_reservation is None:
            if emergency._owns_generation(resource, generation):
                return emergency
            continue
        disposition = (
            _dispose_unused_activation_handoff_reservation_exact_preserving_primary(
                handoff_reservation,
                resource,
                handoff_binding,
            )
        )
        if disposition is _HANDOFF_RESERVATION_RELEASED:
            if emergency._owns_generation(resource, generation):
                return emergency
            continue
        if (
            type(disposition) is _ActivationHandoffCleanupLease
            and disposition.owns_reserved(
                handoff_reservation,
                resource,
                handoff_binding,
            )
        ):
            return disposition
        raise DurableGoogleLoginConfigurationError() from None
    return None


def _retain_unresolved_activation_handoff(
    resource,
    handoff_reservation=None,
):
    if resource is None:
        raise DurableGoogleLoginConfigurationError()
    if (
        handoff_reservation is not None
        and type(handoff_reservation)
        is not _ActivationHandoffReservation
    ):
        raise DurableGoogleLoginConfigurationError()
    handoff_binding = None
    reserved_lease = None
    if handoff_reservation is not None:
        handoff_binding = handoff_reservation._binding()
        if handoff_binding is None:
            raise DurableGoogleLoginConfigurationError()
        reserved_lease = handoff_binding[0]
        if reserved_lease.owns_reserved(
            handoff_reservation,
            resource,
            handoff_binding,
        ):
            offered = reserved_lease.offer_reserved(
                handoff_reservation,
                resource,
                handoff_binding,
            )
            if offered or reserved_lease.owns_reserved(
                handoff_reservation,
                resource,
                handoff_binding,
            ):
                return reserved_lease
            raise DurableGoogleLoginConfigurationError()
        if not reserved_lease.reserved_by(
            handoff_reservation,
            handoff_binding,
        ):
            raise DurableGoogleLoginConfigurationError()
    identifier = id(resource)
    lease = None
    publication_failures = 0
    while True:
        existing_emergency = (
            _retain_existing_emergency_handoff_owner(
                resource,
                handoff_reservation,
                handoff_binding,
            )
        )
        if existing_emergency is not None:
            lease = None
            return existing_emergency
        try:
            if lease is None:
                lease = _ActivationHandoffCleanupLease(resource)
            with _UNRESOLVED_HANDOFF_LOCK:
                existing = _UNRESOLVED_HANDOFFS.get(identifier)
                if (
                    existing is not None
                    and existing.owns(resource)
                ):
                    lease = None
                    if handoff_reservation is not None:
                        disposition = (
                            _dispose_unused_activation_handoff_reservation_exact_preserving_primary(
                                handoff_reservation,
                                resource,
                                handoff_binding,
                            )
                        )
                        if (
                            disposition
                            is not _HANDOFF_RESERVATION_RELEASED
                        ):
                            raise DurableGoogleLoginConfigurationError() from None
                    return existing
                _UNRESOLVED_HANDOFFS[identifier] = lease
                retained = lease
            lease = None
            if handoff_reservation is not None:
                disposition = (
                    _dispose_unused_activation_handoff_reservation_exact_preserving_primary(
                        handoff_reservation,
                        resource,
                        handoff_binding,
                    )
                )
                if disposition is not _HANDOFF_RESERVATION_RELEASED:
                    raise DurableGoogleLoginConfigurationError() from None
            return retained
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ) as exc:
            publication_failures += 1
            _sanitize_exception_graph(exc)
            exc = None
        except Exception as exc:
            publication_failures += 1
            _sanitize_exception_graph(exc)
            exc = None
        if publication_failures >= 4:
            existing_emergency = (
                _retain_existing_emergency_handoff_owner(
                    resource,
                    handoff_reservation,
                    handoff_binding,
                )
            )
            if existing_emergency is not None:
                lease = None
                return existing_emergency
            if reserved_lease is not None:
                while True:
                    try:
                        offered = reserved_lease.offer_reserved(
                            handoff_reservation,
                            resource,
                            handoff_binding,
                        )
                    except (
                        KeyboardInterrupt,
                        SystemExit,
                        GeneratorExit,
                    ) as exc:
                        _sanitize_exception_graph(exc)
                        exc = None
                        if reserved_lease.owns_reserved(
                            handoff_reservation,
                            resource,
                            handoff_binding,
                        ):
                            lease = None
                            return reserved_lease
                        continue
                    except Exception as exc:
                        _sanitize_exception_graph(exc)
                        exc = None
                        if reserved_lease.owns_reserved(
                            handoff_reservation,
                            resource,
                            handoff_binding,
                        ):
                            lease = None
                            return reserved_lease
                        continue
                    if offered or reserved_lease.owns_reserved(
                        handoff_reservation,
                        resource,
                        handoff_binding,
                    ):
                        lease = None
                        return reserved_lease
                    break
                lease = None
                raise DurableGoogleLoginConfigurationError() from None
            for emergency in _EMERGENCY_HANDOFF_LEASES:
                try:
                    if emergency.offer(resource):
                        lease = None
                        return emergency
                except (
                    KeyboardInterrupt,
                    SystemExit,
                    GeneratorExit,
                ) as exc:
                    _sanitize_exception_graph(exc)
                    exc = None
                    if emergency.owns(resource):
                        lease = None
                        return emergency
                except Exception as exc:
                    _sanitize_exception_graph(exc)
                    exc = None
                    if emergency.owns(resource):
                        lease = None
                        return emergency
            lease = None
            resource = None
            raise DurableGoogleLoginConfigurationError() from None


def _forget_unresolved_activation_handoff(lease):
    if type(lease) is not _ActivationHandoffCleanupLease:
        return False
    with _UNRESOLVED_HANDOFF_LOCK:
        for identifier, candidate in tuple(
            _UNRESOLVED_HANDOFFS.items()
        ):
            if candidate is lease and lease.terminal():
                _UNRESOLVED_HANDOFFS.pop(identifier, None)
                return True
    if any(
        candidate is lease
        for candidate in _EMERGENCY_HANDOFF_LEASES
    ):
        return lease.reset_terminal()
    return False


def _close_activation_resource_preserving_primary(
    resource,
    *,
    attempts=4,
):
    if type(attempts) is not int or attempts < 1:
        raise DurableGoogleLoginConfigurationError()
    close = getattr(resource, "close", None)
    if not callable(close):
        return True
    for _attempt in range(attempts):
        try:
            result = close(_preserve_primary=True)
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ) as exc:
            _sanitize_exception_graph(exc)
            exc = None
            continue
        except Exception as exc:
            _sanitize_exception_graph(exc)
            exc = None
            continue
        if (
            type(result) is _CleanupReport
            and result.cleanup_complete
        ) or (type(result) is not _CleanupReport and result is not False):
            return True
    return False


def _retry_unresolved_activation_handoffs():
    for emergency in _EMERGENCY_HANDOFF_LEASES:
        if emergency.terminal():
            try:
                emergency.reset_terminal()
            except (
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ) as exc:
                _sanitize_exception_graph(exc)
                exc = None
            except Exception as exc:
                _sanitize_exception_graph(exc)
                exc = None
    with _UNRESOLVED_HANDOFF_LOCK:
        leases = tuple(_UNRESOLVED_HANDOFFS.values())
    leases += tuple(
        lease
        for lease in _EMERGENCY_HANDOFF_LEASES
        if lease.active()
    )
    for lease in leases:
        if lease.close():
            _forget_unresolved_activation_handoff(lease)
        lease = None
    with _UNRESOLVED_HANDOFF_LOCK:
        ordinary_empty = not _UNRESOLVED_HANDOFFS
    return ordinary_empty and not any(
        lease.active() for lease in _EMERGENCY_HANDOFF_LEASES
    )


def _require_no_unresolved_activation_handoffs():
    if not _retry_unresolved_activation_handoffs():
        raise DurableGoogleLoginConfigurationError()
    return True


def _prepare_durable_google_login_activation_worker(
    configuration_path,
    clock_override,
    gateway_factory,
    browser_integration_factory,
    pre_secret_preparer,
    cleanup_coordinator,
    checkpoint,
    worker_outcome,
):
    if (
        (clock_override is not None and not callable(clock_override))
        or (gateway_factory is not None and not callable(gateway_factory))
        or (
            browser_integration_factory is not None
            and not callable(browser_integration_factory)
        )
        or (
            pre_secret_preparer is not None
            and not callable(pre_secret_preparer)
        )
        or (
            cleanup_coordinator is not None
            and type(cleanup_coordinator) is not _CleanupCoordinator
        )
        or (checkpoint is not None and not callable(checkpoint))
    ):
        raise DurableGoogleLoginConfigurationError()
    handoff_reservation = worker_outcome._handoff_reservation()
    coordinator = (
        _CleanupCoordinator()
        if cleanup_coordinator is None
        else cleanup_coordinator
    )
    clock = _utc_now if clock_override is None else clock_override
    configuration = _load_construction_configuration(configuration_path)
    _emit_runtime_checkpoint(checkpoint, "configuration_validated")
    gateway = None
    key_authority = None
    connections = None
    completion_policy = None
    profile_integration = None
    browser_integration = None
    target = None
    lifetime_resource = None
    lifetime_ownership = None
    secret_token = None
    invitation_lookup_key = None
    completed = False
    cleanup_report = None
    try:
        target = configuration.database_target
        lifetime_resource = _DatabaseLifetimeOwnershipResource(target)
        coordinator.own(
            "database_lifetime_ownership",
            lifetime_resource,
            _cleanup_database_lifetime_ownership_resource,
            probe=_database_lifetime_ownership_resource_is_closed,
            dependencies=_DATABASE_LIFETIME_TERMINAL_DEPENDENCIES,
            require_terminal_dependencies=True,
        )
        lifetime_ownership = acquire_database_lifetime_ownership(
            _database_target_path(target),
            role=ROLE_DURABLE_RUNTIME,
            _publisher=lifetime_resource.publish,
        )
        require_database_lifetime_ownership(
            lifetime_ownership,
            role=ROLE_DURABLE_RUNTIME,
            database_path=_database_target_path(target),
        )
        _reverify_database_target(
            target,
            require_no_sidecars=True,
            require_stable_metadata=True,
        )
        _emit_runtime_checkpoint(checkpoint, "database_lifetime_owned")
        require_database_lifetime_ownership(
            lifetime_ownership,
            role=ROLE_DURABLE_RUNTIME,
            database_path=_database_target_path(target),
        )
        _attest_existing_database(
            target,
            cleanup_coordinator=coordinator,
            lifetime_ownership=lifetime_ownership,
        )
        require_database_lifetime_ownership(
            lifetime_ownership,
            role=ROLE_DURABLE_RUNTIME,
            database_path=_database_target_path(target),
        )
        _reverify_database_target(
            target,
            require_stable_metadata=True,
        )
        _emit_runtime_checkpoint(checkpoint, "database_attested")
        require_database_lifetime_ownership(
            lifetime_ownership,
            role=ROLE_DURABLE_RUNTIME,
            database_path=_database_target_path(target),
        )
        if pre_secret_preparer is not None:
            pre_secret_preparer()
        require_database_lifetime_ownership(
            lifetime_ownership,
            role=ROLE_DURABLE_RUNTIME,
            database_path=_database_target_path(target),
        )
        (
            client_secret,
            invitation_lookup_key,
            lookup_keys,
            protection_keys,
        ) = _load_authority_material(configuration)
        require_database_lifetime_ownership(
            lifetime_ownership,
            role=ROLE_DURABLE_RUNTIME,
            database_path=_database_target_path(target),
        )
        _emit_runtime_checkpoint(checkpoint, "secrets_loaded")
        secret_token = coordinator.own(
            "secret_buffers",
            (
                client_secret,
                invitation_lookup_key,
                lookup_keys,
                protection_keys,
            ),
            _cleanup_secret_material,
        )
        try:
            require_database_lifetime_ownership(
                lifetime_ownership,
                role=ROLE_DURABLE_RUNTIME,
                database_path=_database_target_path(target),
            )
            if gateway_factory is None:
                from wahojobs.google_oidc_gateway import (
                    GoogleOidcGateway,
                    _configure_invitation_provisioning,
                )

                gateway = GoogleOidcGateway(
                    client_id=configuration.google_client_id,
                    client_secret=client_secret,
                    redirect_uri=configuration.google_redirect_uri,
                    environment_namespace=configuration.environment,
                )
                if invitation_lookup_key is not None:
                    _configure_invitation_provisioning(
                        gateway,
                        invitation_lookup_key,
                    )
            else:
                gateway_configuration = (
                    _GoogleGatewayConstructionConfiguration(
                        environment=configuration.environment,
                        google_redirect_uri=(
                            configuration.google_redirect_uri
                        ),
                        google_client_id=configuration.google_client_id,
                        invitation_lookup_key=invitation_lookup_key,
                    )
                )
                gateway = gateway_factory(
                    gateway_configuration,
                    client_secret,
                )
                gateway_configuration = None
            coordinator.own(
                "google_gateway",
                gateway,
                _close_cleanup_resource,
                dependencies=("browser_integration",),
            )
            from wahojobs.google_oidc_gateway import (
                _configure_account_native_bootstrap,
            )
            from wahojobs.ownership import ensure_account_native_principal

            _configure_account_native_bootstrap(
                gateway,
                ensure_account_native_principal,
            )
            _emit_runtime_checkpoint(checkpoint, "gateway_constructed")
            require_database_lifetime_ownership(
                lifetime_ownership,
                role=ROLE_DURABLE_RUNTIME,
                database_path=_database_target_path(target),
            )
            if client_secret:
                raise DurableGoogleLoginConfigurationError()
            if invitation_lookup_key:
                raise DurableGoogleLoginConfigurationError()
            client_secret = None
            invitation_lookup_key = None

            from wahojobs.google_oidc_transaction_protection import (
                GoogleOidcTransactionKeyAuthority,
            )

            key_authority = GoogleOidcTransactionKeyAuthority.from_mutable_keys(
                lookup_keys=lookup_keys,
                protection_keys=protection_keys,
                active_lookup_version=(
                    configuration.oidc_lookup_active_version
                ),
                active_protection_version=(
                    configuration.oidc_protection_active_version
                ),
            )
            coordinator.own(
                "protection_authority",
                key_authority,
                _close_transaction_protection_authority,
                probe=_transaction_protection_authority_is_closed,
                dependencies=("browser_integration",),
            )
            coordinator.own(
                "lookup_authority",
                key_authority,
                _close_transaction_lookup_authority,
                probe=_transaction_lookup_authority_is_closed,
                dependencies=("browser_integration",),
            )
            _emit_runtime_checkpoint(
                checkpoint,
                "key_authority_constructed",
            )
            if any(
                buffer
                for buffer in (
                    *lookup_keys.values(),
                    *protection_keys.values(),
                )
            ):
                raise DurableGoogleLoginConfigurationError()
            lookup_keys = None
            protection_keys = None
        finally:
            _clear_buffer(client_secret)
            _clear_buffer(invitation_lookup_key)
            _clear_key_buffers(lookup_keys)
            _clear_key_buffers(protection_keys)
            if secret_token is not None:
                coordinator.resolve(secret_token)
                secret_token = None

        from wahojobs.browser_session_authentication import (
            DurableBrowserSessionAuthenticationGateway,
        )
        from wahojobs.authenticated_profile_matches import (
            AuthenticatedProfileMatchesBrowserIntegration,
            AuthenticatedProfileMatchesService,
        )
        from wahojobs.matching.metadata_overlay import (
            DEFAULT_OVERLAY_PATH,
            load_overlay,
        )
        from wahojobs.persistent_profile_read_authorization import (
            DurablePersistentProfileReadAuthorizationGateway,
        )
        from .persistent_profiles_application import (
            PersistentProfileApplicationService,
        )
        from .persistent_profile_creation import (
            ConfirmedProfileArtifactVault,
            DurablePersistentProfileCreateAuthorizationGateway,
            PersistentProfileCreationService,
        )
        from .persistent_profile_corrections import (
            ConfirmedProfileCorrectionArtifactVault,
            PersistentProfileCorrectionService,
        )
        from .persistent_profiles_browser import (
            PersistentProfileBrowserIntegration,
        )
        from wahojobs.browser_session_lifecycle import (
            create_request_scoped_session_secret_vault,
            discard_request_scoped_session_secret_vault,
        )
        from wahojobs.trusted_login_completion import (
            create_trusted_login_completion_policy,
            prepare_session_delivery,
        )

        completion_policy = create_trusted_login_completion_policy(
            environment_namespace=configuration.environment,
            idle_ttl=configuration.session_idle_ttl,
            absolute_ttl=configuration.session_absolute_ttl,
        )
        authentication_gateway = DurableBrowserSessionAuthenticationGateway(
            trusted_environment_namespace=configuration.environment,
            clock=clock,
        )
        authorization_gateway = (
            DurablePersistentProfileReadAuthorizationGateway()
        )
        creation_authorization_gateway = (
            DurablePersistentProfileCreateAuthorizationGateway(
                authorization_gateway
            )
        )

        connections = _RuntimeDatabaseConnections(
            target,
            lifetime_ownership=lifetime_ownership,
        )
        coordinator.own(
            "database_connections",
            connections,
            _close_cleanup_resource,
            probe=_cleanup_resource_is_closed,
            dependencies=("browser_integration", "profile_integration"),
        )
        _emit_runtime_checkpoint(checkpoint, "connections_constructed")
        profile_service = PersistentProfileApplicationService(
            durable_authentication_gateway=authentication_gateway,
            durable_authorization_gateway=authorization_gateway,
            connection_provider=(
                connections.read_only_connection_provider
            ),
        )
        profile_artifact_vault = ConfirmedProfileArtifactVault(
            monotonic=time.monotonic,
            token_factory=lambda: secrets.token_urlsafe(32),
        )
        profile_creation_service = PersistentProfileCreationService(
            authentication_gateway=authentication_gateway,
            authorization_gateway=creation_authorization_gateway,
            read_connection_provider=(
                connections.read_only_connection_provider
            ),
            write_connection_provider=(
                connections.writable_connection_provider
            ),
            vault=profile_artifact_vault,
            clock=clock,
            token_factory=lambda: secrets.token_urlsafe(32),
        )
        profile_correction_service = PersistentProfileCorrectionService(
            authentication_gateway=authentication_gateway,
            authorization_gateway=authorization_gateway,
            read_connection_provider=(
                connections.read_only_connection_provider
            ),
            write_connection_provider=(
                connections.writable_connection_provider
            ),
            vault=ConfirmedProfileCorrectionArtifactVault(
                monotonic=time.monotonic,
            ),
            clock=clock,
            token_factory=lambda: secrets.token_urlsafe(32),
            binding_secret=secrets.token_bytes(32),
        )
        profile_integration = PersistentProfileBrowserIntegration(
            profile_service,
            creation_service=profile_creation_service,
            correction_service=profile_correction_service,
            public_origin=configuration.public_configuration.public_origin,
        )
        coordinator.own(
            "profile_integration",
            profile_integration,
            _close_cleanup_resource,
            probe=_cleanup_resource_is_closed,
            dependencies=("browser_integration",),
        )
        matches_service = AuthenticatedProfileMatchesService(
            authentication_gateway=authentication_gateway,
            authorization_gateway=authorization_gateway,
            connection_provider=connections.read_only_connection_provider,
            clock=clock,
            binding_secret=secrets.token_bytes(32),
        )
        matches_integration = AuthenticatedProfileMatchesBrowserIntegration(
            matches_service,
            connection_provider=connections.read_only_connection_provider,
            metadata_overlay=load_overlay(
                path=DEFAULT_OVERLAY_PATH,
                required=False,
            ),
            confirmed_profile_artifact_sink=(
                profile_integration.issue_confirmed_artifact
            ),
            completed_profile_confirmation_authenticator=(
                profile_integration.authenticate_completed_profile_replay
            ),
            public_origin=configuration.public_configuration.public_origin,
            now=clock,
        )
        if profile_integration.attach_matches_integration(matches_integration) is not True:
            raise DurableGoogleLoginConfigurationError()
        if profile_integration.activate() is not True:
            raise DurableGoogleLoginConfigurationError()
        _emit_runtime_checkpoint(
            checkpoint,
            "profile_integration_activated",
        )

        use_default_browser_integration = browser_integration_factory is None
        if use_default_browser_integration:
            from wahojobs.durable_google_login_browser import (
                DurableGoogleLoginBrowserIntegration,
            )

            browser_integration_factory = (
                DurableGoogleLoginBrowserIntegration
            )

        def validate_logout(
            connection,
            *,
            session_token,
            csrf_credential,
            now,
        ):
            from wahojobs.accounts import (
                SessionUnavailable,
                validate_session_csrf,
            )

            try:
                validate_session_csrf(
                    connection,
                    session_token=session_token,
                    csrf_secret=csrf_credential,
                    now=now,
                )
            except (SessionUnavailable, sqlite3.Error, ValueError, TypeError):
                return False
            return True

        def revoke_logout(
            connection,
            *,
            session_token,
            csrf_credential,
            now,
        ):
            from wahojobs.accounts import (
                SessionUnavailable,
                StaleSessionVersion,
                revoke_current_session,
                validate_session_csrf,
            )

            try:
                session = validate_session_csrf(
                    connection,
                    session_token=session_token,
                    csrf_secret=csrf_credential,
                    now=now,
                )
                revoke_current_session(
                    connection,
                    session_token=session_token,
                    expected_session_version=session.session_version,
                    reason="user_logout",
                    now=now,
                )
            except (
                SessionUnavailable,
                StaleSessionVersion,
                sqlite3.Error,
                ValueError,
                TypeError,
            ):
                return False
            return True

        browser_arguments = dict(
            public_origin=configuration.public_configuration.public_origin,
            connection_factory=connections.open_writable_connection,
            connection_borrower=_borrow_internal_database_connection,
            gateway=gateway,
            key_authority=key_authority,
            completion_policy=completion_policy,
            request_secret_vault_factory=(
                create_request_scoped_session_secret_vault
            ),
            prepare_session_delivery=prepare_session_delivery,
            discard_request_secret_vault=(
                discard_request_scoped_session_secret_vault
            ),
            validate_logout=validate_logout,
            revoke_logout=revoke_logout,
            profile_integration=profile_integration,
            now=clock,
        )
        if use_default_browser_integration:
            browser_arguments["process_guard"] = (
                connections.require_current_process
            )
        browser_integration = browser_integration_factory(**browser_arguments)
        browser_arguments = None
        coordinator.own(
            "browser_integration",
            browser_integration,
            _close_cleanup_resource,
            dependencies=(
                "route_integration",
                "request_threads",
            ),
        )
        _emit_runtime_checkpoint(checkpoint, "browser_constructed")
        require_database_lifetime_ownership(
            lifetime_ownership,
            role=ROLE_DURABLE_RUNTIME,
            database_path=_database_target_path(target),
        )
        pending = _PendingDurableGoogleLoginActivation(
            _PENDING_ACTIVATION_CAPABILITY,
            configuration=configuration,
            connections=connections,
            gateway=gateway,
            key_authority=key_authority,
            completion_policy=completion_policy,
            profile_integration=profile_integration,
            browser_integration=browser_integration,
            clock=clock,
            cleanup_coordinator=coordinator,
        )
        worker_outcome._publish("ok", pending)
        completed = True
        pending = None
        return None
    finally:
        configuration_path = None
        clock_override = None
        gateway_factory = None
        browser_integration_factory = None
        pre_secret_preparer = None
        cleanup_coordinator = None
        checkpoint = None
        target = None
        lifetime_resource = None
        lifetime_ownership = None
        if not completed:
            for _attempt in range(2):
                cleanup_report = coordinator.cleanup(
                    preserve_primary=True
                )
                if cleanup_report.cleanup_complete:
                    break
            if (
                cleanup_report is not None
                and not cleanup_report.cleanup_complete
            ):
                try:
                    _retain_unresolved_activation_handoff(
                        coordinator,
                        handoff_reservation,
                    )
                except (
                    KeyboardInterrupt,
                    SystemExit,
                    GeneratorExit,
                ) as exc:
                    _sanitize_exception_graph(exc)
                    exc = None
                except Exception as exc:
                    _sanitize_exception_graph(exc)
                    exc = None
            configuration = None
            gateway = None
            key_authority = None
            connections = None
            completion_policy = None
            profile_integration = None
            browser_integration = None
        cleanup_report = None
        handoff_reservation = None


def _load_construction_configuration(configuration_path):
    configuration_reference = None
    raw = None
    document = None
    pure = None
    try:
        configuration_reference = _validated_file_reference(
            configuration_path,
            configuration=True,
        )
        raw = _read_validated_file(
            configuration_reference,
            minimum=1,
            maximum=CONFIGURATION_MAX_BYTES,
        )
        document = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
        pure = _validated_configuration(document)
        return _resolve_configuration_files(
            pure,
            configuration_reference=configuration_reference,
        )
    finally:
        _clear_buffer(raw)
        _clear_json_containers(document)
        configuration_path = None
        configuration_reference = None
        raw = None
        document = None
        pure = None


def _validated_configuration(document):
    if type(document) is not dict:
        raise DurableGoogleLoginConfigurationError()
    fields = tuple(document.keys())
    field_set = frozenset(fields)
    if (
        any(type(field) is not str for field in fields)
        or field_set
        not in {
            _CONFIGURATION_FIELDS,
            _CONFIGURATION_FIELDS | _OPTIONAL_CONFIGURATION_FIELDS,
        }
        or len(fields) != len(field_set)
    ):
        raise DurableGoogleLoginConfigurationError()
    if (
        type(document["version"]) is not int
        or document["version"] != CONFIGURATION_VERSION
    ):
        raise DurableGoogleLoginConfigurationError()

    environment = document["environment"]
    if type(environment) is not str or environment not in SUPPORTED_ENVIRONMENTS:
        raise DurableGoogleLoginConfigurationError()

    bind_host = document["bind_host"]
    bind_port = document["bind_port"]
    if (
        type(bind_host) is not str
        or bind_host != SUPPORTED_BIND_HOST
        or type(bind_port) is not int
        or not 1 <= bind_port <= 65_535
    ):
        raise DurableGoogleLoginConfigurationError()

    public_origin, public_authority = _canonical_public_origin(
        document["public_origin"],
        expected_port=bind_port,
    )
    google_redirect_uri = document["google_redirect_uri"]
    if (
        type(google_redirect_uri) is not str
        or google_redirect_uri != public_origin + CALLBACK_PATH
    ):
        raise DurableGoogleLoginConfigurationError()

    client_id = document["google_client_id"]
    if type(client_id) is not str or _CLIENT_ID.fullmatch(client_id) is None:
        raise DurableGoogleLoginConfigurationError()

    database_path_text = _validated_path_text(document["database_path"])
    client_secret_path_text = _validated_path_text(
        document["google_client_secret_file"]
    )
    invitation_lookup_key_path_text = (
        None
        if "account_invitation_lookup_key_file" not in document
        else _validated_path_text(
            document["account_invitation_lookup_key_file"]
        )
    )
    lookup_keys = _validated_key_path_specifications(
        document["oidc_lookup_keys"]
    )
    protection_keys = _validated_key_path_specifications(
        document["oidc_protection_keys"]
    )
    lookup_active = _validated_active_version(
        document["oidc_lookup_active_version"],
        lookup_keys,
    )
    protection_active = _validated_active_version(
        document["oidc_protection_active_version"],
        protection_keys,
    )

    (
        minimum_idle_seconds,
        maximum_idle_seconds,
        minimum_absolute_seconds,
        maximum_absolute_seconds,
    ) = _authoritative_ttl_bounds()
    idle_seconds = _validated_ttl_seconds(
        document["session_idle_ttl_seconds"],
        minimum=minimum_idle_seconds,
        maximum=maximum_idle_seconds,
    )
    absolute_seconds = _validated_ttl_seconds(
        document["session_absolute_ttl_seconds"],
        minimum=minimum_absolute_seconds,
        maximum=maximum_absolute_seconds,
    )
    if idle_seconds > absolute_seconds:
        raise DurableGoogleLoginConfigurationError()

    allowed = document["allowed_post_login_paths"]
    if (
        type(allowed) is not list
        or len(allowed) != 1
        or type(allowed[0]) is not str
        or allowed[0] != POST_LOGIN_PATH
    ):
        raise DurableGoogleLoginConfigurationError()

    path_texts = (
        database_path_text,
        client_secret_path_text,
        *(
            ()
            if invitation_lookup_key_path_text is None
            else (invitation_lookup_key_path_text,)
        ),
        *(item.path_text for item in lookup_keys),
        *(item.path_text for item in protection_keys),
    )
    if len(frozenset(path_texts)) != len(path_texts):
        raise DurableGoogleLoginConfigurationError()

    return _PureConfiguration(
        version=CONFIGURATION_VERSION,
        environment=environment,
        database_path_text=database_path_text,
        bind_host=bind_host,
        bind_port=bind_port,
        public_origin=public_origin,
        public_authority=public_authority,
        google_redirect_uri=google_redirect_uri,
        google_client_id=client_id,
        google_client_secret_path_text=client_secret_path_text,
        account_invitation_lookup_key_path_text=(
            invitation_lookup_key_path_text
        ),
        oidc_lookup_keys=lookup_keys,
        oidc_lookup_active_version=lookup_active,
        oidc_protection_keys=protection_keys,
        oidc_protection_active_version=protection_active,
        session_idle_ttl=timedelta(seconds=idle_seconds),
        session_absolute_ttl=timedelta(seconds=absolute_seconds),
        allowed_post_login_paths=(POST_LOGIN_PATH,),
    )


def _resolve_configuration_files(pure, *, configuration_reference):
    if (
        type(pure) is not _PureConfiguration
        or type(configuration_reference) is not _ValidatedFileReference
    ):
        raise DurableGoogleLoginConfigurationError()
    database_reference = _validated_file_reference(
        pure.database_path_text,
        database=True,
    )
    database_target = _database_target_from_reference(database_reference)
    client_secret_file = _validated_file_reference(
        pure.google_client_secret_path_text,
        secret=True,
    )
    invitation_lookup_key_file = (
        None
        if pure.account_invitation_lookup_key_path_text is None
        else _validated_file_reference(
            pure.account_invitation_lookup_key_path_text,
            secret=True,
        )
    )
    lookup_keys = tuple(
        _OidcKeyFileReference(
            version=item.version,
            file_reference=_validated_file_reference(
                item.path_text,
                secret=True,
            ),
        )
        for item in pure.oidc_lookup_keys
    )
    protection_keys = tuple(
        _OidcKeyFileReference(
            version=item.version,
            file_reference=_validated_file_reference(
                item.path_text,
                secret=True,
            ),
        )
        for item in pure.oidc_protection_keys
    )
    _require_distinct_file_identities(
        (
            configuration_reference,
            database_reference,
            client_secret_file,
            *(
                ()
                if invitation_lookup_key_file is None
                else (invitation_lookup_key_file,)
            ),
            *(item.file_reference for item in lookup_keys),
            *(item.file_reference for item in protection_keys),
        )
    )
    return _DurableGoogleLoginConstructionConfiguration(
        environment=pure.environment,
        database_target=database_target,
        public_configuration=DurableGoogleLoginConfiguration(
            bind_host=pure.bind_host,
            bind_port=pure.bind_port,
            public_origin=pure.public_origin,
        ),
        public_authority=pure.public_authority,
        google_redirect_uri=pure.google_redirect_uri,
        google_client_id=pure.google_client_id,
        google_client_secret_file=client_secret_file,
        account_invitation_lookup_key_file=invitation_lookup_key_file,
        oidc_lookup_keys=lookup_keys,
        oidc_lookup_active_version=pure.oidc_lookup_active_version,
        oidc_protection_keys=protection_keys,
        oidc_protection_active_version=(
            pure.oidc_protection_active_version
        ),
        session_idle_ttl=pure.session_idle_ttl,
        session_absolute_ttl=pure.session_absolute_ttl,
        allowed_post_login_paths=pure.allowed_post_login_paths,
    )


def _canonical_public_origin(value, *, expected_port):
    if type(value) is not str or not 1 <= len(value) <= 2_048:
        raise DurableGoogleLoginConfigurationError()
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise DurableGoogleLoginConfigurationError()
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.hostname is None
        or _HOST.fullmatch(parsed.hostname) is None
        or port != expected_port
    ):
        raise DurableGoogleLoginConfigurationError()
    expected = f"https://{parsed.hostname}:{port}"
    if value != expected:
        raise DurableGoogleLoginConfigurationError()
    return expected, f"{parsed.hostname}:{port}"


def _validated_key_path_specifications(value):
    if (
        type(value) is not list
        or not 1 <= len(value) <= MAX_KEY_VERSIONS
    ):
        raise DurableGoogleLoginConfigurationError()
    references = []
    seen = set()
    for item in value:
        if type(item) is not dict or set(item) != _KEY_REFERENCE_FIELDS:
            raise DurableGoogleLoginConfigurationError()
        version = item["version"]
        if (
            type(version) is not int
            or type(version) is bool
            or not 1 <= version <= 2_147_483_647
            or version in seen
        ):
            raise DurableGoogleLoginConfigurationError()
        seen.add(version)
        references.append(
            _OidcKeyPathSpecification(
                version=version,
                path_text=_validated_path_text(item["file"]),
            )
        )
    if tuple(item.version for item in references) != tuple(sorted(seen)):
        raise DurableGoogleLoginConfigurationError()
    return tuple(references)


def _validated_active_version(value, references):
    if (
        type(value) is not int
        or type(value) is bool
        or value not in {item.version for item in references}
    ):
        raise DurableGoogleLoginConfigurationError()
    return value


def _authoritative_ttl_bounds():
    from wahojobs.browser_session_lifecycle import (
        MAX_ABSOLUTE_TTL,
        MAX_IDLE_TTL,
        MIN_ABSOLUTE_TTL,
        MIN_IDLE_TTL,
    )

    values = tuple(
        int(value.total_seconds())
        for value in (
            MIN_IDLE_TTL,
            MAX_IDLE_TTL,
            MIN_ABSOLUTE_TTL,
            MAX_ABSOLUTE_TTL,
        )
    )
    if values != (60, 2_592_000, 60, 7_776_000):
        raise DurableGoogleLoginConfigurationError()
    return values


def _validated_ttl_seconds(value, *, minimum, maximum):
    if (
        type(value) is not int
        or type(minimum) is not int
        or type(maximum) is not int
        or not minimum <= value <= maximum
    ):
        raise DurableGoogleLoginConfigurationError()
    return value


def _validated_path_text(value, *, allow_path=False):
    if type(value) is str:
        text = value
    elif allow_path and type(value) is _PATH_TYPE:
        text = os.fspath(value)
    else:
        raise DurableGoogleLoginConfigurationError()
    if (
        not text
        or "\x00" in text
        or len(text.encode("utf-8", errors="strict")) > MAX_PATH_BYTES
        or (
            os.name == "nt"
            and ":" in os.path.splitdrive(text)[1]
        )
    ):
        raise DurableGoogleLoginConfigurationError()
    path = Path(text)
    if (
        not path.is_absolute()
        or text != str(path)
        or any(part in {".", ".."} for part in path.parts)
    ):
        raise DurableGoogleLoginConfigurationError()
    return text


def _absolute_regular_file(value, *, secret=False):
    reference = _validated_file_reference(value, secret=secret)
    return _file_reference_path(reference)


def _validated_file_reference(
    value,
    *,
    secret=False,
    database=False,
    configuration=False,
):
    if (
        type(secret) is not bool
        or type(database) is not bool
        or type(configuration) is not bool
        or sum((secret, database, configuration)) > 1
    ):
        raise DurableGoogleLoginConfigurationError()
    text = _validated_path_text(value, allow_path=True)
    path = Path(text)
    _require_no_reparse_components(path)
    resolved = path.resolve(strict=True)
    if type(resolved) is not _PATH_TYPE or str(resolved) != text:
        raise DurableGoogleLoginConfigurationError()
    _require_no_reparse_components(resolved)
    if (secret or database) and _inside_repository_checkout(resolved):
        raise DurableGoogleLoginConfigurationError()
    metadata = os.lstat(resolved)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or ((secret or database or configuration) and metadata.st_nlink != 1)
        or (
            secret
            and os.name != "nt"
            and metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        )
    ):
        raise DurableGoogleLoginConfigurationError()
    return _ValidatedFileReference(
        _FILE_REFERENCE_CAPABILITY,
        path=resolved,
        identity=_identity_from_metadata(metadata),
        secret=secret,
    )


def _inside_repository_checkout(path):
    if type(path) is not _PATH_TYPE or not path.is_absolute():
        raise DurableGoogleLoginConfigurationError()
    if _inside_known_checkout_location(path):
        return True
    return any(
        (parent / ".git").exists()
        for parent in (path.parent, *path.parents)
    )


def _inside_known_checkout_location(path):
    folded_parts = tuple(part.casefold() for part in path.parts)
    for index, part in enumerate(folded_parts[:-1]):
        if part == ".codex" and folded_parts[index + 1] == "worktrees":
            return True
    implementation_root = Path(__file__).parent.parent
    if _path_is_within(path, implementation_root):
        return True
    if len(Path(__file__).parents) > 5:
        ordinary_root = (
            Path(__file__).parents[5] / implementation_root.name
        )
        if _path_is_within(path, ordinary_root):
            return True
    return False


def _path_is_within(path, root):
    path_text = os.path.normcase(os.fspath(path))
    root_text = os.path.normcase(os.fspath(root))
    try:
        return os.path.commonpath((path_text, root_text)) == root_text
    except ValueError:
        return False


def _require_no_reparse_components(path):
    current = path
    while True:
        metadata = os.lstat(current)
        attributes = getattr(metadata, "st_file_attributes", 0)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or attributes & _WINDOWS_REPARSE_ATTRIBUTE
        ):
            raise DurableGoogleLoginConfigurationError()
        parent = current.parent
        if parent == current:
            break
        current = parent


def _identity_from_metadata(metadata):
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


def _stable_file_state_matches(expected, actual):
    return (
        type(expected) is _FileIdentity
        and type(actual) is _FileIdentity
        and (
            expected.device,
            expected.inode,
            expected.size,
            expected.modified_ns,
            stat.S_IFMT(expected.mode),
            expected.links,
        )
        == (
            actual.device,
            actual.inode,
            actual.size,
            actual.modified_ns,
            stat.S_IFMT(actual.mode),
            actual.links,
        )
        and (
            os.name == "nt"
            or expected.changed_ns == actual.changed_ns
        )
    )


def _file_reference_path(reference):
    if type(reference) is not _ValidatedFileReference:
        raise DurableGoogleLoginConfigurationError()
    return object.__getattribute__(
        reference,
        "_ValidatedFileReference__path",
    )


def _file_reference_identity(reference):
    if type(reference) is not _ValidatedFileReference:
        raise DurableGoogleLoginConfigurationError()
    return object.__getattribute__(
        reference,
        "_ValidatedFileReference__identity",
    )


def _file_reference_is_secret(reference):
    if type(reference) is not _ValidatedFileReference:
        raise DurableGoogleLoginConfigurationError()
    return object.__getattribute__(
        reference,
        "_ValidatedFileReference__secret",
    )


def _require_distinct_file_identities(paths):
    identities = set()
    for reference in paths:
        identity = _file_reference_identity(reference)
        key = (identity.device, identity.inode)
        if key in identities:
            raise DurableGoogleLoginConfigurationError()
        identities.add(key)


def _load_authority_material(configuration):
    client_secret = None
    invitation_lookup_key = None
    lookup = {}
    protection = {}
    try:
        client_secret = _read_mutable_file(
            configuration.google_client_secret_file,
            minimum=CLIENT_SECRET_MIN_BYTES,
            maximum=CLIENT_SECRET_MAX_BYTES,
        )
        if any(byte < 0x21 or byte > 0x7E for byte in client_secret):
            raise DurableGoogleLoginConfigurationError()
        if configuration.account_invitation_lookup_key_file is not None:
            invitation_lookup_key = _read_mutable_file(
                configuration.account_invitation_lookup_key_file,
                minimum=INVITATION_LOOKUP_KEY_MIN_BYTES,
                maximum=INVITATION_LOOKUP_KEY_MAX_BYTES,
            )
        for item in configuration.oidc_lookup_keys:
            key_buffer = None
            try:
                key_buffer = _read_mutable_file(
                    item.file_reference,
                    minimum=TRANSACTION_KEY_BYTES,
                    maximum=TRANSACTION_KEY_BYTES,
                )
                lookup[item.version] = key_buffer
                key_buffer = None
            finally:
                _clear_buffer(key_buffer)
        for item in configuration.oidc_protection_keys:
            key_buffer = None
            try:
                key_buffer = _read_mutable_file(
                    item.file_reference,
                    minimum=TRANSACTION_KEY_BYTES,
                    maximum=TRANSACTION_KEY_BYTES,
                )
                protection[item.version] = key_buffer
                key_buffer = None
            finally:
                _clear_buffer(key_buffer)
        key_buffers = (
            *(
                ()
                if invitation_lookup_key is None
                else (invitation_lookup_key,)
            ),
            *lookup.values(),
            *protection.values(),
        )
        if any(
            hmac.compare_digest(left, right)
            for index, left in enumerate(key_buffers)
            for right in key_buffers[index + 1 :]
        ):
            raise DurableGoogleLoginConfigurationError()
        key_buffers = None
        return client_secret, invitation_lookup_key, lookup, protection
    except BaseException:
        _clear_buffer(client_secret)
        _clear_buffer(invitation_lookup_key)
        _clear_key_buffers(lookup)
        _clear_key_buffers(protection)
        raise


def _reverify_secret_file_references(configuration):
    references = (
        configuration.google_client_secret_file,
        *(
            ()
            if configuration.account_invitation_lookup_key_file is None
            else (configuration.account_invitation_lookup_key_file,)
        ),
        *(item.file_reference for item in configuration.oidc_lookup_keys),
        *(
            item.file_reference
            for item in configuration.oidc_protection_keys
        ),
    )
    for reference in references:
        _reverify_file_reference(reference, require_stable_metadata=True)


def _reverify_file_reference(
    reference,
    *,
    require_stable_metadata,
):
    if (
        type(reference) is not _ValidatedFileReference
        or type(require_stable_metadata) is not bool
    ):
        raise DurableGoogleLoginConfigurationError()
    path = _file_reference_path(reference)
    expected = _file_reference_identity(reference)
    secret = _file_reference_is_secret(reference)
    _require_no_reparse_components(path)
    if secret and _inside_repository_checkout(path):
        raise DurableGoogleLoginConfigurationError()
    metadata = os.lstat(path)
    actual = _identity_from_metadata(metadata)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or (secret and metadata.st_nlink != 1)
        or (
            secret
            and os.name != "nt"
            and metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        )
        or (actual.device, actual.inode) != (expected.device, expected.inode)
        or (
            require_stable_metadata
            and not _stable_file_state_matches(expected, actual)
        )
    ):
        raise DurableGoogleLoginConfigurationError()
    return metadata


def _read_mutable_file(reference, *, minimum, maximum):
    if type(reference) is not _ValidatedFileReference:
        reference = _validated_file_reference(reference, secret=True)
    return _read_validated_file(
        reference,
        minimum=minimum,
        maximum=maximum,
    )


def _read_validated_file(reference, *, minimum, maximum):
    if (
        type(reference) is not _ValidatedFileReference
        or type(minimum) is not int
        or type(maximum) is not int
        or not 0 <= minimum <= maximum
    ):
        raise DurableGoogleLoginConfigurationError()
    expected = _file_reference_identity(reference)
    if not minimum <= expected.size <= maximum:
        raise DurableGoogleLoginConfigurationError()
    _reverify_file_reference(reference, require_stable_metadata=True)
    path = _file_reference_path(reference)
    buffer = bytearray(expected.size)
    descriptor = None
    handle = None
    view = None
    chunk = None
    completed = False
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        handle = os.fdopen(descriptor, "rb", buffering=0)
        descriptor = None
        metadata_open = _identity_from_metadata(os.fstat(handle.fileno()))
        if (
            not _stable_file_state_matches(expected, metadata_open)
            or not stat.S_ISREG(metadata_open.mode)
        ):
            raise DurableGoogleLoginConfigurationError()
        _reverify_file_reference(reference, require_stable_metadata=True)
        view = memoryview(buffer)
        offset = 0
        while offset < len(buffer):
            chunk = view[offset:]
            try:
                count = handle.readinto(chunk)
            finally:
                chunk.release()
                chunk = None
            if type(count) is not int or count <= 0:
                raise DurableGoogleLoginConfigurationError()
            offset += count
        if handle.read(1) != b"":
            raise DurableGoogleLoginConfigurationError()
        metadata_after = _identity_from_metadata(
            os.fstat(handle.fileno())
        )
        if not _stable_file_state_matches(expected, metadata_after):
            raise DurableGoogleLoginConfigurationError()
        _reverify_file_reference(reference, require_stable_metadata=True)
        view.release()
        view = None
        handle.close()
        handle = None
        _reverify_file_reference(reference, require_stable_metadata=True)
        completed = True
        return buffer
    finally:
        if chunk is not None:
            chunk.release()
        if view is not None:
            view.release()
        try:
            if handle is not None:
                handle.close()
            if descriptor is not None:
                os.close(descriptor)
        finally:
            if not completed:
                _clear_buffer(buffer)


def _database_target_from_reference(reference):
    if (
        type(reference) is not _ValidatedFileReference
        or _file_reference_is_secret(reference)
    ):
        raise DurableGoogleLoginConfigurationError()
    path = _file_reference_path(reference)
    identity = _file_reference_identity(reference)
    if identity.links != 1:
        raise DurableGoogleLoginConfigurationError()
    target = _DatabaseTargetAuthority(
        _DATABASE_TARGET_CAPABILITY,
        path=path,
        identity=identity,
    )
    _reverify_database_target(
        target,
        require_no_sidecars=True,
        require_stable_metadata=True,
    )
    _require_rollback_journal_database_header(target)
    return target


def _database_target_authority(value):
    return _database_target_from_reference(
        _validated_file_reference(value, database=True)
    )


def _database_target_path(target):
    if type(target) is not _DatabaseTargetAuthority:
        raise DurableGoogleLoginConfigurationError()
    return object.__getattribute__(
        target,
        "_DatabaseTargetAuthority__path",
    )


def _database_target_identity(target):
    if type(target) is not _DatabaseTargetAuthority:
        raise DurableGoogleLoginConfigurationError()
    return object.__getattribute__(
        target,
        "_DatabaseTargetAuthority__identity",
    )


def _coerce_database_target(value):
    if type(value) is _DatabaseTargetAuthority:
        return value
    if type(value) in {str, _PATH_TYPE}:
        return _database_target_authority(value)
    raise DurableGoogleLoginConfigurationError()


def _reverify_database_target(
    target,
    *,
    require_no_sidecars=False,
    require_stable_metadata=False,
):
    if (
        type(target) is not _DatabaseTargetAuthority
        or type(require_no_sidecars) is not bool
        or type(require_stable_metadata) is not bool
    ):
        raise DurableGoogleLoginConfigurationError()
    path = _database_target_path(target)
    expected = _database_target_identity(target)
    _require_no_reparse_components(path)
    if _inside_repository_checkout(path):
        raise DurableGoogleLoginConfigurationError()
    metadata = os.lstat(path)
    actual = _identity_from_metadata(metadata)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or (actual.device, actual.inode) != (expected.device, expected.inode)
        or (
            require_stable_metadata
            and not _stable_file_state_matches(expected, actual)
        )
        or (
            require_no_sidecars
            and _database_sidecar_names(path)
        )
    ):
        raise DurableGoogleLoginConfigurationError()
    return metadata


def _database_sidecar_names(path):
    database_name = path.name
    direct_names = {
        os.path.normcase(database_name + "-journal"),
        os.path.normcase(database_name + "-wal"),
        os.path.normcase(database_name + "-shm"),
    }
    master_journal_prefix = os.path.normcase(database_name + "-mj")
    super_journal_prefix = os.path.normcase(
        database_name + "-super-journal"
    )
    found = []
    with os.scandir(path.parent) as entries:
        for entry in entries:
            name = entry.name
            comparable_name = os.path.normcase(name)
            if (
                comparable_name in direct_names
                or comparable_name.startswith(master_journal_prefix)
                or comparable_name.startswith(super_journal_prefix)
            ):
                found.append(name)
    return tuple(sorted(found))


def _require_rollback_journal_database_header(target):
    descriptor_owner = _open_pinned_database_target(
        target,
        require_stable_metadata=True,
    )
    try:
        _verify_pinned_database_target(
            descriptor_owner,
            target,
            require_stable_metadata=True,
        )
    finally:
        descriptor_owner.close()


def _open_pinned_database_target(
    target,
    *,
    require_stable_metadata=False,
    process_epoch=None,
    manager_identity=None,
    generation=1,
):
    if (
        type(target) is not _DatabaseTargetAuthority
        or type(require_stable_metadata) is not bool
        or type(generation) is not int
        or generation < 1
    ):
        raise DurableGoogleLoginConfigurationError()
    if process_epoch is None:
        process_epoch = _current_database_process_epoch()
    if manager_identity is None:
        manager_identity = object()
    _require_current_database_process(process_epoch)
    _reverify_database_target(
        target,
        require_no_sidecars=True,
        require_stable_metadata=require_stable_metadata,
    )
    owner = _DatabaseDescriptorOwnership(
        process_epoch=process_epoch,
        manager_identity=manager_identity,
        generation=generation,
    )
    try:
        owner.open(
            target,
            require_stable_metadata=require_stable_metadata,
        )
        return owner
    except BaseException:
        try:
            owner.close()
        except BaseException as cleanup:
            _sanitize_exception_graph(cleanup)
            cleanup = None
        raise


def _verify_pinned_database_target(
    descriptor_owner,
    target,
    *,
    require_stable_metadata=False,
):
    if (
        type(descriptor_owner) is not _DatabaseDescriptorOwnership
        or type(target) is not _DatabaseTargetAuthority
        or type(require_stable_metadata) is not bool
    ):
        raise DurableGoogleLoginConfigurationError()
    descriptor = descriptor_owner.descriptor_for_validation()
    expected = _database_target_identity(target)
    opened = _identity_from_metadata(os.fstat(descriptor))
    if (
        not stat.S_ISREG(opened.mode)
        or opened.links != 1
        or (opened.device, opened.inode)
        != (expected.device, expected.inode)
        or (
            require_stable_metadata
            and not _stable_file_state_matches(expected, opened)
        )
    ):
        raise DurableGoogleLoginConfigurationError()
    _reverify_database_target(
        target,
        require_no_sidecars=True,
        require_stable_metadata=require_stable_metadata,
    )


def _database_connection_is_closed(connection):
    try:
        explicit = getattr(connection, "closed")
    except AttributeError:
        explicit = None
    if type(explicit) is bool:
        return explicit
    try:
        connection.in_transaction
    except sqlite3.ProgrammingError:
        return True
    return False


def _cleanup_database_connection_independently(
    connection,
    *,
    rollback,
):
    if type(rollback) is not bool:
        raise DurableGoogleLoginConfigurationError()
    if connection is None:
        return True, False, None
    failed = False
    first_control = None
    should_rollback = rollback
    if rollback:
        try:
            should_rollback = connection.in_transaction is True
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ) as exc:
            first_control = exc
            _sanitize_exception_graph(exc)
            exc = None
            failed = True
            should_rollback = True
        except Exception as exc:
            _sanitize_exception_graph(exc)
            exc = None
            failed = True
            should_rollback = True
    if should_rollback:
        try:
            connection.rollback()
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ) as exc:
            if first_control is None:
                first_control = exc
            _sanitize_exception_graph(exc)
            exc = None
            failed = True
        except Exception as exc:
            _sanitize_exception_graph(exc)
            exc = None
            failed = True

    close_succeeded = False
    try:
        connection.close()
        close_succeeded = True
    except (
        KeyboardInterrupt,
        SystemExit,
        GeneratorExit,
    ) as exc:
        if first_control is None:
            first_control = exc
        _sanitize_exception_graph(exc)
        exc = None
        failed = True
    except Exception as exc:
        _sanitize_exception_graph(exc)
        exc = None
        failed = True

    terminal = close_succeeded
    if not terminal:
        try:
            terminal = _database_connection_is_closed(connection)
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ) as exc:
            if first_control is None:
                first_control = exc
            _sanitize_exception_graph(exc)
            exc = None
            failed = True
        except Exception as exc:
            _sanitize_exception_graph(exc)
            exc = None
            failed = True
    return terminal, failed, first_control


def _close_descriptor_independently(descriptor):
    if descriptor is None:
        return True, False, None
    if type(descriptor) is not _DatabaseDescriptorOwnership:
        raise DurableGoogleLoginConfigurationError()
    try:
        terminal = descriptor.close()
        return terminal, not terminal, None
    except (
        KeyboardInterrupt,
        SystemExit,
        GeneratorExit,
    ) as exc:
        _sanitize_exception_graph(exc)
        return False, True, exc
    except Exception as exc:
        _sanitize_exception_graph(exc)
        exc = None
        return False, True, None


def _cleanup_database_connection_resource(connection):
    terminal, failed, control = (
        _cleanup_database_connection_independently(
            connection,
            rollback=True,
        )
    )
    if control is not None:
        propagated = control
        control = None
        raise propagated from None
    if failed:
        raise _DatabaseCleanupFailure()
    return terminal


def _cleanup_descriptor_resource(descriptor):
    terminal, failed, control = _close_descriptor_independently(descriptor)
    if control is not None:
        propagated = control
        control = None
        raise propagated from None
    if failed:
        raise _DatabaseCleanupFailure()
    return terminal


def _database_descriptor_is_closed(descriptor):
    if type(descriptor) is not _DatabaseDescriptorOwnership:
        raise DurableGoogleLoginConfigurationError()
    return descriptor.terminal


def _attest_existing_database(
    target,
    *,
    cleanup_coordinator,
    lifetime_ownership=None,
):
    from wahojobs.google_oidc_authorization_transaction_schema import (
        attest_google_oidc_authorization_transaction_schema,
    )

    if (
        type(target) is not _DatabaseTargetAuthority
        or type(cleanup_coordinator) is not _CleanupCoordinator
        or (
            lifetime_ownership is not None
            and type(lifetime_ownership)
            is not DatabaseLifetimeOwnership
        )
    ):
        raise DurableGoogleLoginConfigurationError()
    if lifetime_ownership is not None:
        try:
            require_database_lifetime_ownership(
                lifetime_ownership,
                role=ROLE_DURABLE_RUNTIME,
                database_path=_database_target_path(target),
            )
        except DatabaseLifetimeOwnershipError:
            raise DurableGoogleLoginConfigurationError() from None
    _reverify_database_target(
        target,
        require_no_sidecars=True,
        require_stable_metadata=True,
    )
    lease, connection_token = _open_database_connection(
        target,
        mode="rw",
        verify_schema=False,
        install_guard=lifetime_ownership is not None,
        _cleanup_coordinator=cleanup_coordinator,
        _lifetime_ownership=lifetime_ownership,
    )
    connection = _borrow_internal_database_connection(lease)
    try:
        if lifetime_ownership is not None:
            try:
                require_database_lifetime_ownership(
                    lifetime_ownership,
                    role=ROLE_DURABLE_RUNTIME,
                    database_path=_database_target_path(target),
                )
            except DatabaseLifetimeOwnershipError:
                raise DurableGoogleLoginConfigurationError() from None
        connection.execute("BEGIN IMMEDIATE")
        connection.rollback()
        if lifetime_ownership is not None:
            try:
                require_database_lifetime_ownership(
                    lifetime_ownership,
                    role=ROLE_DURABLE_RUNTIME,
                    database_path=_database_target_path(target),
                )
            except DatabaseLifetimeOwnershipError:
                raise DurableGoogleLoginConfigurationError() from None
        _reverify_database_target(
            target,
            require_no_sidecars=True,
            require_stable_metadata=True,
        )
        if connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
            raise DurableGoogleLoginConfigurationError()
        connection.execute("PRAGMA query_only = ON")
        if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
            raise DurableGoogleLoginConfigurationError()
        _attest_closed_database_schema(connection)
        attestation = attest_google_oidc_authorization_transaction_schema(
            connection
        )
        if (
            type(attestation) is not dict
            or attestation.get("state") != "correctly_installed"
            or attestation.get("blocking") is not False
            or attestation.get("migration_marker_present") is not True
            or connection.in_transaction
        ):
            raise DurableGoogleLoginConfigurationError()
        integrity = connection.execute("PRAGMA quick_check(1)").fetchone()
        if tuple(integrity) != ("ok",):
            raise DurableGoogleLoginConfigurationError()
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise DurableGoogleLoginConfigurationError()
        _attest_closed_database_schema(connection)
    except BaseException:
        try:
            lease.close()
        except BaseException as cleanup:
            _sanitize_exception_graph(cleanup)
            cleanup = None
        try:
            if lease.closed:
                cleanup_coordinator.resolve(connection_token)
        except BaseException as cleanup:
            _sanitize_exception_graph(cleanup)
            cleanup = None
        connection = None
        lease = None
        connection_token = None
        raise
    else:
        control = None
        failed = False
        try:
            lease.close()
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ) as exc:
            control = exc
            _sanitize_exception_graph(exc)
            exc = None
            failed = True
        except Exception as exc:
            _sanitize_exception_graph(exc)
            exc = None
            failed = True
        terminal = False
        try:
            terminal = lease.closed
        except BaseException as cleanup:
            _sanitize_exception_graph(cleanup)
            cleanup = None
        if terminal:
            cleanup_coordinator.resolve(connection_token)
        connection_token = None
        if control is not None:
            propagated = control
            control = None
            raise propagated from None
        if failed or not terminal:
            raise DurableGoogleLoginConfigurationError()
    _reverify_database_target(
        target,
        require_no_sidecars=True,
        require_stable_metadata=True,
    )
    if lifetime_ownership is not None:
        try:
            require_database_lifetime_ownership(
                lifetime_ownership,
                role=ROLE_DURABLE_RUNTIME,
                database_path=_database_target_path(target),
            )
        except DatabaseLifetimeOwnershipError:
            raise DurableGoogleLoginConfigurationError() from None


def _open_writable_connection(target):
    target = _coerce_database_target(target)
    return _open_database_connection(target, mode="rw")


def _open_database_connection(
    target,
    *,
    mode,
    verify_schema=True,
    install_guard=True,
    _cleanup_coordinator=None,
    _lifetime_ownership=None,
):
    if (
        type(target) is not _DatabaseTargetAuthority
        or mode not in {"ro", "rw"}
        or type(verify_schema) is not bool
        or type(install_guard) is not bool
        or (
            _lifetime_ownership is not None
            and type(_lifetime_ownership)
            is not DatabaseLifetimeOwnership
        )
        or (
            _cleanup_coordinator is not None
            and type(_cleanup_coordinator) is not _CleanupCoordinator
        )
    ):
        raise DurableGoogleLoginConfigurationError()
    manager = _RuntimeDatabaseConnections(
        target,
        lifetime_ownership=_lifetime_ownership,
    )
    lease = None
    connection_token = None
    try:
        if _cleanup_coordinator is not None:
            connection_token = _cleanup_coordinator.own(
                "database_attestation_connection",
                manager,
                _cleanup_database_manager_resource,
                probe=_database_manager_is_closed,
            )
        lease = manager._finish_connection_open(
            mode=mode,
            verify_schema=verify_schema,
            install_guard=install_guard,
            standalone=True,
        )
    except BaseException:
        if lease is not None:
            try:
                lease.close()
            except BaseException as cleanup:
                _sanitize_exception_graph(cleanup)
                cleanup = None
        else:
            try:
                manager.close()
            except BaseException as cleanup:
                _sanitize_exception_graph(cleanup)
                cleanup = None
        raise
    if _cleanup_coordinator is not None:
        return lease, connection_token
    return lease


def _cleanup_database_lease_resource(lease):
    if type(lease) is not _DatabaseConnectionLease:
        raise DurableGoogleLoginConfigurationError()
    return lease.close()


def _borrow_internal_database_connection(lease):
    if type(lease) is not _DatabaseConnectionLease:
        raise DurableGoogleLoginConfigurationError()
    return lease._borrow_internal_connection(
        _DATABASE_INTERNAL_BORROW_CAPABILITY
    )


def _cleanup_database_manager_resource(manager):
    if type(manager) is not _RuntimeDatabaseConnections:
        raise DurableGoogleLoginConfigurationError()
    return manager.close()


def _cleanup_database_lifetime_ownership_resource(resource):
    if type(resource) is not _DatabaseLifetimeOwnershipResource:
        raise DurableGoogleLoginConfigurationError()
    return resource.close()


def _database_lifetime_ownership_resource_is_closed(resource):
    return (
        type(resource) is _DatabaseLifetimeOwnershipResource
        and resource.closed
    )


def _database_manager_is_closed(manager):
    if type(manager) is not _RuntimeDatabaseConnections:
        raise DurableGoogleLoginConfigurationError()
    return manager.closed


def _database_lease_is_closed(lease):
    if type(lease) is not _DatabaseConnectionLease:
        raise DurableGoogleLoginConfigurationError()
    return lease.closed


@contextmanager
def _read_only_connection_scope(target):
    target = _coerce_database_target(target)
    lease = None
    connection = None
    try:
        lease = _open_database_connection(target, mode="ro")
        connection = _borrow_internal_database_connection(lease)
        yield connection
    except BaseException:
        if lease is not None:
            for _attempt in range(3):
                try:
                    if lease.close():
                        break
                except BaseException as cleanup:
                    _sanitize_exception_graph(cleanup)
                    cleanup = None
        raise
    else:
        first_failure = None
        terminal = False
        for _attempt in range(3):
            try:
                terminal = lease.close()
                if terminal:
                    break
            except BaseException as cleanup:
                if first_failure is None:
                    first_failure = cleanup
                _sanitize_exception_graph(cleanup)
                cleanup = None
        if first_failure is not None:
            propagated = first_failure
            first_failure = None
            raise propagated from None
        if not terminal:
            raise _DatabaseCleanupFailure()
    finally:
        connection = None
        lease = None


@contextmanager
def _managed_read_only_connection_scope(manager):
    if type(manager) is not _RuntimeDatabaseConnections:
        raise DurableGoogleLoginConfigurationError()
    lease = None
    connection = None
    try:
        lease = manager._open_read_only_connection()
        connection = _borrow_internal_database_connection(lease)
        yield connection
    except BaseException:
        if lease is not None:
            for _attempt in range(3):
                try:
                    if lease.close():
                        break
                except BaseException as cleanup:
                    _sanitize_exception_graph(cleanup)
                    cleanup = None
        raise
    else:
        first_failure = None
        terminal = False
        for _attempt in range(3):
            try:
                terminal = lease.close()
                if terminal:
                    break
            except BaseException as cleanup:
                if first_failure is None:
                    first_failure = cleanup
                _sanitize_exception_graph(cleanup)
                cleanup = None
        if first_failure is not None:
            propagated = first_failure
            first_failure = None
            raise propagated from None
        if not terminal:
            raise _DatabaseCleanupFailure()
    finally:
        connection = None
        lease = None


@contextmanager
def _managed_writable_connection_scope(manager):
    if type(manager) is not _RuntimeDatabaseConnections:
        raise DurableGoogleLoginConfigurationError()
    lease = None
    connection = None
    try:
        lease = manager.open_writable_connection()
        connection = _borrow_internal_database_connection(lease)
        yield connection
    except BaseException:
        if lease is not None:
            for _attempt in range(3):
                try:
                    if lease.close():
                        break
                except BaseException as cleanup:
                    _sanitize_exception_graph(cleanup)
                    cleanup = None
        raise
    else:
        first_failure = None
        terminal = False
        for _attempt in range(3):
            try:
                terminal = lease.close()
                if terminal:
                    break
            except BaseException as cleanup:
                if first_failure is None:
                    first_failure = cleanup
                _sanitize_exception_graph(cleanup)
                cleanup = None
        if first_failure is not None:
            propagated = first_failure
            first_failure = None
            raise propagated from None
        if not terminal:
            raise _DatabaseCleanupFailure()
    finally:
        connection = None
        lease = None


@contextmanager
def _managed_read_only_lease_scope(manager):
    if type(manager) is not _RuntimeDatabaseConnections:
        raise DurableGoogleLoginConfigurationError()
    lease = None
    try:
        lease = manager._open_read_only_connection()
        yield lease
    except BaseException:
        if lease is not None:
            for _attempt in range(3):
                try:
                    if lease.close():
                        break
                except BaseException as cleanup:
                    _sanitize_exception_graph(cleanup)
                    cleanup = None
        raise
    else:
        first_failure = None
        terminal = False
        for _attempt in range(3):
            try:
                terminal = lease.close()
                if terminal:
                    break
            except BaseException as cleanup:
                if first_failure is None:
                    first_failure = cleanup
                _sanitize_exception_graph(cleanup)
                cleanup = None
        if first_failure is not None:
            propagated = first_failure
            first_failure = None
            raise propagated from None
        if not terminal:
            raise _DatabaseCleanupFailure()
    finally:
        lease = None


def _verify_open_database_target(connection, target):
    cursor = connection.cursor()
    cursor.row_factory = None
    try:
        rows = cursor.execute("PRAGMA database_list").fetchall()
    finally:
        cursor.close()
    if type(rows) is not list or len(rows) != 1:
        raise DurableGoogleLoginConfigurationError()
    row = rows[0]
    if (
        type(row) is not tuple
        or len(row) != 3
        or row[0] != 0
        or row[1] != "main"
        or type(row[2]) is not str
    ):
        raise DurableGoogleLoginConfigurationError()
    path = _database_target_path(target)
    opened_path = Path(row[2])
    if (
        not opened_path.is_absolute()
        or opened_path.resolve(strict=True) != path
    ):
        raise DurableGoogleLoginConfigurationError()
    opened = _identity_from_metadata(os.stat(opened_path))
    expected = _database_target_identity(target)
    if (opened.device, opened.inode) != (expected.device, expected.inode):
        raise DurableGoogleLoginConfigurationError()


def _attest_closed_database_schema(connection):
    try:
        exact = current_closed_schema_is_exact(connection)
    except ClosedSchemaAttestationError:
        raise DurableGoogleLoginConfigurationError() from None
    if exact is not True:
        raise DurableGoogleLoginConfigurationError()


def _sqlite_file_uri(path, *, mode):
    if (
        type(path) is not _PATH_TYPE
        or mode not in {"ro", "rw"}
        or not path.is_absolute()
    ):
        raise DurableGoogleLoginConfigurationError()
    encoded_path = quote(path.as_posix(), safe="/:")
    return f"file:{encoded_path}?mode={mode}"


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise DurableGoogleLoginConfigurationError()
        result[key] = value
    return result


def _reject_json_constant(_value):
    raise DurableGoogleLoginConfigurationError()


def _run_configuration_worker(callback, arguments, outcome):
    if (
        type(outcome) is not _ConfigurationWorkerOutcome
        or object.__getattribute__(
            outcome,
            "_ConfigurationWorkerOutcome__status",
        )
        != "pending"
    ):
        raise DurableGoogleLoginConfigurationError()
    try:
        callback(*arguments, outcome)
        if (
            object.__getattribute__(
                outcome,
                "_ConfigurationWorkerOutcome__status",
            )
            == "pending"
        ):
            raise DurableGoogleLoginConfigurationError()
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
        propagated = exc
        _sanitize_exception_graph(propagated)
        exc = None
        _close_worker_outcome_value_preserving_primary(outcome)
        outcome._replace(
            "control_flow",
            propagated,
        )
    except BaseException as exc:
        _sanitize_exception_graph(exc)
        exc = None
        _close_worker_outcome_value_preserving_primary(outcome)
        outcome._replace("failure")
    finally:
        callback = None
        arguments = None


def _worker_outcome_value(outcome):
    if type(outcome) is not _ConfigurationWorkerOutcome:
        raise DurableGoogleLoginConfigurationError()
    status = object.__getattribute__(
        outcome,
        "_ConfigurationWorkerOutcome__status",
    )
    value = object.__getattribute__(
        outcome,
        "_ConfigurationWorkerOutcome__value",
    )
    if status != "ok" or value is None:
        raise DurableGoogleLoginConfigurationError()
    return value


def _close_worker_outcome_value_preserving_primary(outcome):
    if type(outcome) is not _ConfigurationWorkerOutcome:
        return True
    value = object.__getattribute__(
        outcome,
        "_ConfigurationWorkerOutcome__value",
    )
    if value is None:
        return True
    close = getattr(value, "close", None)
    if not callable(close):
        outcome._clear_value()
        return True
    handoff_reservation = outcome._handoff_reservation()
    lease = None
    terminal = False
    try:
        lease = _retain_unresolved_activation_handoff(
            value,
            handoff_reservation,
        )
        terminal = lease.close(_expected_resource=value)
    except (
        KeyboardInterrupt,
        SystemExit,
        GeneratorExit,
    ) as exc:
        _sanitize_exception_graph(exc)
        exc = None
    except Exception as exc:
        _sanitize_exception_graph(exc)
        exc = None
    if terminal and lease is not None:
        try:
            outcome._clear_value()
            _forget_unresolved_activation_handoff(lease)
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ) as exc:
            _sanitize_exception_graph(exc)
            exc = None
            terminal = False
        except Exception as exc:
            _sanitize_exception_graph(exc)
            exc = None
            terminal = False
    lease = None
    value = None
    handoff_reservation = None
    return terminal


def _publish_configuration_worker_outcome(outcome):
    if type(outcome) is not _ConfigurationWorkerOutcome:
        outcome = None
        raise DurableGoogleLoginConfigurationError()
    status = object.__getattribute__(
        outcome,
        "_ConfigurationWorkerOutcome__status",
    )
    value = object.__getattribute__(
        outcome,
        "_ConfigurationWorkerOutcome__value",
    )
    if status == "ok":
        try:
            return value
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            _close_worker_outcome_value_preserving_primary(outcome)
            raise
        except Exception:
            _close_worker_outcome_value_preserving_primary(outcome)
            raise
    if status == "control_flow":
        propagated = value
        value = None
        outcome = None
        raise propagated from None
    outcome = None
    value = None
    error = DurableGoogleLoginConfigurationError()
    raise error from None


def _close_cleanup_resource(resource):
    close = getattr(resource, "close", None)
    if not callable(close):
        return True
    result = close()
    if type(result) is _CleanupReport:
        return result.cleanup_complete
    return result is not False


def _close_cleanup_resource_if_live(resource):
    if _cleanup_resource_is_closed(resource):
        return True
    return _close_cleanup_resource(resource)


def _transaction_key_authority_record(authority):
    from wahojobs import google_oidc_transaction_protection as protection

    try:
        record = object.__getattribute__(
            authority,
            "_GoogleOidcTransactionKeyAuthority__record",
        )
    except (AttributeError, TypeError):
        return None
    if type(record) is not protection._KeyAuthorityRecord:
        return None
    return record


def _close_transaction_key_ring(authority, ring_name):
    from wahojobs import google_oidc_transaction_protection as protection

    if ring_name not in {"lookup_keys", "protection_keys"}:
        raise DurableGoogleLoginConfigurationError()
    record = _transaction_key_authority_record(authority)
    if record is None:
        return True
    with record.lock:
        if record.closed:
            return True
        ring = getattr(record, ring_name, None)
        if type(ring) is not dict:
            raise DurableGoogleLoginConfigurationError()
        if ring:
            protection._clear_key_ring(ring)
            ring.clear()
        if not record.lookup_keys and not record.protection_keys:
            record.attestation = None
            record.closed = True
        return not ring


def _transaction_key_ring_is_closed(authority, ring_name):
    if ring_name not in {"lookup_keys", "protection_keys"}:
        return False
    record = _transaction_key_authority_record(authority)
    if record is None:
        return True
    with record.lock:
        ring = getattr(record, ring_name, None)
        return record.closed or (type(ring) is dict and not ring)


def _close_transaction_lookup_authority(authority):
    return _close_transaction_key_ring(authority, "lookup_keys")


def _close_transaction_protection_authority(authority):
    return _close_transaction_key_ring(authority, "protection_keys")


def _transaction_lookup_authority_is_closed(authority):
    return _transaction_key_ring_is_closed(authority, "lookup_keys")


def _transaction_protection_authority_is_closed(authority):
    return _transaction_key_ring_is_closed(
        authority,
        "protection_keys",
    )


def _emit_runtime_checkpoint(observer, category):
    if observer is not None:
        observer(category)


def _release_cleanup_resource(_resource):
    return True


def _cleanup_resource_is_closed(resource):
    return getattr(resource, "closed", False) is True


def _cleanup_secret_material(material):
    if type(material) is not tuple or len(material) != 4:
        return False
    (
        client_secret,
        invitation_lookup_key,
        lookup_keys,
        protection_keys,
    ) = material
    _clear_buffer(client_secret)
    _clear_buffer(invitation_lookup_key)
    _clear_key_buffers(lookup_keys)
    _clear_key_buffers(protection_keys)
    return True


def _clear_buffer(value):
    if type(value) is bytearray:
        value[:] = b"\x00" * len(value)
        value.clear()


def _clear_key_buffers(value):
    if type(value) is dict:
        for buffer in value.values():
            _clear_buffer(buffer)
        value.clear()


def _clear_json_containers(value):
    pending = [value]
    while pending:
        current = pending.pop()
        if type(current) is dict:
            pending.extend(current.values())
            current.clear()
        elif type(current) is list:
            pending.extend(current)
            current.clear()


def _close_quietly(value):
    close = None
    try:
        close = getattr(value, "close", None)
        if callable(close):
            close()
    except BaseException as exc:
        _sanitize_exception_graph(exc)
        exc = None
    finally:
        close = None
        value = None


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _detach_exception(exc):
    _sanitize_exception_graph(exc)


def _sanitize_exception_graph(exc):
    try:
        _clear_exception_graph(exc)
        return True
    except BaseException as sanitization_error:
        sanitized = _sanitize_exception_fallback(
            exc,
            sanitization_error,
        )
        sanitization_error = None
        exc = None
        return sanitized


def _clear_exception_graph(exc):
    pending = [exc]
    seen = set()
    while pending:
        current = pending.pop()
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        try:
            cause = BaseException.__dict__["__cause__"].__get__(current)
            context = BaseException.__dict__["__context__"].__get__(current)
            traceback = BaseException.__dict__["__traceback__"].__get__(
                current
            )
        except BaseException:
            cause = None
            context = None
            traceback = None
        if isinstance(cause, BaseException):
            pending.append(cause)
        if isinstance(context, BaseException):
            pending.append(context)

        cursor = traceback
        while cursor is not None:
            next_cursor = cursor.tb_next
            try:
                cursor.tb_frame.clear()
            except RuntimeError:
                pass
            cursor = next_cursor

        try:
            attributes = object.__getattribute__(current, "__dict__")
        except BaseException:
            attributes = None
        if type(attributes) is dict:
            for retained in tuple(attributes.values()):
                if isinstance(retained, BaseException):
                    pending.append(retained)
            attributes.clear()

        try:
            exception_mro = type.__getattribute__(
                type(current),
                "__mro__",
            )
        except BaseException:
            exception_mro = ()
        for exception_type in exception_mro:
            if exception_type is BaseException:
                continue
            try:
                namespace = type.__getattribute__(
                    exception_type,
                    "__dict__",
                )
            except BaseException:
                continue
            for name, descriptor in namespace.items():
                if (
                    type(name) is not str
                    or name in {"__class__", "__dict__", "__weakref__"}
                    or type(descriptor)
                    not in {GetSetDescriptorType, MemberDescriptorType}
                ):
                    continue
                try:
                    retained = descriptor.__get__(
                        current,
                        type(current),
                    )
                except (AttributeError, TypeError):
                    continue
                if isinstance(retained, BaseException):
                    pending.append(retained)
                try:
                    descriptor.__set__(current, None)
                except (AttributeError, TypeError):
                    pass

        for name, replacement in (
            ("args", ()),
            ("__traceback__", None),
            ("__cause__", None),
            ("__context__", None),
            ("__suppress_context__", True),
        ):
            try:
                BaseException.__dict__[name].__set__(
                    current,
                    replacement,
                )
            except BaseException:
                pass


def _sanitize_exception_fallback(*roots):
    pending = list(roots)
    seen = set()
    sanitized = True
    while pending:
        current = pending.pop()
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))

        for link_name in ("__cause__", "__context__"):
            try:
                linked = BaseException.__dict__[link_name].__get__(current)
            except BaseException:
                sanitized = False
                continue
            if isinstance(linked, BaseException):
                pending.append(linked)

        try:
            attributes = object.__getattribute__(current, "__dict__")
        except AttributeError:
            attributes = None
        except BaseException:
            attributes = None
            sanitized = False
        if type(attributes) is dict:
            for retained in tuple(attributes.values()):
                if isinstance(retained, BaseException):
                    pending.append(retained)
            try:
                attributes.clear()
            except BaseException:
                sanitized = False
        attributes = None

        try:
            exception_mro = type.__getattribute__(
                type(current),
                "__mro__",
            )
        except BaseException:
            exception_mro = ()
            sanitized = False
        for exception_type in exception_mro:
            if exception_type is BaseException:
                continue
            try:
                namespace = type.__getattribute__(
                    exception_type,
                    "__dict__",
                )
            except BaseException:
                sanitized = False
                continue
            for name, descriptor in namespace.items():
                if (
                    type(name) is not str
                    or name in {"__class__", "__dict__", "__weakref__"}
                    or type(descriptor)
                    not in {GetSetDescriptorType, MemberDescriptorType}
                ):
                    continue
                try:
                    retained = descriptor.__get__(
                        current,
                        type(current),
                    )
                except (AttributeError, TypeError):
                    continue
                except BaseException:
                    sanitized = False
                    continue
                if isinstance(retained, BaseException):
                    pending.append(retained)
                try:
                    descriptor.__set__(current, None)
                except (AttributeError, TypeError):
                    if retained is not None:
                        sanitized = False
                except BaseException:
                    sanitized = False

        for name, replacement in (
            ("args", ()),
            ("__traceback__", None),
            ("__cause__", None),
            ("__context__", None),
            ("__suppress_context__", True),
        ):
            try:
                BaseException.__dict__[name].__set__(
                    current,
                    replacement,
                )
            except BaseException:
                sanitized = False
        current = None
    roots = None
    return sanitized


__all__ = (
    "DurableGoogleLoginConfiguration",
    "DurableGoogleLoginConfigurationError",
    "DurableGoogleLoginRuntime",
    "build_durable_google_login_runtime",
    "load_durable_google_login_configuration",
)

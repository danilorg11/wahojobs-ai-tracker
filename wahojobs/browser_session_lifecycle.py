"""Dormant trusted mutation boundary for Migration-002 browser sessions."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import hmac
import itertools
import re
import secrets
import sqlite3
import threading
from types import MappingProxyType
import weakref

from wahojobs.account_reconciliation import (
    attest_account_schema,
    authoritative_account_row_valid,
    authoritative_auth_identity_row_valid,
    authoritative_session_row_valid,
)
from wahojobs.accounts import (
    TOKEN_HASH_VERSION,
    session_creation_request_fingerprint,
    session_rotation_request_fingerprint,
    session_secret_digest,
)
from wahojobs.browser_session_authentication import (
    MAX_AUTHENTICATION_IDENTITIES,
    MAX_SESSION_ROTATION_DEPTH,
    SESSION_COOKIE_NAME,
    _rotation_relationship_valid,
)


MIN_IDLE_TTL = timedelta(minutes=1)
MAX_IDLE_TTL = timedelta(days=30)
MIN_ABSOLUTE_TTL = timedelta(minutes=1)
MAX_ABSOLUTE_TTL = timedelta(days=90)
MAX_GENERATION_ATTEMPTS = 4
MAX_REQUEST_VAULT_ENTRIES = 16
MAX_REQUEST_VAULT_SECRET_BYTES = MAX_REQUEST_VAULT_ENTRIES * 64

_ACCOUNT_ID = re.compile(r"^usr_[0-9a-f]{32}$")
_IDENTITY_ID = re.compile(r"^auth_[0-9a-f]{32}$")
_SESSION_ID = re.compile(r"^ses_[0-9a-f]{32}$")
_ROTATION_ID = re.compile(r"^rot_[0-9a-f]{32}$")
_ISSUANCE_HANDLE = re.compile(r"^ish_[0-9a-f]{32}$")
_ISSUANCE_BINDING = re.compile(r"^isb_[0-9a-f]{32}$")
_OPAQUE_CREDENTIAL = re.compile(r"^[A-Za-z0-9_-]{43}$")
_IDEMPOTENCY_KEY = re.compile(r"^[^\x00-\x1f\x7f]{8,256}$")
_COMMAND_ISSUANCE_CAPABILITY = object()
_SERVICE_ACCESS_CAPABILITY = object()
_RESULT_ISSUANCE_CAPABILITY = object()
_RESPONSE_COMPOSITION_CAPABILITY = object()
_REQUEST_SECRET_VAULT_CAPABILITY = object()
_ISSUED_COMMANDS = weakref.WeakSet()
_ISSUED_RESULTS = weakref.WeakSet()
_SAVEPOINT_SEQUENCE = itertools.count(1)

_ERROR_MESSAGES = {
    "schema_capability_unavailable": "Session storage is temporarily unavailable.",
    "ineligible_account_or_identity": "The account is not eligible for this session operation.",
    "session_not_found": "The session is not available.",
    "session_state_conflict": "The session state does not permit this operation.",
    "stale_session": "The session changed before this operation completed.",
    "idempotency_conflict": "The session operation conflicts with an earlier request.",
    "already_completed": "The session operation was already completed.",
    "temporary_contention": "The session operation is temporarily unavailable.",
    "internal_consistency_failure": "The session operation could not be completed.",
}


class BrowserSessionLifecycleError(Exception):
    """Bounded public error with no durable or credential detail."""

    __slots__ = ("code",)

    def __init__(self, code: str):
        if code not in _ERROR_MESSAGES:
            code = "internal_consistency_failure"
        self.code = code
        super().__init__(_ERROR_MESSAGES[code])

    def as_public_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": _ERROR_MESSAGES[self.code]}

    def __repr__(self) -> str:
        return f"BrowserSessionLifecycleError(code={self.code!r})"


class CreateBrowserSessionCommand:
    __slots__ = ("_payload", "__weakref__")

    def __new__(cls, *_args, **_kwargs):
        raise TypeError("trusted_browser_session_command_required")

    @classmethod
    def _issue(cls, capability, **payload):
        if cls is not CreateBrowserSessionCommand or capability is not _COMMAND_ISSUANCE_CAPABILITY:
            raise TypeError("trusted_browser_session_command_required")
        instance = object.__new__(cls)
        object.__setattr__(instance, "_payload", MappingProxyType(dict(payload)))
        _ISSUED_COMMANDS.add(instance)
        return instance

    def _values_for_service(self, capability):
        if capability is not _SERVICE_ACCESS_CAPABILITY or self not in _ISSUED_COMMANDS:
            raise TypeError("trusted_browser_session_command_required")
        return dict(self._payload)

    def __setattr__(self, _name, _value):
        raise AttributeError("trusted_browser_session_command_is_immutable")

    def __repr__(self):
        return "CreateBrowserSessionCommand(<redacted>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("trusted_browser_session_command_not_serializable")

    def __copy__(self):
        raise TypeError("trusted_browser_session_command_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("trusted_browser_session_command_not_copyable")

    def __init_subclass__(cls, **_kwargs):
        raise TypeError("trusted_browser_session_command_not_subclassable")


class RotateBrowserSessionCommand:
    __slots__ = ("_payload", "__weakref__")

    def __new__(cls, *_args, **_kwargs):
        raise TypeError("trusted_browser_session_command_required")

    @classmethod
    def _issue(cls, capability, **payload):
        if cls is not RotateBrowserSessionCommand or capability is not _COMMAND_ISSUANCE_CAPABILITY:
            raise TypeError("trusted_browser_session_command_required")
        instance = object.__new__(cls)
        object.__setattr__(instance, "_payload", MappingProxyType(dict(payload)))
        _ISSUED_COMMANDS.add(instance)
        return instance

    def _values_for_service(self, capability):
        if capability is not _SERVICE_ACCESS_CAPABILITY or self not in _ISSUED_COMMANDS:
            raise TypeError("trusted_browser_session_command_required")
        return dict(self._payload)

    def __setattr__(self, _name, _value):
        raise AttributeError("trusted_browser_session_command_is_immutable")

    def __repr__(self):
        return "RotateBrowserSessionCommand(<redacted>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("trusted_browser_session_command_not_serializable")

    def __copy__(self):
        raise TypeError("trusted_browser_session_command_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("trusted_browser_session_command_not_copyable")

    def __init_subclass__(cls, **_kwargs):
        raise TypeError("trusted_browser_session_command_not_subclassable")


class RevokeBrowserSessionCommand:
    __slots__ = ("_payload", "__weakref__")

    def __new__(cls, *_args, **_kwargs):
        raise TypeError("trusted_browser_session_command_required")

    @classmethod
    def _issue(cls, capability, **payload):
        if cls is not RevokeBrowserSessionCommand or capability is not _COMMAND_ISSUANCE_CAPABILITY:
            raise TypeError("trusted_browser_session_command_required")
        instance = object.__new__(cls)
        object.__setattr__(instance, "_payload", MappingProxyType(dict(payload)))
        _ISSUED_COMMANDS.add(instance)
        return instance

    def _values_for_service(self, capability):
        if capability is not _SERVICE_ACCESS_CAPABILITY or self not in _ISSUED_COMMANDS:
            raise TypeError("trusted_browser_session_command_required")
        return dict(self._payload)

    def __setattr__(self, _name, _value):
        raise AttributeError("trusted_browser_session_command_is_immutable")

    def __repr__(self):
        return "RevokeBrowserSessionCommand(<redacted>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("trusted_browser_session_command_not_serializable")

    def __copy__(self):
        raise TypeError("trusted_browser_session_command_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("trusted_browser_session_command_not_copyable")

    def __init_subclass__(cls, **_kwargs):
        raise TypeError("trusted_browser_session_command_not_subclassable")


class _RequestVaultEntry:
    __slots__ = (
        "token_buffer",
        "csrf_buffer",
        "effective_expires_at",
        "ready",
        "connection_marker",
        "operation",
        "session_id",
        "account_id",
        "token_digest",
        "csrf_digest",
        "request_fingerprint",
        "predecessor_session_id",
        "binding_nonce",
    )

    def __init__(
        self,
        *,
        token_buffer,
        csrf_buffer,
        effective_expires_at,
        ready,
        connection_marker,
        operation,
        session_id,
        account_id,
        token_digest,
        csrf_digest,
        request_fingerprint,
        predecessor_session_id,
        binding_nonce,
    ):
        self.token_buffer = token_buffer
        self.csrf_buffer = csrf_buffer
        self.effective_expires_at = effective_expires_at
        self.ready = ready
        self.connection_marker = connection_marker
        self.operation = operation
        self.session_id = session_id
        self.account_id = account_id
        self.token_digest = token_digest
        self.csrf_digest = csrf_digest
        self.request_fingerprint = request_fingerprint
        self.predecessor_session_id = predecessor_session_id
        self.binding_nonce = binding_nonce

    def clear(self):
        _clear_secret_buffer(self.token_buffer)
        _clear_secret_buffer(self.csrf_buffer)
        self.token_buffer = None
        self.csrf_buffer = None


class RequestScopedSessionSecretVault:
    """Sealed request-local holder used only by trusted response composition."""

    __slots__ = (
        "_entries",
        "_closed",
        "_lock",
        "_max_entries",
        "_max_secret_bytes",
        "_secret_bytes",
        "_seal",
    )

    def __new__(cls, *_args, **_kwargs):
        raise TypeError("request_scoped_session_secret_vault_not_constructible")

    @classmethod
    def _issue(
        cls,
        capability,
        *,
        max_entries=MAX_REQUEST_VAULT_ENTRIES,
        max_secret_bytes=MAX_REQUEST_VAULT_SECRET_BYTES,
    ):
        if (
            cls is not RequestScopedSessionSecretVault
            or capability is not _REQUEST_SECRET_VAULT_CAPABILITY
            or type(max_entries) is not int
            or not 1 <= max_entries <= MAX_REQUEST_VAULT_ENTRIES
            or type(max_secret_bytes) is not int
            or not 64 <= max_secret_bytes <= MAX_REQUEST_VAULT_SECRET_BYTES
        ):
            raise TypeError("request_scoped_session_secret_vault_not_constructible")
        instance = object.__new__(cls)
        object.__setattr__(instance, "_entries", {})
        object.__setattr__(instance, "_closed", False)
        object.__setattr__(instance, "_lock", threading.Lock())
        object.__setattr__(instance, "_max_entries", max_entries)
        object.__setattr__(instance, "_max_secret_bytes", max_secret_bytes)
        object.__setattr__(instance, "_secret_bytes", 0)
        object.__setattr__(instance, "_seal", _REQUEST_SECRET_VAULT_CAPABILITY)
        return instance

    def _deposit(
        self,
        capability,
        *,
        token_buffer,
        csrf_buffer,
        effective_expires_at,
        ready,
        connection_marker,
        operation,
        session_id,
        account_id,
        token_digest,
        csrf_digest,
        request_fingerprint,
        predecessor_session_id,
        failure_injector=None,
    ):
        self._require_service_access(capability)
        if (
            type(token_buffer) is not bytearray
            or len(token_buffer) != 32
            or type(csrf_buffer) is not bytearray
            or len(csrf_buffer) != 32
            or type(ready) is not bool
            or operation not in {"create", "rotate"}
        ):
            raise BrowserSessionLifecycleError("internal_consistency_failure")
        effective_expires_at = _trusted_time(effective_expires_at)
        with self._lock:
            if (
                self._closed
                or len(self._entries) >= self._max_entries
                or self._secret_bytes + 64 > self._max_secret_bytes
            ):
                raise BrowserSessionLifecycleError("internal_consistency_failure")
            handle = self._new_handle_locked()
            binding_nonce = self._new_binding_locked()
            entry = _RequestVaultEntry(
                token_buffer=token_buffer,
                csrf_buffer=csrf_buffer,
                effective_expires_at=effective_expires_at,
                ready=ready,
                connection_marker=connection_marker,
                operation=operation,
                session_id=session_id,
                account_id=account_id,
                token_digest=token_digest,
                csrf_digest=csrf_digest,
                request_fingerprint=request_fingerprint,
                predecessor_session_id=predecessor_session_id,
                binding_nonce=binding_nonce,
            )
            _inject(failure_injector, "during_vault_deposit")
            self._entries[handle] = entry
            object.__setattr__(self, "_secret_bytes", self._secret_bytes + 64)
            return handle, binding_nonce

    def _new_handle_locked(self):
        for _attempt in range(MAX_GENERATION_ATTEMPTS):
            handle = f"ish_{secrets.token_hex(16)}"
            if _ISSUANCE_HANDLE.fullmatch(handle) is not None and handle not in self._entries:
                return handle
        raise BrowserSessionLifecycleError("internal_consistency_failure")

    def _new_binding_locked(self):
        existing = {entry.binding_nonce for entry in self._entries.values()}
        for _attempt in range(MAX_GENERATION_ATTEMPTS):
            binding_nonce = f"isb_{secrets.token_hex(16)}"
            if (
                _ISSUANCE_BINDING.fullmatch(binding_nonce) is not None
                and binding_nonce not in existing
            ):
                return binding_nonce
        raise BrowserSessionLifecycleError("internal_consistency_failure")

    def _pending_metadata(self, capability, handle):
        self._require_service_access(capability)
        with self._lock:
            entry = self._entries.get(handle)
            if entry is None or entry.ready:
                return None
            return {
                "connection_marker": entry.connection_marker,
                "operation": entry.operation,
                "session_id": entry.session_id,
                "account_id": entry.account_id,
                "token_digest": entry.token_digest,
                "csrf_digest": entry.csrf_digest,
                "request_fingerprint": entry.request_fingerprint,
                "predecessor_session_id": entry.predecessor_session_id,
                "effective_expires_at": entry.effective_expires_at,
                "binding_nonce": entry.binding_nonce,
            }

    def _mark_ready(self, capability, handle):
        self._require_service_access(capability)
        with self._lock:
            entry = self._entries.get(handle)
            if entry is None or entry.ready:
                raise BrowserSessionLifecycleError("internal_consistency_failure")
            entry.ready = True
            entry.connection_marker = None

    def _consume(
        self,
        capability,
        *,
        handle,
        expected_binding_nonce,
        expected_effective_expires_at,
        response_now,
        failure_injector=None,
    ):
        self._require_service_access(capability)
        entry = None
        with self._lock:
            if self._closed:
                return ("error", "internal_consistency_failure", True)
            entry = self._entries.get(handle)
            if entry is None:
                return ("error", "internal_consistency_failure", True)
            if not entry.ready:
                return ("error", "internal_consistency_failure", True)
            if not hmac.compare_digest(entry.binding_nonce, expected_binding_nonce):
                self._entries.pop(handle)
                object.__setattr__(self, "_secret_bytes", self._secret_bytes - 64)
                if not _clear_vault_entry(entry):
                    raise BrowserSessionLifecycleError(
                        "internal_consistency_failure"
                    )
                return ("error", "internal_consistency_failure", True)
            if entry.effective_expires_at != expected_effective_expires_at:
                self._entries.pop(handle)
                object.__setattr__(self, "_secret_bytes", self._secret_bytes - 64)
                if not _clear_vault_entry(entry):
                    raise BrowserSessionLifecycleError(
                        "internal_consistency_failure"
                    )
                return ("error", "internal_consistency_failure", True)
            self._entries.pop(handle)
            object.__setattr__(self, "_secret_bytes", self._secret_bytes - 64)
        token = None
        csrf = None
        header = None
        try:
            remaining_seconds = int(
                (entry.effective_expires_at - response_now).total_seconds()
            )
            if remaining_seconds <= 0:
                return ("error", "session_state_conflict", True)
            token = _credential_text(entry.token_buffer)
            csrf = _credential_text(entry.csrf_buffer)
            _inject(failure_injector, "during_cookie_formatting")
            header = (
                f"{SESSION_COOKIE_NAME}={token}; Path=/; "
                f"Max-Age={remaining_seconds}; "
                f"Expires={format_datetime(entry.effective_expires_at, usegmt=True)}; "
                "Secure; HttpOnly; SameSite=Lax"
            )
            if "\r" in header or "\n" in header:
                return ("error", "internal_consistency_failure", True)
            _inject(failure_injector, "before_response_return")
            return ("ok", header, csrf, True)
        except BaseException:
            return ("error", "internal_consistency_failure", True)
        finally:
            cleared = _clear_vault_entry(entry)
            token = None
            csrf = None
            header = None
            if not cleared:
                raise BrowserSessionLifecycleError("internal_consistency_failure")

    def _discard(self, capability, handle):
        self._require_service_access(capability)
        entry = None
        with self._lock:
            entry = self._entries.pop(handle, None)
            if entry is not None:
                object.__setattr__(self, "_secret_bytes", self._secret_bytes - 64)
        if entry is not None:
            if not _clear_vault_entry(entry):
                raise BrowserSessionLifecycleError("internal_consistency_failure")
            return True
        return False

    def _close(self, capability, failure_injector=None):
        self._require_service_access(capability)
        entries = []
        injected_failure = False
        cleanup_failed = False
        try:
            _inject(failure_injector, "during_vault_close")
        except BaseException:
            injected_failure = True
        finally:
            with self._lock:
                entries = list(self._entries.values())
                self._entries.clear()
                object.__setattr__(self, "_secret_bytes", 0)
                object.__setattr__(self, "_closed", True)
            for entry in entries:
                if not _clear_vault_entry(entry):
                    cleanup_failed = True
        if injected_failure or cleanup_failed:
            raise BrowserSessionLifecycleError("internal_consistency_failure")

    def _entry_count(self, capability):
        self._require_service_access(capability)
        with self._lock:
            return len(self._entries)

    def _is_closed_and_empty(self, capability):
        self._require_service_access(capability)
        with self._lock:
            return self._closed and not self._entries and self._secret_bytes == 0

    def _require_service_access(self, capability):
        if (
            capability is not _REQUEST_SECRET_VAULT_CAPABILITY
            or type(self) is not RequestScopedSessionSecretVault
            or self._seal is not _REQUEST_SECRET_VAULT_CAPABILITY
        ):
            raise BrowserSessionLifecycleError("internal_consistency_failure")

    def __setattr__(self, _name, _value):
        raise AttributeError("request_scoped_session_secret_vault_is_immutable")

    def __repr__(self):
        return "RequestScopedSessionSecretVault(<redacted>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("request_scoped_session_secret_vault_not_serializable")

    def __copy__(self):
        raise TypeError("request_scoped_session_secret_vault_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("request_scoped_session_secret_vault_not_copyable")

    def __init_subclass__(cls, **_kwargs):
        raise TypeError("request_scoped_session_secret_vault_not_subclassable")

    def __del__(self):
        entries = []
        try:
            entries = list(self._entries.values())
            self._entries.clear()
            object.__setattr__(self, "_secret_bytes", 0)
            object.__setattr__(self, "_closed", True)
        except BaseException:
            entries = []
        for entry in entries:
            try:
                _clear_vault_entry(entry)
            except BaseException:
                pass


class ConsumedSessionResponse:
    """Ephemeral values delivered once to a trusted response composer."""

    __slots__ = ("_set_cookie_header", "_csrf_credential")

    def __init__(self, capability, *, set_cookie_header, csrf_credential):
        if capability is not _RESULT_ISSUANCE_CAPABILITY:
            raise TypeError("consumed_session_response_not_constructible")
        object.__setattr__(self, "_set_cookie_header", set_cookie_header)
        object.__setattr__(self, "_csrf_credential", csrf_credential)

    @property
    def set_cookie_header(self):
        return self._set_cookie_header

    @property
    def csrf_credential(self):
        return self._csrf_credential

    def __setattr__(self, _name, _value):
        raise AttributeError("consumed_session_response_is_immutable")

    def __repr__(self):
        return "ConsumedSessionResponse(credentials=<redacted>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("consumed_session_response_not_serializable")

    def __copy__(self):
        raise TypeError("consumed_session_response_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("consumed_session_response_not_copyable")


class IssuedBrowserSession:
    """Sealed nonsecret result keyed to an independent request-scoped vault."""

    __slots__ = (
        "_status",
        "_idle_expires_at",
        "_absolute_expires_at",
        "_effective_expires_at",
        "_issuance_handle",
        "_issuance_binding",
        "_consumption_lock",
        "__weakref__",
    )

    def __new__(cls, *_args, **_kwargs):
        raise TypeError("issued_browser_session_not_constructible")

    @classmethod
    def _issue(
        cls,
        capability,
        *,
        status,
        idle_expires_at,
        absolute_expires_at,
        issuance_handle=None,
        issuance_binding=None,
    ):
        if cls is not IssuedBrowserSession or capability is not _RESULT_ISSUANCE_CAPABILITY:
            raise TypeError("issued_browser_session_not_constructible")
        if status not in {"issued", "pending_commit", "already_completed"}:
            raise TypeError("issued_browser_session_not_constructible")
        has_handle = type(issuance_handle) is str and _ISSUANCE_HANDLE.fullmatch(
            issuance_handle
        ) is not None
        has_binding = type(issuance_binding) is str and _ISSUANCE_BINDING.fullmatch(
            issuance_binding
        ) is not None
        if (status in {"issued", "pending_commit"}) != has_handle or has_handle != has_binding:
            raise TypeError("issued_browser_session_not_constructible")
        effective_expires_at = min(idle_expires_at, absolute_expires_at)
        instance = object.__new__(cls)
        object.__setattr__(instance, "_status", status)
        object.__setattr__(instance, "_idle_expires_at", _canonical_time(idle_expires_at))
        object.__setattr__(instance, "_absolute_expires_at", _canonical_time(absolute_expires_at))
        object.__setattr__(instance, "_effective_expires_at", _canonical_time(effective_expires_at))
        object.__setattr__(instance, "_issuance_handle", issuance_handle)
        object.__setattr__(instance, "_issuance_binding", issuance_binding)
        object.__setattr__(instance, "_consumption_lock", threading.Lock())
        _ISSUED_RESULTS.add(instance)
        return instance

    @property
    def status(self):
        return self._status

    @property
    def idle_expires_at(self):
        return self._idle_expires_at

    @property
    def absolute_expires_at(self):
        return self._absolute_expires_at

    @property
    def effective_expires_at(self):
        return self._effective_expires_at

    def consume_for_response(self, vault, capability, *, now, _failure_injector=None):
        return consume_issued_session(
            self,
            vault,
            capability,
            response_now=now,
            _failure_injector=_failure_injector,
        )

    def _handle_for_service(self, capability):
        if capability is not _SERVICE_ACCESS_CAPABILITY or self not in _ISSUED_RESULTS:
            raise BrowserSessionLifecycleError("internal_consistency_failure")
        return self._issuance_handle

    def _binding_for_service(self, capability):
        if capability is not _SERVICE_ACCESS_CAPABILITY or self not in _ISSUED_RESULTS:
            raise BrowserSessionLifecycleError("internal_consistency_failure")
        return self._issuance_binding

    def _clear_handle(self, capability):
        if capability is not _SERVICE_ACCESS_CAPABILITY or self not in _ISSUED_RESULTS:
            raise BrowserSessionLifecycleError("internal_consistency_failure")
        object.__setattr__(self, "_issuance_handle", None)
        object.__setattr__(self, "_issuance_binding", None)

    def _mark_issued(self, capability):
        if (
            capability is not _SERVICE_ACCESS_CAPABILITY
            or self not in _ISSUED_RESULTS
            or self._status != "pending_commit"
            or self._issuance_handle is None
            or self._issuance_binding is None
        ):
            raise BrowserSessionLifecycleError("internal_consistency_failure")
        object.__setattr__(self, "_status", "issued")

    def _mark_consumed(self, capability):
        if (
            capability is not _SERVICE_ACCESS_CAPABILITY
            or self not in _ISSUED_RESULTS
            or self._status != "issued"
            or self._issuance_handle is None
            or self._issuance_binding is None
        ):
            raise BrowserSessionLifecycleError("internal_consistency_failure")
        object.__setattr__(self, "_issuance_handle", None)
        object.__setattr__(self, "_issuance_binding", None)
        object.__setattr__(self, "_status", "consumed")

    def _mark_terminal_failed(self, capability):
        if (
            capability is not _SERVICE_ACCESS_CAPABILITY
            or self not in _ISSUED_RESULTS
            or self._status not in {"issued", "pending_commit"}
        ):
            raise BrowserSessionLifecycleError("internal_consistency_failure")
        object.__setattr__(self, "_issuance_handle", None)
        object.__setattr__(self, "_issuance_binding", None)
        object.__setattr__(self, "_status", "terminal_failed")

    def __setattr__(self, _name, _value):
        raise AttributeError("issued_browser_session_is_immutable")

    def __repr__(self):
        return f"IssuedBrowserSession(status={self._status!r}, credentials=<redacted>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("issued_browser_session_not_serializable")

    def __copy__(self):
        raise TypeError("issued_browser_session_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("issued_browser_session_not_copyable")

    def __init_subclass__(cls, **_kwargs):
        raise TypeError("issued_browser_session_not_subclassable")


class BrowserSessionMutationResult:
    __slots__ = ("_status", "__weakref__")

    def __new__(cls, *_args, **_kwargs):
        raise TypeError("browser_session_mutation_result_not_constructible")

    @classmethod
    def _issue(cls, capability, status):
        if (
            cls is not BrowserSessionMutationResult
            or capability is not _RESULT_ISSUANCE_CAPABILITY
            or status not in {"revoked", "already_completed"}
        ):
            raise TypeError("browser_session_mutation_result_not_constructible")
        instance = object.__new__(cls)
        object.__setattr__(instance, "_status", status)
        _ISSUED_RESULTS.add(instance)
        return instance

    @property
    def status(self):
        return self._status

    def __setattr__(self, _name, _value):
        raise AttributeError("browser_session_mutation_result_is_immutable")

    def __repr__(self):
        return f"BrowserSessionMutationResult(status={self._status!r})"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("browser_session_mutation_result_not_serializable")

    def __copy__(self):
        raise TypeError("browser_session_mutation_result_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("browser_session_mutation_result_not_copyable")

    def __init_subclass__(cls, **_kwargs):
        raise TypeError("browser_session_mutation_result_not_subclassable")


def create_browser_session(
    connection: sqlite3.Connection,
    command: CreateBrowserSessionCommand,
    secret_vault: RequestScopedSessionSecretVault,
    *,
    _failure_injector=None,
    _clock=None,
) -> IssuedBrowserSession:
    return _sanitized_call(
        lambda: _create_browser_session(
            connection,
            command,
            secret_vault,
            _failure_injector,
            _trusted_current_time(_clock),
        )
    )


def rotate_browser_session(
    connection: sqlite3.Connection,
    command: RotateBrowserSessionCommand,
    secret_vault: RequestScopedSessionSecretVault,
    *,
    _failure_injector=None,
    _clock=None,
) -> IssuedBrowserSession:
    return _sanitized_call(
        lambda: _rotate_browser_session(
            connection,
            command,
            secret_vault,
            _failure_injector,
            _trusted_current_time(_clock),
        )
    )


def revoke_browser_session(
    connection: sqlite3.Connection,
    command: RevokeBrowserSessionCommand,
    *,
    _failure_injector=None,
    _clock=None,
) -> BrowserSessionMutationResult:
    return _sanitized_call(
        lambda: _revoke_browser_session(
            connection,
            command,
            _failure_injector,
            _trusted_current_time(_clock),
        )
    )


def consume_issued_session(
    result: IssuedBrowserSession,
    secret_vault: RequestScopedSessionSecretVault,
    capability,
    *,
    response_now,
    _failure_injector=None,
) -> ConsumedSessionResponse:
    return _sanitized_call(
        lambda: _consume_issued_session(
            result,
            secret_vault,
            capability,
            response_now,
            _failure_injector,
        )
    )


def finalize_pending_issued_session(
    connection: sqlite3.Connection,
    result: IssuedBrowserSession,
    secret_vault: RequestScopedSessionSecretVault,
    capability,
) -> IssuedBrowserSession:
    return _sanitized_call(
        lambda: _finalize_pending_issued_session(
            connection,
            result,
            secret_vault,
            capability,
        )
    )


def close_request_scoped_secret_vault(
    secret_vault: RequestScopedSessionSecretVault,
    capability,
    *,
    _failure_injector=None,
):
    return _sanitized_call(
        lambda: _close_request_scoped_secret_vault(
            secret_vault,
            capability,
            _failure_injector,
        )
    )


def _create_browser_session(
    connection,
    command,
    secret_vault,
    failure_injector,
    current_time,
):
    _require_secret_vault(secret_vault)
    values = _command_values(command, CreateBrowserSessionCommand)
    accepted_at = values["accepted_at"]
    _require_not_future(
        accepted_at,
        current_time,
        error_code="ineligible_account_or_identity",
    )
    idle_ttl = values["idle_ttl"]
    absolute_ttl = values["absolute_ttl"]
    if idle_ttl > absolute_ttl:
        raise BrowserSessionLifecycleError("session_state_conflict")
    idle_expires_at = accepted_at + idle_ttl
    absolute_expires_at = accepted_at + absolute_ttl
    effective_expires_at = min(idle_expires_at, absolute_expires_at)
    fingerprint = session_creation_request_fingerprint(
        user_id=values["account_id"],
        idle_ttl=idle_ttl,
        absolute_ttl=absolute_ttl,
    )
    material = None
    result = None
    returned = False
    service_owned_transaction = not connection.in_transaction
    try:
        with _mutation_scope(connection):
            _attest_mutation_connection(connection)
            account, _identity = _eligible_account_and_identity(
                connection,
                account_id=values["account_id"],
                identity_id=values["supporting_identity_id"],
                accepted_at=accepted_at,
                current_time=current_time,
            )
            existing = _session_by_idempotency_key(connection, values["idempotency_key"])
            _inject(failure_injector, "after_idempotency_lookup")
            if existing is not None:
                return _creation_replay(
                    connection,
                    existing=existing,
                    account=account,
                    expected_fingerprint=fingerprint,
                    accepted_at=accepted_at,
                )
            if (
                idle_expires_at <= current_time
                or absolute_expires_at <= current_time
                or effective_expires_at <= current_time
            ):
                raise BrowserSessionLifecycleError("ineligible_account_or_identity")
            material = _generate_unique_session_material(connection)
            _inject(failure_injector, "after_credential_generation")
            _insert_session(
                connection,
                session_id=material.session_id,
                account_id=values["account_id"],
                token_digest=material.token_digest,
                csrf_digest=material.csrf_digest,
                accepted_at=accepted_at,
                idle_expires_at=idle_expires_at,
                absolute_expires_at=absolute_expires_at,
                idempotency_key=values["idempotency_key"],
                request_fingerprint=fingerprint,
            )
            _inject(failure_injector, "after_session_insert")
            if _incoming_edges(connection, material.session_id) or _outgoing_edges(
                connection, material.session_id
            ):
                raise BrowserSessionLifecycleError("internal_consistency_failure")
            _inject(failure_injector, "after_root_lineage_insert")
            stored = _session_by_id(connection, material.session_id)
            _verify_session(
                connection,
                stored,
                expected_account_id=values["account_id"],
                account_created_at=account["created_at"],
                current_time=accepted_at,
            )
            if not hmac.compare_digest(stored["request_fingerprint"], fingerprint):
                raise BrowserSessionLifecycleError("internal_consistency_failure")
            _inject(failure_injector, "after_verification")
            if not service_owned_transaction:
                result = _deposit_issued_material(
                    secret_vault,
                    material=material,
                    effective_expires_at=effective_expires_at,
                    idle_expires_at=idle_expires_at,
                    absolute_expires_at=absolute_expires_at,
                    ready=False,
                    connection=connection,
                    operation="create",
                    account_id=values["account_id"],
                    request_fingerprint=fingerprint,
                    predecessor_session_id=None,
                    failure_injector=failure_injector,
                )
            _inject(failure_injector, "before_commit_or_release")
        if service_owned_transaction:
            result = _deposit_issued_material(
                secret_vault,
                material=material,
                effective_expires_at=effective_expires_at,
                idle_expires_at=idle_expires_at,
                absolute_expires_at=absolute_expires_at,
                ready=True,
                connection=connection,
                operation="create",
                account_id=values["account_id"],
                request_fingerprint=fingerprint,
                predecessor_session_id=None,
                failure_injector=failure_injector,
            )
        returned = True
        return result
    finally:
        if material is not None:
            material.clear()
        if result is not None and not returned:
            _discard_issued_result(secret_vault, result)


def _rotate_browser_session(
    connection,
    command,
    secret_vault,
    failure_injector,
    current_time,
):
    _require_secret_vault(secret_vault)
    values = _command_values(command, RotateBrowserSessionCommand)
    accepted_at = values["accepted_at"]
    _require_not_future(accepted_at, current_time, error_code="session_state_conflict")
    material = None
    result = None
    returned = False
    service_owned_transaction = not connection.in_transaction
    try:
        with _mutation_scope(connection):
            _attest_mutation_connection(connection)
            account = _eligible_account_for_existing_session(
                connection,
                account_id=values["account_id"],
                current_time=current_time,
            )
            predecessor = _session_by_id(connection, values["session_id"])
            if predecessor is None:
                raise BrowserSessionLifecycleError("session_not_found")
            _verify_session(
                connection,
                predecessor,
                expected_account_id=values["account_id"],
                account_created_at=account["created_at"],
                current_time=accepted_at,
            )
            _require_supporting_identity_inventory(
                connection,
                account_id=values["account_id"],
                session_created_at=predecessor["created_at"],
                current_time=current_time,
            )
            fingerprint = session_rotation_request_fingerprint(
                old_token_digest=predecessor["token_hash"],
                expected_session_version=values["expected_session_version"],
                idle_ttl=values["idle_ttl"],
            )
            existing = _session_by_idempotency_key(connection, values["idempotency_key"])
            _inject(failure_injector, "after_idempotency_lookup")
            if existing is not None:
                return _rotation_replay(
                    connection,
                    predecessor=predecessor,
                    replacement=existing,
                    expected_account_id=values["account_id"],
                    expected_fingerprint=fingerprint,
                    accepted_at=accepted_at,
                )
            _require_active_current_session(
                predecessor,
                expected_version=values["expected_session_version"],
                accepted_at=accepted_at,
            )
            if _rotation_depth(connection, predecessor["session_id"]) >= MAX_SESSION_ROTATION_DEPTH:
                raise BrowserSessionLifecycleError("session_state_conflict")
            absolute_expires_at = _parse_time(predecessor["absolute_expires_at"])
            predecessor_effective_expiry = min(
                _parse_time(predecessor["idle_expires_at"]),
                absolute_expires_at,
            )
            idle_expires_at = min(accepted_at + values["idle_ttl"], absolute_expires_at)
            effective_expires_at = min(idle_expires_at, absolute_expires_at)
            if (
                predecessor_effective_expiry <= current_time
                or idle_expires_at <= current_time
                or absolute_expires_at <= current_time
                or effective_expires_at <= current_time
            ):
                raise BrowserSessionLifecycleError("session_state_conflict")
            material = _generate_unique_session_material(connection)
            rotation_id = _generate_unique_rotation_id(connection)
            _inject(failure_injector, "after_credential_generation")
            _insert_session(
                connection,
                session_id=material.session_id,
                account_id=values["account_id"],
                token_digest=material.token_digest,
                csrf_digest=material.csrf_digest,
                accepted_at=accepted_at,
                idle_expires_at=idle_expires_at,
                absolute_expires_at=absolute_expires_at,
                idempotency_key=values["idempotency_key"],
                request_fingerprint=fingerprint,
            )
            _inject(failure_injector, "after_replacement_insert")
            occurred_at = _canonical_time(accepted_at)
            cursor = connection.execute(
                "UPDATE account_sessions SET rotated_at = ?, revoked_at = ?, "
                "revoke_reason = 'session_rotated', session_version = 2 "
                "WHERE session_id = ? AND user_id = ? AND session_version = 1 "
                "AND revoked_at IS NULL AND rotated_at IS NULL",
                (
                    occurred_at,
                    occurred_at,
                    predecessor["session_id"],
                    values["account_id"],
                ),
            )
            if cursor.rowcount != 1:
                raise BrowserSessionLifecycleError("stale_session")
            _inject(failure_injector, "after_predecessor_update")
            connection.execute(
                "INSERT INTO account_session_rotations (rotation_id, user_id, "
                "predecessor_session_id, replacement_session_id, rotated_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    rotation_id,
                    values["account_id"],
                    predecessor["session_id"],
                    material.session_id,
                    occurred_at,
                    occurred_at,
                ),
            )
            _inject(failure_injector, "after_rotation_edge_insert")
            stored_predecessor = _session_by_id(connection, predecessor["session_id"])
            replacement = _session_by_id(connection, material.session_id)
            _verify_session(
                connection,
                stored_predecessor,
                expected_account_id=values["account_id"],
                account_created_at=account["created_at"],
                current_time=accepted_at,
            )
            _verify_session(
                connection,
                replacement,
                expected_account_id=values["account_id"],
                account_created_at=account["created_at"],
                current_time=accepted_at,
            )
            if (
                stored_predecessor["session_version"] != 2
                or stored_predecessor["rotated_at"] != occurred_at
                or stored_predecessor["revoked_at"] != occurred_at
                or stored_predecessor["revoke_reason"] != "session_rotated"
                or replacement["session_version"] != 1
                or not hmac.compare_digest(replacement["request_fingerprint"], fingerprint)
            ):
                raise BrowserSessionLifecycleError("internal_consistency_failure")
            _inject(failure_injector, "after_verification")
            if not service_owned_transaction:
                result = _deposit_issued_material(
                    secret_vault,
                    material=material,
                    effective_expires_at=effective_expires_at,
                    idle_expires_at=idle_expires_at,
                    absolute_expires_at=absolute_expires_at,
                    ready=False,
                    connection=connection,
                    operation="rotate",
                    account_id=values["account_id"],
                    request_fingerprint=fingerprint,
                    predecessor_session_id=predecessor["session_id"],
                    failure_injector=failure_injector,
                )
            _inject(failure_injector, "before_commit_or_release")
        if service_owned_transaction:
            result = _deposit_issued_material(
                secret_vault,
                material=material,
                effective_expires_at=effective_expires_at,
                idle_expires_at=idle_expires_at,
                absolute_expires_at=absolute_expires_at,
                ready=True,
                connection=connection,
                operation="rotate",
                account_id=values["account_id"],
                request_fingerprint=fingerprint,
                predecessor_session_id=predecessor["session_id"],
                failure_injector=failure_injector,
            )
        returned = True
        return result
    finally:
        if material is not None:
            material.clear()
        if result is not None and not returned:
            _discard_issued_result(secret_vault, result)


def _revoke_browser_session(connection, command, failure_injector, current_time):
    values = _command_values(command, RevokeBrowserSessionCommand)
    accepted_at = values["accepted_at"]
    _require_not_future(accepted_at, current_time, error_code="session_state_conflict")
    with _mutation_scope(connection):
        _attest_mutation_connection(connection)
        account = _eligible_account_for_existing_session(
            connection,
            account_id=values["account_id"],
            current_time=current_time,
        )
        session = _session_by_id(connection, values["session_id"])
        if session is None:
            raise BrowserSessionLifecycleError("session_not_found")
        _verify_session(
            connection,
            session,
            expected_account_id=values["account_id"],
            account_created_at=account["created_at"],
            current_time=accepted_at,
        )
        _require_supporting_identity_inventory(
            connection,
            account_id=values["account_id"],
            session_created_at=session["created_at"],
            current_time=current_time,
        )
        accepted_text = _canonical_time(accepted_at)
        if session["revoked_at"] is not None or session["rotated_at"] is not None:
            if (
                session["rotated_at"] is None
                and session["session_version"] == 2
                and session["revoked_at"] == accepted_text
                and session["revoke_reason"] == values["reason"]
                and values["expected_session_version"] == 1
            ):
                return BrowserSessionMutationResult._issue(
                    _RESULT_ISSUANCE_CAPABILITY,
                    "already_completed",
                )
            raise BrowserSessionLifecycleError("session_state_conflict")
        _require_active_current_session(
            session,
            expected_version=values["expected_session_version"],
            accepted_at=accepted_at,
        )
        cursor = connection.execute(
            "UPDATE account_sessions SET revoked_at = ?, revoke_reason = ?, "
            "session_version = 2 WHERE session_id = ? AND user_id = ? "
            "AND session_version = 1 AND revoked_at IS NULL AND rotated_at IS NULL",
            (
                accepted_text,
                values["reason"],
                session["session_id"],
                values["account_id"],
            ),
        )
        if cursor.rowcount != 1:
            raise BrowserSessionLifecycleError("stale_session")
        _inject(failure_injector, "after_session_update")
        stored = _session_by_id(connection, session["session_id"])
        _verify_session(
            connection,
            stored,
            expected_account_id=values["account_id"],
            account_created_at=account["created_at"],
            current_time=accepted_at,
        )
        if (
            stored["session_version"] != 2
            or stored["revoked_at"] != accepted_text
            or stored["rotated_at"] is not None
            or stored["revoke_reason"] != values["reason"]
        ):
            raise BrowserSessionLifecycleError("internal_consistency_failure")
        _inject(failure_injector, "after_fingerprint_verification")
        result = BrowserSessionMutationResult._issue(
            _RESULT_ISSUANCE_CAPABILITY,
            "revoked",
        )
        _inject(failure_injector, "before_commit_or_release")
        return result


def _deposit_issued_material(
    secret_vault,
    *,
    material,
    effective_expires_at,
    idle_expires_at,
    absolute_expires_at,
    ready,
    connection,
    operation,
    account_id,
    request_fingerprint,
    predecessor_session_id,
    failure_injector,
):
    handle = None
    binding_nonce = None
    try:
        _inject(failure_injector, "before_vault_deposit")
        token_buffer, csrf_buffer = material.buffers_for_deposit()
        handle, binding_nonce = secret_vault._deposit(
            _REQUEST_SECRET_VAULT_CAPABILITY,
            token_buffer=token_buffer,
            csrf_buffer=csrf_buffer,
            effective_expires_at=effective_expires_at,
            ready=ready,
            connection_marker=None if ready else id(connection),
            operation=operation,
            session_id=material.session_id,
            account_id=account_id,
            token_digest=material.token_digest,
            csrf_digest=material.csrf_digest,
            request_fingerprint=request_fingerprint,
            predecessor_session_id=predecessor_session_id,
            failure_injector=failure_injector,
        )
        material.relinquish_buffers()
        _inject(failure_injector, "after_vault_deposit")
        return IssuedBrowserSession._issue(
            _RESULT_ISSUANCE_CAPABILITY,
            status="issued" if ready else "pending_commit",
            idle_expires_at=idle_expires_at,
            absolute_expires_at=absolute_expires_at,
            issuance_handle=handle,
            issuance_binding=binding_nonce,
        )
    except BaseException:
        if handle is not None:
            secret_vault._discard(_REQUEST_SECRET_VAULT_CAPABILITY, handle)
        raise


def _discard_issued_result(secret_vault, result):
    handle = result._handle_for_service(_SERVICE_ACCESS_CAPABILITY)
    if handle is not None:
        secret_vault._discard(_REQUEST_SECRET_VAULT_CAPABILITY, handle)
        result._clear_handle(_SERVICE_ACCESS_CAPABILITY)


def _consume_issued_session(
    result,
    secret_vault,
    capability,
    response_now,
    failure_injector,
):
    if (
        capability is not _RESPONSE_COMPOSITION_CAPABILITY
        or type(result) is not IssuedBrowserSession
        or result not in _ISSUED_RESULTS
    ):
        raise BrowserSessionLifecycleError("already_completed")
    _require_secret_vault(secret_vault)
    now = _trusted_time(response_now)
    with result._consumption_lock:
        return _consume_issued_session_once(
            result,
            secret_vault,
            now,
            failure_injector,
        )


def _consume_issued_session_once(result, secret_vault, now, failure_injector):
    if result.status in {"consumed", "already_completed"}:
        raise BrowserSessionLifecycleError("already_completed")
    if result.status == "terminal_failed":
        raise BrowserSessionLifecycleError("internal_consistency_failure")
    if result.status == "pending_commit":
        raise BrowserSessionLifecycleError("session_state_conflict")
    if result.status != "issued":
        raise BrowserSessionLifecycleError("internal_consistency_failure")
    handle = result._handle_for_service(_SERVICE_ACCESS_CAPABILITY)
    binding_nonce = result._binding_for_service(_SERVICE_ACCESS_CAPABILITY)
    if handle is None or binding_nonce is None:
        result._mark_terminal_failed(_SERVICE_ACCESS_CAPABILITY)
        _abort_vault_consumption(secret_vault, failure_injector)
        raise BrowserSessionLifecycleError("internal_consistency_failure")
    outcome = None
    consume_failed = False
    try:
        expected_effective_expires_at = _parse_time(result.effective_expires_at)
        outcome = secret_vault._consume(
            _REQUEST_SECRET_VAULT_CAPABILITY,
            handle=handle,
            expected_binding_nonce=binding_nonce,
            expected_effective_expires_at=expected_effective_expires_at,
            response_now=now,
            failure_injector=failure_injector,
        )
    except BaseException:
        consume_failed = True
    if consume_failed:
        result._mark_terminal_failed(_SERVICE_ACCESS_CAPABILITY)
        _abort_vault_consumption(secret_vault, failure_injector)
        raise BrowserSessionLifecycleError("internal_consistency_failure")
    if outcome[0] != "ok":
        result._mark_terminal_failed(_SERVICE_ACCESS_CAPABILITY)
        _abort_vault_consumption(secret_vault, failure_injector)
        raise BrowserSessionLifecycleError(outcome[1])
    response = None
    response_failed = False
    try:
        response = ConsumedSessionResponse(
            _RESULT_ISSUANCE_CAPABILITY,
            set_cookie_header=outcome[1],
            csrf_credential=outcome[2],
        )
    except BaseException:
        response_failed = True
    if response_failed:
        result._mark_terminal_failed(_SERVICE_ACCESS_CAPABILITY)
        _abort_vault_consumption(secret_vault, failure_injector)
        outcome = None
        raise BrowserSessionLifecycleError("internal_consistency_failure")
    result._mark_consumed(_SERVICE_ACCESS_CAPABILITY)
    outcome = None
    return response


def _finalize_pending_issued_session(connection, result, secret_vault, capability):
    if (
        capability is not _RESPONSE_COMPOSITION_CAPABILITY
        or type(result) is not IssuedBrowserSession
        or result not in _ISSUED_RESULTS
        or result.status != "pending_commit"
        or type(connection) is not sqlite3.Connection
        or connection.in_transaction
    ):
        raise BrowserSessionLifecycleError("internal_consistency_failure")
    _require_secret_vault(secret_vault)
    _attest_mutation_connection(connection)
    handle = result._handle_for_service(_SERVICE_ACCESS_CAPABILITY)
    binding_nonce = result._binding_for_service(_SERVICE_ACCESS_CAPABILITY)
    metadata = secret_vault._pending_metadata(
        _REQUEST_SECRET_VAULT_CAPABILITY,
        handle,
    )
    if (
        metadata is None
        or metadata["connection_marker"] != id(connection)
        or not hmac.compare_digest(metadata["binding_nonce"], binding_nonce)
    ):
        raise BrowserSessionLifecycleError("internal_consistency_failure")
    stored = _session_by_id(connection, metadata["session_id"])
    valid = (
        stored is not None
        and stored["user_id"] == metadata["account_id"]
        and stored["session_version"] == 1
        and stored["rotated_at"] is None
        and stored["revoked_at"] is None
        and stored["revoke_reason"] is None
        and hmac.compare_digest(stored["token_hash"], metadata["token_digest"])
        and hmac.compare_digest(stored["csrf_secret_hash"], metadata["csrf_digest"])
        and hmac.compare_digest(
            stored["request_fingerprint"],
            metadata["request_fingerprint"],
        )
        and min(
            _parse_time(stored["idle_expires_at"]),
            _parse_time(stored["absolute_expires_at"]),
        )
        == metadata["effective_expires_at"]
        == _parse_time(result.effective_expires_at)
    )
    if valid and metadata["operation"] == "create":
        valid = not _incoming_edges(connection, stored["session_id"]) and not _outgoing_edges(
            connection,
            stored["session_id"],
        )
    elif valid and metadata["operation"] == "rotate":
        edges = _rows(
            connection,
            "SELECT predecessor_session_id FROM account_session_rotations "
            "WHERE replacement_session_id = ? LIMIT 2",
            (stored["session_id"],),
        )
        valid = (
            len(edges) == 1
            and edges[0]["predecessor_session_id"]
            == metadata["predecessor_session_id"]
        )
    else:
        valid = False
    if not valid:
        secret_vault._discard(_REQUEST_SECRET_VAULT_CAPABILITY, handle)
        result._mark_terminal_failed(_SERVICE_ACCESS_CAPABILITY)
        raise BrowserSessionLifecycleError("internal_consistency_failure")
    secret_vault._mark_ready(_REQUEST_SECRET_VAULT_CAPABILITY, handle)
    result._mark_issued(_SERVICE_ACCESS_CAPABILITY)
    return result


def _close_request_scoped_secret_vault(secret_vault, capability, failure_injector):
    if capability is not _RESPONSE_COMPOSITION_CAPABILITY:
        raise BrowserSessionLifecycleError("internal_consistency_failure")
    _require_secret_vault(secret_vault)
    secret_vault._close(
        _REQUEST_SECRET_VAULT_CAPABILITY,
        failure_injector=failure_injector,
    )


def _abort_vault_consumption(secret_vault, failure_injector=None):
    for attempt in range(2):
        close_failed = False
        try:
            secret_vault._close(
                _REQUEST_SECRET_VAULT_CAPABILITY,
                failure_injector=failure_injector if attempt == 0 else None,
            )
        except BaseException:
            close_failed = True
        try:
            if secret_vault._is_closed_and_empty(
                _REQUEST_SECRET_VAULT_CAPABILITY
            ) and not close_failed:
                return
        except BaseException:
            pass
    raise BrowserSessionLifecycleError("internal_consistency_failure")


def _require_secret_vault(secret_vault):
    if type(secret_vault) is not RequestScopedSessionSecretVault:
        raise BrowserSessionLifecycleError("internal_consistency_failure")
    secret_vault._require_service_access(_REQUEST_SECRET_VAULT_CAPABILITY)


def _creation_replay(
    connection,
    *,
    existing,
    account,
    expected_fingerprint,
    accepted_at,
):
    if (
        existing["user_id"] != account["user_id"]
        or not hmac.compare_digest(existing["request_fingerprint"], expected_fingerprint)
    ):
        raise BrowserSessionLifecycleError("idempotency_conflict")
    _verify_session(
        connection,
        existing,
        expected_account_id=account["user_id"],
        account_created_at=account["created_at"],
        current_time=accepted_at,
    )
    return IssuedBrowserSession._issue(
        _RESULT_ISSUANCE_CAPABILITY,
        status="already_completed",
        idle_expires_at=_parse_time(existing["idle_expires_at"]),
        absolute_expires_at=_parse_time(existing["absolute_expires_at"]),
    )


def _rotation_replay(
    connection,
    *,
    predecessor,
    replacement,
    expected_account_id,
    expected_fingerprint,
    accepted_at,
):
    edges = _rows(
        connection,
        "SELECT rotation_id, user_id, predecessor_session_id, replacement_session_id, "
        "rotated_at, created_at FROM account_session_rotations "
        "WHERE predecessor_session_id = ? AND replacement_session_id = ? LIMIT 2",
        (predecessor["session_id"], replacement["session_id"]),
    )
    if (
        replacement["user_id"] != expected_account_id
        or not hmac.compare_digest(replacement["request_fingerprint"], expected_fingerprint)
        or len(edges) != 1
        or predecessor["session_version"] != 2
        or predecessor["rotated_at"] != edges[0]["rotated_at"]
        or predecessor["revoked_at"] != edges[0]["rotated_at"]
        or predecessor["revoke_reason"] != "session_rotated"
    ):
        raise BrowserSessionLifecycleError("idempotency_conflict")
    _verify_session(
        connection,
        predecessor,
        expected_account_id=expected_account_id,
        current_time=accepted_at,
    )
    _verify_session(
        connection,
        replacement,
        expected_account_id=expected_account_id,
        current_time=accepted_at,
    )
    return IssuedBrowserSession._issue(
        _RESULT_ISSUANCE_CAPABILITY,
        status="already_completed",
        idle_expires_at=_parse_time(replacement["idle_expires_at"]),
        absolute_expires_at=_parse_time(replacement["absolute_expires_at"]),
    )


def _attest_mutation_connection(connection):
    if (
        type(connection) is not sqlite3.Connection
        or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
        or connection.execute("PRAGMA query_only").fetchone()[0] != 0
        or not attest_account_schema(connection)
    ):
        raise BrowserSessionLifecycleError("schema_capability_unavailable")


@contextmanager
def _mutation_scope(connection):
    if type(connection) is not sqlite3.Connection:
        raise BrowserSessionLifecycleError("schema_capability_unavailable")
    if (
        connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
        or connection.execute("PRAGMA query_only").fetchone()[0] != 0
    ):
        raise BrowserSessionLifecycleError("schema_capability_unavailable")
    owns_transaction = not connection.in_transaction
    savepoint = None
    if owns_transaction:
        connection.execute("BEGIN IMMEDIATE")
    else:
        savepoint = (
            f"browser_session_lifecycle_{next(_SAVEPOINT_SEQUENCE)}_"
            f"{secrets.token_hex(8)}"
        )
        connection.execute(f"SAVEPOINT {savepoint}")
    failure = None
    try:
        yield
    except BaseException as exc:
        failure = exc
    if failure is None:
        try:
            if owns_transaction:
                connection.commit()
            else:
                connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            return
        except BaseException as exc:
            failure = exc
    cleanup_succeeded = (
        _rollback_owned_transaction(connection)
        if owns_transaction
        else _rollback_nested_savepoint(connection, savepoint)
    )
    if not cleanup_succeeded:
        failure = None
        raise BrowserSessionLifecycleError("internal_consistency_failure") from None
    raise failure


def _rollback_owned_transaction(connection):
    for _attempt in range(2):
        if not connection.in_transaction:
            return True
        try:
            _connection_rollback(connection)
        except BaseException:
            pass
    if connection.in_transaction:
        try:
            connection.execute("ROLLBACK")
        except BaseException:
            pass
    return not connection.in_transaction


def _connection_rollback(connection):
    connection.rollback()


def _rollback_nested_savepoint(connection, savepoint):
    if not connection.in_transaction:
        return False
    rollback_succeeded = False
    for _attempt in range(2):
        try:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            rollback_succeeded = True
            break
        except BaseException:
            pass
    if not rollback_succeeded or not connection.in_transaction:
        return False
    release_succeeded = False
    for _attempt in range(2):
        try:
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            release_succeeded = True
            break
        except BaseException:
            pass
    return release_succeeded and connection.in_transaction


def _eligible_account_and_identity(
    connection,
    *,
    account_id,
    identity_id,
    accepted_at,
    current_time,
):
    account = _eligible_account_for_existing_session(
        connection,
        account_id=account_id,
        current_time=current_time,
    )
    identity_rows = _rows(
        connection,
        "SELECT auth_identity_id, user_id, provider, provider_subject, verified_email, "
        "email_verified, created_at, last_authenticated_at, disabled_at, "
        "link_idempotency_key, request_fingerprint FROM auth_identities "
        "WHERE auth_identity_id = ? LIMIT 2",
        (identity_id,),
    )
    if len(identity_rows) != 1:
        raise BrowserSessionLifecycleError("ineligible_account_or_identity")
    identity = identity_rows[0]
    if not authoritative_auth_identity_row_valid(identity):
        raise BrowserSessionLifecycleError("internal_consistency_failure")
    if identity["user_id"] != account_id or identity["disabled_at"] is not None:
        raise BrowserSessionLifecycleError("ineligible_account_or_identity")
    identity_created = _parse_time(identity["created_at"])
    last_authenticated = _parse_time(identity["last_authenticated_at"])
    if (
        identity_created < _parse_time(account["created_at"])
        or last_authenticated < identity_created
        or identity_created > accepted_at
        or last_authenticated > current_time
    ):
        raise BrowserSessionLifecycleError("internal_consistency_failure")
    return account, identity


def _eligible_account_for_existing_session(connection, *, account_id, current_time):
    accounts = _rows(
        connection,
        "SELECT user_id, lifecycle_status, row_version, created_at, updated_at, "
        "deletion_requested_at, deactivated_at FROM users WHERE user_id = ? LIMIT 2",
        (account_id,),
    )
    if len(accounts) != 1:
        raise BrowserSessionLifecycleError("ineligible_account_or_identity")
    account = accounts[0]
    if not authoritative_account_row_valid(account, expected_user_id=account_id):
        raise BrowserSessionLifecycleError("internal_consistency_failure")
    if account["lifecycle_status"] != "active":
        raise BrowserSessionLifecycleError("ineligible_account_or_identity")
    if (
        _parse_time(account["created_at"]) > current_time
        or _parse_time(account["updated_at"]) > current_time
    ):
        raise BrowserSessionLifecycleError("internal_consistency_failure")
    return account


def _require_supporting_identity_inventory(
    connection,
    *,
    account_id,
    session_created_at,
    current_time,
):
    identities = _rows(
        connection,
        "SELECT auth_identity_id, user_id, provider, provider_subject, verified_email, "
        "email_verified, created_at, last_authenticated_at, disabled_at, "
        "link_idempotency_key, request_fingerprint FROM auth_identities "
        "WHERE user_id = ? ORDER BY auth_identity_id LIMIT ?",
        (account_id, MAX_AUTHENTICATION_IDENTITIES + 1),
    )
    if not identities or len(identities) > MAX_AUTHENTICATION_IDENTITIES:
        raise BrowserSessionLifecycleError("internal_consistency_failure")
    session_created = _parse_time(session_created_at)
    supporting = []
    for identity in identities:
        if not authoritative_auth_identity_row_valid(identity, expected_user_id=account_id):
            raise BrowserSessionLifecycleError("internal_consistency_failure")
        created = _parse_time(identity["created_at"])
        last_authenticated = _parse_time(identity["last_authenticated_at"])
        if (
            created > current_time
            or last_authenticated < created
            or last_authenticated > current_time
        ):
            raise BrowserSessionLifecycleError("internal_consistency_failure")
        if identity["disabled_at"] is None and created <= session_created:
            supporting.append(identity)
    if not supporting:
        raise BrowserSessionLifecycleError("ineligible_account_or_identity")


def _require_active_current_session(session, *, expected_version, accepted_at):
    if expected_version != 1 or session["session_version"] != expected_version:
        raise BrowserSessionLifecycleError("stale_session")
    if session["revoked_at"] is not None or session["rotated_at"] is not None:
        raise BrowserSessionLifecycleError("session_state_conflict")
    if (
        _parse_time(session["created_at"]) > accepted_at
        or _parse_time(session["last_seen_at"]) > accepted_at
        or _parse_time(session["idle_expires_at"]) <= accepted_at
        or _parse_time(session["absolute_expires_at"]) <= accepted_at
    ):
        raise BrowserSessionLifecycleError("session_state_conflict")


def _verify_session(
    connection,
    session,
    *,
    expected_account_id,
    current_time,
    account_created_at=None,
):
    if session is None or not authoritative_session_row_valid(
        session,
        expected_user_id=expected_account_id,
        account_created_at=account_created_at,
    ):
        raise BrowserSessionLifecycleError("internal_consistency_failure")
    if not _rotation_relationship_valid(
        connection,
        session,
        current_time=current_time,
    ):
        raise BrowserSessionLifecycleError("internal_consistency_failure")


class _GeneratedSessionMaterial:
    __slots__ = (
        "session_id",
        "token_digest",
        "csrf_digest",
        "_token_buffer",
        "_csrf_buffer",
    )

    def __init__(
        self,
        *,
        session_id,
        token_digest,
        csrf_digest,
        token_buffer,
        csrf_buffer,
    ):
        self.session_id = session_id
        self.token_digest = token_digest
        self.csrf_digest = csrf_digest
        self._token_buffer = token_buffer
        self._csrf_buffer = csrf_buffer

    def buffers_for_deposit(self):
        if (
            type(self._token_buffer) is not bytearray
            or type(self._csrf_buffer) is not bytearray
        ):
            raise BrowserSessionLifecycleError("internal_consistency_failure")
        return self._token_buffer, self._csrf_buffer

    def relinquish_buffers(self):
        self._token_buffer = None
        self._csrf_buffer = None

    def clear(self):
        _clear_secret_buffer(self._token_buffer)
        _clear_secret_buffer(self._csrf_buffer)
        self._token_buffer = None
        self._csrf_buffer = None

def _clear_secret_buffer(value):
    if type(value) is bytearray:
        value[:] = b"\x00" * len(value)


def _clear_vault_entry(entry):
    for _attempt in range(2):
        try:
            entry.clear()
        except BaseException:
            pass
        if entry.token_buffer is None and entry.csrf_buffer is None:
            return True
        try:
            _clear_secret_buffer(entry.token_buffer)
            _clear_secret_buffer(entry.csrf_buffer)
            entry.token_buffer = None
            entry.csrf_buffer = None
        except BaseException:
            pass
        if entry.token_buffer is None and entry.csrf_buffer is None:
            return True
    return False


def _generate_unique_session_material(connection):
    for _attempt in range(MAX_GENERATION_ATTEMPTS):
        token_buffer = None
        csrf_buffer = None
        retained = False
        token = None
        csrf = None
        try:
            session_id = f"ses_{secrets.token_hex(16)}"
            token_buffer = _credential_buffer(_generate_credential())
            csrf_buffer = _credential_buffer(_generate_credential())
            if (
                hmac.compare_digest(token_buffer, csrf_buffer)
                or _SESSION_ID.fullmatch(session_id) is None
            ):
                continue
            token = _credential_text(token_buffer)
            csrf = _credential_text(csrf_buffer)
            token_digest = session_secret_digest(token)
            csrf_digest = session_secret_digest(csrf)
            token = None
            csrf = None
            collision = connection.execute(
                "SELECT 1 FROM account_sessions WHERE session_id = ? "
                "OR token_hash IN (?, ?) OR csrf_secret_hash IN (?, ?) LIMIT 1",
                (
                    session_id,
                    token_digest,
                    csrf_digest,
                    token_digest,
                    csrf_digest,
                ),
            ).fetchone()
            if collision is None:
                retained = True
                return _GeneratedSessionMaterial(
                    session_id=session_id,
                    token_digest=token_digest,
                    csrf_digest=csrf_digest,
                    token_buffer=token_buffer,
                    csrf_buffer=csrf_buffer,
                )
        finally:
            token = None
            csrf = None
            if not retained:
                _clear_secret_buffer(token_buffer)
                _clear_secret_buffer(csrf_buffer)
    raise BrowserSessionLifecycleError("internal_consistency_failure")


def _generate_unique_rotation_id(connection):
    for _attempt in range(MAX_GENERATION_ATTEMPTS):
        rotation_id = f"rot_{secrets.token_hex(16)}"
        if _ROTATION_ID.fullmatch(rotation_id) is None:
            continue
        if connection.execute(
            "SELECT 1 FROM account_session_rotations WHERE rotation_id = ? LIMIT 1",
            (rotation_id,),
        ).fetchone() is None:
            return rotation_id
    raise BrowserSessionLifecycleError("internal_consistency_failure")


def _generate_credential():
    raw = secrets.token_bytes(32)
    if type(raw) is not bytes or len(raw) != 32:
        raise BrowserSessionLifecycleError("internal_consistency_failure")
    return bytearray(raw)


def _credential_buffer(value):
    if type(value) is bytearray and len(value) == 32:
        return value
    if type(value) is str and _OPAQUE_CREDENTIAL.fullmatch(value) is not None:
        failed = False
        decoded = None
        try:
            decoded = base64.urlsafe_b64decode(value + "=")
        except (TypeError, ValueError):
            failed = True
        if not failed and type(decoded) is bytes and len(decoded) == 32:
            return bytearray(decoded)
    raise BrowserSessionLifecycleError("internal_consistency_failure")


def _credential_text(buffer):
    if type(buffer) is not bytearray or len(buffer) != 32:
        raise BrowserSessionLifecycleError("internal_consistency_failure")
    credential = base64.urlsafe_b64encode(buffer).rstrip(b"=").decode("ascii")
    if _OPAQUE_CREDENTIAL.fullmatch(credential) is None:
        raise BrowserSessionLifecycleError("internal_consistency_failure")
    return credential


def _insert_session(
    connection,
    *,
    session_id,
    account_id,
    token_digest,
    csrf_digest,
    accepted_at,
    idle_expires_at,
    absolute_expires_at,
    idempotency_key,
    request_fingerprint,
):
    accepted_text = _canonical_time(accepted_at)
    connection.execute(
        "INSERT INTO account_sessions (session_id, user_id, token_hash, "
        "token_hash_version, csrf_secret_hash, csrf_hash_version, created_at, "
        "last_seen_at, idle_expires_at, absolute_expires_at, session_version, "
        "creation_idempotency_key, request_fingerprint) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
        (
            session_id,
            account_id,
            token_digest,
            TOKEN_HASH_VERSION,
            csrf_digest,
            TOKEN_HASH_VERSION,
            accepted_text,
            accepted_text,
            _canonical_time(idle_expires_at),
            _canonical_time(absolute_expires_at),
            idempotency_key,
            request_fingerprint,
        ),
    )


def _session_by_id(connection, session_id):
    rows = _rows(
        connection,
        "SELECT session_id, user_id, token_hash, token_hash_version, "
        "csrf_secret_hash, csrf_hash_version, created_at, last_seen_at, "
        "idle_expires_at, absolute_expires_at, rotated_at, revoked_at, "
        "revoke_reason, session_version, creation_idempotency_key, "
        "request_fingerprint FROM account_sessions WHERE session_id = ? LIMIT 2",
        (session_id,),
    )
    if len(rows) > 1:
        raise BrowserSessionLifecycleError("internal_consistency_failure")
    return rows[0] if rows else None


def _session_by_idempotency_key(connection, key):
    rows = _rows(
        connection,
        "SELECT session_id, user_id, token_hash, token_hash_version, "
        "csrf_secret_hash, csrf_hash_version, created_at, last_seen_at, "
        "idle_expires_at, absolute_expires_at, rotated_at, revoked_at, "
        "revoke_reason, session_version, creation_idempotency_key, "
        "request_fingerprint FROM account_sessions "
        "WHERE creation_idempotency_key = ? LIMIT 2",
        (key,),
    )
    if len(rows) > 1:
        raise BrowserSessionLifecycleError("internal_consistency_failure")
    return rows[0] if rows else None


def _incoming_edges(connection, session_id):
    return _rows(
        connection,
        "SELECT rotation_id FROM account_session_rotations "
        "WHERE replacement_session_id = ? LIMIT 2",
        (session_id,),
    )


def _outgoing_edges(connection, session_id):
    return _rows(
        connection,
        "SELECT rotation_id FROM account_session_rotations "
        "WHERE predecessor_session_id = ? LIMIT 2",
        (session_id,),
    )


def _rotation_depth(connection, session_id):
    depth = 0
    current = session_id
    visited = set()
    while True:
        if current in visited or depth > MAX_SESSION_ROTATION_DEPTH:
            raise BrowserSessionLifecycleError("internal_consistency_failure")
        visited.add(current)
        edges = _rows(
            connection,
            "SELECT predecessor_session_id FROM account_session_rotations "
            "WHERE replacement_session_id = ? LIMIT 2",
            (current,),
        )
        if not edges:
            return depth
        if len(edges) != 1:
            raise BrowserSessionLifecycleError("internal_consistency_failure")
        current = edges[0]["predecessor_session_id"]
        depth += 1


def _rows(connection, sql, parameters):
    cursor = connection.execute(sql, parameters)
    names = tuple(item[0] for item in cursor.description)
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _command_values(command, expected_type):
    if type(command) is not expected_type or command not in _ISSUED_COMMANDS:
        raise BrowserSessionLifecycleError("internal_consistency_failure")
    return command._values_for_service(_SERVICE_ACCESS_CAPABILITY)


def _validated_id(value, pattern):
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise TypeError("invalid_trusted_browser_session_command")
    return value


def _validated_idempotency_key(value):
    if (
        type(value) is not str
        or value != value.strip()
        or _IDEMPOTENCY_KEY.fullmatch(value) is None
    ):
        raise TypeError("invalid_trusted_browser_session_command")
    return value


def _validated_expected_version(value):
    if type(value) is not int or value < 1:
        raise TypeError("invalid_trusted_browser_session_command")
    return value


def _validated_revoke_reason(value):
    if value not in {"explicit_revoke", "security_reset", "stale", "user_logout"}:
        raise TypeError("invalid_trusted_browser_session_command")
    return value


def _validated_ttl(value, *, minimum, maximum):
    if type(value) is not timedelta:
        raise TypeError("invalid_trusted_browser_session_command")
    seconds = value.total_seconds()
    if seconds != int(seconds) or not (minimum <= value <= maximum):
        raise TypeError("invalid_trusted_browser_session_command")
    return value


def _trusted_time(value):
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
        or value.microsecond != 0
    ):
        raise TypeError("invalid_trusted_browser_session_command")
    return value.astimezone(timezone.utc)


def _trusted_current_time(clock):
    if clock is not None and not callable(clock):
        raise BrowserSessionLifecycleError("internal_consistency_failure")
    callback_failed = False
    value = None
    try:
        value = (
            datetime.now(timezone.utc).replace(microsecond=0)
            if clock is None
            else clock()
        )
    except Exception:
        callback_failed = True
    if callback_failed:
        raise BrowserSessionLifecycleError("internal_consistency_failure")
    invalid_value = False
    validated = None
    try:
        validated = _trusted_time(value)
    except (TypeError, ValueError):
        invalid_value = True
    if invalid_value:
        raise BrowserSessionLifecycleError("internal_consistency_failure")
    return validated


def _require_not_future(accepted_at, current_time, *, error_code):
    if accepted_at > current_time:
        raise BrowserSessionLifecycleError(error_code)


def _canonical_time(value):
    return _trusted_time(value).isoformat()


def _parse_time(value):
    if type(value) is not str or len(value) != 25:
        raise BrowserSessionLifecycleError("internal_consistency_failure")
    failed = False
    parsed = None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S+00:00").replace(
            tzinfo=timezone.utc
        )
    except (TypeError, ValueError):
        failed = True
    if failed or parsed.isoformat() != value:
        raise BrowserSessionLifecycleError("internal_consistency_failure")
    return parsed


def _inject(callback, point):
    if callback is None:
        return
    if not callable(callback):
        raise BrowserSessionLifecycleError("internal_consistency_failure")
    callback(point)


def _sanitized_call(callback):
    failure_code = None
    try:
        return callback()
    except BrowserSessionLifecycleError as exc:
        failure_code = exc.code
    except sqlite3.OperationalError as exc:
        code = getattr(exc, "sqlite_errorcode", None)
        failure_code = (
            "temporary_contention"
            if code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
            else "internal_consistency_failure"
        )
    except BaseException:
        failure_code = "internal_consistency_failure"
    callback = None
    raise BrowserSessionLifecycleError(failure_code) from None

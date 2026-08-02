"""Explicit browser adapter for the durable Google OIDC login flow.

The adapter has no startup side effects.  It is activated only when a fully
constructed instance is injected into the local browser application.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import functools
import itertools
import hmac
import html
from http import HTTPStatus
import os
import re
import secrets
import threading
from types import GetSetDescriptorType, MemberDescriptorType
from urllib.parse import parse_qsl, urlsplit


LOGIN_ROUTE = "/login"
GOOGLE_LOGIN_START_ROUTE = "/auth/google/start"
GOOGLE_LOGIN_CALLBACK_ROUTE = "/auth/google/callback"
LOGOUT_ROUTE = "/logout"
AUTHENTICATED_DESTINATION = "/account/profile"
PERSISTENT_PROFILE_ROUTE = AUTHENTICATED_DESTINATION
FIND_MATCHES_ROUTE = "/find-matches"

LOGIN_CSRF_COOKIE_NAME = "__Host-wahojobs_login_csrf"
GOOGLE_TRANSACTION_COOKIE_NAME = "__Host-wahojobs_google_tx"
SESSION_CSRF_COOKIE_NAME = "__Host-wahojobs_session_csrf"
SESSION_COOKIE_NAME = "wahojobs_session"

MAX_BROWSER_AUTH_RESPONSE_BYTES = 1_048_576
MAX_BROWSER_AUTH_TARGET_BYTES = 8_192
MAX_BROWSER_AUTH_FORM_BYTES = 1_024
MAX_BROWSER_AUTH_COOKIE_BYTES = 4_096
MAX_BROWSER_AUTH_HEADERS = 64
MAX_BROWSER_AUTH_COOKIES = 16
LOGIN_CONTEXT_MAX_AGE_SECONDS = 600

_AUTH_ROUTES = frozenset(
    {
        LOGIN_ROUTE,
        GOOGLE_LOGIN_START_ROUTE,
        GOOGLE_LOGIN_CALLBACK_ROUTE,
        LOGOUT_ROUTE,
        PERSISTENT_PROFILE_ROUTE,
        FIND_MATCHES_ROUTE,
    }
)
_DELEGATED_ACCOUNT_ROUTES = frozenset(
    {PERSISTENT_PROFILE_ROUTE, FIND_MATCHES_ROUTE}
)
_ALLOWED_METHODS = {
    LOGIN_ROUTE: ("GET",),
    GOOGLE_LOGIN_START_ROUTE: ("POST",),
    GOOGLE_LOGIN_CALLBACK_ROUTE: ("GET",),
    LOGOUT_ROUTE: ("GET", "POST"),
}
_PROXY_HEADERS = frozenset(
    {
        "forwarded",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-port",
        "x-forwarded-prefix",
        "x-forwarded-proto",
    }
)
_HTTP_TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_COOKIE_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_OPAQUE_CREDENTIAL = re.compile(r"^[A-Za-z0-9_-]{43}$")
_INVITATION_CREDENTIAL = re.compile(
    r"^inv_[0-9a-f]{32}\.[A-Za-z0-9_-]{43}$"
)
_TRANSACTION_ID = re.compile(r"^oidctx_[0-9a-f]{32}$")
_CONTENT_LENGTH = re.compile(r"^(?:0|[1-9][0-9]{0,3})$")
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_AUTHORITY = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?|\[[0-9a-f:]+\])"
    r"(?::[1-9][0-9]{0,4})?$"
)
_SESSION_COOKIE = re.compile(
    r"^wahojobs_session=([A-Za-z0-9_-]{43}); Path=/; "
    r"Max-Age=([1-9][0-9]{0,7}); Expires=([^;\r\n]{1,64}); "
    r"Secure; HttpOnly; SameSite=Lax$"
)
_HEADER_VALUE_FORBIDDEN = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
_EXPIRED_COOKIE_DATE = "Thu, 01 Jan 1970 00:00:00 GMT"

_SECURITY_HEADERS = (
    (
        "Content-Security-Policy",
        "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'",
    ),
    ("Referrer-Policy", "no-referrer"),
    ("X-Content-Type-Options", "nosniff"),
    ("Cache-Control", "no-store"),
)
_DELIVERY_AUTHORITY_CAPABILITY = object()
_ABANDONED_BROWSER_CLEANUP_CAPABILITY = object()


class _LocalDeliveryAuthority:
    __slots__ = (
        "__generation",
        "__issuance",
        "__pid",
        "__process_epoch",
        "__thread",
    )

    def __init__(self):
        object.__setattr__(self, "_LocalDeliveryAuthority__pid", os.getpid())
        object.__setattr__(
            self,
            "_LocalDeliveryAuthority__process_epoch",
            object(),
        )
        object.__setattr__(
            self,
            "_LocalDeliveryAuthority__thread",
            threading.current_thread(),
        )
        object.__setattr__(
            self,
            "_LocalDeliveryAuthority__issuance",
            object(),
        )
        object.__setattr__(
            self,
            "_LocalDeliveryAuthority__generation",
            1,
        )

    def _require_before_lock(self):
        if (
            type(self) is not _LocalDeliveryAuthority
            or os.getpid() != self.__pid
            or threading.current_thread() is not self.__thread
            or self.__process_epoch is None
            or self.__issuance is None
            or self.__generation != 1
        ):
            raise RuntimeError("browser_response_authority_mismatch")
        return True

    def __setattr__(self, _name, _value):
        raise AttributeError("browser_response_authority_is_immutable")

    def __delattr__(self, _name):
        raise AttributeError("browser_response_authority_is_immutable")

    def __repr__(self):
        return "_LocalDeliveryAuthority(<sealed>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("browser_response_authority_not_serializable")


class _AuthorityFencedSessionDeliveryLease:
    __slots__ = (
        "__authority",
        "__connection",
        "__lease",
        "__owner",
    )

    def __init__(self, lease, owner, connection):
        authority = _delivery_authority_for_owner(owner)
        validator = getattr(
            owner,
            "_wahojobs_validate_delivery_authority",
            None,
        )
        if validator is not None and not callable(validator):
            raise ValueError("invalid_browser_session_delivery")
        if callable(validator):
            try:
                retained_connection = object.__getattribute__(
                    lease,
                    "_connection",
                )
            except (AttributeError, TypeError):
                raise ValueError("invalid_browser_session_delivery") from None
            if connection is None:
                connection = retained_connection
            if retained_connection is not connection:
                raise ValueError("invalid_browser_session_delivery")
            validator(authority, connection)
        if not all(
            callable(getattr(lease, name, None))
            for name in ("acknowledge_delivery", "fail_delivery")
        ):
            raise ValueError("invalid_browser_session_delivery")
        object.__setattr__(
            self,
            "_AuthorityFencedSessionDeliveryLease__authority",
            authority,
        )
        object.__setattr__(
            self,
            "_AuthorityFencedSessionDeliveryLease__connection",
            connection,
        )
        object.__setattr__(
            self,
            "_AuthorityFencedSessionDeliveryLease__lease",
            lease,
        )
        object.__setattr__(
            self,
            "_AuthorityFencedSessionDeliveryLease__owner",
            owner,
        )

    def _require_current(self):
        authority = self.__authority
        _require_delivery_authority_before_lock(authority)
        validator = getattr(
            self.__owner,
            "_wahojobs_validate_delivery_authority",
            None,
        )
        if callable(validator):
            validator(authority, self.__connection)
        return True

    def _response_authority(self, capability, owner):
        if (
            capability is not _DELIVERY_AUTHORITY_CAPABILITY
            or owner is not self.__owner
        ):
            raise ValueError("invalid_browser_session_delivery")
        self._require_current()
        return self.__authority

    def _prepare_response_operation(self, capability, owner, operation):
        if (
            capability is not _DELIVERY_AUTHORITY_CAPABILITY
            or owner is not self.__owner
            or operation
            not in {"acknowledge_delivery", "fail_delivery"}
        ):
            raise ValueError("invalid_browser_session_delivery")
        self._require_current()
        prepared = getattr(self.__lease, operation, None)
        if not callable(prepared):
            raise ValueError("invalid_browser_session_delivery")
        return prepared

    @property
    def status(self):
        self._require_current()
        return getattr(self.__lease, "status")

    @property
    def set_cookie_header(self):
        self._require_current()
        return getattr(self.__lease, "set_cookie_header")

    @property
    def csrf_credential(self):
        self._require_current()
        return getattr(self.__lease, "csrf_credential")

    def acknowledge_delivery(self):
        self._require_current()
        return self.__lease.acknowledge_delivery()

    def fail_delivery(self):
        self._require_current()
        return self.__lease.fail_delivery()

    def __setattr__(self, _name, _value):
        raise AttributeError("browser_session_delivery_lease_is_immutable")

    def __delattr__(self, _name):
        raise AttributeError("browser_session_delivery_lease_is_immutable")

    def __repr__(self):
        return (
            "_AuthorityFencedSessionDeliveryLease("
            "status=<redacted>, credentials=<redacted>)"
        )

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("browser_session_delivery_lease_not_serializable")


def _require_delivery_authority_before_lock(authority):
    require = getattr(authority, "_require_before_lock", None)
    if not callable(require):
        raise ValueError("invalid_browser_session_delivery")
    return require()


def _delivery_authority_for_owner(owner):
    factory = getattr(owner, "_wahojobs_delivery_authority", None)
    if factory is None:
        authority = _LocalDeliveryAuthority()
    elif callable(factory):
        authority = factory()
    else:
        raise ValueError("invalid_browser_session_delivery")
    _require_delivery_authority_before_lock(authority)
    return authority


def _adopt_session_delivery_lease(lease, owner, connection):
    if type(lease) is _AuthorityFencedSessionDeliveryLease:
        lease._response_authority(
            _DELIVERY_AUTHORITY_CAPABILITY,
            owner,
        )
        return lease
    return _AuthorityFencedSessionDeliveryLease(
        lease,
        owner,
        connection,
    )


def _publish_browser_call_result(offer, callback, arguments):
    if (
        type(offer) is not list
        or offer
        or not callable(callback)
        or type(arguments) is not tuple
    ):
        raise RuntimeError("browser_response_ownership_invalid")
    publication = next(
        map(
            offer.append,
            itertools.starmap(callback, (arguments,)),
        )
    )
    if publication is not None or len(offer) != 1:
        raise RuntimeError("browser_response_ownership_invalid")
    return True


class _BrowserRequestDeliveryOwner:
    """Stable owner for request, delivery, and database-release obligations."""

    __slots__ = (
        "__abandoned_cleanup_thread",
        "__abandoned_cleanup_token",
        "__abort_requested",
        "__connection_offer",
        "__connection_complete",
        "__delivery_bundle",
        "__delivery_complete",
        "__delivery_offer",
        "__delivery_operation",
        "__integration",
        "__issuance",
        "__lock",
        "__pid",
        "__process_guard",
        "__registry_entry",
        "__request_complete",
        "__raw_connection_offer",
        "__response",
        "__response_published",
        "__response_scrubbed",
        "__standalone",
        "__standalone_request_release",
        "__terminal",
        "__thread",
    )

    def __init__(self, integration=None, *, request_release=None):
        process_guard = (
            getattr(integration, "_process_guard", None)
            if integration is not None
            else None
        )
        if process_guard is not None and not callable(process_guard):
            raise ValueError("invalid_browser_request_owner")
        if request_release is not None and not callable(request_release):
            raise ValueError("invalid_browser_request_owner")
        self.__abandoned_cleanup_thread = None
        self.__abandoned_cleanup_token = None
        self.__abort_requested = False
        self.__connection_offer = []
        self.__connection_complete = False
        self.__delivery_bundle = None
        self.__delivery_complete = False
        self.__delivery_offer = []
        self.__delivery_operation = None
        self.__integration = integration
        self.__issuance = object()
        self.__lock = threading.Lock()
        self.__pid = os.getpid()
        self.__process_guard = process_guard
        self.__registry_entry = [self, False]
        self.__request_complete = integration is None and request_release is None
        self.__raw_connection_offer = []
        self.__response = None
        self.__response_published = False
        self.__response_scrubbed = False
        self.__standalone = integration is None
        self.__standalone_request_release = request_release
        self.__terminal = False
        self.__thread = threading.current_thread()

    def _acquire_connection_owner(self, factory):
        self._require_before_lock()
        if not callable(factory):
            raise RuntimeError("invalid_connection")
        with self.__lock:
            if (
                self.__terminal
                or self.__connection_offer
                or self.__raw_connection_offer
                or self.__delivery_offer
                or self.__delivery_bundle is not None
            ):
                raise RuntimeError("browser_response_ownership_invalid")
            offer = self.__connection_offer
        _publish_browser_call_result(offer, factory, ())
        connection_owner = offer[0]
        if not callable(getattr(connection_owner, "close", None)):
            with self.__lock:
                if (
                    self.__connection_offer is offer
                    and len(offer) == 1
                    and offer[0] is connection_owner
                    and self.__delivery_bundle is None
                ):
                    offer.clear()
            raise RuntimeError("invalid_connection")
        register_cleanup = getattr(
            connection_owner,
            "_wahojobs_register_browser_cleanup",
            None,
        )
        if register_cleanup is not None:
            if (
                not callable(register_cleanup)
                or register_cleanup(
                    self,
                    self.__issuance,
                    _ABANDONED_BROWSER_CLEANUP_CAPABILITY,
                )
                is not True
            ):
                raise RuntimeError("browser_response_ownership_invalid")
        return connection_owner

    def _borrow_connection(self, borrower):
        self._require_before_lock()
        if not callable(borrower):
            raise RuntimeError("invalid_connection")
        with self.__lock:
            if (
                self.__terminal
                or len(self.__connection_offer) != 1
                or self.__raw_connection_offer
                or self.__delivery_offer
                or self.__delivery_bundle is not None
            ):
                raise RuntimeError("browser_response_ownership_invalid")
            connection_owner = self.__connection_offer[0]
            raw_offer = self.__raw_connection_offer
        _publish_browser_call_result(
            raw_offer,
            borrower,
            (connection_owner,),
        )
        connection = raw_offer[0]
        if connection is None:
            raise RuntimeError("invalid_connection")
        bundle = (None, connection_owner, connection)
        with self.__lock:
            if (
                self.__terminal
                or len(self.__connection_offer) != 1
                or self.__connection_offer[0] is not connection_owner
                or self.__raw_connection_offer is not raw_offer
                or len(raw_offer) != 1
                or raw_offer[0] is not connection
                or self.__delivery_bundle is not None
            ):
                raise RuntimeError("browser_response_ownership_invalid")
            self.__delivery_bundle = bundle
        return connection

    def _adopt_existing_connection(self, connection_owner, connection):
        self._require_before_lock()
        if (
            connection_owner is None
            or connection is None
            or not callable(getattr(connection_owner, "close", None))
        ):
            raise ValueError("invalid_browser_session_delivery")
        bundle = (None, connection_owner, connection)
        with self.__lock:
            if self.__terminal:
                raise RuntimeError("browser_response_delivery_already_terminal")
            if (
                not self.__connection_offer
                and not self.__raw_connection_offer
                and not self.__delivery_offer
                and self.__delivery_bundle is None
            ):
                self.__connection_offer.append(connection_owner)
                self.__raw_connection_offer.append(connection)
                self.__delivery_bundle = bundle
                return True
            existing = self.__delivery_bundle
            if (
                len(self.__connection_offer) == 1
                and self.__connection_offer[0] is connection_owner
                and len(self.__raw_connection_offer) == 1
                and self.__raw_connection_offer[0] is connection
                and existing is not None
                and existing[0] is None
                and existing[1] is connection_owner
                and existing[2] is connection
            ):
                return True
        raise ValueError("invalid_browser_session_delivery")

    def _acquire_delivery(
        self,
        factory,
        connection_owner,
        connection,
        completion,
        vault,
        *,
        now,
    ):
        self._require_before_lock()
        if not callable(factory):
            raise ValueError("invalid_browser_session_delivery")
        with self.__lock:
            bundle = self.__delivery_bundle
            if (
                self.__terminal
                or self.__delivery_offer
                or bundle is None
                or bundle[0] is not None
                or bundle[1] is not connection_owner
                or bundle[2] is not connection
            ):
                raise ValueError("invalid_browser_session_delivery")
            offer = self.__delivery_offer
        producer = functools.partial(
            factory,
            connection,
            completion,
            vault,
            now=now,
        )
        try:
            _publish_browser_call_result(offer, producer, ())
        finally:
            producer = None
        raw_lease = offer[0]
        lease = _adopt_session_delivery_lease(
            raw_lease,
            connection_owner,
            connection,
        )
        self._offer_delivery(
            lease,
            connection_owner,
            connection,
        )
        return lease

    @property
    def _issuance(self):
        return self.__issuance

    @property
    def _registry_entry(self):
        return self.__registry_entry

    @property
    def _lock(self):
        return self.__lock

    def _require_before_lock(self):
        if (
            os.getpid() != self.__pid
            or threading.current_thread() is not self.__thread
        ):
            raise RuntimeError("browser_response_authority_mismatch")
        process_guard = self.__process_guard
        if process_guard is not None:
            process_guard()
        return True

    def _require_reclaim_process_before_lock(self):
        if os.getpid() != self.__pid:
            raise RuntimeError("browser_response_authority_mismatch")
        process_guard = self.__process_guard
        if process_guard is not None:
            process_guard()
        return True

    def _offer_delivery(self, lease, connection_owner, connection):
        self._require_before_lock()
        if (
            type(lease) is not _AuthorityFencedSessionDeliveryLease
            or connection_owner is None
            or connection is None
        ):
            raise ValueError("invalid_browser_session_delivery")
        lease._response_authority(
            _DELIVERY_AUTHORITY_CAPABILITY,
            connection_owner,
        )
        raw_lease = object.__getattribute__(
            lease,
            "_AuthorityFencedSessionDeliveryLease__lease",
        )
        bundle = (lease, connection_owner, connection)
        with self.__lock:
            if self.__terminal:
                raise RuntimeError("browser_response_delivery_already_terminal")
            existing = self.__delivery_bundle
            if not self.__delivery_offer:
                self.__delivery_offer.append(raw_lease)
            elif self.__delivery_offer[0] is not raw_lease:
                raise ValueError("invalid_browser_session_delivery")
            if (
                len(self.__connection_offer) == 1
                and self.__connection_offer[0] is connection_owner
                and len(self.__raw_connection_offer) == 1
                and self.__raw_connection_offer[0] is connection
                and existing is not None
                and existing[0] is None
                and existing[1] is connection_owner
                and existing[2] is connection
            ):
                self.__delivery_bundle = bundle
                return True
            if (
                existing is not None
                and existing[0] is lease
                and existing[1] is connection_owner
                and existing[2] is connection
            ):
                return True
        raise ValueError("invalid_browser_session_delivery")

    def _owns_delivery(self, lease, connection_owner, connection):
        self._require_before_lock()
        with self.__lock:
            bundle = self.__delivery_bundle
            return lease is not None and bundle is not None and (
                bundle[0] is lease
                and bundle[1] is connection_owner
                and bundle[2] is connection
            )

    def _has_delivery_offer(self):
        self._require_before_lock()
        with self.__lock:
            return bool(self.__delivery_offer)

    def _bind_response(
        self,
        response,
        lease,
        connection_owner,
        authority,
        request_release,
    ):
        self._require_before_lock()
        _require_delivery_authority_before_lock(authority)
        with self.__lock:
            bundle = self.__delivery_bundle
            if (
                self.__terminal
                or bundle is None
                or bundle[0] is not lease
                or bundle[1] is not connection_owner
                or (
                    not self.__standalone
                    and request_release is not self
                )
            ):
                raise ValueError("invalid_browser_session_delivery")
            if self.__response is None:
                self.__response = response
            elif self.__response is not response:
                raise ValueError("invalid_browser_session_delivery")
            object.__setattr__(response, "_delivery_lock", self.__lock)
        return True

    def _complete_handle(self, response):
        self._require_before_lock()
        with self.__lock:
            bundle = self.__delivery_bundle
            has_connection_owner = bool(self.__connection_offer)
            bound_response = self.__response
            terminal = self.__terminal
        if bundle is None and not has_connection_owner:
            return self._finish_without_delivery()
        if terminal:
            return True
        if bound_response is response:
            with self.__lock:
                if (
                    not self.__terminal
                    and self.__response is response
                ):
                    self.__response_published = True
            return True
        self._request_abort()
        with self.__lock:
            if not self.__terminal:
                raise RuntimeError("browser_response_publication_incomplete")
        return True

    def _finish_without_delivery(self):
        self._require_before_lock()
        with self.__lock:
            if (
                self.__connection_offer
                or self.__raw_connection_offer
                or self.__delivery_offer
                or self.__delivery_bundle is not None
            ):
                raise RuntimeError("browser_response_delivery_invalid")
            self.__connection_complete = True
            self.__delivery_complete = True
        return self._drive_cleanup()

    def _request_abort(self):
        self._require_before_lock()
        with self.__lock:
            self.__abort_requested = True
            bundle = self.__delivery_bundle
            has_connection_owner = bool(self.__connection_offer)
            has_delivery_offer = bool(self.__delivery_offer)
            if self.__delivery_operation is None and has_connection_owner:
                if has_delivery_offer or (
                    bundle is not None and bundle[0] is not None
                ):
                    self.__delivery_operation = "fail_delivery"
                else:
                    self.__delivery_complete = True
        if bundle is None and not has_connection_owner:
            return self._finish_without_delivery()
        return self._drive_cleanup()

    def _shutdown_cleanup(self):
        self._require_before_lock()
        with self.__lock:
            if (
                self.__response_published
                and not self.__abort_requested
                and not self.__delivery_complete
            ):
                return False
        return self._request_abort()

    def _terminalize(self, response, operation):
        self._require_before_lock()
        if operation not in {"acknowledge_delivery", "fail_delivery"}:
            raise ValueError("invalid_browser_session_delivery")
        with self.__lock:
            already_complete = self.__delivery_complete
            if self.__terminal:
                terminal = True
            else:
                if self.__response is not response:
                    raise RuntimeError("browser_response_delivery_invalid")
                terminal = False
                if self.__delivery_operation is None:
                    self.__delivery_operation = operation
                elif (
                    self.__delivery_operation != operation
                    and not self.__delivery_complete
                ):
                    raise RuntimeError(
                        "browser_response_delivery_already_terminal"
                    )
        if terminal:
            self._prune_terminal()
            raise RuntimeError("browser_response_delivery_already_terminal")
        self._drive_cleanup()
        if already_complete:
            raise RuntimeError("browser_response_delivery_already_terminal")
        return None

    def _drive_cleanup(self):
        primary = None
        try:
            self._advance_delivery()
        except BaseException as exc:
            primary = exc
            exc = None
        delivery_complete = False
        try:
            delivery_complete = self._delivery_is_complete()
        except BaseException as exc:
            if primary is None:
                primary = exc
            else:
                _detach_browser_exception(exc)
            exc = None
        if delivery_complete:
            try:
                self._publish_delivery_terminal()
            except BaseException as exc:
                if primary is None:
                    primary = exc
                else:
                    _detach_browser_exception(exc)
                exc = None
            try:
                self._advance_connection_close()
            except BaseException as exc:
                if primary is None:
                    primary = exc
                else:
                    _detach_browser_exception(exc)
                exc = None
        connection_complete = False
        try:
            connection_complete = self._connection_is_complete()
        except BaseException as exc:
            if primary is None:
                primary = exc
            else:
                _detach_browser_exception(exc)
            exc = None
        if delivery_complete and connection_complete:
            try:
                self._advance_request_release()
            except BaseException as exc:
                if primary is None:
                    primary = exc
                else:
                    _detach_browser_exception(exc)
                exc = None
        try:
            self._retire_if_complete()
        except BaseException as exc:
            if primary is None:
                primary = exc
            else:
                _detach_browser_exception(exc)
            exc = None
        if primary is not None:
            propagated = primary
            primary = None
            raise propagated from None
        with self.__lock:
            return self.__terminal

    def _advance_delivery(self):
        self._require_before_lock()
        with self.__lock:
            if self.__delivery_complete:
                return self.__delivery_complete
            bundle = self.__delivery_bundle
            if bundle is None and not self.__delivery_offer:
                return False
            lease = bundle[0] if bundle is not None else None
            connection_owner = (
                bundle[1]
                if bundle is not None
                else self.__connection_offer[0]
            )
            if lease is None and self.__delivery_offer:
                lease = self.__delivery_offer[0]
            operation = self.__delivery_operation
            abort_requested = self.__abort_requested
        if lease is None:
            if not abort_requested:
                return False
            with self.__lock:
                if (
                    self.__delivery_bundle is bundle
                    and self.__abort_requested
                ):
                    self.__delivery_complete = True
            return True
        if operation is None:
            return False
        status_supported = True
        try:
            status = lease.status
        except AttributeError:
            status = None
            status_supported = False
        if status in {"acknowledged", "failed"}:
            with self.__lock:
                if (
                    self.__delivery_bundle is bundle
                    and self.__delivery_offer
                ):
                    self.__delivery_complete = True
            return True
        if type(lease) is _AuthorityFencedSessionDeliveryLease:
            prepared = lease._prepare_response_operation(
                _DELIVERY_AUTHORITY_CAPABILITY,
                connection_owner,
                operation,
            )
        else:
            prepared = getattr(lease, operation, None)
            if not callable(prepared):
                raise RuntimeError("browser_response_delivery_invalid")
        action_error = None
        try:
            prepared()
        except BaseException as exc:
            action_error = exc
            exc = None
        terminal = (
            not status_supported
            and (
                action_error is None
                or (
                    self.__standalone
                    and type(lease) is _AuthorityFencedSessionDeliveryLease
                )
            )
        )
        if status_supported:
            try:
                terminal = lease.status in {"acknowledged", "failed"}
            except BaseException as exc:
                if action_error is None:
                    action_error = exc
                else:
                    _detach_browser_exception(exc)
                exc = None
        if terminal:
            with self.__lock:
                if self.__delivery_bundle is bundle:
                    self.__delivery_complete = True
        if action_error is not None:
            propagated = action_error
            action_error = None
            raise propagated from None
        if not terminal:
            raise RuntimeError("browser_response_delivery_incomplete")
        return True

    def _publish_delivery_terminal(self):
        self._require_before_lock()
        with self.__lock:
            if not self.__delivery_complete:
                return False
            response = self.__response
            if response is None or self.__response_scrubbed:
                return True
            self.__response_scrubbed = True
            self.__response._delivery_state[0] = "complete"
            object.__setattr__(self.__response, "headers", ())
        return True

    def _advance_connection_close(self):
        self._require_before_lock()
        with self.__lock:
            if not self.__delivery_complete or self.__connection_complete:
                return self.__connection_complete
            bundle = self.__delivery_bundle
            if len(self.__connection_offer) != 1:
                raise RuntimeError("browser_response_connection_owner_invalid")
            connection_owner = self.__connection_offer[0]
            if (
                bundle is not None
                and bundle[1] is not connection_owner
            ):
                raise RuntimeError("browser_response_connection_owner_invalid")
        close = getattr(connection_owner, "close", None)
        if not callable(close):
            raise RuntimeError("browser_response_connection_owner_invalid")
        action_error = None
        result = False
        try:
            result = close()
        except BaseException as exc:
            action_error = exc
            exc = None
        closed = False
        try:
            closed_value = getattr(connection_owner, "closed", None)
            closed = closed_value is True or (
                action_error is None and result is not False
            )
        except BaseException as exc:
            if action_error is None:
                action_error = exc
            else:
                _detach_browser_exception(exc)
            exc = None
        if closed:
            with self.__lock:
                if (
                    len(self.__connection_offer) == 1
                    and self.__connection_offer[0] is connection_owner
                    and self.__delivery_bundle is bundle
                ):
                    self.__connection_complete = True
        if action_error is not None:
            propagated = action_error
            action_error = None
            raise propagated from None
        return closed

    def _advance_request_release(self):
        self._require_before_lock()
        with self.__lock:
            if (
                not self.__delivery_complete
                or not self.__connection_complete
                or self.__request_complete
            ):
                return self.__request_complete
            integration = self.__integration
        action_error = None
        released = False
        if self.__standalone:
            request_release = self.__standalone_request_release
            if request_release is None:
                released = True
            else:
                try:
                    result = request_release()
                    released = result is not False
                except BaseException as exc:
                    action_error = exc
                    exc = None
        else:
            try:
                released = integration._release_request_owner(self)
            except BaseException as exc:
                action_error = exc
                exc = None
            try:
                released = (
                    integration._request_owner_released(self) or released
                )
            except BaseException as exc:
                if action_error is None:
                    action_error = exc
                else:
                    _detach_browser_exception(exc)
                exc = None
        if released:
            with self.__lock:
                self.__request_complete = True
        if action_error is not None:
            propagated = action_error
            action_error = None
            raise propagated from None
        return released

    def _retire_if_complete(self):
        self._require_before_lock()
        with self.__lock:
            if self.__terminal:
                terminal = True
            elif not (
                self.__delivery_complete
                and self.__connection_complete
                and self.__request_complete
            ):
                return False
            else:
                response = self.__response
                if response is not None:
                    response._delivery_state[0] = "complete"
                    object.__setattr__(response, "headers", ())
                    object.__setattr__(response, "_delivery_lease", None)
                    object.__setattr__(response, "_owned_connection", None)
                    object.__setattr__(response, "_request_release", None)
                self.__terminal = True
                terminal = True
        if terminal:
            self._prune_terminal()
        return True

    def _prune_terminal(self):
        self._require_reclaim_process_before_lock()
        with self.__lock:
            if not self.__terminal:
                return False
            integration = self.__integration
            standalone = self.__standalone
            process_guard = self.__process_guard
            response = self.__response
            if response is not None:
                object.__setattr__(response, "_delivery_owner", None)
            self.__connection_offer.clear()
            self.__raw_connection_offer.clear()
            self.__delivery_offer.clear()
            self.__delivery_bundle = None
            self.__delivery_operation = None
            self.__process_guard = None
            self.__response = None
            self.__response_published = False
            self.__standalone_request_release = None
            self.__integration = None
            self.__abandoned_cleanup_thread = None
            self.__abandoned_cleanup_token = None
        if not standalone and integration is not None:
            try:
                if os.getpid() != self.__pid:
                    raise RuntimeError("browser_response_authority_mismatch")
                if process_guard is not None:
                    process_guard()
                if not integration._prune_request_owner(self):
                    return False
            finally:
                process_guard = None
        else:
            process_guard = None
        return True

    def _reclaim_abandoned_empty_request(self):
        if os.getpid() != self.__pid:
            return False
        self._require_reclaim_process_before_lock()
        if self.__thread.is_alive():
            return False
        self._require_reclaim_process_before_lock()
        with self.__lock:
            if self.__terminal:
                terminal = True
                integration = None
            elif (
                self.__connection_offer
                or self.__raw_connection_offer
                or self.__delivery_offer
                or self.__delivery_bundle is not None
                or self.__response is not None
            ):
                return False
            else:
                terminal = False
                integration = self.__integration
        if terminal:
            return self._prune_terminal()
        self._require_reclaim_process_before_lock()
        if integration is None or not integration._release_request_owner(self):
            return False
        self._require_reclaim_process_before_lock()
        with self.__lock:
            if (
                self.__terminal
                or self.__connection_offer
                or self.__raw_connection_offer
                or self.__delivery_offer
                or self.__delivery_bundle is not None
                or self.__response is not None
                or self.__thread.is_alive()
            ):
                return False
            self.__connection_complete = True
            self.__delivery_complete = True
            self.__request_complete = True
            self.__terminal = True
        return self._prune_terminal()

    def _reclaim_abandoned_request(self):
        if os.getpid() != self.__pid:
            return False
        self._require_reclaim_process_before_lock()
        primary = None
        result = False
        caller = threading.current_thread()
        cleanup_token = None
        try:
            result = self._reclaim_abandoned_request_attempt()
        except BaseException as exc:
            primary = exc
            exc = None
        connection_owner = None
        try:
            self._require_reclaim_process_before_lock()
            with self.__lock:
                if (
                    self.__abandoned_cleanup_thread is caller
                    and self.__abandoned_cleanup_token is not None
                ):
                    cleanup_token = self.__abandoned_cleanup_token
                if cleanup_token is not None and self.__connection_offer:
                    if len(self.__connection_offer) != 1:
                        raise RuntimeError(
                            "browser_response_connection_owner_invalid"
                        )
                    connection_owner = self.__connection_offer[0]
            if connection_owner is not None:
                self._require_reclaim_process_before_lock()
                abandon = getattr(
                    connection_owner,
                    "_wahojobs_abandon_abandoned_browser_cleanup",
                    None,
                )
                if callable(abandon):
                    self._require_reclaim_process_before_lock()
                    abandon(
                        self,
                        self.__issuance,
                        _ABANDONED_BROWSER_CLEANUP_CAPABILITY,
                    )
        except BaseException as exc:
            if primary is None:
                primary = exc
            else:
                _detach_browser_exception(exc)
            exc = None
        finally:
            reclaim_authorized = False
            try:
                reclaim_authorized = (
                    self._require_reclaim_process_before_lock()
                )
            except BaseException as exc:
                if primary is None:
                    primary = exc
                else:
                    _detach_browser_exception(exc)
                exc = None
            if reclaim_authorized:
                with self.__lock:
                    if (
                        cleanup_token is not None
                        and self.__abandoned_cleanup_thread is caller
                        and self.__abandoned_cleanup_token is cleanup_token
                    ):
                        try:
                            self.__abandoned_cleanup_thread = None
                        finally:
                            if (
                                self.__abandoned_cleanup_thread is caller
                                or self.__abandoned_cleanup_token
                                is cleanup_token
                            ):
                                self.__abandoned_cleanup_thread = None
                                self.__abandoned_cleanup_token = None
        connection_owner = None
        cleanup_token = None
        caller = None
        if primary is not None:
            propagated = primary
            primary = None
            _detach_browser_exception(propagated)
            raise propagated from None
        return result

    def _reclaim_abandoned_request_attempt(self):
        if os.getpid() != self.__pid:
            return False
        self._require_reclaim_process_before_lock()
        if self.__thread.is_alive():
            return False
        caller = threading.current_thread()
        self._require_reclaim_process_before_lock()
        with self.__lock:
            if self.__terminal:
                terminal = True
                cleanup_token = None
            else:
                terminal = False
                cleanup_thread = self.__abandoned_cleanup_thread
                if (
                    cleanup_thread is not None
                    and cleanup_thread is not caller
                    and cleanup_thread.is_alive()
                ):
                    return False
                if (
                    cleanup_thread is caller
                    and self.__abandoned_cleanup_token is not None
                ):
                    cleanup_token = self.__abandoned_cleanup_token
                else:
                    cleanup_token = object()
                    self.__abandoned_cleanup_thread = caller
                    self.__abandoned_cleanup_token = cleanup_token
                self.__abort_requested = True
                if (
                    self.__delivery_operation is None
                    and self.__delivery_offer
                ):
                    self.__delivery_operation = "fail_delivery"
        if terminal:
            self._require_reclaim_process_before_lock()
            return self._prune_terminal()

        primary = None
        connection_owner = None
        connection = None
        raw_lease = None
        bundle = None
        database_claim = None
        delivery_complete = False
        connection_complete = False
        request_complete = False

        try:
            self._require_reclaim_process_before_lock()
            with self.__lock:
                if (
                    self.__terminal
                    or self.__abandoned_cleanup_thread is not caller
                    or self.__abandoned_cleanup_token
                    is not cleanup_token
                    or self.__thread.is_alive()
                ):
                    return False
                bundle = self.__delivery_bundle
                if len(self.__connection_offer) > 1:
                    raise RuntimeError(
                        "browser_response_connection_owner_invalid"
                    )
                if len(self.__raw_connection_offer) > 1:
                    raise RuntimeError(
                        "browser_response_connection_owner_invalid"
                    )
                if len(self.__delivery_offer) > 1:
                    raise RuntimeError(
                        "browser_response_delivery_invalid"
                    )
                connection_owner = (
                    self.__connection_offer[0]
                    if self.__connection_offer
                    else None
                )
                connection = (
                    self.__raw_connection_offer[0]
                    if self.__raw_connection_offer
                    else None
                )
                raw_lease = (
                    self.__delivery_offer[0]
                    if self.__delivery_offer
                    else None
                )
                if bundle is not None and (
                    bundle[1] is not connection_owner
                    or bundle[2] is not connection
                ):
                    raise RuntimeError(
                        "browser_response_ownership_invalid"
                    )
                delivery_operation = self.__delivery_operation
                delivery_complete = self.__delivery_complete
                connection_complete = self.__connection_complete
                request_complete = self.__request_complete

            if connection_owner is None:
                if connection is not None or raw_lease is not None:
                    raise RuntimeError(
                        "browser_response_ownership_invalid"
                    )
                delivery_complete = True
                connection_complete = True
            else:
                self._require_reclaim_process_before_lock()
                claim_cleanup = getattr(
                    connection_owner,
                    "_wahojobs_claim_abandoned_browser_cleanup",
                    None,
                )
                finish_cleanup = getattr(
                    connection_owner,
                    "_wahojobs_finish_abandoned_browser_cleanup",
                    None,
                )
                acknowledge_cleanup = getattr(
                    connection_owner,
                    "_wahojobs_acknowledge_abandoned_browser_cleanup",
                    None,
                )
                relinquish_cleanup = getattr(
                    connection_owner,
                    (
                        "_wahojobs_relinquish_unregistered_"
                        "browser_cleanup"
                    ),
                    None,
                )
                cleanup_is_closed = getattr(
                    connection_owner,
                    "_wahojobs_browser_cleanup_is_closed",
                    None,
                )
                if not all(
                    callable(candidate)
                    for candidate in (
                        claim_cleanup,
                        finish_cleanup,
                        acknowledge_cleanup,
                        relinquish_cleanup,
                        cleanup_is_closed,
                    )
                ):
                    return False

                manager_handoff = False
                if raw_lease is None:
                    self._require_reclaim_process_before_lock()
                    manager_handoff = relinquish_cleanup(
                        self,
                        self.__issuance,
                        _ABANDONED_BROWSER_CLEANUP_CAPABILITY,
                    )
                    if manager_handoff:
                        delivery_complete = True
                        self._require_reclaim_process_before_lock()
                        connection_complete = (
                            cleanup_is_closed(
                                self.__issuance,
                                _ABANDONED_BROWSER_CLEANUP_CAPABILITY,
                            )
                            is True
                        )
                if not manager_handoff:
                    self._require_reclaim_process_before_lock()
                    database_claim = claim_cleanup(
                        self,
                        self.__issuance,
                        _ABANDONED_BROWSER_CLEANUP_CAPABILITY,
                        connection,
                    )
                    if database_claim is False:
                        return False

                if raw_lease is None:
                    delivery_complete = True
                elif not delivery_complete:
                    self._require_reclaim_process_before_lock()
                    status = getattr(raw_lease, "status")
                    if status not in {"acknowledged", "failed"}:
                        operation = (
                            delivery_operation
                            if delivery_operation
                            in {
                                "acknowledge_delivery",
                                "fail_delivery",
                            }
                            else "fail_delivery"
                        )
                        action = getattr(raw_lease, operation, None)
                        if not callable(action):
                            raise RuntimeError(
                                "browser_response_delivery_invalid"
                            )
                        try:
                            self._require_reclaim_process_before_lock()
                            action()
                        except BaseException as exc:
                            primary = exc
                            exc = None
                    try:
                        self._require_reclaim_process_before_lock()
                        delivery_complete = (
                            getattr(raw_lease, "status")
                            in {"acknowledged", "failed"}
                        )
                    except BaseException as exc:
                        if primary is None:
                            primary = exc
                        else:
                            _detach_browser_exception(exc)
                        exc = None

                if (
                    delivery_complete
                    and not connection_complete
                    and not manager_handoff
                ):
                    try:
                        if database_claim is True:
                            connection_complete = True
                        else:
                            self._require_reclaim_process_before_lock()
                            connection_complete = (
                                finish_cleanup(
                                    self,
                                    self.__issuance,
                                    _ABANDONED_BROWSER_CLEANUP_CAPABILITY,
                                    database_claim,
                                )
                                is True
                            )
                    except BaseException as exc:
                        if primary is None:
                            primary = exc
                        else:
                            _detach_browser_exception(exc)
                        exc = None
                    if connection_complete:
                        connection_complete = False
                        try:
                            self._require_reclaim_process_before_lock()
                            connection_complete = (
                                acknowledge_cleanup(
                                    self,
                                    self.__issuance,
                                    _ABANDONED_BROWSER_CLEANUP_CAPABILITY,
                                )
                                is True
                            )
                        except BaseException as exc:
                            try:
                                self._require_reclaim_process_before_lock()
                                connection_complete = (
                                    cleanup_is_closed(
                                        self.__issuance,
                                        (
                                            _ABANDONED_BROWSER_CLEANUP_CAPABILITY
                                        ),
                                    )
                                    is True
                                )
                            except BaseException as probe:
                                _detach_browser_exception(probe)
                                probe = None
                            if primary is None:
                                primary = exc
                            else:
                                _detach_browser_exception(exc)
                            exc = None

            self._require_reclaim_process_before_lock()
            with self.__lock:
                if (
                    self.__abandoned_cleanup_thread is not caller
                    or self.__abandoned_cleanup_token
                    is not cleanup_token
                    or len(self.__connection_offer)
                    != (1 if connection_owner is not None else 0)
                    or (
                        connection_owner is not None
                        and self.__connection_offer[0]
                        is not connection_owner
                    )
                    or len(self.__raw_connection_offer)
                    != (1 if connection is not None else 0)
                    or (
                        connection is not None
                        and self.__raw_connection_offer[0] is not connection
                    )
                    or len(self.__delivery_offer)
                    != (1 if raw_lease is not None else 0)
                    or (
                        raw_lease is not None
                        and self.__delivery_offer[0] is not raw_lease
                    )
                    or self.__delivery_bundle is not bundle
                ):
                    raise RuntimeError(
                        "browser_response_ownership_invalid"
                    )
                if delivery_complete:
                    self.__delivery_complete = True
                    response = self.__response
                    if response is not None and not self.__response_scrubbed:
                        self.__response_scrubbed = True
                        response._delivery_state[0] = "complete"
                        object.__setattr__(response, "headers", ())
                if connection_complete:
                    self.__connection_complete = True

            if delivery_complete and connection_complete:
                integration = self.__integration
                if integration is None:
                    raise RuntimeError("browser_request_owner_invalid")
                try:
                    self._require_reclaim_process_before_lock()
                    request_complete = (
                        integration._release_request_owner(self)
                        or integration._request_owner_released(self)
                    )
                except BaseException as exc:
                    if primary is None:
                        primary = exc
                    else:
                        _detach_browser_exception(exc)
                    exc = None
                    try:
                        self._require_reclaim_process_before_lock()
                        request_complete = (
                            integration._request_owner_released(self)
                        )
                    except BaseException as probe:
                        _detach_browser_exception(probe)
                        probe = None
                if request_complete:
                    self._require_reclaim_process_before_lock()
                    with self.__lock:
                        if (
                            self.__abandoned_cleanup_thread is caller
                            and self.__abandoned_cleanup_token
                            is cleanup_token
                        ):
                            self.__request_complete = True

            self._require_reclaim_process_before_lock()
            with self.__lock:
                if (
                    self.__abandoned_cleanup_thread is caller
                    and self.__abandoned_cleanup_token
                    is cleanup_token
                    and self.__delivery_complete
                    and self.__connection_complete
                    and self.__request_complete
                ):
                    response = self.__response
                    if response is not None:
                        response._delivery_state[0] = "complete"
                        object.__setattr__(response, "headers", ())
                        object.__setattr__(
                            response,
                            "_delivery_lease",
                            None,
                        )
                        object.__setattr__(
                            response,
                            "_owned_connection",
                            None,
                        )
                        object.__setattr__(
                            response,
                            "_request_release",
                            None,
                        )
                    self.__terminal = True
        except BaseException as exc:
            if primary is None:
                primary = exc
            else:
                _detach_browser_exception(exc)
            exc = None

        terminal = False
        reclaim_authorized = False
        try:
            reclaim_authorized = self._require_reclaim_process_before_lock()
        except BaseException as exc:
            if primary is None:
                primary = exc
            else:
                _detach_browser_exception(exc)
            exc = None
        if reclaim_authorized:
            try:
                terminal = self._is_terminal()
            except BaseException as exc:
                if primary is None:
                    primary = exc
                else:
                    _detach_browser_exception(exc)
                exc = None
        if terminal:
            try:
                self._require_reclaim_process_before_lock()
                terminal = self._prune_terminal()
            except BaseException as exc:
                if primary is None:
                    primary = exc
                else:
                    _detach_browser_exception(exc)
                exc = None
        if primary is not None:
            propagated = primary
            primary = None
            _detach_browser_exception(propagated)
            raise propagated from None
        return terminal

    def _delivery_is_complete(self):
        with self.__lock:
            return self.__delivery_complete

    def _connection_is_complete(self):
        with self.__lock:
            return self.__connection_complete

    def _is_terminal(self):
        with self.__lock:
            return self.__terminal

    def _is_current_thread(self):
        return (
            os.getpid() == self.__pid
            and threading.current_thread() is self.__thread
        )

    def __call__(self):
        self._require_before_lock()
        integration = self.__integration
        if self.__standalone:
            request_release = self.__standalone_request_release
            if request_release is None:
                return True
            return request_release()
        return integration._release_request_owner(self)

    def __repr__(self):
        return "_BrowserRequestDeliveryOwner(<sealed>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("browser_request_delivery_owner_not_serializable")


@dataclass(frozen=True, slots=True, repr=False)
class DurableGoogleLoginBrowserResponse:
    """Bounded browser response with optional one-shot delivery callbacks."""

    status: int
    body: bytes = field(repr=False)
    headers: tuple[tuple[str, str], ...]
    _delivery_lease: object | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _owned_connection: object | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _request_release: object | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _delivery_owner: object | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _delivery_authority: object | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    _delivery_lock: threading.Lock = field(
        default_factory=threading.Lock,
        repr=False,
        compare=False,
    )
    _delivery_state: list[str] = field(
        default_factory=lambda: ["pending"],
        repr=False,
        compare=False,
    )

    def __post_init__(self):
        if (
            type(self.status) is not int
            or not 100 <= self.status <= 599
            or type(self.body) is not bytes
            or len(self.body) > MAX_BROWSER_AUTH_RESPONSE_BYTES
            or type(self.headers) is not tuple
        ):
            raise ValueError("invalid_durable_google_login_browser_response")
        total = 0
        for item in self.headers:
            if type(item) is not tuple or len(item) != 2:
                raise ValueError("invalid_durable_google_login_browser_response")
            name, value = item
            if (
                type(name) is not str
                or _HTTP_TOKEN.fullmatch(name) is None
                or type(value) is not str
                or _HEADER_VALUE_FORBIDDEN.search(value) is not None
            ):
                raise ValueError("invalid_durable_google_login_browser_response")
            try:
                total += len(name.encode("ascii")) + len(value.encode("latin-1"))
            except UnicodeError:
                raise ValueError(
                    "invalid_durable_google_login_browser_response"
                ) from None
        if total > 16_384:
            raise ValueError("invalid_durable_google_login_browser_response")
        lease = self._delivery_lease
        connection = self._owned_connection
        request_release = self._request_release
        if (lease is None) != (connection is None):
            raise ValueError("invalid_durable_google_login_browser_response")
        if request_release is not None and not callable(request_release):
            raise ValueError("invalid_durable_google_login_browser_response")
        if lease is not None and not all(
            callable(getattr(lease, name, None))
            for name in ("acknowledge_delivery", "fail_delivery")
        ):
            raise ValueError("invalid_durable_google_login_browser_response")
        if lease is None:
            authority = _LocalDeliveryAuthority()
        elif type(lease) is _AuthorityFencedSessionDeliveryLease:
            authority = lease._response_authority(
                _DELIVERY_AUTHORITY_CAPABILITY,
                connection,
            )
        else:
            lease = _AuthorityFencedSessionDeliveryLease(
                lease,
                connection,
                None,
            )
            authority = lease._response_authority(
                _DELIVERY_AUTHORITY_CAPABILITY,
                connection,
            )
            object.__setattr__(self, "_delivery_lease", lease)
        _require_delivery_authority_before_lock(authority)
        object.__setattr__(self, "_delivery_authority", authority)
        delivery_owner = self._delivery_owner
        if lease is not None:
            if delivery_owner is None:
                delivery_owner = _BrowserRequestDeliveryOwner(
                    request_release=request_release,
                )
                retained_connection = object.__getattribute__(
                    lease,
                    "_AuthorityFencedSessionDeliveryLease__connection",
                )
                delivery_owner._adopt_existing_connection(
                    connection,
                    (
                        retained_connection
                        if retained_connection is not None
                        else connection
                    ),
                )
                delivery_owner._offer_delivery(
                    lease,
                    connection,
                    (
                        retained_connection
                        if retained_connection is not None
                        else connection
                    ),
                )
                object.__setattr__(self, "_delivery_owner", delivery_owner)
            if type(delivery_owner) is not _BrowserRequestDeliveryOwner:
                raise ValueError("invalid_browser_session_delivery")
            delivery_owner._bind_response(
                self,
                lease,
                connection,
                authority,
                request_release,
            )

    def acknowledge_delivery(self):
        self._terminalize_delivery("acknowledge_delivery")

    def fail_delivery(self):
        self._terminalize_delivery("fail_delivery")

    def _terminalize_delivery(self, operation):
        _require_delivery_authority_before_lock(self._delivery_authority)
        owner = self._delivery_owner
        if owner is None and self._delivery_lease is None:
            with self._delivery_lock:
                if self._delivery_state[0] != "pending":
                    raise RuntimeError(
                        "browser_response_delivery_already_terminal"
                    )
                self._delivery_state[0] = "complete"
                object.__setattr__(self, "headers", ())
                return None
        if type(owner) is not _BrowserRequestDeliveryOwner:
            raise RuntimeError("browser_response_delivery_invalid")
        return owner._terminalize(self, operation)

    def __repr__(self) -> str:
        return (
            "DurableGoogleLoginBrowserResponse("
            f"status={self.status}, body=<redacted>, header_count={len(self.headers)}, "
            f"delivery_pending={self._delivery_state[0] == 'pending' and self._delivery_lease is not None})"
        )


class DurableGoogleLoginBrowserIntegration:
    """Own the explicit durable-login and authenticated account routes."""

    __slots__ = (
        "_public_origin",
        "_public_authority",
        "_callback_url",
        "_profile_integration",
        "_connection_factory",
        "_connection_borrower",
        "_gateway",
        "_key_authority",
        "_completion_policy",
        "_request_secret_vault_factory",
        "_prepare_session_delivery",
        "_discard_request_secret_vault",
        "_validate_logout",
        "_revoke_logout",
        "_prepare_authorization",
        "_complete_authorization",
        "_now",
        "_token_factory",
        "_process_guard",
        "_lifecycle_condition",
        "_accepting_requests",
        "_request_owners",
        "_closed",
    )

    def __init__(
        self,
        *,
        public_origin,
        profile_integration,
        connection_factory,
        gateway,
        key_authority,
        completion_policy,
        request_secret_vault_factory,
        prepare_session_delivery,
        discard_request_secret_vault,
        validate_logout,
        revoke_logout,
        connection_borrower=None,
        prepare_authorization=None,
        complete_authorization=None,
        now=None,
        token_factory=None,
        process_guard=None,
    ):
        origin, authority = _validated_public_origin(public_origin)
        if not all(
            callable(getattr(profile_integration, name, None))
            for name in ("matches_route", "handle")
        ) or profile_integration.matches_route(PERSISTENT_PROFILE_ROUTE) is not True:
            raise ValueError("invalid_durable_google_login_browser_configuration")
        callables = (
            connection_factory,
            request_secret_vault_factory,
            prepare_session_delivery,
            discard_request_secret_vault,
            validate_logout,
            revoke_logout,
        )
        if not all(callable(value) for value in callables):
            raise ValueError("invalid_durable_google_login_browser_configuration")
        if connection_borrower is None:
            connection_borrower = _borrow_database_connection
        if not callable(connection_borrower):
            raise ValueError("invalid_durable_google_login_browser_configuration")
        if prepare_authorization is None:
            from wahojobs.google_oidc_durable_gateway import (
                prepare_durable_google_oidc_authorization,
            )

            prepare_authorization = prepare_durable_google_oidc_authorization
        if complete_authorization is None:
            from wahojobs.google_oidc_durable_gateway import (
                complete_browser_bound_durable_google_oidc_authorization,
            )

            complete_authorization = (
                complete_browser_bound_durable_google_oidc_authorization
            )
        if not callable(prepare_authorization) or not callable(complete_authorization):
            raise ValueError("invalid_durable_google_login_browser_configuration")
        if now is None:
            now = lambda: datetime.now(timezone.utc)
        if token_factory is None:
            token_factory = lambda: secrets.token_urlsafe(32)
        if (
            not callable(now)
            or not callable(token_factory)
            or (
                process_guard is not None
                and not callable(process_guard)
            )
        ):
            raise ValueError("invalid_durable_google_login_browser_configuration")

        self._public_origin = origin
        self._public_authority = authority
        self._callback_url = origin + GOOGLE_LOGIN_CALLBACK_ROUTE
        self._profile_integration = profile_integration
        self._connection_factory = connection_factory
        self._connection_borrower = connection_borrower
        self._gateway = gateway
        self._key_authority = key_authority
        self._completion_policy = completion_policy
        self._request_secret_vault_factory = request_secret_vault_factory
        self._prepare_session_delivery = prepare_session_delivery
        self._discard_request_secret_vault = discard_request_secret_vault
        self._validate_logout = validate_logout
        self._revoke_logout = revoke_logout
        self._prepare_authorization = prepare_authorization
        self._complete_authorization = complete_authorization
        self._now = now
        self._token_factory = token_factory
        self._process_guard = process_guard
        self._lifecycle_condition = threading.Condition(threading.Lock())
        self._accepting_requests = True
        self._request_owners = {}
        self._closed = False

    def matches_route(self, path: str) -> bool:
        self._require_current_process()
        with self._lifecycle_condition:
            return self._accepting_requests and path in _AUTH_ROUTES

    def issue_confirmed_profile_artifact(self, **kwargs):
        """Private server composition hook; it is not an HTTP route."""
        self._require_current_process()
        with self._lifecycle_condition:
            if not self._accepting_requests or self._profile_integration is None:
                raise RuntimeError("profile_confirmation_unavailable")
            profile_integration = self._profile_integration
        issue = getattr(profile_integration, "issue_confirmed_artifact", None)
        if not callable(issue):
            raise RuntimeError("profile_confirmation_unavailable")
        return issue(**kwargs)

    def authenticate_completed_profile_replay(self, **kwargs):
        """Private lookup-only composition hook for a cached confirmation."""
        self._require_current_process()
        with self._lifecycle_condition:
            if not self._accepting_requests or self._profile_integration is None:
                return False
            profile_integration = self._profile_integration
        authenticate = getattr(
            profile_integration,
            "authenticate_completed_profile_replay",
            None,
        )
        if not callable(authenticate):
            return False
        return authenticate(**kwargs) is True

    def handle(self, method, target, headers, body_stream=None):
        self._require_current_process()
        request_owner = _BrowserRequestDeliveryOwner(self)
        try:
            try:
                if not self._register_request_owner(request_owner):
                    return _failure_response(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "Request unavailable",
                        "This sign-in request is not available.",
                    )
                response = self._handle(
                    method,
                    target,
                    headers,
                    body_stream,
                    request_owner,
                )
                request_owner._complete_handle(response)
                return response
            except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
                propagated = exc
                _abort_browser_request_owner_preserving_primary(request_owner)
                method = None
                target = None
                headers = None
                body_stream = None
                response = None
                exc = None
                _detach_browser_exception(propagated)
                raise propagated from None
            except Exception as exc:
                propagated = exc
                _abort_browser_request_owner_preserving_primary(request_owner)
                method = None
                target = None
                headers = None
                body_stream = None
                response = None
                exc = None
                _detach_browser_exception(propagated)
                raise propagated from None
        finally:
            request_owner = None
            self = None

    def _register_request_owner(self, owner):
        self._require_current_process()
        if type(owner) is not _BrowserRequestDeliveryOwner:
            raise RuntimeError("browser_request_owner_invalid")
        with self._lifecycle_condition:
            if not self._accepting_requests:
                return False
            entry = owner._registry_entry
            if owner._issuance in self._request_owners:
                raise RuntimeError("browser_request_owner_invalid")
            self._request_owners[owner._issuance] = entry
            return True

    def _release_request_owner(self, owner):
        self._require_current_process()
        if type(owner) is not _BrowserRequestDeliveryOwner:
            raise RuntimeError("browser_request_owner_invalid")
        condition = self._lifecycle_condition
        with condition:
            entry = self._request_owners.get(owner._issuance)
            if entry is not owner._registry_entry or entry[0] is not owner:
                return False
            entry[1] = True
            condition.notify_all()
        condition = None
        return True

    def _request_owner_released(self, owner):
        self._require_current_process()
        if type(owner) is not _BrowserRequestDeliveryOwner:
            raise RuntimeError("browser_request_owner_invalid")
        with self._lifecycle_condition:
            entry = self._request_owners.get(owner._issuance)
            return (
                entry is owner._registry_entry
                and entry[0] is owner
                and entry[1] is True
            )

    def _prune_request_owner(self, owner):
        self._require_current_process()
        if type(owner) is not _BrowserRequestDeliveryOwner:
            raise RuntimeError("browser_request_owner_invalid")
        condition = self._lifecycle_condition
        with condition:
            entry = self._request_owners.get(owner._issuance)
            if (
                entry is owner._registry_entry
                and entry[0] is owner
                and entry[1] is True
            ):
                del self._request_owners[owner._issuance]
                condition.notify_all()
                return True
            return entry is None

    def close(self):
        self._require_current_process()
        with self._lifecycle_condition:
            self._accepting_requests = False
            if self._closed:
                return True
            owners = tuple(
                entry[0] for entry in self._request_owners.values()
            )
        for owner in owners:
            if owner._prune_terminal():
                self._prune_request_owner(owner)
                continue
            if owner._is_current_thread():
                owner._shutdown_cleanup()
            else:
                if not owner._reclaim_abandoned_empty_request():
                    owner._reclaim_abandoned_request()
        owners = None
        with self._lifecycle_condition:
            if self._request_owners:
                return False
            self._profile_integration = None
            self._connection_factory = None
            self._connection_borrower = None
            self._gateway = None
            self._key_authority = None
            self._completion_policy = None
            self._request_secret_vault_factory = None
            self._prepare_session_delivery = None
            self._discard_request_secret_vault = None
            self._validate_logout = None
            self._revoke_logout = None
            self._prepare_authorization = None
            self._complete_authorization = None
            self._now = None
            self._token_factory = None
            self._process_guard = None
            self._closed = True
            return True

    @property
    def closed(self):
        self._require_current_process()
        with self._lifecycle_condition:
            return self._closed

    @property
    def active_request_count(self):
        self._require_current_process()
        with self._lifecycle_condition:
            return sum(
                1
                for entry in self._request_owners.values()
                if entry[1] is False
            )

    def _require_current_process(self):
        process_guard = self._process_guard
        if process_guard is not None:
            process_guard()
        return True

    def __repr__(self):
        self._require_current_process()
        with self._lifecycle_condition:
            state = "closed" if self._closed else "configured"
        return f"DurableGoogleLoginBrowserIntegration(<{state}>)"

    __str__ = __repr__

    def _handle(self, method, target, headers, body_stream, request_owner):
        parsed_target = _parse_target(target)
        if parsed_target is None:
            return _failure_response(
                HTTPStatus.BAD_REQUEST,
                "Request unavailable",
                "This sign-in request is not valid.",
            )
        path, raw_query = parsed_target
        header_items = _validated_header_items(headers)
        trusted_headers = (
            header_items is not None
            and (
                self._trusted_profile_post_headers(header_items)
                if path in _DELEGATED_ACCOUNT_ROUTES and method == "POST"
                else self._trusted_request_headers(header_items)
            )
        )
        if not trusted_headers:
            return _failure_response(
                HTTPStatus.BAD_REQUEST,
                "Request unavailable",
                "This sign-in request is not valid.",
            )
        if path not in _AUTH_ROUTES:
            return _failure_response(
                HTTPStatus.NOT_FOUND,
                "Page not found",
                "This page is not available.",
            )
        if path in _DELEGATED_ACCOUNT_ROUTES:
            return self._profile_integration.handle(
                method,
                target,
                header_items,
                body_stream,
            )
        allowed = _ALLOWED_METHODS[path]
        if method not in allowed:
            return _failure_response(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "Method not allowed",
                "This route does not accept that method.",
                extra_headers=(("Allow", ", ".join(allowed)),),
            )
        if path != GOOGLE_LOGIN_CALLBACK_ROUTE and raw_query:
            return _failure_response(
                HTTPStatus.BAD_REQUEST,
                "Request unavailable",
                "This sign-in request is not valid.",
            )
        if method == "POST" and not self._trusted_same_origin_post(header_items):
            return _failure_response(
                HTTPStatus.FORBIDDEN,
                "Request rejected",
                "This request could not be verified.",
            )

        if path == LOGIN_ROUTE:
            return self._login_page()
        if path == GOOGLE_LOGIN_START_ROUTE:
            return self._start_login(header_items, body_stream)
        if path == GOOGLE_LOGIN_CALLBACK_ROUTE:
            return self._complete_login(
                target,
                raw_query,
                header_items,
                request_owner,
            )
        if method in {"GET", "HEAD"}:
            return self._logout_page(header_items)
        return self._logout(header_items, body_stream)

    def _trusted_request_headers(self, items):
        hosts = _header_values(items, "host")
        if (
            len(hosts) != 1
            or not _constant_ascii_equal(hosts[0], self._public_authority)
            or any(
                name.lower() in _PROXY_HEADERS
                or name.lower().startswith("x-forwarded-")
                for name, _value in items
            )
        ):
            return False
        origins = _header_values(items, "origin")
        if len(origins) > 1:
            return False
        if origins and not _constant_ascii_equal(origins[0], self._public_origin):
            return False
        return True

    def _trusted_profile_post_headers(self, items):
        hosts = _header_values(items, "host")
        return (
            len(hosts) == 1
            and _constant_ascii_equal(hosts[0], self._public_authority)
            and not any(
                name.lower() in _PROXY_HEADERS
                or name.lower().startswith("x-forwarded-")
                for name, _value in items
            )
        )

    def _trusted_same_origin_post(self, items):
        origins = _header_values(items, "origin")
        fetch_sites = _header_values(items, "sec-fetch-site")
        if len(origins) != 1 or not _constant_ascii_equal(
            origins[0],
            self._public_origin,
        ):
            return False
        return not fetch_sites or (
            len(fetch_sites) == 1 and fetch_sites[0].lower() == "same-origin"
        )

    def _login_page(self):
        try:
            csrf = self._token_factory()
        except Exception as exc:
            _detach_browser_exception(exc)
            exc = None
            return _temporarily_unavailable()
        if type(csrf) is not str or _OPAQUE_CREDENTIAL.fullmatch(csrf) is None:
            return _temporarily_unavailable()
        body = _page(
            "Sign in",
            "<section class='card'>"
            "<p class='eyebrow'>Wahojobs account</p>"
            "<h1>Sign in</h1>"
            "<p>Continue with Google to open your account profile.</p>"
            f"<form method='post' action='{GOOGLE_LOGIN_START_ROUTE}'>"
            f"<input type='hidden' name='csrf' value='{html.escape(csrf, quote=True)}'>"
            "<label for='invitation'>Invitation credential (optional)</label>"
            "<input id='invitation' name='invitation' type='password' "
            "autocomplete='one-time-code' spellcheck='false'>"
            "<button type='submit'>Continue with Google</button>"
            "</form></section>",
        )
        return _response(
            HTTPStatus.OK,
            body,
            extra_headers=(("Set-Cookie", _login_csrf_cookie(csrf)),),
        )

    def _start_login(self, header_items, body_stream):
        form = _strict_form(
            header_items,
            body_stream,
            expected_fields=("csrf",),
            optional_fields=("invitation",),
        )
        cookie, cookie_valid = _security_cookie(
            header_items,
            LOGIN_CSRF_COOKIE_NAME,
            _OPAQUE_CREDENTIAL,
        )
        if (
            form is None
            or not cookie_valid
            or not _constant_ascii_equal(form["csrf"], cookie)
        ):
            return _failure_response(
                HTTPStatus.FORBIDDEN,
                "Sign-in request rejected",
                "Start sign-in again from this page.",
                extra_headers=(("Set-Cookie", _clear_login_csrf_cookie()),),
            )

        invitation_text = form.get("invitation")
        if invitation_text == "":
            invitation_text = None
        if (
            invitation_text is not None
            and _INVITATION_CREDENTIAL.fullmatch(invitation_text) is None
        ):
            form = None
            invitation_text = None
            return _failure_response(
                HTTPStatus.FORBIDDEN,
                "Sign-in request rejected",
                "Start sign-in again from this page.",
                extra_headers=(("Set-Cookie", _clear_login_csrf_cookie()),),
            )

        connection_owner = None
        connection = None
        prepared = None
        invitation_credential = (
            None
            if invitation_text is None
            else bytearray(invitation_text.encode("ascii"))
        )
        if "invitation" in form:
            form["invitation"] = None
        invitation_text = None
        try:
            connection_owner = self._connection_factory()
            connection = self._connection_borrower(connection_owner)
            if (
                connection is None
                or not callable(getattr(connection_owner, "close", None))
            ):
                raise RuntimeError("invalid_connection")
            if invitation_credential is None:
                prepared = self._prepare_authorization(
                    connection,
                    self._gateway,
                    self._key_authority,
                )
            else:
                prepared = self._prepare_authorization(
                    connection,
                    self._gateway,
                    self._key_authority,
                    invitation_credential=invitation_credential,
                )
            transaction_id = prepared.transaction_id
            authorization_url = prepared.authorization_url
            if (
                type(transaction_id) is not str
                or _TRANSACTION_ID.fullmatch(transaction_id) is None
                or not _trusted_google_authorization_url(authorization_url)
            ):
                raise RuntimeError("invalid_prepared_authorization")
            prepared.close()
            prepared = None
            response = _redirect_response(
                authorization_url,
                extra_headers=(
                    ("Set-Cookie", _transaction_cookie(transaction_id)),
                    ("Set-Cookie", _clear_login_csrf_cookie()),
                ),
            )
        except Exception as exc:
            _detach_browser_exception(exc)
            exc = None
            form = None
            cookie = None
            header_items = None
            body_stream = None
            transaction_id = None
            authorization_url = None
            response = _failure_response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Sign-in temporarily unavailable",
                "Sign-in could not be started safely. Please try again.",
                extra_headers=(("Set-Cookie", _clear_login_csrf_cookie()),),
            )
        finally:
            _clear_browser_secret(invitation_credential)
            _close_quietly(prepared)
            _close_quietly(connection_owner)
            connection = None
            invitation_credential = None
        return response

    def _complete_login(
        self,
        target,
        raw_query,
        header_items,
        request_owner,
    ):
        browser_transaction_id, browser_cookie_valid = _security_cookie(
            header_items,
            GOOGLE_TRANSACTION_COOKIE_NAME,
            _TRANSACTION_ID,
        )
        if not browser_cookie_valid:
            browser_transaction_id = None
        callback_url = self._callback_url + (f"?{raw_query}" if raw_query else "")
        connection_owner = None
        connection = None
        vault = None
        completion = None
        lease = None
        try:
            connection_owner = request_owner._acquire_connection_owner(
                self._connection_factory
            )
            connection = request_owner._borrow_connection(
                self._connection_borrower
            )
            if (
                connection is None
                or not callable(getattr(connection_owner, "close", None))
            ):
                raise RuntimeError("invalid_connection")
            vault = self._request_secret_vault_factory()
            completion = self._complete_authorization(
                connection,
                self._gateway,
                self._key_authority,
                callback_url,
                browser_transaction_id,
                self._completion_policy,
                vault,
            )
            status = getattr(completion, "status", None)
            if status != "issued":
                self._discard_request_secret_vault(vault)
                vault = None
                return _callback_failure_response(status)

            response_now = _trusted_now(self._now)
            lease = request_owner._acquire_delivery(
                self._prepare_session_delivery,
                connection_owner,
                connection,
                completion,
                vault,
                now=response_now,
            )
            session_cookie, csrf_cookie = _validated_delivery_cookies(lease)
            payload = _redirect_response(
                AUTHENTICATED_DESTINATION,
                extra_headers=(
                    ("Set-Cookie", session_cookie),
                    ("Set-Cookie", csrf_cookie),
                    ("Set-Cookie", _clear_transaction_cookie()),
                ),
                delivery_lease=lease,
                owned_connection=connection_owner,
                request_release=request_owner,
                delivery_owner=request_owner,
            )
            vault = None
            return payload
        except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
            propagated = exc
            self._abort_complete_login_delivery(
                request_owner,
                connection,
                connection_owner,
                completion,
                vault,
                lease,
            )
            _detach_browser_exception(propagated)
            target = None
            raw_query = None
            header_items = None
            browser_transaction_id = None
            callback_url = None
            vault = None
            lease = None
            completion = None
            exc = None
            raise propagated from None
        except Exception as exc:
            self._abort_complete_login_delivery(
                request_owner,
                connection,
                connection_owner,
                completion,
                vault,
                lease,
            )
            _detach_browser_exception(exc)
            exc = None
            target = None
            raw_query = None
            header_items = None
            browser_transaction_id = None
            callback_url = None
            vault = None
            lease = None
            completion = None
            return _callback_failure_response("unavailable")
        finally:
            connection_owner = None
            connection = None

    def _abort_complete_login_delivery(
        self,
        request_owner,
        connection,
        connection_owner,
        completion,
        vault,
        lease,
    ):
        delivery_owned = False
        if lease is not None:
            try:
                delivery_owned = request_owner._offer_delivery(
                    lease,
                    connection_owner,
                    connection,
                )
            except BaseException as exc:
                _detach_browser_exception(exc)
                exc = None
        try:
            delivery_owned = (
                request_owner._has_delivery_offer()
                or delivery_owned
            )
        except BaseException as exc:
            _detach_browser_exception(exc)
            exc = None
            delivery_owned = True
        if not delivery_owned:
            self._fail_delivery_before_boundary(
                connection,
                completion,
                vault,
                lease,
            )
        cleanup_complete = _abort_browser_request_owner_preserving_primary(
            request_owner
        )
        if delivery_owned and cleanup_complete and vault is not None:
            try:
                self._discard_request_secret_vault(vault)
            except BaseException as exc:
                _detach_browser_exception(exc)
                exc = None

    def _fail_delivery_before_boundary(
        self,
        connection,
        completion,
        vault,
        lease,
    ):
        if lease is not None:
            try:
                lease.fail_delivery()
            except BaseException as exc:
                _detach_browser_exception(exc)
                exc = None
        else:
            try:
                issued = (
                    connection is not None
                    and vault is not None
                    and getattr(completion, "status", None) == "issued"
                )
            except BaseException as exc:
                _detach_browser_exception(exc)
                exc = None
                issued = False
        if lease is None and issued:
            emergency_lease = None
            try:
                emergency_lease = self._prepare_session_delivery(
                    connection,
                    completion,
                    vault,
                    now=_delivery_compensation_time(completion),
                )
                emergency_lease.fail_delivery()
            except BaseException as exc:
                _detach_browser_exception(exc)
                exc = None
                if emergency_lease is not None:
                    try:
                        emergency_lease.fail_delivery()
                    except BaseException as retry_exc:
                        _detach_browser_exception(retry_exc)
                        retry_exc = None
        if vault is not None:
            try:
                self._discard_request_secret_vault(vault)
            except BaseException as exc:
                _detach_browser_exception(exc)
                exc = None

    def _logout_page(self, header_items):
        session_token, session_valid = _security_cookie(
            header_items,
            SESSION_COOKIE_NAME,
            _OPAQUE_CREDENTIAL,
        )
        csrf, csrf_valid = _security_cookie(
            header_items,
            SESSION_CSRF_COOKIE_NAME,
            _OPAQUE_CREDENTIAL,
        )
        if not session_valid or not csrf_valid:
            return _authentication_required()
        connection_owner = None
        connection = None
        try:
            connection_owner = self._connection_factory()
            connection = self._connection_borrower(connection_owner)
            if (
                connection is None
                or not callable(getattr(connection_owner, "close", None))
                or self._validate_logout(
                    connection,
                    session_token=session_token,
                    csrf_credential=csrf,
                    now=_trusted_now(self._now),
                )
                is not True
            ):
                return _authentication_required()
        except Exception as exc:
            _detach_browser_exception(exc)
            exc = None
            header_items = None
            session_token = None
            csrf = None
            return _authentication_required()
        finally:
            _close_quietly(connection_owner)
            connection = None
        body = _page(
            "Sign out",
            "<section class='card'>"
            "<p class='eyebrow'>Wahojobs account</p>"
            "<h1>Sign out?</h1>"
            "<p>This browser session will no longer open your account profile.</p>"
            f"<form method='post' action='{LOGOUT_ROUTE}'>"
            f"<input type='hidden' name='csrf' value='{html.escape(csrf, quote=True)}'>"
            "<button type='submit'>Sign out</button>"
            "</form>"
            f"<p><a href='{AUTHENTICATED_DESTINATION}'>Return to profile</a></p>"
            "</section>",
        )
        return _response(HTTPStatus.OK, body)

    def _logout(self, header_items, body_stream):
        form = _strict_form(header_items, body_stream, expected_fields=("csrf",))
        session_token, session_valid = _security_cookie(
            header_items,
            SESSION_COOKIE_NAME,
            _OPAQUE_CREDENTIAL,
        )
        csrf, csrf_valid = _security_cookie(
            header_items,
            SESSION_CSRF_COOKIE_NAME,
            _OPAQUE_CREDENTIAL,
        )
        if (
            form is None
            or not session_valid
            or not csrf_valid
            or not _constant_ascii_equal(form["csrf"], csrf)
        ):
            return _logout_rejected()
        connection_owner = None
        connection = None
        try:
            connection_owner = self._connection_factory()
            connection = self._connection_borrower(connection_owner)
            if (
                connection is None
                or not callable(getattr(connection_owner, "close", None))
                or self._revoke_logout(
                    connection,
                    session_token=session_token,
                    csrf_credential=csrf,
                    now=_trusted_now(self._now),
                )
                is not True
            ):
                return _logout_rejected()
        except Exception as exc:
            _detach_browser_exception(exc)
            exc = None
            header_items = None
            body_stream = None
            form = None
            session_token = None
            csrf = None
            return _logout_rejected()
        finally:
            _close_quietly(connection_owner)
            connection = None
        return _redirect_response(
            LOGIN_ROUTE,
            extra_headers=(
                ("Set-Cookie", _clear_session_cookie()),
                ("Set-Cookie", _clear_session_csrf_cookie()),
            ),
        )


def _validated_public_origin(value):
    if (
        type(value) is not str
        or len(value) > 512
        or _CONTROL_CHARACTERS.search(value) is not None
        or "\\" in value
    ):
        raise ValueError("invalid_durable_google_login_browser_configuration")
    try:
        parsed = urlsplit(value)
        authority = parsed.netloc
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
    except ValueError:
        raise ValueError("invalid_durable_google_login_browser_configuration") from None
    if (
        parsed.scheme != "https"
        or not authority
        or authority != authority.lower()
        or _AUTHORITY.fullmatch(authority) is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid_durable_google_login_browser_configuration")
    return f"https://{authority}", authority


def _parse_target(target):
    if type(target) is not str:
        return None
    try:
        encoded = target.encode("ascii")
    except UnicodeError:
        return None
    if (
        not encoded
        or len(encoded) > MAX_BROWSER_AUTH_TARGET_BYTES
        or _CONTROL_CHARACTERS.search(target) is not None
        or "\\" in target
        or not target.startswith("/")
        or target.startswith("//")
    ):
        return None
    try:
        parsed = urlsplit(target)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc or parsed.fragment:
        return None
    return parsed.path, parsed.query


def _validated_header_items(headers):
    try:
        if hasattr(headers, "raw_items"):
            raw = tuple(headers.raw_items())
        elif hasattr(headers, "items"):
            raw = tuple(headers.items())
        else:
            raw = tuple(headers)
    except Exception as exc:
        _detach_browser_exception(exc)
        exc = None
        return None
    if len(raw) > MAX_BROWSER_AUTH_HEADERS:
        return None
    validated = []
    for item in raw:
        if type(item) is not tuple or len(item) != 2:
            return None
        name, value = item
        if (
            type(name) is not str
            or _HTTP_TOKEN.fullmatch(name) is None
            or type(value) is not str
            or _HEADER_VALUE_FORBIDDEN.search(value) is not None
        ):
            return None
        try:
            if len(name.encode("ascii")) > 64 or len(value.encode("latin-1")) > 8_192:
                return None
        except UnicodeError:
            return None
        validated.append((name, value))
    return tuple(validated)


def _header_values(items, name):
    lowered = name.lower()
    return tuple(value for candidate, value in items if candidate.lower() == lowered)


def _strict_form(
    header_items,
    body_stream,
    *,
    expected_fields,
    optional_fields=(),
):
    if (
        type(expected_fields) is not tuple
        or type(optional_fields) is not tuple
        or not expected_fields
        or any(type(name) is not str for name in (*expected_fields, *optional_fields))
        or len(set((*expected_fields, *optional_fields)))
        != len(expected_fields) + len(optional_fields)
    ):
        return None
    allowed_fields = frozenset((*expected_fields, *optional_fields))
    content_types = _header_values(header_items, "content-type")
    lengths = _header_values(header_items, "content-length")
    transfer_encodings = _header_values(header_items, "transfer-encoding")
    if (
        len(content_types) != 1
        or not _strict_form_content_type(content_types[0])
        or len(lengths) != 1
        or _CONTENT_LENGTH.fullmatch(lengths[0]) is None
        or transfer_encodings
        or body_stream is None
        or not callable(getattr(body_stream, "read", None))
    ):
        return None
    length = int(lengths[0])
    if length < 1 or length > MAX_BROWSER_AUTH_FORM_BYTES:
        return None
    try:
        body = body_stream.read(length)
    except Exception as exc:
        _detach_browser_exception(exc)
        exc = None
        return None
    if type(body) is not bytes or len(body) != length:
        return None
    try:
        text = body.decode("utf-8")
    except UnicodeError:
        return None
    if _CONTROL_CHARACTERS.search(text) is not None or _INVALID_PERCENT_ESCAPE.search(text):
        return None
    try:
        pairs = parse_qsl(
            text,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=4,
            encoding="utf-8",
            errors="strict",
        )
    except (UnicodeError, ValueError):
        return None
    if not (
        len(expected_fields)
        <= len(pairs)
        <= len(expected_fields) + len(optional_fields)
    ):
        return None
    values = {}
    for name, value in pairs:
        if name not in allowed_fields or name in values:
            return None
        if (
            type(value) is not str
            or len(value) > 128
            or _CONTROL_CHARACTERS.search(value) is not None
        ):
            return None
        values[name] = value
    if any(name not in values for name in expected_fields):
        return None
    return values


def _clear_browser_secret(value):
    if type(value) is bytearray:
        value[:] = b"\x00" * len(value)
        value.clear()


def _security_cookie(header_items, name, value_pattern):
    cookie_headers = _header_values(header_items, "cookie")
    if len(cookie_headers) != 1:
        return None, False
    header = cookie_headers[0]
    try:
        encoded = header.encode("ascii")
    except UnicodeError:
        return None, False
    if not encoded or len(encoded) > MAX_BROWSER_AUTH_COOKIE_BYTES:
        return None, False
    parts = header.split(";")
    if len(parts) > MAX_BROWSER_AUTH_COOKIES:
        return None, False
    found = []
    for raw_part in parts:
        part = raw_part.strip(" \t")
        if (
            not part
            or "=" not in part
            or _CONTROL_CHARACTERS.search(part) is not None
        ):
            return None, False
        cookie_name, value = part.split("=", 1)
        if (
            _COOKIE_NAME.fullmatch(cookie_name) is None
            or value != value.strip()
            or any(character in value for character in ('"', ",", ";", "\\"))
        ):
            return None, False
        if cookie_name == name:
            found.append(value)
    if len(found) != 1 or value_pattern.fullmatch(found[0]) is None:
        return None, False
    return found[0], True


def _trusted_google_authorization_url(value):
    if (
        type(value) is not str
        or len(value) > MAX_BROWSER_AUTH_TARGET_BYTES
        or _CONTROL_CHARACTERS.search(value) is not None
        or "\\" in value
    ):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.netloc == "accounts.google.com"
        and parsed.path == "/o/oauth2/v2/auth"
        and parsed.username is None
        and parsed.password is None
        and bool(parsed.query)
        and not parsed.fragment
    )


def _trusted_now(provider):
    value = provider()
    if type(value) is not datetime or value.tzinfo is None:
        raise ValueError("invalid_browser_clock")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _delivery_compensation_time(completion):
    issued_session = getattr(completion, "issued_session", None)
    effective_expires_at = getattr(
        issued_session,
        "effective_expires_at",
        None,
    )
    if type(effective_expires_at) is not str:
        raise ValueError("invalid_browser_session_delivery")
    try:
        parsed = datetime.fromisoformat(effective_expires_at)
    except (TypeError, ValueError):
        raise ValueError("invalid_browser_session_delivery") from None
    if parsed.tzinfo is None:
        raise ValueError("invalid_browser_session_delivery")
    return parsed.astimezone(timezone.utc) - timedelta(seconds=1)


def _validated_delivery_cookies(lease):
    session_cookie = getattr(lease, "set_cookie_header", None)
    csrf = getattr(lease, "csrf_credential", None)
    if type(session_cookie) is not str or type(csrf) is not str:
        raise ValueError("invalid_browser_session_delivery")
    match = _SESSION_COOKIE.fullmatch(session_cookie)
    if match is None or _OPAQUE_CREDENTIAL.fullmatch(csrf) is None:
        raise ValueError("invalid_browser_session_delivery")
    max_age = int(match.group(2))
    if max_age < 1 or max_age > 7_776_000:
        raise ValueError("invalid_browser_session_delivery")
    expires = match.group(3)
    try:
        parsed = parsedate_to_datetime(expires)
    except (TypeError, ValueError):
        raise ValueError("invalid_browser_session_delivery") from None
    if parsed.tzinfo is None:
        raise ValueError("invalid_browser_session_delivery")
    csrf_cookie = (
        f"{SESSION_CSRF_COOKIE_NAME}={csrf}; Path=/; Max-Age={max_age}; "
        f"Expires={expires}; Secure; HttpOnly; SameSite=Strict"
    )
    return session_cookie, csrf_cookie


def _response(
    status,
    content,
    *,
    extra_headers=(),
    delivery_lease=None,
    owned_connection=None,
    request_release=None,
    delivery_owner=None,
):
    if type(content) is str:
        payload = content.encode("utf-8")
    elif type(content) is bytes:
        payload = content
    else:
        raise ValueError("invalid_durable_google_login_browser_response")
    headers = (
        ("Content-Type", "text/html; charset=utf-8"),
        ("Content-Length", str(len(payload))),
        *_SECURITY_HEADERS,
        *extra_headers,
    )
    return DurableGoogleLoginBrowserResponse(
        status=int(status),
        body=payload,
        headers=tuple(headers),
        _delivery_lease=delivery_lease,
        _owned_connection=owned_connection,
        _request_release=request_release,
        _delivery_owner=delivery_owner,
    )


def _redirect_response(
    location,
    *,
    extra_headers=(),
    delivery_lease=None,
    owned_connection=None,
    request_release=None,
    delivery_owner=None,
):
    if (
        type(location) is not str
        or not location
        or _CONTROL_CHARACTERS.search(location) is not None
    ):
        raise ValueError("invalid_browser_redirect")
    body = _page(
        "Redirecting",
        "<section class='card'><h1>Redirecting</h1>"
        "<p>Your request is continuing safely.</p></section>",
    )
    return _response(
        HTTPStatus.SEE_OTHER,
        body,
        extra_headers=(("Location", location), *extra_headers),
        delivery_lease=delivery_lease,
        owned_connection=owned_connection,
        request_release=request_release,
        delivery_owner=delivery_owner,
    )


def _failure_response(status, title, message, *, extra_headers=()):
    return _response(
        status,
        _page(
            title,
            f"<section class='card'><h1>{html.escape(title, quote=True)}</h1>"
            f"<p>{html.escape(message, quote=True)}</p>"
            f"<p><a href='{LOGIN_ROUTE}'>Return to sign in</a></p></section>",
        ),
        extra_headers=extra_headers,
    )


def _temporarily_unavailable():
    return _failure_response(
        HTTPStatus.SERVICE_UNAVAILABLE,
        "Sign-in temporarily unavailable",
        "Sign-in could not be started safely. Please try again.",
    )


def _callback_failure_response(status):
    if status == "authentication_denied":
        http_status = HTTPStatus.UNAUTHORIZED
        title = "Sign-in not completed"
        message = "Google sign-in was not accepted. Start again to continue."
    elif status in {"provider_unavailable", "unavailable", "idempotency_conflict"}:
        http_status = HTTPStatus.SERVICE_UNAVAILABLE
        title = "Sign-in temporarily unavailable"
        message = "Sign-in could not be completed safely. Start again to retry."
    else:
        http_status = HTTPStatus.BAD_REQUEST
        title = "Sign-in not completed"
        message = "This sign-in request is no longer valid. Start again to continue."
    return _failure_response(
        http_status,
        title,
        message,
        extra_headers=(("Set-Cookie", _clear_transaction_cookie()),),
    )


def _authentication_required():
    return _failure_response(
        HTTPStatus.UNAUTHORIZED,
        "Authentication required",
        "Sign in before signing out of an account session.",
    )


def _logout_rejected():
    return _failure_response(
        HTTPStatus.FORBIDDEN,
        "Sign-out request rejected",
        "This sign-out request could not be verified.",
    )


def _login_csrf_cookie(value):
    return (
        f"{LOGIN_CSRF_COOKIE_NAME}={value}; Path=/; "
        f"Max-Age={LOGIN_CONTEXT_MAX_AGE_SECONDS}; Secure; HttpOnly; SameSite=Strict"
    )


def _transaction_cookie(value):
    return (
        f"{GOOGLE_TRANSACTION_COOKIE_NAME}={value}; Path=/; "
        f"Max-Age={LOGIN_CONTEXT_MAX_AGE_SECONDS}; Secure; HttpOnly; SameSite=Lax"
    )


def _clear_login_csrf_cookie():
    return (
        f"{LOGIN_CSRF_COOKIE_NAME}=; Path=/; Max-Age=0; "
        f"Expires={_EXPIRED_COOKIE_DATE}; Secure; HttpOnly; SameSite=Strict"
    )


def _clear_transaction_cookie():
    return (
        f"{GOOGLE_TRANSACTION_COOKIE_NAME}=; Path=/; Max-Age=0; "
        f"Expires={_EXPIRED_COOKIE_DATE}; Secure; HttpOnly; SameSite=Lax"
    )


def _clear_session_cookie():
    return (
        f"{SESSION_COOKIE_NAME}=; Path=/; Max-Age=0; "
        f"Expires={_EXPIRED_COOKIE_DATE}; Secure; HttpOnly; SameSite=Lax"
    )


def _clear_session_csrf_cookie():
    return (
        f"{SESSION_CSRF_COOKIE_NAME}=; Path=/; Max-Age=0; "
        f"Expires={_EXPIRED_COOKIE_DATE}; Secure; HttpOnly; SameSite=Strict"
    )


def _constant_ascii_equal(left, right):
    if type(left) is not str or type(right) is not str:
        return False
    try:
        return hmac.compare_digest(left.encode("ascii"), right.encode("ascii"))
    except UnicodeError:
        return False


def _strict_form_content_type(value):
    if type(value) is not str:
        return False
    try:
        encoded = value.encode("ascii")
    except UnicodeError:
        return False
    return encoded.lower() == b"application/x-www-form-urlencoded"


def _detach_browser_exception(exc):
    try:
        _detach_browser_exception_graph(exc)
        return True
    except BaseException as sanitization_error:
        sanitized = _detach_browser_exception_fallback(
            exc,
            sanitization_error,
        )
        sanitization_error = None
        exc = None
        return sanitized


def _detach_browser_exception_graph(exc):
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
            traceback = BaseException.__dict__["__traceback__"].__get__(current)
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
                    retained = descriptor.__get__(current, type(current))
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
                BaseException.__dict__[name].__set__(current, replacement)
            except BaseException:
                pass


def _detach_browser_exception_fallback(*roots):
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
                    retained = descriptor.__get__(current, type(current))
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
                BaseException.__dict__[name].__set__(current, replacement)
            except BaseException:
                sanitized = False
        current = None
    roots = None
    return sanitized


def _close_quietly(value):
    close = None
    try:
        close = getattr(value, "close", None)
        if callable(close):
            close()
    except BaseException as exc:
        _detach_browser_exception(exc)
        exc = None
    finally:
        close = None
        value = None


def _abort_browser_request_owner_preserving_primary(owner):
    if type(owner) is not _BrowserRequestDeliveryOwner:
        return False
    try:
        return owner._request_abort()
    except BaseException as exc:
        _detach_browser_exception(exc)
        exc = None
        return False


def _borrow_database_connection(owner):
    if owner is None:
        return None
    borrow = getattr(owner, "_wahojobs_database_connection", None)
    if borrow is None:
        return owner
    if not callable(borrow):
        raise RuntimeError("invalid_connection")
    connection = borrow()
    if connection is None:
        raise RuntimeError("invalid_connection")
    return connection


def _page(title, body):
    safe_title = html.escape(title, quote=True)
    return f"""<!doctype html>
<html lang='en'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>{safe_title} | Wahojobs</title>
  <style>
    :root {{ color-scheme: light; font-family: Inter, system-ui, sans-serif; color: #202523; background: #f4f6f5; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; }}
    main {{ width: min(620px, calc(100% - 32px)); margin: 0 auto; padding: 64px 0; }}
    .card {{ background: white; border: 1px solid #dce2df; border-radius: 10px; padding: 28px; }}
    h1, p {{ margin-top: 0; }}
    .eyebrow {{ color: #466257; font-weight: 700; }}
    button {{ appearance: none; border: 0; border-radius: 7px; padding: 12px 18px; background: #174d3b; color: white; font: inherit; font-weight: 700; cursor: pointer; }}
    a {{ color: #174d3b; font-weight: 700; }}
  </style>
</head>
<body><main>{body}</main></body>
</html>"""


__all__ = [
    "AUTHENTICATED_DESTINATION",
    "DurableGoogleLoginBrowserIntegration",
    "DurableGoogleLoginBrowserResponse",
    "FIND_MATCHES_ROUTE",
    "GOOGLE_LOGIN_CALLBACK_ROUTE",
    "GOOGLE_LOGIN_START_ROUTE",
    "GOOGLE_TRANSACTION_COOKIE_NAME",
    "LOGIN_CSRF_COOKIE_NAME",
    "LOGIN_ROUTE",
    "LOGOUT_ROUTE",
    "MAX_BROWSER_AUTH_COOKIE_BYTES",
    "MAX_BROWSER_AUTH_FORM_BYTES",
    "MAX_BROWSER_AUTH_RESPONSE_BYTES",
    "MAX_BROWSER_AUTH_TARGET_BYTES",
    "PERSISTENT_PROFILE_ROUTE",
    "SESSION_CSRF_COOKIE_NAME",
]

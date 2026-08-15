"""Explicit browser routes for the small WorkOS AuthKit vertical slice."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hmac
import html
from http import HTTPStatus
import re
import sqlite3
import threading
from urllib.parse import parse_qsl, urlsplit

from wahojobs.accounts import (
    SessionUnavailable,
    StaleSessionVersion,
    revoke_current_session,
    validate_session_csrf,
)
from wahojobs.browser_session_lifecycle import (
    create_request_scoped_session_secret_vault,
    discard_request_scoped_session_secret_vault,
)
from wahojobs.trusted_login_completion import prepare_session_delivery
from wahojobs.workos_authkit import CALLBACK_PATH


LOGIN_ROUTE = "/login"
WORKOS_LOGIN_START_ROUTE = "/auth/workos/start"
WORKOS_LOGIN_CALLBACK_ROUTE = CALLBACK_PATH
LOGOUT_ROUTE = "/logout"
AUTHENTICATED_DESTINATION = "/account/profile"
FIND_MATCHES_ROUTE = "/find-matches"
TRACKER_ROUTE = "/tracker"
ACTION_ROUTE = "/action"

LOGIN_CSRF_COOKIE_NAME = "__Host-wahojobs_login_csrf"
WORKOS_TRANSACTION_COOKIE_NAME = "__Host-wahojobs_workos_tx"
SESSION_CSRF_COOKIE_NAME = "__Host-wahojobs_session_csrf"
SESSION_COOKIE_NAME = "wahojobs_session"
LOGIN_CONTEXT_MAX_AGE_SECONDS = 600

_AUTH_ROUTES = frozenset(
    {
        LOGIN_ROUTE,
        WORKOS_LOGIN_START_ROUTE,
        WORKOS_LOGIN_CALLBACK_ROUTE,
        LOGOUT_ROUTE,
        AUTHENTICATED_DESTINATION,
        FIND_MATCHES_ROUTE,
        TRACKER_ROUTE,
        ACTION_ROUTE,
    }
)
_DELEGATED_ROUTES = frozenset(
    {AUTHENTICATED_DESTINATION, FIND_MATCHES_ROUTE, TRACKER_ROUTE, ACTION_ROUTE}
)
_ALLOWED_METHODS = {
    LOGIN_ROUTE: ("GET",),
    WORKOS_LOGIN_START_ROUTE: ("POST",),
    WORKOS_LOGIN_CALLBACK_ROUTE: ("GET",),
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
_OPAQUE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_TRANSACTION_ID = re.compile(r"^wtx_[0-9a-f]{32}$")
_INVITATION = re.compile(r"^inv_[0-9a-f]{32}\.[A-Za-z0-9_-]{43}$")
_CONTENT_LENGTH = re.compile(r"^(?:0|[1-9][0-9]{0,3})$")
_AUTHORITY = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?|\[[0-9a-f:]+\])"
    r"(?::[1-9][0-9]{0,4})?$"
)
_SESSION_COOKIE = re.compile(
    r"^wahojobs_session=([A-Za-z0-9_-]{43}); Path=/; "
    r"Max-Age=([1-9][0-9]{0,7}); Expires=([^;\r\n]{1,64}); "
    r"Secure; HttpOnly; SameSite=Lax$"
)
_FORBIDDEN_HEADER_VALUE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_EXPIRED_COOKIE_DATE = "Thu, 01 Jan 1970 00:00:00 GMT"
_SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("Cache-Control", "no-store"),
    ("Referrer-Policy", "same-origin"),
)
_CONTENT_SECURITY_POLICY = (
    "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
    "form-action 'self' https://api.workos.com https://*.authkit.app; "
    "frame-ancestors 'none'"
)


class WorkOSAuthKitBrowserResponse:
    """Bounded response retaining B2C4 compensation until header delivery."""

    __slots__ = (
        "status",
        "body",
        "headers",
        "_lease",
        "_connection_owner",
        "_lock",
        "_state",
    )

    def __init__(
        self,
        status,
        body,
        headers,
        *,
        delivery_lease=None,
        connection_owner=None,
    ):
        if (
            type(status) is not int
            or not 100 <= status <= 599
            or type(body) is not bytes
            or len(body) > 1_048_576
            or type(headers) is not tuple
            or (delivery_lease is None) != (connection_owner is None)
        ):
            raise ValueError("invalid_workos_authkit_browser_response")
        for item in headers:
            if (
                type(item) is not tuple
                or len(item) != 2
                or type(item[0]) is not str
                or _HTTP_TOKEN.fullmatch(item[0]) is None
                or type(item[1]) is not str
                or _FORBIDDEN_HEADER_VALUE.search(item[1]) is not None
            ):
                raise ValueError("invalid_workos_authkit_browser_response")
        if delivery_lease is not None and not all(
            callable(getattr(delivery_lease, name, None))
            for name in ("acknowledge_delivery", "fail_delivery")
        ):
            raise ValueError("invalid_workos_authkit_browser_response")
        self.status = status
        self.body = body
        self.headers = headers
        self._lease = delivery_lease
        self._connection_owner = connection_owner
        self._lock = threading.Lock()
        self._state = "pending"

    def acknowledge_delivery(self):
        return self._terminalize("acknowledge_delivery")

    def fail_delivery(self):
        return self._terminalize("fail_delivery")

    def _terminalize(self, operation):
        with self._lock:
            if self._state != "pending":
                raise RuntimeError("browser_response_delivery_already_terminal")
            self._state = "terminalizing"
            lease = self._lease
            owner = self._connection_owner
        failed = False
        try:
            if lease is not None:
                getattr(lease, operation)()
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except Exception as exc:
            failed = True
            _detach_exception(exc)
        finally:
            _close_quietly(owner)
            with self._lock:
                self._lease = None
                self._connection_owner = None
                self.headers = ()
                self._state = "failed" if failed else "complete"
        if failed:
            raise RuntimeError("browser_response_delivery_unavailable") from None
        return None

    def __repr__(self):
        return (
            "WorkOSAuthKitBrowserResponse("
            f"status={self.status}, body=<redacted>, "
            f"header_count={len(self.headers)}, delivery={self._state!r})"
        )

    def __del__(self):
        try:
            if self._state == "pending" and self._lease is not None:
                self.fail_delivery()
        except BaseException:
            pass


class WorkOSAuthKitBrowserIntegration:
    """Own only login, callback, existing logout, and delegated profile routes."""

    __slots__ = (
        "_public_origin",
        "_public_authority",
        "_profile_integration",
        "_connection_factory",
        "_connection_borrower",
        "_gateway",
        "_completion_policy",
        "_vault_factory",
        "_prepare_delivery",
        "_discard_vault",
        "_validate_logout",
        "_revoke_logout",
        "_clock",
        "_token_factory",
        "_process_guard",
        "_lock",
        "_closed",
    )

    def __init__(
        self,
        *,
        public_origin,
        profile_integration,
        connection_factory,
        gateway,
        completion_policy,
        connection_borrower=None,
        request_secret_vault_factory=None,
        prepare_delivery=None,
        discard_secret_vault=None,
        validate_logout=None,
        revoke_logout=None,
        clock=None,
        token_factory=None,
        process_guard=None,
    ):
        origin, authority = _validated_public_origin(public_origin)
        if (
            not callable(getattr(profile_integration, "matches_route", None))
            or not callable(getattr(profile_integration, "handle", None))
            or profile_integration.matches_route(AUTHENTICATED_DESTINATION) is not True
            or not callable(connection_factory)
            or not callable(getattr(gateway, "prepare_authorization", None))
            or not callable(getattr(gateway, "complete_authorization", None))
            or getattr(gateway, "redirect_uri", None)
            != origin + WORKOS_LOGIN_CALLBACK_ROUTE
        ):
            raise ValueError("invalid_workos_authkit_browser_configuration")
        if connection_borrower is None:
            connection_borrower = _borrow_connection
        if request_secret_vault_factory is None:
            request_secret_vault_factory = create_request_scoped_session_secret_vault
        if prepare_delivery is None:
            prepare_delivery = prepare_session_delivery
        if discard_secret_vault is None:
            discard_secret_vault = discard_request_scoped_session_secret_vault
        if validate_logout is None:
            validate_logout = _validate_logout
        if revoke_logout is None:
            revoke_logout = _revoke_logout
        if clock is None:
            clock = lambda: datetime.now(timezone.utc)
        if token_factory is None:
            import secrets

            token_factory = lambda: secrets.token_urlsafe(32)
        callables = (
            connection_borrower,
            request_secret_vault_factory,
            prepare_delivery,
            discard_secret_vault,
            validate_logout,
            revoke_logout,
            clock,
            token_factory,
        )
        if not all(callable(value) for value in callables) or (
            process_guard is not None and not callable(process_guard)
        ):
            raise ValueError("invalid_workos_authkit_browser_configuration")
        self._public_origin = origin
        self._public_authority = authority
        self._profile_integration = profile_integration
        self._connection_factory = connection_factory
        self._connection_borrower = connection_borrower
        self._gateway = gateway
        self._completion_policy = completion_policy
        self._vault_factory = request_secret_vault_factory
        self._prepare_delivery = prepare_delivery
        self._discard_vault = discard_secret_vault
        self._validate_logout = validate_logout
        self._revoke_logout = revoke_logout
        self._clock = clock
        self._token_factory = token_factory
        self._process_guard = process_guard
        self._lock = threading.Lock()
        self._closed = False

    def matches_route(self, path):
        self._require_open()
        return path in _AUTH_ROUTES

    def handle(self, method, target, headers, body_stream=None):
        self._require_open()
        parsed = _parse_target(target)
        header_items = _header_items(headers)
        if parsed is None or header_items is None:
            return _failure(HTTPStatus.BAD_REQUEST, "Request unavailable")
        path, raw_query = parsed
        if path not in _AUTH_ROUTES:
            return _failure(HTTPStatus.NOT_FOUND, "Page not found")
        if not self._trusted_headers(header_items, profile_post=(path in _DELEGATED_ROUTES and method == "POST")):
            return _failure(HTTPStatus.BAD_REQUEST, "Request unavailable")
        if path in _DELEGATED_ROUTES:
            return self._profile_integration.handle(
                method,
                target,
                header_items,
                body_stream,
            )
        if method not in _ALLOWED_METHODS[path]:
            return _failure(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "Method not allowed",
                extra_headers=(("Allow", ", ".join(_ALLOWED_METHODS[path])),),
            )
        if path != WORKOS_LOGIN_CALLBACK_ROUTE and raw_query:
            return _failure(HTTPStatus.BAD_REQUEST, "Request unavailable")
        if method == "POST" and not self._same_origin(header_items):
            return _failure(HTTPStatus.FORBIDDEN, "Request rejected")
        if path == LOGIN_ROUTE:
            return self._login_page()
        if path == WORKOS_LOGIN_START_ROUTE:
            return self._start_login(header_items, body_stream)
        if path == WORKOS_LOGIN_CALLBACK_ROUTE:
            return self._complete_login(target, header_items)
        if method == "GET":
            return self._logout_page(header_items)
        return self._logout(header_items, body_stream)

    def close(self):
        with self._lock:
            if self._closed:
                return True
            self._closed = True
            self._profile_integration = None
            self._connection_factory = None
            self._connection_borrower = None
            self._gateway = None
            self._completion_policy = None
            self._vault_factory = None
            self._prepare_delivery = None
            self._discard_vault = None
            self._validate_logout = None
            self._revoke_logout = None
            self._clock = None
            self._token_factory = None
            self._process_guard = None
        return True

    def _require_open(self):
        if self._process_guard is not None:
            self._process_guard()
        with self._lock:
            if self._closed:
                raise RuntimeError("workos_authkit_browser_closed")

    def _trusted_headers(self, items, *, profile_post):
        hosts = _header_values(items, "host")
        if (
            len(hosts) != 1
            or not _constant_equal(hosts[0], self._public_authority)
            or any(
                name.lower() in _PROXY_HEADERS
                or name.lower().startswith("x-forwarded-")
                for name, _value in items
            )
        ):
            return False
        if profile_post:
            return True
        origins = _header_values(items, "origin")
        return len(origins) <= 1 and (
            not origins or _constant_equal(origins[0], self._public_origin)
        )

    def _same_origin(self, items):
        origins = _header_values(items, "origin")
        fetch_sites = _header_values(items, "sec-fetch-site")
        return (
            len(origins) == 1
            and _constant_equal(origins[0], self._public_origin)
            and (
                not fetch_sites
                or (len(fetch_sites) == 1 and fetch_sites[0].lower() == "same-origin")
            )
        )

    def _login_page(self):
        try:
            csrf = self._token_factory()
        except Exception as exc:
            _detach_exception(exc)
            return _failure(HTTPStatus.SERVICE_UNAVAILABLE, "Sign-in unavailable")
        if type(csrf) is not str or _OPAQUE.fullmatch(csrf) is None:
            return _failure(HTTPStatus.SERVICE_UNAVAILABLE, "Sign-in unavailable")
        body = _page(
            "Sign in",
            "<section class='card'><h1>Sign in</h1>"
            "<p>Continue with a one-time email code.</p>"
            f"<form method='post' action='{WORKOS_LOGIN_START_ROUTE}'>"
            f"<input type='hidden' name='csrf' value='{html.escape(csrf, quote=True)}'>"
            "<label for='invitation'>Invitation credential (first login only)</label>"
            "<input id='invitation' name='invitation' type='password' "
            "autocomplete='off' spellcheck='false'>"
            "<button type='submit'>Continue with email</button>"
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
            expected=("csrf",),
            optional=("invitation",),
        )
        cookie, valid = _cookie(header_items, LOGIN_CSRF_COOKIE_NAME, _OPAQUE)
        if form is None or not valid or not _constant_equal(form["csrf"], cookie):
            return _failure(
                HTTPStatus.FORBIDDEN,
                "Sign-in request rejected",
                extra_headers=(("Set-Cookie", _clear_login_csrf_cookie()),),
            )
        invitation_text = form.get("invitation") or None
        if invitation_text is not None and _INVITATION.fullmatch(invitation_text) is None:
            invitation_text = None
            return _failure(
                HTTPStatus.FORBIDDEN,
                "Sign-in request rejected",
                extra_headers=(("Set-Cookie", _clear_login_csrf_cookie()),),
            )
        invitation = None if invitation_text is None else bytearray(invitation_text.encode("ascii"))
        form["invitation"] = None
        invitation_text = None
        owner = None
        try:
            owner = self._connection_factory()
            connection = self._connection_borrower(owner)
            prepared = self._gateway.prepare_authorization(
                connection,
                invitation_credential=invitation,
            )
            if (
                _TRANSACTION_ID.fullmatch(prepared.transaction_id) is None
                or type(prepared.authorization_url) is not str
            ):
                raise RuntimeError("invalid_prepared_authorization")
            return _redirect(
                prepared.authorization_url,
                extra_headers=(
                    ("Set-Cookie", _transaction_cookie(prepared.transaction_id)),
                    ("Set-Cookie", _clear_login_csrf_cookie()),
                ),
            )
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except Exception as exc:
            _detach_exception(exc)
            return _failure(
                HTTPStatus.FORBIDDEN,
                "Sign-in request rejected",
                extra_headers=(("Set-Cookie", _clear_login_csrf_cookie()),),
            )
        finally:
            _clear_buffer(invitation)
            _close_quietly(owner)
            owner = None

    def _complete_login(self, target, header_items):
        transaction_id, valid = _cookie(
            header_items,
            WORKOS_TRANSACTION_COOKIE_NAME,
            _TRANSACTION_ID,
        )
        if not valid:
            transaction_id = None
        owner = None
        connection = None
        vault = None
        completion = None
        lease = None
        try:
            owner = self._connection_factory()
            connection = self._connection_borrower(owner)
            vault = self._vault_factory()
            completion = self._gateway.complete_authorization(
                connection,
                target,
                transaction_id,
                self._completion_policy,
                vault,
            )
            status = getattr(completion, "status", None)
            if status != "issued":
                self._discard_vault(vault)
                vault = None
                _close_quietly(owner)
                owner = None
                return _callback_failure(status)
            lease = self._prepare_delivery(
                connection,
                completion,
                vault,
                now=_trusted_now(self._clock),
            )
            session_cookie, csrf_cookie = _delivery_cookies(lease)
            response = _redirect(
                AUTHENTICATED_DESTINATION,
                extra_headers=(
                    ("Set-Cookie", session_cookie),
                    ("Set-Cookie", csrf_cookie),
                    ("Set-Cookie", _clear_transaction_cookie()),
                ),
                delivery_lease=lease,
                connection_owner=owner,
            )
            lease = None
            owner = None
            vault = None
            return response
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            self._compensate(connection, completion, vault, lease)
            raise
        except Exception as exc:
            _detach_exception(exc)
            self._compensate(connection, completion, vault, lease)
            return _callback_failure("unavailable")
        finally:
            _close_quietly(owner)
            connection = None
            target = None
            transaction_id = None

    def _compensate(self, connection, completion, vault, lease):
        if lease is not None:
            try:
                lease.fail_delivery()
            except BaseException as exc:
                _detach_exception(exc)
        elif (
            connection is not None
            and vault is not None
            and getattr(completion, "status", None) == "issued"
        ):
            emergency = None
            try:
                emergency = self._prepare_delivery(
                    connection,
                    completion,
                    vault,
                    now=_compensation_time(completion),
                )
                emergency.fail_delivery()
            except BaseException as exc:
                _detach_exception(exc)
                if emergency is not None:
                    try:
                        emergency.fail_delivery()
                    except BaseException as retry:
                        _detach_exception(retry)
        if vault is not None:
            try:
                self._discard_vault(vault)
            except BaseException as exc:
                _detach_exception(exc)

    def _logout_page(self, header_items):
        session, session_valid = _cookie(header_items, SESSION_COOKIE_NAME, _OPAQUE)
        csrf, csrf_valid = _cookie(header_items, SESSION_CSRF_COOKIE_NAME, _OPAQUE)
        if not session_valid or not csrf_valid:
            return _failure(HTTPStatus.UNAUTHORIZED, "Authentication required")
        owner = None
        try:
            owner = self._connection_factory()
            connection = self._connection_borrower(owner)
            if not self._validate_logout(
                connection,
                session_token=session,
                csrf_credential=csrf,
                now=_trusted_now(self._clock),
            ):
                return _failure(HTTPStatus.UNAUTHORIZED, "Authentication required")
        except Exception as exc:
            _detach_exception(exc)
            return _failure(HTTPStatus.UNAUTHORIZED, "Authentication required")
        finally:
            _close_quietly(owner)
        return _response(
            HTTPStatus.OK,
            _page(
                "Sign out",
                "<section class='card'><h1>Sign out?</h1>"
                f"<form method='post' action='{LOGOUT_ROUTE}'>"
                f"<input type='hidden' name='csrf' value='{html.escape(csrf, quote=True)}'>"
                "<button type='submit'>Sign out</button></form></section>",
            ),
        )

    def _logout(self, header_items, body_stream):
        form = _strict_form(header_items, body_stream, expected=("csrf",), optional=())
        session, session_valid = _cookie(header_items, SESSION_COOKIE_NAME, _OPAQUE)
        csrf, csrf_valid = _cookie(header_items, SESSION_CSRF_COOKIE_NAME, _OPAQUE)
        if (
            form is None
            or not session_valid
            or not csrf_valid
            or not _constant_equal(form["csrf"], csrf)
        ):
            return _failure(HTTPStatus.FORBIDDEN, "Sign-out request rejected")
        owner = None
        try:
            owner = self._connection_factory()
            connection = self._connection_borrower(owner)
            if not self._revoke_logout(
                connection,
                session_token=session,
                csrf_credential=csrf,
                now=_trusted_now(self._clock),
            ):
                return _failure(HTTPStatus.FORBIDDEN, "Sign-out request rejected")
        except Exception as exc:
            _detach_exception(exc)
            return _failure(HTTPStatus.SERVICE_UNAVAILABLE, "Sign-out unavailable")
        finally:
            _close_quietly(owner)
        return _redirect(
            LOGIN_ROUTE,
            extra_headers=(
                ("Set-Cookie", _clear_session_cookie()),
                ("Set-Cookie", _clear_session_csrf_cookie()),
            ),
        )


def _validate_logout(connection, *, session_token, csrf_credential, now):
    try:
        validate_session_csrf(
            connection,
            session_token=session_token,
            csrf_secret=csrf_credential,
            now=now,
        )
        return True
    except (SessionUnavailable, sqlite3.Error, ValueError, TypeError):
        return False


def _revoke_logout(connection, *, session_token, csrf_credential, now):
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
        return True
    except (
        SessionUnavailable,
        StaleSessionVersion,
        sqlite3.Error,
        ValueError,
        TypeError,
    ):
        return False


def _validated_public_origin(value):
    if type(value) is not str or len(value) > 2048:
        raise ValueError("invalid_workos_authkit_browser_configuration")
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise ValueError("invalid_workos_authkit_browser_configuration") from None
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or _AUTHORITY.fullmatch(parsed.netloc.lower()) is None
    ):
        raise ValueError("invalid_workos_authkit_browser_configuration")
    return f"https://{parsed.netloc.lower()}", parsed.netloc.lower()


def _parse_target(target):
    if type(target) is not str:
        return None
    try:
        if not 1 <= len(target.encode("utf-8")) <= 8192:
            return None
        parsed = urlsplit(target)
    except (UnicodeError, ValueError):
        return None
    if parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith("/"):
        return None
    return parsed.path, parsed.query


def _header_items(headers):
    try:
        items = tuple(headers.items())
    except (AttributeError, TypeError, ValueError):
        try:
            items = tuple(headers)
        except (TypeError, ValueError):
            return None
    if len(items) > 64:
        return None
    total = 0
    for item in items:
        if (
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or _HTTP_TOKEN.fullmatch(item[0]) is None
            or type(item[1]) is not str
            or _FORBIDDEN_HEADER_VALUE.search(item[1]) is not None
        ):
            return None
        try:
            total += len(item[0].encode("ascii")) + len(item[1].encode("latin-1"))
        except UnicodeError:
            return None
    return items if total <= 16_384 else None


def _header_values(items, name):
    return tuple(value for key, value in items if key.lower() == name)


def _strict_form(header_items, stream, *, expected, optional):
    content_types = _header_values(header_items, "content-type")
    lengths = _header_values(header_items, "content-length")
    if (
        len(content_types) != 1
        or content_types[0].lower() != "application/x-www-form-urlencoded"
        or len(lengths) != 1
        or _CONTENT_LENGTH.fullmatch(lengths[0]) is None
    ):
        return None
    length = int(lengths[0])
    if length > 1024 or stream is None or not callable(getattr(stream, "read", None)):
        return None
    try:
        payload = stream.read(length)
    except Exception:
        return None
    if type(payload) is not bytes or len(payload) != length or len(payload) > 1024:
        return None
    try:
        text = payload.decode("utf-8", "strict")
        if _INVALID_PERCENT_ESCAPE.search(text) is not None:
            return None
        pairs = parse_qsl(
            text,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=len(expected) + len(optional),
        )
    except (UnicodeError, ValueError):
        return None
    values = {}
    for key, value in pairs:
        if key in values:
            return None
        values[key] = value
    if set(values) - set(expected) - set(optional) or any(key not in values for key in expected):
        return None
    return values


def _cookie(header_items, name, pattern):
    headers = _header_values(header_items, "cookie")
    if len(headers) != 1 or len(headers[0].encode("latin-1")) > 4096:
        return None, False
    found = []
    parts = headers[0].split(";")
    if len(parts) > 16:
        return None, False
    for part in parts:
        if "=" not in part:
            return None, False
        key, value = (item.strip() for item in part.split("=", 1))
        if key == name:
            found.append(value)
    if len(found) != 1 or pattern.fullmatch(found[0]) is None:
        return None, False
    return found[0], True


def _delivery_cookies(lease):
    session_cookie = getattr(lease, "set_cookie_header", None)
    csrf = getattr(lease, "csrf_credential", None)
    if type(session_cookie) is not str or type(csrf) is not str:
        raise ValueError("invalid_browser_session_delivery")
    match = _SESSION_COOKIE.fullmatch(session_cookie)
    if match is None or _OPAQUE.fullmatch(csrf) is None:
        raise ValueError("invalid_browser_session_delivery")
    max_age = int(match.group(2))
    expires = match.group(3)
    parsed = parsedate_to_datetime(expires)
    if not 1 <= max_age <= 7_776_000 or parsed.tzinfo is None:
        raise ValueError("invalid_browser_session_delivery")
    csrf_cookie = (
        f"{SESSION_CSRF_COOKIE_NAME}={csrf}; Path=/; Max-Age={max_age}; "
        f"Expires={expires}; Secure; HttpOnly; SameSite=Strict"
    )
    return session_cookie, csrf_cookie


def _compensation_time(completion):
    value = getattr(getattr(completion, "issued_session", None), "effective_expires_at", None)
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError("invalid_browser_session_delivery") from None
    if parsed.tzinfo is None:
        raise ValueError("invalid_browser_session_delivery")
    return parsed.astimezone(timezone.utc) - timedelta(seconds=1)


def _response(status, content, *, extra_headers=(), delivery_lease=None, connection_owner=None):
    payload = content.encode("utf-8") if type(content) is str else content
    if type(payload) is not bytes:
        raise ValueError("invalid_workos_authkit_browser_response")
    headers = (
        ("Content-Type", "text/html; charset=utf-8"),
        ("Content-Length", str(len(payload))),
        ("Content-Security-Policy", _CONTENT_SECURITY_POLICY),
        *_SECURITY_HEADERS,
        *extra_headers,
    )
    return WorkOSAuthKitBrowserResponse(
        int(status),
        payload,
        tuple(headers),
        delivery_lease=delivery_lease,
        connection_owner=connection_owner,
    )


def _redirect(location, *, extra_headers=(), delivery_lease=None, connection_owner=None):
    if type(location) is not str or not location or _CONTROL.search(location) is not None:
        raise ValueError("invalid_browser_redirect")
    return _response(
        HTTPStatus.SEE_OTHER,
        _page("Redirecting", "<section><h1>Redirecting</h1></section>"),
        extra_headers=(("Location", location), *extra_headers),
        delivery_lease=delivery_lease,
        connection_owner=connection_owner,
    )


def _failure(status, title, *, extra_headers=()):
    return _response(
        status,
        _page(
            title,
            f"<section><h1>{html.escape(title, quote=True)}</h1>"
            f"<p><a href='{LOGIN_ROUTE}'>Return to sign in</a></p></section>",
        ),
        extra_headers=extra_headers,
    )


def _callback_failure(status):
    http_status = (
        HTTPStatus.UNAUTHORIZED
        if status == "authentication_denied"
        else HTTPStatus.SERVICE_UNAVAILABLE
        if status in {"provider_unavailable", "unavailable", "idempotency_conflict"}
        else HTTPStatus.BAD_REQUEST
    )
    return _failure(
        http_status,
        "Sign-in not completed",
        extra_headers=(("Set-Cookie", _clear_transaction_cookie()),),
    )


def _page(title, body):
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>{html.escape(title, quote=True)}</title>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "</head><body>" + body + "</body></html>"
    )


def _login_csrf_cookie(value):
    return (
        f"{LOGIN_CSRF_COOKIE_NAME}={value}; Path=/; "
        f"Max-Age={LOGIN_CONTEXT_MAX_AGE_SECONDS}; Secure; HttpOnly; SameSite=Strict"
    )


def _transaction_cookie(value):
    return (
        f"{WORKOS_TRANSACTION_COOKIE_NAME}={value}; Path=/; "
        f"Max-Age={LOGIN_CONTEXT_MAX_AGE_SECONDS}; Secure; HttpOnly; SameSite=Lax"
    )


def _clear_login_csrf_cookie():
    return (
        f"{LOGIN_CSRF_COOKIE_NAME}=; Path=/; Max-Age=0; Expires={_EXPIRED_COOKIE_DATE}; "
        "Secure; HttpOnly; SameSite=Strict"
    )


def _clear_transaction_cookie():
    return (
        f"{WORKOS_TRANSACTION_COOKIE_NAME}=; Path=/; Max-Age=0; "
        f"Expires={_EXPIRED_COOKIE_DATE}; Secure; HttpOnly; SameSite=Lax"
    )


def _clear_session_cookie():
    return (
        f"{SESSION_COOKIE_NAME}=; Path=/; Max-Age=0; Expires={_EXPIRED_COOKIE_DATE}; "
        "Secure; HttpOnly; SameSite=Lax"
    )


def _clear_session_csrf_cookie():
    return (
        f"{SESSION_CSRF_COOKIE_NAME}=; Path=/; Max-Age=0; Expires={_EXPIRED_COOKIE_DATE}; "
        "Secure; HttpOnly; SameSite=Strict"
    )


def _trusted_now(provider):
    value = provider()
    if type(value) is not datetime or value.tzinfo is None:
        raise ValueError("invalid_browser_clock")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _constant_equal(left, right):
    if type(left) is not str or type(right) is not str:
        return False
    try:
        return hmac.compare_digest(left.encode("ascii"), right.encode("ascii"))
    except UnicodeError:
        return False


def _borrow_connection(owner):
    if type(owner) is sqlite3.Connection:
        return owner
    connection = getattr(owner, "connection", None)
    if type(connection) is sqlite3.Connection:
        return connection
    raise RuntimeError("invalid_connection_owner")


def _close_quietly(value):
    if value is None:
        return
    try:
        close = getattr(value, "close", None)
        if callable(close):
            close()
    except BaseException as exc:
        _detach_exception(exc)


def _clear_buffer(value):
    if type(value) is bytearray:
        for index in range(len(value)):
            value[index] = 0
        value.clear()


def _detach_exception(exc):
    try:
        exc.__traceback__ = None
        exc.__cause__ = None
        exc.__context__ = None
    except (AttributeError, TypeError):
        pass


__all__ = [
    "AUTHENTICATED_DESTINATION",
    "LOGIN_ROUTE",
    "LOGOUT_ROUTE",
    "SESSION_COOKIE_NAME",
    "SESSION_CSRF_COOKIE_NAME",
    "WORKOS_LOGIN_CALLBACK_ROUTE",
    "WORKOS_LOGIN_START_ROUTE",
    "WORKOS_TRANSACTION_COOKIE_NAME",
    "WorkOSAuthKitBrowserIntegration",
    "WorkOSAuthKitBrowserResponse",
]

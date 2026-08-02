"""Protected browser reads and explicit create-once persistent profiles."""

from __future__ import annotations

from dataclasses import dataclass, field
import hmac
import html
from http import HTTPStatus
import re
from urllib.parse import parse_qs, urlsplit

from wahojobs.persistent_profiles_application import (
    BrowserRequestContext,
    MAX_BROWSER_CURSOR,
    PersistentProfileApplicationService,
    PersistentProfilePageResult,
)
from wahojobs.persistent_profile_creation import (
    ConfirmedProfileArtifactUnavailable,
    PersistentProfileCreationService,
    ProfileCreateOutcome,
)


PERSISTENT_PROFILE_ROUTE = "/account/profile"
FIND_MATCHES_ROUTE = "/find-matches"
MAX_PROFILE_BROWSER_RESPONSE_BYTES = 1_048_576
MAX_PROFILE_QUERY_BYTES = 256
MAX_PROFILE_CREATE_BODY_BYTES = 1_024
MAX_PROFILE_CREATE_HEADERS = 64
MAX_PROFILE_CREATE_COOKIE_BYTES = 4_096
MAX_PROFILE_CREATE_COOKIES = 16

SESSION_COOKIE_NAME = "wahojobs_session"
SESSION_CSRF_COOKIE_NAME = "__Host-wahojobs_session_csrf"

_CURSOR = re.compile(r"^[1-9][0-9]{0,9}$")
_OPAQUE_CREDENTIAL = re.compile(r"^[A-Za-z0-9_-]{43}$")
_CONTENT_LENGTH = re.compile(r"^(?:0|[1-9][0-9]{0,3})$")
_HTTP_TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_COOKIE_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_HEADER_VALUE_FORBIDDEN = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
_PROFILE_CREATE_FORM = re.compile(
    r"^(?:artifact=([A-Za-z0-9_-]{43})&csrf=([A-Za-z0-9_-]{43})"
    r"|csrf=([A-Za-z0-9_-]{43})&artifact=([A-Za-z0-9_-]{43}))$"
)
_PROXY_HEADERS = frozenset(
    {
        "forwarded",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-port",
        "x-forwarded-prefix",
        "x-forwarded-proto",
        "via",
        "x-original-host",
        "x-real-ip",
    }
)
_BIDI_CONTROLS = dict.fromkeys(
    (
        0x061C,
        0x200E,
        0x200F,
        0x202A,
        0x202B,
        0x202C,
        0x202D,
        0x202E,
        0x2066,
        0x2067,
        0x2068,
        0x2069,
    )
)
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


@dataclass(frozen=True, slots=True, repr=False)
class PersistentProfileBrowserResponse:
    status: int
    body: bytes = field(repr=False)
    headers: tuple[tuple[str, str], ...]

    def __post_init__(self):
        if type(self.status) is not int or not 100 <= self.status <= 599:
            raise ValueError("invalid_persistent_profile_browser_response")
        if type(self.body) is not bytes or len(self.body) > MAX_PROFILE_BROWSER_RESPONSE_BYTES:
            raise ValueError("invalid_persistent_profile_browser_response")
        if type(self.headers) is not tuple:
            raise ValueError("invalid_persistent_profile_browser_response")
        for item in self.headers:
            if (
                type(item) is not tuple
                or len(item) != 2
                or any(type(value) is not str or "\r" in value or "\n" in value for value in item)
            ):
                raise ValueError("invalid_persistent_profile_browser_response")

    def __repr__(self) -> str:
        return (
            "PersistentProfileBrowserResponse("
            f"status={self.status}, body=<redacted>, header_count={len(self.headers)})"
        )


class PersistentProfileBrowserIntegration:
    """Render the profile route and its explicit create-once POST boundary."""

    __slots__ = (
        "_closed",
        "_creation_service",
        "_matches_integration",
        "_public_authority",
        "_public_origin",
        "_service",
    )

    def __init__(
        self,
        service: PersistentProfileApplicationService,
        *,
        creation_service=None,
        matches_integration=None,
        public_origin=None,
    ):
        if (
            type(service) is not PersistentProfileApplicationService
            or (
                creation_service is not None
                and type(creation_service) is not PersistentProfileCreationService
            )
            or ((creation_service is None) != (public_origin is None))
        ):
            raise ValueError("invalid_persistent_profile_browser_configuration")
        if matches_integration is not None:
            _require_matches_integration(matches_integration)
        authority = None
        if public_origin is not None:
            try:
                parsed = urlsplit(public_origin)
                authority = parsed.netloc
            except ValueError:
                raise ValueError(
                    "invalid_persistent_profile_browser_configuration"
                ) from None
            if (
                type(public_origin) is not str
                or parsed.scheme != "https"
                or not authority
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or parsed.username is not None
                or parsed.password is not None
                or authority != authority.lower()
            ):
                raise ValueError("invalid_persistent_profile_browser_configuration")
        self._service = service
        self._creation_service = creation_service
        self._matches_integration = matches_integration
        self._public_origin = public_origin
        self._public_authority = authority
        self._closed = False

    def activate(self):
        if self._closed or self._creation_service is None:
            raise ConfirmedProfileArtifactUnavailable()
        return self._creation_service.activate()

    def attach_matches_integration(self, matches_integration):
        if self._closed or self._matches_integration is not None:
            raise ValueError("invalid_persistent_profile_browser_configuration")
        _require_matches_integration(matches_integration)
        self._matches_integration = matches_integration
        return True

    def matches_route(self, path: str) -> bool:
        if path == PERSISTENT_PROFILE_ROUTE:
            return True
        matches_integration = self._matches_integration
        return (
            matches_integration is not None
            and matches_integration.matches_route(path) is True
        )

    def handle(
        self,
        method: str,
        target: str,
        authentication_input=None,
        body_stream=None,
    ) -> PersistentProfileBrowserResponse:
        if self._closed:
            return _create_failure_response(HTTPStatus.SERVICE_UNAVAILABLE)
        if _request_target_path(target) == FIND_MATCHES_ROUTE:
            matches_integration = self._matches_integration
            if matches_integration is None:
                return _response(
                    HTTPStatus.NOT_FOUND,
                    _generic_page("Page not found", "This page is not available."),
                )
            return matches_integration.handle(
                method,
                target,
                authentication_input,
                body_stream,
            )
        allowed_methods = (
            ("GET", "HEAD", "POST")
            if self._creation_service is not None
            else ("GET", "HEAD")
        )
        if method not in allowed_methods:
            return _response(
                HTTPStatus.METHOD_NOT_ALLOWED,
                _generic_page(
                    "Method not allowed",
                    (
                        "This profile route does not accept that method."
                        if self._creation_service is not None
                        else "This profile page is read-only."
                    ),
                ),
                extra_headers=(("Allow", ", ".join(allowed_methods)),),
            )
        if method == "POST":
            return self._handle_create(target, authentication_input, body_stream)
        cursor, request_valid = _parse_request_target(target)
        if not request_valid:
            return _response(
                HTTPStatus.BAD_REQUEST,
                _generic_page("Profile request unavailable", "This profile request is not valid."),
            )
        try:
            result = self._service.read_my_profile(
                BrowserRequestContext(
                    method,
                    PERSISTENT_PROFILE_ROUTE,
                    authentication_input,
                ),
                before_revision_number=cursor,
            )
            content, status = render_persistent_profile_page(result)
        except Exception:
            content = _generic_page(
                "Profile temporarily unavailable",
                "Your persistent profile could not be loaded safely.",
            )
            status = HTTPStatus.SERVICE_UNAVAILABLE
        payload = content.encode("utf-8")
        if len(payload) > MAX_PROFILE_BROWSER_RESPONSE_BYTES:
            return _response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                _generic_page(
                    "Profile temporarily unavailable",
                    "Your persistent profile could not be displayed safely.",
                ),
            )
        return _response(status, content)

    def issue_confirmed_artifact(
        self,
        *,
        reviewed_profile,
        raw_about_you,
        normalized_updates,
        profile_confirmed,
        authentication_input,
        _confirmation_identity=None,
        _confirmation_witness=None,
        _confirmation_recovery_only=False,
    ):
        if self._closed or self._creation_service is None:
            raise ConfirmedProfileArtifactUnavailable()
        header_items = _validated_header_items(authentication_input)
        if header_items is None or not _trusted_host_headers(
            header_items,
            self._public_authority,
        ):
            raise ConfirmedProfileArtifactUnavailable()
        session_token, session_valid = _security_cookie(
            header_items,
            SESSION_COOKIE_NAME,
            _OPAQUE_CREDENTIAL,
        )
        csrf_secret, csrf_valid = _security_cookie(
            header_items,
            SESSION_CSRF_COOKIE_NAME,
            _OPAQUE_CREDENTIAL,
        )
        if not session_valid or not csrf_valid:
            raise ConfirmedProfileArtifactUnavailable()
        return self._creation_service.issue_confirmed_artifact(
            reviewed_profile=reviewed_profile,
            raw_about_you=raw_about_you,
            normalized_updates=normalized_updates,
            profile_confirmed=profile_confirmed,
            authentication_input=header_items,
            session_token=session_token,
            csrf_secret=csrf_secret,
            _confirmation_identity=_confirmation_identity,
            _confirmation_witness=_confirmation_witness,
            _confirmation_recovery_only=(
                _confirmation_recovery_only
            ),
        )

    def authenticate_completed_profile_replay(
        self,
        *,
        authentication_input,
        authority_binding,
    ):
        if self._closed or self._creation_service is None:
            return False
        header_items = _validated_header_items(authentication_input)
        if header_items is None or not _trusted_host_headers(
            header_items,
            self._public_authority,
        ):
            return False
        session_token, session_valid = _security_cookie(
            header_items,
            SESSION_COOKIE_NAME,
            _OPAQUE_CREDENTIAL,
        )
        csrf_secret, csrf_valid = _security_cookie(
            header_items,
            SESSION_CSRF_COOKIE_NAME,
            _OPAQUE_CREDENTIAL,
        )
        if not session_valid or not csrf_valid:
            return False
        return self._creation_service.authenticate_completed_replay(
            authentication_input=header_items,
            session_token=session_token,
            csrf_secret=csrf_secret,
            authority_binding=authority_binding,
        )

    def _handle_create(self, target, headers, body_stream):
        if not _profile_create_target_valid(target):
            return _create_failure_response(HTTPStatus.BAD_REQUEST)
        header_items = _validated_header_items(headers)
        if header_items is None or not _trusted_host_headers(
            header_items,
            self._public_authority,
        ):
            return _create_failure_response(HTTPStatus.BAD_REQUEST)
        if not _trusted_same_origin(
            header_items,
            self._public_origin,
        ):
            return _create_failure_response(HTTPStatus.FORBIDDEN)
        form = _strict_create_form(header_items, body_stream)
        if form is None:
            return _create_failure_response(HTTPStatus.BAD_REQUEST)
        session_token, session_valid = _security_cookie(
            header_items,
            SESSION_COOKIE_NAME,
            _OPAQUE_CREDENTIAL,
        )
        if not session_valid:
            return _create_failure_response(HTTPStatus.UNAUTHORIZED)
        csrf_secret, csrf_valid = _security_cookie(
            header_items,
            SESSION_CSRF_COOKIE_NAME,
            _OPAQUE_CREDENTIAL,
        )
        if not csrf_valid:
            return _create_failure_response(HTTPStatus.FORBIDDEN)
        if self._creation_service is None:
            return _create_failure_response(HTTPStatus.SERVICE_UNAVAILABLE)
        try:
            outcome = self._creation_service.consume(
                authentication_input=header_items,
                session_token=session_token,
                csrf_secret=csrf_secret,
                artifact_reference=form["artifact"],
                csrf_proof=form["csrf"],
            )
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except Exception:
            return _create_failure_response(HTTPStatus.SERVICE_UNAVAILABLE)
        if type(outcome) is not ProfileCreateOutcome:
            return _create_failure_response(HTTPStatus.SERVICE_UNAVAILABLE)
        return _create_response_for_outcome(outcome.state)

    def close(self):
        if self._closed:
            return self.closed
        if self._creation_service is not None:
            if self._creation_service.close() is False:
                return False
        if self._matches_integration is not None:
            if self._matches_integration.close() is False:
                return False
        self._closed = True
        return True

    @property
    def closed(self):
        return self._closed and (
            self._creation_service is None or self._creation_service.closed
        ) and (
            self._matches_integration is None
            or self._matches_integration.closed is True
        )


def _require_matches_integration(matches_integration):
    try:
        matches_route = getattr(matches_integration, "matches_route", None)
        handle_matches = getattr(matches_integration, "handle", None)
        close_matches = getattr(matches_integration, "close", None)
        matches_closed = getattr(matches_integration, "closed", None)
        matches_route_owned = (
            callable(matches_route)
            and matches_route(FIND_MATCHES_ROUTE) is True
        )
    except Exception:
        raise ValueError(
            "invalid_persistent_profile_browser_configuration"
        ) from None
    if (
        not matches_route_owned
        or not callable(handle_matches)
        or not callable(close_matches)
        or type(matches_closed) is not bool
        or matches_closed
    ):
        raise ValueError("invalid_persistent_profile_browser_configuration")


def _parse_request_target(target: str) -> tuple[int | None, bool]:
    if type(target) is not str or len(target.encode("utf-8", errors="ignore")) > MAX_PROFILE_QUERY_BYTES:
        return None, False
    try:
        parsed = urlsplit(target)
        if parsed.path != PERSISTENT_PROFILE_ROUTE or parsed.fragment:
            return None, False
        params = parse_qs(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=4,
        ) if parsed.query else {}
    except (UnicodeError, ValueError):
        return None, False
    if set(params) - {"before"}:
        return None, False
    if not params:
        return None, True
    values = params.get("before", ())
    if len(values) != 1 or _CURSOR.fullmatch(values[0]) is None:
        return None, False
    cursor = int(values[0])
    if cursor > MAX_BROWSER_CURSOR:
        return None, False
    return cursor, True


def _request_target_path(target: str) -> str | None:
    if type(target) is not str:
        return None
    try:
        parsed = urlsplit(target)
    except (UnicodeError, ValueError):
        return None
    if parsed.scheme or parsed.netloc or parsed.fragment:
        return None
    return parsed.path


def render_persistent_profile_page(
    result: PersistentProfilePageResult,
) -> tuple[str, HTTPStatus]:
    if type(result) is not PersistentProfilePageResult:
        raise ValueError("invalid_persistent_profile_page_result")
    if result.state == "authentication_required":
        return (
            _page(
                "Authentication required",
                "<section class='empty'><h1>Authentication required</h1>"
                "<p>Sign in to open your persistent profile.</p>"
                "<p><a class='primary-link' href='/login'>Continue to sign in</a></p>"
                "</section>",
            ),
            HTTPStatus.UNAUTHORIZED,
        )
    if result.state == "authorization_denied":
        return (
            _generic_page("Profile not found", "This profile page is not available."),
            HTTPStatus.NOT_FOUND,
        )
    if result.state == "empty":
        return (
            _page(
                "My persistent profile",
                _authenticated_navigation()
                + "<section class='empty'><h1>No persistent profile yet</h1>"
                "<p>Confirm your reviewed About You details to create this profile explicitly.</p>"
                "<p>Reading this page does not create or change profile data.</p>"
                f"<p><a class='primary-link' href='{FIND_MATCHES_ROUTE}'>"
                "Create profile</a></p></section>",
            ),
            HTTPStatus.OK,
        )
    if result.state == "temporary_contention":
        return (
            _generic_page(
                "Profile temporarily unavailable",
                "Your profile is busy. Please try again shortly.",
            ),
            HTTPStatus.SERVICE_UNAVAILABLE,
        )
    if result.state == "schema_unavailable":
        return (
            _generic_page(
                "Profile temporarily unavailable",
                "The persistent-profile capability is not available.",
            ),
            HTTPStatus.SERVICE_UNAVAILABLE,
        )
    if result.state == "unavailable":
        return (
            _generic_page(
                "Profile temporarily unavailable",
                "Your persistent profile could not be loaded safely.",
            ),
            HTTPStatus.SERVICE_UNAVAILABLE,
        )
    return _render_available(result), HTTPStatus.OK


def _render_available(result: PersistentProfilePageResult) -> str:
    profile = result.profile
    lifecycle_note = {
        "active": "This profile is active and shown read-only.",
        "archived": "This profile is archived and remains read-only.",
        "deletion_requested": (
            "Deletion has been requested. Profile content is hidden while that request is pending."
        ),
    }[result.state]
    title = _safe_text(profile.display_name) or "My persistent profile"
    groups = "".join(
        "<section class='profile-group'>"
        f"<h2>{_safe_text(group.label)}</h2>"
        "<ul>"
        + "".join(f"<li>{_safe_text(value)}</li>" for value in group.values)
        + "</ul></section>"
        for group in profile.field_groups
    )
    if not groups and result.state != "deletion_requested":
        groups = "<p class='muted'>No additional profile details are available.</p>"
    history = "".join(
        "<li class='history-item'>"
        f"<strong>Revision {item.revision_number}</strong>"
        f"<span>{_safe_text(_humanize(item.revision_kind))}</span>"
        f"<span>{_safe_text(_humanize(item.lifecycle_status))}</span>"
        f"<time>{_safe_text(item.accepted_at)}</time>"
        "</li>"
        for item in result.history
    ) or "<li class='muted'>No revision history is available.</li>"
    next_link = (
        f"<a class='next' href='{PERSISTENT_PROFILE_ROUTE}?before={result.next_cursor}'>"
        "Older revisions</a>"
        if result.next_cursor is not None
        else ""
    )
    body = f"""
    <header class='profile-header'>
      <p class='eyebrow'>Account profile</p>
      <h1>{title}</h1>
      <p>{_safe_text(lifecycle_note)}</p>
      <p><a class='primary-link' href='{FIND_MATCHES_ROUTE}'>Find matches</a></p>
      <dl class='meta'>
        <div><dt>Status</dt><dd>{_safe_text(_humanize(profile.lifecycle_status))}</dd></div>
        <div><dt>Current revision</dt><dd>{profile.revision_number}</dd></div>
        <div><dt>Last accepted update</dt><dd>{_safe_text(profile.updated_at)}</dd></div>
      </dl>
    </header>
    <div class='profile-grid'>{groups}</div>
    <section class='history'>
      <h2>Revision history</h2>
      <p class='muted'>History shows metadata only. Stored profile content and sources are omitted.</p>
      <ol>{history}</ol>
      {next_link}
    </section>
    """
    return _page("My persistent profile", _authenticated_navigation() + body)


def _response(status, content: str, *, extra_headers=()) -> PersistentProfileBrowserResponse:
    payload = content.encode("utf-8")
    headers = (
        ("Content-Type", "text/html; charset=utf-8"),
        ("Content-Length", str(len(payload))),
        *_SECURITY_HEADERS,
        *extra_headers,
    )
    return PersistentProfileBrowserResponse(int(status), payload, tuple(headers))


def _create_response_for_outcome(state):
    if state == "created":
        return _response(
            HTTPStatus.SEE_OTHER,
            _generic_page("Profile created", "Your persistent profile is ready."),
            extra_headers=(("Location", FIND_MATCHES_ROUTE),),
        )
    status = {
        "conflict": HTTPStatus.CONFLICT,
        "gone": HTTPStatus.GONE,
        "authentication_required": HTTPStatus.UNAUTHORIZED,
        "csrf_denied": HTTPStatus.FORBIDDEN,
        "authorization_denied": HTTPStatus.NOT_FOUND,
        "temporary_contention": HTTPStatus.SERVICE_UNAVAILABLE,
        "unavailable": HTTPStatus.SERVICE_UNAVAILABLE,
    }.get(state, HTTPStatus.SERVICE_UNAVAILABLE)
    return _create_failure_response(status)


def _create_failure_response(status):
    title, message = {
        HTTPStatus.BAD_REQUEST: (
            "Profile request unavailable",
            "This profile request is not valid.",
        ),
        HTTPStatus.UNAUTHORIZED: (
            "Authentication required",
            "Sign in to continue.",
        ),
        HTTPStatus.FORBIDDEN: (
            "Profile request rejected",
            "This request could not be verified.",
        ),
        HTTPStatus.NOT_FOUND: (
            "Profile not found",
            "This profile page is not available.",
        ),
        HTTPStatus.GONE: (
            "Profile confirmation expired",
            "Confirm your profile again before creating it.",
        ),
        HTTPStatus.CONFLICT: (
            "Profile already exists",
            "This account already has a persistent profile.",
        ),
        HTTPStatus.SERVICE_UNAVAILABLE: (
            "Profile temporarily unavailable",
            "Your profile could not be created safely.",
        ),
    }[HTTPStatus(status)]
    return _response(status, _generic_page(title, message))


def _profile_create_target_valid(target):
    if type(target) is not str:
        return False
    try:
        parsed = urlsplit(target)
    except ValueError:
        return False
    return (
        target == PERSISTENT_PROFILE_ROUTE
        and parsed.path == PERSISTENT_PROFILE_ROUTE
        and not parsed.scheme
        and not parsed.netloc
        and not parsed.query
        and not parsed.fragment
    )


def _validated_header_items(headers):
    try:
        if hasattr(headers, "raw_items"):
            raw = tuple(headers.raw_items())
        elif hasattr(headers, "items"):
            raw = tuple(headers.items())
        else:
            raw = tuple(headers)
    except Exception:
        return None
    if len(raw) > MAX_PROFILE_CREATE_HEADERS:
        return None
    result = []
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
        result.append((name, value))
    return tuple(result)


def _header_values(items, name):
    lowered = name.lower()
    return tuple(value for candidate, value in items if candidate.lower() == lowered)


def _trusted_host_headers(items, authority):
    if type(authority) is not str:
        return False
    hosts = _header_values(items, "host")
    try:
        host_matches = len(hosts) == 1 and hmac.compare_digest(
            hosts[0].encode("ascii"),
            authority.encode("ascii"),
        )
    except UnicodeError:
        host_matches = False
    return (
        host_matches
        and not any(
            name.lower() in _PROXY_HEADERS or name.lower().startswith("x-forwarded-")
            for name, _value in items
        )
    )


def _trusted_same_origin(items, public_origin):
    if type(public_origin) is not str:
        return False
    origins = _header_values(items, "origin")
    fetch_sites = _header_values(items, "sec-fetch-site")
    try:
        origin_matches = len(origins) == 1 and hmac.compare_digest(
            origins[0].encode("ascii"),
            public_origin.encode("ascii"),
        )
    except UnicodeError:
        origin_matches = False
    return (
        origin_matches
        and (
            not fetch_sites
            or (len(fetch_sites) == 1 and fetch_sites[0].lower() == "same-origin")
        )
    )


def _strict_create_form(header_items, body_stream):
    content_types = _header_values(header_items, "content-type")
    lengths = _header_values(header_items, "content-length")
    if (
        len(content_types) != 1
        or content_types[0].lower() != "application/x-www-form-urlencoded"
        or len(lengths) != 1
        or _CONTENT_LENGTH.fullmatch(lengths[0]) is None
        or _header_values(header_items, "transfer-encoding")
        or body_stream is None
        or not callable(getattr(body_stream, "read", None))
    ):
        return None
    length = int(lengths[0])
    if length < 1 or length > MAX_PROFILE_CREATE_BODY_BYTES:
        return None
    try:
        body = body_stream.read(length)
    except Exception:
        return None
    if type(body) is not bytes or len(body) != length:
        return None
    try:
        text = body.decode("utf-8")
    except UnicodeError:
        return None
    match = _PROFILE_CREATE_FORM.fullmatch(text)
    if match is None:
        return None
    if match.group(1) is not None:
        return {"artifact": match.group(1), "csrf": match.group(2)}
    return {"artifact": match.group(4), "csrf": match.group(3)}


def _security_cookie(header_items, name, value_pattern):
    cookie_headers = _header_values(header_items, "cookie")
    if len(cookie_headers) != 1:
        return None, False
    header = cookie_headers[0]
    try:
        encoded = header.encode("ascii")
    except UnicodeError:
        return None, False
    if not encoded or len(encoded) > MAX_PROFILE_CREATE_COOKIE_BYTES:
        return None, False
    parts = header.split(";")
    if len(parts) > MAX_PROFILE_CREATE_COOKIES:
        return None, False
    found = []
    for raw_part in parts:
        part = raw_part.strip(" \t")
        if not part or "=" not in part or _CONTROL_CHARACTERS.search(part) is not None:
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


def _safe_text(value) -> str:
    if type(value) is not str:
        value = str(value)
    return html.escape(value.translate(_BIDI_CONTROLS), quote=True)


def _humanize(value: str) -> str:
    return value.replace("_", " ").strip().title()


def _generic_page(title: str, message: str) -> str:
    return _page(
        title,
        f"<section class='empty'><h1>{_safe_text(title)}</h1>"
        f"<p>{_safe_text(message)}</p></section>",
    )


def _authenticated_navigation() -> str:
    return (
        "<nav class='account-nav' aria-label='Account'>"
        f"<a href='{PERSISTENT_PROFILE_ROUTE}'>My profile</a>"
        "<a href='/logout'>Sign out</a>"
        "</nav>"
    )


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang='en'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>{_safe_text(title)} | Wahojobs</title>
  <style>
    :root {{ color-scheme: light; font-family: Inter, system-ui, sans-serif; color: #202523; background: #f4f6f5; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; }}
    main {{ width: min(960px, calc(100% - 32px)); margin: 0 auto; padding: 36px 0 64px; }}
    .account-nav {{ display: flex; justify-content: flex-end; gap: 16px; margin-bottom: 16px; }}
    .account-nav a, .primary-link {{ color: #174d3b; font-weight: 700; }}
    .profile-header, .history, .empty {{ background: white; border: 1px solid #dce2df; border-radius: 8px; padding: 24px; }}
    .profile-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; margin: 16px 0; }}
    .profile-group {{ background: white; border: 1px solid #dce2df; border-radius: 8px; padding: 18px; }}
    h1, h2, p {{ margin-top: 0; }}
    h1 {{ font-size: 30px; margin-bottom: 10px; }}
    h2 {{ font-size: 18px; }}
    .eyebrow {{ color: #466257; font-weight: 700; }}
    .meta {{ display: flex; flex-wrap: wrap; gap: 20px; margin-bottom: 0; }}
    .meta div {{ min-width: 150px; }}
    dt {{ color: #66716c; font-size: 13px; }}
    dd {{ margin: 3px 0 0; font-weight: 650; }}
    ul, ol {{ padding-left: 20px; }}
    .history-item {{ display: grid; grid-template-columns: 1fr 1fr 1fr 2fr; gap: 10px; padding: 10px 0; border-bottom: 1px solid #e8ecea; }}
    .muted {{ color: #66716c; }}
    .next {{ display: inline-block; margin-top: 12px; color: #174d3b; font-weight: 700; }}
    @media (max-width: 650px) {{ .history-item {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body><main>{body}</main></body>
</html>"""

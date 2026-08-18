"""Authenticated profile-to-matches composition for the durable browser.

The module owns no startup behavior and accepts every durable dependency from
runtime composition.  Candidate drafts remain bounded and process-local;
stored profiles and opportunity inventory are read only through the configured
runtime connection provider.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import html
from http import HTTPStatus
import json
import re
import secrets
import sqlite3
import threading
from urllib.parse import parse_qs, urlencode, urlsplit

from scripts import local_product_app as local_product
from scripts import profile_to_matches_preview as profile_preview
from wahojobs import (
    pipeline_actions,
    pipeline_records,
    pipeline_state,
    public_job_page,
    public_jobs_catalog,
)
from wahojobs.accounts import SessionUnavailable, resolve_session, validate_session_csrf
from wahojobs.browser_session_authentication import (
    BrowserSessionAuthenticationUnavailable,
    DurableBrowserSessionAuthenticationGateway,
)
from wahojobs.matching.metadata_overlay import (
    OpportunityMetadataOverlay,
    apply_overlay_to_rows,
)
from wahojobs.persistent_profile_read_authorization import (
    DurablePersistentProfileReadAuthorizationGateway,
    PersistentProfileReadAuthorizationDecision,
)
from wahojobs.persistent_profiles import PersistentProfileDomainError
from wahojobs.persistent_profiles_repository import read_current_profile
from wahojobs.profiles.canonical_v2 import (
    CanonicalProfileV2Error,
    project_v2_to_matcher_v1,
)


AUTHENTICATED_MATCHES_ROUTE = "/find-matches"
AUTHENTICATED_TRACKER_ROUTE = "/tracker"
AUTHENTICATED_ACTION_ROUTE = "/action"
AUTHENTICATED_CANDIDATE_ROUTES = frozenset(
    {
        AUTHENTICATED_MATCHES_ROUTE,
        AUTHENTICATED_TRACKER_ROUTE,
        AUTHENTICATED_ACTION_ROUTE,
    }
)
MAX_MATCHES_RESPONSE_BYTES = 1_048_576
MAX_PUBLIC_JOBS_RESPONSE_BYTES = 8_388_608
MAX_BROWSER_RESPONSE_BYTES = max(
    MAX_MATCHES_RESPONSE_BYTES,
    MAX_PUBLIC_JOBS_RESPONSE_BYTES,
)
MAX_MATCHES_TARGET_BYTES = 2_048
MAX_MATCHES_POST_BODY_BYTES = 65_536
MAX_MATCHES_POST_FIELDS = 128
MAX_MATCHES_HEADERS = 64
MAX_MATCHES_COOKIE_BYTES = 4_096
MAX_MATCHES_COOKIES = 16
MATCH_PRESENTATION_LIMIT = 10

SESSION_COOKIE_NAME = "wahojobs_session"
SESSION_CSRF_COOKIE_NAME = "__Host-wahojobs_session_csrf"

_OPAQUE_CREDENTIAL = re.compile(r"^[A-Za-z0-9_-]{43}$")
_MATCH_RUN_REFERENCE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_CONTENT_LENGTH = re.compile(r"^(?:0|[1-9][0-9]{0,5})$")
_HTTP_TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_HEADER_VALUE_FORBIDDEN = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
_COOKIE_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
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
_SECURITY_HEADERS = (
    (
        "Content-Security-Policy",
        "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'",
    ),
    ("X-Content-Type-Options", "nosniff"),
)
_NO_REFERRER_POLICY = "no-referrer"
_SAME_ORIGIN_REFERRER_POLICY = "same-origin"


class DurableMatchesRequestContext:
    """Sealed request facts accepted by durable session authentication."""

    __slots__ = ("method", "route", "_authentication_input", "_sealed")

    def __init__(self, method: str, authentication_input):
        if method not in {"GET", "HEAD", "POST"}:
            raise ValueError("invalid_authenticated_matches_request_context")
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "route", AUTHENTICATED_MATCHES_ROUTE)
        object.__setattr__(self, "_authentication_input", authentication_input)
        object.__setattr__(self, "_sealed", True)

    def authentication_input_for_gateway(self):
        return self._authentication_input

    def __setattr__(self, _name, _value):
        raise AttributeError("authenticated_matches_request_context_is_immutable")

    def __repr__(self):
        return (
            "DurableMatchesRequestContext("
            f"method={self.method!r}, route='/find-matches', "
            "authentication_input=<redacted>)"
        )

    def __reduce_ex__(self, _protocol):
        raise TypeError("authenticated_matches_request_context_not_serializable")


@dataclass(frozen=True, slots=True, repr=False)
class AuthenticatedMatchesBrowserResponse:
    status: int
    body: bytes = field(repr=False)
    headers: tuple[tuple[str, str], ...]

    def __post_init__(self):
        if (
            type(self.status) is not int
            or not 100 <= self.status <= 599
            or type(self.body) is not bytes
            or len(self.body) > MAX_BROWSER_RESPONSE_BYTES
            or type(self.headers) is not tuple
        ):
            raise ValueError("invalid_authenticated_matches_response")
        for item in self.headers:
            if (
                type(item) is not tuple
                or len(item) != 2
                or any(
                    type(value) is not str or "\r" in value or "\n" in value
                    for value in item
                )
            ):
                raise ValueError("invalid_authenticated_matches_response")

    def __repr__(self):
        return (
            "AuthenticatedMatchesBrowserResponse("
            f"status={self.status}, body=<redacted>, "
            f"header_count={len(self.headers)})"
        )


class _AuthorizedMatchesState:
    __slots__ = (
        "_account_id",
        "_draft_binding",
        "_environment_namespace",
        "_principal_id",
        "_profile_id",
        "_profile_v2",
        "_session_id",
        "state",
    )

    def __init__(
        self,
        state,
        *,
        draft_binding,
        account_id=None,
        environment_namespace=None,
        principal_id=None,
        session_id=None,
        profile_id=None,
        profile_v2=None,
    ):
        if (
            state not in {"empty", "profile"}
            or type(draft_binding) is not str
            or re.fullmatch(r"[0-9a-f]{64}", draft_binding) is None
            or (state == "profile") != (type(profile_v2) is dict)
            or any(
                value is not None and (type(value) is not str or not value)
                for value in (
                    account_id,
                    environment_namespace,
                    principal_id,
                    session_id,
                    profile_id,
                )
            )
            or (state == "empty" and profile_id is not None)
            or (state == "profile" and profile_id is None and account_id is not None)
        ):
            raise ValueError("invalid_authenticated_matches_authority")
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "_draft_binding", draft_binding)
        object.__setattr__(self, "_profile_v2", deepcopy(profile_v2))
        object.__setattr__(self, "_account_id", account_id)
        object.__setattr__(self, "_environment_namespace", environment_namespace)
        object.__setattr__(self, "_principal_id", principal_id)
        object.__setattr__(self, "_session_id", session_id)
        object.__setattr__(self, "_profile_id", profile_id)

    def __setattr__(self, _name, _value):
        raise AttributeError("authenticated_matches_authority_is_immutable")

    def draft_binding(self):
        return self._draft_binding

    def trusted_profile_v2(self):
        if self.state != "profile" or self._profile_v2 is None:
            raise ValueError("authenticated_matches_profile_unavailable")
        return deepcopy(self._profile_v2)

    def candidate_workflow_authority(self):
        values = (
            self._account_id,
            self._environment_namespace,
            self._principal_id,
            self._session_id,
            self._profile_id,
        )
        if self.state != "profile" or any(type(value) is not str or not value for value in values):
            raise ValueError("authenticated_candidate_workflow_authority_unavailable")
        return values

    def __repr__(self):
        return f"_AuthorizedMatchesState(state={self.state!r}, content=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class MatchesAuthorityResult:
    state: str
    _authorized: object | None = field(default=None, repr=False)

    def __post_init__(self):
        states = {
            "authentication_required",
            "csrf_denied",
            "authorization_denied",
            "empty",
            "profile",
            "profile_unavailable",
            "schema_unavailable",
            "unavailable",
        }
        if self.state not in states:
            raise ValueError("invalid_authenticated_matches_authority_result")
        if self.state in {"empty", "profile"}:
            if type(self._authorized) is not _AuthorizedMatchesState:
                raise ValueError("invalid_authenticated_matches_authority_result")
        elif self._authorized is not None:
            raise ValueError("invalid_authenticated_matches_authority_result")

    def authorized_state(self):
        return self._authorized if self.state in {"empty", "profile"} else None

    def __repr__(self):
        return f"MatchesAuthorityResult(state={self.state!r}, content=<redacted>)"


class AuthenticatedProfileMatchesService:
    """Resolve durable request authority and its current V2 profile read-only."""

    __slots__ = (
        "_authentication_gateway",
        "_authorization_gateway",
        "_binding_secret",
        "_clock",
        "_connection_provider",
    )

    def __init__(
        self,
        *,
        authentication_gateway,
        authorization_gateway,
        connection_provider,
        clock,
        binding_secret,
    ):
        if (
            type(authentication_gateway)
            is not DurableBrowserSessionAuthenticationGateway
            or type(authorization_gateway)
            is not DurablePersistentProfileReadAuthorizationGateway
            or not callable(connection_provider)
            or not callable(clock)
            or type(binding_secret) is not bytes
            or len(binding_secret) < 32
        ):
            raise ValueError("invalid_authenticated_matches_service_configuration")
        self._authentication_gateway = authentication_gateway
        self._authorization_gateway = authorization_gateway
        self._connection_provider = connection_provider
        self._clock = clock
        self._binding_secret = bytes(binding_secret)

    def resolve(
        self,
        *,
        method,
        authentication_input,
        session_token,
        csrf_secret=None,
    ) -> MatchesAuthorityResult:
        if (
            method not in {"GET", "HEAD", "POST"}
            or type(session_token) is not str
            or _OPAQUE_CREDENTIAL.fullmatch(session_token) is None
            or (
                method == "POST"
                and (
                    type(csrf_secret) is not str
                    or _OPAQUE_CREDENTIAL.fullmatch(csrf_secret) is None
                )
            )
        ):
            return MatchesAuthorityResult(
                "csrf_denied" if method == "POST" else "authentication_required"
            )
        result = None
        connection = None
        try:
            with self._connection_provider() as connection:
                if (
                    not isinstance(connection, sqlite3.Connection)
                    or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
                    or connection.execute("PRAGMA query_only").fetchone()[0] != 1
                    or connection.in_transaction
                ):
                    return MatchesAuthorityResult("schema_unavailable")
                connection.execute("BEGIN")
                try:
                    now = _trusted_utc(self._clock())
                    actor = self._authentication_gateway.authenticate_browser_request(
                        connection,
                        DurableMatchesRequestContext(method, authentication_input),
                        now=now,
                    )
                    if actor is None:
                        return MatchesAuthorityResult("authentication_required")
                    try:
                        session = (
                            validate_session_csrf(
                                connection,
                                session_token=session_token,
                                csrf_secret=csrf_secret,
                                now=now,
                            )
                            if method == "POST"
                            else resolve_session(
                                connection,
                                session_token=session_token,
                                now=now,
                            )
                        )
                    except SessionUnavailable:
                        return MatchesAuthorityResult(
                            "csrf_denied"
                            if method == "POST"
                            else "authentication_required"
                        )
                    account_reference = actor.account_reference_for_authorization()
                    if (
                        type(account_reference) is not tuple
                        or len(account_reference) != 2
                        or account_reference[0] != session.user_id
                    ):
                        return MatchesAuthorityResult("unavailable")
                    decision = self._authorization_gateway.authorize_persistent_profile_read(
                        connection,
                        actor,
                    )
                    if type(decision) is not PersistentProfileReadAuthorizationDecision:
                        return MatchesAuthorityResult("unavailable")
                    if decision.state == "denied":
                        return MatchesAuthorityResult("authorization_denied")
                    if decision.state != "authorized":
                        return MatchesAuthorityResult("unavailable")
                    grant = decision.grant_for_application()
                    principal = grant.principal_for_repository()
                    binding = _authority_binding(
                        self._binding_secret,
                        account_id=account_reference[0],
                        environment_namespace=account_reference[1],
                        principal_id=principal.principal_id,
                        session_id=session.session_id,
                    )
                    try:
                        summary = read_current_profile(
                            connection,
                            principal,
                            include_structured_profile=True,
                        )
                    except PersistentProfileDomainError as exc:
                        reason = exc.reason_code
                        exc = None
                        if reason == "profile_not_found":
                            return MatchesAuthorityResult(
                                "empty",
                                _AuthorizedMatchesState(
                                    "empty",
                                    draft_binding=binding,
                                    account_id=account_reference[0],
                                    environment_namespace=account_reference[1],
                                    principal_id=principal.principal_id,
                                    session_id=session.session_id,
                                ),
                            )
                        if reason == "schema_capability_unavailable":
                            return MatchesAuthorityResult("schema_unavailable")
                        if reason == "temporary_contention":
                            return MatchesAuthorityResult("unavailable")
                        return MatchesAuthorityResult("unavailable")
                    if summary.lifecycle_status != "active":
                        return MatchesAuthorityResult("profile_unavailable")
                    trusted_summary = summary.trusted_dict(
                        include_structured_profile=True
                    )
                    profile_v2 = trusted_summary.get("structured_profile")
                    profile_id = trusted_summary.get("profile_id")
                    if (
                        trusted_summary.get("structured_profile_included") is not True
                        or type(profile_v2) is not dict
                        or type(profile_id) is not str
                        or not profile_id
                    ):
                        return MatchesAuthorityResult("profile_unavailable")
                    return MatchesAuthorityResult(
                        "profile",
                        _AuthorizedMatchesState(
                            "profile",
                            draft_binding=binding,
                            account_id=account_reference[0],
                            environment_namespace=account_reference[1],
                            principal_id=principal.principal_id,
                            session_id=session.session_id,
                            profile_id=profile_id,
                            profile_v2=profile_v2,
                        ),
                    )
                finally:
                    if connection.in_transaction:
                        connection.rollback()
        except BrowserSessionAuthenticationUnavailable:
            result = MatchesAuthorityResult("unavailable")
        except (sqlite3.Error, ValueError, TypeError):
            result = MatchesAuthorityResult("unavailable")
        except Exception:
            result = MatchesAuthorityResult("unavailable")
        finally:
            connection = None
        return result or MatchesAuthorityResult("unavailable")


class AuthenticatedProfileMatchesBrowserIntegration:
    """Own authenticated candidate review and query-only profile matching."""

    __slots__ = (
        "_artifact_sink",
        "_closed",
        "_completed_replay_authenticator",
        "_connection_provider",
        "_write_connection_provider",
        "_ephemeral_identity_factory",
        "_metadata_overlay",
        "_now",
        "_public_authority",
        "_public_jobs_cache",
        "_public_jobs_cache_lock",
        "_public_origin",
        "_registry",
        "_service",
    )

    def __init__(
        self,
        service,
        *,
        connection_provider,
        write_connection_provider=None,
        metadata_overlay,
        confirmed_profile_artifact_sink,
        completed_profile_confirmation_authenticator,
        public_origin,
        now,
        ephemeral_identity_factory=None,
        registry=None,
    ):
        origin, authority = _validated_public_origin(public_origin)
        if (
            type(service) is not AuthenticatedProfileMatchesService
            or not callable(connection_provider)
            or (
                write_connection_provider is not None
                and not callable(write_connection_provider)
            )
            or type(metadata_overlay) is not OpportunityMetadataOverlay
            or not callable(confirmed_profile_artifact_sink)
            or not callable(completed_profile_confirmation_authenticator)
            or not callable(now)
        ):
            raise ValueError("invalid_authenticated_matches_browser_configuration")
        if ephemeral_identity_factory is None:
            ephemeral_identity_factory = lambda: "matcher-" + secrets.token_hex(16)
        if not callable(ephemeral_identity_factory):
            raise ValueError("invalid_authenticated_matches_browser_configuration")
        if registry is None:
            registry = local_product.MatchRunRegistry()
        if type(registry) is not local_product.MatchRunRegistry:
            raise ValueError("invalid_authenticated_matches_browser_configuration")
        self._service = service
        self._connection_provider = connection_provider
        self._write_connection_provider = write_connection_provider
        self._metadata_overlay = metadata_overlay
        self._artifact_sink = confirmed_profile_artifact_sink
        self._completed_replay_authenticator = (
            completed_profile_confirmation_authenticator
        )
        self._public_origin = origin
        self._public_authority = authority
        self._public_jobs_cache = None
        self._public_jobs_cache_lock = threading.Lock()
        self._now = now
        self._ephemeral_identity_factory = ephemeral_identity_factory
        self._registry = registry
        self._closed = False

    def matches_route(self, path):
        if self._closed or path == AUTHENTICATED_ACTION_ROUTE and self._write_connection_provider is None:
            return False
        if path == AUTHENTICATED_TRACKER_ROUTE and self._write_connection_provider is None:
            return False
        return (
            path in AUTHENTICATED_CANDIDATE_ROUTES
            or path == public_jobs_catalog.PUBLIC_JOBS_ROUTE
            or public_job_page.parse_public_job_path(path) is not None
        )

    def handle(self, method, target, authentication_input=None, body_stream=None):
        if self._closed:
            return _failure_response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Matches temporarily unavailable",
                "Matches cannot be loaded safely right now.",
            )
        if method not in {"GET", "HEAD", "POST"}:
            return _failure_response(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "Method not allowed",
                "This matches route does not accept that method.",
                extra_headers=(("Allow", "GET, HEAD, POST"),),
            )
        parsed_target = _parse_target(target, method=method)
        params = None if parsed_target is None else parsed_target[1]
        if params is None:
            return _failure_response(
                HTTPStatus.BAD_REQUEST,
                "Matches request unavailable",
                "This matches request is not valid.",
            )
        header_items = _validated_header_items(authentication_input)
        if header_items is None or not _trusted_host_headers(
            header_items,
            self._public_authority,
        ):
            return _failure_response(
                HTTPStatus.BAD_REQUEST,
                "Matches request unavailable",
                "This matches request is not valid.",
            )
        route = parsed_target[0]
        if route == public_jobs_catalog.PUBLIC_JOBS_ROUTE:
            if method not in {"GET", "HEAD"}:
                return _failure_response(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "Method not allowed",
                    "The public jobs catalog accepts GET and HEAD only.",
                    extra_headers=(("Allow", "GET, HEAD"),),
                )
            return self._handle_public_jobs(params, header_items)
        if public_job_page.parse_public_job_path(route) is not None:
            if method not in {"GET", "HEAD"}:
                return _failure_response(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "Method not allowed",
                    "This public job page accepts GET and HEAD only.",
                    extra_headers=(("Allow", "GET, HEAD"),),
                )
            return self._handle_public_job(
                route,
                header_items,
                catalog_return_to=params.get("return_to"),
            )
        if route != AUTHENTICATED_MATCHES_ROUTE and self._write_connection_provider is None:
            return _failure_response(HTTPStatus.NOT_FOUND, "Page not found", "This page is not available.")
        if method == "POST" and not _trusted_same_origin(
            header_items,
            self._public_origin,
        ):
            return _failure_response(
                HTTPStatus.FORBIDDEN,
                "Matches request rejected",
                "This request could not be verified.",
            )
        session_token, session_valid = _security_cookie(
            header_items,
            SESSION_COOKIE_NAME,
            _OPAQUE_CREDENTIAL,
        )
        if not session_valid:
            return _authority_failure("authentication_required")
        csrf_secret = None
        if method == "POST":
            csrf_secret, csrf_valid = _security_cookie(
                header_items,
                SESSION_CSRF_COOKIE_NAME,
                _OPAQUE_CREDENTIAL,
            )
            if not csrf_valid:
                return _authority_failure("csrf_denied")
        authority_result = self._service.resolve(
            method=method,
            authentication_input=header_items,
            session_token=session_token,
            csrf_secret=csrf_secret,
        )
        if authority_result.state not in {"empty", "profile"}:
            return _authority_failure(authority_result.state)
        authority = authority_result.authorized_state()
        if route == AUTHENTICATED_ACTION_ROUTE:
            if method != "POST":
                return _failure_response(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "Method not allowed",
                    "This action route accepts POST only.",
                    extra_headers=(("Allow", "POST"),),
                )
            if authority.state != "profile":
                return _authority_failure("profile_unavailable")
            form = _strict_post_form(header_items, body_stream)
            if form is None:
                return _workflow_failure(HTTPStatus.BAD_REQUEST, "Malformed action request.", header_items)
            return self._handle_action(form, authority, header_items)
        if route == AUTHENTICATED_TRACKER_ROUTE:
            if method not in {"GET", "HEAD"}:
                return _failure_response(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "Method not allowed",
                    "My Jobs accepts GET and HEAD only.",
                    extra_headers=(("Allow", "GET, HEAD"),),
                )
            if authority.state != "profile":
                return _authority_failure("profile_unavailable")
            return self._handle_tracker(params, authority)
        if method in {"GET", "HEAD"}:
            return self._handle_get(params, authority)
        form = _strict_post_form(header_items, body_stream)
        if form is None:
            return _failure_response(
                HTTPStatus.BAD_REQUEST,
                "Matches request unavailable",
                "This matches request is not valid.",
            )
        return self._handle_post(form, authority, header_items)

    def current_matches_target(self, run_id, authentication_input=None):
        """Return a current-run target only when this request still owns the run."""
        if self._closed:
            return None
        header_items = _validated_header_items(authentication_input)
        if header_items is None or not _trusted_host_headers(
            header_items,
            self._public_authority,
        ):
            return None
        session_token, session_valid = _security_cookie(
            header_items,
            SESSION_COOKIE_NAME,
            _OPAQUE_CREDENTIAL,
        )
        if not session_valid:
            return None
        authority_result = self._service.resolve(
            method="GET",
            authentication_input=header_items,
            session_token=session_token,
            csrf_secret=None,
        )
        if authority_result.state != "profile":
            return None
        authority = authority_result.authorized_state()
        run = self._authorized_run(run_id, authority)
        if run is None or run.recommendation_context is None:
            return None
        return AUTHENTICATED_MATCHES_ROUTE + "?" + urlencode(
            {"run": run.match_run_id}
        )

    def _handle_get(self, params, authority):
        if authority.state == "profile":
            current_run = None
            if params:
                if self._write_connection_provider is None or set(params) - {"run", "review"}:
                    return _failure_response(
                        HTTPStatus.BAD_REQUEST,
                        "Matches request unavailable",
                        "This matches request is not valid.",
                    )
                current_run = self._authorized_run(params.get("run"), authority)
                if current_run is None:
                    return _failure_response(
                        HTTPStatus.GONE,
                        "Matches session expired",
                        "Reload matches to continue.",
                    )
            return self._render_persistent_matches(authority, run=current_run)
        if not params:
            return _form_page_response(HTTPStatus.OK, _render_candidate_entry())
        run = self._authorized_run(params["run"], authority)
        if run is None:
            return _failure_response(
                HTTPStatus.GONE,
                "Profile review expired",
                "That profile review is unknown or has expired.",
            )
        if params.get("edit_text") == "1":
            return _form_page_response(
                HTTPStatus.OK,
                _render_candidate_entry(run=run),
            )
        return _form_page_response(
            HTTPStatus.OK,
            _render_candidate_review(run),
        )

    def _handle_post(self, form, authority, header_items):
        if authority.state == "profile":
            return _redirect_response(AUTHENTICATED_MATCHES_ROUTE)
        try:
            if "form_action" in form:
                run_id = _single_form_value(form, "edit_run_id")
                run = self._authorized_run(run_id, authority, confirmation=True)
                if run is None:
                    return _failure_response(
                        HTTPStatus.GONE,
                        "Profile review expired",
                        "That profile review is unknown or has expired.",
                    )
                result = local_product.confirm_profile_review(
                    form,
                    self._registry,
                    confirmed_profile_artifact_sink=self._artifact_sink,
                    completed_profile_confirmation_authenticator=(
                        self._completed_replay_authenticator
                    ),
                    authentication_input=header_items,
                    _allow_matching=False,
                )
                if type(result) is not local_product.ConfirmedProfileCreation:
                    raise RuntimeError("profile_confirmation_unavailable")
                content = local_product.render_confirmed_profile_creation(
                    result.artifact_offer
                )
                return _form_page_response(HTTPStatus.OK, content)
            run = self._create_candidate_draft(form, authority)
            location = AUTHENTICATED_MATCHES_ROUTE + "?" + urlencode(
                {"run": run.match_run_id, "review": "1"}
            )
            return _redirect_response(location)
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except local_product.ActionError as exc:
            status = exc.status
            exc = None
            return _failure_response(
                status,
                "Profile review unavailable",
                "This profile review could not be completed safely.",
            )
        except (ValueError, TypeError):
            return _failure_response(
                HTTPStatus.BAD_REQUEST,
                "Matches request unavailable",
                "This matches request is not valid.",
            )
        except Exception:
            return _failure_response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Profile review unavailable",
                "This profile review could not be completed safely.",
            )

    def _handle_tracker(self, params, authority):
        try:
            run = None
            run_id = params.get("run")
            if run_id:
                run = self._authorized_run(run_id, authority)
                if run is None:
                    return _failure_response(
                        HTTPStatus.GONE,
                        "My Jobs session expired",
                        "Reload My Jobs to continue.",
                    )
            if run is None:
                run = self._registry.create(
                    owner_profile_id=authority.candidate_workflow_authority()[4],
                    raw_input="",
                    input_style="short_paragraph",
                    recommendation_context=None,
                    profile_confirmed=True,
                )
            records = self._load_pipeline_records(authority)
            content = _render_authenticated_tracker(
                records,
                run.match_run_id,
                params.get("view", "all"),
                current_matches_available=run.recommendation_context is not None,
            )
            return _form_page_response(HTTPStatus.OK, content)
        except (sqlite3.Error, ValueError, TypeError, pipeline_records.PipelineRecordInvariant):
            return _failure_response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "My Jobs temporarily unavailable",
                "Your jobs cannot be loaded safely right now.",
            )

    def _handle_action(self, form, authority, header_items):
        wants_json = (
            any("application/json" in value.lower() for value in _header_values(header_items, "accept"))
            or _header_values(header_items, local_product.INLINE_ACTION_HEADER.lower()) == ("1",)
        )
        try:
            allowed_fields = (
                local_product.ACTION_REQUIRED_SINGLE_FIELDS
                | local_product.ACTION_OPTIONAL_SINGLE_FIELDS
            )
            if set(form) - allowed_fields:
                raise local_product.MalformedActionRequest()
            local_product.validate_action_form(form)
            run_id = local_product.action_form_value(form, "match_run_id")
            run = self._authorized_run(run_id, authority)
            if run is None:
                raise local_product.ActionError(
                    "That match run is unknown or has expired. Reload and try again.",
                    HTTPStatus.GONE,
                )
            result = self._perform_pipeline_action(form, run, authority)
            if wants_json:
                return _json_response(
                    HTTPStatus.OK,
                    local_product.action_json_payload(result, run, form),
                )
            section = local_product.action_form_value(form, "section")
            if section == "tracker":
                location = AUTHENTICATED_TRACKER_ROUTE + "?" + urlencode(
                    {
                        "run": run.match_run_id,
                        "view": local_product.action_form_value(
                            form,
                            "tracker_view",
                            allow_empty=True,
                        )
                        or "all",
                    }
                )
            elif section == "public_job":
                location = local_product.action_form_value(form, "return_to")
                if public_job_page.parse_public_job_path(location) is None:
                    raise local_product.MalformedActionRequest()
            else:
                location = AUTHENTICATED_MATCHES_ROUTE + "?" + urlencode(
                    {"run": run.match_run_id}
                )
            return _redirect_response(location)
        except local_product.ActionError as exc:
            message = str(exc)
            status = exc.status
        except (pipeline_state.StaleStateVersion, pipeline_state.IdempotencyConflict) as exc:
            status = HTTPStatus.CONFLICT
            message = (
                "This item changed since the page was loaded. Refresh and try again."
                if isinstance(exc, pipeline_state.StaleStateVersion)
                else "This action conflicts with an earlier request. Refresh and try again."
            )
        except (pipeline_actions.UnresolvedLegacyWorkflow, pipeline_state.InvalidTransition) as exc:
            status = HTTPStatus.CONFLICT
            message = str(exc)
        except pipeline_actions.PipelineActionValidationError as exc:
            status = HTTPStatus.BAD_REQUEST
            message = str(exc)
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except Exception:
            status = HTTPStatus.SERVICE_UNAVAILABLE
            message = "The action could not be completed safely."
        return _workflow_failure(status, message, header_items, wants_json=wants_json)

    def _handle_public_job(self, path, header_items, *, catalog_return_to=None):
        try:
            connection = None
            with self._connection_provider() as connection:
                if (
                    not isinstance(connection, sqlite3.Connection)
                    or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
                    or connection.execute("PRAGMA query_only").fetchone()[0] != 1
                    or connection.in_transaction
                ):
                    raise ValueError("public_job_inventory_unavailable")
                connection.execute("BEGIN")
                try:
                    job = public_job_page.load_public_job(
                        connection,
                        path,
                        now=self._now(),
                    )
                finally:
                    if connection.in_transaction:
                        connection.rollback()
            if job is None:
                return _failure_response(
                    HTTPStatus.NOT_FOUND,
                    "Job not found",
                    "This opportunity page is not available.",
                )

            authority = self._optional_public_authority(header_items)
            authenticated = authority is not None and authority.state == "profile"
            controls = ""
            status = ""
            if authenticated and self._write_connection_provider is not None:
                records = self._load_pipeline_records(authority)
                match = job["workflow_match"]
                context = {"matches": {"do_these_first": [match]}}
                run = self._registry.create(
                    owner_profile_id=authority.candidate_workflow_authority()[4],
                    raw_input="",
                    input_style="short_paragraph",
                    recommendation_context=context,
                    profile_confirmed=True,
                )
                record = local_product.demo.tracked_record_for_match(
                    match,
                    local_product.demo.build_tracked_index(records),
                )
                controls = local_product.render_preview_full_forms(
                    match,
                    record,
                    run.match_run_id,
                    path,
                    "public_job",
                )
                if record is not None:
                    status = local_product.readable_status(record["status"])

            content = public_job_page.render_public_job_page(
                job,
                public_origin=self._public_origin,
                authenticated=authenticated,
                navigation=_public_navigation(
                    authenticated=authenticated,
                    current="job",
                ),
                workflow_controls=controls,
                workflow_status=status,
                catalog_return_to=catalog_return_to,
            )
            return _html_response(
                HTTPStatus.OK,
                content,
                referrer_policy=(
                    _SAME_ORIGIN_REFERRER_POLICY
                    if authenticated
                    else _NO_REFERRER_POLICY
                ),
                cache_control=(
                    "no-store"
                    if authenticated
                    else "public, max-age=300"
                ),
            )
        except (sqlite3.Error, ValueError, TypeError):
            return _failure_response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Job temporarily unavailable",
                "This opportunity page cannot be loaded safely right now.",
            )
        except Exception:
            return _failure_response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Job temporarily unavailable",
                "This opportunity page cannot be loaded safely right now.",
            )

    def _handle_public_jobs(self, params, header_items):
        try:
            query_present = bool(params.pop("_query_present", False))
            jobs = self._load_public_jobs_inventory()

            authority = self._optional_public_authority(header_items)
            authenticated = authority is not None and authority.state == "profile"
            catalog = public_jobs_catalog.build_catalog(jobs, params)
            content = public_jobs_catalog.render_public_jobs_page(
                catalog,
                public_origin=self._public_origin,
                navigation=_public_navigation(
                    authenticated=authenticated,
                    current="jobs",
                ),
                query_present=query_present,
            )
            return _html_response(
                HTTPStatus.OK,
                content,
                referrer_policy=(
                    _SAME_ORIGIN_REFERRER_POLICY
                    if authenticated
                    else _NO_REFERRER_POLICY
                ),
                cache_control=("no-store" if authenticated else "public, max-age=300"),
                max_bytes=MAX_PUBLIC_JOBS_RESPONSE_BYTES,
            )
        except (sqlite3.Error, ValueError, TypeError):
            return _failure_response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Jobs temporarily unavailable",
                "The current jobs catalog cannot be loaded safely right now.",
            )
        except Exception:
            return _failure_response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Jobs temporarily unavailable",
                "The current jobs catalog cannot be loaded safely right now.",
            )

    def _load_public_jobs_inventory(self):
        now = _trusted_utc(self._now())
        with self._public_jobs_cache_lock:
            cached = self._public_jobs_cache
            if (
                cached is not None
                and cached[0] <= now
                and now < cached[1]
            ):
                return cached[2]

            connection = None
            with self._connection_provider() as connection:
                if (
                    not isinstance(connection, sqlite3.Connection)
                    or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
                    or connection.execute("PRAGMA query_only").fetchone()[0] != 1
                    or connection.in_transaction
                ):
                    raise ValueError("public_jobs_inventory_unavailable")
                connection.execute("BEGIN")
                try:
                    jobs = public_jobs_catalog.load_public_jobs(
                        connection,
                        now=now,
                    )
                finally:
                    if connection.in_transaction:
                        connection.rollback()

            snapshot = tuple(jobs)
            self._public_jobs_cache = (
                now,
                public_jobs_catalog.catalog_cache_deadline(snapshot, now),
                snapshot,
            )
            return snapshot

    def _optional_public_authority(self, header_items):
        session_token, session_valid = _security_cookie(
            header_items,
            SESSION_COOKIE_NAME,
            _OPAQUE_CREDENTIAL,
        )
        if not session_valid:
            return None
        result = self._service.resolve(
            method="GET",
            authentication_input=header_items,
            session_token=session_token,
            csrf_secret=None,
        )
        if result.state != "profile":
            return None
        return result.authorized_state()

    def _load_pipeline_records(self, authority):
        account_id, _environment, _principal_id, _session_id, profile_id = (
            authority.candidate_workflow_authority()
        )
        connection = None
        with self._connection_provider() as connection:
            if (
                not isinstance(connection, sqlite3.Connection)
                or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
                or connection.execute("PRAGMA query_only").fetchone()[0] != 1
                or connection.in_transaction
            ):
                raise ValueError("candidate_workflow_read_unavailable")
            connection.execute("BEGIN")
            try:
                owner = connection.execute(
                    "SELECT user_id, is_sample FROM user_profiles WHERE profile_id=?",
                    (profile_id,),
                ).fetchone()
                count = connection.execute(
                    "SELECT COUNT(*) FROM user_pipeline_items WHERE profile_id=?",
                    (profile_id,),
                ).fetchone()[0]
                if owner is None and count:
                    raise ValueError("candidate_workflow_owner_unavailable")
                if owner is not None and (
                    owner["user_id"] != account_id or owner["is_sample"] != 0
                ):
                    raise ValueError("candidate_workflow_owner_unavailable")
                local_product.require_normalized_browser_read_ready(connection)
                return [
                    local_product.normalized_browser_record(record)
                    for record in pipeline_records.list_pipeline_records(
                        connection,
                        profile_id,
                        mutation_grade=True,
                    )
                ]
            finally:
                if connection.in_transaction:
                    connection.rollback()

    def _perform_pipeline_action(self, form, run, authority):
        if self._write_connection_provider is None:
            raise local_product.ActionError(
                "Candidate workflow is unavailable.",
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        connection = None
        with self._write_connection_provider() as connection:
            if (
                not isinstance(connection, sqlite3.Connection)
                or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
                or connection.execute("PRAGMA query_only").fetchone()[0] != 0
                or connection.in_transaction
            ):
                raise local_product.ActionError(
                    "Candidate workflow is unavailable.",
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )
            with pipeline_state.atomic(connection):
                _ensure_candidate_workflow_owner(
                    connection,
                    authority,
                    now=_trusted_utc(self._now()),
                )
                return _perform_authenticated_pipeline_action(
                    connection,
                    form=form,
                    run=run,
                    owner_profile_id=authority.candidate_workflow_authority()[4],
                    now=_trusted_utc(self._now()),
                )

    def _create_candidate_draft(self, form, authority):
        allowed = {"input_text", "input_style", "edit_run_id", "edit_review_token"}
        if set(form) - allowed:
            raise ValueError("invalid_candidate_entry_form")
        for key in form:
            _single_form_value(form, key)
        raw_input = _single_form_value(form, "input_text")
        input_style = (
            _single_form_value(form, "input_style", required=False)
            or "short_paragraph"
        )
        edit_run_id = _single_form_value(form, "edit_run_id", required=False)
        edit_token = _single_form_value(
            form,
            "edit_review_token",
            required=False,
        )
        parent = None
        if edit_run_id:
            parent = self._authorized_run(edit_run_id, authority)
            if parent is None:
                raise local_product.ActionError(
                    "This profile review has expired.",
                    HTTPStatus.GONE,
                )
            if not edit_token or not secrets.compare_digest(
                edit_token,
                parent.review_token,
            ):
                raise local_product.ActionError(
                    "This profile edit is not authorized.",
                    HTTPStatus.FORBIDDEN,
                )
        elif edit_token:
            raise ValueError("invalid_candidate_entry_form")
        if not raw_input:
            raise local_product.ActionError(
                "Add a short background before finding matches."
            )
        if input_style not in profile_preview.INPUT_STYLES:
            input_style = "short_paragraph"
        canonical = local_product.normalize_identity_free_profile_input(
            raw_input,
            input_style,
        )
        return self._registry.create(
            owner_profile_id=authority.draft_binding(),
            raw_input=raw_input,
            input_style=input_style,
            demo_persona=None,
            recommendation_context=None,
            canonical_profile=canonical,
            profile_confirmed=False,
        )

    def _authorized_run(self, run_id, authority, *, confirmation=False):
        if (
            type(run_id) is not str
            or _MATCH_RUN_REFERENCE.fullmatch(run_id) is None
        ):
            return None
        run = (
            self._registry.confirmation_draft(run_id)
            if confirmation
            else self._registry.get(run_id)
        )
        expected_owner = authority.draft_binding()
        if self._write_connection_provider is not None and authority.state == "profile":
            expected_owner = authority.candidate_workflow_authority()[4]
        if run is None or not hmac.compare_digest(run.owner_profile_id, expected_owner):
            return None
        return run

    def _render_persistent_matches(self, authority, *, run=None):
        try:
            if run is not None and run.recommendation_context is not None:
                context = run.recommendation_context
                inventory_count = context.get("_authenticated_inventory_count")
                if type(inventory_count) is not int or inventory_count < 0:
                    raise ValueError("candidate_match_run_inventory_unavailable")
            else:
                profile_v2 = authority.trusted_profile_v2()
                matcher_profile_id = self._ephemeral_identity_factory()
                projected = project_v2_to_matcher_v1(
                    profile_v2,
                    matcher_profile_id=matcher_profile_id,
                )
                rows, overlay_status = self._load_inventory()
                inventory_count = len(rows)
                evaluated_at = _trusted_utc(self._now())
                context = profile_preview.build_preview_context_from_canonical_rows(
                    projected,
                    inventory_rows=rows,
                    metadata_overlay_status=overlay_status,
                    limit=local_product.PREVIEW_MATCH_LIMIT,
                    normalizer_name="canonical_v2_projection",
                    normalization_warnings=[],
                    extraction_quality="reviewed",
                    evaluated_at=evaluated_at,
                )
            if self._write_connection_provider is None:
                content = _render_match_results(context, inventory_count=inventory_count)
            else:
                records = self._load_pipeline_records(authority)
                if run is None or run.recommendation_context is None:
                    context["_authenticated_inventory_count"] = inventory_count
                    run = self._registry.create(
                        owner_profile_id=authority.candidate_workflow_authority()[4],
                        raw_input="",
                        input_style="short_paragraph",
                        recommendation_context=context,
                        profile_confirmed=True,
                    )
                content = _render_match_results(
                    context,
                    inventory_count=inventory_count,
                    tracked=local_product.demo.build_tracked_index(records),
                    match_run_id=run.match_run_id,
                )
            return (
                _form_page_response(HTTPStatus.OK, content)
                if self._write_connection_provider is not None
                else _html_response(HTTPStatus.OK, content)
            )
        except (CanonicalProfileV2Error, sqlite3.Error, ValueError, TypeError):
            return _failure_response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Matches temporarily unavailable",
                "Matches cannot be loaded safely right now.",
            )
        except Exception:
            return _failure_response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Matches temporarily unavailable",
                "Matches cannot be loaded safely right now.",
            )

    def _load_inventory(self):
        connection = None
        try:
            with self._connection_provider() as connection:
                if (
                    not isinstance(connection, sqlite3.Connection)
                    or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
                    or connection.execute("PRAGMA query_only").fetchone()[0] != 1
                    or connection.in_transaction
                ):
                    raise ValueError("configured_inventory_unavailable")
                connection.execute("BEGIN")
                try:
                    rows = profile_preview.query_preview_rows(connection)
                finally:
                    if connection.in_transaction:
                        connection.rollback()
        finally:
            connection = None
        enriched = apply_overlay_to_rows(rows, self._metadata_overlay)
        status = {
            "enabled": self._metadata_overlay.enabled,
            "records_loaded": len(self._metadata_overlay.records_by_key),
            "rows_enriched": sum(
                1 for row in enriched if row.get("metadata_overlay_applied")
            ),
        }
        return enriched, status

    def close(self):
        if self._closed:
            return True
        self._closed = True
        self._registry = None
        self._service = None
        self._connection_provider = None
        self._write_connection_provider = None
        self._metadata_overlay = None
        self._artifact_sink = None
        self._completed_replay_authenticator = None
        self._ephemeral_identity_factory = None
        self._public_jobs_cache = None
        self._public_jobs_cache_lock = None
        self._now = None
        return True

    @property
    def closed(self):
        return self._closed


def _ensure_candidate_workflow_owner(connection, authority, *, now):
    account_id, environment, principal_id, session_id, profile_id = (
        authority.candidate_workflow_authority()
    )
    if environment != "private_beta":
        raise local_product.ActionError(
            "Candidate workflow is unavailable for this account.",
            HTTPStatus.FORBIDDEN,
        )
    timestamp = _trusted_utc(now).replace(microsecond=0).isoformat()
    authorized = connection.execute(
        """
        SELECT 1
        FROM account_sessions AS session
        JOIN users AS account ON account.user_id = session.user_id
        JOIN product_principals AS principal
          ON principal.principal_id = ?
         AND principal.environment_namespace = ?
        JOIN principal_account_bindings AS binding
          ON binding.principal_id = principal.principal_id
         AND binding.user_id = account.user_id
         AND binding.environment_namespace = principal.environment_namespace
        JOIN current_product_profiles AS profile
          ON profile.profile_id = ?
         AND profile.principal_id = principal.principal_id
         AND profile.environment_namespace = principal.environment_namespace
        WHERE session.session_id = ?
          AND session.user_id = ?
          AND session.revoked_at IS NULL
          AND session.rotated_at IS NULL
          AND julianday(session.idle_expires_at) > julianday(?)
          AND julianday(session.absolute_expires_at) > julianday(?)
          AND account.lifecycle_status = 'active'
          AND principal.principal_type = 'account_native'
          AND principal.lifecycle_status = 'active'
          AND principal.claim_policy = 'account_native'
          AND binding.binding_role = 'owner'
          AND binding.binding_status = 'active'
          AND profile.lifecycle_status = 'active'
        LIMIT 1
        """,
        (
            principal_id,
            environment,
            profile_id,
            session_id,
            account_id,
            timestamp,
            timestamp,
        ),
    ).fetchone()
    if authorized is None:
        raise local_product.ActionError(
            "Candidate workflow authorization expired. Sign in again.",
            HTTPStatus.UNAUTHORIZED,
        )

    compatibility = connection.execute(
        "SELECT user_id, is_sample FROM user_profiles WHERE profile_id = ?",
        (profile_id,),
    ).fetchone()
    if compatibility is None:
        connection.execute(
            """
            INSERT INTO user_profiles (user_id, profile_id, display_name, is_sample)
            VALUES (?, ?, 'Authenticated profile', 0)
            """,
            (account_id, profile_id),
        )
        compatibility = connection.execute(
            "SELECT user_id, is_sample FROM user_profiles WHERE profile_id = ?",
            (profile_id,),
        ).fetchone()
    if compatibility is None or (
        compatibility["user_id"] != account_id or compatibility["is_sample"] != 0
    ):
        raise local_product.ActionError(
            "Candidate workflow ownership could not be verified.",
            HTTPStatus.FORBIDDEN,
        )


def _perform_authenticated_pipeline_action(
    connection,
    *,
    form,
    run,
    owner_profile_id,
    now,
):
    action = local_product.action_form_value(form, "action")
    pipeline_id = local_product.action_form_value(
        form,
        "pipeline_item_id",
        allow_empty=True,
    )
    idempotency_key = local_product.action_form_value(form, "idempotency_key")
    call = {
        "action": action,
        "owner_profile_id": owner_profile_id,
        "idempotency_key": idempotency_key,
        "match_run_id": run.match_run_id,
        "note": local_product.action_note(action),
    }
    if action == "remind_later":
        reminder_date = (_trusted_utc(now).date() + timedelta(days=7)).isoformat()
        call["reminder_at"] = f"{reminder_date}T00:00:00+00:00"
    try:
        pipeline_records.require_pipeline_state_schema(connection)
        local_product.require_browser_pipeline_ready(connection)
        if pipeline_id:
            persisted = connection.execute(
                "SELECT profile_id FROM user_pipeline_items WHERE pipeline_item_id = ?",
                (pipeline_id,),
            ).fetchone()
            if persisted is None:
                raise local_product.ActionError(
                    "That tracker item was not found.",
                    HTTPStatus.NOT_FOUND,
                )
            if persisted["profile_id"] != owner_profile_id:
                raise local_product.ActionError(
                    "That tracker item is unavailable for this profile.",
                    HTTPStatus.FORBIDDEN,
                )
            record = local_product.normalized_browser_record(
                pipeline_records.load_pipeline_record(
                    connection,
                    pipeline_id,
                    owner_profile_id=owner_profile_id,
                    mutation_grade=True,
                )
            )
            requested_opportunity = local_product.optional_action_form_value(
                form,
                "opportunity_key",
                allow_empty=True,
            )
            if requested_opportunity:
                opportunity = local_product.resolve_run_opportunity(
                    run,
                    requested_opportunity,
                )
                if not local_product.same_record_opportunity(record, opportunity):
                    raise local_product.ActionError(
                        "That action does not match this opportunity.",
                        HTTPStatus.FORBIDDEN,
                    )
            call.update(
                action=local_product.normalized_browser_action(action, form, record),
                pipeline_item_id=pipeline_id,
                expected_version=local_product.required_expected_version(form),
            )
        else:
            if "expected_version" in form:
                raise local_product.ActionError(
                    "A new opportunity must not submit a state version."
                )
            if action not in {"save", "applied", "not_interested"}:
                raise local_product.ActionError(
                    "That action requires a tracked opportunity."
                )
            opportunity = local_product.resolve_run_opportunity(
                run,
                local_product.action_form_value(form, "opportunity_key"),
            )
            call.update(
                source=opportunity["source"],
                title=opportunity["title"],
                url=opportunity["url"],
            )
        operation = pipeline_actions.perform_pipeline_action(connection, **call)
        loaded = pipeline_records.load_pipeline_record(
            connection,
            operation.pipeline_item["pipeline_item_id"],
            owner_profile_id=owner_profile_id,
            mutation_grade=True,
        )
        record = local_product.normalized_browser_record(loaded)
        all_records = [
            local_product.normalized_browser_record(current)
            for current in pipeline_records.list_pipeline_records(
                connection,
                owner_profile_id,
                mutation_grade=True,
            )
        ]
    except local_product.ActionError:
        raise
    except pipeline_state.OwnershipError as exc:
        raise local_product.ActionError(
            "That tracker item is unavailable for this profile.",
            HTTPStatus.FORBIDDEN,
        ) from exc
    except (pipeline_state.StaleStateVersion, pipeline_state.IdempotencyConflict) as exc:
        message = (
            "This item changed since the page was loaded. Refresh and try again."
            if isinstance(exc, pipeline_state.StaleStateVersion)
            else "This action conflicts with an earlier request. Refresh and try again."
        )
        raise local_product.ActionError(message, HTTPStatus.CONFLICT) from exc
    except (pipeline_actions.UnresolvedLegacyWorkflow, pipeline_state.InvalidTransition) as exc:
        raise local_product.ActionError(str(exc), HTTPStatus.CONFLICT) from exc
    except (
        pipeline_records.PipelineRecordInvariant,
        pipeline_actions.PipelineInvariantError,
        pipeline_state.ProjectionNotInitialized,
    ) as exc:
        raise local_product.ActionError(
            "Pipeline state needs reconciliation before this action can continue.",
            HTTPStatus.SERVICE_UNAVAILABLE,
        ) from exc
    except pipeline_actions.PipelineActionValidationError as exc:
        raise local_product.ActionError(str(exc), HTTPStatus.BAD_REQUEST) from exc

    return {
        "message": (
            local_product.reminder_success_message(record["reminder_date"])
            if action == "remind_later"
            else local_product.action_success_message(action)
        ),
        "item": record,
        "source": record["source"],
        "title": record["title"],
        "url": record["url"],
        "replayed": operation.replayed,
        "all_records": all_records,
    }


def _authority_binding(
    secret,
    *,
    account_id,
    environment_namespace,
    principal_id,
    session_id,
):
    payload = json.dumps(
        {
            "account_id": account_id,
            "environment_namespace": environment_namespace,
            "principal_id": principal_id,
            "purpose": "authenticated-profile-matches-draft-v1",
            "session_id": session_id,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def _trusted_utc(value):
    if type(value) is not datetime or value.tzinfo is None:
        raise ValueError("invalid_authenticated_matches_clock")
    converted = value.astimezone(timezone.utc)
    if converted.utcoffset() is None:
        raise ValueError("invalid_authenticated_matches_clock")
    return converted


def _validated_public_origin(value):
    if type(value) is not str:
        raise ValueError("invalid_authenticated_matches_public_origin")
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise ValueError("invalid_authenticated_matches_public_origin") from None
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or parsed.netloc != parsed.netloc.lower()
    ):
        raise ValueError("invalid_authenticated_matches_public_origin")
    return value.rstrip("/"), parsed.netloc


def _parse_target(target, *, method):
    if type(target) is not str:
        return None
    try:
        if len(target.encode("utf-8")) > MAX_MATCHES_TARGET_BYTES:
            return None
        parsed = urlsplit(target)
    except (UnicodeError, ValueError):
        return None
    if (
        parsed.scheme
        or parsed.netloc
        or (
            parsed.path not in AUTHENTICATED_CANDIDATE_ROUTES
            and parsed.path != public_jobs_catalog.PUBLIC_JOBS_ROUTE
            and public_job_page.parse_public_job_path(parsed.path) is None
        )
        or parsed.fragment
        or (parsed.path == AUTHENTICATED_ACTION_ROUTE and parsed.query)
        or (method == "POST" and parsed.path != AUTHENTICATED_MATCHES_ROUTE and parsed.query)
    ):
        return None
    if public_job_page.parse_public_job_path(parsed.path) is not None:
        if not parsed.query:
            return parsed.path, {}
        try:
            raw = parse_qs(
                parsed.query,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=1,
            )
        except (UnicodeError, ValueError):
            return None
        if set(raw) != {"return_to"} or len(raw["return_to"]) != 1:
            return None
        return_to = public_jobs_catalog.validate_catalog_return_target(
            raw["return_to"][0]
        )
        return (
            (parsed.path, {"return_to": return_to})
            if return_to is not None
            else None
        )
    if parsed.path == public_jobs_catalog.PUBLIC_JOBS_ROUTE:
        params = public_jobs_catalog.parse_catalog_query(parsed.query)
        return (parsed.path, params) if params is not None else None
    if parsed.path == AUTHENTICATED_ACTION_ROUTE:
        return parsed.path, {}
    try:
        raw = (
            parse_qs(
                parsed.query,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=3,
            )
            if parsed.query
            else {}
        )
    except (UnicodeError, ValueError):
        return None
    if any(type(values) is not list or len(values) != 1 for values in raw.values()):
        return None
    params = {key: values[0] for key, values in raw.items()}
    if parsed.path == AUTHENTICATED_TRACKER_ROUTE:
        if set(params) - {"run", "view"}:
            return None
        run_id = params.get("run")
        if run_id is not None and (
            type(run_id) is not str
            or _MATCH_RUN_REFERENCE.fullmatch(run_id) is None
        ):
            return None
        view = params.get("view", "all")
        if view != local_product.normalize_tracker_view(view):
            return None
        params["view"] = view
        return parsed.path, params
    if not params:
        return parsed.path, {}
    if set(params) - {"run", "review", "edit_text"}:
        return None
    run_id = params.get("run")
    if type(run_id) is not str or _MATCH_RUN_REFERENCE.fullmatch(run_id) is None:
        return None
    modes = {key for key in ("review", "edit_text") if key in params}
    if len(modes) > 1 or any(params[key] != "1" for key in modes):
        return None
    if not modes:
        params["review"] = "1"
    return parsed.path, params


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
    if len(raw) > MAX_MATCHES_HEADERS:
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
    hosts = _header_values(items, "host")
    try:
        host_matches = len(hosts) == 1 and hmac.compare_digest(
            hosts[0].encode("ascii"),
            authority.encode("ascii"),
        )
    except (AttributeError, UnicodeError):
        host_matches = False
    return host_matches and not any(
        name.lower() in _PROXY_HEADERS
        or name.lower().startswith("x-forwarded-")
        for name, _value in items
    )


def _trusted_same_origin(items, public_origin):
    origins = _header_values(items, "origin")
    fetch_sites = _header_values(items, "sec-fetch-site")
    try:
        origin_matches = len(origins) == 1 and hmac.compare_digest(
            origins[0].encode("ascii"),
            public_origin.encode("ascii"),
        )
    except (AttributeError, UnicodeError):
        origin_matches = False
    return origin_matches and (
        not fetch_sites
        or (len(fetch_sites) == 1 and fetch_sites[0].lower() == "same-origin")
    )


def _security_cookie(header_items, name, value_pattern):
    cookie_headers = _header_values(header_items, "cookie")
    if len(cookie_headers) != 1:
        return None, False
    header = cookie_headers[0]
    try:
        encoded = header.encode("ascii")
    except UnicodeError:
        return None, False
    if not encoded or len(encoded) > MAX_MATCHES_COOKIE_BYTES:
        return None, False
    parts = header.split(";")
    if len(parts) > MAX_MATCHES_COOKIES:
        return None, False
    found = []
    for raw_part in parts:
        part = raw_part.strip(" \t")
        if not part or "=" not in part or _CONTROL_CHARACTERS.search(part):
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


def _strict_post_form(header_items, body_stream):
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
    if length < 1 or length > MAX_MATCHES_POST_BODY_BYTES:
        return None
    try:
        body = body_stream.read(length)
    except Exception:
        return None
    if type(body) is not bytes or len(body) != length:
        return None
    return local_product._strict_urlencoded_multimap(body)


def _single_form_value(form, name, *, required=True):
    values = form.get(name)
    if values is None and not required:
        return ""
    if type(values) is not list or len(values) != 1:
        raise ValueError("invalid_authenticated_matches_form")
    value = values[0]
    if type(value) is not str or value != value.strip():
        raise ValueError("invalid_authenticated_matches_form")
    return value


def _render_candidate_entry(*, run=None):
    raw_input = run.raw_input if run is not None else ""
    input_style = run.input_style if run is not None else "short_paragraph"
    edit_fields = ""
    title = "Tell us about your background"
    button = "Continue to profile review"
    if run is not None:
        edit_fields = (
            f"<input type='hidden' name='edit_run_id' value='{_safe(run.match_run_id)}'>"
            f"<input type='hidden' name='edit_review_token' value='{_safe(run.review_token)}'>"
        )
        title = "Update your background"
        button = "Review these updates"
    body = f"""
    {_navigation()}
    <section class='panel entry'>
      <p class='eyebrow'>Candidate profile</p>
      <h1>{_safe(title)}</h1>
      <p>Include your location, languages, experience, skills, and the type of work you want.</p>
      <form method='post' action='/find-matches' id='find-matches-form'>
        <label for='input_text'>About you</label>
        <textarea id='input_text' name='input_text' rows='8' required>{_safe(raw_input)}</textarea>
        <input type='hidden' name='input_style' value='{_safe(input_style)}'>
        {edit_fields}
        <button type='submit'>{_safe(button)}</button>
      </form>
    </section>
    """
    return _page("Create your profile", body)


def _render_candidate_review(run):
    review = local_product.render_structured_profile_review(
        run.canonical_profile,
        run.match_run_id,
        run.review_token,
    )
    body = f"""
    {_navigation()}
    <section class='intro'>
      <p class='eyebrow'>Review your profile</p>
      <h1>Make sure we understood you</h1>
      <p>Correct anything missing or inaccurate, then explicitly confirm the profile.</p>
    </section>
    {review}
    """
    return _page("Review your profile", body)


def _render_match_results(
    context,
    *,
    inventory_count,
    tracked=None,
    match_run_id=None,
):
    matches = local_product.build_browser_presentation_matches(
        context,
        limit=MATCH_PRESENTATION_LIMIT,
    )
    cards = []
    for match in matches:
        url = public_job_page.public_job_path_for_match(match)
        if url is None:
            continue
        reason = profile_preview.user_fit_reason(match)
        caution = local_product.product_caution_note(match)
        record = (
            local_product.demo.tracked_record_for_match(match, tracked)
            if tracked is not None
            else None
        )
        controls = (
            local_product.render_preview_card_actions(
                match,
                record,
                match_run_id,
                match["presentation_source_section"],
                "ranked-" + local_product.match_opportunity_key(match),
            )
            if match_run_id is not None
            else ""
        )
        status = (
            local_product.readable_status(record["status"])
            if record is not None
            else ""
        )
        cards.append(
            "<article class='match-card' data-action-card>"
            f"<p class='source'>{_safe(match.get('source') or 'Opportunity')}</p>"
            f"<h3>{_safe(match.get('display_title') or match.get('title') or 'Opportunity')}</h3>"
            f"<p class='muted'>{_safe(match.get('location') or 'Location not listed')}</p>"
            f"<p><strong>Why it fits:</strong> {_safe(reason)}</p>"
            + (
                f"<p class='caution'><strong>Before applying:</strong> {_safe(caution)}</p>"
                if caution
                else ""
            )
            + (
                f"<p class='pill card-status js-card-status'>{_safe(status)}</p>"
                if status
                else "<p class='pill card-status js-card-status'></p>"
            )
            + f"<p><a class='button' href='{_safe(url)}'>View opportunity</a></p>"
            + (f"<div class='js-card-controls'>{controls}</div>" if controls else "")
            + "</article>"
        )
    if cards:
        count = len(cards)
        content = (
            f"<p class='summary'><strong>{count} "
            f"{'match' if count == 1 else 'matches'}</strong></p>"
            "<section class='match-list' aria-label='Ranked matches'>"
            + "".join(cards)
            + "</section>"
        )
    elif inventory_count == 0:
        content = (
            "<section class='panel empty'><h2>No current opportunities are available</h2>"
            "<p>The configured opportunity inventory is empty. Please try again after it is refreshed.</p>"
            "</section>"
        )
    else:
        content = (
            "<section class='panel empty'><h2>No sufficiently trusted matches are available</h2>"
            "<p>The configured inventory may need a fresh successful source update. No alternate inventory was used.</p>"
            "</section>"
        )
    body = f"""
    {_navigation(match_run_id=match_run_id)}
    <header class='intro'>
      <p class='eyebrow'>Matches</p>
      <h1>Your current matches</h1>
      <p>Regenerated from your saved account profile and the configured opportunity inventory.</p>
    </header>
    <div id='action-feedback' aria-live='polite'></div>
    {content}
    """
    return _page("Your matches", body, workflow=match_run_id is not None)


def _render_authenticated_tracker(
    records,
    match_run_id,
    tracker_view,
    *,
    current_matches_available,
):
    body = (
        _navigation(
            match_run_id=match_run_id,
            show_current_matches=current_matches_available,
        )
        + local_product.render_lightweight_tracker_header(records)
        + "<div id='action-feedback' aria-live='polite'></div>"
        + local_product.render_my_jobs_workspace(
            records,
            match_run_id,
            tracker_view,
        )
    )
    return _page("My Jobs", body, workflow=True)


def _navigation(*, match_run_id=None, show_current_matches=False):
    tracker = ""
    current_matches = ""
    if match_run_id is not None:
        tracker = (
            "<a href='/tracker?"
            + urlencode({"run": match_run_id})
            + "'>My Jobs</a>"
        )
        if show_current_matches:
            current_matches = (
                "<a href='/find-matches?"
                + urlencode({"run": match_run_id})
                + "'>Current matches</a>"
            )
    profile_target = "/account/profile"
    if match_run_id is not None:
        profile_target += "?" + urlencode({"run": match_run_id})
    return (
        "<nav class='account-nav' aria-label='Account'>"
        f"<a href='{profile_target}'>My profile</a>"
        + current_matches
        + tracker
        + "<a href='/logout'>Sign out</a>"
        + "</nav>"
    )


def _public_navigation(*, authenticated, current):
    jobs_link = (
        "<a href='/jobs' aria-current='page'>Jobs</a>"
        if current == "jobs"
        else "<a href='/jobs'>Jobs</a>"
    )
    if not authenticated:
        return (
            "<nav class='account-nav' aria-label='Account'>"
            + jobs_link
            + "<a href='/find-matches'>Find matches</a>"
            "<a href='/login'>Sign in</a>"
            "</nav>"
        )
    return (
        "<nav class='account-nav' aria-label='Account'>"
        + jobs_link
        + "<a href='/find-matches'>Matches</a>"
        "<a href='/tracker'>My Jobs</a>"
        "<a href='/account/profile'>My profile</a>"
        "<a href='/logout'>Sign out</a>"
        "</nav>"
    )


def _page(title, body, *, workflow=False):
    return f"""<!doctype html>
<html lang='en'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>{_safe(title)} | Wahojobs</title>
  <style>
    :root {{ color-scheme: light; font-family: Inter, system-ui, sans-serif; color: #17211c; background: #f5f7f6; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; }}
    main {{ width: min(960px, calc(100% - 32px)); margin: 0 auto; padding: 32px 0 64px; }}
    .account-nav {{ display: flex; justify-content: flex-end; gap: 18px; margin-bottom: 20px; }}
    a {{ color: #176b52; font-weight: 700; }}
    .intro, .panel, .match-card, .review-section {{ background: #fff; border: 1px solid #d9e0dc; border-radius: 10px; padding: 22px; margin-bottom: 16px; }}
    .eyebrow, .source {{ color: #466257; font-weight: 750; }}
    h1, h2, p {{ margin-top: 0; }}
    label, .review-field {{ display: grid; gap: 6px; font-weight: 700; }}
    textarea, input, select {{ width: 100%; padding: 10px; border: 1px solid #aebbb4; border-radius: 6px; font: inherit; }}
    form {{ display: grid; gap: 14px; }}
    button, .button {{ display: inline-block; border: 0; border-radius: 6px; background: #176b52; color: #fff; padding: 10px 15px; font: inherit; font-weight: 750; cursor: pointer; text-decoration: none; }}
    .review-grid, .language-review-row {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
    .review-checks {{ display: grid; gap: 8px; margin-top: 12px; }}
    .review-checkbox {{ display: flex; gap: 8px; font-weight: 600; }}
    .review-checkbox input {{ width: auto; }}
    .review-actions {{ display: flex; align-items: center; gap: 14px; }}
    .muted {{ color: #5b6861; }}
    .caution {{ color: #7a3b24; }}
    @media (max-width: 680px) {{ .review-grid, .language-review-row {{ grid-template-columns: 1fr; }} }}
    {local_product.CSS if workflow else ''}
  </style>
</head>
<body><main>{body}</main></body>
</html>"""


def _html_response(
    status,
    content,
    *,
    referrer_policy=_NO_REFERRER_POLICY,
    extra_headers=(),
    cache_control="no-store",
    max_bytes=MAX_MATCHES_RESPONSE_BYTES,
):
    payload = content.encode("utf-8")
    if (
        type(max_bytes) is not int
        or not 1 <= max_bytes <= MAX_BROWSER_RESPONSE_BYTES
        or len(payload) > max_bytes
    ):
        return _failure_response(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Matches temporarily unavailable",
            "Matches cannot be displayed safely right now.",
        )
    if referrer_policy not in {
        _NO_REFERRER_POLICY,
        _SAME_ORIGIN_REFERRER_POLICY,
    }:
        raise ValueError("invalid_authenticated_matches_response")
    if cache_control not in {"no-store", "public, max-age=300"}:
        raise ValueError("invalid_authenticated_matches_response")
    return AuthenticatedMatchesBrowserResponse(
        int(status),
        payload,
        (
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Length", str(len(payload))),
            *_SECURITY_HEADERS,
            ("Cache-Control", cache_control),
            ("Referrer-Policy", referrer_policy),
            *extra_headers,
        ),
    )


def _form_page_response(status, content):
    return _html_response(
        status,
        content,
        referrer_policy=_SAME_ORIGIN_REFERRER_POLICY,
    )


def _redirect_response(location):
    return _html_response(
        HTTPStatus.SEE_OTHER,
        _page(
            "Continue",
            "<section class='panel'><h1>Continue</h1>"
            "<p>Your request was accepted.</p></section>",
        ),
        extra_headers=(("Location", location),),
    )


def _json_response(status, document):
    payload = json.dumps(
        document,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > MAX_MATCHES_RESPONSE_BYTES:
        return _failure_response(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Candidate workflow unavailable",
            "The response could not be returned safely.",
        )
    return AuthenticatedMatchesBrowserResponse(
        int(status),
        payload,
        (
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(payload))),
            *_SECURITY_HEADERS,
            ("Referrer-Policy", _NO_REFERRER_POLICY),
        ),
    )


def _workflow_failure(status, message, header_items, *, wants_json=None):
    if wants_json is None:
        wants_json = bool(
            any(
                "application/json" in value.lower()
                for value in _header_values(header_items, "accept")
            )
            or _header_values(
                header_items,
                local_product.INLINE_ACTION_HEADER.lower(),
            )
            == ("1",)
        )
    if wants_json:
        return _json_response(status, {"error": str(message), "ok": False})
    return _failure_response(
        status,
        "Candidate action unavailable",
        str(message),
    )


def _failure_response(status, title, message, *, extra_headers=()):
    return _html_response(
        status,
        _page(
            title,
            f"<section class='panel'><h1>{_safe(title)}</h1>"
            f"<p>{_safe(message)}</p></section>",
        ),
        extra_headers=extra_headers,
    )


def _authority_failure(state):
    if state == "authentication_required":
        return _failure_response(
            HTTPStatus.UNAUTHORIZED,
            "Authentication required",
            "Sign in to continue.",
        )
    if state == "csrf_denied":
        return _failure_response(
            HTTPStatus.FORBIDDEN,
            "Matches request rejected",
            "This request could not be verified.",
        )
    if state == "authorization_denied":
        return _failure_response(
            HTTPStatus.NOT_FOUND,
            "Matches not found",
            "This matches page is not available.",
        )
    if state == "profile_unavailable":
        return _failure_response(
            HTTPStatus.CONFLICT,
            "Profile unavailable for matching",
            "This saved profile cannot currently be used for matching.",
        )
    return _failure_response(
        HTTPStatus.SERVICE_UNAVAILABLE,
        "Matches temporarily unavailable",
        "Matches cannot be loaded safely right now.",
    )


def _safe(value):
    return html.escape(str(value or ""), quote=True)


__all__ = [
    "AUTHENTICATED_ACTION_ROUTE",
    "AUTHENTICATED_CANDIDATE_ROUTES",
    "AUTHENTICATED_MATCHES_ROUTE",
    "AUTHENTICATED_TRACKER_ROUTE",
    "AuthenticatedMatchesBrowserResponse",
    "AuthenticatedProfileMatchesBrowserIntegration",
    "AuthenticatedProfileMatchesService",
    "DurableMatchesRequestContext",
    "MatchesAuthorityResult",
]

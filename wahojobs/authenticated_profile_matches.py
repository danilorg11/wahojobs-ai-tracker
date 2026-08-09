"""Authenticated profile-to-matches composition for the durable browser.

The module owns no startup behavior and accepts every durable dependency from
runtime composition.  Candidate drafts remain bounded and process-local;
stored profiles and opportunity inventory are read only through the configured
runtime connection provider.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import html
from http import HTTPStatus
import json
import re
import secrets
import sqlite3
from urllib.parse import parse_qs, urlencode, urlsplit

from scripts import local_product_app as local_product
from scripts import profile_to_matches_preview as profile_preview
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
MAX_MATCHES_RESPONSE_BYTES = 1_048_576
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
    ("Cache-Control", "no-store"),
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
            or len(self.body) > MAX_MATCHES_RESPONSE_BYTES
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
    __slots__ = ("_draft_binding", "_profile_v2", "state")

    def __init__(self, state, *, draft_binding, profile_v2=None):
        if (
            state not in {"empty", "profile"}
            or type(draft_binding) is not str
            or re.fullmatch(r"[0-9a-f]{64}", draft_binding) is None
            or (state == "profile") != (type(profile_v2) is dict)
        ):
            raise ValueError("invalid_authenticated_matches_authority")
        self.state = state
        self._draft_binding = draft_binding
        self._profile_v2 = deepcopy(profile_v2)

    def draft_binding(self):
        return self._draft_binding

    def trusted_profile_v2(self):
        if self.state != "profile" or self._profile_v2 is None:
            raise ValueError("authenticated_matches_profile_unavailable")
        return deepcopy(self._profile_v2)

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
                    if (
                        trusted_summary.get("structured_profile_included") is not True
                        or type(profile_v2) is not dict
                    ):
                        return MatchesAuthorityResult("profile_unavailable")
                    return MatchesAuthorityResult(
                        "profile",
                        _AuthorizedMatchesState(
                            "profile",
                            draft_binding=binding,
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
        "_ephemeral_identity_factory",
        "_metadata_overlay",
        "_now",
        "_public_authority",
        "_public_origin",
        "_registry",
        "_service",
    )

    def __init__(
        self,
        service,
        *,
        connection_provider,
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
        self._metadata_overlay = metadata_overlay
        self._artifact_sink = confirmed_profile_artifact_sink
        self._completed_replay_authenticator = (
            completed_profile_confirmation_authenticator
        )
        self._public_origin = origin
        self._public_authority = authority
        self._now = now
        self._ephemeral_identity_factory = ephemeral_identity_factory
        self._registry = registry
        self._closed = False

    def matches_route(self, path):
        return not self._closed and path == AUTHENTICATED_MATCHES_ROUTE

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
        params = _parse_target(target, method=method)
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

    def _handle_get(self, params, authority):
        if authority.state == "profile":
            if params:
                return _failure_response(
                    HTTPStatus.BAD_REQUEST,
                    "Matches request unavailable",
                    "This matches request is not valid.",
                )
            return self._render_persistent_matches(authority)
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
        if run is None or not hmac.compare_digest(
            run.owner_profile_id,
            authority.draft_binding(),
        ):
            return None
        return run

    def _render_persistent_matches(self, authority):
        try:
            profile_v2 = authority.trusted_profile_v2()
            matcher_profile_id = self._ephemeral_identity_factory()
            projected = project_v2_to_matcher_v1(
                profile_v2,
                matcher_profile_id=matcher_profile_id,
            )
            rows, overlay_status = self._load_inventory()
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
            content = _render_match_results(context, inventory_count=len(rows))
            return _html_response(HTTPStatus.OK, content)
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
        self._metadata_overlay = None
        self._artifact_sink = None
        self._completed_replay_authenticator = None
        self._ephemeral_identity_factory = None
        self._now = None
        return True

    @property
    def closed(self):
        return self._closed


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
        or parsed.path != AUTHENTICATED_MATCHES_ROUTE
        or parsed.fragment
        or (method == "POST" and parsed.query)
    ):
        return None
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
    if not params:
        return {}
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
    return params


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


def _render_match_results(context, *, inventory_count):
    matches = local_product.build_browser_presentation_matches(
        context,
        limit=MATCH_PRESENTATION_LIMIT,
    )
    cards_by_section = {section: [] for section in profile_preview.SECTION_ORDER}
    for match in matches:
        url = local_product.safe_job_url(match.get("url"))
        if url is None:
            continue
        section = match.get("presentation_source_section")
        if section not in cards_by_section:
            continue
        reason = profile_preview.user_fit_reason(match)
        caution = local_product.product_caution_note(match)
        cards_by_section[section].append(
            "<article class='match-card'>"
            f"<p class='source'>{_safe(match.get('source') or 'Opportunity')}</p>"
            f"<h3>{_safe(match.get('display_title') or match.get('title') or 'Opportunity')}</h3>"
            f"<p class='muted'>{_safe(match.get('location') or 'Location not listed')}</p>"
            f"<p><strong>Why it fits:</strong> {_safe(reason)}</p>"
            + (
                f"<p class='caution'><strong>Before applying:</strong> {_safe(caution)}</p>"
                if caution
                else ""
            )
            + f"<p><a class='button' href='{_safe(url)}' target='_blank' "
            "rel='noopener noreferrer'>View opportunity</a></p>"
            "</article>"
        )
    sections = []
    for section in profile_preview.SECTION_ORDER:
        cards = cards_by_section[section]
        if not cards:
            continue
        label = profile_preview.SECTION_LABELS.get(section, section)
        sections.append(
            "<section class='match-section'>"
            f"<h2>{_safe(label)}</h2>"
            + "".join(cards)
            + "</section>"
        )
    if sections:
        count = sum(len(cards) for cards in cards_by_section.values())
        content = (
            f"<p class='summary'><strong>{count} "
            f"{'match' if count == 1 else 'matches'}</strong></p>"
            + "".join(sections)
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
    {_navigation()}
    <header class='intro'>
      <p class='eyebrow'>Matches</p>
      <h1>Your current matches</h1>
      <p>Regenerated from your saved account profile and the configured opportunity inventory.</p>
    </header>
    {content}
    """
    return _page("Your matches", body)


def _navigation():
    return (
        "<nav class='account-nav' aria-label='Account'>"
        "<a href='/account/profile'>My profile</a>"
        "<a href='/logout'>Sign out</a>"
        "</nav>"
    )


def _page(title, body):
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
):
    payload = content.encode("utf-8")
    if len(payload) > MAX_MATCHES_RESPONSE_BYTES:
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
    return AuthenticatedMatchesBrowserResponse(
        int(status),
        payload,
        (
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Length", str(len(payload))),
            *_SECURITY_HEADERS,
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
    "AUTHENTICATED_MATCHES_ROUTE",
    "AuthenticatedMatchesBrowserResponse",
    "AuthenticatedProfileMatchesBrowserIntegration",
    "AuthenticatedProfileMatchesService",
    "DurableMatchesRequestContext",
    "MatchesAuthorityResult",
]

"""Authenticated profile-to-matches composition for the durable browser.

The module owns no startup behavior and accepts every durable dependency from
runtime composition.  Candidate drafts remain bounded and process-local;
stored profiles and opportunity inventory are read only through the configured
runtime connection provider.
"""

from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import base64
import hmac
import html
from http import HTTPStatus
import json
import math
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
    pipeline_postings,
    pipeline_state,
    public_company_page,
    public_job_canary,
    public_job_page,
    public_jobs_catalog,
    public_seo,
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
from wahojobs.matching.recommendation_validity import (
    database_commit_token,
    inventory_deadline,
)
from wahojobs.matching.source_geography import apply_mercor_applicant_geography
from wahojobs.matching.typed_criteria import (
    SINGLE_CRITERION_RELAXATION_SCHEMA_VERSION,
    aggregate_single_criterion_relaxations_v1,
    bridge_existing_matcher_eligibility,
    evaluate_match_criteria_shadow,
    evaluate_primary_preference_admission_v1,
    evaluate_single_criterion_relaxations_v1,
    match_criteria_v1_from_profile,
    project_opportunity_criteria_v1,
    run_typed_match_criteria_shadow,
)
from wahojobs.opportunity_enrichment import resolve_effective_enrichments
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
from wahojobs.profiles.preference_model import (
    profile_preference_control_catalog_v1,
    profile_preference_control_catalog_v2,
)


AUTHENTICATED_MATCHES_ROUTE = "/find-matches"
AUTHENTICATED_TRACKER_ROUTE = "/tracker"
AUTHENTICATED_TRACKER_ITEM_ROUTE = "/tracker/item"
AUTHENTICATED_ACTION_ROUTE = "/action"
AUTHENTICATED_CANDIDATE_ROUTES = frozenset(
    {
        AUTHENTICATED_MATCHES_ROUTE,
        AUTHENTICATED_TRACKER_ROUTE,
        AUTHENTICATED_TRACKER_ITEM_ROUTE,
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
TYPED_PREFERENCE_ENFORCEMENT_SCHEMA_VERSION = (
    "typed_preference_enforcement_v1"
)
RELAXATION_PRESENTATION_INITIAL_LIMIT = 3

_RELAXATION_PRESENTATION_SPEC = {
    "preferences.employment_relationships": (
        "employment_relationship",
        "add_employment_relationship",
        "employment_relationships",
    ),
    "preferences.workloads": (
        "workload",
        "add_workload",
        "workloads",
    ),
    "preferences.engagement_terms": (
        "engagement_term",
        "add_engagement_term",
        "engagement_terms",
    ),
    "preferences.schedule.flexibility_modes": (
        "schedule_flexibility",
        "allow_schedule_flexibility",
        "schedule.flexibility_modes",
    ),
    "preferences.schedule.coordination_modes": (
        "schedule_coordination",
        "allow_schedule_coordination",
        "schedule.coordination_modes",
    ),
    "preferences.schedule.time_windows": (
        "schedule_time_window",
        "allow_schedule_time_window",
        "schedule.time_windows",
    ),
    "preferences.schedule.working_days": (
        "schedule_working_day",
        "allow_schedule_working_day",
        "schedule.working_days",
    ),
    "preferences.schedule.time_of_day": (
        "schedule_time_of_day",
        "allow_schedule_time_of_day",
        "schedule.time_of_day",
    ),
    "preferences.accepted_phone_voice_modes": (
        "phone_voice",
        "allow_phone_voice_mode",
        "accepted_phone_voice_modes",
    ),
    "preferences.job_interests": (
        "job_interest",
        "broaden_job_interests",
        "job_interests",
    ),
    "preferences.accepted_career_levels": (
        "career_level",
        "add_accepted_career_level",
        "accepted_career_levels",
    ),
    "preferences.compensation.minimum": (
        "compensation_minimum",
        "lower_preferred_compensation_minimum",
        None,
    ),
}
_V2_COMPENSATION_RELAXATION_CRITERION = re.compile(
    r"^preferences\.compensation_expectations\.([A-Z]{3})\."
    r"(hour|month|year)\.minimum$"
)

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

def _has_authoritative_preference_model(profile_v2):
    preferences = profile_v2.get("preferences")
    return (
        type(preferences) is dict
        and type(preferences.get("preference_model")) is dict
    )


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
        "_revision_id",
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
        revision_id=None,
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
                    revision_id,
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
        object.__setattr__(self, "_revision_id", revision_id)

    def __setattr__(self, _name, _value):
        raise AttributeError("authenticated_matches_authority_is_immutable")

    def draft_binding(self):
        return self._draft_binding

    def manual_draft_owner(self):
        values = (self._account_id, self._environment_namespace, self._principal_id)
        if any(type(value) is not str or not value for value in values):
            raise ValueError('manual_checkpoint_authority_unavailable')
        return hashlib.sha256(json.dumps(values).encode('utf-8')).hexdigest()

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

    def professional_background_context(self, evidence):
        from wahojobs.professional_background_semantics import ComparisonContext, digest
        if evidence is None or self._revision_id is None:
            return None
        owner = self.candidate_workflow_authority()
        return ComparisonContext(owner[:3], self._profile_id, self._revision_id,
                                 digest(self._profile_v2), evidence)

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
                            revision_id=trusted_summary.get("revision_id"),
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
        "_criteria_shadow_sink",
        "_professional_background_evidence",
        "_professional_background_preparer",
        "_write_connection_provider",
        "_ephemeral_identity_factory",
        "_metadata_overlay",
        "_now",
        "_public_authority",
        "_public_catalog_auth_routes_enabled",
        "_public_job_canary_gate",
        "_public_jobs_cache",
        "_public_jobs_cache_lock",
        "_public_origin",
        "_public_seo_policy",
        "_registry",
        "_reuse_namespace",
        "_service",
        "_manual_references",
        "_manual_lock",
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
        public_seo_policy=None,
        public_job_canary_gate=None,
        public_catalog_auth_routes_enabled=True,
        criteria_shadow_sink=None,
        professional_background_evidence=None,
        professional_background_preparer=None,
    ):
        origin, authority = _validated_public_origin(public_origin)
        if professional_background_preparer is not None:
            from wahojobs.professional_background_preparation import ProfessionalBackgroundPreparer
            if (type(professional_background_preparer) is not ProfessionalBackgroundPreparer
                    or professional_background_evidence is not None
                    and professional_background_evidence is not professional_background_preparer.evidence):
                raise ValueError("invalid_professional_background_preparer")
            professional_background_evidence = professional_background_preparer.evidence
        from wahojobs.professional_background_semantics import ProfessionalBackgroundEvidence
        if professional_background_evidence is not None and type(professional_background_evidence) is not ProfessionalBackgroundEvidence:
            raise ValueError("invalid_professional_background_evidence")
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
            or type(public_catalog_auth_routes_enabled) is not bool
            or (
                criteria_shadow_sink is not None
                and not callable(criteria_shadow_sink)
            )
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
        if public_seo_policy is None:
            public_seo_policy = public_seo.PublicSeoRoutePolicy.empty()
        if type(public_seo_policy) is not public_seo.PublicSeoRoutePolicy:
            raise ValueError("invalid_authenticated_matches_browser_configuration")
        if public_job_canary_gate is None:
            public_job_canary_gate = public_job_canary.PublicJobCanaryRoutingGate.disabled()
        if type(public_job_canary_gate) is not public_job_canary.PublicJobCanaryRoutingGate:
            raise ValueError("invalid_authenticated_matches_browser_configuration")
        self._service = service
        self._connection_provider = connection_provider
        self._write_connection_provider = write_connection_provider
        self._manual_references = {}
        self._manual_lock = threading.RLock()
        self._metadata_overlay = metadata_overlay
        self._artifact_sink = confirmed_profile_artifact_sink
        self._completed_replay_authenticator = (
            completed_profile_confirmation_authenticator
        )
        # A sink is the explicit diagnostic-mode opt-in.  The normal browser
        # path keeps observational criteria-shadow work off its critical path.
        self._criteria_shadow_sink = criteria_shadow_sink
        self._professional_background_evidence = professional_background_evidence
        self._professional_background_preparer = professional_background_preparer
        self._public_origin = origin
        self._public_authority = authority
        self._public_seo_policy = public_seo_policy
        self._public_job_canary_gate = public_job_canary_gate
        self._public_catalog_auth_routes_enabled = (
            public_catalog_auth_routes_enabled
        )
        self._public_jobs_cache = None
        self._public_jobs_cache_lock = threading.Lock()
        self._now = now
        self._ephemeral_identity_factory = ephemeral_identity_factory
        self._registry = registry
        self._reuse_namespace = secrets.token_hex(16)
        self._closed = False

    def prepare_professional_background(self, **selection):
        """Explicit internal operation; never called by Matches/detail routes."""
        if self._closed or self._professional_background_preparer is None:
            raise ValueError("preparation_execution_disabled")
        return self._professional_background_preparer.prepare(
            self._service, self._connection_provider, **selection)

    def matches_route(self, path):
        if self._closed or path == AUTHENTICATED_ACTION_ROUTE and self._write_connection_provider is None:
            return False
        if path == AUTHENTICATED_TRACKER_ROUTE and self._write_connection_provider is None:
            return False
        return (
            path in AUTHENTICATED_CANDIDATE_ROUTES
            or path == public_jobs_catalog.PUBLIC_JOBS_ROUTE
            or path in public_seo.PUBLIC_SEO_DOCUMENT_ROUTES
            or self._public_seo_policy.owns_path(path)
            or self._public_job_canary_gate.owns_candidate_path(path)
            or public_company_page.parse_public_company_path(path) is not None
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
        parsed_target = _parse_target(
            target,
            method=method,
            public_seo_policy=self._public_seo_policy,
            public_job_canary_gate=self._public_job_canary_gate,
        )
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
        directive = self._public_seo_policy.resolve_path(route)
        if directive is not None:
            if method not in {"GET", "HEAD"}:
                return _failure_response(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "Method not allowed",
                    "This SEO route accepts GET and HEAD only.",
                    extra_headers=(("Allow", "GET, HEAD"),),
                )
            if directive.kind == "redirect":
                return _permanent_redirect_response(directive.location)
            return _gone_response()
        if route in public_seo.PUBLIC_SEO_DOCUMENT_ROUTES:
            if method not in {"GET", "HEAD"}:
                return _failure_response(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "Method not allowed",
                    "This SEO document accepts GET and HEAD only.",
                    extra_headers=(("Allow", "GET, HEAD"),),
                )
            return self._handle_public_seo_document(route)
        if route == public_jobs_catalog.PUBLIC_JOBS_ROUTE:
            if method not in {"GET", "HEAD"}:
                return _failure_response(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "Method not allowed",
                    "The public jobs catalog accepts GET and HEAD only.",
                    extra_headers=(("Allow", "GET, HEAD"),),
                )
            return self._handle_public_jobs(params, header_items)
        if public_company_page.parse_public_company_path(route) is not None:
            if method not in {"GET", "HEAD"}:
                return _failure_response(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "Method not allowed",
                    "This public company page accepts GET and HEAD only.",
                    extra_headers=(("Allow", "GET, HEAD"),),
                )
            return self._handle_public_company(route, params, header_items)
        if (
            public_job_page.parse_public_job_path(route) is not None
            or self._public_job_canary_gate.owns_candidate_path(route)
        ):
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
                selected_job_id=params.get("variant"),
                match_run_id=params.get("run"),
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
        if route in {AUTHENTICATED_TRACKER_ROUTE, AUTHENTICATED_TRACKER_ITEM_ROUTE}:
            if method not in {"GET", "HEAD"}:
                return _failure_response(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "Method not allowed",
                    "My Jobs accepts GET and HEAD only.",
                    extra_headers=(("Allow", "GET, HEAD"),),
                )
            if authority.state != "profile":
                return _authority_failure("profile_unavailable")
            return (self._handle_tracker_item(params, authority, header_items)
                    if route == AUTHENTICATED_TRACKER_ITEM_ROUTE else self._handle_tracker(params, authority))
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
            if self._write_connection_provider is not None:
                from wahojobs import manual_profile_drafts as drafts
                with self._connection_provider() as connection:
                    saved = drafts.load(connection, authority.manual_draft_owner())
                if saved:
                    reference, payload = saved
                    run = self._registry.create(owner_profile_id=authority.draft_binding(),
                        raw_input=payload['raw_input'], input_style=payload['input_style'],
                        canonical_profile=local_product.IdentityFreeCanonicalProfileV1.from_mapping(payload['canonical']), recommendation_context=None,
                        profile_confirmed=False)
                    self._remember_manual(run, reference)
                    return self._manual_page(run, entry=payload['stage'] == 'entry')
                return self._manual_page(None, entry=True)
            return _form_page_response(HTTPStatus.OK, _render_candidate_entry())
        run = self._authorized_run(params["run"], authority)
        if run is None:
            return _candidate_failure_response(
                HTTPStatus.GONE,
                "Profile review expired",
                "That profile review is unknown or has expired.",
            )
        if params.get("edit_text") == "1":
            if self._write_connection_provider is not None:
                from wahojobs import manual_profile_drafts as drafts
                with self._connection_provider() as connection:
                    saved = drafts.load(connection, authority.manual_draft_owner())
                if saved:
                    reference, payload = saved
                    run = self._registry.create(owner_profile_id=authority.draft_binding(),
                        raw_input=payload['raw_input'], input_style=payload['input_style'],
                        canonical_profile=local_product.IdentityFreeCanonicalProfileV1.from_mapping(payload['canonical']),
                        recommendation_context=None, profile_confirmed=False)
                    self._remember_manual(run, reference)
                return self._manual_page(run, entry=True)
            return _form_page_response(
                HTTPStatus.OK,
                _render_candidate_entry(run=run),
            )
        if self._write_connection_provider is not None:
            # Autosave updates the durable unconfirmed checkpoint, not this
            # immutable run. Refresh must restore that saved material with a
            # fresh review token instead of pairing old fields with a new version.
            return self._handle_get({}, authority)
        return _form_page_response(
            HTTPStatus.OK,
            _render_candidate_review(run),
        )

    def _handle_post(self, form, authority, header_items):
        # Serialize draft version checks with confirmation within this exclusive
        # runtime. The sidecar also enforces compare-and-swap across requests.
        with self._manual_lock:
            return self._handle_candidate_post(form, authority, header_items)

    def _remember_manual(self, run, reference):
        if len(self._manual_references) >= 128:
            self._manual_references.pop(next(iter(self._manual_references)))
        self._manual_references[run.match_run_id] = reference

    def _manual_page(self, run, *, entry=False, submitted=None, issue=None, status=HTTPStatus.OK):
        from wahojobs import manual_profile_drafts as drafts
        content = (_render_candidate_entry(run=run) if entry else
                   _render_candidate_review(run, manual=True, submitted=submitted, issue=issue))
        form_id = 'find-matches-form' if entry else 'profile-review-form'
        # Both quote styles are emitted by the shared components.
        for quote in ("'", '"'):
            marker = 'id='+quote+form_id+quote
            content = content.replace(marker, marker+' data-manual-draft')
        reference = self._manual_references.get(run.match_run_id, '') if run else ''
        content = content.replace('</form>', drafts.controls(reference)+'</form>', 1)
        content = content.replace('</body>', '<script>'+drafts.SCRIPT+'</script></body>')
        return _form_page_response(status, content)

    def _manual_invalid(self, form, authority, checkpoint, status):
        """Keep authorized input visible without saving invalid material."""
        from wahojobs.profiles.correction_editor import actionable_issue
        run_id = form.get('edit_run_id', [None])
        run = self._authorized_run(run_id[0] if len(run_id) == 1 else None, authority)
        if run is not None and 'form_action' in form and status == HTTPStatus.BAD_REQUEST:
            updates = {key: values[0] for key, values in form.items() if len(values) == 1}
            self._remember_manual(run, checkpoint)
            return self._manual_page(run, submitted=form,
                issue=actionable_issue(local_product, updates), status=status)
        return _candidate_failure_response(status, 'Profile review unavailable',
            'This request could not be completed. Your saved progress is still available. Review it and try again.')

    def _save_manual(self, form, authority, *, expected, stage):
        from wahojobs import manual_profile_drafts as drafts
        if 'form_action' in form:
            draft_form = dict(form, credentials_confirmed=['1'])
            run, updates = local_product.validate_profile_review_submission(draft_form, self._registry)
            if self._authorized_run(run.match_run_id, authority, confirmation=True) is None:
                raise ValueError('manual_checkpoint_authority_unavailable')
            canonical = local_product.apply_identity_free_profile_review(run.canonical_profile, updates)
            payload = dict(raw_input=run.raw_input, input_style=run.input_style,
                           canonical=json.loads(canonical.canonical_bytes), stage='review')
        else:
            run = self._create_candidate_draft(form, authority)
            payload = dict(raw_input=run.raw_input, input_style=run.input_style,
                           canonical=json.loads(run.canonical_profile.canonical_bytes), stage=stage)
        with self._connection_provider() as connection:
            reference = drafts.save(connection, authority.manual_draft_owner(), expected, payload)
        self._remember_manual(run, reference)
        return run, reference

    def _handle_candidate_post(self, form, authority, header_items):
        if authority.state == "profile":
            return _redirect_response(AUTHENTICATED_MATCHES_ROUTE)
        action = None
        manual = self._write_connection_provider is not None
        checkpoint = ''
        try:
            checkpoint = _single_form_value(form, 'manual_checkpoint', required=False) or ''
            action = _single_form_value(form, 'manual_action', required=False)
            if action not in (None, '', 'save') or (action and not manual):
                raise ValueError('invalid_manual_action')
            form = {key: value for key, value in form.items() if key not in {'manual_checkpoint', 'manual_action'}}
            if manual and action == 'save':
                run, reference = self._save_manual(form, authority, expected=checkpoint, stage='entry')
                return _json_response(HTTPStatus.OK, {'checkpoint': reference})
            if "form_action" in form:
                run_id = _single_form_value(form, "edit_run_id")
                run = self._authorized_run(run_id, authority, confirmation=True)
                if run is None:
                    return _candidate_failure_response(
                        HTTPStatus.GONE,
                        "Profile review expired",
                        "That profile review is unknown or has expired.",
                    )
                if manual:
                    self._save_manual(form, authority, expected=checkpoint, stage='review')
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
            if manual:
                run, _reference = self._save_manual(form, authority, expected=checkpoint, stage='review')
            else:
                run = self._create_candidate_draft(form, authority)
            location = AUTHENTICATED_MATCHES_ROUTE + "?" + urlencode(
                {"run": run.match_run_id, "review": "1"}
            )
            return _redirect_response(location)
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except local_product.ActionError as exc:
            status = exc.status
            if action == 'save':
                return _json_response(status, {'error': str(exc)})
            return self._manual_invalid(form, authority, checkpoint, status) if manual else _failure_response(
                status, 'Profile review unavailable', 'This profile review could not be completed safely.')
        except (ValueError, TypeError) as exc:
            from wahojobs.manual_profile_drafts import StaleManualDraft
            if isinstance(exc, StaleManualDraft):
                if action == 'save':
                    return _json_response(HTTPStatus.CONFLICT, {'error': 'Newer progress was saved in another tab. Return to saved progress before continuing.'})
                return _candidate_failure_response(HTTPStatus.CONFLICT, 'Newer profile draft available',
                    'Resume your latest saved progress before continuing. Nothing was confirmed by this request.')
            if action == 'save':
                return _json_response(HTTPStatus.BAD_REQUEST, {'error': 'Check the profile fields and retry. Your previous saved draft is preserved.'})
            return self._manual_invalid(form, authority, checkpoint, HTTPStatus.BAD_REQUEST) if manual else _failure_response(
                HTTPStatus.BAD_REQUEST, 'Matches request unavailable', 'This matches request is not valid.')
        except Exception:
            if action == 'save':
                return _json_response(HTTPStatus.SERVICE_UNAVAILABLE, {'error': 'Draft could not be saved. Keep this page open and retry.'})
            return _candidate_failure_response(
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
                    # Durable history is read under current owner authority;
                    # a previous process's run is not required to return.
                    run_id = None
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

    def _handle_tracker_item(self, params, authority, header_items):
        records = self._load_pipeline_records(authority)
        record = next((r for r in records if r['pipeline_item_id'] == params['item']), None)
        if record is None:
            return _failure_response(HTTPStatus.NOT_FOUND, 'Saved job unavailable',
                                     'This saved job is not available for your profile.')
        record = self._with_workflow_events(record)
        job_id, canonical = record.get('_posting_job_id'), record.get('_posting_canonical_id')
        if job_id is not None and canonical is not None:
            return self._render_public_job_variant(public_job_page.public_job_path(canonical),
                header_items, selected_job_id=job_id, authority=authority, tracker_record=record)
        return self._render_tracker_fallback(record, authority)

    def _with_workflow_events(self, record):
        with self._connection_provider() as connection:
            events = connection.execute(
                'SELECT occurred_at, affected_dimension, action_name, after_state_json '
                'FROM user_pipeline_transitions WHERE pipeline_item_id=? AND profile_id=? ORDER BY id',
                (record['pipeline_item_id'], record['profile_id'])).fetchall()
        return dict(record, _workflow_events=[dict(e) for e in events])

    def _history_controls(self, record, authority):
        target = local_product.tracker_item_url(record)
        run = self._registry.create(owner_profile_id=authority.candidate_workflow_authority()[4],
            raw_input='', input_style='short_paragraph',
            recommendation_context={'matches': {}, '_workflow_return': target}, profile_confirmed=True)
        controls = ' '.join(local_product.action_form(action,
            local_product.action_label_for_record(action, record), run.match_run_id,
            pipeline_id=record['pipeline_item_id'], expected_version=record['state_version'],
            resolution_mode=local_product.resolution_mode_for_record(action, record),
            return_to=target, section='tracker_item') for action in local_product.actions_for_record(record))
        return controls

    def _render_tracker_fallback(self, record, authority):
        controls = self._history_controls(record, authority)
        history = _render_workflow_history(record)
        body = ("<section class='panel' data-action-card><h1>" + _safe(record['title']) + '</h1>'
                + "<p>Current local source details cannot be linked safely to this saved history. "
                "Your progress is preserved; no other posting has been substituted.</p>"
                + history + "<div class='js-card-controls'>" + controls + '</div>'
                + (f"<p><a href='{_safe(record['url'])}' target='_blank' rel='noopener noreferrer'>Original saved listing</a></p>"
                   if local_product.safe_job_url(record['url']) else '')
                + "<p><a href='/tracker'>Back to My Jobs</a></p></section>")
        return _form_page_response(HTTPStatus.OK, _page('Saved job history', body, workflow=True))

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
            section = local_product.action_form_value(form, 'section')
            if section in {'public_job', 'tracker_item'}:
                target = local_product.action_form_value(form, 'return_to')
                if (run.recommendation_context or {}).get('_workflow_return') != target:
                    raise local_product.MalformedActionRequest()
            result = self._perform_pipeline_action(form, run, authority)
            if wants_json:
                payload = local_product.action_json_payload(result, run, form)
                if section in {'public_job', 'tracker_item'}:
                    payload['workflow_history_html'] = _render_workflow_history(
                        self._with_workflow_events(result['item']))
                if (section not in {'tracker','tracker_item','public_job'}
                        and form['action'][0] in {'not_interested','show_again'}):
                    payload['matches_refresh_url'] = '/find-matches'
                return _json_response(
                    HTTPStatus.OK,
                    payload,
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
            elif section in {"public_job", "tracker_item"}:
                location = local_product.action_form_value(form, "return_to")
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

    def _handle_public_job(self, path, header_items, *, catalog_return_to=None,
                           selected_job_id=None, match_run_id=None):
        authority = self._optional_public_authority(header_items)
        options = dict(catalog_return_to=catalog_return_to, selected_job_id=selected_job_id,
                       match_run_id=match_run_id, authority=authority)
        return self._render_public_job_variant(path, header_items, **options)

    def _render_public_job_variant(self, path, header_items, *, catalog_return_to=None,
                                  selected_job_id=None, match_run_id=None, authority=None, tracker_record=None):
        try:
            connection = None
            route_decision = None
            canonical_opportunity_id = public_job_page.parse_public_job_path(path)
            authenticated = authority is not None and authority.state == "profile"
            prepared = self._professional_background_evidence if authenticated else None
            generation = prepared.generation_token if prepared is not None else None
            if (selected_job_id is not None or match_run_id is not None) and not authenticated:
                return _failure_response(HTTPStatus.UNAUTHORIZED, "Sign in required",
                                         "Sign in to view this recommendation.")
            selected_match = None
            snapshot = None
            local_checks = None
            membership_known = False
            evaluated_at = _trusted_utc(self._now())
            run = None
            if authenticated:
                run = self._authorized_run(match_run_id, authority) if match_run_id else None
                if match_run_id and run is None:
                    return _failure_response(HTTPStatus.NOT_FOUND, 'Saved matches unavailable',
                                             'Return to your current matches or My Jobs.')
                profile_v2 = authority.trusted_profile_v2()
                records = self._load_pipeline_records(authority) if self._write_connection_provider is not None else []
                inputs = self._recommendation_input_key(profile_v2, authority,
                    hidden_ids=pipeline_postings.hidden_job_ids(records))
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
                    if canonical_opportunity_id is None:
                        route_decision = (
                            self._public_job_canary_gate.resolve_registered_path(
                                connection,
                                path,
                            )
                        )
                        canonical_opportunity_id = (
                            route_decision.canonical_opportunity_id
                            if route_decision is not None
                            and route_decision.kind == "serve"
                            else None
                        )
                    elif (self._public_job_canary_gate.enabled and tracker_record is None
                          and selected_job_id is None):
                        route_decision = self._public_job_canary_gate.resolve_canonical(
                            connection,
                            canonical_opportunity_id,
                        )

                    if (
                        public_job_page.parse_public_job_path(path) is not None
                        and route_decision is not None
                    ):
                        job = None
                    elif route_decision is not None and route_decision.kind != "serve":
                        job = None
                    elif canonical_opportunity_id is not None:
                        if authenticated:
                            from wahojobs.authenticated_variant_details import load_scoped_snapshot
                            snapshot = load_scoped_snapshot(connection, canonical_opportunity_id,
                                                            selected_job_id, now=evaluated_at)
                            snapshot['_workflow_bindings'] = {r['job_id']: pipeline_postings.source_binding(connection, r['job_id'])
                                for r in (snapshot.get('detail_evidence') or {}).get('rows', [])}
                            if self._professional_background_evidence is not None:
                                from wahojobs.professional_background_semantics import accepted_source_binding
                                snapshot["task_sources"] = accepted_source_binding(connection, snapshot["task_sources"])
                            job = None
                        else:
                            job = public_job_page.load_public_job(
                                connection, public_job_page.public_job_path(canonical_opportunity_id),
                                now=evaluated_at, selected_job_id=selected_job_id)
                        if job is not None and route_decision is not None:
                            job["path"] = route_decision.primary_path
                    else:
                        job = None
                finally:
                    if connection.in_transaction:
                        connection.rollback()
            if snapshot is not None:
                from wahojobs.authenticated_variant_details import resolve_scoped_variant, find_presented_variant
                job, local_checks = resolve_scoped_variant(
                    snapshot, profile_v2, self._metadata_overlay, selected_job_id, now=evaluated_at,
                    background_context=authority.professional_background_context(self._professional_background_evidence))
                membership_known = self._can_reuse_recommendations(
                    run, inputs, snapshot["token"], _trusted_utc(self._now()))
                if job is not None and membership_known:
                    selected_match = find_presented_variant(
                        run.recommendation_context, canonical_opportunity_id, job["job_id"])
                if job is not None and route_decision is not None:
                    job["path"] = route_decision.primary_path
            if route_decision is not None:
                if public_job_page.parse_public_job_path(path) is not None:
                    return _permanent_redirect_response(route_decision.primary_path)
                if route_decision.kind == "redirect":
                    return _permanent_redirect_response(route_decision.location)
                if route_decision.kind == "gone":
                    return _gone_response()
            if job is None:
                if tracker_record is not None:
                    return self._render_tracker_fallback(tracker_record, authority)
                return _failure_response(
                    HTTPStatus.NOT_FOUND,
                    "Job not found",
                    "This opportunity page is not available.",
                )

            workflow_enabled = (job["public_state"] == public_job_page.PUBLIC_JOB_STATE_LIVE
                                and authenticated and self._write_connection_provider is not None)
            # Scoped comparisons and saved membership are accepted together.
            # A replacement before this boundary makes the whole response fail.
            with prepared.consume_generation(generation) if prepared is not None else nullcontext():
                if authenticated:
                    from wahojobs.authenticated_variant_details import prepare_variant_notice
                    prepare_variant_notice(job, selected_match, local=local_checks,
                                           membership_known=membership_known)
                controls = ""
                status = ""
                record = tracker_record
                if authenticated and record is None:
                    record = local_product.demo.tracked_record_for_match(job['workflow_match'],
                        local_product.demo.build_tracked_index(records))
                if record is not None and self._write_connection_provider is not None:
                    record = self._with_workflow_events(record)
                    controls = self._history_controls(record, authority)
                    status = local_product.readable_status(record['status'])
                elif workflow_enabled:
                    match = job["workflow_match"]
                    match = dict(match, _workflow_posting_id=job['job_id'],
                                 _workflow_source_binding=snapshot['_workflow_bindings'][job['job_id']])
                    from wahojobs.authenticated_variant_details import variant_detail_url
                    target = variant_detail_url(match)
                    context = {"matches": {"do_these_first": [match]}, '_workflow_return': target}
                    run = self._registry.create(
                        owner_profile_id=authority.candidate_workflow_authority()[4],
                        raw_input="",
                        input_style="short_paragraph",
                        recommendation_context=context,
                        profile_confirmed=True,
                    )
                    controls = local_product.render_preview_full_forms(
                        match,
                        record,
                        run.match_run_id,
                        target,
                        "public_job",
                    )
                    if job['job_id'] in local_product.demo.build_tracked_index(records)['ambiguous_job_ids']:
                        controls = "<p>Separate histories are linked to this posting. Review each in <a href='/tracker'>My Jobs</a>.</p>"

                if authenticated:
                    from wahojobs.authenticated_source_detail import render_authenticated_job_page
                    content = render_authenticated_job_page(
                        job, profile=profile_v2,
                        navigation=_public_navigation(authenticated=True, current="job"),
                        workflow_controls=controls, workflow_status=status,
                        workflow_history=_render_workflow_history(record) if record is not None else '',
                        tracker_return=tracker_record is not None,
                        catalog_return_to=catalog_return_to,
                        return_run_id=match_run_id if membership_known else None)
                else:
                    content = public_job_page.render_public_job_page(
                        job, public_origin=self._public_origin, authenticated=False,
                        navigation=_public_navigation(authenticated=False, current="job"),
                        catalog_return_to=catalog_return_to)
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
                    robots_directive=(
                        "noindex, follow"
                        if catalog_return_to
                        or job["public_state"]
                        != public_job_page.PUBLIC_JOB_STATE_LIVE
                        else None
                    ),
                    max_bytes=MAX_PUBLIC_JOBS_RESPONSE_BYTES,
                )
        except Exception:
            # Owner-authorized history was loaded independently of current
            # source evidence. A source failure cannot erase that return path.
            if tracker_record is not None:
                return self._render_tracker_fallback(tracker_record, authority)
            return _failure_response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Job temporarily unavailable",
                "This opportunity page cannot be loaded safely right now.",
            )

    def _handle_public_jobs(self, params, header_items):
        try:
            query_present = bool(params.pop("_query_present", False))
            raw_query = params.pop("_raw_query", "")
            raw_query_present = bool(params.pop("_raw_query_present", False))
            jobs = self._load_public_jobs_inventory()

            authority = self._optional_public_authority(header_items)
            authenticated = authority is not None and authority.state == "profile"
            catalog = public_jobs_catalog.build_catalog(jobs, params)
            request_target = public_jobs_catalog.PUBLIC_JOBS_ROUTE + (
                "?" + raw_query if raw_query or raw_query_present else ""
            )
            if request_target != catalog["normalized_target"]:
                return _permanent_redirect_response(catalog["normalized_target"])
            content = public_jobs_catalog.render_public_jobs_page(
                catalog,
                public_origin=self._public_origin,
                navigation=_public_navigation(
                    authenticated=authenticated,
                    current="jobs",
                    auth_routes_enabled=(
                        self._public_catalog_auth_routes_enabled
                    ),
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
                robots_directive=(
                    "noindex, follow" if catalog["filters"] else None
                ),
            )
        except public_jobs_catalog.CatalogPageOutOfRange:
            return _failure_response(
                HTTPStatus.NOT_FOUND,
                "Jobs page not found",
                "This jobs page is not available.",
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

    def _handle_public_company(self, path, params, header_items):
        try:
            jobs = self._load_public_jobs_inventory()
            known_company = None
            if not any(job.get("company_slug") == path.rsplit("/", 1)[-1] for job in jobs):
                known_company = self._load_public_company_identity(path)
            company = public_company_page.build_public_company(
                jobs,
                path,
                page=params["page"],
                known_company=known_company,
            )
            if company is None:
                return _failure_response(
                    HTTPStatus.NOT_FOUND,
                    "Company not found",
                    "This company page is not available.",
                )
            raw_query = params.get("raw_query", "")
            request_target = path + (
                "?" + raw_query
                if raw_query or params.get("raw_query_present")
                else ""
            )
            normalized_target = public_company_page.company_page_target(
                path,
                company["catalog"]["page"],
            )
            if request_target != normalized_target:
                return _permanent_redirect_response(normalized_target)
            authority = self._optional_public_authority(header_items)
            authenticated = authority is not None and authority.state == "profile"
            content = public_company_page.render_public_company_page(
                company,
                public_origin=self._public_origin,
                navigation=_public_navigation(
                    authenticated=authenticated,
                    current="company",
                ),
                authenticated=authenticated,
                query_present=params["query_present"],
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
                robots_directive=(
                    None if company["has_current_jobs"] else "noindex, follow"
                ),
            )
        except public_jobs_catalog.CatalogPageOutOfRange:
            return _failure_response(
                HTTPStatus.NOT_FOUND,
                "Company page not found",
                "This company page is not available.",
            )
        except (sqlite3.Error, ValueError, TypeError):
            return _failure_response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Company temporarily unavailable",
                "This company page cannot be loaded safely right now.",
            )
        except Exception:
            return _failure_response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Company temporarily unavailable",
                "This company page cannot be loaded safely right now.",
            )

    def _load_public_company_identity(self, path):
        connection = None
        with self._connection_provider() as connection:
            if (
                not isinstance(connection, sqlite3.Connection)
                or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
                or connection.execute("PRAGMA query_only").fetchone()[0] != 1
                or connection.in_transaction
            ):
                raise ValueError("public_company_inventory_unavailable")
            connection.execute("BEGIN")
            try:
                return public_company_page.load_public_company_identity(
                    connection,
                    path,
                )
            finally:
                if connection.in_transaction:
                    connection.rollback()

    def _handle_public_seo_document(self, route):
        try:
            if route == public_seo.ROBOTS_ROUTE:
                return _text_response(
                    HTTPStatus.OK,
                    public_seo.render_robots(self._public_origin),
                    content_type="text/plain; charset=utf-8",
                )
            if route == public_seo.SITEMAP_INDEX_ROUTE:
                content = public_seo.render_sitemap_index(self._public_origin)
            else:
                if route == public_seo.STATIC_SITEMAP_ROUTE:
                    paths = (
                        ()
                        if self._public_seo_policy.resolve_path(
                            public_jobs_catalog.PUBLIC_JOBS_ROUTE
                        )
                        is not None
                        else (public_jobs_catalog.PUBLIC_JOBS_ROUTE,)
                    )
                else:
                    jobs = self._load_public_jobs_inventory()
                    if route == public_seo.JOBS_SITEMAP_ROUTE:
                        paths = tuple(job["path"] for job in jobs)
                    else:
                        paths = tuple(
                            sorted(
                                {
                                    public_job_page.public_company_path(
                                        job["company_slug"]
                                    )
                                    for job in jobs
                                    if self._public_seo_policy.resolve_path(
                                        public_job_page.public_company_path(
                                            job["company_slug"]
                                        )
                                    )
                                    is None
                                }
                            )
                        )
                content = public_seo.render_urlset(self._public_origin, paths)
            return _text_response(
                HTTPStatus.OK,
                content,
                content_type="application/xml; charset=utf-8",
            )
        except (sqlite3.Error, ValueError, TypeError):
            return _failure_response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "SEO document temporarily unavailable",
                "This SEO document cannot be generated safely right now.",
            )
        except Exception:
            return _failure_response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "SEO document temporarily unavailable",
                "This SEO document cannot be generated safely right now.",
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
                    if self._public_job_canary_gate.enabled:
                        for job in jobs:
                            decision = self._public_job_canary_gate.resolve_canonical(
                                connection,
                                job["canonical_opportunity_id"],
                            )
                            if decision is not None and decision.kind == "serve":
                                job["catalog_detail_target"] = decision.primary_path
                                job["catalog_detail_owner"] = "published"
                            else:
                                job["catalog_detail_target"] = job.get("official_url")
                                job["catalog_detail_owner"] = "official"
                finally:
                    if connection.in_transaction:
                        connection.rollback()

            snapshot = tuple(
                job
                for job in jobs
                if self._public_seo_policy.resolve_path(job["path"]) is None
            )
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
                records = [
                    local_product.normalized_browser_record(record)
                    for record in pipeline_records.list_pipeline_records(
                        connection,
                        profile_id,
                        mutation_grade=True,
                    )
                ]
                return pipeline_postings.link_records(connection, records)
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
            allow_fallbacks=self._write_connection_provider is None,
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

    def _render_persistent_matches(self, authority, *, run=None, return_context=False):
        try:
            prepared = self._professional_background_evidence
            generation = prepared.generation_token if prepared is not None else None
            profile_v2 = authority.trusted_profile_v2()
            background_context = authority.professional_background_context(self._professional_background_evidence)
            # The commit proof must cover owner visibility as well as source
            # rows. A Hide committed after this point invalidates the response.
            before = self._inventory_commit_token() if self._write_connection_provider is not None else None
            records = self._load_pipeline_records(authority) if self._write_connection_provider is not None else []
            hidden_ids = pipeline_postings.hidden_job_ids(records)
            if run is not None:
                expected_owner = authority.candidate_workflow_authority()[4]
                if not hmac.compare_digest(run.owner_profile_id, expected_owner):
                    raise ValueError("candidate_match_run_owner_mismatch")
            inputs = self._recommendation_input_key(profile_v2, authority, hidden_ids=hidden_ids)
            if self._write_connection_provider is None and (inputs is not None or return_context):
                before = self._inventory_commit_token()
            evaluated_at = _trusted_utc(self._now())
            reused = self._can_reuse_recommendations(run, inputs, before, evaluated_at)
            if reused:
                context = run.recommendation_context
                inventory_count = context.get("_authenticated_inventory_count")
                if type(inventory_count) is not int or inventory_count < 0:
                    raise ValueError("candidate_match_run_inventory_unavailable")
            else:
                matcher_profile_id = self._ephemeral_identity_factory()
                projected = project_v2_to_matcher_v1(
                    profile_v2,
                    matcher_profile_id=matcher_profile_id,
                )
                rows, overlay_status = self._load_inventory()
                inventory_count = len(rows)
                # Remove hidden exact postings before representative selection
                # and section caps so another eligible variant can compete.
                rows = [row for row in rows if row['job_id'] not in hidden_ids]
                authoritative_matches = (
                    [] if self._criteria_shadow_sink is not None else None
                )
                context = profile_preview.build_preview_context_from_canonical_rows(
                    projected,
                    inventory_rows=rows,
                    metadata_overlay_status=overlay_status,
                    limit=local_product.PREVIEW_MATCH_LIMIT,
                    normalizer_name="canonical_v2_projection",
                    normalization_warnings=[],
                    extraction_quality="reviewed",
                    evaluated_at=evaluated_at,
                    evaluated_match_sink=(
                        authoritative_matches.append
                        if authoritative_matches is not None
                        else None
                    ),
                )
                context = self._with_source_task_fit(context, profile_v2, background_context=background_context)
                context['_hidden_posting_ids'] = sorted(hidden_ids)
                effective_enrichments = {}
                enrichment_read_succeeded = True
                if (_has_authoritative_preference_model(profile_v2)
                        or self._criteria_shadow_sink is not None):
                    try:
                        enrichment_rows = (
                            rows if self._criteria_shadow_sink is not None
                            else _typed_preference_candidate_rows(context, rows)
                        )
                        effective_enrichments = self._load_shadow_enrichments(enrichment_rows)
                    except Exception:
                        effective_enrichments = {}
                        enrichment_read_succeeded = False
                if authoritative_matches is not None:
                    self._emit_criteria_shadow(
                        profile_v2,
                        rows,
                        authoritative_matches,
                        effective_enrichments=effective_enrichments,
                    )
                if _has_authoritative_preference_model(profile_v2):
                    context = _apply_typed_preference_enforcement_v1(
                        profile_v2,
                        context,
                        rows,
                        effective_enrichments,
                    )
                after = self._inventory_commit_token() if inputs is not None or return_context else None
                if before is not None and after is not None and before != after:
                    # Do not publish a computation spanning different commits.
                    # The next request can retry against the new inventory.
                    raise ValueError("candidate_match_inventory_changed_during_evaluation")
                if before is not None and before == after and enrichment_read_succeeded:
                    context["_authenticated_reuse"] = {
                        "inputs": inputs,
                        "inventory": after,
                        "evaluated_at": evaluated_at,
                        "valid_until": inventory_deadline(
                            rows, evaluated_at,
                            recent_cache_hours=local_product.RECENT_CACHED_MATCH_MAX_AGE_HOURS,
                        ),
                    }
            dependencies = self._professional_background_dependencies(context, profile_v2, background_context)
            if return_context:
                proof = context.get("_authenticated_reuse")
                if (type(proof) is dict and not
                        proof["evaluated_at"] <= _trusted_utc(self._now()) < proof["valid_until"]):
                    raise ValueError("candidate_match_expired_during_detail_selection")
                with prepared.consume_generation(generation, dependencies=dependencies) if prepared is not None else nullcontext():
                    return context
            context = self._with_card_evidence(context, profile_v2, background_context=background_context)
            if self._write_connection_provider is not None and not reused:
                context = self._bind_workflow_context(context)
            proof = context.get('_authenticated_reuse')
            if proof and proof['inventory'] != self._inventory_commit_token():
                raise ValueError('candidate_match_inventory_changed_during_binding')
            # Reuse/comparison above is tentative. Accept and register exactly
            # that generation under the publisher's lock; never retry in a GET.
            with prepared.consume_generation(generation, dependencies=dependencies) if prepared is not None else nullcontext():
                if self._write_connection_provider is None:
                    content = _render_match_results(context, inventory_count=inventory_count)
                else:
                    proof = context.get("_authenticated_reuse")
                    if (type(proof) is dict
                            and proof["valid_until"] > proof["evaluated_at"]
                            and not proof["evaluated_at"] <= _trusted_utc(self._now()) < proof["valid_until"]):
                        # A clock boundary crossed during calculation/render preparation.
                        # Do not publish or register a result evaluated before that boundary.
                        raise ValueError("candidate_match_expired_during_evaluation")
                    if not reused:
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

    def _professional_background_dependencies(self, context, profile_v2, background_context):
        """Rebuild only the exact requests whose results this context consumed.

        Source/profile work precedes the publication guard. The provider then
        revalidates these records and outputs in its acceptance transaction;
        refreshed card explanation alone cannot validate cached admission.
        """
        if background_context is None or not background_context.evidence.requires_dependency_validation:
            return ()
        from wahojobs.authenticated_card_evidence import load_card_sources, prepare_card_evidence
        from wahojobs.professional_background_semantics import accepted_source_binding, build_request
        candidates = []
        for matches in context['matches'].values():
            for match in matches:
                semantics = [row.get('components', {}).get('occupational_relevance', {}).get('semantic')
                             for row in match.get('source_qualification_comparisons', ())]
                semantics = [value for value in semantics if value is not None]
                if semantics:
                    candidates.append((match, semantics))
        if not candidates:
            return ()
        with self._connection_provider() as connection:
            if connection.in_transaction or connection.execute('PRAGMA query_only').fetchone()[0] != 1:
                raise ValueError('source_task_evidence_read_unavailable')
            connection.execute('BEGIN')
            try:
                sources = accepted_source_binding(connection,
                    load_card_sources(connection, [match for match, _ in candidates]))
            finally:
                connection.rollback()
        dependencies = []
        for match, semantics in candidates:
            packet = prepare_card_evidence(match, sources.get(match['job_id']), profile_v2)
            requests = {}
            for comparison in packet.get('comparisons', ()):
                request = build_request(packet, comparison, profile_v2, background_context)
                if request is not None:
                    requests[request['request_id']] = request
            for semantic in semantics:
                request = requests.get(semantic['request_id'])
                if request is None or any(semantic.get(k) != getattr(background_context.evidence, k)
                                          for k in ('basis', 'recipe', 'model')):
                    raise ValueError('professional_evidence_dependency_invalid')
                dependencies.append((request, {k: v for k, v in semantic.items()
                                               if k not in ('basis', 'recipe', 'model')}))
        return tuple(dependencies)

    def _with_source_task_fit(self, context, profile_v2, *, background_context=None):
        from wahojobs.authenticated_card_evidence import load_card_sources
        from wahojobs.matching.source_task_fit import apply_source_task_fit
        from wahojobs.matching.accepted_tasks import needs_accepted_task_comparison
        # Existing pre-admission representatives, including those below the UI
        # limit. No catalog re-scoring or inference from another variant.
        candidates = [m for m in _ranked_presentation_eligible_pool(context) + _conditional_presentation_pool(context)
                      if m.get("matched_languages") or m.get("accepted_task_fit")]
        # Inspect existing bounded representatives excluded only by an
        # unmodeled title. Neither canonical choice nor section limits change.
        candidates += [m for values in context['matches'].values() for m in values
                       if needs_accepted_task_comparison(m)]
        if not candidates:
            return context
        with self._connection_provider() as connection:
            if connection.in_transaction or connection.execute("PRAGMA query_only").fetchone()[0] != 1:
                raise ValueError("source_task_evidence_read_unavailable")
            connection.execute("BEGIN")
            try:
                sources = load_card_sources(connection, candidates)
                if background_context is not None:
                    from wahojobs.professional_background_semantics import accepted_source_binding
                    sources = accepted_source_binding(connection, sources)
            finally:
                connection.rollback()
        ids = {m["job_id"] for m in candidates}
        return dict(context, matches={section: [
            apply_source_task_fit(m, sources.get(m.get("job_id")), profile_v2, background_context=background_context)
            if m.get("job_id") in ids else m for m in values]
            for section, values in context["matches"].items()})

    def _with_card_evidence(self, context, profile_v2, *, background_context=None):
        # Presentation-only enrichment of the final visible IDs, never the pool.
        from wahojobs.authenticated_card_evidence import load_card_sources, prepare_card_evidence
        conditional = _conditional_presentation_matches(context)
        conditional_ids = {match["job_id"] for match in conditional}
        matches = _primary_presentation_matches(context) + conditional
        sources = {}
        if matches:
            try:
                with self._connection_provider() as connection:
                    if (connection.in_transaction
                            or connection.execute("PRAGMA query_only").fetchone()[0] != 1):
                        raise ValueError("card_evidence_read_unavailable")
                    connection.execute("BEGIN")
                    try:
                        sources = load_card_sources(connection, matches)
                        if background_context is not None:
                            from wahojobs.professional_background_semantics import accepted_source_binding
                            sources = accepted_source_binding(connection, sources)
                    finally:
                        connection.rollback()
            except (sqlite3.Error, ValueError, TypeError):
                # Missing evidence changes the explanation, never admission.
                sources = {}
        return dict(context, _card_evidence={
            match["job_id"]: prepare_card_evidence(match, sources.get(match["job_id"]), profile_v2,
                include_item_experience=True, conditional_placement=match["job_id"] in conditional_ids,
                background_context=background_context)
            for match in matches
        })

    def _bind_workflow_context(self, context):
        with self._connection_provider() as connection:
            connection.execute('BEGIN')
            try:
                return dict(context, matches={section: [pipeline_postings.bind_match(connection, match)
                    for match in matches] for section, matches in context['matches'].items()})
            finally:
                connection.rollback()

    def _recommendation_input_key(self, profile_v2, authority, *, hidden_ids=()):
        from wahojobs.matching.accepted_tasks import TASK_PROJECTION_VERSION, SOURCE_ELIGIBILITY_VERSION, TASK_ADMISSION_VERSION
        if self._write_connection_provider is None or self._criteria_shadow_sink is not None:
            return None
        owner = authority.candidate_workflow_authority()
        from wahojobs.professional_background_duration import VERSION as background_version
        from wahojobs.professional_background_semantics import SEMANTIC_VERSION, model_identity_policy_digest
        prepared = self._professional_background_evidence
        # Hash the small trusted profile/configuration, never the inventory.
        document = {
            "hidden_exact_postings": sorted(hidden_ids),
            "profile": profile_v2,
            "overlay": self._metadata_overlay.records_by_key,
            "overlay_path": str(self._metadata_overlay.path),
            "preview_limit": local_product.PREVIEW_MATCH_LIMIT,
            "presentation_limit": MATCH_PRESENTATION_LIMIT,
            "recent_cache_hours": local_product.RECENT_CACHED_MATCH_MAX_AGE_HOURS,
            "source_task_fit_version": 3,
            "confirmed_activity_signal_version": 1,
            "accepted_task_projection_version": TASK_PROJECTION_VERSION,
            "accepted_task_admission_version": TASK_ADMISSION_VERSION,
            "professional_background_version": background_version,
            "professional_background_semantic_version": SEMANTIC_VERSION,
            "professional_model_identity_policy": model_identity_policy_digest(),
            "professional_background_revision": authority._revision_id,
            "professional_background_evidence": (dict(recipe=prepared.recipe, model=prepared.model,
                basis=prepared.basis, generation=prepared.generation, instance=id(prepared)) if prepared is not None else None),
            "accepted_source_eligibility_version": SOURCE_ELIGIBILITY_VERSION,
        }
        digest = hashlib.sha256(json.dumps(
            document, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest()
        return (self._reuse_namespace, id(self._connection_provider),
                owner[:3], owner[4], digest)

    def _inventory_commit_token(self):
        try:
            with self._connection_provider() as connection:
                if (not isinstance(connection, sqlite3.Connection)
                        or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
                        or connection.execute("PRAGMA query_only").fetchone()[0] != 1
                        or connection.in_transaction):
                    return None
                connection.execute("BEGIN")
                try:
                    return database_commit_token(connection)
                finally:
                    connection.rollback()
        except (OSError, sqlite3.Error, ValueError, TypeError):
            return None

    @staticmethod
    def _can_reuse_recommendations(run, inputs, inventory, now):
        if run is None or inputs is None or inventory is None:
            return False
        proof = (run.recommendation_context or {}).get("_authenticated_reuse")
        try:
            return (type(proof) is dict
                    and proof.get("inputs") == inputs
                    and proof.get("inventory") == inventory
                    and type(proof.get("evaluated_at")) is datetime
                    and type(proof.get("valid_until")) is datetime
                    and proof["evaluated_at"] <= now < proof["valid_until"])
        except (TypeError, ValueError):
            return False

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
                    rows = apply_mercor_applicant_geography(connection, rows)
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

    def _emit_criteria_shadow(
        self,
        profile_v2,
        rows,
        authoritative_matches,
        *,
        effective_enrichments=None,
    ):
        """Best-effort diagnostics that cannot affect the visible match context."""
        if self._criteria_shadow_sink is None:
            return
        try:
            if effective_enrichments is None:
                effective = {}
            else:
                effective = effective_enrichments
            if effective_enrichments is None and _has_authoritative_preference_model(
                profile_v2
            ):
                try:
                    effective = self._load_shadow_enrichments(rows)
                except Exception:
                    effective = {}
            run_typed_match_criteria_shadow(
                profile_v2,
                rows,
                effective,
                authoritative_matches=authoritative_matches,
                diagnostic_sink=self._criteria_shadow_sink,
            )
        except Exception:
            # Shadow diagnostics are strictly fail-open relative to the already
            # completed production matcher evaluation.
            return

    def _load_shadow_enrichments(self, rows):
        canonical_ids = {
            row.get("canonical_opportunity_id")
            for row in rows
            if type(row.get("canonical_opportunity_id")) is int
        }
        if not canonical_ids:
            return {}
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
                    return resolve_effective_enrichments(connection, canonical_ids)
                finally:
                    if connection.in_transaction:
                        connection.rollback()
        finally:
            connection = None

    def close(self):
        if self._closed:
            return True
        self._closed = True
        self._registry = None
        self._reuse_namespace = None
        self._service = None
        self._connection_provider = None
        self._write_connection_provider = None
        self._metadata_overlay = None
        self._artifact_sink = None
        self._completed_replay_authenticator = None
        self._criteria_shadow_sink = None
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
    section = local_product.action_form_value(form, 'section')
    requested_key = local_product.optional_action_form_value(form, 'opportunity_key', allow_empty=True)
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
        opportunity, posting = None, None
        if requested_key:
            opportunity = local_product.resolve_run_opportunity(run, requested_key)
            posting = pipeline_postings.current_action_posting(connection, opportunity, now=now)
            if posting is None:
                raise local_product.ActionError('This source changed or is no longer available. Open current details or My Jobs and try again.', HTTPStatus.CONFLICT)
        elif section not in {'tracker', 'tracker_item'}:
            raise local_product.MalformedActionRequest()
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
            if posting is not None:
                linked, _ = pipeline_postings.resolve_record(connection, record)
                if linked is None or linked['id'] != posting['id']:
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
            if posting is None:
                raise local_product.MalformedActionRequest()
            # A form rendered before another action must not silently attach to
            # or overwrite a newer/legacy history. Exact duplicate submissions
            # continue to replay through the orchestrator's commit marker.
            existing = pipeline_postings.link_records(connection, [local_product.normalized_browser_record(r)
                for r in pipeline_records.list_pipeline_records(connection, owner_profile_id, mutation_grade=True)])
            linked = [r for r in existing if r.get('_posting_job_id') == posting['id']]
            expected_id = pipeline_postings.item_id(owner_profile_id, posting)
            if linked and (len(linked) != 1 or linked[0]['pipeline_item_id'] != expected_id):
                raise local_product.ActionError('This posting already has saved history. Open My Jobs to review it.', HTTPStatus.CONFLICT)
            call.update(
                source=opportunity["source"],
                title=opportunity["title"],
                url=opportunity["url"],
                posting_job_id=posting['id'],
                opportunity_external_id=posting['external_id'] or '',
                canonical_id=posting['canonical_opportunity_id'],
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
        all_records = pipeline_postings.link_records(connection, all_records)
        record = next(r for r in all_records if r['pipeline_item_id'] == record['pipeline_item_id'])
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


def _parse_target(
    target,
    *,
    method,
    public_seo_policy=None,
    public_job_canary_gate=None,
):
    if public_seo_policy is None:
        public_seo_policy = public_seo.PublicSeoRoutePolicy.empty()
    if type(public_seo_policy) is not public_seo.PublicSeoRoutePolicy:
        return None
    if public_job_canary_gate is None:
        public_job_canary_gate = public_job_canary.PublicJobCanaryRoutingGate.disabled()
    if type(public_job_canary_gate) is not public_job_canary.PublicJobCanaryRoutingGate:
        return None
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
            and parsed.path not in public_seo.PUBLIC_SEO_DOCUMENT_ROUTES
            and not public_seo_policy.owns_path(parsed.path)
            and not public_job_canary_gate.owns_candidate_path(parsed.path)
            and public_company_page.parse_public_company_path(parsed.path) is None
            and public_job_page.parse_public_job_path(parsed.path) is None
        )
        or parsed.fragment
        or not public_jobs_catalog.valid_query_encoding(parsed.query)
        or (
            parsed.path in public_seo.PUBLIC_SEO_DOCUMENT_ROUTES
            and parsed.query
        )
        or (parsed.path == AUTHENTICATED_ACTION_ROUTE and parsed.query)
        or (
            method == "POST"
            and parsed.path != AUTHENTICATED_MATCHES_ROUTE
            and parsed.query
            and not public_seo_policy.owns_path(parsed.path)
        )
    ):
        return None
    if public_seo_policy.owns_path(parsed.path):
        return parsed.path, {}
    if parsed.path in public_seo.PUBLIC_SEO_DOCUMENT_ROUTES:
        return parsed.path, {}
    if public_company_page.parse_public_company_path(parsed.path) is not None:
        params = public_company_page.parse_company_query(parsed.query)
        if params is not None:
            params["raw_query_present"] = "?" in target
        return (parsed.path, params) if params is not None else None
    if (
        public_job_page.parse_public_job_path(parsed.path) is not None
        or public_job_canary_gate.owns_candidate_path(parsed.path)
    ):
        from wahojobs.authenticated_variant_details import parse_variant_query
        params = parse_variant_query(parsed.query)
        return (parsed.path, params) if params is not None else None
    if parsed.path == public_jobs_catalog.PUBLIC_JOBS_ROUTE:
        params = public_jobs_catalog.parse_catalog_query(parsed.query)
        if params is not None:
            params["_raw_query_present"] = "?" in target
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
    if parsed.path == AUTHENTICATED_TRACKER_ITEM_ROUTE:
        item = params.get('item')
        if set(params) != {'item'} or not item or len(item) > 256 or item != item.strip():
            return None
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in item):
            return None
        return parsed.path, params
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


def _render_candidate_review(run, *, manual=False, submitted=None, issue=None):
    if manual:
        from wahojobs.profiles.correction_editor import render_editor
        review = render_editor(local_product, run.canonical_profile, run.match_run_id, run.review_token,
            action=AUTHENTICATED_MATCHES_ROUTE,
            back_url=AUTHENTICATED_MATCHES_ROUTE+'?'+urlencode({'run':run.match_run_id,'edit_text':'1'}),
            education=run.canonical_profile.get('education') or {},
            form_defaults=local_product.profile_review_form_fields(run.canonical_profile, run.match_run_id, run.review_token),
            submitted=submitted, issue=issue, manual_draft=True)
    else:
        review = local_product.render_structured_profile_review(
        run.canonical_profile,
        run.match_run_id,
        run.review_token,
        submit_label="Confirm reviewed details",
    )
    review = review.replace('Confirmed by you', 'Entered by you; not yet confirmed')
    body = f"""
    {_navigation()}
    <section class='intro'>
      <p class='eyebrow'>Review your profile</p>
      <h1>Make sure we understood you</h1>
      <p>Correct anything missing or inaccurate, then explicitly confirm the profile. Leave unknown information blank. Your next step saves the confirmed profile.</p>
    </section>
    {review}
    """
    return _page("Review your profile", body)


def _apply_typed_preference_enforcement_v1(
    profile_v2,
    context,
    inventory_rows,
    effective_enrichments,
):
    """Remove disallowed items before the ranked pool receives its UI limit."""
    criteria = match_criteria_v1_from_profile(profile_v2)
    if criteria.source_status != "present":
        return context
    ranked_pool = _ranked_presentation_eligible_pool(context) + _conditional_presentation_pool(context)
    rows_by_job_id = _unique_inventory_rows_by_job_id(inventory_rows)
    candidate_references = []
    surviving_references = []
    evaluations = []
    relaxation_candidates = []
    for original_rank, match in enumerate(ranked_pool, start=1):
        reference = _typed_presentation_reference(match)
        if reference is None:
            continue
        candidate_references.append(reference)
        outcomes = []
        opportunity = None
        eligibility_outcomes = ()
        preference_evaluation_status = "unavailable"
        job_id = _typed_match_job_id(match)
        row = rows_by_job_id.get(job_id)
        if row is not None:
            canonical_id = row.get("canonical_opportunity_id")
            effective = (
                effective_enrichments.get(canonical_id)
                if type(canonical_id) is int
                else None
            )
            try:
                opportunity = project_opportunity_criteria_v1(
                    effective_enrichment=effective,
                    inventory_row=row,
                )
                outcomes.extend(
                    evaluate_match_criteria_shadow(
                        criteria,
                        opportunity,
                    ).outcomes
                )
                preference_evaluation_status = "complete"
            except Exception:
                # Missing strict results fail closed in the admission policy;
                # missing soft results remain unknown and therefore fail open.
                preference_evaluation_status = "unavailable"
        try:
            eligibility_outcomes = bridge_existing_matcher_eligibility(match)
            outcomes.extend(eligibility_outcomes)
        except Exception:
            # The existing matcher result remains the eligibility authority.
            # A bridge diagnostic failure must not invent a new gate.
            pass
        if opportunity is not None and eligibility_outcomes and not match.get("conditional_task_fit"):
            try:
                relaxation_candidates.extend(
                    evaluate_single_criterion_relaxations_v1(
                        criteria,
                        opportunity,
                        eligibility_outcomes,
                        opportunity_reference=reference,
                        original_rank=original_rank,
                    )
                )
            except Exception:
                # Counterfactual diagnostics can never affect primary admission.
                pass
        outcomes = tuple(sorted(outcomes, key=lambda item: item.criterion_id))
        admission = evaluate_primary_preference_admission_v1(
            criteria,
            outcomes,
        )
        if admission.status == "keep":
            surviving_references.append(reference)
        evaluations.append(
            {
                "opportunity_reference": reference,
                "preference_evaluation_status": preference_evaluation_status,
                "outcomes": [item.as_dict() for item in outcomes],
                "admission": admission.as_dict(),
            }
        )

    try:
        relaxation_scenarios = aggregate_single_criterion_relaxations_v1(
            tuple(relaxation_candidates)
        )
    except Exception:
        # Bounded counterfactual aggregation is diagnostic-only.
        relaxation_scenarios = ()
    updated = dict(context)
    updated["_typed_preference_enforcement"] = {
        "schema_version": TYPED_PREFERENCE_ENFORCEMENT_SCHEMA_VERSION,
        "criteria_source_status": criteria.source_status,
        "candidate_references": candidate_references,
        "surviving_references": surviving_references,
        "evaluations": evaluations,
        "single_criterion_relaxations": {
            "schema_version": SINGLE_CRITERION_RELAXATION_SCHEMA_VERSION,
            "scenarios": [item.as_dict() for item in relaxation_scenarios],
        },
    }
    return updated


def _unique_inventory_rows_by_job_id(rows):
    result = {}
    duplicates = set()
    for row in rows:
        if type(row) is not dict:
            continue
        job_id = row.get("job_id")
        if type(job_id) is not int or job_id <= 0:
            continue
        if job_id in result:
            duplicates.add(job_id)
        else:
            result[job_id] = row
    for job_id in duplicates:
        result.pop(job_id, None)
    return result


def _typed_preference_candidate_rows(context, inventory_rows):
    """Use exactly the same reference/variant resolution as typed admission.

    Both primary admission and all single-criterion relaxations consume this
    entire pre-admission pool, including candidates past the final UI limit.
    Duplicate job IDs stay unresolved rather than selecting arbitrary evidence.
    """
    rows_by_job_id = _unique_inventory_rows_by_job_id(inventory_rows)
    return [
        rows_by_job_id[job_id]
        for match in _ranked_presentation_eligible_pool(context) + _conditional_presentation_pool(context)
        if _typed_presentation_reference(match) is not None
        and (job_id := _typed_match_job_id(match)) in rows_by_job_id
    ]


def _typed_match_job_id(match):
    job_id = match.get("job_id")
    if type(job_id) is int and job_id > 0:
        return job_id
    selected = (match.get("opportunity_trust") or {}).get(
        "selected_variant_id"
    )
    return selected if type(selected) is int and selected > 0 else None


def _typed_presentation_reference(match):
    identity = local_product.stable_opportunity_identity(match)
    if (
        type(identity) is not tuple
        or len(identity) != 2
        or identity[0] not in {"canonical", "job"}
        or type(identity[1]) is not int
        or identity[1] <= 0
    ):
        return None
    return f"{identity[0]}:{identity[1]}"


def _visible_workflow_context(context, tracked=None):
    hidden = set((context or {}).get('_hidden_posting_ids', []))
    if tracked is not None:
        hidden.update(pipeline_postings.hidden_job_ids(tracked.get('records', [])))
    def visible(match):
        record = local_product.demo.tracked_record_for_match(match, tracked) if tracked is not None else None
        return match.get('job_id') not in hidden and (record is None or record.get('visibility') != 'hidden')
    return dict(context or {}, matches={section: [m for m in matches if visible(m)]
                for section, matches in ((context or {}).get('matches') or {}).items()})


def _ranked_presentation_eligible_pool(context):
    context = _visible_workflow_context(context)
    matches_by_section = (context or {}).get("matches") or {}
    pool_bound = sum(
        len(values)
        for values in matches_by_section.values()
        if type(values) is list
    )
    if pool_bound <= 0:
        return []
    return local_product.build_browser_presentation_matches(
        context,
        limit=pool_bound,
    )


def _primary_presentation_matches(context):
    context = _visible_workflow_context(context)
    enforcement = (context or {}).get("_typed_preference_enforcement")
    if enforcement is None:
        return local_product.build_browser_presentation_matches(
            context,
            limit=MATCH_PRESENTATION_LIMIT,
        )
    if (
        type(enforcement) is not dict
        or enforcement.get("schema_version")
        != TYPED_PREFERENCE_ENFORCEMENT_SCHEMA_VERSION
        or type(enforcement.get("candidate_references")) is not list
        or type(enforcement.get("surviving_references")) is not list
        or any(
            type(value) is not str or not value
            for value in (
                enforcement.get("candidate_references", [])
                + enforcement.get("surviving_references", [])
            )
        )
    ):
        return []
    candidates = enforcement["candidate_references"]
    survivors = enforcement["surviving_references"]
    if (
        len(candidates) != len(set(candidates))
        or len(survivors) != len(set(survivors))
        or not set(survivors).issubset(candidates)
    ):
        return []
    survivor_set = set(survivors)
    return [
        match
        for match in _ranked_presentation_eligible_pool(context)
        if _typed_presentation_reference(match) in survivor_set
    ][:MATCH_PRESENTATION_LIMIT]


def _conditional_presentation_pool(context):
    context = _visible_workflow_context(context)
    bound = sum(len(v) for v in ((context or {}).get("matches") or {}).values())
    return (local_product.build_browser_presentation_matches(context, limit=bound, conditional_only=True)
            if bound else [])


def _conditional_presentation_matches(context):
    pool = _conditional_presentation_pool(context)
    enforcement = (context or {}).get("_typed_preference_enforcement")
    if enforcement is not None:
        # Use the same validated survivor document as main admission. The
        # conditional pool is evaluated by the same typed preference policy.
        if (not isinstance(enforcement, dict)
                or enforcement.get("schema_version") != TYPED_PREFERENCE_ENFORCEMENT_SCHEMA_VERSION):
            return []
        survivors = enforcement.get("surviving_references", [])
        candidates = enforcement.get("candidate_references", [])
        if (not isinstance(survivors, list) or not isinstance(candidates, list)
                or any(not isinstance(v, str) for v in survivors + candidates)
                or len(set(candidates)) != len(candidates)
                or len(set(survivors)) != len(survivors)
                or not set(survivors).issubset(candidates)):
            return []
        pool = [m for m in pool if _typed_presentation_reference(m) in survivors]
    return pool[:MATCH_PRESENTATION_LIMIT]


def _presented_relaxation_scenarios(context):
    """Validate and translate only committed-engine scenarios for rendering."""
    enforcement = (context or {}).get("_typed_preference_enforcement")
    if type(enforcement) is not dict:
        return ()
    relaxation_document = enforcement.get("single_criterion_relaxations")
    if (
        type(relaxation_document) is not dict
        or set(relaxation_document) != {"schema_version", "scenarios"}
        or relaxation_document.get("schema_version")
        != SINGLE_CRITERION_RELAXATION_SCHEMA_VERSION
        or type(relaxation_document.get("scenarios")) is not list
        or len(relaxation_document["scenarios"]) > 256
    ):
        return ()
    catalogs = (
        profile_preference_control_catalog_v1(),
        profile_preference_control_catalog_v2(),
    )
    labels_by_dimension = {}
    for catalog in catalogs:
        labels_by_dimension.update(
            {
                dimension["id"]: {
                    choice["code"]: choice["label"]
                    for choice in dimension["choices"]
                }
                for dimension in catalog["dimensions"]
            }
        )
    currencies = set(catalogs[1]["compensation"]["currencies"])
    periods = {
        item["code"] for item in catalogs[1]["compensation"]["periods"]
    }
    ranked_pool = _ranked_presentation_eligible_pool(context)
    candidate_references = enforcement.get("candidate_references")
    surviving_references = enforcement.get("surviving_references")
    if (
        type(candidate_references) is not list
        or type(surviving_references) is not list
    ):
        return ()
    candidate_set = set(candidate_references)
    survivor_set = set(surviving_references)
    scenarios = []
    for raw in relaxation_document["scenarios"]:
        scenario = _presented_relaxation_scenario(
            raw,
            ranked_pool=ranked_pool,
            candidate_references=candidate_set,
            surviving_references=survivor_set,
            labels_by_dimension=labels_by_dimension,
            currencies=currencies,
            periods=periods,
        )
        if scenario is not None:
            scenarios.append(scenario)
    return tuple(
        sorted(
            scenarios,
            key=lambda item: (
                -item["unlock_count"],
                item["best_rank"],
                item["headline"].casefold(),
            ),
        )
    )


def _presented_relaxation_scenario(
    raw,
    *,
    ranked_pool,
    candidate_references,
    surviving_references,
    labels_by_dimension,
    currencies,
    periods,
):
    required = {
        "schema_version",
        "scenario_id",
        "criterion_id",
        "dimension",
        "relaxation_type",
        "current",
        "proposed",
        "blocking_reason_code",
        "counterfactual_reason_code",
        "unlock_count",
        "unlocked_opportunities",
    }
    if (
        type(raw) is not dict
        or set(raw) != required
        or raw.get("schema_version")
        != SINGLE_CRITERION_RELAXATION_SCHEMA_VERSION
    ):
        return None
    criterion_id = raw["criterion_id"]
    compensation_basis = None
    if criterion_id in _RELAXATION_PRESENTATION_SPEC:
        dimension, relaxation_type, catalog_id = _RELAXATION_PRESENTATION_SPEC[
            criterion_id
        ]
    else:
        match = _V2_COMPENSATION_RELAXATION_CRITERION.fullmatch(criterion_id)
        if match is None:
            return None
        dimension = "compensation_minimum"
        relaxation_type = "lower_preferred_compensation_minimum"
        catalog_id = None
        compensation_basis = match.groups()
    if (
        raw.get("dimension") != dimension
        or raw.get("relaxation_type") != relaxation_type
        or type(raw.get("scenario_id")) is not str
        or len(raw["scenario_id"]) > 256
    ):
        return None

    if dimension == "compensation_minimum":
        change = _presented_compensation_relaxation(
            raw,
            currencies=currencies,
            periods=periods,
        )
        if (
            change is not None
            and compensation_basis is not None
            and (
                raw["current"].get("currency"),
                raw["current"].get("period"),
            )
            != compensation_basis
        ):
            return None
    else:
        change = _presented_choice_relaxation(
            raw,
            labels=labels_by_dimension.get(catalog_id, {}),
        )
    if change is None:
        return None

    unlocked = raw.get("unlocked_opportunities")
    if (
        type(raw.get("unlock_count")) is not int
        or type(unlocked) is not list
        or raw["unlock_count"] != len(unlocked)
        or not 1 <= len(unlocked) <= 10_000
    ):
        return None
    resolved = []
    seen_references = set()
    previous_rank = 0
    for item in unlocked:
        if (
            type(item) is not dict
            or set(item) != {"opportunity_reference", "original_rank"}
            or type(item.get("opportunity_reference")) is not str
            or type(item.get("original_rank")) is not int
        ):
            return None
        reference = item["opportunity_reference"]
        rank = item["original_rank"]
        if (
            reference in seen_references
            or reference not in candidate_references
            or reference in surviving_references
            or not previous_rank < rank <= len(ranked_pool)
            or _typed_presentation_reference(ranked_pool[rank - 1]) != reference
            or public_job_page.public_job_path_for_match(ranked_pool[rank - 1])
            is None
        ):
            return None
        seen_references.add(reference)
        previous_rank = rank
        resolved.append(ranked_pool[rank - 1])
    return {
        "scenario_id": raw["scenario_id"],
        "headline": change["headline"],
        "explanation": change["explanation"],
        "unlock_count": len(resolved),
        "best_rank": unlocked[0]["original_rank"],
        "matches": tuple(resolved),
    }


def _presented_choice_relaxation(raw, *, labels):
    current = raw.get("current")
    proposed = raw.get("proposed")
    if (
        type(current) is not dict
        or set(current) != {"accepted_values"}
        or type(current.get("accepted_values")) is not list
        or not current["accepted_values"]
        or any(value not in labels for value in current["accepted_values"])
        or len(current["accepted_values"])
        != len(set(current["accepted_values"]))
        or current["accepted_values"] != sorted(current["accepted_values"])
        or type(proposed) is not dict
        or set(proposed) != {"add_value"}
        or proposed.get("add_value") not in labels
        or proposed["add_value"] in current["accepted_values"]
        or raw.get("blocking_reason_code") != "accepted_value_absent"
        or raw.get("counterfactual_reason_code") != "accepted_value_present"
        or raw.get("scenario_id")
        != f"{raw['criterion_id']}|add|{proposed.get('add_value')}"
    ):
        return None
    label = labels[proposed["add_value"]]
    headline = _relaxation_choice_headline(raw["relaxation_type"], label)
    return {
        "headline": headline,
        "explanation": (
            f"This preview also allows {label.casefold()} opportunities. "
            "Your saved preferences stay unchanged."
        ),
    }


def _presented_compensation_relaxation(raw, *, currencies, periods):
    current = raw.get("current")
    proposed = raw.get("proposed")
    expected_fields = {"minimum_amount", "currency", "period"}
    if (
        type(current) is not dict
        or set(current) != expected_fields
        or type(proposed) is not dict
        or set(proposed) != expected_fields
        or current.get("currency") not in currencies
        or current.get("period") not in periods
        or proposed.get("currency") != current.get("currency")
        or proposed.get("period") != current.get("period")
        or raw.get("blocking_reason_code")
        != "compensation_below_preferred_minimum"
        or raw.get("counterfactual_reason_code")
        != "compensation_preferred_minimum_guaranteed"
    ):
        return None
    current_amount = _positive_presentation_decimal(current.get("minimum_amount"))
    proposed_amount = _positive_presentation_decimal(
        proposed.get("minimum_amount")
    )
    if current_amount is None or proposed_amount is None or proposed_amount >= current_amount:
        return None
    expected_id = (
        f"{raw['criterion_id']}|lower|{proposed['minimum_amount']}|"
        f"{proposed['currency']}|{proposed['period']}"
    )
    if raw.get("scenario_id") != expected_id:
        return None
    current_label = _relaxation_pay_label(
        current["minimum_amount"], current["currency"], current["period"]
    )
    proposed_label = _relaxation_pay_label(
        proposed["minimum_amount"], proposed["currency"], proposed["period"]
    )
    return {
        "headline": f"Lower your preferred pay to {proposed_label}",
        "explanation": (
            f"This preview uses {proposed_label} instead of {current_label}. "
            "Your saved preferred minimum stays unchanged."
        ),
    }


def _positive_presentation_decimal(value):
    if type(value) is not str or re.fullmatch(r"[0-9]{1,18}(?:\.[0-9]{1,2})?", value) is None:
        return None
    try:
        decimal = Decimal(value)
    except InvalidOperation:
        return None
    return decimal if decimal > 0 else None


def _relaxation_choice_headline(relaxation_type, label):
    if relaxation_type == "add_employment_relationship":
        return (
            "Also consider freelance work"
            if label == "Independent contractor / freelance"
            else f"Also consider {label.casefold()} roles"
        )
    if relaxation_type == "add_workload":
        return (
            "Open to part-time work?"
            if label == "Part-time"
            else f"Also consider {label.casefold()} work"
        )
    if relaxation_type == "add_engagement_term":
        return f"Also consider {label.casefold()} roles"
    if relaxation_type == "allow_schedule_flexibility":
        return f"Open to {label.casefold()} schedules?"
    if relaxation_type == "allow_schedule_coordination":
        return f"Open to {label.casefold()} teamwork?"
    if relaxation_type == "allow_schedule_time_window":
        return f"Open to working {label.casefold()}?"
    if relaxation_type == "allow_schedule_working_day":
        return f"Open to working {label.casefold()}?"
    if relaxation_type == "allow_schedule_time_of_day":
        return f"Open to working {label.casefold()}?"
    if relaxation_type == "allow_phone_voice_mode":
        return (
            "Open to phone or voice work?"
            if label == "Phone"
            else f"Also consider {label.casefold()} work"
        )
    if relaxation_type == "add_accepted_career_level":
        return f"Also consider {label.casefold()} roles"
    return f"Also consider {label.casefold()} opportunities"


def _relaxation_pay_label(amount, currency, period):
    symbols = {"USD": "$", "BRL": "R$", "EUR": "€", "GBP": "£"}
    prefix = symbols.get(currency)
    value = f"{prefix}{amount}" if prefix else f"{currency} {amount}"
    return f"{value}/{period}"


def _render_relaxation_section(context, *, profile_target):
    scenarios = _presented_relaxation_scenarios(context)
    if not scenarios:
        return ""
    initial = scenarios[:RELAXATION_PRESENTATION_INITIAL_LIMIT]
    additional = scenarios[RELAXATION_PRESENTATION_INITIAL_LIMIT:]
    initial_markup = "".join(
        _render_relaxation_scenario(scenario, index, profile_target)
        for index, scenario in enumerate(initial, start=1)
    )
    additional_markup = ""
    if additional:
        label = (
            "1 more way to broaden your search"
            if len(additional) == 1
            else f"{len(additional)} more ways to broaden your search"
        )
        additional_markup = (
            "<details class='more-relaxations'><summary>"
            + _safe(label)
            + "</summary><div class='more-relaxations-list'>"
            + "".join(
                _render_relaxation_scenario(
                    scenario,
                    index,
                    profile_target,
                )
                for index, scenario in enumerate(
                    additional,
                    start=RELAXATION_PRESENTATION_INITIAL_LIMIT + 1,
                )
            )
            + "</div></details>"
        )
    return (
        "<section class='relaxation-section' aria-labelledby='flexibility-title'>"
        "<p class='eyebrow'>Optional ways to broaden your search</p>"
        "<h2 id='flexibility-title'>More opportunities if you're flexible</h2>"
        "<p class='relaxation-intro'>Preview opportunities opened by changing "
        "one preference at a time. Nothing changes in your profile unless you "
        "choose to update it.</p>"
        f"<div class='relaxation-list'>{initial_markup}</div>"
        + additional_markup
        + "</section>"
    )


def _render_relaxation_scenario(scenario, index, profile_target):
    count = scenario["unlock_count"]
    count_label = (
        "1 more opportunity" if count == 1 else f"{count} more opportunities"
    )
    cards = "".join(
        _render_relaxation_preview_card(match, index, item_index)
        for item_index, match in enumerate(scenario["matches"], start=1)
    )
    return (
        "<details class='relaxation-scenario'>"
        "<summary><span class='relaxation-summary-copy'><strong>"
        + _safe(scenario["headline"])
        + "</strong><span>Preview this one change</span></span>"
        "<span class='relaxation-count'>"
        + _safe(count_label)
        + "</span></summary>"
        "<div class='relaxation-preview'>"
        f"<p class='relaxation-change'>{_safe(scenario['explanation'])}</p>"
        "<p class='relaxation-preview-note'>These opportunities keep every other "
        "saved preference in place and appear in their existing match order.</p>"
        f"<div class='relaxation-preview-list'>{cards}</div>"
        "<div class='relaxation-actions'><a class='button' href='"
        + _safe(profile_target)
        + "'>Update my preferences</a><span>This preview does not save a change.</span>"
        "</div></div></details>"
    )


def _render_relaxation_preview_card(match, scenario_index, item_index):
    from wahojobs.authenticated_variant_details import variant_detail_url
    url = variant_detail_url(match)
    title = match.get("display_title") or match.get("title") or "Opportunity"
    location = _bounded_presentation_text(match.get("location"), 120) or "Location not listed"
    compensation = _presented_match_compensation(match)["label"]
    card_id = f"relaxation-{scenario_index}-opportunity-{item_index}"
    availability_note = (
        "<p>Availability is not recently verified. Confirm it on the application page.</p>"
        if match.get("presentation_data_status") == "recently_cached" else ""
    )
    return (
        f"<article class='relaxation-preview-card' aria-labelledby='{card_id}-title'>"
        "<div><p class='relaxation-preview-label'>Additional opportunity</p>"
        f"<h3 id='{card_id}-title'>{_safe(title)}</h3>"
        f"<p>{_safe(match.get('source') or 'Opportunity')}</p>"
        "<ul aria-label='Job details'>"
        f"<li>{_safe(location)}</li><li>{_safe(compensation)}</li>"
        f"</ul>{availability_note}</div>"
        f"<a href='{_safe(url)}'>View job details</a></article>"
    )


def _render_match_results(
    context,
    *,
    inventory_count,
    tracked=None,
    match_run_id=None,
):
    from wahojobs.authenticated_variant_details import variant_detail_url
    from wahojobs.authenticated_card_evidence import render_conditions, render_opportunity_kind, render_location_context
    context = _visible_workflow_context(context, tracked)
    matches = _primary_presentation_matches(context)
    cards = []
    for match in matches:
        from wahojobs.authenticated_variant_details import variant_detail_url
        url = variant_detail_url(match, run_id=match_run_id)
        if url is None:
            continue
        caution = _candidate_match_caution(match)
        if match.get("presentation_data_status") == "recently_cached":
            caution = " ".join(filter(None, (
                "Availability is not recently verified. Confirm it on the application page.",
                caution,
            )))
        compensation = {"note": ""}
        description = ""
        title = match.get("display_title") or match.get("title") or "Opportunity"
        card_id = "match-" + str(len(cards) + 1)
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
        if tracked is not None and match.get('job_id') in tracked.get('ambiguous_job_ids', set()):
            controls = "<p>More than one saved history is linked here. Review each item in <a href='/tracker'>My Jobs</a>.</p>"
        status = (
            local_product.readable_status(record["status"])
            if record is not None
            else ""
        )
        evidence = (context.get("_card_evidence") or {}).get(match.get("job_id"))
        if evidence is not None:
            meta = [(label, value, "source-fact") for label, value in evidence['facts']]
            # Source facts replace the lossy enrichment summary only in the view.
            description = ""
            compensation = {"note": ""}
        else:
            # Missing exact-source evidence is unknown in both card and detail.
            meta = []
            description = ""
        meta_markup = "".join(
            "<li class='match-meta-item match-meta-"
            + _safe(state)
            + "'><span>"
            + _safe(label)
            + "</span><strong>"
            + _safe(value)
            + "</strong></li>"
            for label, value, state in meta
        )
        from wahojobs.authenticated_card_evidence import render_card_evidence
        evidence_markup = render_card_evidence(evidence, card_id, profile_return_to=url)
        cards.append(
            f"<article class='match-card' id='opportunity-{match['job_id']}' data-action-card aria-labelledby='{card_id}-title'>"
            "<div class='match-card-main'>"
            f"<p class='match-rank-label'>Match {len(cards) + 1}</p>"
            f"<h3 id='{card_id}-title'>{_safe(title)}</h3>"
            f"<p class='match-company'>{_safe(match.get('source') or 'Opportunity')}</p>"
            f"<ul class='match-meta' aria-label='Job details'>{meta_markup}</ul>"
            + (f"<p class='match-description'>{_safe(description)}</p>" if description else "")
            + evidence_markup
            + (
                f"<p class='pay-note'>{_safe(compensation['note'])}</p>"
                if compensation["note"]
                else ""
            )
            + (
                f"<p class='caution'><strong>Good to know:</strong> {_safe(caution)}</p>"
                if caution
                else ""
            )
            + (
                f"<p class='pill card-status js-card-status'>{_safe(status)}</p>"
                if status
                else "<p class='pill card-status js-card-status'></p>"
            )
            + "</div><div class='match-card-actions'>"
            + f"<a class='button match-primary-action' href='{_safe(url)}'>View job details</a>"
            + (f"<div class='js-card-controls'>{controls}</div>" if controls else "")
            + "</div></article>"
        )
    profile_target = "/account/profile"
    if match_run_id is not None:
        profile_target += "?" + urlencode({"run": match_run_id})
    relaxation_section = _render_relaxation_section(
        context,
        profile_target="/account/profile?correction=start",
    )
    conditional_cards = []
    for index, match in enumerate(_conditional_presentation_matches(context), 1):
        url = variant_detail_url(match, run_id=match_run_id)
        if url is None:
            continue
        packet = (context.get("_card_evidence") or {}).get(match["job_id"]) or {}
        pay = next((value for label, value in packet.get("facts", []) if label == "Pay"), "")
        record = local_product.demo.tracked_record_for_match(match, tracked) if tracked is not None else None
        controls = (local_product.render_preview_full_forms(match, record, match_run_id,
                    'conditional-' + str(match['job_id']), 'also_worth_reviewing') if match_run_id else '')
        if tracked is not None and match.get('job_id') in tracked.get('ambiguous_job_ids', set()):
            controls = "<p>Review the separate histories in <a href='/tracker'>My Jobs</a>.</p>"
        conditional_cards.append(
            f"<article class='relaxation-preview-card' data-action-card id='opportunity-{match['job_id']}'><div>"
            f"<h3>{_safe(match.get('display_title') or match.get('title'))}</h3>"
            f"<p>{_safe(match.get('source'))}</p>"
            + render_opportunity_kind(packet)
            + (f"<p>{_safe(pay)}</p>" if pay else "")
            + f"<p>{_safe(match['source_task_fit']['candidate_note'])}</p>"
            + ''.join(f"<p>{_safe(note)}</p>" for note in packet.get('language_notes', []))
            + (f"<p>{_safe(packet['geography'])}</p>" if packet.get('geography') else "")
            + render_location_context(packet)
            + render_conditions(packet, f"conditional-{match['job_id']}")
            + ("<p>Availability needs confirmation.</p>" if match.get('presentation_data_status') == 'recently_cached' else "")
            + (f"<p class='pill js-card-status'>{_safe(local_product.readable_status(record['status']))}</p>" if record else "<p class='pill js-card-status'></p>")
            + f"</div><div><a href='{_safe(url)}'>View job details</a><div class='js-card-controls'>{controls}</div></div></article>")
    if conditional_cards:
        relaxation_section = (
            "<details class='relaxation-scenario'><summary>Possibilities with conditions to check</summary>"
            "<div class='relaxation-preview-list'>" + "".join(conditional_cards) + "</div></details>"
            + relaxation_section)
    if cards:
        count = len(cards)
        summary = _visible_match_summary(count)
        has_unverified_availability = any(
            match.get("presentation_data_status") == "recently_cached"
            for match in matches
        )
        if has_unverified_availability:
            summary = (
                "We found 1 opportunity to review. It needs availability confirmation."
                if count == 1 else
                f"We found {count} opportunities to review. Some need availability confirmation."
            )
        low_result_note = (
            "<aside class='low-result-note'><strong>A focused list is useful.</strong> "
            "Review the requirements before deciding which to pursue. "
            "New matches can appear as available jobs change.</aside>"
            if count <= 3 and not has_unverified_availability
            else ""
        )
        content = (
            "<section class='match-list' aria-label='Your ranked matches'>"
            + "".join(cards)
            + "</section>"
            + low_result_note
            + relaxation_section
        )
    else:
        summary = "We don't have a current match to show yet."
        availability_copy = (
            "There are no current opportunities available to compare with your profile."
            if inventory_count == 0
            else "None of the available opportunities is a clear fit for your profile right now."
        )
        content = (
            "<section class='matches-empty' aria-labelledby='matches-empty-title'>"
            "<div><p class='eyebrow'>Your search is up to date</p>"
            "<h2 id='matches-empty-title'>No matches to show right now</h2>"
            f"<p>{_safe(availability_copy)} Matches reflect your saved profile and the opportunities currently available.</p>"
            "<p>New opportunities may appear as the market changes. You can also review your profile to make sure it reflects what you want.</p>"
            "</div><div class='empty-actions'>"
            f"<a class='button' href='{_safe(profile_target)}'>Review my profile</a>"
            "<a class='secondary-action' href='/jobs'>Browse all jobs</a>"
            "</div></section>"
            + relaxation_section
        )
    profile_context = (
        "<aside class='matches-profile-context'>"
        f"<a href='{_safe(profile_target)}'>Review profile &amp; preferences</a>"
        "</aside>"
        if cards
        else ""
    )
    body = f"""
    {_navigation(match_run_id=match_run_id)}
    <header class='matches-hero'>
      <h1>Your matches</h1>
      <p class='matches-summary'>{_safe(summary)}</p>
    </header>
    {profile_context}
    <div id='action-feedback' aria-live='polite'></div>
    {content}
    """
    return _page("Your matches", body, workflow=match_run_id is not None)


_INTERNAL_PRESENTATION_MARKERS = (
    "hard gate",
    "hard-gate",
    "matcher",
    "primary recommendation",
    "threshold",
    "trust suppression",
    "explore only",
    "capped to",
)


def _visible_match_summary(count):
    if count == 1:
        return "1 opportunity to review."
    return f"{count} opportunities to review."


def _bounded_presentation_text(value, limit):
    if type(value) is not str:
        return ""
    cleaned = " ".join(value.split())
    return cleaned[:limit].rstrip() if cleaned else ""


def _candidate_facing_evidence(value):
    text = _bounded_presentation_text(value, 320)
    lowered = text.casefold()
    if not text or any(marker in lowered for marker in _INTERNAL_PRESENTATION_MARKERS):
        return ""
    return text


def _candidate_match_explanations(match):
    explanations = []
    for field in ("affirmative_fit_why", "profile_explanation_evidence"):
        values = match.get(field) or []
        if type(values) not in {list, tuple}:
            continue
        for value in values:
            explanation = _candidate_facing_evidence(value)
            if explanation and explanation not in explanations:
                explanations.append(explanation)
            if len(explanations) == 2:
                return tuple(explanations)
        if explanations:
            return tuple(explanations)
    languages = [
        _bounded_presentation_text(language, 40).title()
        for language in (match.get("matched_languages") or [])
        if _bounded_presentation_text(language, 40)
    ]
    if languages:
        return ("Your listed languages match this role: " + ", ".join(languages[:3]) + ".",)
    return ()


def _candidate_match_caution(match):
    return _candidate_facing_evidence(local_product.product_caution_note(match))


def _presented_match_description(match):
    for field in ("short_description", "description"):
        description = _bounded_presentation_text(match.get(field), 320)
        if description:
            return description
    return ""


def _presented_match_compensation(match):
    compensation = match.get("compensation")
    if type(compensation) is not dict:
        attributes = match.get("attributes")
        compensation = (
            attributes.get("compensation") if type(attributes) is dict else None
        )
    if type(compensation) is not dict or compensation.get("disclosed") is not True:
        return {"label": "Pay not disclosed", "note": "", "state": "undisclosed"}

    currency = compensation.get("currency")
    period = compensation.get("period")
    amount_type = compensation.get("amount_type")
    minimum = compensation.get("amount_min")
    maximum = compensation.get("amount_max")
    if (
        type(currency) is not str
        or re.fullmatch(r"[A-Z]{3}", currency) is None
        or period not in {"hour", "month", "year", "project", "task"}
        or amount_type not in {"exact", "range", "from", "up_to"}
        or not _valid_presentation_amount(minimum, optional=True)
        or not _valid_presentation_amount(maximum, optional=True)
    ):
        return {
            "label": "Pay disclosed — see job details",
            "note": "The available pay details are not complete enough to summarize here.",
            "state": "disclosed-unstructured",
        }
    if amount_type == "range" and (minimum is None or maximum is None):
        return {
            "label": "Pay disclosed — see job details",
            "note": "The available pay range is incomplete.",
            "state": "disclosed-unstructured",
        }
    if minimum is not None and maximum is not None and minimum > maximum:
        return {
            "label": "Pay disclosed — see job details",
            "note": "The available pay range is inconsistent.",
            "state": "disclosed-unstructured",
        }
    if amount_type == "exact" and (
        minimum is None or maximum is None or minimum != maximum
    ):
        return {
            "label": "Pay disclosed — see job details",
            "note": "The available pay details are not complete enough to summarize here.",
            "state": "disclosed-unstructured",
        }
    if amount_type == "from" and minimum is None:
        return {
            "label": "Pay disclosed — see job details",
            "note": "The available pay details are not complete enough to summarize here.",
            "state": "disclosed-unstructured",
        }
    if amount_type == "up_to" and maximum is None:
        return {
            "label": "Pay disclosed — see job details",
            "note": "The available pay details are not complete enough to summarize here.",
            "state": "disclosed-unstructured",
        }
    structured = {
        "disclosed": True,
        "currency": currency,
        "amount_min": minimum,
        "amount_max": maximum,
        "period": period,
        "amount_type": amount_type,
        "notes": None,
    }
    label = public_job_page.compensation_label(structured)
    if not label:
        return {
            "label": "Pay disclosed — see job details",
            "note": "The available pay details are not complete enough to summarize here.",
            "state": "disclosed-unstructured",
        }
    if period in {"project", "task"}:
        return {
            "label": label,
            "note": "This pay format can't be compared directly with hourly, monthly, or yearly pay.",
            "state": "not-directly-comparable",
        }
    return {"label": label, "note": "", "state": "disclosed"}


def _valid_presentation_amount(value, *, optional):
    if value is None:
        return optional
    return type(value) in {int, float} and math.isfinite(value) and value >= 0


def _render_workflow_history(record):
    summary = local_product.render_workflow_summary(record) + local_product.render_reminder_note(record)
    events = []
    for event in record.get('_workflow_events', []):
        state = json.loads(event['after_state_json'])
        workflow = state.get('workflow_status')
        label = local_product.readable_status(workflow) if workflow else 'Progress unknown'
        visibility = 'hidden' if state.get('visibility') == 'hidden' else 'visible'
        reminder = state.get('reminder_at') or 'none'
        events.append(f"<li>{_safe(event['occurred_at'])}: {_safe(label)}, {visibility}; reminder {_safe(reminder)}.</li>")
    legacy = record.get('_posting_link_state', '')
    limitation = ("<p>More than one history is linked to this posting. Each history is kept separately.</p>"
                  if legacy == 'ambiguous_history' else '')
    return ("<div class='workflow-history'>" + summary + limitation
            + ("<details><summary>Application history</summary><ol>" + ''.join(events) + '</ol></details>' if events else '')
            + '</div>')


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
        current_matches = "<a href='/find-matches'>Matches</a>"
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


def _public_navigation(*, authenticated, current, auth_routes_enabled=True):
    jobs_link = (
        "<a href='/jobs' aria-current='page'>Jobs</a>"
        if current == "jobs"
        else "<a href='/jobs'>Jobs</a>"
    )
    if not auth_routes_enabled:
        return (
            "<nav class='account-nav' aria-label='Account'>"
            + jobs_link
            + "</nav>"
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
    from wahojobs.candidate_source_display import DISPLAY_CSS
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
    button:focus-visible, a:focus-visible, textarea:focus-visible, input:focus-visible, select:focus-visible {{ outline: 3px solid #2563eb; outline-offset: 3px; }}
    .review-grid, .language-review-row {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
    .review-checks {{ display: grid; gap: 8px; margin-top: 12px; }}
    .review-checkbox {{ display: flex; gap: 8px; font-weight: 600; }}
    .review-checkbox input {{ width: auto; }}
    .review-actions {{ display: flex; align-items: center; gap: 14px; }}
    .muted {{ color: #5b6861; }}
    .caution {{ color: #7a3b24; }}
    {local_product.CSS if workflow else ''}
    .matches-hero {{ margin: 30px 0 24px; max-width: 760px; }}
    .matches-hero h1 {{ font-size: clamp(2.35rem, 6vw, 4rem); letter-spacing: -.045em; line-height: 1.02; margin-bottom: 14px; }}
    .matches-summary {{ color: #4d5e55; font-size: 1.16rem; line-height: 1.55; margin-bottom: 0; max-width: 650px; }}
    .matches-profile-context {{ align-items: center; background: #eaf4ef; border-radius: 14px; display: flex; gap: 24px; justify-content: space-between; margin: 0 0 28px; padding: 16px 18px; }}
    .matches-profile-context div {{ display: grid; gap: 2px; }}
    .matches-profile-context span {{ color: #506058; font-size: .94rem; }}
    .matches-profile-context a {{ align-items: center; display: flex; flex: 0 0 auto; min-height: 44px; }}
    .match-list {{ display: grid; gap: 18px; margin: 0; }}
    .match-card {{ align-items: start; border: 1px solid #dce4df; border-radius: 18px; box-shadow: 0 8px 26px rgba(32, 55, 44, .055); display: grid; gap: 26px; grid-template-columns: minmax(0, 1fr) 190px; margin: 0; padding: 28px; }}
    .match-card-main {{ min-width: 0; }}
    .match-rank-label {{ color: #176b52; font-size: .76rem; font-weight: 800; letter-spacing: .055em; margin-bottom: 8px; text-transform: uppercase; }}
    .match-card h3 {{ font-size: clamp(1.3rem, 3vw, 1.65rem); line-height: 1.22; margin-bottom: 7px; }}
    .match-company {{ color: #42534a; font-size: 1rem; font-weight: 700; margin-bottom: 18px; }}
    .match-meta {{ display: flex; flex-wrap: wrap; gap: 9px; list-style: none; margin: 0 0 18px; padding: 0; }}
    .match-meta-item {{ background: #f2f5f3; border-radius: 10px; display: grid; gap: 1px; min-width: 138px; padding: 9px 11px; }}
    .match-meta-item span {{ color: #627068; font-size: .73rem; font-weight: 750; text-transform: uppercase; }}
    .match-meta-item strong {{ color: #27372f; font-size: .91rem; overflow-wrap: anywhere; }}
    .match-meta-disclosed {{ background: #eaf4ef; }}
    .match-meta-not-directly-comparable, .match-meta-disclosed-unstructured {{ background: #fbf5e9; }}
    .match-description {{ color: #536159; line-height: 1.6; max-width: 70ch; }}
    .why-match {{ background: #f2f8f5; border-radius: 12px; margin: 20px 0 0; padding: 16px 18px; }}
    .why-match h4 {{ font-size: .94rem; margin: 0 0 8px; }}
    .why-match ul {{ display: grid; gap: 5px; margin: 0; padding-left: 20px; }}
    .why-match li {{ line-height: 1.5; }}
    .pay-note {{ color: #675227; font-size: .9rem; margin: 12px 0 0; }}
    .match-card .caution {{ margin: 16px 0 0; }}
    .match-card-actions {{ display: grid; gap: 10px; }}
    .match-primary-action {{ min-height: 46px; text-align: center; width: 100%; }}
    .match-card-actions .js-card-controls {{ display: grid; gap: 8px; }}
    .match-card-actions form, .match-card-actions button {{ width: 100%; }}
    .low-result-note {{ background: #fff; border-left: 3px solid #8ab9a6; border-radius: 0 10px 10px 0; color: #526158; margin: 22px 0 0; padding: 15px 18px; }}
    .matches-empty {{ align-items: center; background: #fff; border: 1px solid #dce4df; border-radius: 18px; display: grid; gap: 28px; grid-template-columns: minmax(0, 1fr) 190px; margin: 0; padding: 30px; }}
    .matches-empty h2 {{ font-size: 1.55rem; }}
    .matches-empty p:not(.eyebrow) {{ color: #536159; max-width: 65ch; }}
    .empty-actions {{ display: grid; gap: 12px; }}
    .secondary-action {{ min-height: 44px; padding: 10px; text-align: center; }}
    .relaxation-section {{ border-top: 1px solid #dce4df; margin: 36px 0 0; padding: 32px 0 0; }}
    .relaxation-section h2 {{ font-size: clamp(1.45rem, 3vw, 1.8rem); margin-bottom: 9px; }}
    .relaxation-intro {{ color: #536159; line-height: 1.55; max-width: 68ch; }}
    .relaxation-list, .more-relaxations-list {{ display: grid; gap: 12px; }}
    .relaxation-scenario {{ background: #fff; border: 1px solid #dce4df; border-radius: 14px; overflow: clip; }}
    .relaxation-scenario > summary {{ align-items: center; cursor: pointer; display: flex; gap: 18px; justify-content: space-between; list-style: none; min-height: 64px; padding: 15px 18px; }}
    .relaxation-scenario > summary::-webkit-details-marker {{ display: none; }}
    .relaxation-scenario > summary::after {{ color: #176b52; content: '+'; flex: 0 0 auto; font-size: 1.35rem; font-weight: 800; }}
    .relaxation-scenario[open] > summary::after {{ content: '−'; }}
    .relaxation-scenario > summary:focus-visible, .more-relaxations > summary:focus-visible {{ outline: 3px solid #2563eb; outline-offset: -3px; }}
    .relaxation-summary-copy {{ display: grid; gap: 3px; min-width: 0; }}
    .relaxation-summary-copy strong {{ color: #24352d; font-size: 1rem; }}
    .relaxation-summary-copy span {{ color: #637168; font-size: .85rem; }}
    .relaxation-count {{ color: #176b52; font-size: .9rem; font-weight: 800; margin-left: auto; white-space: nowrap; }}
    .relaxation-preview {{ background: #f4f8f6; border-top: 1px solid #dce4df; padding: 20px; }}
    .relaxation-change {{ color: #31453b; font-weight: 750; margin-bottom: 7px; }}
    .relaxation-preview-note {{ color: #5a6961; font-size: .92rem; line-height: 1.5; }}
    .relaxation-preview-list {{ display: grid; gap: 10px; margin: 18px 0; }}
    .relaxation-preview-card {{ align-items: center; background: #fff; border: 1px solid #dce4df; border-left: 4px solid #8ab9a6; border-radius: 10px; display: grid; gap: 18px; grid-template-columns: minmax(0, 1fr) auto; padding: 15px 16px; }}
    .relaxation-preview-card h3 {{ font-size: 1rem; margin: 0 0 3px; }}
    .relaxation-preview-card p:not(.relaxation-preview-label) {{ color: #596860; font-size: .88rem; margin: 0; }}
    .relaxation-preview-label {{ color: #176b52; font-size: .7rem; font-weight: 800; letter-spacing: .05em; margin: 0 0 5px; text-transform: uppercase; }}
    .relaxation-preview-card ul {{ color: #536159; display: flex; flex-wrap: wrap; font-size: .82rem; gap: 6px 18px; list-style: none; margin: 8px 0 0; padding: 0; }}
    .relaxation-preview-card a {{ min-height: 44px; padding: 11px 0; }}
    .relaxation-actions {{ align-items: center; display: flex; gap: 16px; justify-content: space-between; }}
    .relaxation-actions span {{ color: #5b6861; font-size: .86rem; }}
    .more-relaxations {{ margin-top: 14px; }}
    .more-relaxations > summary {{ color: #176b52; cursor: pointer; font-weight: 800; min-height: 44px; padding: 12px 4px; }}
    .more-relaxations-list {{ padding-top: 8px; }}
    @media (max-width: 680px) {{
      main {{ width: min(100% - 24px, 960px); padding-top: 18px; }}
      .account-nav {{ flex-wrap: wrap; gap: 10px 16px; }}
      .matches-hero {{ margin-top: 22px; }}
      .matches-hero h1 {{ font-size: 2.45rem; }}
      .matches-profile-context {{ align-items: flex-start; flex-direction: column; gap: 10px; }}
      .match-card, .matches-empty {{ gap: 20px; grid-template-columns: 1fr; padding: 21px; }}
      .match-meta {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .match-meta-item:last-child:nth-child(odd) {{ grid-column: 1 / -1; }}
      .match-primary-action, .empty-actions .button, .secondary-action {{ align-items: center; display: flex; justify-content: center; min-height: 48px; }}
      .relaxation-scenario > summary {{ align-items: flex-start; flex-wrap: wrap; gap: 7px 12px; padding: 15px; }}
      .relaxation-count {{ margin-left: 0; order: 3; width: 100%; }}
      .relaxation-scenario > summary::after {{ margin-left: auto; order: 2; }}
      .relaxation-preview {{ padding: 16px; }}
      .relaxation-preview-card {{ align-items: start; grid-template-columns: 1fr; }}
      .relaxation-preview-card a {{ display: inline-flex; }}
      .relaxation-actions {{ align-items: stretch; flex-direction: column; }}
      .relaxation-actions .button {{ align-items: center; display: flex; justify-content: center; min-height: 48px; }}
      .review-grid, .language-review-row {{ grid-template-columns: 1fr; }}
    }}
    @media (max-width: 410px) {{ .match-meta {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} .match-meta-item {{ min-width: 0; }} }}
    @media (prefers-reduced-motion: reduce) {{ html {{ scroll-behavior: auto; }} }}

.card-evidence {{ min-width: 0; overflow-wrap: anywhere; }}
.card-evidence h4 {{ margin: 14px 0 5px; font-size: .95rem; }}
.card-evidence p {{ margin: 7px 0; }}
.opportunity-type {{ font-size: .82rem; font-weight: 750; color: #355447; }}
.source-task, .source-workload {{ font-size: .91rem; color: #3e564b; }}
.card-source-disclosure {{ margin: 12px 0; border: 1px solid #d6e3dc; border-radius: 8px; }}
.card-source-disclosure summary {{ padding: 10px 12px; cursor: pointer; font-weight: 700; }}
.card-source-disclosure summary:focus-visible, .card-evidence a:focus-visible {{ outline: 3px solid #146149; outline-offset: 3px; }}
.card-source-body {{ padding: 0 12px 12px; }}
.card-source-body h5 {{ margin: 14px 0 4px; font-size: .93rem; }}
.card-source-body blockquote {{ white-space: pre-wrap; overflow-wrap: anywhere; margin: 6px 0; padding-left: 12px; border-left: 2px solid #cadbd0; }}
.source-reference, .application-uncertainty {{ font-size: .8rem; color: #5c6d65; }}
{DISPLAY_CSS}
</style>
</head>
<body><main>{body}</main>{local_product.render_inline_action_script() if workflow else ''}</body>
</html>"""


def _html_response(
    status,
    content,
    *,
    referrer_policy=_NO_REFERRER_POLICY,
    extra_headers=(),
    cache_control="no-store",
    max_bytes=MAX_MATCHES_RESPONSE_BYTES,
    robots_directive="noindex, nofollow",
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
    if robots_directive not in {None, "noindex, nofollow", "noindex, follow"}:
        raise ValueError("invalid_authenticated_matches_response")
    robots_header = (
        (("X-Robots-Tag", robots_directive),)
        if robots_directive is not None
        else ()
    )
    security_headers = _SECURITY_HEADERS
    from wahojobs.manual_profile_drafts import SCRIPT as manual_script
    from wahojobs.profiles.correction_editor import EDITOR_SCRIPT
    scripts = (local_product.render_inline_action_script(), '<script>'+manual_script+'</script>',
               '<script>'+EDITOR_SCRIPT+'</script>')
    digests = []
    for script in scripts:
        if script in content:
            body = script.split('<script>', 1)[1].split('</script>', 1)[0]
            digests.append(base64.b64encode(hashlib.sha256(body.encode('utf-8')).digest()).decode('ascii'))
    if digests:
        policy = "; script-src " + " ".join("'sha256-"+digest+"'" for digest in digests) + "; connect-src 'self'"
        security_headers = tuple((name, value + policy if name == 'Content-Security-Policy' else value)
                                 for name, value in security_headers)
    return AuthenticatedMatchesBrowserResponse(
        int(status),
        payload,
        (
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Length", str(len(payload))),
            *security_headers,
            ("Cache-Control", cache_control),
            ("Referrer-Policy", referrer_policy),
            *robots_header,
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
            ("Cache-Control", "no-store"),
            ("X-Robots-Tag", "noindex, nofollow"),
        ),
    )


def _text_response(status, content, *, content_type):
    if type(content) is not str or content_type not in {
        "text/plain; charset=utf-8",
        "application/xml; charset=utf-8",
    }:
        raise ValueError("invalid_public_seo_response")
    payload = content.encode("utf-8")
    if len(payload) > MAX_PUBLIC_JOBS_RESPONSE_BYTES:
        return _failure_response(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "SEO document temporarily unavailable",
            "This SEO document cannot be generated safely right now.",
        )
    return AuthenticatedMatchesBrowserResponse(
        int(status),
        payload,
        (
            ("Content-Type", content_type),
            ("Content-Length", str(len(payload))),
            *_SECURITY_HEADERS,
            ("Cache-Control", "public, max-age=300"),
            ("Referrer-Policy", _NO_REFERRER_POLICY),
        ),
    )


def _permanent_redirect_response(location):
    return _html_response(
        HTTPStatus.MOVED_PERMANENTLY,
        _page(
            "Moved permanently",
            "<section class='panel'><h1>Moved permanently</h1>"
            "<p>This public URL has moved.</p></section>",
        ),
        extra_headers=(("Location", location),),
        cache_control="public, max-age=300",
    )


def _gone_response():
    return _failure_response(
        HTTPStatus.GONE,
        "Opportunity removed",
        "This public URL has been permanently removed.",
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


def _candidate_failure_response(status, title, message):
    return _html_response(status, _page(title,
        f"<section class='panel'><h1>{_safe(title)}</h1><p role='alert'>{_safe(message)}</p>"
        "<p><a class='primary-link' href='/find-matches'>Resume saved profile draft</a></p>"
        "<p><a href='/account/profile'>Return to your profile</a></p></section>"))


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

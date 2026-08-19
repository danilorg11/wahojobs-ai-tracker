"""Protected browser reads, create-once profiles, and bounded corrections."""

from __future__ import annotations

from dataclasses import dataclass, field
import hmac
import html
from http import HTTPStatus
import re
from urllib.parse import parse_qs, unquote_to_bytes, urlsplit

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
from wahojobs.persistent_profile_corrections import (
    ConfirmedProfileCorrectionArtifactOffer,
    PersistentProfileCorrectionService,
    PreparedProfileCorrectionReview,
    ProfileCorrectionAuthorityResult,
    ProfileCorrectionOutcome,
    PROFILE_CORRECTION_ARTIFACT_CAPACITY,
    PROFILE_CORRECTION_ARTIFACT_LIFETIME_SECONDS,
    profile_correction_action_csrf_proof,
)


PERSISTENT_PROFILE_ROUTE = "/account/profile"
FIND_MATCHES_ROUTE = "/find-matches"
MAX_PROFILE_BROWSER_RESPONSE_BYTES = 1_048_576
MAX_PROFILE_QUERY_BYTES = 256
MAX_PROFILE_CREATE_BODY_BYTES = 1_024
MAX_PROFILE_CORRECTION_BODY_BYTES = 524_288
MAX_PROFILE_CORRECTION_FIELDS = 128
MAX_PROFILE_CREATE_HEADERS = 64
MAX_PROFILE_CREATE_COOKIE_BYTES = 4_096
MAX_PROFILE_CREATE_COOKIES = 16

SESSION_COOKIE_NAME = "wahojobs_session"
SESSION_CSRF_COOKIE_NAME = "__Host-wahojobs_session_csrf"

_CURSOR = re.compile(r"^[1-9][0-9]{0,9}$")
_OPAQUE_CREDENTIAL = re.compile(r"^[A-Za-z0-9_-]{43}$")
_MATCH_RUN_REFERENCE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_CONTENT_LENGTH = re.compile(r"^(?:0|[1-9][0-9]{0,3})$")
_CORRECTION_CONTENT_LENGTH = re.compile(r"^(?:0|[1-9][0-9]{0,5})$")
_HTTP_TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_COOKIE_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_HEADER_VALUE_FORBIDDEN = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
_PROFILE_CREATE_FORM = re.compile(
    r"^(?:artifact=([A-Za-z0-9_-]{43})&csrf=([A-Za-z0-9_-]{43})"
    r"|csrf=([A-Za-z0-9_-]{43})&artifact=([A-Za-z0-9_-]{43}))$"
)
_CORRECTION_POST_TARGET = re.compile(
    r"^/account/profile\?action=(start|redraft|confirm|apply)"
    r"&proof=([A-Za-z0-9_-]{43})$"
)
_CORRECTION_DRAFT_REFERENCE = re.compile(r"^[A-Za-z0-9_-]{24}$")
_CORRECTION_GET_TARGET = re.compile(
    r"^/account/profile\?correction=(review|edit)"
    r"&draft=([A-Za-z0-9_-]{24})&token=([A-Za-z0-9_-]{43})$"
)
_INVALID_FORM_PERCENT_ESCAPE = re.compile(rb"%(?![0-9A-Fa-f]{2})")
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
    ("X-Content-Type-Options", "nosniff"),
    ("Cache-Control", "no-store"),
)
_NO_REFERRER_POLICY = "no-referrer"
_SAME_ORIGIN_REFERRER_POLICY = "same-origin"


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
    """Render the profile route and its explicit create/correction boundaries."""

    __slots__ = (
        "_closed",
        "_correction_registry",
        "_correction_service",
        "_creation_service",
        "_matches_integration",
        "_public_authority",
        "_public_origin",
        "_review_support",
        "_service",
    )

    def __init__(
        self,
        service: PersistentProfileApplicationService,
        *,
        creation_service=None,
        correction_service=None,
        correction_registry=None,
        matches_integration=None,
        public_origin=None,
    ):
        if (
            type(service) is not PersistentProfileApplicationService
            or (
                creation_service is not None
                and type(creation_service) is not PersistentProfileCreationService
            )
            or (
                correction_service is not None
                and type(correction_service) is not PersistentProfileCorrectionService
            )
            or (
                (creation_service is None and correction_service is None)
                != (public_origin is None)
            )
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
        review_support = None
        if correction_service is not None:
            review_support = _load_profile_review_support()
            if correction_registry is None:
                correction_registry = review_support.MatchRunRegistry(
                    max_size=PROFILE_CORRECTION_ARTIFACT_CAPACITY,
                    absolute_ttl_seconds=(
                        PROFILE_CORRECTION_ARTIFACT_LIFETIME_SECONDS
                    ),
                )
            elif type(correction_registry) is not review_support.MatchRunRegistry:
                raise ValueError("invalid_persistent_profile_browser_configuration")
        elif correction_registry is not None:
            raise ValueError("invalid_persistent_profile_browser_configuration")
        self._service = service
        self._creation_service = creation_service
        self._correction_service = correction_service
        self._correction_registry = correction_registry
        self._review_support = review_support
        self._matches_integration = matches_integration
        self._public_origin = public_origin
        self._public_authority = authority
        self._closed = False

    def activate(self):
        if self._closed or (
            self._creation_service is None and self._correction_service is None
        ):
            raise ConfirmedProfileArtifactUnavailable()
        if (
            self._creation_service is not None
            and self._creation_service.activate() is not True
        ):
            return False
        if (
            self._correction_service is not None
            and self._correction_service.activate() is not True
        ):
            return False
        return True

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
        response = self._handle_request(
            method,
            target,
            authentication_input,
            body_stream,
        )
        if (
            method == "HEAD"
            and _request_target_path(target) == PERSISTENT_PROFILE_ROUTE
            and type(response) is PersistentProfileBrowserResponse
        ):
            return _head_response(response)
        return response

    def _handle_request(
        self,
        method,
        target,
        authentication_input,
        body_stream,
    ):
        if self._closed:
            return _create_failure_response(HTTPStatus.SERVICE_UNAVAILABLE)
        request_path = _request_target_path(target)
        if request_path != PERSISTENT_PROFILE_ROUTE:
            matches_integration = self._matches_integration
            if (
                matches_integration is None
                or matches_integration.matches_route(request_path) is not True
            ):
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
            if (
                self._creation_service is not None
                or self._correction_service is not None
            )
            else ("GET", "HEAD")
        )
        if method not in allowed_methods:
            return _response(
                HTTPStatus.METHOD_NOT_ALLOWED,
                _generic_page(
                    "Method not allowed",
                    (
                        "This profile route does not accept that method."
                        if len(allowed_methods) == 3
                        else "This profile page is read-only."
                    ),
                ),
                extra_headers=(("Allow", ", ".join(allowed_methods)),),
            )
        if method == "POST":
            correction_target = _parse_correction_post_target(target)
            if correction_target is not None:
                return self._handle_correction_post(
                    correction_target,
                    authentication_input,
                    body_stream,
                )
            return self._handle_create(target, authentication_input, body_stream)
        correction_target = _parse_correction_get_target(target)
        if correction_target is not None:
            return self._handle_correction_get(
                method,
                correction_target,
                authentication_input,
            )
        cursor, current_run_id, request_valid = _parse_request_target(target)
        if not request_valid:
            return _response(
                HTTPStatus.BAD_REQUEST,
                _generic_page("Profile request unavailable", "This profile request is not valid."),
            )
        current_matches_target = None
        if current_run_id is not None:
            matches_integration = self._matches_integration
            if matches_integration is not None:
                try:
                    current_matches_target = matches_integration.current_matches_target(
                        current_run_id,
                        authentication_input,
                    )
                except Exception:
                    current_matches_target = None
        try:
            result = self._service.read_my_profile(
                BrowserRequestContext(
                    method,
                    PERSISTENT_PROFILE_ROUTE,
                    authentication_input,
                ),
                before_revision_number=cursor,
            )
            content, status = render_persistent_profile_page(
                result,
                correction_enabled=self._correction_service is not None,
                current_matches_target=current_matches_target,
            )
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

    def _handle_correction_get(self, method, correction_target, headers):
        if self._correction_service is None:
            return _correction_failure_response(HTTPStatus.NOT_FOUND)
        grant, _header_items, csrf_secret, failure = self._authorize_correction(
            method,
            headers,
        )
        if failure is not None:
            return failure
        stage, draft_reference, review_token = correction_target
        if stage == "start":
            return _form_page_response(
                HTTPStatus.OK,
                _render_correction_start(
                    _correction_action_target(csrf_secret, "start")
                ),
            )
        run, failure = self._authorized_correction_run(
            grant,
            draft_reference,
            review_token,
        )
        if failure is not None:
            return failure
        if stage == "review":
            return _form_page_response(
                HTTPStatus.OK,
                _render_correction_review(
                    run,
                    edit_target=_correction_view_target("edit", run),
                    confirm_target=_correction_action_target(
                        csrf_secret,
                        "confirm",
                    ),
                ),
            )
        presentation = _correction_presentation_value(
            run.canonical_profile.to_mapping()
        )
        provenance = presentation.get("provenance")
        if type(provenance) is dict:
            provenance["field_sources"] = {}
        review_form = self._review_support.render_structured_profile_review(
            presentation,
            run.match_run_id,
            run.review_token,
            form_action=_correction_action_target(csrf_secret, "redraft"),
            back_url=_correction_view_target("review", run),
            submit_label="Review changes",
            include_draft_fingerprint=False,
        )
        return _form_page_response(
            HTTPStatus.OK,
            _page(
                "Edit profile correction",
                _authenticated_navigation()
                + "<section class='profile-header'><p class='eyebrow'>Update profile</p>"
                "<h1>Edit your profile</h1>"
                "<p>Correct the fields below, then review the complete result before applying it.</p>"
                "</section>"
                + review_form,
            ),
        )

    def _handle_correction_post(self, correction_target, headers, body_stream):
        if self._correction_service is None:
            return _correction_failure_response(HTTPStatus.NOT_FOUND)
        action, action_proof = correction_target
        grant, header_items, csrf_secret, failure = self._authorize_correction(
            "POST",
            headers,
            action=action,
            action_proof=action_proof,
        )
        if failure is not None:
            return failure
        form = _strict_correction_form(header_items, body_stream)
        if form is None:
            return _correction_failure_response(HTTPStatus.BAD_REQUEST)
        if action == "start":
            return self._start_correction(grant, form)
        if action == "redraft":
            return self._redraft_correction(grant, form)
        if action == "confirm":
            return self._confirm_correction(
                grant,
                csrf_secret,
                header_items,
                form,
            )
        return self._apply_correction(grant, csrf_secret, form)

    def _authorize_correction(
        self,
        method,
        headers,
        *,
        action=None,
        action_proof=None,
    ):
        header_items = _validated_header_items(headers)
        if header_items is None or not _trusted_host_headers(
            header_items,
            self._public_authority,
        ):
            return None, None, None, _correction_failure_response(
                HTTPStatus.BAD_REQUEST
            )
        if method == "POST" and not _trusted_same_origin(
            header_items,
            self._public_origin,
        ):
            return None, None, None, _correction_failure_response(
                HTTPStatus.FORBIDDEN
            )
        session_token, session_valid = _security_cookie(
            header_items,
            SESSION_COOKIE_NAME,
            _OPAQUE_CREDENTIAL,
        )
        if not session_valid:
            return None, None, None, _correction_failure_response(
                HTTPStatus.UNAUTHORIZED
            )
        csrf_secret, csrf_valid = _security_cookie(
            header_items,
            SESSION_CSRF_COOKIE_NAME,
            _OPAQUE_CREDENTIAL,
        )
        if not csrf_valid:
            return None, None, None, _correction_failure_response(
                HTTPStatus.FORBIDDEN
            )
        try:
            authority = self._correction_service.authorize_request(
                method=method,
                authentication_input=header_items,
                session_token=session_token,
                csrf_secret=csrf_secret,
                action=action,
                action_proof=action_proof,
            )
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except Exception:
            return None, None, None, _correction_failure_response(
                HTTPStatus.SERVICE_UNAVAILABLE
            )
        if type(authority) is not ProfileCorrectionAuthorityResult:
            return None, None, None, _correction_failure_response(
                HTTPStatus.SERVICE_UNAVAILABLE
            )
        if authority.state != "authorized":
            return (
                None,
                None,
                None,
                _correction_authority_failure_response(authority.state),
            )
        grant = authority.grant_for_service()
        if grant is None:
            return None, None, None, _correction_failure_response(
                HTTPStatus.SERVICE_UNAVAILABLE
            )
        return grant, header_items, csrf_secret, None

    def _authorized_correction_run(self, grant, draft_reference, review_token):
        run = self._correction_registry.peek(draft_reference)
        if run is None:
            return None, _correction_failure_response(HTTPStatus.GONE)
        if not hmac.compare_digest(run.review_token, review_token):
            return None, _correction_failure_response(HTTPStatus.GONE)
        current_binding = self._correction_service.draft_binding(grant)
        if not hmac.compare_digest(run.owner_profile_id, current_binding):
            context = run.recommendation_context
            actor_binding = (
                context.get("correction_actor_binding")
                if type(context) is dict
                else None
            )
            if actor_binding == grant.actor_profile_binding():
                return None, _correction_failure_response(HTTPStatus.CONFLICT)
            return None, _correction_failure_response(HTTPStatus.GONE)
        return run, None

    def _new_correction_run(
        self,
        grant,
        *,
        reviewed_profile,
        raw_about_you,
        preparation,
    ):
        if type(preparation) is not PreparedProfileCorrectionReview:
            raise ValueError("invalid_profile_correction_preparation")
        return self._correction_registry.create(
            owner_profile_id=self._correction_service.draft_binding(grant),
            raw_input=raw_about_you,
            input_style="short_paragraph",
            recommendation_context={
                "correction_actor_binding": grant.actor_profile_binding(),
                "correction_preparation": preparation,
            },
            canonical_profile=reviewed_profile,
            profile_confirmed=False,
        )

    def _start_correction(self, grant, form):
        if form != {"intent": ["update_profile"]}:
            return _correction_failure_response(HTTPStatus.BAD_REQUEST)
        try:
            draft, raw_about_you = self._correction_service.prepare_review_draft(
                grant
            )
            preparation = self._correction_service.prepare_initial_review(grant)
            authoritative_draft = preparation.reviewed_profile_for_browser()
            if not hmac.compare_digest(
                authoritative_draft.canonical_bytes,
                draft.canonical_bytes,
            ):
                raise ValueError("invalid_profile_correction_preparation")
            run = self._new_correction_run(
                grant,
                reviewed_profile=authoritative_draft,
                raw_about_you=raw_about_you,
                preparation=preparation,
            )
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except Exception:
            return _correction_failure_response(HTTPStatus.SERVICE_UNAVAILABLE)
        return _correction_redirect(
            _correction_view_target("review", run),
            "Profile correction started",
        )

    def _redraft_correction(self, grant, form):
        if "profile_draft_fingerprint" in form:
            return _correction_failure_response(HTTPStatus.BAD_REQUEST)
        draft_reference = _strict_form_value(form, "edit_run_id")
        review_token = _strict_form_value(form, "review_token")
        if (
            draft_reference is None
            or _CORRECTION_DRAFT_REFERENCE.fullmatch(draft_reference) is None
            or review_token is None
            or _OPAQUE_CREDENTIAL.fullmatch(review_token) is None
        ):
            return _correction_failure_response(HTTPStatus.BAD_REQUEST)
        run, failure = self._authorized_correction_run(
            grant,
            draft_reference,
            review_token,
        )
        if failure is not None:
            return failure
        context = run.recommendation_context
        preparation = (
            context.get("correction_preparation")
            if type(context) is dict
            else None
        )
        if type(preparation) is not PreparedProfileCorrectionReview:
            return _correction_failure_response(HTTPStatus.GONE)
        submitted = dict(form)
        submitted["profile_draft_fingerprint"] = [
            self._review_support.profile_draft_fingerprint(run.canonical_profile)
        ]
        try:
            validated_run, updates = (
                self._review_support.validate_profile_review_submission(
                    submitted,
                    self._correction_registry,
                )
            )
            if validated_run.match_run_id != run.match_run_id:
                raise ValueError("invalid_profile_correction_draft")
            locally_reviewed = self._review_support.apply_identity_free_profile_review(
                validated_run.canonical_profile,
                updates,
            )
            next_preparation = self._correction_service.prepare_reviewed_correction(
                grant=grant,
                preparation=preparation,
                reviewed_profile=locally_reviewed,
                normalized_updates=updates,
            )
            reviewed = next_preparation.reviewed_profile_for_browser()
            redrafted = self._new_correction_run(
                grant,
                reviewed_profile=reviewed,
                raw_about_you=validated_run.raw_input,
                preparation=next_preparation,
            )
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except self._review_support.ActionError as exc:
            return _correction_failure_response(_bounded_correction_status(exc.status))
        except Exception:
            return _correction_failure_response(HTTPStatus.BAD_REQUEST)
        return _correction_redirect(
            _correction_view_target("review", redrafted),
            "Profile correction updated",
        )

    def _confirm_correction(self, grant, csrf_secret, header_items, form):
        if set(form) != {"draft", "review_token", "confirmed"}:
            return _correction_failure_response(HTTPStatus.BAD_REQUEST)
        draft_reference = _strict_form_value(form, "draft")
        review_token = _strict_form_value(form, "review_token")
        confirmed = _strict_form_value(form, "confirmed")
        if (
            draft_reference is None
            or _CORRECTION_DRAFT_REFERENCE.fullmatch(draft_reference) is None
            or review_token is None
            or _OPAQUE_CREDENTIAL.fullmatch(review_token) is None
            or confirmed != "1"
        ):
            return _correction_failure_response(HTTPStatus.BAD_REQUEST)
        run, failure = self._authorized_correction_run(
            grant,
            draft_reference,
            review_token,
        )
        if failure is not None:
            return failure
        context = run.recommendation_context
        preparation = (
            context.get("correction_preparation")
            if type(context) is dict
            else None
        )
        if type(preparation) is not PreparedProfileCorrectionReview:
            return _correction_failure_response(HTTPStatus.GONE)
        try:
            form_fields = self._correction_service.prepare_confirmation_form_fields(
                grant=grant,
                preparation=preparation,
                reviewed_profile=run.canonical_profile,
                draft_reference=run.match_run_id,
                review_token=run.review_token,
            )
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except Exception:
            return _correction_failure_response(HTTPStatus.SERVICE_UNAVAILABLE)
        server_form = {
            name: [value]
            for name, value in form_fields.items()
        }

        def issue_correction_artifact(**kwargs):
            return self._correction_service.issue_confirmed_artifact(
                grant=grant,
                csrf_secret=csrf_secret,
                _prepared_review=preparation,
                **kwargs,
            )

        def authenticate_completed_correction(
            *,
            authentication_input,
            authority_binding,
        ):
            del authentication_input
            return self._correction_service.authenticate_completed_confirmation(
                grant=grant,
                authority_binding=authority_binding,
            )

        try:
            result = self._review_support.confirm_profile_review(
                server_form,
                self._correction_registry,
                confirmed_profile_artifact_sink=issue_correction_artifact,
                completed_profile_confirmation_authenticator=(
                    authenticate_completed_correction
                ),
                authentication_input=header_items,
                _allow_matching=False,
            )
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except self._review_support.ActionError as exc:
            return _correction_failure_response(_bounded_correction_status(exc.status))
        except Exception:
            return _correction_failure_response(HTTPStatus.SERVICE_UNAVAILABLE)
        offer = getattr(result, "artifact_offer", None)
        if type(offer) is not ConfirmedProfileCorrectionArtifactOffer:
            return _correction_failure_response(HTTPStatus.SERVICE_UNAVAILABLE)
        return _form_page_response(
            HTTPStatus.OK,
            _render_correction_apply(
                offer,
                action_target=_correction_action_target(csrf_secret, "apply"),
            ),
        )

    def _apply_correction(self, grant, csrf_secret, form):
        if set(form) != {"artifact", "csrf"}:
            return _correction_failure_response(HTTPStatus.BAD_REQUEST)
        artifact = _strict_form_value(form, "artifact")
        artifact_csrf = _strict_form_value(form, "csrf")
        if (
            artifact is None
            or _OPAQUE_CREDENTIAL.fullmatch(artifact) is None
            or artifact_csrf is None
            or _OPAQUE_CREDENTIAL.fullmatch(artifact_csrf) is None
        ):
            return _correction_failure_response(HTTPStatus.BAD_REQUEST)
        try:
            outcome = self._correction_service.consume(
                grant=grant,
                csrf_secret=csrf_secret,
                artifact_reference=artifact,
                csrf_proof=artifact_csrf,
            )
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except Exception:
            return _correction_failure_response(HTTPStatus.SERVICE_UNAVAILABLE)
        if type(outcome) is not ProfileCorrectionOutcome:
            return _correction_failure_response(HTTPStatus.SERVICE_UNAVAILABLE)
        return _correction_response_for_outcome(outcome.state)

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
        if self._correction_service is not None:
            if self._correction_service.close() is False:
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
            self._correction_service is None or self._correction_service.closed
        ) and (
            self._matches_integration is None
            or self._matches_integration.closed is True
        )


def _load_profile_review_support():
    try:
        from scripts import local_product_app as review_support

        required = (
            "ActionError",
            "MatchRunRegistry",
            "apply_identity_free_profile_review",
            "confirm_profile_review",
            "profile_draft_fingerprint",
            "profile_review_form_fields",
            "render_structured_profile_review",
            "validate_profile_review_submission",
        )
        if not all(hasattr(review_support, name) for name in required):
            raise ValueError
        return review_support
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except Exception:
        raise ValueError("invalid_persistent_profile_browser_configuration") from None


def _parse_correction_get_target(target):
    if target == PERSISTENT_PROFILE_ROUTE + "?correction=start":
        return "start", None, None
    if type(target) is not str or len(target) > MAX_PROFILE_QUERY_BYTES:
        return None
    match = _CORRECTION_GET_TARGET.fullmatch(target)
    if match is None:
        return None
    return match.group(1), match.group(2), match.group(3)


def _parse_correction_post_target(target):
    if type(target) is not str or len(target) > MAX_PROFILE_QUERY_BYTES:
        return None
    match = _CORRECTION_POST_TARGET.fullmatch(target)
    if match is None:
        return None
    return match.group(1), match.group(2)


def _correction_action_target(csrf_secret, action):
    proof = profile_correction_action_csrf_proof(csrf_secret, action)
    return f"{PERSISTENT_PROFILE_ROUTE}?action={action}&proof={proof}"


def _correction_view_target(stage, run):
    if (
        stage not in {"review", "edit"}
        or _CORRECTION_DRAFT_REFERENCE.fullmatch(run.match_run_id) is None
        or _OPAQUE_CREDENTIAL.fullmatch(run.review_token) is None
    ):
        raise ValueError("invalid_profile_correction_draft")
    return (
        f"{PERSISTENT_PROFILE_ROUTE}?correction={stage}"
        f"&draft={run.match_run_id}&token={run.review_token}"
    )


def _strict_correction_form(header_items, body_stream):
    content_types = _header_values(header_items, "content-type")
    lengths = _header_values(header_items, "content-length")
    if (
        len(content_types) != 1
        or content_types[0].lower() != "application/x-www-form-urlencoded"
        or len(lengths) != 1
        or _CORRECTION_CONTENT_LENGTH.fullmatch(lengths[0]) is None
        or _header_values(header_items, "transfer-encoding")
        or body_stream is None
        or not callable(getattr(body_stream, "read", None))
    ):
        return None
    length = int(lengths[0])
    if length < 1 or length > MAX_PROFILE_CORRECTION_BODY_BYTES:
        return None
    try:
        body = body_stream.read(length)
    except Exception:
        return None
    if (
        type(body) is not bytes
        or len(body) != length
        or _INVALID_FORM_PERCENT_ESCAPE.search(body) is not None
    ):
        return None
    fields = body.split(b"&")
    if (
        len(fields) > MAX_PROFILE_CORRECTION_FIELDS
        or any(not item or b"=" not in item for item in fields)
    ):
        return None
    form = {}
    try:
        for item in fields:
            encoded_name, encoded_value = item.split(b"=", 1)
            name = unquote_to_bytes(encoded_name.replace(b"+", b" ")).decode(
                "utf-8",
                "strict",
            )
            value = unquote_to_bytes(encoded_value.replace(b"+", b" ")).decode(
                "utf-8",
                "strict",
            )
            if (
                not name
                or _CONTROL_CHARACTERS.search(name) is not None
                or _CONTROL_CHARACTERS.search(value) is not None
            ):
                return None
            form.setdefault(name, []).append(value)
    except (UnicodeError, ValueError):
        return None
    return form


def _strict_form_value(form, name):
    values = form.get(name)
    if type(values) is not list or len(values) != 1:
        return None
    value = values[0]
    if type(value) is not str or value != value.strip():
        return None
    return value


def _bounded_correction_status(value):
    try:
        status = HTTPStatus(value)
    except (TypeError, ValueError):
        return HTTPStatus.SERVICE_UNAVAILABLE
    if status in {
        HTTPStatus.BAD_REQUEST,
        HTTPStatus.UNAUTHORIZED,
        HTTPStatus.FORBIDDEN,
        HTTPStatus.NOT_FOUND,
        HTTPStatus.CONFLICT,
        HTTPStatus.GONE,
        HTTPStatus.SERVICE_UNAVAILABLE,
    }:
        return status
    return HTTPStatus.SERVICE_UNAVAILABLE


def _correction_authority_failure_response(state):
    status = {
        "authentication_required": HTTPStatus.UNAUTHORIZED,
        "csrf_denied": HTTPStatus.FORBIDDEN,
        "authorization_denied": HTTPStatus.NOT_FOUND,
        "empty": HTTPStatus.CONFLICT,
        "profile_unavailable": HTTPStatus.CONFLICT,
        "schema_unavailable": HTTPStatus.SERVICE_UNAVAILABLE,
        "unavailable": HTTPStatus.SERVICE_UNAVAILABLE,
    }.get(state, HTTPStatus.SERVICE_UNAVAILABLE)
    return _correction_failure_response(status)


def _correction_failure_response(status):
    status = HTTPStatus(status)
    title, message = {
        HTTPStatus.BAD_REQUEST: (
            "Profile correction unavailable",
            "This profile correction request is not valid.",
        ),
        HTTPStatus.UNAUTHORIZED: (
            "Authentication required",
            "Sign in to update your profile.",
        ),
        HTTPStatus.FORBIDDEN: (
            "Profile correction rejected",
            "This profile correction request could not be verified.",
        ),
        HTTPStatus.NOT_FOUND: (
            "Profile not found",
            "This profile page is not available.",
        ),
        HTTPStatus.CONFLICT: (
            "Profile changed",
            "Your saved profile changed after this correction began. Start again.",
        ),
        HTTPStatus.GONE: (
            "Profile correction expired",
            "This profile correction is no longer available. Start again.",
        ),
        HTTPStatus.SERVICE_UNAVAILABLE: (
            "Profile temporarily unavailable",
            "Your profile correction could not be completed safely. Try again shortly.",
        ),
    }[status]
    return _response(status, _generic_page(title, message))


def _correction_redirect(location, title):
    if _parse_correction_get_target(location) is None:
        return _correction_failure_response(HTTPStatus.SERVICE_UNAVAILABLE)
    return _response(
        HTTPStatus.SEE_OTHER,
        _generic_page(title, "Continue to review your profile correction."),
        extra_headers=(("Location", location),),
    )


def _correction_response_for_outcome(state):
    if state == "corrected":
        return _response(
            HTTPStatus.SEE_OTHER,
            _generic_page("Profile updated", "Your updated profile is ready."),
            extra_headers=(("Location", FIND_MATCHES_ROUTE),),
        )
    return _correction_failure_response(
        {
            "stale": HTTPStatus.CONFLICT,
            "conflict": HTTPStatus.CONFLICT,
            "gone": HTTPStatus.GONE,
            "temporary_contention": HTTPStatus.SERVICE_UNAVAILABLE,
            "unavailable": HTTPStatus.SERVICE_UNAVAILABLE,
        }.get(state, HTTPStatus.SERVICE_UNAVAILABLE)
    )


def _render_correction_start(action_target):
    return _page(
        "Update profile",
        _authenticated_navigation()
        + "<section class='empty'><p class='eyebrow'>Update profile</p>"
        "<h1>Start a profile correction</h1>"
        "<p>Begin with your current saved profile, correct only what needs changing, "
        "and review the complete result before it is applied.</p>"
        f"<form method='post' action='{_safe_text(action_target)}'>"
        "<input type='hidden' name='intent' value='update_profile'>"
        "<button type='submit'>Update profile</button>"
        "</form></section>",
    )


def _render_correction_review(run, *, edit_target, confirm_target):
    canonical = run.canonical_profile.to_mapping()
    identity = canonical.get("identity") or {}
    title = _safe_text(identity.get("display_name") or "My profile")
    sections = "".join(
        _render_correction_summary_section(label, canonical.get(name))
        for name, label in (
            ("location", "Location"),
            ("languages", "Languages"),
            ("experience", "Experience"),
            ("education", "Education"),
            ("skills", "Skills"),
            ("preferences", "Work preferences"),
            ("credentials", "Credentials"),
            ("constraints", "Constraints"),
        )
    )
    if not sections:
        sections = "<p class='muted'>No additional profile details are available.</p>"
    return _page(
        "Review profile correction",
        _authenticated_navigation()
        + "<header class='profile-header'><p class='eyebrow'>Update profile</p>"
        f"<h1>{title}</h1>"
        "<p>Review the complete profile below. Nothing is saved until you apply the correction.</p>"
        f"<p><a class='primary-link' href='{_safe_text(edit_target)}'>Edit profile</a></p>"
        "</header>"
        f"<div class='profile-grid'>{sections}</div>"
        "<section class='empty'><h2>Confirm this correction</h2>"
        "<p>Confirm that the reviewed details are accurate before preparing the update.</p>"
        f"<form method='post' action='{_safe_text(confirm_target)}'>"
        f"<input type='hidden' name='draft' value='{_safe_text(run.match_run_id)}'>"
        f"<input type='hidden' name='review_token' value='{_safe_text(run.review_token)}'>"
        "<label><input type='checkbox' name='confirmed' value='1' required> "
        "I confirm these profile details, including licenses and certifications, "
        "are accurate.</label>"
        "<p><button type='submit'>Prepare profile update</button></p>"
        "</form></section>",
    )


def _render_correction_summary_section(label, value):
    if value in (None, "", (), [], {}):
        return ""
    return (
        "<section class='profile-group'>"
        f"<h2>{_safe_text(label)}</h2>"
        + _render_correction_summary_value(value)
        + "</section>"
    )


def _correction_presentation_value(value):
    if type(value) is str:
        return value.translate(_BIDI_CONTROLS)
    if type(value) is dict:
        return {
            key: _correction_presentation_value(item)
            for key, item in value.items()
        }
    if type(value) is list:
        return [_correction_presentation_value(item) for item in value]
    return value


def _render_correction_summary_value(value):
    if type(value) is dict:
        items = "".join(
            "<div><dt>"
            + _safe_text(_humanize(str(key)))
            + "</dt><dd>"
            + _render_correction_summary_value(item)
            + "</dd></div>"
            for key, item in value.items()
            if item not in (None, "", (), [], {})
            and _correction_summary_key_visible(key)
        )
        return f"<dl class='correction-summary'>{items}</dl>" if items else ""
    if type(value) in {list, tuple}:
        items = "".join(
            f"<li>{_render_correction_summary_value(item)}</li>" for item in value
        )
        return f"<ul>{items}</ul>" if items else ""
    if type(value) is bool:
        return "Yes" if value else "No"
    return _safe_text(value)


def _correction_summary_key_visible(key):
    normalized = str(key).strip().lower()
    return not (
        not normalized
        or normalized.endswith("_id")
        or "provenance" in normalized
        or "source" in normalized
        or "evidence" in normalized
        or "confidence" in normalized
        or "revision" in normalized
        or "fingerprint" in normalized
        or "hash" in normalized
        or normalized in {"profile_id", "sha256", "idempotency_key"}
    )


def _render_correction_apply(offer, *, action_target):
    return _page(
        "Apply profile correction",
        _authenticated_navigation()
        + "<section class='empty'><p class='eyebrow'>Update profile</p>"
        "<h1>Your profile correction is ready</h1>"
        "<p>Apply this reviewed correction to update your persistent profile. "
        "The previous saved revision will not be changed.</p>"
        f"<form method='post' action='{_safe_text(action_target)}'>"
        f"<input type='hidden' name='artifact' value='{_safe_text(offer.artifact_reference)}'>"
        f"<input type='hidden' name='csrf' value='{_safe_text(offer.csrf_proof)}'>"
        "<button type='submit'>Apply profile update</button>"
        "</form></section>",
    )


def _head_response(response):
    if type(response) is not PersistentProfileBrowserResponse:
        raise ValueError("invalid_persistent_profile_browser_response")
    return PersistentProfileBrowserResponse(
        response.status,
        b"",
        response.headers,
    )


def _require_matches_integration(matches_integration):
    try:
        matches_route = getattr(matches_integration, "matches_route", None)
        handle_matches = getattr(matches_integration, "handle", None)
        close_matches = getattr(matches_integration, "close", None)
        current_matches_target = getattr(
            matches_integration,
            "current_matches_target",
            None,
        )
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
        or not callable(current_matches_target)
        or type(matches_closed) is not bool
        or matches_closed
    ):
        raise ValueError("invalid_persistent_profile_browser_configuration")


def _parse_request_target(target: str) -> tuple[int | None, str | None, bool]:
    if type(target) is not str or len(target.encode("utf-8", errors="ignore")) > MAX_PROFILE_QUERY_BYTES:
        return None, None, False
    try:
        parsed = urlsplit(target)
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.path != PERSISTENT_PROFILE_ROUTE
            or parsed.fragment
        ):
            return None, None, False
        params = parse_qs(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=4,
        ) if parsed.query else {}
    except (UnicodeError, ValueError):
        return None, None, False
    if set(params) - {"before", "run"}:
        return None, None, False
    if not params:
        return None, None, True
    cursor = None
    if "before" in params:
        values = params["before"]
        if len(values) != 1 or _CURSOR.fullmatch(values[0]) is None:
            return None, None, False
        cursor = int(values[0])
        if cursor > MAX_BROWSER_CURSOR:
            return None, None, False
    current_run_id = None
    if "run" in params:
        values = params["run"]
        if len(values) != 1 or _MATCH_RUN_REFERENCE.fullmatch(values[0]) is None:
            return None, None, False
        current_run_id = values[0]
    return cursor, current_run_id, True


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
    *,
    correction_enabled=False,
    current_matches_target=None,
) -> tuple[str, HTTPStatus]:
    if (
        type(result) is not PersistentProfilePageResult
        or type(correction_enabled) is not bool
        or (
            current_matches_target is not None
            and (
                type(current_matches_target) is not str
                or not current_matches_target.startswith(FIND_MATCHES_ROUTE + "?run=")
            )
        )
    ):
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
    return _render_available(
        result,
        correction_enabled=correction_enabled,
        current_matches_target=current_matches_target,
    ), HTTPStatus.OK


def _render_available(
    result: PersistentProfilePageResult,
    *,
    correction_enabled=False,
    current_matches_target=None,
) -> str:
    profile = result.profile
    lifecycle_note = {
        "active": (
            "This profile is active."
            if correction_enabled
            else "This profile is active and shown read-only."
        ),
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
    update_link = (
        f"<a class='primary-link' href='{PERSISTENT_PROFILE_ROUTE}?correction=start'>"
        "Update profile</a>"
        if correction_enabled and result.state == "active"
        else ""
    )
    matches_target = current_matches_target or FIND_MATCHES_ROUTE
    matches_label = "Current matches" if current_matches_target else "Find matches"
    body = f"""
    <header class='profile-header'>
      <p class='eyebrow'>Account profile</p>
      <h1>{title}</h1>
      <p>{_safe_text(lifecycle_note)}</p>
      <p class='profile-actions'><a class='primary-link' href='{_safe_text(matches_target)}'>{matches_label}</a>{update_link}</p>
      <dl class='meta'>
        <div><dt>Status</dt><dd>{_safe_text(_humanize(profile.lifecycle_status))}</dd></div>
        <div><dt>Last accepted update</dt><dd>{_safe_text(profile.updated_at)}</dd></div>
      </dl>
    </header>
    <div class='profile-grid'>{groups}</div>
    """
    return _page("My persistent profile", _authenticated_navigation() + body)


def _response(
    status,
    content: str,
    *,
    referrer_policy=_NO_REFERRER_POLICY,
    extra_headers=(),
) -> PersistentProfileBrowserResponse:
    payload = content.encode("utf-8")
    if referrer_policy not in {
        _NO_REFERRER_POLICY,
        _SAME_ORIGIN_REFERRER_POLICY,
    }:
        raise ValueError("invalid_persistent_profile_browser_response")
    headers = (
        ("Content-Type", "text/html; charset=utf-8"),
        ("Content-Length", str(len(payload))),
        *_SECURITY_HEADERS,
        ("Referrer-Policy", referrer_policy),
        ("X-Robots-Tag", "noindex, nofollow"),
        *extra_headers,
    )
    return PersistentProfileBrowserResponse(int(status), payload, tuple(headers))


def _form_page_response(status, content: str) -> PersistentProfileBrowserResponse:
    return _response(
        status,
        content,
        referrer_policy=_SAME_ORIGIN_REFERRER_POLICY,
    )


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
    .profile-header, .empty, .profile-review-form {{ background: white; border: 1px solid #dce2df; border-radius: 8px; padding: 24px; }}
    .profile-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; margin: 16px 0; }}
    .profile-group {{ background: white; border: 1px solid #dce2df; border-radius: 8px; padding: 18px; }}
    h1, h2, p {{ margin-top: 0; }}
    h1 {{ font-size: 30px; margin-bottom: 10px; }}
    h2 {{ font-size: 18px; }}
    .eyebrow {{ color: #466257; font-weight: 700; }}
    .profile-actions, .review-actions {{ display: flex; flex-wrap: wrap; gap: 16px; }}
    .meta {{ display: flex; flex-wrap: wrap; gap: 20px; margin-bottom: 0; }}
    .meta div {{ min-width: 150px; }}
    dt {{ color: #66716c; font-size: 13px; }}
    dd {{ margin: 3px 0 0; font-weight: 650; }}
    ul, ol {{ padding-left: 20px; }}
    .correction-summary div {{ margin-bottom: 10px; }}
    .review-section {{ border-top: 1px solid #e8ecea; margin-top: 22px; padding-top: 18px; }}
    .review-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 12px; }}
    .review-field {{ display: grid; gap: 5px; margin-bottom: 12px; }}
    .review-field input, .review-field select {{ width: 100%; padding: 9px; border: 1px solid #aebbb5; border-radius: 5px; }}
    .review-checks {{ display: grid; gap: 8px; margin: 12px 0; }}
    button {{ background: #174d3b; color: white; border: 0; border-radius: 5px; padding: 10px 16px; font: inherit; font-weight: 700; cursor: pointer; }}
    .muted {{ color: #66716c; }}
  </style>
</head>
<body><main>{body}</main></body>
</html>"""

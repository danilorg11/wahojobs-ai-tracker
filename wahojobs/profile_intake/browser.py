"""Private browser boundary for one authenticated AI-assisted profile draft."""

from __future__ import annotations

import base64
import hashlib
from http import HTTPStatus
import json
import re
from urllib.parse import parse_qs, urlencode, urlsplit

from python_multipart import MultipartParser
from python_multipart.multipart import MultipartParseError, MultipartState, parse_options_header

from wahojobs.persistent_profiles_browser import (
    PersistentProfileBrowserResponse,
    SESSION_COOKIE_NAME,
    SESSION_CSRF_COOKIE_NAME,
    _authenticated_navigation,
    _form_page_response,
    _header_values,
    _page,
    _response,
    _safe_text,
    _security_cookie,
    _trusted_host_headers,
    _trusted_same_origin,
    _validated_header_items,
)
from wahojobs.profile_intake.contracts import (
    DEFAULT_DOCUMENT_LIMITS,
    DocumentFormat,
    DocumentKind,
    ProfileIntakeError,
    _FIELD_SPECS,
)
from wahojobs.profile_intake.runtime import (
    PROFILE_INTAKE_REVIEW_ROUTE,
    PROFILE_INTAKE_ROUTE,
    ProfileIntakeDocumentInput,
    ProfileIntakeAuthorityService,
    ProfileIntakeProcessingService,
    profile_intake_csrf_proof,
    review_value_for_form,
    update_editable_review,
)
from wahojobs.profiles.preference_model import (
    ProfilePreferenceModelError,
    canonicalize_profile_preferences_v1,
    empty_profile_preferences_v1,
    profile_preference_control_catalog_v1,
)


MAX_MULTIPART_BODY_BYTES = (2 * DEFAULT_DOCUMENT_LIMITS.max_upload_bytes) + 65_536
MAX_REVIEW_BODY_BYTES = 131_072
MAX_REVIEW_FIELDS = 1_024
MAX_MULTIPART_PARTS = 3
MAX_MULTIPART_METADATA_BYTES = 128
MAX_MULTIPART_HEADER_BYTES = 4_224

_OPAQUE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_CONTENT_LENGTH = re.compile(r"^(?:0|[1-9][0-9]{0,8})$")
_FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_INVALID_PERCENT_ESCAPE = re.compile(rb"%(?![0-9A-Fa-f]{2})")
_FILE_PARTS = {
    "resume": DocumentKind.RESUME,
    "linkedin_profile_export": DocumentKind.LINKEDIN_PROFILE_EXPORT,
}
_ALLOWED_MULTIPART_NAMES = frozenset({"csrf", *_FILE_PARTS})
_MIME_PDF = b"application/pdf"
_MIME_DOCX = b"application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_PREFERENCE_COMPENSATION_FIELDS = (
    "preference_compensation_minimum_kind",
    "preference_compensation_amount",
    "preference_compensation_currency",
    "preference_compensation_period",
)

_CLASSIFICATION_DESCRIPTIONS = {
    "advanced_degree": "A graduate or professional qualification beyond a bachelor's degree.",
    "associate": "An associate-level college degree.",
    "bachelor": "A bachelor's or equivalent undergraduate degree.",
    "doctorate": "A doctoral-level academic or professional degree.",
    "high_school": "Secondary-school completion without a higher degree.",
    "master": "A master's or equivalent graduate degree.",
    "no_degree": "No completed degree is stated or required.",
    "not_specified": "The documents do not support a more specific classification.",
    "phd": "A research doctorate (PhD or equivalent).",
    "professional": "A profession-specific advanced qualification.",
    "professional_degree": "A degree preparing for a regulated or specialized profession.",
    "technical": "A technical, vocational, or trade qualification.",
    "in_progress": "The education or credential is currently being completed.",
    "completed": "The education or credential has been completed.",
    "absent": "The document explicitly indicates that the credential is not held.",
    "explicit": "The document explicitly states that the credential is held.",
    "advanced": "Advanced specialist scope beyond typical senior responsibility.",
    "entry-level": "Entry-level scope with limited prior experience expected.",
    "executive": "Executive responsibility for an organization or major function.",
    "junior": "Early-career scope with guidance expected.",
    "entry": "Entry-level scope with limited prior experience expected.",
    "mid": "Established independent contributor scope.",
    "mid-level": "Established independent contributor scope.",
    "senior": "Senior individual-contributor scope.",
    "lead": "Technical or functional leadership scope.",
    "principal": "High-scope expert individual-contributor work.",
    "manager": "People-management responsibility.",
    "student": "Student or pre-entry-career scope.",
    "unknown": "The available evidence does not support a reliable classification.",
    "individual contributor": "Work delivered without people-management authority.",
    "management": "Work that includes managing people or a function.",
    "asynchronous": "Work can be completed without continuous real-time overlap.",
    "flexible": "Timing or coordination can vary within agreed expectations.",
    "no preference": "No preference is asserted for this dimension.",
    "synchronous": "Work includes real-time overlap with teammates or customers.",
    "non-phone preferred": "Non-phone work is preferred, but phone work may be considered.",
    "non-phone required": "Only work without phone or live voice duties is acceptable.",
    "phone acceptable": "Phone and non-phone work are both acceptable.",
    "phone preferred": "Phone work is preferred, but non-phone work may be considered.",
    "available": "The candidate states that they are available.",
    "full-time": "The candidate states full-time availability.",
    "immediate": "The candidate states they can start immediately.",
    "limited": "The candidate states limited availability.",
    "part-time": "The candidate states part-time availability.",
    "unavailable": "The candidate states they are currently unavailable.",
}

_PROCESSING_SCRIPT = """(function(){var f=document.getElementById('profile-intake-upload');if(!f){return;}f.addEventListener('submit',function(){if(!f.checkValidity()){return;}var files=f.querySelectorAll('input[type=file]');if(!files[0].files.length&&!files[1].files.length){return;}var state=document.getElementById('profile-building-state');var content=document.getElementById('profile-upload-content');f.setAttribute('aria-busy','true');f.classList.add('is-processing');f.querySelector('button[type=submit]').disabled=true;content.hidden=true;state.hidden=false;state.focus();});}());"""
_PROCESSING_SCRIPT_HASH = base64.b64encode(
    hashlib.sha256(_PROCESSING_SCRIPT.encode("utf-8")).digest()
).decode("ascii")


class _NoContentParserLogger:
    """Suppress parser diagnostics that can describe provider-bound bytes."""

    def warning(self, *_args, **_kwargs):
        return None


class ProfileIntakeBrowserIntegration:
    """HTTP rendering around the bounded, non-durable intake services."""

    __slots__ = ("_authority", "_closed", "_processing", "_public_authority", "_public_origin")

    def __init__(self, authority_service, processing_service, *, public_origin):
        if (
            type(authority_service) is not ProfileIntakeAuthorityService
            or type(processing_service) is not ProfileIntakeProcessingService
            or type(public_origin) is not str
        ):
            raise ValueError("invalid_profile_intake_browser_configuration")
        parsed = urlsplit(public_origin)
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
            raise ValueError("invalid_profile_intake_browser_configuration")
        self._authority = authority_service
        self._processing = processing_service
        self._public_origin = public_origin.rstrip("/")
        self._public_authority = parsed.netloc
        self._closed = False

    @property
    def closed(self):
        return self._closed or self._processing.vault.closed

    def activate(self):
        if self._closed:
            return False
        return self._processing.vault.activate()

    def close(self):
        self._closed = True
        self._processing.vault.close()

    def matches_route(self, path):
        return path in {PROFILE_INTAKE_ROUTE, PROFILE_INTAKE_REVIEW_ROUTE}

    def handle(self, method, target, authentication_input=None, body_stream=None):
        if self.closed:
            return _failure("unavailable")
        parsed = _target(target)
        if parsed is None or not self.matches_route(parsed.path):
            return _failure("not_found")
        if method not in {"GET", "HEAD", "POST"}:
            return _response(
                HTTPStatus.METHOD_NOT_ALLOWED,
                _message_page("Method not allowed", "This intake page does not accept that method."),
                extra_headers=(("Allow", "GET, HEAD, POST"),),
            )
        headers = _validated_header_items(authentication_input)
        if headers is None or not _trusted_host_headers(headers, self._public_authority):
            return _failure("invalid_request")
        session_token, session_ok = _security_cookie(headers, SESSION_COOKIE_NAME, _OPAQUE)
        csrf_secret, csrf_ok = _security_cookie(headers, SESSION_CSRF_COOKIE_NAME, _OPAQUE)
        if not session_ok or not csrf_ok:
            return _failure("authentication_required")
        if method == "POST" and not _trusted_same_origin(headers, self._public_origin):
            return _failure("csrf_denied")
        try:
            if parsed.path == PROFILE_INTAKE_ROUTE:
                response = self._upload(
                    method,
                    parsed,
                    headers,
                    authentication_input,
                    body_stream,
                    session_token,
                    csrf_secret,
                )
            else:
                response = self._review(
                    method,
                    parsed,
                    headers,
                    authentication_input,
                    body_stream,
                    session_token,
                    csrf_secret,
                )
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except Exception:
            response = _failure("unavailable")
        if method == "HEAD" and type(response) is PersistentProfileBrowserResponse:
            return PersistentProfileBrowserResponse(
                response.status,
                b"",
                tuple(
                    (name, "0" if name.lower() == "content-length" else value)
                    for name, value in response.headers
                ),
            )
        return response

    def _authorize(
        self,
        *,
        method,
        route,
        authentication_input,
        session_token,
        csrf_secret,
        action=None,
        proof=None,
        reference=None,
        version=None,
    ):
        outcome = self._authority.authorize(
            method=method,
            route=route,
            authentication_input=authentication_input,
            session_token=session_token,
            csrf_secret=csrf_secret,
            action=action,
            proof=proof,
            draft_reference=reference,
            version=version,
        )
        if outcome.state != "authorized":
            return None, _failure(outcome.state)
        return outcome.grant_for_service(), None

    def _upload(self, method, parsed, headers, authentication_input, body_stream, session_token, csrf_secret):
        if parsed.query or parsed.fragment:
            return _failure("invalid_request")
        grant, failure = self._authorize(
            method=method,
            route=PROFILE_INTAKE_ROUTE,
            authentication_input=authentication_input,
            session_token=session_token,
            csrf_secret=csrf_secret,
        )
        if failure is not None:
            return failure
        if method in {"GET", "HEAD"}:
            try:
                preflight = self._processing.preflight(grant)
            except ProfileIntakeError as exc:
                return _failure(_processing_error_code(exc.code))
            if preflight != "eligible":
                return _failure(_preflight_error_code(preflight))
            proof = profile_intake_csrf_proof(csrf_secret, "upload")
            return _form_page_response(
                HTTPStatus.OK,
                _upload_page(proof),
                script_sha256=_PROCESSING_SCRIPT_HASH,
            )
        parsed_upload = _parse_multipart_upload(headers, body_stream)
        if type(parsed_upload) is str:
            return _failure(parsed_upload)
        proof, documents = parsed_upload
        parsed_upload = None
        grant, failure = self._authorize(
            method="POST",
            route=PROFILE_INTAKE_ROUTE,
            authentication_input=authentication_input,
            session_token=session_token,
            csrf_secret=csrf_secret,
            action="upload",
            proof=proof,
        )
        if failure is not None:
            return failure
        try:
            reference, _snapshot = self._processing.process_bundle(
                grant,
                documents,
            )
        except ProfileIntakeError as exc:
            return _failure(_processing_error_code(exc.code))
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except Exception:
            return _failure("extraction_unavailable")
        finally:
            documents = None
        location = PROFILE_INTAKE_REVIEW_ROUTE + "?" + urlencode({"draft": reference})
        return _response(
            HTTPStatus.SEE_OTHER,
            _message_page("Draft ready", "Review your generated profile draft."),
            extra_headers=(("Location", location),),
        )

    def _review(self, method, parsed, headers, authentication_input, body_stream, session_token, csrf_secret):
        parameters = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        if parsed.fragment or set(parameters) != {"draft"} or len(parameters["draft"]) != 1:
            return _failure("invalid_draft")
        reference = parameters["draft"][0]
        if _OPAQUE.fullmatch(reference) is None:
            return _failure("invalid_draft")
        grant, failure = self._authorize(
            method=method,
            route=PROFILE_INTAKE_REVIEW_ROUTE,
            authentication_input=authentication_input,
            session_token=session_token,
            csrf_secret=csrf_secret,
        )
        if failure is not None:
            return failure
        if method in {"GET", "HEAD"}:
            state, snapshot = self._processing.lookup(reference, grant)
            if state == "completed":
                return _matches_redirect()
            if state != "active" or snapshot is None:
                return _failure("expired_draft")
            return _form_page_response(
                HTTPStatus.OK,
                _review_page(
                    reference,
                    snapshot,
                    csrf_secret,
                    save_enabled=self._processing.durable_save_enabled,
                ),
            )
        form = _parse_review_form(headers, body_stream)
        if form is None:
            return _failure("invalid_review")
        action = _single(form, "action")
        raw_version = _single(form, "version")
        proof = _single(form, "csrf")
        if action not in {"update", "cancel", "save"} or raw_version is None or not raw_version.isdigit():
            return _failure("invalid_review")
        version = int(raw_version)
        grant, failure = self._authorize(
            method="POST",
            route=PROFILE_INTAKE_REVIEW_ROUTE,
            authentication_input=authentication_input,
            session_token=session_token,
            csrf_secret=csrf_secret,
            action=action,
            proof=proof,
            reference=reference,
            version=version,
        )
        if failure is not None:
            return failure
        request_digest = _review_request_digest(form) if action == "save" else None
        if action == "save":
            completion = self._processing.completion_matches(
                reference,
                grant,
                expected_version=version,
                request_digest=request_digest,
            )
            if completion == "completed":
                return _matches_redirect()
            if completion == "stale":
                return _failure("stale_review")
        state, snapshot = self._processing.lookup(reference, grant)
        if state == "completed":
            return _matches_redirect() if action == "save" else _failure("stale_review")
        if state != "active" or snapshot is None:
            return _failure("expired_draft")
        if action == "cancel":
            if set(form) != {"action", "version", "csrf"}:
                return _failure("invalid_review")
            state = self._processing.cancel(reference, grant, expected_version=version)
            if state == "stale":
                return _failure("stale_review")
            if state != "cancelled":
                return _failure("expired_draft")
            return _response(
                HTTPStatus.SEE_OTHER,
                _message_page("Import cancelled", "Return to profile creation when you are ready."),
                extra_headers=(("Location", "/account/profile"),),
            )
        try:
            review = _review_from_form(snapshot.review, form)
        except ProfileIntakeError:
            return _failure("invalid_review")
        if action == "save":
            try:
                self._processing.save(
                    reference,
                    grant,
                    expected_version=version,
                    review=review,
                    request_digest=request_digest,
                )
            except ProfileIntakeError as exc:
                failure_code = _save_error_code(exc.code)
                if failure_code == "existing_profile":
                    return _matches_redirect()
                return _failure(failure_code)
            return _matches_redirect()
        state, _updated = self._processing.vault.update(
            reference,
            grant,
            expected_version=version,
            review=review,
        )
        if state == "stale":
            return _failure("stale_review")
        if state != "updated":
            return _failure("expired_draft")
        return _response(
            HTTPStatus.SEE_OTHER,
            _message_page("Review updated", "Your ephemeral review changes are ready."),
            extra_headers=(("Location", PROFILE_INTAKE_REVIEW_ROUTE + "?" + urlencode({"draft": reference})),),
        )


def _target(target):
    if type(target) is not str or len(target) > 512:
        return None
    try:
        parsed = urlsplit(target)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc:
        return None
    return parsed


def _parse_multipart_upload(headers, body_stream):
    content_types = _header_values(headers, "content-type")
    lengths = _header_values(headers, "content-length")
    if (
        len(content_types) != 1
        or len(lengths) != 1
        or _CONTENT_LENGTH.fullmatch(lengths[0]) is None
        or _header_values(headers, "transfer-encoding")
        or body_stream is None
        or not callable(getattr(body_stream, "read", None))
    ):
        return "malformed_upload"
    length = int(lengths[0])
    if length < 1:
        return "malformed_upload"
    if length > MAX_MULTIPART_BODY_BYTES:
        return "file_too_large"
    try:
        media_type, options = parse_options_header(content_types[0])
    except Exception:
        return "malformed_upload"
    boundary = options.get(b"boundary")
    if media_type != b"multipart/form-data" or type(boundary) is not bytes or not 1 <= len(boundary) <= 70:
        return "malformed_upload"
    try:
        body = _bounded_read(body_stream, length, MAX_MULTIPART_BODY_BYTES)
        parts = _multipart_parts(body, boundary)
    except (MultipartParseError, ValueError, TypeError):
        return "malformed_upload"
    finally:
        body = None
    if (
        parts is None
        or "csrf" not in parts
        or not 1 <= len(set(parts) & set(_FILE_PARTS)) <= 2
        or not set(parts) <= _ALLOWED_MULTIPART_NAMES
    ):
        return "malformed_upload"
    csrf = parts["csrf"][1]
    if (
        type(csrf) is not bytes
        or len(csrf) > MAX_MULTIPART_METADATA_BYTES
    ):
        return "malformed_upload"
    try:
        proof = csrf.decode("ascii")
    except UnicodeError:
        return "malformed_upload"
    documents = []
    for name, kind in _FILE_PARTS.items():
        if name not in parts:
            continue
        file_headers, document = parts[name]
        if len(document) > DEFAULT_DOCUMENT_LIMITS.max_upload_bytes:
            return "file_too_large"
        declared = file_headers.get(b"content-type")
        detected = _detect_format(document)
        expected_mime = (
            _MIME_PDF
            if detected is DocumentFormat.PDF
            else _MIME_DOCX
            if detected is DocumentFormat.DOCX
            else None
        )
        if detected is None or declared != expected_mime:
            return "unsupported_format"
        if kind is DocumentKind.LINKEDIN_PROFILE_EXPORT and detected is not DocumentFormat.PDF:
            return "unsupported_format"
        try:
            documents.append(
                ProfileIntakeDocumentInput(
                    document_bytes=document,
                    document_kind=kind,
                    document_format=detected,
                )
            )
        except ProfileIntakeError:
            return "malformed_upload"
    return proof, tuple(documents)


def _bounded_read(stream, length, hard_limit):
    remaining = length
    chunks = []
    total = 0
    while remaining:
        chunk = stream.read(min(65_536, remaining))
        if type(chunk) is not bytes or not chunk or len(chunk) > remaining:
            raise ValueError("malformed_upload")
        chunks.append(chunk)
        remaining -= len(chunk)
        total += len(chunk)
        if total > hard_limit:
            raise ValueError("upload_too_large")
    return b"".join(chunks)


def _multipart_parts(body, boundary):
    parts = {}
    seen = set()
    current = {"header_name": bytearray(), "header_value": bytearray(), "headers": {}, "data": bytearray()}
    ended = {"value": False}

    def reset():
        current["header_name"] = bytearray()
        current["header_value"] = bytearray()
        current["headers"] = {}
        current["data"] = bytearray()

    def append(key, data, start, end, limit):
        value = current[key]
        value.extend(data[start:end])
        if len(value) > limit:
            raise ValueError("multipart_limit")

    def header_end():
        name = bytes(current["header_name"]).lower()
        value = bytes(current["header_value"]).strip()
        if not name or name in current["headers"]:
            raise ValueError("duplicate_header")
        current["headers"][name] = value
        current["header_name"] = bytearray()
        current["header_value"] = bytearray()

    def part_data(data, start, end):
        maximum = (
            DEFAULT_DOCUMENT_LIMITS.max_upload_bytes
            if _part_name(current["headers"]) in _FILE_PARTS
            else MAX_MULTIPART_METADATA_BYTES
        )
        append("data", data, start, end, maximum)

    def part_end():
        name = _part_name(current["headers"])
        if (
            name not in _ALLOWED_MULTIPART_NAMES
            or name in seen
            or len(seen) >= MAX_MULTIPART_PARTS
        ):
            raise ValueError("invalid_part")
        seen.add(name)
        disposition, options = parse_options_header(current["headers"].get(b"content-disposition", b""))
        if disposition != b"form-data" or options.get(b"name") != name.encode("ascii"):
            raise ValueError("invalid_part")
        filename = options.get(b"filename")
        if name in _FILE_PARTS:
            if type(filename) is not bytes or len(filename) > 255:
                raise ValueError("invalid_file_part")
            if not filename:
                if current["data"] or current["headers"].get(
                    b"content-type", b"application/octet-stream"
                ) != b"application/octet-stream":
                    raise ValueError("invalid_empty_file_part")
                return
        elif filename is not None or b"content-type" in current["headers"]:
            raise ValueError("invalid_metadata_part")
        parts[name] = (dict(current["headers"]), bytes(current["data"]))

    parser = MultipartParser(
        boundary,
        callbacks={
            "on_part_begin": reset,
            "on_header_field": lambda data, start, end: append("header_name", data, start, end, 64),
            "on_header_value": lambda data, start, end: append("header_value", data, start, end, MAX_MULTIPART_HEADER_BYTES),
            "on_header_end": header_end,
            "on_part_data": part_data,
            "on_part_end": part_end,
            "on_end": lambda: ended.__setitem__("value", True),
        },
        max_size=len(body),
        max_header_count=8,
        max_header_size=MAX_MULTIPART_HEADER_BYTES,
    )
    parser.logger = _NoContentParserLogger()
    parser.write(body)
    parser.finalize()
    if parser.state != MultipartState.END or not ended["value"]:
        raise ValueError("incomplete_multipart")
    return parts


def _part_name(headers):
    disposition = headers.get(b"content-disposition")
    if disposition is None:
        return None
    media, options = parse_options_header(disposition)
    if media != b"form-data":
        return None
    try:
        return options.get(b"name", b"").decode("ascii")
    except UnicodeError:
        return None


def _detect_format(document):
    if type(document) is not bytes or not document:
        return None
    if b"%PDF-" in document[:1024]:
        return DocumentFormat.PDF
    if document.startswith(b"PK\x03\x04"):
        return DocumentFormat.DOCX
    return None


def _parse_review_form(headers, body_stream):
    content_types = _header_values(headers, "content-type")
    lengths = _header_values(headers, "content-length")
    if (
        content_types != ("application/x-www-form-urlencoded",)
        or len(lengths) != 1
        or _CONTENT_LENGTH.fullmatch(lengths[0]) is None
        or _header_values(headers, "transfer-encoding")
        or body_stream is None
    ):
        return None
    length = int(lengths[0])
    if not 1 <= length <= MAX_REVIEW_BODY_BYTES:
        return None
    try:
        body = _bounded_read(body_stream, length, MAX_REVIEW_BODY_BYTES)
        if _INVALID_PERCENT_ESCAPE.search(body) is not None:
            return None
        text = body.decode("utf-8")
        form = parse_qs(text, keep_blank_values=True, strict_parsing=True, max_num_fields=MAX_REVIEW_FIELDS)
    except (UnicodeError, ValueError, TypeError):
        return None
    finally:
        body = None
    if any(_FIELD_NAME.fullmatch(name) is None or len(values) != 1 for name, values in form.items()):
        return None
    return form


def _review_from_form(review, form):
    expected = {"action", "version", "csrf"}
    values = []
    decisions = []
    for index, _fact in enumerate(review.facts):
        value_name = f"fact_{index}_value"
        decision_name = f"fact_{index}_decision"
        expected.update({value_name, decision_name})
        value = _single(form, value_name)
        decision = _single(form, decision_name)
        if value is None or decision is None:
            raise ProfileIntakeError("invalid_review_submission")
        values.append(value)
        decisions.append(decision)
    user_inputs = {}
    for name in review.missing_user_fields:
        key = "missing_" + name
        expected.add(key)
        value = _single(form, key)
        if value is None:
            raise ProfileIntakeError("invalid_review_submission")
        user_inputs[name] = value
    preference_model, preference_fields = _preference_model_from_form(form)
    expected.update(preference_fields)
    if set(form) != expected:
        raise ProfileIntakeError("invalid_review_submission")
    return update_editable_review(
        review,
        tuple(values),
        tuple(decisions),
        user_inputs,
        preference_model,
    )


def _preference_model_from_form(form):
    """Build the sole authoritative model from closed server-owned controls."""

    if type(form) is not dict:
        raise ProfileIntakeError("invalid_review_submission")
    catalog = profile_preference_control_catalog_v1()
    model = empty_profile_preferences_v1()
    allowed_checkbox_fields = {}
    for dimension in catalog["dimensions"]:
        path = dimension["path"]
        for choice in dimension["choices"]:
            field_name = _preference_choice_field(path, choice["code"])
            allowed_checkbox_fields[field_name] = (path, choice["code"])

    submitted = set(_PREFERENCE_COMPENSATION_FIELDS)
    for field_name in _PREFERENCE_COMPENSATION_FIELDS:
        if _single(form, field_name) is None:
            raise ProfileIntakeError("invalid_review_submission")
    for field_name, (path, code) in allowed_checkbox_fields.items():
        if field_name not in form:
            continue
        if _single(form, field_name) != "selected":
            raise ProfileIntakeError("invalid_review_submission")
        parent = model
        for part in path[:-1]:
            parent = parent[part]
        parent[path[-1]].append(code)
        submitted.add(field_name)

    kind = _single(form, "preference_compensation_minimum_kind")
    amount = _single(form, "preference_compensation_amount")
    currency = _single(form, "preference_compensation_currency")
    period = _single(form, "preference_compensation_period")
    model["compensation"] = {
        "minimum_kind": kind,
        "amount": amount or None,
        "currency": currency or None,
        "period": period or None,
    }
    try:
        return canonicalize_profile_preferences_v1(model), submitted
    except ProfilePreferenceModelError:
        raise ProfileIntakeError("invalid_review_submission") from None


def _preference_choice_field(path, code):
    name = "preference_" + "_".join((*path, code))
    if _FIELD_NAME.fullmatch(name) is None:
        raise ProfileIntakeError("invalid_review_submission")
    return name


def _preference_form_values_for_model(model):
    """Return the exact browser fields for tests and server-built replays."""

    canonical = canonicalize_profile_preferences_v1(model)
    fields = {}
    for dimension in profile_preference_control_catalog_v1()["dimensions"]:
        path = dimension["path"]
        parent = canonical
        for part in path:
            parent = parent[part]
        for code in parent:
            fields[_preference_choice_field(path, code)] = "selected"
    compensation = canonical["compensation"]
    fields.update(
        {
            "preference_compensation_minimum_kind": compensation["minimum_kind"],
            "preference_compensation_amount": compensation["amount"] or "",
            "preference_compensation_currency": compensation["currency"] or "",
            "preference_compensation_period": compensation["period"] or "",
        }
    )
    return fields


def _single(form, name):
    values = form.get(name)
    return values[0] if type(values) is list and len(values) == 1 else None


def _review_request_digest(form):
    if type(form) is not dict:
        return ""
    try:
        payload = [
            (name, values[0])
            for name, values in sorted(form.items())
            if name != "csrf" and type(values) is list and len(values) == 1
        ]
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("ascii")
        ).hexdigest()
    except (AttributeError, TypeError, UnicodeError, ValueError):
        return ""


def _upload_page(proof):
    body = f"""
    {_authenticated_navigation()}
    <section class='profile-header intake-hero'>
      <p class='eyebrow'>Create your Wahojobs profile</p>
      <h1>Start with what you already have</h1>
      <p class='hero-lede'>Add a resume, a LinkedIn PDF, or both. Wahojobs will organize a profile draft for you to review.</p>
      <p class='reassurance-line'><span aria-hidden='true'>&#10003;</span> You review every detail before anything is saved.</p>
    </section>
    <form class='profile-review-form intake-upload-form' id='profile-intake-upload' method='post' enctype='multipart/form-data' action='{PROFILE_INTAKE_ROUTE}'>
      <input type='hidden' name='csrf' value='{_safe_text(proof)}'>
      <div id='profile-upload-content'>
        <div class='upload-heading'>
          <div><p class='eyebrow'>Your documents</p><h2>Choose one or add both</h2></div>
          <p class='selection-hint'>Either option works on its own.</p>
        </div>
        <div class='upload-choice-grid' role='group' aria-label='Documents to use for your profile'>
          <label class='upload-choice'>
            <span class='upload-choice-mark' aria-hidden='true'>CV</span>
            <span class='upload-choice-copy'><strong>Resume or CV</strong><small>PDF or DOCX, up to 10 MiB</small></span>
            <input type='file' name='resume' accept='.pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document'>
          </label>
          <label class='upload-choice'>
            <span class='upload-choice-mark' aria-hidden='true'>in</span>
            <span class='upload-choice-copy'><strong>LinkedIn profile PDF</strong><small>A PDF exported by you, up to 10 MiB</small></span>
            <input type='file' name='linkedin_profile_export' accept='.pdf,application/pdf'>
          </label>
        </div>
        <details class='file-guidance'>
          <summary>File requirements</summary>
          <ul><li>Use a text-based PDF or DOCX</li><li>Scanned or image-only PDFs are not supported yet</li><li>Wahojobs does not accept or visit LinkedIn profile links</li></ul>
        </details>
        <div class='upload-actions'>
          <button type='submit'>Build my profile</button>
          <a class='secondary-link' href='/find-matches'>Create it manually instead</a>
        </div>
      </div>
      <section id='profile-building-state' class='processing-state' role='status' aria-live='polite' aria-atomic='true' tabindex='-1' hidden>
        <div class='processing-mark' aria-hidden='true'><span></span><span></span><span></span></div>
        <p class='eyebrow'>Building your profile</p>
        <h2>Preparing a draft that is easy to review</h2>
        <p class='processing-lede'>Keep this page open while Wahojobs reads and organizes your documents.</p>
        <ol class='processing-steps' aria-label='What Wahojobs is preparing'>
          <li>Reading your documents</li>
          <li>Organizing experience and skills</li>
          <li>Preparing your review</li>
        </ol>
        <p class='reassurance-line'><span aria-hidden='true'>&#10003;</span> Nothing is saved before you review and confirm it.</p>
      </section>
    </form>
    <script>{_PROCESSING_SCRIPT}</script>
    """
    return _page("Create your profile", body)


def _review_page(reference, snapshot, csrf_secret, *, save_enabled=False):
    fact_fields = []
    for index, fact in enumerate(snapshot.review.facts):
        label = fact.review_field.replace("_", " ").title()
        raw_value = review_value_for_form(fact.value)
        if fact.suggested:
            choice = (
                f"<label class='decision-field'><span>Use this suggestion?</span><select name='fact_{index}_decision'>"
                f"<option value='pending'{' selected' if fact.decision == 'pending' else ''}>Choose whether to use this suggestion</option>"
                f"<option value='accept'{' selected' if fact.decision == 'accept' else ''}>Accept suggestion</option>"
                f"<option value='reject'{' selected' if fact.decision == 'reject' else ''}>Do not use</option></select></label>"
            )
            badge = "Suggested from your document — please confirm"
        else:
            choice = (
                f"<label class='decision-field'><span>Keep this detail?</span><select name='fact_{index}_decision'>"
                f"<option value='keep'{' selected' if fact.decision == 'keep' else ''}>Keep</option>"
                f"<option value='remove'{' selected' if fact.decision == 'remove' else ''}>Remove</option></select></label>"
            )
            badge = "Document-supported prefill"
        if fact.conflict_group is not None:
            badge = "Sources disagree — please confirm"
        source_label = _review_source_label(fact)
        value_control = _review_fact_value_control(index, fact, raw_value, label)
        card = (
            f"<article class='profile-group fact-card'><p class='fact-meta'>{_safe_text(badge)} · {_safe_text(source_label)}</p>"
            f"{value_control}{choice}</article>"
        )
        fact_fields.append((fact, card))
    missing = []
    existing_inputs = dict(snapshot.review.user_inputs)
    for name in snapshot.review.missing_user_fields:
        missing.append(
            f"<label class='review-field'>{_safe_text(name.replace('_', ' ').title())}"
            f"<input name='missing_{_safe_text(name)}' value='{_safe_text(existing_inputs.get(name, ''))}' maxlength='512'></label>"
        )
    primary_action = "save" if save_enabled else "update"
    primary_proof = profile_intake_csrf_proof(
        csrf_secret,
        primary_action,
        draft_reference=reference,
        version=snapshot.version,
    )
    cancel_proof = profile_intake_csrf_proof(
        csrf_secret,
        "cancel",
        draft_reference=reference,
        version=snapshot.version,
    )
    issue_note = (
        "<p class='intake-callout'>Some document details may be ambiguous or conflicting; "
        "review them carefully.</p>"
        if snapshot.review.issue_count
        else ""
    )
    target = PROFILE_INTAKE_REVIEW_ROUTE + "?" + urlencode({"draft": reference})
    primary_label = (
        "Find my matches"
        if save_enabled
        else "Update temporary review"
    )
    persistence_note = (
        "Your profile is saved only when you choose Find my matches."
        if save_enabled
        else "You can update this preview, but it cannot be saved here."
    )
    found_cards = "".join(
        card
        for fact, card in fact_fields
        if not fact.suggested and fact.conflict_group is None
    )
    suggestion_cards = "".join(
        card
        for fact, card in fact_fields
        if fact.suggested and fact.conflict_group is None
    )
    if not suggestion_cards:
        suggestion_cards = (
            "<p class='empty-inline'>No suggestions need your confirmation.</p>"
        )
    conflict_cards = "".join(
        card for fact, card in fact_fields if fact.conflict_group is not None
    )
    conflict_section = (
        "<div class='review-subsection'><h3>Sources disagree — please confirm</h3>"
        "<p class='muted'>Choose at most one value for each disagreement, edit it "
        "if needed, or leave the alternatives out.</p>"
        f"<div class='profile-grid'>{conflict_cards}</div></div>"
        if conflict_cards
        else ""
    )
    missing_section = (
        "<div class='review-subsection user-details'><h3>Information you still need "
        "to provide</h3><p class='muted'>These details need your answer and stay "
        "separate from preferences you may choose to relax.</p>"
        f"<div class='review-grid'>{''.join(missing)}</div></div>"
        if missing
        else ""
    )
    body = f"""
    {_authenticated_navigation()}
    <section class='profile-header intake-hero intake-review-hero'><p class='eyebrow'>Your Wahojobs profile draft</p><h1>Review it and make it yours</h1>
      <p class='hero-lede'>We organized what your documents say. You decide what belongs in your profile.</p>
      <p class='reassurance-line'><span aria-hidden='true'>&#10003;</span> Nothing is saved until you finish.</p>
      <nav class='review-progress' aria-label='Profile review steps'><ol>
        <li><a href='#review-found'><span>1</span>What we found</a></li>
        <li><a href='#review-suggestions'><span>2</span>Confirm suggestions</a></li>
        <li><a href='#review-preferences'><span>3</span>What you want</a></li>
        <li><a href='#review-finish'><span>4</span>Find matches</a></li>
      </ol></nav>
    </section>
    {issue_note}
    <form class='profile-review-form intake-review-form' method='post' action='{target}'>
      <input type='hidden' name='action' value='{primary_action}'><input type='hidden' name='version' value='{snapshot.version}'><input type='hidden' name='csrf' value='{primary_proof}'>
      <section class='review-section' id='review-found' aria-labelledby='review-found-title'><div class='section-heading'><p class='eyebrow'>Step 1 of 4</p><h2 id='review-found-title'>What we found</h2><p>Check the details taken directly from your documents. Edit or remove anything that is not right.</p></div><div class='profile-grid'>{found_cards}</div></section>
      <section class='review-section' id='review-suggestions' aria-labelledby='review-suggestions-title'><div class='section-heading'><p class='eyebrow'>Step 2 of 4</p><h2 id='review-suggestions-title'>Confirm our suggestions</h2><p>These classifications can make your profile more useful. See every available choice and select what feels accurate.</p></div><div class='profile-grid'>{suggestion_cards}</div>{conflict_section}</section>
      <section class='review-section' id='review-preferences' aria-labelledby='review-preferences-title'><div class='section-heading'><p class='eyebrow'>Step 3 of 4</p><h2 id='review-preferences-title'>What are you looking for?</h2><p>Choose all the options you would consider. Each group is separate, so choices such as freelance and full-time can work together.</p></div>{_render_preference_controls(snapshot.review.preference_model)}
        {missing_section}</section>
      <section class='review-section finish-section' id='review-finish' aria-labelledby='review-finish-title'><div class='finish-panel'><p class='eyebrow'>Step 4 of 4</p><h2 id='review-finish-title'>Review &amp; find matches</h2><p>When everything looks right, see the opportunities that fit the profile you confirmed. You can update your profile later.</p><div class='finish-actions'><button type='submit'>{primary_label}</button><span class='muted'>{persistence_note}</span></div></div></section>
    </form>
    <form class='intake-cancel-form' method='post' action='{target}'>
      <input type='hidden' name='action' value='cancel'><input type='hidden' name='version' value='{snapshot.version}'><input type='hidden' name='csrf' value='{cancel_proof}'><button class='button-quiet' type='submit'>Discard this draft</button>
    </form>
    """
    return _page("Review your profile", body)


def _review_fact_value_control(index, fact, raw_value, label):
    spec = _FIELD_SPECS.get(fact.field_path)
    if spec is None or spec.kind != "enum" or spec.multiple:
        return (
            f"<label class='review-field'>{_safe_text(label)}"
            f"<input name='fact_{index}_value' value='{_safe_text(raw_value)}' maxlength='512'></label>"
        )
    choices = []
    for option in sorted(spec.allowed):
        option_id = f"fact-{index}-{option}"
        option_label = option.replace("_", " ").title()
        description = _CLASSIFICATION_DESCRIPTIONS.get(
            option,
            f"Use the {_safe_text(option_label)} classification.",
        )
        choices.append(
            f"<label class='choice-card' for='{_safe_text(option_id)}'>"
            f"<input id='{_safe_text(option_id)}' type='radio' name='fact_{index}_value' "
            f"value='{_safe_text(option)}'{' checked' if option == raw_value else ''}>"
            f"<span><strong>{_safe_text(option_label)}</strong><small>{_safe_text(description)}</small></span></label>"
        )
    return (
        f"<fieldset class='choice-fieldset'><legend>{_safe_text(label)}</legend>"
        "<p class='selection-hint'>Choose one</p>"
        f"<div class='choice-grid'>{''.join(choices)}</div>"
        "</fieldset>"
    )


def _render_preference_controls(model):
    canonical = canonicalize_profile_preferences_v1(model)
    catalog = profile_preference_control_catalog_v1()
    sections = []
    for dimension in catalog["dimensions"]:
        parent = canonical
        for part in dimension["path"]:
            parent = parent[part]
        selected = set(parent)
        choices = []
        definitions = []
        for choice in dimension["choices"]:
            field_name = _preference_choice_field(
                dimension["path"], choice["code"]
            )
            choice_id = field_name.replace("_", "-")
            choices.append(
                f"<label class='choice-card' for='{choice_id}'>"
                f"<input id='{choice_id}' type='checkbox' name='{field_name}' value='selected'"
                f"{' checked' if choice['code'] in selected else ''}>"
                f"<span>{_safe_text(choice['label'])}</span></label>"
            )
            definitions.append(
                f"<div><dt>{_safe_text(choice['label'])}</dt>"
                f"<dd>{_safe_text(choice['description'])}</dd></div>"
            )
        help_id = "preference-help-" + dimension["id"].replace(".", "-").replace("_", "-")
        sections.append(
            f"<fieldset class='preference-group' aria-describedby='{help_id}'>"
            f"<legend>{_safe_text(dimension['title'])}</legend><span class='selection-hint'>Choose all that apply</span>"
            f"<p class='muted' id='{help_id}'>{_safe_text(dimension['help'])} Leave every option blank if you are open to all.</p>"
            f"<div class='choice-grid'>{''.join(choices)}</div>"
            f"<details><summary>Understand these choices</summary>"
            f"<dl class='choice-definitions'>{''.join(definitions)}</dl></details></fieldset>"
        )

    compensation = canonical["compensation"]
    kind_choices = []
    for choice in catalog["compensation"]["minimum_kinds"]:
        choice_id = "compensation-kind-" + choice["code"]
        kind_choices.append(
            f"<label class='choice-card' for='{choice_id}'>"
            f"<input id='{choice_id}' type='radio' name='preference_compensation_minimum_kind' "
            f"value='{choice['code']}'{' checked' if choice['code'] == compensation['minimum_kind'] else ''}>"
            f"<span><strong>{_safe_text(choice['label'])}</strong><small>{_safe_text(choice['description'])}</small></span></label>"
        )
    currency_options = ["<option value=''>Choose currency</option>"]
    for currency in catalog["compensation"]["currencies"]:
        currency_options.append(
            f"<option value='{currency}'{' selected' if currency == compensation['currency'] else ''}>{currency}</option>"
        )
    period_options = ["<option value=''>Choose period</option>"]
    for period in catalog["compensation"]["periods"]:
        period_options.append(
            f"<option value='{period['code']}'{' selected' if period['code'] == compensation['period'] else ''}>{_safe_text(period['label'])}</option>"
        )
    sections.append(
        "<fieldset class='preference-group compensation-group'><legend>Expected compensation</legend>"
        "<p class='muted'>Set a minimum only if you have one. Amounts use the currency and time period you choose, with no automatic conversion.</p>"
        f"<div class='choice-grid'>{''.join(kind_choices)}</div>"
        "<div class='compensation-guide' aria-label='Preferred and strict minimum explained'>"
        "<p><strong>Preferred minimum</strong><span>Your target. You may choose to relax it to see more opportunities.</span></p>"
        "<p><strong>Strict minimum</strong><span>Your firm floor. It will not be presented as something to relax.</span></p>"
        "</div>"
        "<div class='review-grid'>"
        f"<label class='review-field'>Amount<input name='preference_compensation_amount' inputmode='decimal' pattern='[0-9]{{1,18}}(?:\\.[0-9]{{1,2}})?' value='{_safe_text(compensation['amount'] or '')}' maxlength='21'></label>"
        f"<label class='review-field'>Currency<select name='preference_compensation_currency'>{''.join(currency_options)}</select></label>"
        f"<label class='review-field'>Period<select name='preference_compensation_period'>{''.join(period_options)}</select></label>"
        "</div></fieldset>"
    )
    return "".join(sections)


def _review_source_label(fact):
    kinds = {item.document_kind for item in fact.source_attributions}
    if kinds == {DocumentKind.RESUME, DocumentKind.LINKEDIN_PROFILE_EXPORT}:
        return "Found in both"
    if kinds == {DocumentKind.RESUME}:
        return "Found in your resume"
    if kinds == {DocumentKind.LINKEDIN_PROFILE_EXPORT}:
        return "Found in your LinkedIn profile"
    return "Document source unavailable"


def _processing_error_code(code):
    return {
        "upload_too_large": "file_too_large",
        "encrypted_pdf": "encrypted_pdf",
        "no_extractable_text": "no_readable_text",
        "invalid_pdf": "malformed_document",
        "invalid_docx": "malformed_document",
        "unsafe_pdf": "malformed_document",
        "profile_extraction_unavailable": "extraction_unavailable",
        "profile_intake_in_flight": "in_flight",
        "ai_import_profile_exists": "existing_profile",
        "ai_import_entitlement_consumed": "existing_profile",
        "ai_import_entitlement_reserved": "import_reserved",
        "ai_import_schema_unavailable": "durable_unavailable",
        "durable_intake_unavailable": "durable_unavailable",
    }.get(code, "extraction_unavailable")


def _preflight_error_code(state):
    return {
        "profile_exists": "existing_profile",
        "entitlement_consumed": "existing_profile",
        "entitlement_reserved": "import_reserved",
    }.get(state, "durable_unavailable")


def _save_error_code(code):
    return {
        "stale_review": "stale_review",
        "ai_import_review_unresolved": "unresolved_review",
        "ai_import_review_invalid": "invalid_review",
        "invalid_review_submission": "invalid_review",
        "ai_import_reservation_expired": "expired_draft",
        "ai_import_reservation_mismatch": "invalid_draft",
        "ai_import_entitlement_consumed": "existing_profile",
        "ai_import_profile_exists": "existing_profile",
        "ai_import_ownership_stale": "authorization_denied",
        "ai_import_idempotency_conflict": "stale_review",
        "ai_import_temporary_contention": "unavailable",
        "ai_import_schema_unavailable": "durable_unavailable",
        "durable_intake_unavailable": "durable_unavailable",
        "draft_expired": "expired_draft",
    }.get(code, "unavailable")


def _matches_redirect():
    return _response(
        HTTPStatus.SEE_OTHER,
        _message_page("Profile ready", "Continue to your matches."),
        extra_headers=(("Location", "/find-matches"),),
    )


def _failure(code):
    status, title, message = {
        "invalid_request": (400, "Upload request unavailable", "This request is not valid."),
        "malformed_upload": (400, "Upload could not be read", "Choose one valid PDF or DOCX and try again."),
        "invalid_review": (400, "Review could not be updated", "Check the values and try again."),
        "authentication_required": (401, "Authentication required", "Sign in to continue."),
        "csrf_denied": (403, "Request rejected", "Reload the page and try again."),
        "authorization_denied": (404, "Page not found", "This page is not available."),
        "not_found": (404, "Page not found", "This page is not available."),
        "invalid_draft": (400, "Draft request unavailable", "Start the import again."),
        "expired_draft": (410, "Draft expired", "This temporary draft has expired. Start again."),
        "stale_review": (409, "Review changed", "Reload the draft before submitting another change."),
        "unresolved_review": (409, "Review needs confirmation", "Resolve every suggestion or source disagreement before saving."),
        "existing_profile": (409, "Profile already exists", "Continue to your existing profile and matches."),
        "import_reserved": (409, "Import already in progress", "Finish or cancel the current import before starting another."),
        "durable_unavailable": (503, "Profile saving unavailable", "AI profile saving is not available in this environment. You can still create your profile manually."),
        "file_too_large": (413, "Document too large", "Choose a document no larger than 10 MiB."),
        "unsupported_format": (415, "Unsupported document", "Use a text-based PDF or DOCX. LinkedIn exports must be PDF."),
        "malformed_document": (422, "Document could not be read", "Export a fresh text-based PDF or DOCX and try again."),
        "encrypted_pdf": (422, "Encrypted PDF not supported", "Remove the PDF password and try again."),
        "no_readable_text": (422, "No readable text found", "Scanned and image-only PDFs are not supported yet."),
        "extraction_unavailable": (503, "AI extraction unavailable", "Try again later, or create your profile manually."),
        "in_flight": (409, "Import already processing", "Wait for the current import to finish."),
        "unavailable": (503, "Profile intake unavailable", "Try again later, or create your profile manually."),
    }.get(code, (503, "Profile intake unavailable", "Try again later, or create your profile manually."))
    return _response(HTTPStatus(status), _message_page(title, message))


def _message_page(title, message):
    return _page(
        title,
        _authenticated_navigation()
        + f"<section class='empty'><h1>{_safe_text(title)}</h1><p>{_safe_text(message)}</p>"
        + "<p><a href='/account/profile/intake'>Start again</a> · <a href='/find-matches'>Create manually</a></p></section>",
    )

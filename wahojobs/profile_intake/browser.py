"""Private browser boundary for one authenticated AI-assisted profile draft."""

from __future__ import annotations

from http import HTTPStatus
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
            proof = profile_intake_csrf_proof(csrf_secret, "upload")
            return _form_page_response(HTTPStatus.OK, _upload_page(proof))
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
        snapshot = self._processing.vault.get(reference, grant)
        if snapshot is None:
            return _failure("expired_draft")
        if method in {"GET", "HEAD"}:
            return _form_page_response(
                HTTPStatus.OK,
                _review_page(reference, snapshot, csrf_secret),
            )
        form = _parse_review_form(headers, body_stream)
        if form is None:
            return _failure("invalid_review")
        action = _single(form, "action")
        raw_version = _single(form, "version")
        proof = _single(form, "csrf")
        if action not in {"update", "cancel"} or raw_version is None or not raw_version.isdigit():
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
        if action == "cancel":
            if set(form) != {"action", "version", "csrf"}:
                return _failure("invalid_review")
            state = self._processing.vault.cancel(reference, grant, expected_version=version)
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
    if set(form) != expected:
        raise ProfileIntakeError("invalid_review_submission")
    return update_editable_review(review, tuple(values), tuple(decisions), user_inputs)


def _single(form, name):
    values = form.get(name)
    return values[0] if type(values) is list and len(values) == 1 else None


def _upload_page(proof):
    body = f"""
    {_authenticated_navigation()}
    <section class='profile-header'>
      <p class='eyebrow'>Optional AI-assisted profile</p>
      <h1>Create your profile faster</h1>
      <p>Upload either document or both. LinkedIn means a PDF you exported; we do not accept or scrape LinkedIn URLs.</p>
      <ul><li>Maximum 10 MiB per document</li><li>Text-based documents only</li><li>Scanned or image-only PDFs are not supported yet</li></ul>
    </section>
    <form class='profile-review-form' method='post' enctype='multipart/form-data' action='{PROFILE_INTAKE_ROUTE}'>
      <input type='hidden' name='csrf' value='{_safe_text(proof)}'>
      <label class='review-field'>Resume or CV <span class='muted'>PDF or DOCX</span><input type='file' name='resume' accept='.pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document'></label>
      <label class='review-field'>LinkedIn profile <span class='muted'>LinkedIn profile PDF exported by you</span><input type='file' name='linkedin_profile_export' accept='.pdf,application/pdf'></label>
      <p class='review-actions'><button type='submit'>Build review draft</button><a href='/find-matches'>Create profile manually</a></p>
    </form>
    """
    return _page("AI-assisted profile", body)


def _review_page(reference, snapshot, csrf_secret):
    fact_fields = []
    for index, fact in enumerate(snapshot.review.facts):
        label = fact.review_field.replace("_", " ").title()
        value = _safe_text(review_value_for_form(fact.value))
        if fact.suggested:
            choice = (
                f"<select name='fact_{index}_decision'>"
                f"<option value='accept'{' selected' if fact.decision == 'accept' else ''}>Accept suggestion</option>"
                f"<option value='reject'{' selected' if fact.decision in {'pending', 'reject'} else ''}>Do not use</option></select>"
            )
            badge = "Suggested from your document — please confirm"
        else:
            choice = (
                f"<select name='fact_{index}_decision'>"
                f"<option value='keep'{' selected' if fact.decision == 'keep' else ''}>Keep</option>"
                f"<option value='remove'{' selected' if fact.decision == 'remove' else ''}>Remove</option></select>"
            )
            badge = "Document-supported prefill"
        if fact.conflict_group is not None:
            badge = "Sources disagree — please confirm"
        source_label = _review_source_label(fact)
        card = (
            f"<div class='profile-group'><p class='eyebrow'>{_safe_text(badge)} · {_safe_text(source_label)}</p>"
            f"<label class='review-field'>{_safe_text(label)}<input name='fact_{index}_value' value='{value}' maxlength='512'></label>{choice}</div>"
        )
        fact_fields.append((fact, card))
    missing = []
    existing_inputs = dict(snapshot.review.user_inputs)
    for name in snapshot.review.missing_user_fields:
        missing.append(
            f"<label class='review-field'>{_safe_text(name.replace('_', ' ').title())}"
            f"<input name='missing_{_safe_text(name)}' value='{_safe_text(existing_inputs.get(name, ''))}' maxlength='512'></label>"
        )
    update_proof = profile_intake_csrf_proof(
        csrf_secret,
        "update",
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
        "<p class='muted'>Some document details may be ambiguous or conflicting; "
        "review them carefully.</p>"
        if snapshot.review.issue_count
        else ""
    )
    target = PROFILE_INTAKE_REVIEW_ROUTE + "?" + urlencode({"draft": reference})
    body = f"""
    {_authenticated_navigation()}
    <section class='profile-header'><p class='eyebrow'>Private, temporary review</p><h1>Review your profile draft</h1>
      <p>Correct or remove prefills, and choose whether to use suggestions. Nothing on this page has been saved to your profile.</p></section>
    {issue_note}
    <form class='profile-review-form' method='post' action='{target}'>
      <input type='hidden' name='action' value='update'><input type='hidden' name='version' value='{snapshot.version}'><input type='hidden' name='csrf' value='{update_proof}'>
      <section class='review-section'><h2>Document-supported prefills</h2><div class='profile-grid'>{''.join(card for fact, card in fact_fields if not fact.suggested and fact.conflict_group is None)}</div></section>
      <section class='review-section'><h2>Suggestions requiring confirmation</h2><p class='muted'>These are suggestions, not facts or matcher decisions.</p><div class='profile-grid'>{''.join(card for fact, card in fact_fields if fact.suggested and fact.conflict_group is None)}</div></section>
      <section class='review-section'><h2>Sources disagree — please confirm</h2><p class='muted'>Choose at most one value for each disagreement, edit it if needed, or reject the alternatives.</p><div class='profile-grid'>{''.join(card for fact, card in fact_fields if fact.conflict_group is not None)}</div></section>
      <section class='review-section'><h2>Information you still need to provide</h2><p class='muted'>Historical resume details are not treated as your current preferences.</p><div class='review-grid'>{''.join(missing)}</div></section>
      <p class='review-actions'><button type='submit'>Update temporary review</button><span class='muted'>Profile saving will be added in a future step.</span></p>
    </form>
    <form class='profile-review-form' method='post' action='{target}'>
      <input type='hidden' name='action' value='cancel'><input type='hidden' name='version' value='{snapshot.version}'><input type='hidden' name='csrf' value='{cancel_proof}'><button type='submit'>Cancel import</button>
    </form>
    """
    return _page("Review AI profile draft", body)


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
    }.get(code, "extraction_unavailable")


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
